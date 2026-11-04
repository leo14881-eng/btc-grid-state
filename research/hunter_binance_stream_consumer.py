"""Existing-only frozen Binance reads; explicit UNKNOWN and no network fallback."""
import copy
import datetime as dt
import os
import sys
import time

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


class Frozen:
    def __init__(self,generation,checked_at=None):
        if generation.get('status')!='READY':
            raise ValueError('BINANCE_CACHE_UNKNOWN:'+str(generation.get('reason','UNAVAILABLE')))
        self.generation=copy.deepcopy(generation)
        if generation.get('health_contract'):
            from research.hunter_binance_health_store import valid_proof,CONTRACT
            from research.hunter_binance_stream_store import content_hash
            if generation['health_contract']!=CONTRACT:raise ValueError('BINANCE_CACHE_VALIDITY_CONTRACT')
            now=time.time_ns()//1_000_000 if checked_at is None else checked_at
            meta=dict(epoch=generation['epoch'],scope_hash=generation['manifest_sha256'],
                      roster_hash=generation['roster_hash'],health_epoch_started=generation['epoch_started_ms'])
            for name,window in generation['windows'].items():
                evidence=window.get('validity',{})
                payload={k:v for k,v in window.items() if k!='validity'}
                if (not valid_proof(evidence,'kline',name,meta,now)
                        or not valid_proof(window.get('instrument_validity',{}),'instrument',name,meta,now)
                        or evidence['generation_end_ms']!=generation['generation_end_ms']
                        or evidence['window_sha256']!=content_hash(payload)):
                    raise ValueError('BINANCE_CACHE_VALIDITY_MISSING_EXPIRED_OR_MISMATCHED')

    def rows(self,symbol,interval,count=None):
        rows=self.generation['windows'][topic(symbol,interval)]['rows']
        return copy.deepcopy(rows if count is None else rows[-count:])

    def packet(self,symbol,interval,count=None):
        g=self.generation; window=g['windows'][topic(symbol,interval)]
        rows=self.rows(symbol,interval,count)
        end=dt.datetime.fromtimestamp(g['generation_end_ms']/1000,dt.timezone.utc)
        out=capture(rows,symbol,interval,end)
        out.update(source=CACHE_SOURCE,cache={k:g[k] for k in CACHE_FIELDS},
                   transport=copy.deepcopy(window['transport'][-len(rows):]))
        for bar,transport in zip(out['bars'],out['transport']): bar['confirmed']=transport['confirm']
        out['content_hash']=hash_value(out['bars'])
        validate_cache_packet(out,symbol,interval,len(rows))
        return out


def read(names,now,reader=None,clock=lambda:time.time_ns()//1_000_000,expected_roster=None):
    cut=int(now.timestamp()*1000); started=clock()
    generation=(reader or Store.reader()).freeze(names,cut,started,expected_roster)
    finished=clock()
    if finished<started or not 0<=finished-cut<=FRESH_MS:
        raise ValueError('BINANCE_CACHE_READ_FINISHED_STALE_OR_CLOCK_ROLLBACK')
    return Frozen(generation,checked_at=finished)


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
