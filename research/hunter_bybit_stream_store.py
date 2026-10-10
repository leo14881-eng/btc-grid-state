"""Sidecar-only durable public data. No production consumers import this module.

One absolute SQLite path is shared by all cooperating checkouts/processes on a
host. Latest-only bars deliberately cannot reconstruct an older generation after
an update: that read is UNKNOWN, never a future-data backfill.
"""
import contextlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

STATE_PATH = Path('/var/lib/hunter-bybit-stream/state.sqlite3')
LIMITS = {'15': 25, '60': 5}
FRESH_MS = 90_000
SYMBOL = re.compile(r'[A-Z0-9]{2,30}USDT\Z')


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
        if not instance.path.is_absolute():
            raise ValueError('ABSOLUTE_STATE_PATH_REQUIRED')
        return instance

    def __init__(self, path=STATE_PATH):
        self.path = Path(path)
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
            ''')

    @contextlib.contextmanager
    def db(self, readonly=False):
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro' if readonly else str(self.path),
                             uri=readonly, timeout=10)
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
            db.executemany('INSERT INTO topics(topic,epoch) VALUES(?,?)', [(t, epoch) for t in topics])
            for k, v in [('epoch', epoch), ('scope_hash', scope_hash), ('connected', '1')]:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (k, v))
            # Bound storage and remove symbols no longer in this captured roster.
            db.execute('DELETE FROM bars WHERE topic NOT IN (SELECT topic FROM topics)')
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
        return True

    def _trim(self, db, name, end):
        _, interval = split_topic(name)
        step = int(interval)*60_000
        db.execute('DELETE FROM bars WHERE topic=? AND start<?',
                   (name, (end//step-LIMITS[interval]-2)*step))

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
            if changed and ts > state['ws_ts']:
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

    def acquire(self, now_ms=None):
        with self.store.db() as db:
            if now_ms is None:
                now_ms = time.time_ns()//1_000_000
            b = db.execute('SELECT * FROM budget WHERE id=1').fetchone()
            if now_ms < b['last_ms']:
                raise RuntimeError('CLOCK_ROLLBACK')
            db.execute('UPDATE budget SET last_ms=? WHERE id=1', (now_ms,))
            if b['halt']:
                raise RuntimeError(b['halt'])
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

    def failure(self, status, text, now_ms, retry_after=1):
        text = text.lower()
        with self.store.db() as db:
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

    def status(self):
        with self.store.db(readonly=True) as db:
            return dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone())
