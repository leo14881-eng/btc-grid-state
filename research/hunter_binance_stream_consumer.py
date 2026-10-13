"""Existing-only frozen Binance reads; explicit UNKNOWN and no network fallback."""
import copy
import datetime as dt
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from threading import Lock

try:
    from research.hunter_binance_stream_store import Store,topic,roster,roster_hash,CACHE_SOURCE,FRESH_MS
    from research.hunter_signal_provenance import capture,hash_value,validate_cache_packet,CACHE_FIELDS
except ModuleNotFoundError as exc:
    if exc.name!='research': raise
    from hunter_binance_stream_store import Store,topic,roster,roster_hash,CACHE_SOURCE,FRESH_MS
    from hunter_signal_provenance import capture,hash_value,validate_cache_packet,CACHE_FIELDS


def selected():
    source=os.getenv('HUNTER_BINANCE_KLINE_SOURCE','REST')
    if source not in ('REST','SPOT_CACHE'): raise ValueError('BINANCE_KLINE_SELECTOR_UNKNOWN')
    if source=='SPOT_CACHE' and (sys.platform!='linux' or os.getenv('GITHUB_ACTIONS')=='true'
            or os.getenv('HUNTER_SERVER_ISOLATED_CHECKOUT')!='1'):
        raise ValueError('BINANCE_CACHE_REQUIRES_ISOLATED_LINUX_SERVER')
    return source


CONTROL_HEALTH_CONTRACT = 'binance_health_control_v2'
CONTROL_CONTRACT = 'binance_control_v1'


class CacheReadError(ValueError):
    """Keep the rejected snapshot's identity for a read-only acceptance receipt."""
    def __init__(self,reason,generation=None):
        super().__init__(reason)
        self.generation=copy.deepcopy(generation or {})

    @classmethod
    def _from_frozen(cls,reason,frozen):
        # generation returns an owned audit copy from the cached JSON, or keeps
        # deepcopy's graph/type semantics for snapshots outside that fast path.
        error=cls(reason)
        error.generation=frozen.generation
        return error


def _encode_json_tree(value):
    """Check once; preserve deepcopy semantics for every non-JSON graph.

    JSON would coerce tuple/key/subclass types and duplicate aliased containers.
    Those inputs retain the existing deepcopy path, as do cycles and nonfinite
    numbers. Real cache generations contain plain, unaliased JSON containers.
    """
    pending=[value];seen=set()
    while pending:
        item=pending.pop();kind=type(item)
        if kind is dict or kind is list:
            identity=id(item)
            if identity in seen:return None
            seen.add(identity)
            if kind is dict:
                if any(type(key) is not str for key in item):return None
                pending.extend(item.values())
            else:pending.extend(item)
        elif kind is float:
            if not math.isfinite(item):return None
        elif kind not in (str,int,bool,type(None)):
            return None
    try:
        # The explicit walk above has already rejected cycles, aliased
        # containers, non-JSON types and nonfinite floats. Avoid making the
        # JSON C encoder repeat the circular-container check for every node.
        # Output bytes, ownership, proof hashes and delivery fences stay intact.
        return json.dumps(value,ensure_ascii=True,allow_nan=False,
                          separators=(',',':'),check_circular=False)
    except (TypeError,ValueError,OverflowError,RecursionError):
        return None


class Frozen:
    def __init__(self,generation,checked_at=None,*,reader=None,fence=None,names=None,
                 clock=lambda:time.time_ns()//1_000_000,started_at=None):
        if generation.get('status')!='READY':
            raise CacheReadError('BINANCE_CACHE_UNKNOWN:'+str(generation.get('reason','UNAVAILABLE')),generation)
        # Private owned snapshot and separately owned exports. Cache the JSON
        # encoding once so repeated exports do not traverse the Python tree.
        self._snapshot_json=_encode_json_tree(generation)
        self._generation=(json.loads(self._snapshot_json) if self._snapshot_json is not None
                          else copy.deepcopy(generation))
        self._reader=reader;self._fence=copy.deepcopy(fence);self._clock=clock
        self._names=tuple(sorted(names if names is not None else generation['windows']))
        self._started_at=started_at
        self.checked_at_ms=checked_at
        if reader is not None:
            from research.hunter_binance_stream_store import content_hash
            unsigned=dict(self._generation);digest=unsigned.pop('generation_sha256',None)
            if digest!=content_hash(unsigned):raise ValueError('BINANCE_FINAL_GENERATION_HASH')
            if not isinstance(self._fence,dict):raise ValueError('BINANCE_CACHE_CONTROL_FENCE_REQUIRED')
            self._fence['generation_sha256']=digest
        if (generation.get('health_contract')==CONTROL_HEALTH_CONTRACT
                or generation.get('control_contract')==CONTROL_CONTRACT) and (reader is None or fence is None):
            raise ValueError('BINANCE_CACHE_CONTROL_FENCE_REQUIRED')
        if set(self._names)!=set(generation['windows']):raise ValueError('BINANCE_CACHE_SCOPE_MISMATCH')
        self._validate_at(time.time_ns()//1_000_000 if checked_at is None else checked_at,self._names)

    @property
    def generation(self):
        # Callers can inspect a snapshot, but cannot mutate the version used by
        # later rows/packet deliveries or the independent revocation fence.
        return (json.loads(self._snapshot_json) if self._snapshot_json is not None
                else copy.deepcopy(self._generation))

    @property
    def fence_sha256(self):
        if self._fence is None:return None
        from research.hunter_binance_stream_store import content_hash
        return content_hash(self._fence)

    @property
    def source_head_sha(self):
        control=(self._fence or {}).get('control') or {}
        return control.get('source_head',self._generation.get('source_head_sha'))

    def _validate_at(self,now,names):
        generation=self._generation
        if self._reader is not None:
            if (type(now) is not int or not 0<=now-generation['generation_end_ms']<=FRESH_MS
                    or self._started_at is not None and now<self._started_at):
                raise ValueError('BINANCE_CACHE_READ_FINISHED_STALE_OR_CLOCK_ROLLBACK')
        if generation.get('health_contract'):
            from research.hunter_binance_health_store import valid_proof,CONTRACT
            from research.hunter_binance_stream_store import content_hash
            if generation['health_contract'] not in (CONTRACT,'binance_health_v1',CONTROL_HEALTH_CONTRACT):
                raise ValueError('BINANCE_CACHE_VALIDITY_CONTRACT')
            meta=dict(epoch=generation['epoch'],scope_hash=generation['manifest_sha256'],
                      roster_hash=generation['roster_hash'],health_epoch_started=generation['epoch_started_ms'])
            for name in names:
                window=generation['windows'][name]
                evidence=window.get('validity',{})
                payload={k:v for k,v in window.items() if k!='validity'}
                if (not valid_proof(evidence,'kline',name,meta,now)
                        or not valid_proof(window.get('instrument_validity',{}),'instrument',name,meta,now)
                        or evidence['generation_end_ms']!=generation['generation_end_ms']
                        or evidence['window_sha256']!=content_hash(payload)):
                    raise ValueError('BINANCE_CACHE_VALIDITY_MISSING_EXPIRED_OR_MISMATCHED')

    def check_delivery(self,names=None,*,started=None):
        """Revalidate after preparing a result, immediately before handing it out.

        Control checks cover the entire original scope, including revocation
        ABA. Ordinary market updates leave that fence valid. Local proof checks
        need only visit the delivered topics; the initial read checked all of
        them. No market history is frozen again.
        """
        if self._reader is None:return self.checked_at_ms
        started=self._clock() if started is None else started
        try:
            if self._reader.finish_read(self._generation,list(self._names),started,self._fence) is not True:
                raise ValueError('BINANCE_CACHE_CONTROL_FENCE_REJECTED')
            finished=self._clock()
            if finished<started:raise ValueError('BINANCE_CACHE_READ_FINISHED_STALE_OR_CLOCK_ROLLBACK')
            self._validate_at(finished,self._names if names is None else names)
            self.checked_at_ms=finished
            return finished
        except (ValueError,RuntimeError) as error:
            raise CacheReadError._from_frozen(str(error),self) from error

    def _rows(self,symbol,interval,count=None):
        name=topic(symbol,interval)
        rows=self._generation['windows'][name]['rows']
        return copy.deepcopy(rows if count is None else rows[-count:])

    def _packet(self,symbol,interval,count=None):
        name=topic(symbol,interval);g=self._generation;window=g['windows'][name]
        rows=copy.deepcopy(window['rows'] if count is None else window['rows'][-count:])
        end=dt.datetime.fromtimestamp(g['generation_end_ms']/1000,dt.timezone.utc)
        out=capture(rows,symbol,interval,end)
        out.update(source=CACHE_SOURCE,cache={k:g[k] for k in CACHE_FIELDS},
                   transport=copy.deepcopy(window['transport'][-len(rows):]))
        for bar,transport in zip(out['bars'],out['transport']): bar['confirmed']=transport['confirm']
        out['content_hash']=hash_value(out['bars'])
        validate_cache_packet(out,symbol,interval,len(rows))
        return out

    def rows(self,symbol,interval,count=None):
        out=self._rows(symbol,interval,count)
        self.check_delivery([topic(symbol,interval)])
        return out

    def packet(self,symbol,interval,count=None):
        out=self._packet(symbol,interval,count)
        self.check_delivery([topic(symbol,interval)])
        return out

    @contextmanager
    def _formula(self):
        """Private joined-worker scope; no result may escape before its fence.

        Only rolling/micro use this scope. Public rows/packet retain their own
        full delivery fence. Immutable local copies still check proof/hash and
        usage-time expiry; the complete original scope is fenced after workers
        join and before the formula returns. Monitor use guards remain separate.
        """
        if self._reader is None:
            yield self
            return
        view=_FormulaView(self)
        try:
            yield view
            view.finish()
        finally:
            view.close()


class _FormulaView:
    """An internal calculation view, never a consumer delivery interface."""
    def __init__(self,frozen):
        self._frozen=frozen;self._lock=Lock();self._active=True;self._failure=None

    def close(self):
        with self._lock:self._active=False

    def _deliver(self,prepare,symbol,interval,count):
        with self._lock:
            if not self._active:raise ValueError('BINANCE_FORMULA_SCOPE_CLOSED')
            if self._failure is not None:raise self._failure
        frozen=self._frozen;started=frozen._clock()
        try:
            out=prepare(symbol,interval,count)
            finished=frozen._clock()
            if finished<started:raise ValueError('BINANCE_CACHE_READ_FINISHED_STALE_OR_CLOCK_ROLLBACK')
            frozen._validate_at(finished,[topic(symbol,interval)])
            return out
        except (ValueError,RuntimeError) as error:
            # Formula workers deliberately catch errors. Keep invalid cache
            # evidence sticky so that they cannot turn a failed check into a
            # partial successful result, even if a later clock moves back.
            failure=CacheReadError._from_frozen(str(error),frozen)
            with self._lock:
                if self._failure is None:self._failure=failure
                failure=self._failure
            raise failure from error

    def rows(self,symbol,interval,count=None):
        return self._deliver(self._frozen._rows,symbol,interval,count)

    def packet(self,symbol,interval,count=None):
        return self._deliver(self._frozen._packet,symbol,interval,count)

    def finish(self):
        with self._lock:
            self._active=False;failure=self._failure
        if failure is not None:raise failure
        self._frozen.check_delivery()


def read(names,now,reader=None,clock=lambda:time.time_ns()//1_000_000,expected_roster=None):
    cut=int(now.timestamp()*1000);started=clock();reader=reader or Store.reader()
    names=sorted(names)
    fence=reader.capture_fence(names)
    generation=reader.freeze(names,cut,started,expected_roster)
    frozen=Frozen(generation,checked_at=started,reader=reader,fence=fence,names=names,
                  clock=clock,started_at=started)
    frozen.check_delivery(started=started)
    return frozen


def signal_inputs(scan,symbols):
    if selected()=='REST': return None
    now=dt.datetime.now(dt.timezone.utc)
    expected=roster_hash(roster(scan,now))
    names=sorted(topic(s,i) for s in symbols for i in ('15m','1h'))
    return read(names,now,expected_roster=expected)


def management_inputs(symbols,now):
    """One frozen set for the original holdings batch plus its BTC benchmark.

    Monitor runs every five minutes; its holdings are independent of Discovery's
    30-minute scan admission age. The cache still checks every requested topic,
    its full stored roster binding, bar closure, source and 90-second freshness.
    """
    if selected()=='REST': return None
    names=sorted(topic(s,i) for s in set(symbols)|{'BTCUSDT'} for i in ('15m','1h'))
    return read(names,now)


def window(symbol,interval,now):
    if selected()=='REST': return None
    return read([topic(symbol,interval)],now)
