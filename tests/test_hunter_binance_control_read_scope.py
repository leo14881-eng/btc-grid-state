"""Event-controlled SQLite read scope, with the declared TestLock substitute."""
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from research import hunter_binance_control as control
from tests.test_hunter_binance_control import make_control, EPOCH, NAMES


class ControlReadScopeTests(unittest.TestCase):
    def setUp(self):
        scratch=tempfile.TemporaryDirectory();self.addCleanup(scratch.cleanup)
        self.c=make_control(Path(scratch.name))
        self.context=dict(epoch=EPOCH,job_key=('seed',NAMES[0]),scope=[NAMES[0]])

    def intent(self,name=None):
        name=name or NAMES[0]
        payload=dict(kind='seed',names=[name],body='tiny decoder fixture')
        return self.c.publication_intent(EPOCH,('seed',name),[name],control.digest(payload),payload=payload)

    def while_decoder_paused(self,read,*,patch_target=None,write=None):
        entered=threading.Event();release=threading.Event();values=[];errors=[];write_error=None
        owner,attribute=patch_target or (self.c,'_intent')
        original=getattr(owner,attribute)
        def decode(*args,**kwargs):
            entered.set()
            if not release.wait(2):raise AssertionError('FIXTURE_DECODER_RELEASE_NOT_RECEIVED')
            return original(*args,**kwargs)
        def reader():
            try:values.append(read())
            except BaseException as error:errors.append(error)
        worker=threading.Thread(target=reader)
        with patch.object(owner,attribute,side_effect=decode):
            worker.start()
            try:
                self.assertTrue(entered.wait(1),'reader never reached decoder')
                # The actual default 250ms SQLite timeout is unchanged. No
                # sleep race: decoder cannot resume until this write returns.
                (write or (lambda:self.c.begin_call(self.context)))()
                self.assertFalse(release.is_set())
            except sqlite3.OperationalError as error:
                write_error=error
            finally:
                release.set();worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(write_error,repr(getattr(write_error,'sqlite_context',None)))
        return values,errors

    def test_get_intent_decoding_does_not_hold_read_transaction(self):
        identity=self.intent()
        values,errors=self.while_decoder_paused(lambda:self.c.get_intent(identity))
        self.assertEqual(errors,[])
        self.assertEqual(values[0]['id'],identity)

    def test_pending_intents_decoding_does_not_hold_read_transaction(self):
        identity=self.intent()
        values,errors=self.while_decoder_paused(lambda:self.c.pending_intents(EPOCH))
        self.assertEqual(errors,[])
        self.assertEqual([row['id'] for row in values[0]],[identity])

    def test_fence_publication_decode_releases_read_lock_but_keeps_denial(self):
        self.intent()
        values,errors=self.while_decoder_paused(lambda:self.c.fence([NAMES[0]]),
            patch_target=(control.json,'loads'))
        self.assertEqual(values,[])
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],control.ScopeQuarantined)
        self.assertEqual(str(errors[0]),'CONTROL_PUBLICATION_PENDING')

    def test_fence_quarantine_decode_releases_read_lock_but_keeps_denial(self):
        with self.c._db() as db:
            db.execute('INSERT INTO quarantines VALUES(?,?,?,?,?,?,?,?)',
                ('q','token',EPOCH,'job',control.canonical([NAMES[0]]),'fixture','REVERIFY_PENDING','PENDING'))
        values,errors=self.while_decoder_paused(lambda:self.c.fence([NAMES[0]]),
            patch_target=(control.json,'loads'))
        self.assertEqual(values,[])
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],control.ScopeQuarantined)
        self.assertEqual(str(errors[0]),'CONTROL_SCOPE_QUARANTINED')

    def test_fence_retiring_scope_decode_releases_read_lock_but_keeps_denial(self):
        request=self.c.begin_call(self.context)
        owner=self.c.admit(self.context,request,1,'/api/v3/klines',2,self.c.clock()+30)
        self.assertIsNotNone(owner)
        owner.not_sent=True
        self.c.prepare_retire(owner,self.c.clock()-1)
        try:
            values,errors=self.while_decoder_paused(lambda:self.c.fence([NAMES[0]]),
                patch_target=(control.json,'loads'))
            self.assertEqual(values,[])
            self.assertEqual(len(errors),1)
            self.assertIsInstance(errors[0],control.ScopeQuarantined)
            self.assertEqual(str(errors[0]),'CONTROL_SCOPE_QUARANTINED')
        finally:
            self.c.retire(owner)
            self.c.end_call(request,'RETIRED',{})

    def test_fence_copies_versions_and_publications_from_same_snapshot(self):
        self.intent(NAMES[1])
        before=self.c.fence([NAMES[0]])
        values,errors=self.while_decoder_paused(lambda:self.c.fence([NAMES[0]]),
            patch_target=(control.json,'loads'),write=lambda:self.intent(NAMES[0]))
        self.assertEqual(errors,[])
        self.assertEqual(values,[before])
        # The old captured fence is revoked by a subsequent fresh comparison.
        with self.assertRaisesRegex(control.ScopeQuarantined,'PUBLICATION_PENDING'):
            self.c.validate_fence(values[0],[NAMES[0]])


if __name__=='__main__':unittest.main()
