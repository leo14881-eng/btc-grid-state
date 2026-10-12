"""Binance-only rolling cache; reuse durable SQLite coordination, never Bybit data."""
import datetime as dt
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

try:
    from research.hunter_bybit_stream_store import (
        Store as CoordinationStore, Budget, budget_state, request_gate, content_hash,
        FRESH_MS, HISTORY_MS, MAX_REVISIONS)
except ModuleNotFoundError as exc:
    if exc.name != 'research': raise
    from hunter_bybit_stream_store import (
        Store as CoordinationStore, Budget, budget_state, request_gate, content_hash,
        FRESH_MS, HISTORY_MS, MAX_REVISIONS)

STATE_PATH = Path('/var/lib/hunter-binance-stream/state.sqlite3')
LIMITS = {'5m':16, '15m':25, '1h':5, '1d':35}
STEPS = {'5m':300_000, '15m':900_000, '1h':3_600_000, '1d':86_400_000}
WS_SOURCE = 'OFFICIAL_BINANCE_SPOT_WS'
REST_SOURCE = 'OFFICIAL_BINANCE_SPOT_REST'
CACHE_SOURCE = 'OFFICIAL_BINANCE_SPOT_CACHE'
SCHEMA = 'binance_stream_generation_v1'


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


class Store(CoordinationStore):
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
        with self.db() as db:
            db.execute('DELETE FROM topics')
            db.execute('DELETE FROM readiness')
            db.executemany('INSERT INTO topics(topic,epoch) VALUES(?,?)', [(n,epoch) for n in names])
            for k,v in [('epoch',epoch),('scope_hash',manifest),('roster_hash',digest),('connected','1')]:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,v))
            db.execute('DELETE FROM bars WHERE topic NOT IN (SELECT topic FROM topics)')
            db.execute('DELETE FROM revisions WHERE topic NOT IN (SELECT topic FROM topics)')
        return epoch

    def _trim(self, db, name, end):
        _, interval = split_topic(name)
        oldest = (end//STEPS[interval]-LIMITS[interval]-2)*STEPS[interval]
        for table in ('bars','revisions'):
            db.execute('DELETE FROM '+table+' WHERE topic=? AND start<?',(name,oldest))
        db.execute('''DELETE FROM revisions AS old WHERE old.topic=? AND old.observed<?
            AND old.source_time < (SELECT MAX(prior.source_time) FROM revisions AS prior
              WHERE prior.topic=old.topic AND prior.start=old.start AND prior.observed<?)''',
            (name,end-HISTORY_MS,end-HISTORY_MS))

    def _ingest_ws(self, db, epoch, message, received):
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
        state = db.execute('SELECT * FROM topics WHERE topic=?',(name,)).fetchone()
        if not state or not state['ack']: raise ValueError('BINANCE_TOPIC_NOT_ACKED')
        if source <= state['ws_ts']: return False
        changed = self._put(db,name,row,received,source,k['x'],WS_SOURCE,epoch)
        db.execute('UPDATE topics SET ws_rx=?,ws_ts=? WHERE topic=?',(received,source,name))
        self._trim(db,name,source)
        bars = {r['start']:r for r in db.execute('SELECT * FROM bars WHERE topic=?',(name,))}
        anchor = max(bars)
        floor = db.execute('SELECT payload FROM metadata WHERE kind=?',('history_floor:'+name,)).fetchone()
        needed = {anchor-i*step for i in range(LIMITS[k['i']])
                  if floor is None or anchor-i*step >= int(floor[0])}
        missing = not needed <= set(bars)
        unclosed = any(s+step+FRESH_MS <= source and not bars[s]['confirmed'] for s in needed & bars.keys())
        if missing or unclosed:
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

    def freeze(self, names, cut, read_at, expected_roster=None):
        out = dict(schema=SCHEMA,status='UNKNOWN',generation_end_ms=cut,windows={})
        try:
            roster_hash(names)
            if type(cut) is not int or type(read_at) is not int or not 0 <= read_at-cut <= FRESH_MS:
                return dict(out,reason='BINANCE_GENERATION_STALE_OR_FUTURE')
            with self.db(readonly=True) as db:
                meta = dict(db.execute('SELECT key,value FROM meta'))
                states = {r['topic']:r for r in db.execute('SELECT * FROM topics')}
                digest = roster_hash(list(states))
                out.update(roster_hash=digest,manifest_sha256=meta.get('scope_hash'),epoch=meta.get('epoch'))
                if (digest != meta.get('roster_hash') or (expected_roster and digest != expected_roster)
                        or not set(names) <= states.keys()
                        or not re.fullmatch('[0-9a-f]{64}',meta.get('scope_hash',''))):
                    return dict(out,reason='BINANCE_ROSTER_MISMATCH')
                budget = budget_state(db)
                if (meta.get('connected') != '1' or budget['halt'] or request_gate(self,db)
                        or read_at < budget['cooldown_ms']):
                    return dict(out,reason='BINANCE_DISCONNECTED_OR_GLOBAL_STOP')
                windows = {}
                for name in sorted(names):
                    current = states[name]
                    state = db.execute('''SELECT * FROM readiness WHERE topic=? AND observed<=?
                        ORDER BY observed DESC,id DESC LIMIT 1''',(name,cut)).fetchone()
                    if (not current['ack'] or current['gap'] or not state or not state['ack'] or state['gap']
                            or state['epoch'] != meta['epoch']
                            or any(not 0 <= cut-state[k] <= FRESH_MS for k in ('ws_rx','ws_ts'))):
                        return dict(out,reason='BINANCE_TOPIC_STALE_OR_NOT_READY',failed_topic=name)
                    _, interval = split_topic(name); step = STEPS[interval]
                    chosen = []
                    for start in sorted((cut//step-i)*step for i in range(LIMITS[interval])):
                        r = db.execute('''SELECT * FROM revisions WHERE topic=? AND start=?
                            AND observed<=? AND source_time<=? ORDER BY source_time DESC LIMIT 1''',
                            (name,start,cut,cut)).fetchone()
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
                out.update(status='READY',windows=windows)
                out['generation_sha256'] = content_hash(out)
                return out
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,AttributeError,OverflowError):
            return dict(out,status='UNKNOWN',windows={},reason='BINANCE_CACHE_UNAVAILABLE_OR_CORRUPT')
