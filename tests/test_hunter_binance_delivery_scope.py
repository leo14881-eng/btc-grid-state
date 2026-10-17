"""Real SQLite delivery-scope equivalence; no market-history seed or load.

The small synthetic generation below exercises finish_read's identity envelope,
not Frozen's proof checks. Existing consumer/proof tests retain that coverage.
TestLock is the existing declared native-lock substitute. WAL interleavings use
a second real connection that commits before chunk two, without sleeps/threads.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research.hunter_binance_health_store import Store
from research.hunter_binance_market_control import HEALTH_CONTROL_CONTRACT
from research.hunter_binance_stream_store import roster_hash, topic
from tests.test_hunter_binance_control import EPOCH, HEAD, make_control


NOW = 2_000
MANIFEST = 'c'*64
GENERATION_HASH = 'd'*64


def old_delivery_states(db, requested):
    """The pre-change full-table query and Python membership, verbatim."""
    return {r['topic']: dict(r) for r in db.execute('SELECT * FROM topics')
            if r['topic'] in requested}


def old_fence_versions(db, scope):
    return dict(db.execute('SELECT topic,version FROM versions'))


class BeforeSecondChunk:
    def __init__(self, db, prefix, commit):
        self.db = db
        self.prefix = prefix
        self.commit = commit
        self.sizes = []

    def execute(self, sql, parameters=()):
        if sql.startswith(self.prefix):
            if not self.db.in_transaction:
                raise AssertionError('chunk escaped the original transaction')
            self.sizes.append(len(parameters))
            if len(self.sizes) == 2:
                self.commit()
        return self.db.execute(sql, parameters)


class DeliveryScopeTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        folder = Path(scratch.name)
        self.names = [topic('ASSET%04dUSDT' % i, '15m') for i in range(802)]
        self.control = make_control(folder, clock=lambda: 100., wall=lambda: 2.)
        self.control.begin_epoch(EPOCH, HEAD, roster_hash(self.names),
                                 manifest_sha256=MANIFEST, names=self.names)
        self.store = Store(folder/'state.sqlite3')
        self.store.control = self.control
        self.store.enable_health()
        with self.store.db() as db:
            db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', [
                ('epoch', EPOCH), ('scope_hash', MANIFEST),
                ('roster_hash', roster_hash(self.names)), ('connected', '1'),
                ('health_phase', 'ACTIVE')])
            db.executemany('INSERT INTO topics(topic,epoch,ack,gap) VALUES(?,?,1,0)',
                           [(name, EPOCH) for name in self.names])
        self.generation = dict(epoch=EPOCH, manifest_sha256=MANIFEST,
            roster_hash=roster_hash(self.names), health_contract=HEALTH_CONTROL_CONTRACT,
            generation_end_ms=NOW, generation_sha256=GENERATION_HASH)

    def fence(self, names):
        value = self.store.capture_fence(names)
        value['generation_sha256'] = GENERATION_HASH
        return value

    def delivery(self, names, fence):
        return lambda: self.store.finish_read(self.generation, names, NOW, fence)

    @staticmethod
    def outcome(call):
        try:
            return ('value', call())
        except ValueError as error:
            return ('error', type(error), str(error))

    def equivalent(self, call, reason=None):
        actual = self.outcome(call)
        with patch.object(self.store, '_delivery_states', side_effect=old_delivery_states), \
                patch.object(self.control, '_fence_versions', side_effect=old_fence_versions):
            legacy = self.outcome(call)
        self.assertEqual(actual, legacy)
        if reason is None:
            self.assertEqual(actual, ('value', True))
        else:
            self.assertEqual(actual[0], 'error')
            self.assertEqual(actual[2], reason)

    @staticmethod
    def write(path, sql, parameters=()):
        with closing(sqlite3.connect(path, timeout=1)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(sql, parameters)
            db.commit()

    def restore_topic(self, name):
        self.write(self.store.path,
            'INSERT OR REPLACE INTO topics(topic,epoch,ack,gap) VALUES(?,?,1,0)',
            (name, EPOCH))

    def wal(self, path):
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')

    def test_market_first_and_last_chunk_missing_ack_gap_and_epoch(self):
        for count in (401, 801):
            names = self.names[:count]
            fence = self.fence(names)
            self.equivalent(self.delivery(names, fence))
            for name in (names[0], names[-1]):
                for change in ('missing', 'ack', 'gap', 'epoch'):
                    with self.subTest(count=count, topic=name, change=change):
                        try:
                            if change == 'missing':
                                self.write(self.store.path, 'DELETE FROM topics WHERE topic=?', (name,))
                            else:
                                value = {'ack': 0, 'gap': 1, 'epoch': 'f'*32}[change]
                                self.write(self.store.path, 'UPDATE topics SET '+change+'=? WHERE topic=?',
                                           (value, name))
                            self.equivalent(self.delivery(names, fence), 'BINANCE_FINAL_TOPIC_REVOKED')
                        finally:
                            self.restore_topic(name)

    def test_control_first_and_last_chunk_missing_and_changed_version(self):
        for count in (401, 801):
            names = self.names[:count]
            fence = self.control.fence(names, EPOCH)
            call = lambda: self.control.validate_fence(fence, names, EPOCH)
            self.equivalent(call)
            for name in (names[0], names[-1]):
                for change in ('missing', 'version'):
                    with self.subTest(count=count, topic=name, change=change):
                        try:
                            sql = ('DELETE FROM versions WHERE topic=?' if change == 'missing'
                                   else 'UPDATE versions SET version=version+1 WHERE topic=?')
                            self.write(self.control.path, sql, (name,))
                            self.equivalent(call, 'CONTROL_SCOPE_OUTSIDE_ROSTER' if change == 'missing'
                                            else 'CONTROL_READ_FENCE_CHANGED')
                        finally:
                            self.write(self.control.path,
                                'INSERT OR REPLACE INTO versions VALUES(?,0)', (name,))

    def test_unrequested_changes_do_not_revoke_original_scope(self):
        for count in (401, 801):
            with self.subTest(count=count):
                names = self.names[:count]
                fence = self.fence(names)
                outside = self.names[-1]
                # Reordering/duplicates at delivery retain the original set.
                delivered = list(reversed(names))+[names[0]]
                try:
                    self.write(self.store.path,
                        "UPDATE topics SET ack=0,gap=1,epoch='outside' WHERE topic=?", (outside,))
                    self.write(self.control.path, 'UPDATE versions SET version=42 WHERE topic=?', (outside,))
                    self.equivalent(self.delivery(delivered, fence))
                    self.write(self.store.path, 'DELETE FROM topics WHERE topic=?', (outside,))
                    self.write(self.control.path, 'DELETE FROM versions WHERE topic=?', (outside,))
                    self.equivalent(self.delivery(delivered, fence))
                finally:
                    self.restore_topic(outside)
                    self.write(self.control.path, 'INSERT OR REPLACE INTO versions VALUES(?,0)', (outside,))

    def test_sqlite_value_types_keep_old_python_truth_and_equality(self):
        names = self.names[:401]
        fence = self.fence(names)
        cases = dict(ack=(None, 0, 1, '', '0', 'false', b'', b'0', .5),
                     gap=(None, 0, 1, '', '0', 'false', b'', b'0', .5),
                     epoch=(None, EPOCH, EPOCH.encode(), 7))
        for column, values in cases.items():
            for value in values:
                with self.subTest(column=column, value=value):
                    try:
                        self.write(self.store.path, 'UPDATE topics SET '+column+'=? WHERE topic=?',
                                   (value, names[-1]))
                        # Read stored values: INTEGER/TEXT affinity can coerce inputs.
                        with self.store.db(readonly=True) as db:
                            row = old_delivery_states(db, set(names))[names[-1]]
                        revoked = not row['ack'] or row['gap'] or row['epoch'] != EPOCH
                        self.equivalent(self.delivery(names, fence),
                                        'BINANCE_FINAL_TOPIC_REVOKED' if revoked else None)
                    finally:
                        self.restore_topic(names[-1])
        for value in (0, 1, '0', 'false', b'', b'0', .5):
            with self.subTest(column='version', value=value):
                try:
                    self.write(self.control.path, 'UPDATE versions SET version=? WHERE topic=?',
                               (value, names[-1]))
                    with self.control._db(readonly=True) as db:
                        changed = old_fence_versions(db, names)[names[-1]] != 0
                    self.equivalent(lambda: self.control.validate_fence(fence['control'], names, EPOCH),
                                    'CONTROL_READ_FENCE_CHANGED' if changed else None)
                finally:
                    self.write(self.control.path, 'UPDATE versions SET version=0 WHERE topic=?', (names[-1],))

    def test_market_wal_commit_between_chunks_keeps_one_snapshot(self):
        self.wal(self.store.path)
        original = self.store._delivery_states
        for count in (401, 801):
            with self.subTest(count=count):
                names = self.names[:count]
                fence = self.fence(names)
                proxies = []
                def interleave(db, requested):
                    proxy = BeforeSecondChunk(db, 'SELECT topic,ack,gap,epoch FROM topics WHERE',
                        lambda: self.write(self.store.path, 'UPDATE topics SET gap=1 WHERE topic=?', (names[-1],)))
                    proxies.append(proxy)
                    value = original(proxy, requested)
                    legacy = old_delivery_states(db, requested)
                    self.assertEqual(value, {n: {k: row[k] for k in ('topic', 'ack', 'gap', 'epoch')}
                                             for n, row in legacy.items()})
                    return value
                try:
                    with patch.object(self.store, '_delivery_states', side_effect=interleave):
                        self.assertTrue(self.delivery(names, fence)())
                    self.assertEqual(len(proxies), 1)
                    self.assertEqual(proxies[0].sizes, [400]*(count//400)+[count%400])
                    self.equivalent(self.delivery(names, fence), 'BINANCE_FINAL_TOPIC_REVOKED')
                finally:
                    self.restore_topic(names[-1])

    def test_control_wal_commit_between_chunks_keeps_one_snapshot(self):
        self.wal(self.control.path)
        original = self.control._fence_versions
        for count in (401, 801):
            with self.subTest(count=count):
                names = self.names[:count]
                fence = self.control.fence(names, EPOCH)
                proxies = []
                def interleave(db, scope):
                    proxy = BeforeSecondChunk(db, 'SELECT topic,version FROM versions WHERE',
                        lambda: self.write(self.control.path,
                            'UPDATE versions SET version=version+1 WHERE topic=?', (names[-1],)))
                    proxies.append(proxy)
                    value = original(proxy, scope)
                    legacy = old_fence_versions(db, scope)
                    self.assertEqual(value, {n: legacy[n] for n in scope})
                    return value
                try:
                    with patch.object(self.control, '_fence_versions', side_effect=interleave):
                        self.assertEqual(self.control.fence(names, EPOCH), fence)
                    self.assertEqual(len(proxies), 1)
                    self.assertEqual(proxies[0].sizes, [400]*(count//400)+[count%400])
                    self.equivalent(lambda: self.control.validate_fence(fence, names, EPOCH),
                                    'CONTROL_READ_FENCE_CHANGED')
                finally:
                    self.write(self.control.path, 'UPDATE versions SET version=0 WHERE topic=?', (names[-1],))


if __name__ == '__main__':
    unittest.main()
