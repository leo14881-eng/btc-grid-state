"""Durable public data with bounded revisions and atomic generation reads."""
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

STATE_PATH = Path('/var/lib/hunter-bybit-stream/state.sqlite3')
LIMITS = {'15': 25, '60': 5}
FRESH_MS = 90_000
HISTORY_MS = 180_000
MAX_REVISIONS = 256
SYMBOL = re.compile(r'[A-Z0-9]{2,30}USDT\Z')


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def roster_hash(topics):
    for name in topics:
        split_topic(name)
    if not topics or len(topics) != len(set(topics)):
        raise ValueError('INVALID_SCOPE')
    return content_hash(sorted(topics))


def topic(symbol, interval):
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) or interval not in LIMITS:
        raise ValueError('INVALID_TOPIC')
    return f'kline.{interval}.{symbol}'


def split_topic(value):
    prefix, interval, symbol = value.split('.')
    if prefix != 'kline' or topic(symbol, interval) != value:
        raise ValueError('INVALID_TOPIC')
    return symbol, interval


def valid_row(row, interval):
    step = int(interval) * 60_000
    if not isinstance(row, list) or len(row) != 7:
        raise ValueError('INVALID_BAR')
    if not str(row[0]).isdigit() or int(row[0]) % step:
        raise ValueError('INVALID_START')
    values = [float(x) for x in row[1:]]
    o, h, low, c, v, q = values
    if (not all(math.isfinite(x) for x in values) or min(o, h, low, c) <= 0
            or min(v, q) < 0 or low > min(o, c) or h < max(o, c) or low > h):
        raise ValueError('INVALID_OHLC')
    return [str(x) for x in row]


class Store:
    @classmethod
    def reader(cls, path=STATE_PATH):
        instance = cls.__new__(cls)
        instance.path = Path(path)
        instance._existing = True
        if not instance.path.is_absolute():
            raise ValueError('ABSOLUTE_STATE_PATH_REQUIRED')
        return instance

    def __init__(self, path=STATE_PATH):
        self.path = Path(path)
        self._existing = False
        if not self.path.is_absolute() or not self.path.parent.is_dir():
            raise ValueError('PREPROVISIONED_ABSOLUTE_STATE_PATH_REQUIRED')
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS topics (
                    topic TEXT PRIMARY KEY, epoch TEXT, ack INTEGER DEFAULT 0,
                    ws_rx INTEGER DEFAULT 0, ws_ts INTEGER DEFAULT 0,
                    gap INTEGER DEFAULT 1, error TEXT);
                CREATE TABLE IF NOT EXISTS bars (
                    topic TEXT, start INTEGER, row_json TEXT NOT NULL,
                    observed INTEGER, source_time INTEGER, confirmed INTEGER,
                    origin TEXT, epoch TEXT, PRIMARY KEY(topic,start));
                CREATE TABLE IF NOT EXISTS budget (
                    id INTEGER PRIMARY KEY CHECK(id=1), next_ms INTEGER DEFAULT 0,
                    cooldown_ms INTEGER DEFAULT 0, halt TEXT, last_ms INTEGER DEFAULT 0);
                INSERT OR IGNORE INTO budget(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS leases (token TEXT PRIMARY KEY, acquired_ms INTEGER);
                CREATE TABLE IF NOT EXISTS requests (token TEXT PRIMARY KEY, denied INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS budget_clock (id INTEGER PRIMARY KEY CHECK(id=1), boot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata (kind TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    topic TEXT, start INTEGER, row_json TEXT NOT NULL,
                    observed INTEGER, source_time INTEGER, confirmed INTEGER,
                    origin TEXT, epoch TEXT, PRIMARY KEY(topic,start,source_time));
                CREATE TABLE IF NOT EXISTS readiness (
                    id INTEGER PRIMARY KEY, topic TEXT, observed INTEGER,
                    epoch TEXT, ack INTEGER, ws_rx INTEGER, ws_ts INTEGER, gap INTEGER);
                CREATE INDEX IF NOT EXISTS readiness_topic ON readiness(topic,observed);
            ''')
        self._existing = True

    @contextlib.contextmanager
    def db(self, readonly=False, timeout=10):
        existing = readonly or self._existing
        address = self.path.as_uri()+('?mode=ro' if readonly else '?mode=rw') if existing else str(self.path)
        db = sqlite3.connect(address, uri=existing, timeout=timeout)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN' if readonly else 'BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def begin_session(self, topics, scope_hash):
        if not topics or len(set(topics)) != len(topics):
            raise ValueError('INVALID_SCOPE')
        for name in topics:
            split_topic(name)
        epoch = uuid.uuid4().hex
        with self.db() as db:
            db.execute('DELETE FROM topics')
            db.execute('DELETE FROM readiness')
            db.executemany('INSERT INTO topics(topic,epoch) VALUES(?,?)', [(t, epoch) for t in topics])
            for k, v in [('epoch', epoch), ('scope_hash', scope_hash),
                         ('roster_hash', roster_hash(topics)), ('connected', '1')]:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (k, v))
            # Bound storage and remove symbols no longer in this captured roster.
            db.execute('DELETE FROM bars WHERE topic NOT IN (SELECT topic FROM topics)')
            db.execute('DELETE FROM revisions WHERE topic NOT IN (SELECT topic FROM topics)')
        return epoch

    def check_epoch(self, db, epoch):
        row = db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
        connected = db.execute("SELECT value FROM meta WHERE key='connected'").fetchone()
        if not row or row[0] != epoch or not connected or connected[0] != '1':
            raise ValueError('STALE_SESSION')

    def disconnect(self, epoch):
        with self.db() as db:
            self.check_epoch(db, epoch)
            db.execute("UPDATE meta SET value='0' WHERE key='connected'")
            db.execute("UPDATE topics SET ack=0,gap=1,error='DISCONNECTED'")

    def ack(self, epoch, topics):
        with self.db() as db:
            self.check_epoch(db, epoch)
            for name in topics:
                if db.execute('UPDATE topics SET ack=1 WHERE topic=? AND epoch=?', (name, epoch)).rowcount != 1:
                    raise ValueError('ACK_SCOPE')

    def _put(self, db, name, row, observed, source_time, confirmed, origin, epoch):
        previous = db.execute('SELECT * FROM bars WHERE topic=? AND start=?', (name, int(row[0]))).fetchone()
        if previous and (source_time <= previous['source_time']
                         or (previous['confirmed'] and not confirmed)):
            return False  # Duplicate, delayed revision, or attempted un-close.
        db.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?)',
                   (name, int(row[0]), json.dumps(row), observed, source_time, int(confirmed), origin, epoch))
        db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)',
                   (name, int(row[0]), json.dumps(row), observed, source_time, int(confirmed), origin, epoch))
        # Keep one pre-horizon baseline plus bounded recent revisions per bar.
        db.execute('''DELETE FROM revisions WHERE topic=? AND start=? AND source_time NOT IN
            (SELECT source_time FROM revisions WHERE topic=? AND start=? AND
             (observed>=? OR source_time=(SELECT MAX(source_time) FROM revisions
              WHERE topic=? AND start=? AND observed<?))
             ORDER BY source_time DESC LIMIT ?)''',
            (name, int(row[0]), name, int(row[0]), observed-HISTORY_MS,
             name, int(row[0]), observed-HISTORY_MS, MAX_REVISIONS))
        return True

    def _trim(self, db, name, end):
        _, interval = split_topic(name)
        step = int(interval)*60_000
        db.execute('DELETE FROM bars WHERE topic=? AND start<?',
                   (name, (end//step-LIMITS[interval]-2)*step))
        db.execute('DELETE FROM revisions WHERE topic=? AND start<?',
                   (name, (end//step-LIMITS[interval]-2)*step))
        # Closed bars stop receiving _put calls. Compact their old revisions on
        # subsequent topic traffic too, retaining the last pre-horizon baseline.
        db.execute('''DELETE FROM revisions AS old WHERE old.topic=? AND old.observed<?
            AND old.source_time < (SELECT MAX(prior.source_time) FROM revisions AS prior
                WHERE prior.topic=old.topic AND prior.start=old.start AND prior.observed<?)''',
            (name, end-HISTORY_MS, end-HISTORY_MS))

    def _record_readiness(self, db, name, observed):
        db.execute('''INSERT INTO readiness(topic,observed,epoch,ack,ws_rx,ws_ts,gap)
            SELECT topic,?,epoch,ack,ws_rx,ws_ts,gap FROM topics WHERE topic=?''', (observed, name))
        db.execute('''DELETE FROM readiness WHERE topic=? AND id NOT IN
            (SELECT id FROM readiness WHERE topic=? AND observed>=?
             ORDER BY observed DESC,id DESC LIMIT ?)''',
            (name, name, observed-HISTORY_MS, MAX_REVISIONS))

    def ingest_ws(self, epoch, message, received_ms):
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
        with self.db() as db:
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
            if observed:
                self._record_readiness(db, name, received_ms)
        return changed

    def ingest_rest(self, epoch, name, body, requested_end, received_ms):
        symbol, interval = split_topic(name)
        step, count = int(interval)*60_000, LIMITS[interval]
        data, source_time = body.get('result', {}), body.get('time')
        if (type(body.get('retCode')) is not int or body['retCode'] != 0
                or data.get('category') != 'spot' or data.get('symbol') != symbol
                or type(source_time) is not int or not requested_end <= received_ms
                or not 0 <= received_ms-source_time <= FRESH_MS):
            raise ValueError('INVALID_REST_ENVELOPE')
        rows = [valid_row(r, interval) for r in data.get('list', [])]
        stamps = sorted(int(r[0]) for r in rows)
        expected = [(requested_end//step-i)*step for i in range(count)]
        if stamps != sorted(expected):
            raise ValueError('INCOMPLETE_REST_WINDOW')
        with self.db() as db:
            self.check_epoch(db, epoch)
            if not db.execute('SELECT 1 FROM topics WHERE topic=?', (name,)).fetchone():
                raise ValueError('REST_SCOPE')
            for row in rows:
                self._put(db, name, row, received_ms, source_time,
                          int(row[0])+step <= source_time, 'OFFICIAL_BYBIT_V5_SPOT_REST', epoch)
            self._trim(db, name, requested_end)
            db.execute('UPDATE topics SET gap=0,error=NULL WHERE topic=?', (name,))
            self._record_readiness(db, name, received_ms)

    def freeze(self, topics, generation_end, now_ms):
        """One read transaction for ALL requested topics; never repair or publish.

        A stable topic digest (not the changing manifest receipt hash) binds the
        consumer's current Bybit-only roster + BTC to the collector subscription.
        Revisions received after the cut cannot supply or overwrite that cut.
        Reconnect, roster drift, global denial or any missing topic fails closed.
        """
        expected = roster_hash(topics)
        out = dict(schema='bybit_stream_generation_v1', status='UNKNOWN',
                   generation_end_ms=generation_end, roster_hash=expected, windows={})
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
                out.update(epoch=meta.get('epoch'), manifest_sha256=meta['scope_hash'])
                if (meta.get('connected') != '1' or not budget or budget['halt'] or request_gate(self, db)
                        or now_ms < budget['cooldown_ms']):
                    return dict(out, reason='COLLECTOR_DISCONNECTED_OR_GLOBAL_STOP')
                windows = {}
                for name in sorted(topics):
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
                out.update(status='READY', windows=windows)
                out['generation_sha256'] = content_hash(out)
                return out
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return dict(out, status='UNKNOWN', reason='CACHE_UNAVAILABLE_OR_CORRUPT', windows={})

    def states(self):
        with self.db(readonly=True) as db:
            return [dict(r) for r in db.execute('SELECT * FROM topics ORDER BY topic')]

    def window(self, symbol, interval, generation_end, now_ms):
        """Read-only adapter seam. No IO fallback; current bar stays included."""
        name = topic(symbol, interval)
        try:
            return self._window(symbol, interval, generation_end, now_ms)
        except (sqlite3.Error, OSError, json.JSONDecodeError):
            return dict(status='UNKNOWN', topic=name, rows=[], generation_end_ms=generation_end,
                        reason='CACHE_UNAVAILABLE_OR_CORRUPT')

    def _window(self, symbol, interval, generation_end, now_ms):
        name = topic(symbol, interval)
        unknown = {'status': 'UNKNOWN', 'topic': name, 'rows': [], 'generation_end_ms': generation_end}
        with self.db(readonly=True) as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
            state = db.execute('SELECT * FROM topics WHERE topic=?', (name,)).fetchone()
            if (not state or meta.get('connected') != '1' or not state['ack'] or state['gap']
                    or not 0 <= now_ms-generation_end <= FRESH_MS
                    or not 0 <= generation_end-state['ws_rx'] <= FRESH_MS
                    or not 0 <= generation_end-state['ws_ts'] <= FRESH_MS):
                return dict(unknown, reason='TOPIC_NOT_READY_OR_STALE')
            step, count = int(interval)*60_000, LIMITS[interval]
            starts = [(generation_end//step-i)*step for i in range(count)]
            bars = {r['start']:r for r in db.execute('SELECT * FROM bars WHERE topic=?', (name,))}
            if any(s not in bars for s in starts):
                return dict(unknown, reason='GAPPED_WINDOW')
            chosen = [bars[s] for s in sorted(starts)]
            if any(r['observed'] > generation_end or r['source_time'] > generation_end for r in chosen):
                return dict(unknown, reason='FUTURE_OBSERVATION_FOR_GENERATION')
            return dict(status='READY', topic=name, rows=[json.loads(r['row_json']) for r in chosen],
                        generation_end_ms=generation_end, scope_hash=meta['scope_hash'],
                        provenance=[dict(source=r['origin'], received_at_ms=r['observed'],
                                         source_time_ms=r['source_time'], confirm=bool(r['confirmed']),
                                         confirmation=('WS_EXPLICIT' if r['origin'].endswith('_WS') else 'REST_INFERRED'),
                                         session=r['epoch']) for r in chosen])


class Budget:
    """Shared 200ms admission spacing, <=4 held requests; crashes fail closed.

Leases NEVER expire automatically: a stalled process cannot silently create a
fifth request. A crashed collector requires reviewed lease recovery, not a new
database or another hostname. This governor cannot police unrelated programs.
"""
    def __init__(self, store):
        self.store = store

    def acquire(self, now_ms=None, timeout=10):
        with self.store.db(timeout=timeout) as db:
            b = db.execute('SELECT * FROM budget WHERE id=1').fetchone()
            now_ms = budget_time(db, b, now_ms)
            if now_ms < b['last_ms']:
                raise RuntimeError('CLOCK_ROLLBACK')
            db.execute('UPDATE budget SET last_ms=? WHERE id=1', (now_ms,))
            if b['halt']:
                raise RuntimeError(b['halt'])
            gate = request_gate(self.store, db)
            if gate:
                raise RuntimeError(gate)
            if now_ms < max(b['next_ms'], b['cooldown_ms']):
                return None
            if db.execute('SELECT COUNT(*) FROM leases').fetchone()[0] >= 4:
                return None
            token = uuid.uuid4().hex
            db.execute('INSERT INTO leases VALUES(?,?)', (token, now_ms))
            db.execute('UPDATE budget SET next_ms=? WHERE id=1', (now_ms+200,))
            return token

    def release(self, token):
        with self.store.db() as db:
            db.execute('DELETE FROM leases WHERE token=?', (token,))

    def failure(self, status, text, now_ms=None, retry_after=1, timeout=10):
        text = text.lower()
        with self.store.db(timeout=timeout) as db:
            b = db.execute('SELECT * FROM budget WHERE id=1').fetchone()
            now_ms = budget_time(db,b,now_ms)
            if status == 403:
                if 'access too frequent' in text and 'country' not in text:
                    db.execute('UPDATE budget SET cooldown_ms=MAX(cooldown_ms,?) WHERE id=1', (now_ms+600_000,))
                    return 'GLOBAL_COOLDOWN_10_MINUTES'
                reason = 'COUNTRY_403_MANUAL_REVIEW' if 'country' in text else 'UNKNOWN_403_MANUAL_REVIEW'
                db.execute('UPDATE budget SET halt=? WHERE id=1', (reason,))
                return reason
            if status == 429 or status == 0 or 500 <= status < 600:
                delay = max(1, retry_after) if math.isfinite(retry_after) else 60
                db.execute('UPDATE budget SET cooldown_ms=MAX(cooldown_ms,?) WHERE id=1', (now_ms+int(delay*1000),))
                return 'BOUNDED_RETRY'
            return 'NONRETRYABLE_RESPONSE'

    def status(self, timeout=10):
        with self.store.db(readonly=True,timeout=timeout) as db:
            value = budget_state(db)
            value['halt'] = value['halt'] or request_gate(self.store, db)
            return value


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def budget_time(db, state, explicit=None):
    domain=db.execute('SELECT boot FROM budget_clock WHERE id=1').fetchone()
    if explicit is not None:
        if domain: raise RuntimeError('EXPLICIT_CLOCK_ON_MONOTONIC_BUDGET')
        return explicit  # Only injected legacy fixture clocks use this path.
    boot=boot_id()
    if domain is None:
        if state['next_ms'] or state['last_ms'] or state['cooldown_ms'] or db.execute('SELECT 1 FROM leases LIMIT 1').fetchone():
            raise RuntimeError('BUDGET_CLOCK_MIGRATION_REQUIRES_REVIEW')
        db.execute('INSERT INTO budget_clock(id,boot) VALUES(1,?)',(boot,))
    elif domain[0]!=boot:
        raise RuntimeError('BUDGET_BOOT_CHANGED_MANUAL_REVIEW')
    return time.monotonic_ns()//1_000_000


def budget_state(db):
    value=dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone())
    domain=db.execute('SELECT boot FROM budget_clock WHERE id=1').fetchone()
    if domain:
        if domain[0]!=boot_id(): value['halt']=value['halt'] or 'BUDGET_BOOT_CHANGED_MANUAL_REVIEW'
        # Project remaining monotonic cooldown only for existing UTC consumers.
        # Actual admission never uses the wall clock or this projection.
        remain=max(0,value['cooldown_ms']-time.monotonic_ns()//1_000_000)
        value['cooldown_ms']=time.time_ns()//1_000_000+remain if remain else 0
    return value


def request_gate(store, db):
    """An interrupted tracked HTTP operation is never silently treated as done.

    Locks belong to the supervising process, not its HTTP child. A lost owner
    leaves a persistent marker; no TTL, PID reuse or restart clears it.
    """
    rows = db.execute('SELECT token,denied FROM requests').fetchall()
    if not rows:
        return None
    import fcntl
    for row in rows:
        if row['denied']:
            return 'HTTP_403_PENDING_MANUAL_REVIEW'
        try:
            with (store.path.parent/('request-'+row['token']+'.lock')).open('rb') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                return 'ORPHANED_HTTP_REQUEST_MANUAL_REVIEW'
        except OSError:
            return 'HTTP_REQUEST_OWNER_UNKNOWN_MANUAL_REVIEW'
    return None
