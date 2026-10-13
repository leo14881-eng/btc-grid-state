"""Isolated Linux market WAL migration/interrupt tests; scratch databases only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research import hunter_binance_market_wal as wal
from research.hunter_binance_stream_store import Store


@unittest.skipUnless(sys.platform == 'linux', 'offline migration requires Linux flock')
class WalOfflineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.state = self.directory/'state.sqlite3'
        self.lock = self.directory/'collector.lock'
        self.lock.write_bytes(b'')
        self.store = Store(self.state)
        with self.store.db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES('connected','0')")
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)",
                       ('btcusdt@kline_15m',0,'[1,2]',0,0,0,'test','test'))

    def marker(self):
        return Path(str(self.state)+wal.MARKER_SUFFIX)

    def mode(self, path=None):
        with closing(sqlite3.connect(path or self.state)) as db:
            return db.execute('PRAGMA journal_mode').fetchone()[0].upper()

    def test_offline_wal_keeps_rows_and_reopens_readonly(self):
        result=wal.migrate(self.state)
        self.assertEqual(result['status'],'VERIFIED')
        self.assertEqual(self.mode(),'WAL')
        with closing(sqlite3.connect(self.state.as_uri()+'?mode=ro',uri=True)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0],1)
        self.assertEqual(json.loads(self.marker().read_text()),result)
        wal.require_no_interrupted_migration(self.state)
        reader=Store.reader(self.state)
        with reader.db(readonly=True) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'wal')
        Store(self.state)
        with self.assertRaisesRegex(wal.WalMigrationError,'PREVIOUS_MIGRATION'):
            wal.migrate(self.state)

    def test_active_epoch_never_provisions(self):
        with self.store.db() as db:
            db.execute("UPDATE meta SET value='1' WHERE key='connected'")
        with self.assertRaisesRegex(wal.WalMigrationError,'ACTIVE_EPOCH'):
            wal.migrate(self.state)
        self.assertEqual(self.mode(),'DELETE')
        self.assertFalse(self.marker().exists())

    def test_unknown_epoch_with_topics_rejected(self):
        with self.store.db() as db:
            db.execute("DELETE FROM meta WHERE key='connected'")
            db.execute("INSERT INTO topics(topic) VALUES('btcusdt@kline_15m')")
        with self.assertRaisesRegex(wal.WalMigrationError,'UNKNOWN_EPOCH'):
            wal.migrate(self.state)

    def test_legacy_owners_are_not_reset(self):
        with self.store.db() as db:
            db.execute("INSERT INTO leases VALUES('owner',0)")
        with self.assertRaisesRegex(wal.WalMigrationError,'LEGACY_OWNERS'):
            wal.migrate(self.state)
        self.assertFalse(self.marker().exists())

    def test_active_collector_lock_rejected(self):
        child=subprocess.Popen([sys.executable,'-c',
            'import fcntl,sys,time; f=open(sys.argv[1],"r+b");'
            'fcntl.flock(f,fcntl.LOCK_EX); print("HELD",flush=True); time.sleep(5)',
            str(self.lock)],stdout=subprocess.PIPE,text=True)
        def cleanup():
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()
        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline().strip(),'HELD')
        with self.assertRaises(BlockingIOError):
            wal.migrate(self.state)
        self.assertFalse(self.marker().exists())

    def test_existing_sidecar_needs_review(self):
        Path(str(self.state)+'-wal').write_bytes(b'unknown')
        with self.assertRaisesRegex(wal.WalMigrationError,'SIDECAR_REVIEW'):
            wal.migrate(self.state)
        self.assertFalse(self.marker().exists())

    def test_prepared_marker_blocks_existing_and_new_reader(self):
        self.marker().write_text(json.dumps(dict(schema='binance_market_wal_migration_v1',
                                                state=str(self.state),status='PREPARED')))
        with self.assertRaisesRegex(wal.WalMigrationError,'UNVERIFIED_MIGRATION'):
            Store.reader(self.state)
        with self.assertRaisesRegex(wal.WalMigrationError,'UNVERIFIED_MIGRATION'):
            Store(self.state)
        with self.assertRaisesRegex(wal.WalMigrationError,'PREVIOUS_MIGRATION'):
            wal.migrate(self.state)

    def test_corrupted_marker_blocks_reader(self):
        self.marker().write_text('{bad')
        with self.assertRaisesRegex(wal.WalMigrationError,'UNVERIFIED_MIGRATION'):
            Store.reader(self.state)

    def test_kill_between_wal_commit_and_verified_marker_blocks_start(self):
        original=wal._phase
        def interrupt(marker,value,*,create):
            if not create:raise RuntimeError('injected kill boundary')
            return original(marker,value,create=create)
        with patch.object(wal,'_phase',side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError,'injected kill boundary'):
                wal.migrate(self.state)
        self.assertEqual(self.mode(),'WAL')
        self.assertEqual(json.loads(self.marker().read_text())['status'],'PREPARED')
        with self.assertRaisesRegex(wal.WalMigrationError,'UNVERIFIED_MIGRATION'):
            Store(self.state)

    def test_journal_mode_drift_blocks_start(self):
        wal.migrate(self.state)
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone()[0],'delete')
        with self.assertRaisesRegex(wal.WalMigrationError,'UNVERIFIED_MIGRATION'):
            Store.reader(self.state)

    def test_control_remains_delete(self):
        control=self.directory/'control.sqlite3'
        with closing(sqlite3.connect(control)) as db:
            db.executescript("""CREATE TABLE meta(key TEXT,value TEXT);
                INSERT INTO meta VALUES('phase','OFFLINE');
                CREATE TABLE owners(token TEXT);
                CREATE TABLE requests(token TEXT);""")
        self.assertEqual(self.mode(control),'DELETE')
        wal.migrate(self.state)
        self.assertEqual(self.mode(control),'DELETE')

    def test_control_owner_refused(self):
        control=self.directory/'control.sqlite3'
        with closing(sqlite3.connect(control)) as db:
            db.executescript("""CREATE TABLE meta(key TEXT,value TEXT);
                INSERT INTO meta VALUES('phase','OFFLINE');
                CREATE TABLE owners(token TEXT);
                INSERT INTO owners VALUES('owner');
                CREATE TABLE requests(token TEXT);""")
        with self.assertRaisesRegex(wal.WalMigrationError,'CONTROL_OWNERS'):
            wal.migrate(self.state)

    def test_control_not_offline_refused(self):
        control=self.directory/'control.sqlite3'
        with closing(sqlite3.connect(control)) as db:
            db.executescript("""CREATE TABLE meta(key TEXT,value TEXT);
                INSERT INTO meta VALUES('phase','ACTIVE');
                CREATE TABLE owners(token TEXT);
                CREATE TABLE requests(token TEXT);""")
        with self.assertRaisesRegex(wal.WalMigrationError,'CONTROL_PHASE_NOT_OFFLINE'):
            wal.migrate(self.state)


    def test_long_reader_retains_snapshot_during_wal_writer_commits(self):
        wal.migrate(self.state)
        reader=sqlite3.connect(self.state.as_uri()+'?mode=ro',uri=True,timeout=.25)
        try:
            reader.execute('BEGIN')
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM revisions').fetchone()[0],1)
            with closing(sqlite3.connect(self.state,timeout=.25)) as writer:
                for i in range(4):
                    writer.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)',
                                   ('btcusdt@kline_15m',i+1,'[1,2]',i+1,i+1,0,'test','test'))
                    writer.commit()
                checkpoint=tuple(writer.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())
                self.assertEqual(checkpoint[0],0)
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM revisions').fetchone()[0],1)
        finally:
            reader.rollback()
            reader.close()
        with closing(sqlite3.connect(self.state,timeout=.25)) as writer:
            self.assertEqual(tuple(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())[0],0)
            self.assertEqual(writer.execute('SELECT COUNT(*) FROM revisions').fetchone()[0],5)

    def test_real_sigkill_after_durable_prepared_marker_fails_closed(self):
        worker='''import sys,time
from pathlib import Path
from research import hunter_binance_market_wal as w
original=w._phase
def delayed(marker,value,*,create):
    original(marker,value,create=create)
    if create:
        print('PREPARED_DURABLE',flush=True)
        time.sleep(10)
w._phase=delayed
w.migrate(Path(sys.argv[1]))
'''
        proc=subprocess.Popen([sys.executable,'-c',worker,str(self.state)],
                              cwd=str(Path(__file__).resolve().parents[1]),
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(),'PREPARED_DURABLE')
            proc.kill()
            proc.wait(timeout=3)
            self.assertNotEqual(proc.returncode,0)
            self.assertEqual(json.loads(self.marker().read_text())['status'],'PREPARED')
            with self.assertRaisesRegex(wal.WalMigrationError,'UNVERIFIED_MIGRATION'):
                Store.reader(self.state)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def test_symlink_database_refused(self):
        alias=self.directory/'alias.sqlite3'
        alias.symlink_to(self.state)
        with self.assertRaisesRegex(wal.WalMigrationError,'UNSAFE_FILE_TYPE'):
            wal.migrate(alias)


if __name__ == '__main__':
    unittest.main()
