"""Binance-only rolling cache; reuse durable SQLite coordination, never Bybit data."""
import datetime as dt
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

try:
    from research.hunter_binance_market_control import MarketControlMixin, HEALTH_CONTROL_CONTRACT, CONTROL_CONTRACT
    from research.hunter_bybit_stream_store import (
        Store as CoordinationStore, Budget as SharedBudget, budget_state, budget_time, request_gate, content_hash,
        FRESH_MS, HISTORY_MS, MAX_REVISIONS)
except ModuleNotFoundError as exc:
    if exc.name != 'research': raise
    from hunter_binance_market_control import MarketControlMixin, HEALTH_CONTROL_CONTRACT, CONTROL_CONTRACT
    from hunter_bybit_stream_store import (
        Store as CoordinationStore, Budget as SharedBudget, budget_state, budget_time, request_gate, content_hash,
        FRESH_MS, HISTORY_MS, MAX_REVISIONS)

STATE_PATH = Path('/var/lib/hunter-binance-stream/state.sqlite3')
LIMITS = {'5m':16, '15m':25, '1h':5, '1d':35}
STEPS = {'5m':300_000, '15m':900_000, '1h':3_600_000, '1d':86_400_000}
WS_SOURCE = 'OFFICIAL_BINANCE_SPOT_WS'
REST_SOURCE = 'OFFICIAL_BINANCE_SPOT_REST'
CACHE_SOURCE = 'OFFICIAL_BINANCE_SPOT_CACHE'
SCHEMA = 'binance_stream_generation_v1'


_REVISION_COLUMNS = ('topic','start','row_json','observed','source_time','confirmed','origin','epoch')
_REVISION_TEXT_COLUMNS = frozenset(('topic','row_json','origin','epoch'))


def _copy_revision_rows_legacy(db, query, parameters):
    """Owned copies from the original query; also the unusual-value fallback."""
    return 'rows', [dict(row) for row in db.execute(query,parameters)]


def _copy_revision_rows(db, query, parameters):
    """Pack conventional revision cells in SQLite; decode after the read closes.

    This changes only the copy boundary, never the revision selection or row_json
    parser. SQLite REAL values must not make a potentially lossy JSON round trip.
    BLOBs, unusual text and schema extensions retain the original query/copies in
    this SAME transaction. Ordinary row_json is already ensure_ascii=True text.
    """
    description=db.execute('SELECT * FROM ('+query+') LIMIT 0',parameters).description
    if tuple(column[0] for column in description)!=_REVISION_COLUMNS:
        return _copy_revision_rows_legacy(db,query,parameters)
    predicates=[];members=[]
    for name in _REVISION_COLUMNS:
        column='copied.'+name
        if name in _REVISION_TEXT_COLUMNS:
            # ASCII only avoids depending on JSON1's handling of malformed UTF-8
            # stored as TEXT. Legitimate Unicode/control/NUL text is copied by
            # the legacy path, without rejecting or rewriting that value.
            valid=("(typeof("+column+")='null' OR (typeof("+column+")='text'"
                   " AND "+column+" NOT GLOB '*[^ -~]*'"
                   " AND instr("+column+",char(0))=0"
                   " AND length(CAST("+column+" AS BLOB))=length("+column+")))")
        else:
            valid="typeof("+column+") IN ('integer','null')"
        predicates.append(valid)
        # Do not feed REAL/BLOB values into JSON1, even for a discarded packet.
        members.extend(("'"+name+"'",'CASE WHEN '+valid+' THEN '+column+' ELSE NULL END'))
    packed=('SELECT COALESCE(SUM(CASE WHEN '+' AND '.join(predicates)+
            ' THEN 0 ELSE 1 END),0),json_group_array(json_object('+','.join(members)+
            ')) FROM ('+query+') AS copied')
    try:
        unusual,payload=db.execute(packed,parameters).fetchone()
    except sqlite3.OperationalError as error:
        if str(error) not in ('no such function: json_group_array','no such function: json_object'):
            raise
        return _copy_revision_rows_legacy(db,query,parameters)
    if unusual:
        return _copy_revision_rows_legacy(db,query,parameters)
    return 'json',payload


class Budget(SharedBudget):
    """Same four owners; directory weight consumes proportionally more spacing."""
    def acquire(self,now_ms=None,timeout=10,*,weight=2):
        if getattr(self.store,'control',None) is not None:
            raise ValueError('BINANCE_CONTROL_ADMISSION_REQUIRES_IDENTITY')
        if type(weight) is not int or weight not in (2,20):raise ValueError('BINANCE_WEIGHT_INVALID')
        with self.store.db(timeout=timeout) as db:
            b=db.execute('SELECT * FROM budget WHERE id=1').fetchone()
            now_ms=budget_time(db,b,now_ms)
            if now_ms<b['last_ms']:raise RuntimeError('CLOCK_ROLLBACK')
            db.execute('UPDATE budget SET last_ms=? WHERE id=1',(now_ms,))
            if b['halt']:raise RuntimeError(b['halt'])
            gate=request_gate(self.store,db)
            if gate:raise RuntimeError(gate)
            if now_ms<max(b['next_ms'],b['cooldown_ms']):return None
            if db.execute('SELECT COUNT(*) FROM leases').fetchone()[0]>=4:return None
            token=uuid.uuid4().hex
            db.execute('INSERT INTO leases VALUES(?,?)',(token,now_ms))
            db.execute('UPDATE budget SET next_ms=? WHERE id=1',(now_ms+100*weight,))
            return token

    def status(self,timeout=10):
        control=getattr(self.store,'control',None)
        return control.status(timeout=min(timeout,.25)) if control else super().status(timeout=timeout)

    def failure(self,status,text,now_ms=None,retry_after=1,timeout=10):
        control=getattr(self.store,'control',None)
        if control:
            return control.failure(status,text,retry_after=retry_after)
        return super().failure(status,text,now_ms,retry_after,timeout)


def topic(symbol, interval):
    if (not isinstance(symbol,str) or not symbol.endswith('USDT')
            or not 1 <= len(symbol[:-4]) <= 30 or not symbol.isalnum()
            or symbol.upper()!=symbol or symbol.lower().upper()!=symbol or interval not in LIMITS):
        raise ValueError('BINANCE_TOPIC_INVALID')
    return symbol.lower()+'@kline_'+interval


def split_topic(name):
    symbol, interval = name.split('@kline_')
    if topic(symbol.upper(), interval) != name:
        raise ValueError('BINANCE_TOPIC_INVALID')
    return symbol.upper(), interval


def roster_hash(names):
    if not names or len(names) != len(set(names)):
        raise ValueError('BINANCE_ROSTER_INVALID')
    for name in names: split_topic(name)
    return content_hash(sorted(names))


def roster(scan, now):
    stamp = dt.datetime.fromisoformat(scan['as_of_utc'].replace('Z', '+00:00'))
    if (scan.get('binance_complete') is not True or not scan.get('generation_id')
            or stamp.tzinfo is None or not 0 <= (now-stamp).total_seconds() <= 1800):
        raise ValueError('BINANCE_SCAN_NOT_FRESH_COMPLETE')
    symbols = []
    for base, coin in scan['coins'].items():
        for pair in coin.get('pairs', []):
            if pair.get('venue') == 'binance':
                if pair.get('base') != base or pair.get('pair') != base+'USDT':
                    raise ValueError('BINANCE_SCAN_PAIR_MISMATCH')
                symbols.append(pair['pair'])
    if not symbols or len(symbols) != len(set(symbols)) or 'BTCUSDT' not in symbols:
        raise ValueError('BINANCE_SCAN_ROSTER_INVALID')
    names = sorted([topic(s, i) for s in symbols for i in ('15m','1h','1d')]+[topic('BTCUSDT','5m')])
    roster_hash(names)
    return names


def valid_row(row, interval):
    if not isinstance(row, list) or len(row) != 12:
        raise ValueError('BINANCE_BAR_SHAPE_INVALID')
    if (type(row[0]) is not int or type(row[6]) is not int or row[0] < 0
            or row[0] % STEPS[interval] or row[6] != row[0]+STEPS[interval]-1
            or type(row[8]) is not int or row[8] < 0):
        raise ValueError('BINANCE_BAR_TIME_OR_TRADES_INVALID')
    o,h,l,c,v,q,tv,tq = [float(row[i]) for i in (1,2,3,4,5,7,9,10)]
    if (not all(math.isfinite(x) for x in (o,h,l,c,v,q,tv,tq)) or min(o,h,l,c) <= 0
            or not l <= min(o,c) <= max(o,c) <= h or min(v,q,tv,tq) < 0 or tv > v or tq > q):
        raise ValueError('BINANCE_BAR_VALUES_INVALID')
    return list(row)


class Store(MarketControlMixin, CoordinationStore):
    def __init__(self, path=STATE_PATH):
        super().__init__(path)

    @classmethod
    def reader(cls, path=STATE_PATH):
        return super().reader(path)

    def begin_session(self, names, manifest):
        digest = roster_hash(names)
        if not re.fullmatch('[a-f0-9]{64}', manifest):
            raise ValueError('BINANCE_MANIFEST_HASH_INVALID')
        epoch = uuid.uuid4().hex
        control=getattr(self,'control',None)
        if control:
            control.begin_epoch(epoch,self._control_source_head,digest,
                                manifest_sha256=manifest,names=names)
        with self.db() as db:
            db.execute('DELETE FROM topics')
            db.execute('DELETE FROM readiness')
            db.executemany('INSERT INTO topics(topic,epoch) VALUES(?,?)', [(n,epoch) for n in names])
            for k,v in [('epoch',epoch),('scope_hash',manifest),('roster_hash',digest),('connected','1')]:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,v))
            db.execute('DELETE FROM bars WHERE topic NOT IN (SELECT topic FROM topics)')
            db.execute('DELETE FROM revisions WHERE topic NOT IN (SELECT topic FROM topics)')
        return epoch

    def disconnect(self,epoch):
        control=getattr(self,'control',None)
        if control:
            control.end_epoch(epoch,'DISCONNECTED')
        return super().disconnect(epoch)

    def _trim(self, db, name, end):
        _, interval = split_topic(name)
        oldest = (end//STEPS[interval]-LIMITS[interval]-2)*STEPS[interval]
        for table in ('bars','revisions'):
            db.execute('DELETE FROM '+table+' WHERE topic=? AND start<?',(name,oldest))
        db.execute('''DELETE FROM revisions AS old WHERE old.topic=? AND old.observed<?
            AND old.source_time < (SELECT MAX(prior.source_time) FROM revisions AS prior
              WHERE prior.topic=old.topic AND prior.start=old.start AND prior.observed<?)''',
            (name,end-HISTORY_MS,end-HISTORY_MS))

    def _put(self, db, name, row, observed, source_time, confirmed, origin, epoch):
        # Only these two fields decide replacement. Keep the shared store's
        # revision and pruning semantics without materializing the prior bar.
        previous = db.execute('SELECT source_time,confirmed FROM bars WHERE topic=? AND start=?',
                              (name,int(row[0]))).fetchone()
        if previous and (source_time <= previous['source_time']
                         or (previous['confirmed'] and not confirmed)):
            return False
        values = (name,int(row[0]),json.dumps(row),observed,source_time,int(confirmed),origin,epoch)
        db.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?)',values)
        db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)',values)
        db.execute('''DELETE FROM revisions WHERE topic=? AND start=? AND source_time NOT IN
            (SELECT source_time FROM revisions WHERE topic=? AND start=? AND
             (observed>=? OR source_time=(SELECT MAX(source_time) FROM revisions
              WHERE topic=? AND start=? AND observed<?))
             ORDER BY source_time DESC LIMIT ?)''',
            (name,int(row[0]),name,int(row[0]),observed-HISTORY_MS,
             name,int(row[0]),observed-HISTORY_MS,MAX_REVISIONS))
        return True

    def _history_gap(self, db, name, interval, source):
        step = STEPS[interval]
        summary = db.execute('''SELECT MAX(start),MIN(typeof(start)='integer'),
            (SELECT payload FROM metadata WHERE kind=?) FROM bars WHERE topic=?''',
            ('history_floor:'+name,name)).fetchone()
        if summary[1] != 1:
            # Keep the former Python ordering/type errors for malformed stored
            # starts (and an empty table); SQLite MAX alone would ignore NULL.
            bars = {r['start']:r for r in db.execute(
                'SELECT start,confirmed FROM bars WHERE topic=?',(name,))}
            anchor = max(bars)
            floor = summary[2]
            needed = {anchor-i*step for i in range(LIMITS[interval])
                      if floor is None or anchor-i*step >= int(floor)}
            return (not needed <= set(bars) or any(s+step+FRESH_MS <= source
                    and not bars[s]['confirmed'] for s in needed & bars.keys()))
        anchor,floor = summary[0],summary[2]
        needed = {anchor-i*step for i in range(LIMITS[interval])
                  if floor is None or anchor-i*step >= int(floor)}
        if not needed:return False
        # Return two scalars instead of materializing every history row. An IN
        # set preserves exact/off-grid membership. Match Python truthiness of
        # SQLite values: NULL, numeric zero and empty text/blob are false; SQL
        # NOT confirmed would incorrectly treat nonempty text as false.
        marks = ','.join('?' for _ in needed)
        present,unclosed = db.execute('''SELECT COUNT(*),MAX(start<=? AND
            (confirmed IS NULL OR confirmed=0 OR length(CAST(confirmed AS BLOB))=0))
            FROM bars WHERE topic=? AND start IN ('''+marks+')',
            (source-step-FRESH_MS,name,*sorted(needed))).fetchone()
        return present != len(needed) or bool(unclosed)

    def _ingest_ws(self, db, epoch, message, received, state=None):
        # A supplied state belongs to this frame in this same write transaction.
        # It must never be cached across frames, batches, or transactions.
        k = message['k']; name = topic(message['s'], k['i'])
        source = message['E']; step = STEPS[k['i']]
        if (message.get('e') != 'kline' or k.get('s') != message['s']
                or type(source) is not int or type(received) is not int
                or not 0 <= received-source <= FRESH_MS or type(k.get('x')) is not bool):
            raise ValueError('BINANCE_WS_ENVELOPE_INVALID')
        row = valid_row([k['t'],k['o'],k['h'],k['l'],k['c'],k['v'],k['T'],k['q'],k['n'],k['V'],k['Q'],'0'], k['i'])
        if row[0] > source or (k['x'] and row[6] > source):
            raise ValueError('BINANCE_WS_CONFIRMATION_INVALID')
        self.check_epoch(db, epoch)
        if state is None:
            state = db.execute('SELECT * FROM topics WHERE topic=?',(name,)).fetchone()
        if not state or not state['ack']: raise ValueError('BINANCE_TOPIC_NOT_ACKED')
        if source <= state['ws_ts']: return False
        changed = self._put(db,name,row,received,source,k['x'],WS_SOURCE,epoch)
        db.execute('UPDATE topics SET ws_rx=?,ws_ts=? WHERE topic=?',(received,source,name))
        self._trim(db,name,source)
        if self._history_gap(db,name,k['i'],source):
            db.execute("UPDATE topics SET gap=1,error='MISSING_HISTORY_OR_CLOSE' WHERE topic=?",(name,))
        self._record_readiness(db,name,received)
        return changed

    def ingest_ws(self, epoch, message, received):
        with self.db() as db:
            return self._ingest_ws(db,epoch,message,received)

    def ingest_ws_batch(self, epoch, observations):
        if not 1 <= len(observations) <= 100:
            raise ValueError('BINANCE_INGEST_BATCH_BOUND_INVALID')
        with self.db() as db:
            return [self._ingest_ws(db,epoch,message,received) for message,received in observations]

    def ingest_rest(self, epoch, name, rows, requested, received):
        _, interval = split_topic(name); step = STEPS[interval]
        if type(requested) is not int or type(received) is not int or not 0 <= received-requested <= FRESH_MS:
            raise ValueError('BINANCE_REST_RECEIPT_TIME_INVALID')
        rows = [valid_row(r, interval) for r in rows]
        if not 1 <= len(rows) <= LIMITS[interval]:
            raise ValueError('BINANCE_REST_WINDOW_SIZE_INVALID')
        expected = sorted((requested//step-i)*step for i in range(len(rows)))
        if [r[0] for r in rows] != expected:
            raise ValueError('BINANCE_REST_INCOMPLETE_WINDOW')
        with self.db() as db:
            self.check_epoch(db, epoch)
            if not db.execute('SELECT 1 FROM topics WHERE topic=?',(name,)).fetchone():
                raise ValueError('BINANCE_REST_SCOPE_INVALID')
            for row in rows:
                # REST has no exchange observation timestamp. The request cutoff
                # is a labelled local clock, never relabelled WS/server time.
                self._put(db,name,row,received,requested,row[6]<requested,REST_SOURCE,epoch)
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('history_floor:'+name,str(rows[0][0])))
            self._trim(db,name,requested)
            db.execute('UPDATE topics SET gap=0,error=NULL WHERE topic=?',(name,))
            self._record_readiness(db,name,received)

    def _freeze_snapshot(self, cut, names):
        # Copy rows under one short snapshot. JSON validation and hashing happen
        # after this read transaction closes, so they cannot pin a WAL snapshot.
        with self.db(readonly=True) as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
            states = {r['topic']:dict(r) for r in db.execute('SELECT * FROM topics')}
            readiness={};revision_copies=[]
            # Keep the complete roster binding, but only copy history needed by
            # this consumer. Chunks stay below SQLite's older 999-variable cap
            # and all share this one consistent read transaction.
            for offset in range(0,len(names),400):
                group=names[offset:offset+400];marks=','.join('?' for _ in group)
                # Query only the latest qualifying row for each requested topic,
                # using readiness_topic(topic, observed) rather than scanning and
                # sorting every historical readiness row in the time window.
                # The chosen=1 projection preserves the legacy copied-row schema.
                values=','.join('( ? )' for _ in group)
                readiness_query=('WITH requested(topic) AS (VALUES '+values+') '
                    'SELECT r.*,1 AS chosen FROM requested JOIN readiness r ON r.id = ('
                    'SELECT prior.id FROM readiness prior WHERE prior.topic=requested.topic '
                    'AND prior.observed<=? ORDER BY prior.observed DESC,prior.id DESC LIMIT 1)')
                readiness.update({r['topic']:dict(r) for r in db.execute(readiness_query,(*group,cut))})
                revision_query='''SELECT r.* FROM revisions r
                    JOIN (SELECT topic,start,MAX(source_time) AS latest FROM revisions
                          WHERE observed<=? AND source_time<=? AND topic IN ('''+marks+''') GROUP BY topic,start) chosen
                    ON r.topic=chosen.topic AND r.start=chosen.start AND r.source_time=chosen.latest'''
                revision_copies.append(_copy_revision_rows(db,revision_query,(cut,cut,*group)))
            instruments={};unresolved=set();reverify=set()
            if meta.get('health_contract'):
                instruments = dict(db.execute("SELECT topic,payload FROM bn_validity WHERE kind='instrument'"))
                unresolved = {r[0] for r in db.execute('SELECT DISTINCT topic FROM bn_unprocessed WHERE resolved_at IS NULL')}
                reverify = {r[0].split(':',1)[1] for r in db.execute("SELECT kind FROM metadata WHERE kind LIKE 'quiet_reverify:%'")}
            # Legacy readers retain their exact governor. Coordinated readers
            # check the independent control file after closing this transaction.
            if meta.get('health_contract')==HEALTH_CONTROL_CONTRACT:
                budget={'halt':None,'cooldown_ms':0};legacy_gate=None
            else:
                budget=dict(budget_state(db));legacy_gate=request_gate(self,db)
        revisions={}
        for kind,value in revision_copies:
            # row_json remains an unchanged string, not embedded/parsed SQL JSON.
            # The selected non-NULL primary keys are unique; aggregation order
            # cannot change the (topic,start) mapping. Consumer order remains the
            # existing sorted topics/starts, with every source/proof/hash check.
            copied=json.loads(value) if kind=='json' else value
            revisions.update({(row['topic'],row['start']):row for row in copied})
        return meta,states,readiness,revisions,instruments,unresolved,reverify,budget,legacy_gate

    def freeze(self, names, cut, read_at, expected_roster=None):
        out = dict(schema=SCHEMA,status='UNKNOWN',generation_end_ms=cut,windows={})
        try:
            roster_hash(names)
            if type(cut) is not int or type(read_at) is not int or not 0 <= read_at-cut <= FRESH_MS:
                return dict(out,reason='BINANCE_GENERATION_STALE_OR_FUTURE')
            meta,states,readiness,revisions,instruments,unresolved,reverify,budget,legacy_gate = self._freeze_snapshot(cut,names)
            digest = roster_hash(list(states))
            out.update(roster_hash=digest,manifest_sha256=meta.get('scope_hash'),epoch=meta.get('epoch'))
            if (digest != meta.get('roster_hash') or (expected_roster and digest != expected_roster)
                    or not set(names) <= states.keys()
                    or not re.fullmatch('[0-9a-f]{64}',meta.get('scope_hash',''))):
                return dict(out,reason='BINANCE_ROSTER_MISMATCH')
            if (meta.get('connected') != '1' or budget['halt'] or legacy_gate
                    or read_at < budget['cooldown_ms']):
                return dict(out,reason='BINANCE_DISCONNECTED_OR_GLOBAL_STOP')
            windows = {}
            for name in sorted(names):
                if meta.get('health_contract'):
                    from research.hunter_binance_health_store import topic_gate_values
                    denial=topic_gate_values(name,cut,meta,instruments.get(name),name in unresolved,name in reverify)
                    if denial:return dict(out,reason=denial,failed_topic=name)
                current = states[name]
                state = readiness.get(name)
                if (not current['ack'] or current['gap'] or not state or not state['ack'] or state['gap']
                        or state['epoch'] != meta['epoch']
                        or any(not 0 <= cut-state[k] <= FRESH_MS for k in ('ws_rx','ws_ts'))):
                    return dict(out,reason='BINANCE_TOPIC_STALE_OR_NOT_READY',failed_topic=name)
                _, interval = split_topic(name); step = STEPS[interval]
                chosen = []
                for start in sorted((cut//step-i)*step for i in range(LIMITS[interval])):
                    r = revisions.get((name,start))
                    if r is None:
                        # Existing daily research accepts 15..35 candles for
                        # newly listed assets; only an oldest prefix may be absent.
                        if interval == '1d' and not chosen: continue
                        return dict(out,reason='BINANCE_HISTORY_MISSING',failed_topic=name)
                    row = valid_row(json.loads(r['row_json']),interval)
                    if (row[0] != start or r['origin'] not in (WS_SOURCE,REST_SOURCE) or r['confirmed'] not in (0,1)
                            or not start <= r['source_time'] <= r['observed'] <= cut
                            or not re.fullmatch('[0-9a-f]{32}',r['epoch'])
                            or (r['confirmed'] and row[6] > r['source_time'])
                            or (row[6] < cut and not r['confirmed'])
                            or (r['origin']==REST_SOURCE and bool(r['confirmed']) != (row[6]<r['source_time']))):
                        return dict(out,reason='BINANCE_CLOSE_OR_SOURCE_INVALID',failed_topic=name)
                    chosen.append((r,row))
                if len(chosen) < (15 if interval == '1d' else LIMITS[interval]):
                    return dict(out,reason='BINANCE_HISTORY_INSUFFICIENT',failed_topic=name)
                # A repair alone never qualifies a current market observation.
                last = chosen[-1][0]
                if (last['origin'] != WS_SOURCE or last['epoch'] != meta['epoch']
                        or any(not 0 <= cut-last[k] <= FRESH_MS for k in ('observed','source_time'))):
                    return dict(out,reason='BINANCE_CURRENT_WS_REQUIRED',failed_topic=name)
                windows[name] = dict(rows=[r for _,r in chosen],transport=[dict(
                    source=r['origin'],source_time_ms=r['source_time'],received_at_ms=r['observed'],
                    source_clock='EXCHANGE_EVENT' if r['origin']==WS_SOURCE else 'LOCAL_REQUEST_CUTOFF',
                    confirm=bool(r['confirmed']),confirmation='WS_EXPLICIT' if r['origin']==WS_SOURCE else 'REST_INFERRED',
                    session=r['epoch']) for r,_ in chosen])
                if meta.get('health_contract'):
                    from research.hunter_binance_health_store import proof
                    window=windows[name]
                    window['instrument_validity']=json.loads(instruments[name])
                    window['validity']=proof('kline',name,meta,WS_SOURCE,last['observed'],cut,
                        min(min(last['observed'],last['source_time'])+FRESH_MS,window['instrument_validity']['expires_at']),'VALID',None,
                        generation_end_ms=cut,ws_event_time=last['source_time'],window_sha256=content_hash(window))
            out.update(status='READY',windows=windows)
            if meta.get('health_contract'):
                out.update(health_contract=meta['health_contract'],epoch_started_ms=int(meta['health_epoch_started']))
            if meta.get('health_contract')==HEALTH_CONTROL_CONTRACT:
                out['control_contract']=CONTROL_CONTRACT
                control=self._control_for_meta(meta)
                control.fence(names,epoch=meta['epoch'])
            out['generation_sha256'] = content_hash(out)
            return out
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,AttributeError,OverflowError):
            return dict(out,status='UNKNOWN',windows={},reason='BINANCE_CACHE_UNAVAILABLE_OR_CORRUPT')
