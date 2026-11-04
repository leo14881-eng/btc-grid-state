"""Opt-in Bybit health evidence; connection health never substitutes for WS data.

The distinct connected marker intentionally makes legacy readers fail closed.
The shared Bybit/Binance budget and base store retain their existing semantics.
"""
import json
import re
import sqlite3
import uuid
from decimal import Decimal

try:
    from research.hunter_bybit_stream_store import (
        Store as BaseStore, FRESH_MS, LIMITS, content_hash, roster_hash,
        topic, split_topic, valid_row, budget_state, request_gate)
except ModuleNotFoundError as error:
    if error.name != 'research': raise
    from hunter_bybit_stream_store import (
        Store as BaseStore, FRESH_MS, LIMITS, content_hash, roster_hash,
        topic, split_topic, valid_row, budget_state, request_gate)

CONTRACT = 'bybit_health_v1'
WS_SOURCE = 'OFFICIAL_BYBIT_V5_SPOT_WS'
REST_SOURCE = 'OFFICIAL_BYBIT_V5_SPOT_REST'
CACHE_SOURCE = 'OFFICIAL_BYBIT_V5_SPOT_CACHE'
DIRECTORY_SOURCE = 'OFFICIAL_BYBIT_V5_SPOT_REST_INSTRUMENTS_INFO'
CONNECTION_SOURCE = 'OFFICIAL_BYBIT_V5_SPOT_WS_CONNECTION'
SOURCES = dict(instrument=DIRECTORY_SOURCE, connection=CONNECTION_SOURCE,
               kline=CACHE_SOURCE, quiet=REST_SOURCE)


def payload_hash(value):
    # Decoded JSON hash, not a claim to preserve raw wire bytes.
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=True).encode()).hexdigest()


def proof(kind, name, meta, observed, verified, expires, result='VALID', reason=None, **extra):
    symbol, interval = split_topic(name)
    return dict(schema=CONTRACT, kind=kind, exchange='BYBIT', market='spot',
                symbol=symbol, interval=interval, epoch=meta['epoch'], generation=meta['epoch'],
                roster_hash=meta['roster_hash'], manifest_sha256=meta['scope_hash'],
                source=SOURCES[kind], observed_at=observed, verified_at=verified,
                expires_at=expires, result=result, reason=reason, **extra)


def valid_proof(value, kind, name, meta, now):
    try:
        expected = proof(kind, name, meta, 0, 0, 0)
        keys = ('schema', 'kind', 'exchange', 'market', 'symbol', 'interval', 'epoch',
                'generation', 'roster_hash', 'manifest_sha256', 'source', 'result', 'reason')
        if any(value.get(k) != expected[k] for k in keys): return False
        a, b, c = (value[k] for k in ('observed_at', 'verified_at', 'expires_at'))
        if any(type(x) is not int for x in (a, b, c, now)): return False
        if not int(meta['health_epoch_started']) <= a <= b <= now <= c or c-a > FRESH_MS: return False
        if kind == 'instrument':
            if (value.get('trading_status') != 'Trading' or value.get('quote_coin') != 'USDT'
                    or value.get('base_coin', '')+'USDT' != value['symbol']
                    or value.get('symbol_type') == 'xstocks'
                    or not re.fullmatch('[a-f0-9]{64}', value.get('response_sha256', ''))): return False
        if kind == 'connection' and value.get('evidence') not in ('MATCHING_PONG', 'VALIDATED_MARKET_FRAME'):
            return False
        if kind == 'quiet':
            step = int(expected['interval'])*60_000
            if (value.get('comparison') != 'SAME_CLOSED_NATIVE_BAR'
                    or value.get('closed_start') != (now//step-1)*step
                    or not re.fullmatch('[a-f0-9]{64}', value.get('response_sha256', ''))): return False
        if kind == 'kline':
            if (value.get('generation_end_ms') != b or type(value.get('ws_event_time')) is not int
                    or type(value.get('ws_received_at')) is not int
                    or not value['ws_event_time'] <= value['ws_received_at'] <= b
                    or c > min(value['ws_event_time'], value['ws_received_at'])+FRESH_MS
                    or value.get('current_bar_source') not in (WS_SOURCE, REST_SOURCE)
                    or not re.fullmatch('[a-f0-9]{64}', value.get('window_sha256', ''))): return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def validate_generation(generation, now):
    """Validate evidence again at actual read completion (and historical replay cut)."""
    if 'health_contract' not in generation: return True  # Existing legacy generations.
    try:
        if generation['health_contract'] != CONTRACT or generation['status'] != 'READY': return False
        meta = dict(epoch=generation['epoch'], roster_hash=generation['roster_hash'],
                    scope_hash=generation['manifest_sha256'], health_epoch_started=generation['health_epoch_started'])
        if roster_hash(list(generation['windows'])) != meta['roster_hash']: return False
        for name, window in generation['windows'].items():
            evidence = window['validity']
            for kind in ('instrument', 'connection', 'kline'):
                if not valid_proof(evidence[kind], kind, name, meta, now): return False
            kline = evidence['kline']
            if (kline['generation_end_ms'] != generation['generation_end_ms']
                    or kline['window_sha256'] != content_hash({k:v for k,v in window.items() if k != 'validity'})):
                return False
            if 'quiet' in evidence and not valid_proof(evidence['quiet'], 'quiet', name, meta, now): return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def topic_gate(db, name, cut, meta):
    if meta.get('health_contract') != CONTRACT or meta.get('health_epoch') != meta.get('epoch'):
        return 'BYBIT_HEALTH_EPOCH_OR_CONTRACT_INVALID'
    if meta.get('health_phase') != 'ACTIVE': return 'BYBIT_HEALTH_DRAINING_OR_STOPPED'
    for kind in ('instrument', 'connection'):
        row = db.execute('SELECT payload FROM bb_validity WHERE kind=? AND topic=?', (kind, name)).fetchone()
        if not row or not valid_proof(json.loads(row[0]), kind, name, meta, cut):
            return 'BYBIT_'+kind.upper()+'_PROOF_UNKNOWN'
    if db.execute('SELECT 1 FROM bb_unprocessed WHERE topic=? AND resolved_at IS NULL LIMIT 1', (name,)).fetchone():
        return 'BYBIT_UNRECONCILED_RECEIPT'
    if db.execute('SELECT 1 FROM metadata WHERE kind=?', ('bybit_quiet_reverify:'+name,)).fetchone():
        return 'BYBIT_QUIET_REVERIFICATION_REQUIRED'
    return None


class Store(BaseStore):
    def enable_health(self):
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS bb_validity(kind TEXT,topic TEXT,payload TEXT NOT NULL,PRIMARY KEY(kind,topic));
                CREATE TABLE IF NOT EXISTS bb_counts(epoch TEXT,topic TEXT,committed INTEGER DEFAULT 0,
                    duplicate INTEGER DEFAULT 0,invalid INTEGER DEFAULT 0,unprocessed_recorded INTEGER DEFAULT 0,
                    revisions_changed INTEGER DEFAULT 0,PRIMARY KEY(epoch,topic));
                CREATE TABLE IF NOT EXISTS bb_keys(epoch TEXT,topic TEXT,event_time INTEGER,payload_sha256 TEXT,
                    PRIMARY KEY(epoch,topic,event_time));
                CREATE TABLE IF NOT EXISTS bb_unprocessed(id INTEGER PRIMARY KEY,epoch TEXT,topic TEXT,
                    received_at INTEGER,opens_json TEXT,event_time INTEGER,payload_sha256 TEXT,
                    reason TEXT,category TEXT,resolved_at INTEGER);
                CREATE TABLE IF NOT EXISTS bb_protocol_invalid(id INTEGER PRIMARY KEY,epoch TEXT,
                    received_at INTEGER,payload_sha256 TEXT,reason TEXT);
            ''')

    def begin_health(self, names, manifest, started):
        expected = roster_hash(names)
        if not re.fullmatch('[a-f0-9]{64}', manifest): raise ValueError('MANIFEST_HASH_INVALID')
        epoch = uuid.uuid4().hex
        with self.db() as db:
            db.execute('DELETE FROM topics')
            db.execute('DELETE FROM readiness')
            db.execute('DELETE FROM bb_validity')
            db.execute('DELETE FROM bb_keys')
            db.executemany('INSERT INTO topics(topic,epoch) VALUES(?,?)', [(n,epoch) for n in names])
            db.executemany('INSERT INTO bb_counts(epoch,topic) VALUES(?,?)', [(epoch,n) for n in names])
            for key,value in dict(epoch=epoch, scope_hash=manifest, roster_hash=expected, connected=CONTRACT,
                                  health_contract=CONTRACT, health_epoch=epoch, health_phase='ACTIVE',
                                  health_epoch_started=str(started)).items():
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key,value))
            db.execute('DELETE FROM bars WHERE topic NOT IN (SELECT topic FROM topics)')
            db.execute('DELETE FROM revisions WHERE topic NOT IN (SELECT topic FROM topics)')
        return epoch

    def metadata(self):
        with self.db(readonly=True) as db: return dict(db.execute('SELECT key,value FROM meta'))

    def check_epoch(self, db, epoch, *, draining=False):
        meta = dict(db.execute('SELECT key,value FROM meta'))
        phases = ('ACTIVE', 'DRAINING') if draining else ('ACTIVE',)
        if (meta.get('epoch') != epoch or meta.get('health_epoch') != epoch
                or meta.get('connected') != CONTRACT or meta.get('health_contract') != CONTRACT
                or meta.get('health_phase') not in phases): raise ValueError('STALE_SESSION')

    def disconnect(self, epoch):
        with self.db() as db:
            self.check_epoch(db, epoch, draining=True)
            db.execute("UPDATE meta SET value='0' WHERE key='connected'")
            db.execute("UPDATE meta SET value='STOPPED' WHERE key='health_phase'")
            db.execute("UPDATE topics SET ack=0,gap=1,error='DISCONNECTED'")
            db.execute('DELETE FROM bb_validity')

    def save_proof(self, value):
        name = topic(value['symbol'], value['interval'])
        with self.db() as db:
            self.check_epoch(db, value['epoch'])
            db.execute('INSERT OR REPLACE INTO bb_validity VALUES(?,?,?)',
                       (value['kind'], name, json.dumps(value, sort_keys=True)))

    def get_proof(self, kind, name):
        with self.db(readonly=True) as db:
            row = db.execute('SELECT payload FROM bb_validity WHERE kind=? AND topic=?', (kind,name)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _times(meta, requested, received):
        if (type(requested) is not int or type(received) is not int
                or not int(meta['health_epoch_started']) <= requested <= received <= requested+FRESH_MS):
            raise ValueError('BYBIT_VERIFICATION_TIME_INVALID')

    def instrument_batch(self, epoch, names, body, requested, received):
        meta = self.metadata(); self._times(meta, requested, received)
        result, source_time = body.get('result', {}), body.get('time')
        if (type(body.get('retCode')) is not int or body['retCode'] != 0
                or result.get('category') != 'spot' or result.get('nextPageCursor')
                or not isinstance(result.get('list'), list) or type(source_time) is not int
                or not 0 <= received-source_time <= FRESH_MS): raise ValueError('BYBIT_INSTRUMENT_ENVELOPE')
        symbols = {}
        for row in result['list']:
            symbol = row.get('symbol')
            if not isinstance(symbol, str) or symbol in symbols: raise ValueError('BYBIT_INSTRUMENT_DUPLICATE')
            symbols[symbol] = row
        values = []
        for name in names:
            symbol, _ = split_topic(name); row = symbols.get(symbol, {})
            if (row.get('symbol') != symbol or row.get('baseCoin', '')+'USDT' != symbol
                    or row.get('quoteCoin') != 'USDT'): raise ValueError('BYBIT_INSTRUMENT_IDENTITY')
            good = row.get('status') == 'Trading' and row.get('symbolType') != 'xstocks'
            values.append(proof('instrument', name, meta, requested, received,
                                min(requested,source_time)+FRESH_MS,
                                'VALID' if good else 'INVALID', None if good else 'NOT_TRADING_SPOT',
                                trading_status=row.get('status'), base_coin=row['baseCoin'], quote_coin='USDT',
                                symbol_type=row.get('symbolType'), response_sha256=content_hash(body),
                                exchange_time_ms=source_time))
        with self.db() as db:
            self.check_epoch(db, epoch)
            if meta['epoch'] != epoch: raise ValueError('STALE_SESSION')
            if set(names) != {r[0] for r in db.execute('SELECT topic FROM topics')}: raise ValueError('BYBIT_INSTRUMENT_SCOPE')
            db.executemany('INSERT OR REPLACE INTO bb_validity VALUES(?,?,?)',
                          [('instrument',topic(v['symbol'],v['interval']),json.dumps(v,sort_keys=True)) for v in values])

    def _record(self, db, epoch, name, message, received, reason, category):
        bars = message.get('data')
        opens = [bar.get('start') if isinstance(bar,dict) else None for bar in bars] if isinstance(bars,list) else []
        step = int(split_topic(name)[1])*60000
        if (not 1 <= len(opens) <= 2 or any(type(x) is not int or x < 0 or x % step for x in opens)
                or len(set(opens)) != len(opens)): opens = None
        event = message.get('ts')
        if type(event) is not int: event = None
        db.execute('''INSERT INTO bb_unprocessed(epoch,topic,received_at,opens_json,event_time,
                   payload_sha256,reason,category) VALUES(?,?,?,?,?,?,?,?)''',
                   (epoch,name,received,json.dumps(opens),event,payload_hash(message),reason,category))
        db.execute('UPDATE topics SET gap=1,error=? WHERE topic=?', (reason,name))
        db.execute('UPDATE bb_counts SET '+category+'='+category+'+1 WHERE epoch=? AND topic=?', (epoch,name))

    def unprocessed(self, epoch, observations, reason, timeout=10):
        with self.db(timeout=timeout) as db:
            self.check_epoch(db, epoch, draining=True)
            for message,received in observations:
                name = message['topic']; split_topic(name)
                if not db.execute('SELECT 1 FROM topics WHERE topic=?', (name,)).fetchone(): raise ValueError('RECEIPT_SCOPE')
                self._record(db,epoch,name,message,received,reason,'unprocessed_recorded')

    def protocol_invalid(self, epoch, received, message, reason):
        import hashlib
        raw = message if isinstance(message,bytes) else message.encode('utf-8')
        with self.db() as db:
            self.check_epoch(db, epoch, draining=True)
            db.execute('INSERT INTO bb_protocol_invalid(epoch,received_at,payload_sha256,reason) VALUES(?,?,?,?)',
                       (epoch,received,hashlib.sha256(raw).hexdigest(),reason+':RAW_WIRE_BYTES_SHA256'))

    def ingest_accounted(self, epoch, observations):
        outcomes = []
        with self.db() as db:
            self.check_epoch(db, epoch)
            for message,received in observations:
                name = message['topic']; split_topic(name)
                if not db.execute('SELECT 1 FROM topics WHERE topic=?', (name,)).fetchone(): raise ValueError('RECEIPT_SCOPE')
                try:
                    if type(message.get('ts')) is not int: raise ValueError('BYBIT_EVENT_KEY_INVALID')
                    key = (epoch,name,message['ts']); digest = payload_hash(message)
                    prior = db.execute('SELECT payload_sha256 FROM bb_keys WHERE epoch=? AND topic=? AND event_time=?', key).fetchone()
                    if prior:
                        if prior[0] != digest: raise ValueError('BYBIT_EVENT_KEY_CONFLICT')
                        db.execute('UPDATE bb_counts SET duplicate=duplicate+1 WHERE epoch=? AND topic=?', (epoch,name))
                        outcomes.append('duplicate'); continue
                    state = db.execute('SELECT ws_ts FROM topics WHERE topic=?', (name,)).fetchone()
                    if message['ts'] <= state[0]: raise ValueError('BYBIT_OLD_UNVERIFIABLE_EVENT')
                    db.execute('SAVEPOINT observation')
                    try: changed = self._ingest_ws(db, epoch, message, received)
                    except (ValueError,KeyError,TypeError,OverflowError):
                        db.execute('ROLLBACK TO observation'); raise
                    finally: db.execute('RELEASE observation')
                    db.execute('INSERT INTO bb_keys VALUES(?,?,?,?)', (*key,digest))
                    db.execute('DELETE FROM bb_keys WHERE epoch=? AND topic=? AND event_time<?', (epoch,name,message['ts']-180000))
                    db.execute('UPDATE bb_counts SET committed=committed+1,revisions_changed=revisions_changed+? WHERE epoch=? AND topic=?',
                               (int(changed),epoch,name))
                    outcomes.append('committed')
                except (ValueError,KeyError,TypeError,OverflowError) as error:
                    reason = str(error) if re.fullmatch('[A-Z0-9_]+',str(error)) else 'BYBIT_INVALID_MARKET_FRAME'
                    self._record(db,epoch,name,message,received,reason,'invalid'); outcomes.append('invalid')
        return outcomes

    def ingest_rest(self, epoch, name, body, requested_end, received_ms):
        self._times(self.metadata(), requested_end, received_ms)
        super().ingest_rest(epoch,name,body,requested_end,received_ms)
        returned = {int(row[0]) for row in body['result']['list']}
        with self.db() as db:
            self.check_epoch(db,epoch)
            # A multibar frame is ONE receipt: every explicit native open must be covered.
            for receipt in db.execute('SELECT id,opens_json FROM bb_unprocessed WHERE topic=? AND received_at<=? AND resolved_at IS NULL',
                                      (name,requested_end)).fetchall():
                opens = json.loads(receipt['opens_json'])
                if opens and set(opens) <= returned:
                    db.execute('UPDATE bb_unprocessed SET resolved_at=? WHERE id=?', (received_ms,receipt['id']))

    def verify_quiet(self, epoch, name, body, requested, received):
        meta = self.metadata(); self._times(meta,requested,received)
        symbol,interval = split_topic(name); step = int(interval)*60_000
        result,source_time = body.get('result', {}),body.get('time')
        if (type(body.get('retCode')) is not int or body['retCode'] != 0 or result.get('category') != 'spot'
                or result.get('symbol') != symbol or type(source_time) is not int
                or not 0 <= received-source_time <= FRESH_MS): raise ValueError('BYBIT_QUIET_ENVELOPE')
        rows = [valid_row(row,interval) for row in result.get('list', [])]
        expected = sorted((requested//step-i)*step for i in range(LIMITS[interval]))
        if sorted(int(row[0]) for row in rows) != expected: raise ValueError('BYBIT_QUIET_WINDOW_INVALID')
        closed = (requested//step-1)*step
        remote = next(row for row in rows if int(row[0]) == closed)
        with self.db() as db:
            self.check_epoch(db,epoch)
            if meta['epoch'] != epoch: raise ValueError('STALE_SESSION')
            local = db.execute('SELECT row_json,confirmed FROM bars WHERE topic=? AND start=?', (name,closed)).fetchone()
            status = db.execute("SELECT payload FROM bb_validity WHERE kind='instrument' AND topic=?", (name,)).fetchone()
            comparable = lambda row: [Decimal(str(x)) for x in row]
            same = bool(local and local['confirmed'] and closed+step <= source_time
                        and comparable(json.loads(local['row_json'])) == comparable(remote))
            good = same and status is not None and valid_proof(json.loads(status[0]),'instrument',name,meta,received)
            reason = None if good else ('CLOSED_BAR_MISMATCH' if not same else 'TRADING_STATUS_UNVERIFIED')
            value = proof('quiet',name,meta,requested,received,
                          min(requested+FRESH_MS,source_time+FRESH_MS,(requested//step+1)*step-1),
                          'VALID' if good else 'UNKNOWN',reason,comparison='SAME_CLOSED_NATIVE_BAR',closed_start=closed,
                          response_sha256=content_hash(body),exchange_time_ms=source_time)
            if good and not valid_proof(value,'quiet',name,meta,received):
                good = False; value.update(result='UNKNOWN',reason='VERIFICATION_EXPIRED_OR_NATIVE_BOUNDARY')
            db.execute('INSERT OR REPLACE INTO bb_validity VALUES(?,?,?)', ('quiet',name,json.dumps(value,sort_keys=True)))
            if not same:
                db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('bybit_quiet_reverify:'+name,str(received)))
            elif good:
                db.execute('DELETE FROM metadata WHERE kind=?', ('bybit_quiet_reverify:'+name,))
        if not same: self.ingest_rest(epoch,name,body,requested,received)
        return value

    def connection_proofs(self, epoch, observed, evidence, now):
        meta = self.metadata()
        good = observed > 0 and 0 <= now-observed <= FRESH_MS
        with self.db() as db:
            self.check_epoch(db,epoch)
            for row in db.execute('SELECT topic FROM topics').fetchall():
                value = proof('connection',row[0],meta,observed,now,observed+FRESH_MS,
                              'VALID' if good else 'UNKNOWN',None if good else 'CONNECTION_EVIDENCE_EXPIRED',evidence=evidence)
                db.execute('INSERT OR REPLACE INTO bb_validity VALUES(?,?,?)', ('connection',row[0],json.dumps(value,sort_keys=True)))

    def counts(self, epoch):
        with self.db(readonly=True) as db:
            return {r['topic']:dict(r) for r in db.execute('SELECT * FROM bb_counts WHERE epoch=?', (epoch,))}

    def store_health(self, value, finalizer=None):
        with self.db() as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
            if meta.get('epoch') != value['epoch'] or meta.get('health_epoch') != value['epoch']: raise ValueError('STALE_SESSION')
            if value.get('service_status') != 'STOPPED': self.check_epoch(db,value['epoch'])
            elif meta.get('health_phase') != 'STOPPED': raise ValueError('HEALTH_NOT_STOPPED')
            if finalizer is not None:
                budget=budget_state(db)
                budget['halt']=budget['halt'] or request_gate(self,db)
                def publication_gate(now):
                    states=db.execute('SELECT * FROM topics').fetchall()
                    return (len(states)==value['topics'] and roster_hash([s['topic'] for s in states])==value['roster_hash']
                            and all(s['ack'] and not s['gap'] and s['epoch']==value['epoch']
                                    and topic_gate(db,s['topic'],now,meta) is None for s in states))
                value=finalizer(value,budget,publication_gate)
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (CONTRACT,json.dumps(value,sort_keys=True)))
        return value

    def read_health(self):
        with self.db(readonly=True) as db:
            row = db.execute('SELECT payload FROM metadata WHERE kind=?', (CONTRACT,)).fetchone()
            return json.loads(row[0]) if row else None

    def start_draining(self, epoch):
        with self.db() as db:
            self.check_epoch(db,epoch)
            db.execute("UPDATE meta SET value='DRAINING' WHERE key='health_phase'")
            db.execute('DELETE FROM bb_validity')
            value = dict(contract=CONTRACT,epoch=epoch,service_status='DRAINING',data_status='UNKNOWN')
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (CONTRACT,json.dumps(value,sort_keys=True)))

    def window(self, symbol, interval, generation_end, now_ms):
        name = topic(symbol,interval)
        try:
            meta = self.metadata()
            if 'health_contract' not in meta: return super().window(symbol,interval,generation_end,now_ms)
            result = self.freeze([row['topic'] for row in self.states()],generation_end,now_ms)
            if result['status'] == 'READY':
                return dict(status='READY',topic=name,generation_end_ms=generation_end,scope_hash=result['manifest_sha256'],**result['windows'][name])
            return dict(status='UNKNOWN',topic=name,rows=[],generation_end_ms=generation_end,reason=result['reason'])
        except (sqlite3.Error,OSError,ValueError,KeyError,TypeError):
            return dict(status='UNKNOWN',topic=name,rows=[],generation_end_ms=generation_end,reason='CACHE_UNAVAILABLE_OR_CORRUPT')

    def completed_generation(self, generation, now, clock=None):
        if not validate_generation(generation,now): return False
        try:
            with self.db(readonly=True) as db:
                meta = dict(db.execute('SELECT key,value FROM meta'))
                budget = budget_state(db)
                health = 'health_contract' in generation
                if (meta.get('epoch') != generation['epoch']
                        or meta.get('scope_hash') != generation['manifest_sha256']
                        or meta.get('roster_hash') != generation['roster_hash']
                        or meta.get('connected') != (CONTRACT if health else '1')
                        or ('health_contract' in meta) != health
                        or budget['halt'] or now < budget['cooldown_ms'] or request_gate(self,db)):
                    return False
                if health and any(topic_gate(db,name,now,meta) for name in generation['windows']): return False
                copied = [(row['kind'],row['topic'],json.loads(row['payload']))
                          for row in db.execute("SELECT * FROM bb_validity WHERE kind IN ('instrument','connection')")] if health else []
            # Include the FINAL SQLite fence's wait time, not just freeze's time.
            finished = clock() if clock is not None else now
            return (0 <= finished-now and 0 <= finished-generation['generation_end_ms'] <= FRESH_MS
                    and validate_generation(generation,finished)
                    and all(valid_proof(value,kind,name,meta,finished) for kind,name,value in copied))
        except (sqlite3.Error,OSError,ValueError,KeyError,TypeError): return False

    def _ingest_ws(self, db, epoch, message, received_ms):
        name = message['topic']
        _, interval = split_topic(name)
        step = int(interval)*60_000
        ts = message['ts']
        if (type(ts) is not int or not 0 <= received_ms-ts <= FRESH_MS
                or message.get('type') != 'snapshot' or not isinstance(message.get('data'), list)
                or not 1 <= len(message['data']) <= 2):
            raise ValueError('INVALID_WS_ENVELOPE')
        parsed = []
        for bar in message['data']:
            row = valid_row([bar['start']]+[bar[k] for k in ('open','high','low','close','volume','turnover')], interval)
            start = int(row[0])
            if (str(bar['interval']) != interval or bar['end'] != start+step-1
                    or type(bar['confirm']) is not bool or type(bar['timestamp']) is not int
                    or not start <= bar['timestamp'] <= min(ts, start+step-1)
                    or (bar['confirm'] and ts < start+step-1) or start > ts):
                raise ValueError('INVALID_WS_BAR')
            parsed.append((row, bar['confirm']))
        if len({row[0] for row,_ in parsed}) != len(parsed):
            raise ValueError('BYBIT_DUPLICATE_NATIVE_OPEN')
        self.check_epoch(db, epoch)
        state = db.execute('SELECT * FROM topics WHERE topic=?', (name,)).fetchone()
        if not state or not state['ack']:
            raise ValueError('UNACKNOWLEDGED_TOPIC')
        changed = False
        for row, confirm in parsed:
            changed |= self._put(db, name, row, received_ms, ts, confirm, 'OFFICIAL_BYBIT_V5_SPOT_WS', epoch)
        # A newly seen WS observation can be older than a just-completed
        # REST snapshot. It proves topic liveness without overwriting that
        # newer bar. True duplicate/out-of-order WS timestamps do neither.
        observed = ts > state['ws_ts']
        if observed:
            db.execute('UPDATE topics SET ws_rx=?,ws_ts=? WHERE topic=?', (received_ms, ts, name))
        self._trim(db, name, ts)
        have = {r[0] for r in db.execute('SELECT start FROM bars WHERE topic=?', (name,))}
        # At a normal boundary the closing previous candle can arrive before
        # the forming next one. Missing only that not-yet-pushed candle is
        # not a transport gap; window() stays UNKNOWN until it arrives.
        anchor = max(have)
        needed = {anchor-i*step for i in range(LIMITS[interval])}
        if not needed <= have:
            db.execute("UPDATE topics SET gap=1,error='MISSING_WINDOW' WHERE topic=?", (name,))
        # A forming bar may arrive before the previous close. Readers stay
        # UNKNOWN immediately; repair only after the normal freshness grace.
        unclosed = db.execute('''SELECT 1 FROM bars WHERE topic=? AND start>=?
            AND start+?<=? AND confirmed=0 LIMIT 1''',
            (name, min(needed), step+FRESH_MS, ts)).fetchone()
        if unclosed:
            db.execute("UPDATE topics SET gap=1,error='MISSING_CLOSE_CONFIRMATION' WHERE topic=?", (name,))
        if observed:
            self._record_readiness(db, name, received_ms)
        return changed


    def freeze(self, topics, generation_end, now_ms):
        """One read transaction for ALL requested topics; never repair or publish.

        A stable topic digest (not the changing manifest receipt hash) binds the
        consumer's current Bybit-only roster + BTC to the collector subscription.
        Revisions received after the cut cannot supply or overwrite that cut.
        Reconnect, roster drift, global denial or any missing topic fails closed.
        """
        try:
            meta = self.metadata()
            if 'health_contract' not in meta:
                return super().freeze(topics, generation_end, now_ms)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return dict(schema='bybit_stream_generation_v1',status='UNKNOWN',windows={},
                        generation_end_ms=generation_end,roster_hash=roster_hash(topics),reason='CACHE_UNAVAILABLE_OR_CORRUPT')
        expected = roster_hash(topics)
        out = dict(schema='bybit_stream_generation_v1', status='UNKNOWN',
                   generation_end_ms=generation_end, roster_hash=expected, windows={}, health_contract=CONTRACT)
        try:
            with self.db(readonly=True) as db:
                meta = dict(db.execute('SELECT key,value FROM meta'))
                states = {r['topic']: r for r in db.execute('SELECT * FROM topics')}
                budget = budget_state(db)
                if (type(generation_end) is not int or type(now_ms) is not int
                        or not 0 <= now_ms-generation_end <= FRESH_MS):
                    return dict(out, reason='GENERATION_STALE_OR_FUTURE')
                if (set(states) != set(topics) or meta.get('roster_hash') != expected
                        or not re.fullmatch('[a-f0-9]{64}', meta.get('scope_hash', ''))):
                    return dict(out, reason='ROSTER_OR_MANIFEST_MISMATCH')
                out.update(epoch=meta.get('epoch'), manifest_sha256=meta['scope_hash'], health_epoch_started=meta.get('health_epoch_started'))
                if (meta.get('connected') != CONTRACT or not budget or budget['halt'] or request_gate(self, db)
                        or now_ms < budget['cooldown_ms']):
                    return dict(out, reason='COLLECTOR_DISCONNECTED_OR_GLOBAL_STOP')
                windows = {}
                for name in sorted(topics):
                    failure = topic_gate(db, name, generation_end, meta)
                    if failure: return dict(out, reason=failure, failed_topic=name)
                    current = states[name]
                    state = db.execute('''SELECT * FROM readiness WHERE topic=? AND observed<=?
                        ORDER BY observed DESC,id DESC LIMIT 1''', (name, generation_end)).fetchone()
                    if (not current['ack'] or current['gap'] or not state or not state['ack'] or state['gap']
                            or state['epoch'] != meta['epoch']
                            or not 0 <= generation_end-state['ws_rx'] <= FRESH_MS
                            or not 0 <= generation_end-state['ws_ts'] <= FRESH_MS):
                        return dict(out, reason='TOPIC_NOT_READY_OR_STALE', failed_topic=name)
                    _, interval = split_topic(name)
                    step = int(interval)*60_000
                    starts = sorted((generation_end//step-i)*step for i in range(LIMITS[interval]))
                    chosen = []
                    for start in starts:
                        bar = db.execute('''SELECT * FROM revisions WHERE topic=? AND start=?
                            AND observed<=? AND source_time<=? ORDER BY source_time DESC LIMIT 1''',
                            (name, start, generation_end, generation_end)).fetchone()
                        if bar is None:
                            return dict(out, reason='HISTORY_MISSING_FOR_GENERATION', failed_topic=name)
                        if start+step <= generation_end and not bar['confirmed']:
                            return dict(out, reason='CLOSED_BAR_UNCONFIRMED', failed_topic=name)
                        if (bar['origin'] not in ('OFFICIAL_BYBIT_V5_SPOT_WS','OFFICIAL_BYBIT_V5_SPOT_REST')
                                or type(bar['confirmed']) is not int or bar['confirmed'] not in (0,1)
                                or not isinstance(bar['epoch'], str) or not re.fullmatch('[a-f0-9]{32}',bar['epoch'])
                                or type(bar['observed']) is not int or type(bar['source_time']) is not int
                                or not 0 <= start <= bar['source_time'] <= bar['observed'] <= generation_end
                                or (bar['confirmed'] and start+step-1 > bar['source_time'])
                                or (bar['origin'].endswith('_REST') and bool(bar['confirmed']) != (start+step <= bar['source_time']))):
                            return dict(out, reason='CACHE_UNAVAILABLE_OR_CORRUPT')
                        chosen.append(bar)
                    # A fresh delayed close is not evidence of a fresh current bar.
                    if (chosen[-1]['epoch'] != meta['epoch'] or
                            any(not 0 <= generation_end-chosen[-1][k] <= FRESH_MS for k in ('observed','source_time'))):
                        return dict(out, reason='CURRENT_BAR_STALE', failed_topic=name)
                    windows[name] = dict(rows=[valid_row(json.loads(r['row_json']), interval) for r in chosen],
                        provenance=[dict(source=r['origin'], received_at_ms=r['observed'],
                            source_time_ms=r['source_time'], confirm=bool(r['confirmed']),
                            confirmation='WS_EXPLICIT' if r['origin'].endswith('_WS') else 'REST_INFERRED',
                            session=r['epoch']) for r in chosen])
                    evidence = {}
                    for kind in ('instrument', 'connection'):
                        value = db.execute('SELECT payload FROM bb_validity WHERE kind=? AND topic=?', (kind,name)).fetchone()
                        evidence[kind] = json.loads(value[0])
                    latest = chosen[-1]
                    expiry = min(state['ws_rx'], state['ws_ts'], latest['observed'], latest['source_time'])+FRESH_MS
                    evidence['kline'] = proof('kline', name, meta, state['ws_rx'], generation_end, expiry,
                        window_sha256=content_hash(windows[name]), generation_end_ms=generation_end,
                        ws_event_time=state['ws_ts'], ws_received_at=state['ws_rx'], current_bar_source=latest['origin'])
                    quiet = db.execute("SELECT payload FROM bb_validity WHERE kind='quiet' AND topic=?", (name,)).fetchone()
                    if quiet and valid_proof(json.loads(quiet[0]), 'quiet', name, meta, generation_end):
                        evidence['quiet'] = json.loads(quiet[0])
                    windows[name]['validity'] = evidence
                out.update(status='READY', windows=windows)
                if not validate_generation(out, now_ms):
                    return dict(out, status='UNKNOWN', windows={}, reason='BYBIT_HEALTH_PROOF_EXPIRED')
                out['generation_sha256'] = content_hash(out)
                return out
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return dict(out, status='UNKNOWN', reason='CACHE_UNAVAILABLE_OR_CORRUPT', windows={})
