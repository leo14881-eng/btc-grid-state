"""Real SQLite admission tests; no socket, native-lock or HTTP substitution."""
import contextlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from research import hunter_binance_market_control as market


class Store(market.MarketControlMixin):
    def __init__(self,path):
        self.path=Path(path);self._existing=True;self.control=object()
        self._local()


class MarketAdmissionTests(unittest.TestCase):
    def setUp(self):
        scratch=tempfile.TemporaryDirectory();self.addCleanup(scratch.cleanup)
        path=Path(scratch.name)/'market.sqlite3'
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE records(value TEXT)');db.commit()
        self.store=Store(path);self.other=Store(path)
        self.errors=[];self.threads=[]
        self.addCleanup(self.join_all)

    def join_all(self):
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(),'writer thread did not terminate')

    def run_thread(self,action):
        def run():
            try:action()
            except BaseException as error:self.errors.append(error)
        thread=threading.Thread(target=run);self.threads.append(thread);thread.start()
        return thread

    def queued(self,count):
        gate=self.store._market_writer
        with gate.condition:
            self.assertTrue(gate.condition.wait_for(lambda:len(gate.waiting)+len(gate.priority_waiting)==count,timeout=1))

    def append(self,store,value,**kwargs):
        with store.db(write_kind=value,**kwargs) as db:
            db.execute('INSERT INTO records VALUES(?)',(value,))

    def values(self):
        with self.store.db(readonly=True) as db:
            return [r[0] for r in db.execute('SELECT value FROM records ORDER BY rowid')]

    def test_fifo_across_store_instances_prevents_continuous_writer_overtake(self):
        self.assertIs(self.store._market_writer,self.other._market_writer)
        with self.store.db(write_kind='ws_receipts'):
            self.run_thread(lambda:self.append(self.other,'publication:directory'))
            self.queued(1)
            def continuous_ws():
                for _ in range(8):self.append(self.store,'ws_receipts')
            self.run_thread(continuous_ws);self.queued(2)
        self.join_all();self.assertEqual(self.errors,[])
        self.assertEqual(self.values(),['publication:directory']+['ws_receipts']*8)
        self.assertEqual(self.other.transaction_diagnostics()['publication:directory:admission_wait']['count'],1)

    def test_priority_overtakes_prequeued_market_without_preempting_active(self):
        with self.store.db(write_kind='ws_receipts'):
            self.run_thread(lambda:self.append(self.other,'market-first'))
            self.queued(1)
            self.run_thread(lambda:self.append(self.other,'publication:directory'))
            self.queued(2)
        self.join_all()
        self.assertEqual(self.errors,[])
        self.assertEqual(self.values(),['publication:directory','market-first'])

    def test_real_continuous_ws_writer_yields_to_waiting_publication(self):
        # Uses only public db/diagnostics and real SQLite. This also runs against
        # the pre-admission implementation to reproduce its BEGIN starvation.
        first_ws=threading.Event();publication_done=threading.Event()
        begun=[]
        def continuous_ws():
            for number in range(8):
                with self.store.db(write_kind='ws_receipts') as db:
                    db.execute('INSERT INTO records VALUES(?)',('ws'+str(number),))
                    begun.append('ws'+str(number));first_ws.set()
                    publication_done.wait(.12)
        def publication():
            with self.other.db(write_kind='publication:directory') as db:
                db.execute("INSERT INTO records VALUES('publication')")
                begun.append('publication')
            publication_done.set()
        self.run_thread(continuous_ws);self.assertTrue(first_ws.wait(1))
        started=time.monotonic();self.run_thread(publication)
        self.join_all()
        self.assertEqual(self.errors,[],[(type(e).__name__,str(e)) for e in self.errors])
        self.assertTrue(publication_done.is_set())
        self.assertEqual(begun[:2],['ws0','publication'])
        self.assertLess(time.monotonic()-started,2)

    def test_expired_head_and_middle_tickets_do_not_block_followers(self):
        for expired_first in (True,False):
            with self.subTest(expired_first=expired_first):
                self.errors.clear();self.threads.clear()
                with self.store.db():
                    if not expired_first:
                        self.run_thread(lambda:self.append(self.other,'first'));self.queued(1)
                    expired=self.run_thread(lambda:self.append(self.other,'expired',timeout=.04))
                    self.queued(1 if expired_first else 2)
                    self.run_thread(lambda:self.append(self.other,'last'))
                    expired.join(1);self.assertFalse(expired.is_alive())
                    self.assertEqual(len(self.errors),1)
                    error=self.errors[0]
                    self.assertIsInstance(error,market.MarketAdmissionTimeout)
                    self.assertEqual(error.market_phase,'admission_wait')
                    self.assertIsNone(getattr(error,'sqlite_errorcode',None))
                self.join_all()
                self.assertNotIn('expired',self.values())
        diag=self.other.transaction_diagnostics()['expired:admission_wait']
        self.assertEqual(diag['errors'],{'None:MarketAdmissionTimeout':2})

    def test_rollback_releases_turn_without_committing_failed_publication(self):
        entered=threading.Event();release=threading.Event()
        def failed():
            with self.store.db() as db:
                db.execute("INSERT INTO records VALUES('must_rollback')")
                entered.set();release.wait(1)
                raise ValueError('rejected publication')
        self.run_thread(failed);self.assertTrue(entered.wait(1))
        self.run_thread(lambda:self.append(self.other,'next'));self.queued(1)
        release.set();self.join_all()
        self.assertEqual(self.values(),['next'])
        self.assertEqual([str(e) for e in self.errors],['rejected publication'])

    def test_read_and_existing_nested_transaction_bypass_admission(self):
        with self.store.db() as outer:
            with self.other.db(readonly=True) as reader:
                self.assertEqual(reader.execute('SELECT COUNT(*) FROM records').fetchone()[0],0)
            local=self.store._local();local.connection=outer
            try:
                with self.store.db() as nested:self.assertIs(nested,outer)
            finally:local.connection=None
            with self.assertRaisesRegex(sqlite3.OperationalError,'WRITER_REENTRY'):
                with self.other.db():pass
        self.append(self.other,'after')
        self.assertEqual(self.values(),['after'])

    def test_external_sqlite_writer_still_uses_one_250ms_busy_attempt(self):
        original=sqlite3.connect;statements=[];timeouts=[]
        def connect(*args,**kwargs):
            timeouts.append(kwargs.get('timeout'))
            db=original(*args,**kwargs);db.set_trace_callback(statements.append);return db
        with contextlib.closing(original(self.store.path)) as external:
            external.execute('BEGIN IMMEDIATE')
            started=time.monotonic()
            with patch.object(market.sqlite3,'connect',side_effect=connect):
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    self.append(self.store,'blocked')
            elapsed=time.monotonic()-started
            external.rollback()
        self.assertEqual(caught.exception.market_phase,'begin')
        self.assertEqual(str(caught.exception),'database is locked')
        if sys.version_info >= (3,11):
            self.assertEqual(caught.exception.sqlite_errorcode,sqlite3.SQLITE_BUSY)
        self.assertEqual(timeouts,[.25]);self.assertEqual(statements.count('BEGIN IMMEDIATE'),1)
        self.assertGreaterEqual(elapsed,.20);self.assertLess(elapsed,1.5)
        self.append(self.other,'after');self.assertEqual(self.values(),['after'])

    def test_connection_close_error_still_releases_admission(self):
        original=sqlite3.connect
        class CloseFailure:
            def __init__(self,db):object.__setattr__(self,'db',db)
            def __getattr__(self,name):return getattr(self.db,name)
            def __setattr__(self,name,value):setattr(self.db,name,value)
            def close(self):self.db.close();raise OSError('close acknowledgement failed')
        with patch.object(market.sqlite3,'connect',side_effect=lambda *a,**k:CloseFailure(original(*a,**k))):
            with self.assertRaisesRegex(OSError,'close acknowledgement'):
                self.append(self.store,'committed')
        self.append(self.other,'next');self.assertEqual(self.values(),['committed','next'])


if __name__=='__main__':unittest.main()
