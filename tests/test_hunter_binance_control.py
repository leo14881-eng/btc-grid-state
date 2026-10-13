"""Control protocol tests. TestLock is a declared in-process lock substitute.

Native flock/subprocess/contended two-database acceptance is separate Linux CI.
These tests use real SQLite transactions, files and an injected stable clock.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

from research.hunter_binance_control import Control,ControlError,ScopeQuarantined,digest

EPOCH='a'*32
HEAD='b'*40
NAMES=['algousdt@kline_15m','btcusdt@kline_5m']


class TestLock:
    held={}
    def __init__(self,path,*,create=False,shared=False):
        self.path=Path(path);self.shared=shared;self.closed=False
        if create:
            with self.path.open('xb'):pass
        active=self.held.setdefault(str(self.path),[])
        if active and (not shared or any(not a.shared for a in active)):
            error=BlockingIOError('test lock held');stat=self.path.stat()
            error.owner_identity=(stat.st_dev,stat.st_ino);error.owner_nlink=stat.st_nlink
            raise error
        stat=self.path.stat();self.identity=(stat.st_dev,stat.st_ino)
        active.append(self)
    def verify(self):
        if self.closed:raise ControlError('TEST_OWNER_CLOSED')
        stat=self.path.stat()
        if self.identity!=(stat.st_dev,stat.st_ino):raise ControlError('TEST_OWNER_CHANGED')
    def close(self):
        if not self.closed:
            self.held[str(self.path)].remove(self);self.closed=True


def make_control(folder,*,clock=None,wall=None):
    path=Path(folder)/'market.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE budget(id INTEGER PRIMARY KEY,next_ms INTEGER,cooldown_ms INTEGER,halt TEXT,last_ms INTEGER);
            INSERT INTO budget VALUES(1,0,0,NULL,0);
            CREATE TABLE budget_clock(id INTEGER PRIMARY KEY,boot TEXT);
            CREATE TABLE requests(token TEXT PRIMARY KEY,denied INTEGER);
            CREATE TABLE leases(token TEXT PRIMARY KEY,acquired_ms INTEGER);''')
    options=dict(boot='test-boot',lock_factory=TestLock)
    if clock:options['clock']=clock
    if wall:options['wall']=wall
    control=Control.provision(Path(folder)/'control.sqlite3',path,**options)
    control.begin_epoch(EPOCH,HEAD,digest(NAMES),manifest_sha256='c'*64,names=NAMES)
    return control


class ControlTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.folder=Path(temp.name);self.mono=100.;self.wall=1791878400.
        self.c=make_control(self.folder,clock=lambda:self.mono,wall=lambda:self.wall)
        self.context=dict(epoch=EPOCH,job_key=('repair',NAMES[0]),scope=[NAMES[0]],kind='repair')

    def owner(self,context=None):
        context=context or self.context
        call=self.c.begin_call(context)
        owner=self.c.admit(context,call,1,'/api/v3/klines',2,self.mono+30)
        self.assertIsNotNone(owner)
        return owner

    def retire(self,owner):
        owner.not_sent=True
        self.c.prepare_retire(owner,self.mono+1)
        self.c.retire(owner)
        self.c.end_call(owner.request_id,'RETIRED',{})

    def test_existing_only_and_reader_never_creates_state(self):
        with self.assertRaises(ControlError):Control(self.folder/'absent.sqlite3',boot='test-boot')
        r=Control.reader(self.c.path,boot='test-boot',lock_factory=TestLock)
        self.assertEqual(r.fence(NAMES)['epoch'],EPOCH)
        with self.assertRaisesRegex(ControlError,'READONLY'):r.begin_call(self.context)
        self.assertFalse((self.folder/'absent.sqlite3').exists())

    def test_retirement_keeps_original_lock_until_atomic_delete_and_never_refunds(self):
        owner=self.owner();charged=self.c.status()['charged_total']
        owner.not_sent=True;self.c.prepare_retire(owner,self.mono+1)
        with self.assertRaises(BlockingIOError):TestLock(self.c._owner_path(owner.token),shared=True)
        with self.c._db(readonly=True) as db:
            self.assertEqual([db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('requests','owners')],[1,1])
        self.c.retire(owner)
        self.assertTrue(owner.retired);self.assertTrue(owner.lock.closed)
        self.assertEqual(self.c.status()['charged_total'],charged)
        self.assertEqual(self.c.status()['active_owners'],0)

    def test_any_relevant_http_retirement_revokes_previously_held_fence(self):
        fence=self.c.fence([NAMES[0]])
        outside=self.c.fence([NAMES[1]])
        owner=self.owner();self.retire(owner)
        with self.assertRaisesRegex(ControlError,'FENCE_CHANGED'):self.c.validate_fence(fence,[NAMES[0]])
        self.assertTrue(self.c.validate_fence(outside,[NAMES[1]]))

    def test_external_reader_rejects_held_lock_evidence_for_another_inode(self):
        owner=self.owner();path=self.c._owner_path(owner.token)
        if sys.platform=='linux':
            # Linux exercises actual replacement. Windows sandbox forbids
            # rename here, so its branch injects explicit foreign-inode proof.
            path.rename(path.with_suffix('.old'))
            path.write_bytes(b'')
        def unrelated_lock(candidate,**kwargs):
            if Path(candidate)==path:
                error=BlockingIOError('different inode lock held')
                stat=path.stat()
                error.owner_identity=((stat.st_dev,stat.st_ino) if sys.platform=='linux'
                    else (owner.lock.identity[0],owner.lock.identity[1]+1))
                error.owner_nlink=1
                raise error
            return TestLock(candidate,**kwargs)
        reader=Control.reader(self.c.path,boot='test-boot',lock_factory=unrelated_lock)
        with self.assertRaisesRegex(ControlError,'OWNER_BINDING_MISMATCH'):reader.fence([NAMES[1]])

    def test_control_cleanup_busy_prearmed_scope_gate_is_independent_of_next_write(self):
        owner=self.owner();owner.not_sent=True;self.c.prepare_retire(owner,self.mono+.1)
        writer=sqlite3.connect(self.c.path,timeout=0);writer.execute('BEGIN IMMEDIATE')
        try:
            self.mono+=.2
            with self.assertRaises(sqlite3.OperationalError):self.c.retire(owner)
            with self.assertRaises(ScopeQuarantined):self.c.fence([NAMES[0]])
            self.assertEqual(self.c.fence([NAMES[1]])['epoch'],EPOCH)
        finally:writer.rollback();writer.close()
        q=self.c.quarantine(owner,'BUSY');self.c.retire(owner)
        self.assertEqual(self.c.claim_requeue(EPOCH,self.context['job_key']),q)
        self.assertIsNone(self.c.claim_requeue(EPOCH,self.context['job_key']))
        with self.assertRaises(ScopeQuarantined):self.c.fence([NAMES[0]])
        self.assertTrue(self.c.can_schedule([NAMES[0]],EPOCH,q))
        self.assertFalse(self.c.can_schedule([NAMES[0]],EPOCH))

    def test_orphan_is_global_stop_even_when_raw_budget_halt_is_null(self):
        owner=self.owner();owner.lock.close()
        with self.assertRaises(ControlError):self.c.fence([NAMES[1]])
        with self.c._db(readonly=True) as db:self.assertIsNone(db.execute('SELECT halt FROM budget').fetchone()[0])

    def test_missing_reap_or_completion_cannot_retire_owner(self):
        owner=self.owner();owner.go=True
        with self.assertRaisesRegex(ControlError,'REAP'):self.c.prepare_retire(owner,self.mono+1)
        owner.reaped=True
        with self.assertRaisesRegex(ControlError,'COMPLETION'):self.c.prepare_retire(owner,self.mono+1)
        self.assertEqual(self.c.status()['active_owners'],1)

    def test_four_slot_limit_includes_held_cleanup_owner(self):
        owners=[]
        for _ in range(4):
            owners.append(self.owner());self.mono+=.21
        call=self.c.begin_call(self.context)
        self.assertIsNone(self.c.admit(self.context,call,1,'/api/v3/klines',2,self.mono+30))
        self.assertEqual(self.c.status()['active_owners'],4)
        for owner in owners:self.retire(owner)

    def test_pacing_wait_avoids_owner_files_and_control_writer(self):
        owner=self.owner();self.retire(owner)
        call=self.c.begin_call(self.context)
        writer=sqlite3.connect(self.c.path,timeout=0);writer.execute('BEGIN IMMEDIATE')
        before=list(self.folder.glob('*.lock'))
        try:
            self.assertIsNone(self.c.admit(self.context,call,1,'/api/v3/klines',2,self.mono+30))
            self.assertEqual(list(self.folder.glob('*.lock')),before)
            self.assertGreaterEqual(self.c.admission_delay(),.19)
            self.assertLessEqual(self.c.admission_delay(),.25)
        finally:writer.rollback();writer.close()
        self.assertEqual(self.c.status()['charged_total'],2)
        self.mono+=.21
        admitted=self.c.admit(self.context,call,1,'/api/v3/klines',2,self.mono+30)
        self.assertIsNotNone(admitted);self.retire(admitted)

    def test_same_window_header_is_monotonic_and_old_window_cannot_reset(self):
        owner=self.owner();before=self.c.status();window=before['window_ms']
        self.c.calibrate(100,window);self.c.calibrate(20,window);self.c.calibrate(500,window-60000)
        self.assertEqual(self.c.status()['used'],100)
        self.retire(owner)
        self.assertEqual(self.c.status()['used'],100)
        self.assertEqual(self.c.status()['charged_total'],2)

    def test_wall_clock_jump_does_not_reset_before_monotonic_window_end(self):
        owner=self.owner();window=self.c.status()['window_ms']
        self.c.calibrate(600,window);self.retire(owner)
        self.wall+=120;self.mono+=.21
        call=self.c.begin_call(self.context)
        self.assertIsNone(self.c.admit(self.context,call,1,'/api/v3/klines',2,self.mono+30))
        self.mono+=60
        new=self.c.admit(self.context,call,1,'/api/v3/klines',2,self.mono+30)
        self.assertIsNotNone(new);self.assertEqual(self.c.status()['used'],2)
        self.assertEqual(self.c.status()['charged_total'],4);self.retire(new)

    def test_publication_pending_and_failed_gate_until_matching_new_receipt(self):
        payload=dict(kind='repair',body=[],requested=1)
        identity=self.c.publication_intent(EPOCH,self.context['job_key'],[NAMES[0]],digest(payload),payload=payload)
        with self.assertRaises(ScopeQuarantined):self.c.fence([NAMES[0]])
        self.c.reject_publication(identity,'MARKET_WRITE_FAILED')
        with self.assertRaises(ScopeQuarantined):self.c.fence([NAMES[0]])
        self.assertEqual(self.c.fence([NAMES[1]])['epoch'],EPOCH)
        newer=dict(payload,requested=2)
        new=self.c.publication_intent(EPOCH,self.context['job_key'],[NAMES[0]],digest(newer),payload=newer)
        receipt=dict(intent_id=new,epoch=EPOCH,payload_hash=digest(newer),scope_hash=digest([NAMES[0]]))
        self.c.reconcile(new,receipt,supersede_failed=True)
        self.assertEqual(self.c.get_intent(identity)['state'],'SUPERSEDED')
        self.assertTrue(self.c.fence([NAMES[0]]))

    def test_quarantine_recovery_requires_new_committed_publication(self):
        owner=self.owner();owner.not_sent=True;self.c.prepare_retire(owner,self.mono+1)
        q=self.c.quarantine(owner,'BUSY');self.c.retire(owner)
        self.c.claim_requeue(EPOCH,self.context['job_key'])
        payload=dict(kind='repair',body=[],requested=2)
        p=self.c.publication_intent(EPOCH,self.context['job_key'],[NAMES[0]],digest(payload),payload=payload,recovery_id=q)
        with self.assertRaises(ScopeQuarantined):self.c.fence([NAMES[0]])
        receipt=dict(intent_id=p,epoch=EPOCH,payload_hash=digest(payload),scope_hash=digest([NAMES[0]]))
        self.c.reconcile(p,receipt)
        self.assertTrue(self.c.fence([NAMES[0]]))
        self.assertIsNone(self.c.claim_requeue(EPOCH,self.context['job_key']))

    def test_read_fence_rejects_transient_gate_even_after_reconciliation(self):
        fence=self.c.fence([NAMES[0]])
        payload={};p=self.c.publication_intent(EPOCH,'x',[NAMES[0]],digest(payload),payload=payload)
        self.c.reconcile(p,dict(intent_id=p,epoch=EPOCH,payload_hash=digest(payload),scope_hash=digest([NAMES[0]])))
        with self.assertRaisesRegex(ControlError,'FENCE_CHANGED'):self.c.validate_fence(fence,[NAMES[0]])

    def test_draining_denies_new_work_but_allows_original_owner_cleanup(self):
        owner=self.owner();self.c.begin_draining(EPOCH)
        pending=self.c.begin_call(self.context)
        with self.assertRaises(ControlError):self.c.admit(self.context,pending,1,'/api/v3/klines',2,self.mono+30)
        self.c.end_call(pending,'NOT_ADMITTED',{})
        self.retire(owner);self.c.end_epoch(EPOCH,'STOP')
        self.assertTrue(self.c.terminal_summary(EPOCH)['all_retired'])

    def test_legacy_owner_or_cooldown_cannot_initialize_fresh_budget(self):
        with sqlite3.connect(self.folder/'market.sqlite3') as db:
            db.execute('INSERT INTO requests VALUES(?,0)',('f'*32,))
        with self.assertRaisesRegex(ControlError,'LEGACY_OWNER'):
            Control.provision(self.folder/'unsafe.sqlite3',self.folder/'market.sqlite3',boot='test-boot',lock_factory=TestLock)
        self.assertFalse((self.folder/'unsafe.sqlite3').exists())

    def test_legacy_halt_cooldown_and_unmatched_lease_each_block_provision(self):
        for name,statement in [('halt',"UPDATE budget SET halt='REVIEW'"),
                ('cooldown','UPDATE budget SET cooldown_ms=100001'),
                ('lease',"INSERT INTO leases VALUES('pending',1)")]:
            with self.subTest(name=name):
                with sqlite3.connect(self.folder/'market.sqlite3') as db:
                    db.execute('UPDATE budget SET halt=NULL,cooldown_ms=0')
                    db.execute('DELETE FROM leases')
                    db.execute("INSERT OR REPLACE INTO budget_clock VALUES(1,'test-boot')")
                    db.execute(statement)
                with self.assertRaises(ControlError):
                    Control.provision(self.folder/(name+'.sqlite3'),self.folder/'market.sqlite3',
                        boot='test-boot',lock_factory=TestLock,clock=lambda:self.mono,wall=lambda:self.wall)
                self.assertFalse((self.folder/(name+'.sqlite3')).exists())

    def test_applied_reconcile_can_explicitly_supersede_prior_failed_intent(self):
        old={"requested":1};new={"requested":2};scope=[NAMES[0]]
        first=self.c.publication_intent(EPOCH,'same',scope,digest(old),payload=old)
        self.c.reject_publication(first,'FAILED')
        second=self.c.publication_intent(EPOCH,'same',scope,digest(new),payload=new)
        receipt=dict(intent_id=second,epoch=EPOCH,payload_hash=digest(new),scope_hash=digest(scope))
        self.c.reconcile(second,receipt)
        with self.assertRaises(ScopeQuarantined):self.c.fence(scope)
        self.c.reconcile(second,receipt,supersede_failed=True)
        self.assertTrue(self.c.fence(scope))

    def test_old_applied_or_pending_success_never_supersedes_newer_failure(self):
        for applied in (False,True):
            with self.subTest(applied=applied):
                key=['ordered',applied];scope=[NAMES[0]]
                old={'requested':1};new={'requested':2}
                first=self.c.publication_intent(EPOCH,key,scope,digest(old),payload=old)
                receipt=dict(intent_id=first,epoch=EPOCH,payload_hash=digest(old),scope_hash=digest(scope))
                if applied:self.c.reconcile(first,receipt)
                second=self.c.publication_intent(EPOCH,key,scope,digest(new),payload=new)
                self.c.reject_publication(second,'NEWER_INVALID_RESULT')
                self.c.reconcile(first,receipt,supersede_failed=True)
                self.assertEqual(self.c.get_intent(second)['state'],'FAILED')
                self.assertLess(self.c.get_intent(first)['created_seq'],self.c.get_intent(second)['created_seq'])
                with self.assertRaises(ScopeQuarantined):self.c.fence(scope)

    def test_independent_halt_latch_blocks_unrelated_scope(self):
        with self.assertRaises(ControlError):self.c.latch_stop('UNCONFIRMED')
        with self.assertRaises(ControlError):self.c.fence([NAMES[1]])


if __name__=='__main__':unittest.main()
