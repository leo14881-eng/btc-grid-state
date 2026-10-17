"""Scratch-only diagnostics. BUSY_SNAPSHOT here is a fixture, not production evidence."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from research import hunter_binance_market_control as market
from research import hunter_binance_control as control


class Market(market.MarketControlMixin):
    def __init__(self,path):
        self.path=path;self._existing=True;self.control=object();self._local()


class SQLiteDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        scratch=tempfile.TemporaryDirectory();self.addCleanup(scratch.cleanup)
        self.folder=Path(scratch.name)
        for name in ('market','control'):
            path=self.folder/(name+'.sqlite3')
            with closing(sqlite3.connect(path)) as db:
                db.executescript("CREATE TABLE records(value INTEGER); INSERT INTO records VALUES(1);"
                    "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);"
                    "INSERT INTO meta VALUES('format','binance_control_v1'),('boot','diagnostics-boot');")
                db.commit()
        (self.folder/'control.sqlite3.gate').write_bytes(b'0')
        self.market=Market(self.folder/'market.sqlite3')
        self.control=control.Control(self.folder/'control.sqlite3',boot='diagnostics-boot')

    def check_histograms(self,diagnostics):
        self.assertEqual(diagnostics['_timing']['scope'],'PER_INSTANCE_LIFETIME')
        self.assertEqual(diagnostics['_timing']['sample_resolution_us'],1)
        self.assertEqual(len(diagnostics['_timing']['histogram_upper_bounds_us']),32)
        for key,item in diagnostics.items():
            if key in ('_timing','connection_observations','wal_bytes'):continue
            self.assertEqual(sum(item['histogram']),item['count'])
            self.assertEqual(len(item['histogram']),32)
            self.assertGreaterEqual(item['total_us'],item['max_us'])
            self.assertIn('p99_upper_bound_us',item)

    def native_error(self,error,code,name):
        # Python 3.10 cannot report these C result attributes. Never invent them.
        if sys.version_info >= (3,11):
            self.assertEqual(error.sqlite_errorcode,code)
            self.assertEqual(error.sqlite_errorname,name)
        else:
            self.assertFalse(hasattr(error,'sqlite_errorcode'))
        self.assertEqual(error.sqlite_context['sqlite_errorcode'],
                         getattr(error,'sqlite_errorcode','unknown'))
        self.assertEqual(error.sqlite_context['sqlite_errorname'],
                         getattr(error,'sqlite_errorname','unknown'))

    def test_success_phases_and_once_observed_connection_settings(self):
        with self.market.db(timeout=.05) as db:db.execute('INSERT INTO records VALUES(2)')
        with self.market.db(readonly=True,timeout=.05) as db:db.execute('SELECT * FROM records').fetchall()
        with self.control._db(timeout=.05) as db:db.execute('INSERT INTO records VALUES(2)')
        for owner,write_kind,read_kind in ((self.market,'market','read'),(self.control,'write','read')):
            diagnostics=owner.transaction_diagnostics();self.check_histograms(diagnostics)
            for kind,begin in ((write_kind,'BEGIN IMMEDIATE'),(read_kind,'BEGIN')):
                observation=diagnostics['connection_observations'][kind]
                self.assertEqual(observation['begin_statement'],begin)
                self.assertEqual(observation['observation'],'FIRST_CONNECTION_FOR_KIND')
                self.assertEqual(observation['journal_mode'],'delete')
                self.assertEqual(observation['probe_errors'],[])
                self.assertEqual(observation['busy_timeout_ms'],int(observation['connect_timeout_seconds']*1000))
            phases=('connect','begin','body','commit','close') if owner is self.market else ('CONNECT','BEGIN','BODY','COMMIT','CLOSE')
            for phase in phases:self.assertIn(write_kind+':'+phase,diagnostics)

    def test_histogram_ceiling_rank_overflow_and_detached_snapshot(self):
        for owner in (self.market,self.control):
            for _ in range(99):owner._measure('fixture','sample',0,elapsed_ns=1001)
            owner._measure('fixture','sample',0,elapsed_ns=1_000_001)
            owner._measure('fixture','overflow',0,elapsed_ns=(market.SQLITE_TIMING_BOUNDS_US[-1]+1)*1000)
            diagnostics=owner.transaction_diagnostics();self.check_histograms(diagnostics)
            item=diagnostics['fixture:sample']
            self.assertEqual((item['count'],item['total_us'],item['max_us']),(100,1099,1000))
            self.assertEqual(item['p99_upper_bound_us'],2)
            self.assertEqual(diagnostics['fixture:overflow']['p99_upper_bound_us'],'unknown')
            item['histogram'][0]=9999
            self.assertEqual(sum(owner.transaction_diagnostics()['fixture:sample']['histogram']),100)

    def busy(self,owner,db_factory,phase):
        with closing(sqlite3.connect(owner.path)) as external:
            external.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    with db_factory(timeout=.01):pass
            finally:external.rollback()
        error=caught.exception;self.native_error(error,5,'SQLITE_BUSY')
        self.assertEqual(error.sqlite_context['phase'],phase)
        self.assertEqual(error.sqlite_context['begin_statement'],'BEGIN IMMEDIATE')
        self.assertEqual(error.sqlite_context['connect_timeout_seconds'],.01)
        self.assertEqual(error.sqlite_context['busy_timeout_ms'],10)
        self.assertEqual(error.sqlite_context['journal_mode'],'delete')
        self.assertEqual(error.sqlite_context['observation'],'AFTER_FAILURE_CLEANUP_SAME_CONNECTION')
        key='market:begin' if owner is self.market else 'write:BEGIN'
        legacy_key=str(getattr(error,'sqlite_errorcode',None))+':'+str(getattr(error,'sqlite_errorname',type(error).__name__))
        self.assertEqual(owner.transaction_diagnostics()[key]['errors'],{legacy_key:1})
        with db_factory(timeout=.05):pass
        item=owner.transaction_diagnostics()[key]
        self.assertEqual(item['last_context']['connect_timeout_seconds'],.05)
        self.assertEqual(item['last_error_context'],error.sqlite_context)

    def test_market_real_busy_preserves_native_attributes(self):
        self.busy(self.market,self.market.db,'begin')

    def test_control_real_busy_preserves_native_attributes(self):
        self.busy(self.control,self.control._db,'BEGIN')

    def snapshot_busy(self,owner,db_factory,phase):
        with closing(sqlite3.connect(owner.path)) as external:
            self.assertEqual(external.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
            with self.assertRaises(sqlite3.OperationalError) as caught:
                with db_factory(timeout=.01) as db:
                    # Test-only replacement of the initial writer transaction:
                    # a deferred WAL snapshot becomes stale after another commit.
                    # Production continues to issue BEGIN IMMEDIATE unchanged.
                    db.rollback();db.execute('BEGIN')
                    db.execute('SELECT * FROM records').fetchall()
                    external.execute('INSERT INTO records VALUES(3)');external.commit()
                    db.execute('INSERT INTO records VALUES(4)')
        error=caught.exception;self.native_error(error,517,'SQLITE_BUSY_SNAPSHOT')
        self.assertEqual(error.sqlite_context['phase'],phase)
        self.assertEqual(error.sqlite_context['journal_mode'],'wal')
        self.assertEqual(error.sqlite_context['begin_statement'],'BEGIN IMMEDIATE')
        with closing(sqlite3.connect(owner.path)) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT value FROM records')],[1,3])

    def test_market_fixture_busy_snapshot_preserves_extended_code(self):
        self.snapshot_busy(self.market,self.market.db,'body')

    def test_control_fixture_busy_snapshot_preserves_extended_code(self):
        self.snapshot_busy(self.control,self.control._db,'BODY')

    def test_python_errors_and_serialization_timeout_have_unknown_sqlite_codes(self):
        original=ValueError('not an SQLite result')
        with self.assertRaises(ValueError) as caught:
            with self.market.db():raise original
        self.assertIs(caught.exception,original)
        self.assertEqual(original.sqlite_context['sqlite_errorcode'],'unknown')
        self.assertEqual(original.sqlite_context['sqlite_errorname'],'unknown')
        entered=threading.Event();release=threading.Event()
        def hold():
            with self.control._writer_lock:entered.set();release.wait(2)
        worker=threading.Thread(target=hold);worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(sqlite3.OperationalError) as caught:
                with self.control._db(timeout=.01):pass
        finally:release.set();worker.join(2)
        self.assertFalse(worker.is_alive())
        context=caught.exception.sqlite_context
        self.assertEqual(context['phase'],'SERIALIZE')
        self.assertEqual(context['observation'],'NO_CONNECTION')
        self.assertEqual(context['sqlite_errorcode'],'unknown')
        self.assertEqual(context['sqlite_errorname'],'unknown')
        self.assertEqual(context['busy_timeout_ms'],'unknown')

    def test_first_probes_are_once_per_kind_and_outside_diagnostics_locks(self):
        original=sqlite3.connect;statements=[];lock_errors=[]
        def trace(statement):
            statements.append(statement)
            if not statement.startswith('PRAGMA '):return
            for lock in (self.market._market_metrics_lock,self.control._diagnostics_lock):
                if lock.acquire(blocking=False):lock.release()
                else:lock_errors.append(statement)
        def connect(*args,**kwargs):
            db=original(*args,**kwargs);db.set_trace_callback(trace);return db
        with patch.object(sqlite3,'connect',side_effect=connect):
            for _ in range(2):
                with self.market.db():pass
                with self.control._db():pass
        self.assertEqual(statements.count('PRAGMA busy_timeout'),2)
        self.assertEqual(statements.count('PRAGMA journal_mode'),2)
        self.assertEqual(lock_errors,[])

    def test_probe_failures_cannot_replace_original_exception(self):
        connect=sqlite3.connect;original=ValueError('original failure')
        class ProbeFailure:
            def __init__(self,db):object.__setattr__(self,'db',db)
            def __getattr__(self,name):return getattr(self.db,name)
            def __setattr__(self,name,value):setattr(self.db,name,value)
            def execute(self,sql,*args):
                if sql.startswith('PRAGMA '):raise sqlite3.OperationalError('probe unavailable')
                return self.db.execute(sql,*args)
        with patch.object(sqlite3,'connect',side_effect=lambda *a,**k:ProbeFailure(connect(*a,**k))):
            with self.assertRaises(ValueError) as caught:
                with self.market.db():raise original
        self.assertIs(caught.exception,original)
        self.assertEqual(original.sqlite_context['busy_timeout_ms'],'unknown')
        self.assertEqual(original.sqlite_context['journal_mode'],'unknown')
        self.assertEqual(len(original.sqlite_context['probe_errors']),2)

    def test_failed_diagnostics_cannot_skip_rollback_or_replace_original_error(self):
        for owner,factory in ((self.market,self.market.db),(self.control,self.control._db)):
            for failure_point in ('measure','probe'):
                with self.subTest(database=type(owner).__name__,failure_point=failure_point):
                    original=ValueError('retain original');measure=owner._measure
                    def broken_measure(kind,phase,started,error=None,**kwargs):
                        if error is not None or phase.lower() in ('rollback','close'):
                            raise RuntimeError('diagnostic failure')
                        return measure(kind,phase,started,error,**kwargs)
                    target=(patch.object(owner,'_measure',side_effect=broken_measure)
                            if failure_point=='measure' else
                            patch.object(market,'_failure_context',side_effect=RuntimeError('probe failure')))
                    with target:
                        with self.assertRaises(ValueError) as caught:
                            with factory() as db:
                                db.execute('INSERT INTO records VALUES(99)')
                                raise original
                    self.assertIs(caught.exception,original)
                    with closing(sqlite3.connect(owner.path)) as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM records WHERE value=99').fetchone()[0],0)

    def test_control_close_and_diagnostic_failures_release_writer_for_another_thread(self):
        connect=sqlite3.connect;measure=self.control._measure
        for broken_diagnostics in (False,True):
            with self.subTest(broken_diagnostics=broken_diagnostics):
                original=OSError('original close failure')
                class CloseFailure:
                    def __init__(self,db):object.__setattr__(self,'db',db)
                    def __getattr__(self,name):return getattr(self.db,name)
                    def __setattr__(self,name,value):setattr(self.db,name,value)
                    def close(self):self.db.close();raise original
                def record(kind,phase,started,error=None,**kwargs):
                    if broken_diagnostics and phase=='CLOSE':raise RuntimeError('failed close metric')
                    return measure(kind,phase,started,error,**kwargs)
                with patch.object(sqlite3,'connect',side_effect=lambda *a,**k:CloseFailure(connect(*a,**k))), \
                     patch.object(self.control,'_measure',side_effect=record):
                    with self.assertRaises(OSError) as caught:
                        with self.control._db() as db:db.execute('INSERT INTO records VALUES(2)')
                self.assertIs(caught.exception,original)
                failures=[]
                def write_after_close():
                    try:
                        with self.control._db(timeout=.05) as db:db.execute('INSERT INTO records VALUES(3)')
                    except BaseException as error:failures.append(error)
                worker=threading.Thread(target=write_after_close);worker.start();worker.join(1)
                self.assertFalse(worker.is_alive());self.assertEqual(failures,[])

    def test_concurrent_lifetime_updates_are_consistent_and_locks_independent(self):
        errors=[]
        def record():
            try:
                for _ in range(200):
                    self.market._measure('fixture','parallel',0,elapsed_ns=2001)
                    self.control._measure('fixture','parallel',0,elapsed_ns=2001)
            except BaseException as error:errors.append(error)
        workers=[threading.Thread(target=record) for _ in range(4)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(2);self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        for owner in (self.market,self.control):
            diagnostics=owner.transaction_diagnostics();self.check_histograms(diagnostics)
            self.assertEqual(diagnostics['fixture:parallel']['count'],800)
        with self.market._market_metrics_lock:
            self.assertEqual(self.control.transaction_diagnostics()['fixture:parallel']['count'],800)


if __name__=='__main__':unittest.main()
