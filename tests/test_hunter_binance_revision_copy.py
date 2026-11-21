"""SQLite revision-copy oracle, including native types and snapshot boundaries.

No exchange/network or full-roster history seed. Runtime cases reuse the small
existing coordinated fixture; its declared portable lock substitute is unchanged.
"""
from contextlib import closing, contextmanager
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research import hunter_binance_stream_store as store_module
from research import hunter_binance_stream_consumer as consumer
from tests import test_hunter_binance_coordinated_runtime as runtime


COLUMNS = ('topic','start','row_json','observed','source_time','confirmed','origin','epoch')
QUERY = 'SELECT * FROM revisions'


def old_copy(db, query, parameters):
    # Independent literal pre-change query/dict conversion, including all columns.
    return 'rows', [dict(row) for row in db.execute(query, parameters)]


def decode(copy):
    kind, value = copy
    return json.loads(value) if kind == 'json' else value


class RevisionCopyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = store_module.Store(Path(temporary.name)/'state.sqlite3')

    def replace(self, values, *, untyped=False):
        with self.store.db() as db:
            if untyped:
                db.execute('DROP TABLE revisions')
                db.execute('CREATE TABLE revisions('+','.join(COLUMNS)+')')
            else:
                db.execute('DELETE FROM revisions')
            db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)', values)

    @staticmethod
    def values():
        return ['btcusdt@kline_15m', 1, '[1,"2",null]', 100, 90, 0,
                store_module.WS_SOURCE, 'a'*32]

    def assert_rows_exact(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for got, old in zip(actual, expected):
            self.assertEqual(list(got), list(old))
            for key in old:
                self.assertIs(type(got[key]), type(old[key]), key)
                if type(old[key]) is float:
                    self.assertEqual(got[key].hex(), old[key].hex(), key)
                else:
                    self.assertEqual(got[key], old[key], key)

    def compare(self, query=QUERY, parameters=(), *, kind=None):
        with self.store.db(readonly=True) as db:
            self.assertTrue(db.in_transaction)
            actual = store_module._copy_revision_rows(db, query, parameters)
            expected = old_copy(db, query, parameters)
            self.assertTrue(db.in_transaction)
        if kind is not None:
            self.assertEqual(actual[0], kind)
        self.assert_rows_exact(decode(actual), expected[1])
        return actual

    def test_plain_int64_null_and_literal_json_text_use_packed_copy(self):
        self.compare(kind='json')  # Empty aggregate is still the empty row set.
        for value in (-(2**63), 2**63-1, None):
            with self.subTest(value=value):
                row = self.values()
                row[1] = value
                row[3:6] = [None, value, None]
                # SQL must not parse/normalize nested JSON, duplicate keys,
                # NaN, a surrogate escape, or negative zero inside row_json.
                row[2] = '[NaN,Infinity,-0.0,"\\ud800",{"x":1,"x":2}]'
                row[-1] = None
                self.replace(row)
                actual = self.compare(kind='json')
                self.assertEqual(decode(actual)[0]['row_json'], row[2])

    def test_native_real_blob_and_unusual_storage_types_use_old_copy(self):
        # Untyped columns deliberately retain REAL -0.0/1.0 rather than letting
        # INTEGER affinity first normalize them. Eligibility uses storage typeof.
        self.replace(self.values(), untyped=True)
        values = (0.0, -0.0, 0.1, 1.0, float.fromhex('0x0.0000000000001p-1022'),
                  float.fromhex('0x1.fffffffffffffp+1023'), math.inf, -math.inf,
                  b'', b'0', '0', 'not-a-number')
        for column in ('start', 'observed', 'source_time', 'confirmed'):
            for value in values:
                with self.subTest(column=column, value=value):
                    row = self.values(); row[COLUMNS.index(column)] = value
                    self.replace(row)
                    self.compare(kind='rows')
        for column in ('topic', 'row_json', 'origin', 'epoch'):
            for value in (b'[]', 7, .1):
                with self.subTest(column=column, value=value):
                    row = self.values(); row[COLUMNS.index(column)] = value
                    self.replace(row)
                    self.compare(kind='rows')
        # SQLite itself maps a bound NaN to NULL: compare its returned value,
        # not the caller's pre-storage float. NULL remains a normal fast-path cell.
        row = self.values(); row[3] = math.nan
        self.replace(row)
        copied = self.compare(kind='json')
        self.assertIsNone(decode(copied)[0]['observed'])

    def test_unicode_nul_controls_and_invalid_utf8_keep_original_text_semantics(self):
        for value in ('雪', 'a\0b', 'line\nbreak', '\t', '\x7f', '{malformed JSON'):
            with self.subTest(value=value):
                row = self.values(); row[2] = value
                self.replace(row)
                self.compare(kind='json' if value == '{malformed JSON' else 'rows')
        self.replace(self.values())
        with self.store.db() as db:
            db.execute("UPDATE revisions SET row_json=CAST(X'80' AS TEXT)")
        with self.store.db(readonly=True) as db:
            with self.assertRaises(sqlite3.Error) as expected:
                old_copy(db, QUERY, ())
            with self.assertRaises(sqlite3.Error) as actual:
                store_module._copy_revision_rows(db, QUERY, ())
            self.assertIs(type(actual.exception), type(expected.exception))
            self.assertEqual(str(actual.exception), str(expected.exception))

    def test_extended_missing_and_reordered_columns_keep_legacy_shape_even_empty(self):
        self.replace(self.values())
        with self.store.db() as db:
            db.execute('ALTER TABLE revisions ADD COLUMN extra BLOB')
            db.execute("UPDATE revisions SET extra=X'00FF'")
        self.compare(kind='rows')
        self.compare('SELECT epoch,topic FROM revisions', kind='rows')
        with self.store.db() as db:
            db.execute('DELETE FROM revisions')
        self.compare(kind='rows')
        self.compare('SELECT epoch,topic FROM revisions', kind='rows')
        with self.store.db(readonly=True) as db:
            with self.assertRaises(sqlite3.OperationalError):
                store_module._copy_revision_rows(db, 'SELECT nonexistent FROM revisions', ())

    def test_only_exact_missing_json_functions_fall_back_in_the_same_transaction(self):
        self.replace(self.values())
        class RejectedAggregate:
            def __init__(self, db, error):
                self.db = db; self.error = error; self.original_queries = 0
            def execute(self, sql, parameters=()):
                if not self.db.in_transaction:
                    raise AssertionError('copy escaped its transaction')
                if sql.startswith('SELECT COALESCE(SUM('):
                    raise self.error
                if sql == QUERY:
                    self.original_queries += 1
                return self.db.execute(sql, parameters)
        for function in ('json_object', 'json_group_array'):
            with self.subTest(function=function), self.store.db(readonly=True) as db:
                proxy = RejectedAggregate(db, sqlite3.OperationalError('no such function: '+function))
                actual = store_module._copy_revision_rows(proxy, QUERY, ())
                self.assertEqual(actual[0], 'rows')
                self.assertEqual(proxy.original_queries, 1)
                self.assert_rows_exact(actual[1], old_copy(db, QUERY, ())[1])
        for message in ('database is locked', 'disk I/O error', 'no such function: unrelated'):
            with self.subTest(message=message), self.store.db(readonly=True) as db:
                error = sqlite3.OperationalError(message)
                error.sqlite_errorcode = 10
                proxy = RejectedAggregate(db, error)
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    store_module._copy_revision_rows(proxy, QUERY, ())
                self.assertIs(caught.exception, error)
                self.assertEqual(caught.exception.sqlite_errorcode, 10)
                self.assertEqual(proxy.original_queries, 0)

    def test_all_chunks_share_wal_snapshot_and_decode_after_transaction_closes(self):
        names = [store_module.topic('ASSET%04dUSDT' % i, '15m') for i in range(801)]
        epoch = self.store.begin_session(names, 'a'*64)
        with self.store.db() as db:
            # Multiple revisions also exercise the unchanged MAX(source_time)
            # and cut selection. These are copy fixtures, not usable K-lines.
            db.executemany('INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)',
                [(name, 0, '["before"]', observed, source, 0, store_module.WS_SOURCE, epoch)
                 for name in names for observed, source in ((50, 40), (100, 90), (200, 190))])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        original = store_module._copy_revision_rows
        original_db = self.store.db
        original_loads = json.loads
        active = [0]; connections = []; sizes = []; payloads = set(); decoded = []; expected = {}
        @contextmanager
        def tracked_db(*args, **kwargs):
            with original_db(*args, **kwargs) as db:
                active[0] += 1
                try:
                    yield db
                finally:
                    active[0] -= 1
        def copy(db, query, parameters):
            connections.append(db); sizes.append(len(parameters)-2)
            self.assertTrue(db.in_transaction)
            if len(sizes) == 2:
                with closing(sqlite3.connect(self.store.path, timeout=1)) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.execute('UPDATE revisions SET row_json=? WHERE topic IN (?,?) AND source_time=90',
                                   ('["after"]', names[0], names[-1]))
                    writer.commit()
            result = original(db, query, parameters)
            self.assertEqual(result[0], 'json')
            payloads.add(id(result[1]))
            expected.update({(row['topic'], row['start']): row for row in old_copy(db, query, parameters)[1]})
            return result
        def loads(value, *args, **kwargs):
            if id(value) in payloads:
                self.assertEqual(active[0], 0)
                decoded.append(1)
            return original_loads(value, *args, **kwargs)
        with patch.object(self.store, 'db', tracked_db), \
                patch.object(store_module, '_copy_revision_rows', side_effect=copy), \
                patch.object(store_module.json, 'loads', side_effect=loads):
            before = self.store._freeze_snapshot(100, names)[3]
        self.assertEqual(sizes, [400, 400, 1])
        self.assertEqual(len({id(db) for db in connections}), 1)
        self.assertEqual(len(decoded), 3)
        self.assertEqual(before, expected)
        self.assertEqual(len(before), 801)
        self.assertTrue(all(row['source_time'] == 90 for row in before.values()))
        after = self.store._freeze_snapshot(100, names)[3]
        for name in (names[0], names[-1]):
            self.assertEqual(before[name, 0]['row_json'], '["before"]')
            self.assertEqual(after[name, 0]['row_json'], '["after"]')


class RevisionConsumerCopyTests(unittest.TestCase):
    setUp = runtime.DataCases.setUp
    market = runtime.DataCases.market

    @staticmethod
    def outputs(market, stamp):
        frozen = market.read(market.names, stamp)
        result = dict(generation=frozen.generation, fence=frozen.fence_sha256,
                      source_head=frozen.source_head_sha, rows={}, packets={})
        for name in market.names:
            symbol, interval = store_module.split_topic(name)
            result['rows'][name] = frozen.rows(symbol, interval)
            result['packets'][name] = frozen.packet(symbol, interval)
        result['checked_at'] = frozen.check_delivery()
        return result

    def test_complete_freeze_and_consumer_outputs_match_literal_old_query(self):
        market = self.market(); stamp = market.seed()+1
        with patch.object(store_module, '_copy_revision_rows', side_effect=old_copy):
            previous = market.store.freeze(market.names, stamp, stamp)
            old_outputs = self.outputs(market, stamp)
        current = market.store.freeze(market.names, stamp, stamp)
        self.assertEqual(current['status'], 'READY')
        self.assertEqual(current, previous)
        self.assertEqual(self.outputs(market, stamp), old_outputs)

    def test_unusual_bodies_rejections_and_full_scope_delivery_fence_match(self):
        market = self.market(); stamp = market.seed()+1
        name = store_module.topic('BTCUSDT', '15m')
        with market.store.db(readonly=True) as db:
            raw = dict(db.execute('SELECT rowid,* FROM revisions WHERE topic=? ORDER BY start,source_time LIMIT 1',
                                  (name,)).fetchone())
        row = json.loads(raw['row_json'])
        unicode_row = list(row); unicode_row[11] = '雪'
        nan_row = list(row); nan_row[11] = math.nan
        cases = [('row_json', '{bad JSON', 'UNKNOWN'),
                 ('row_json', json.dumps(nan_row), 'UNKNOWN'),
                 ('row_json', json.dumps(unicode_row, ensure_ascii=False), 'READY'),
                 ('row_json', raw['row_json'].encode(), 'READY'),
                 ('confirmed', 0, 'UNKNOWN'),
                 ('origin', b'OFFICIAL_BINANCE_SPOT_WS', 'UNKNOWN'),
                 ('epoch', 'invalid', 'UNKNOWN')]
        def outcome():
            try:
                return ('value', self.outputs(market, stamp))
            except consumer.CacheReadError as error:
                return ('error', str(error), error.generation)
        for column, value, status in cases:
            with self.subTest(column=column, value=value):
                try:
                    with market.store.db() as db:
                        db.execute('UPDATE revisions SET '+column+'=? WHERE rowid=?', (value, raw['rowid']))
                    with patch.object(store_module, '_copy_revision_rows', side_effect=old_copy):
                        previous = market.store.freeze(market.names, stamp, stamp)
                        old_outcome = outcome()
                    current = market.store.freeze(market.names, stamp, stamp)
                    self.assertEqual(current['status'], status)
                    self.assertEqual(current, previous)
                    self.assertEqual(outcome(), old_outcome)
                finally:
                    with market.store.db() as db:
                        db.execute('UPDATE revisions SET '+column+'=? WHERE rowid=?', (raw[column], raw['rowid']))
        with patch.object(store_module, '_copy_revision_rows', side_effect=old_copy):
            previous = market.read(market.names, stamp)
        current = market.read(market.names, stamp)
        with market.store.db() as db:
            db.execute('UPDATE topics SET gap=1 WHERE topic=?', (store_module.topic('ETHUSDT', '1h'),))
        errors = []
        for frozen in (previous, current):
            with self.assertRaisesRegex(consumer.CacheReadError, 'BINANCE_FINAL_TOPIC_REVOKED') as caught:
                frozen.rows('BTCUSDT', '15m')
            errors.append(caught.exception.generation)
        self.assertEqual(errors[0], errors[1])


if __name__ == '__main__':
    unittest.main()
