"""Bounded local query cursors, never prices or portfolio state.

SQLite rollback-journal transactions publish one snapshot. Network work happens
outside transactions; an opaque snapshot token rejects conflicting publishers.
Every cache failure is optional: the caller can still use the original query.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from time import perf_counter
import uuid

SCHEMA_VERSION = 1
APPLICATION_ID = 0x53515043
MAX_SYMBOLS = 2048
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_DATABASE_BYTES = 1024 * 1024
BUSY_TIMEOUT_SECONDS = 0.25
# SQLite INTEGER is signed 64-bit. A loaded revision must leave room for +1.
MAX_INCREMENTABLE_REVISION = (1 << 63) - 2
ENV_KEYS = ('STOCK_SHADOW_INCREMENTAL_QUOTES', 'STOCK_SHADOW_QUOTE_CACHE_PATH',
            'STOCK_SHADOW_QUOTE_CACHE_SCOPE')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def asof(value):
    if not isinstance(value, str):
        raise ValueError('INVALID_CURSOR_TIME')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('NAIVE_CURSOR_TIME')
    return result


def digest(config, query_end, payload):
    return hashlib.sha256(canonical([config, query_end, payload]).encode()).hexdigest()


def _incrementable_revision(value):
    return type(value) is int and 0 <= value <= MAX_INCREMENTABLE_REVISION


@dataclass
class Lease:
    token: str
    revision: int
    cursors: dict


class QuoteWindowStore:
    def __init__(self, path, config):
        self.path = Path(path)
        self.config = canonical({'cache_schema': SCHEMA_VERSION, **config})

    def _connect(self):
        # The runner supplies an existing stock runtime directory. Never mkdir,
        # truncate/rename a corrupt cache, or change directory/service permissions.
        if not self.path.is_absolute() or self.path.is_symlink():
            raise ValueError('CACHE_PATH_NOT_ABSOLUTE_REGULAR_FILE')
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_size > MAX_DATABASE_BYTES):
            raise ValueError('CACHE_FILE_INVALID_OR_OVERSIZE')
        con = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            app = con.execute('PRAGMA application_id').fetchone()[0]
            version = con.execute('PRAGMA user_version').fetchone()[0]
            if (app not in (0, APPLICATION_ID) or (app == APPLICATION_ID and version != SCHEMA_VERSION)
                    or (app == 0 and version != 0)):
                raise ValueError('INCOMPATIBLE_CACHE_SCHEMA')
            if app == 0 and con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError('UNRECOGNIZED_CACHE_DATABASE')
            con.execute('PRAGMA synchronous=FULL')
            con.execute('PRAGMA journal_mode=DELETE')
            page_size = con.execute('PRAGMA page_size').fetchone()[0]
            con.execute(f'PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}')
            if app == 0:
                con.execute('BEGIN IMMEDIATE')
                # Another process may have initialized while this one waited.
                app = con.execute('PRAGMA application_id').fetchone()[0]
                if app == 0:
                    if con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise ValueError('UNRECOGNIZED_CACHE_DATABASE')
                    con.execute('CREATE TABLE cache (id INTEGER PRIMARY KEY CHECK(id=1), '
                                'token TEXT NOT NULL, revision INTEGER NOT NULL, config TEXT NOT NULL, '
                                'query_end TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL)')
                    con.execute('INSERT INTO cache VALUES (1, ?, 0, ?, ?, ?, ?)',
                                (uuid.uuid4().hex, '', '', '{}', ''))
                    con.execute(f'PRAGMA application_id={APPLICATION_ID}')
                    con.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                con.execute('COMMIT')
            if (con.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or
                    con.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                raise ValueError('INCOMPATIBLE_CACHE_SCHEMA')
            return con
        except Exception:
            con.close()  # Closing an unfinished transaction rolls it back.
            raise

    def read(self, symbols, end, lookback, usage):
        began = perf_counter()
        con = None
        usage.update(read_status='UNAVAILABLE', publish_status='NOT_ATTEMPTED')
        try:
            if len(symbols) > MAX_SYMBOLS:
                usage['read_status'] = 'CAPACITY_BYPASS'
                return None
            con = self._connect()
            row = con.execute('SELECT token, revision, config, query_end, payload, digest FROM cache WHERE id=1').fetchone()
            if not row or not isinstance(row[0], str) or len(row[0]) != 32 or not _incrementable_revision(row[1]):
                raise ValueError('INVALID_CACHE_HEADER')
            token, revision, config, query_end, payload, checksum = row
            lease = Lease(token, revision, {})
            if revision == 0:
                usage['read_status'] = 'EMPTY'
                return lease
            if config != self.config:
                usage['read_status'] = 'CONFIG_MISMATCH'
                return lease
            try:
                if len(payload.encode()) > MAX_PAYLOAD_BYTES or checksum != digest(config, query_end, payload):
                    raise ValueError('INVALID_CACHE_PAYLOAD')
                previous_end = asof(query_end)
                if previous_end > end:
                    usage['read_status'] = 'FUTURE_SNAPSHOT'
                    return lease
                if previous_end < end - lookback:
                    usage['read_status'] = 'EXPIRED'
                    return lease
                cursors = json.loads(payload)
                if not isinstance(cursors, dict) or len(cursors) > MAX_SYMBOLS:
                    raise ValueError('INVALID_CACHE_ENTRIES')
                for symbol, cursor in cursors.items():
                    if not isinstance(symbol, str) or not 1 <= len(symbol) <= 32:
                        raise ValueError('INVALID_CACHE_SYMBOL')
                    if not previous_end - lookback <= asof(cursor) <= previous_end:
                        raise ValueError('INVALID_CACHE_CURSOR')
                lease.cursors = {s: cursors[s] for s in symbols if s in cursors}
                usage.update(read_status='HIT', entries=len(lease.cursors), payload_bytes=len(payload.encode()))
            except (ValueError, TypeError, AttributeError):
                # Corrupt data with an intact token can be replaced only after a
                # full successful query. Corrupt SQLite/schema fails closed above.
                usage['read_status'] = 'CORRUPT_PAYLOAD'
            return lease
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
            usage['read_error_type'] = type(exc).__name__
            return None
        finally:
            if con is not None:
                con.close()
            usage['read_seconds'] = perf_counter() - began

    def publish(self, lease, cursors, end, lookback, usage):
        began = perf_counter()
        con = None
        try:
            if not _incrementable_revision(getattr(lease, 'revision', None)):
                raise ValueError('INVALID_CACHE_REVISION')
            if len(cursors) > MAX_SYMBOLS:
                usage['publish_status'] = 'CAPACITY_BYPASS'
                return
            for symbol, cursor in cursors.items():
                if not isinstance(symbol, str) or not 1 <= len(symbol) <= 32 or not end - lookback <= asof(cursor) <= end:
                    raise ValueError('INVALID_PUBLICATION_CURSOR')
            payload = canonical(cursors)
            if len(payload.encode()) > MAX_PAYLOAD_BYTES:
                usage['publish_status'] = 'CAPACITY_BYPASS'
                return
            query_end = end.isoformat()
            con = self._connect()
            con.execute('BEGIN IMMEDIATE')
            current = con.execute('SELECT token, query_end FROM cache WHERE id=1').fetchone()
            if not current or current[0] != lease.token:
                usage['publish_status'] = 'CONFLICT'
                return
            try:
                newer = bool(current[1]) and asof(current[1]) > end
            except ValueError:
                newer = False  # An invalid payload was cold-fetched before repair.
            if newer:
                usage['publish_status'] = 'CLOCK_REGRESSION'
                return
            con.execute('UPDATE cache SET token=?, revision=?, config=?, query_end=?, payload=?, digest=? WHERE id=1',
                        (uuid.uuid4().hex, lease.revision + 1, self.config, query_end, payload,
                         digest(self.config, query_end, payload)))
            con.execute('COMMIT')
            usage.update(publish_status='PUBLISHED', published_entries=len(cursors))
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
            usage.update(publish_status='UNAVAILABLE', publish_error_type=type(exc).__name__)
        finally:
            if con is not None:
                con.close()
            usage['write_seconds'] = perf_counter() - began
