"""New coordinated HTTP/control branch; explicit proof, no real socket claim."""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research import hunter_binance_public as public
from research.hunter_binance_control import ScopeQuarantined
from tests.test_hunter_binance_control import make_control,EPOCH,NAMES


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.control=make_control(Path(temp.name))
        self.context=dict(epoch=EPOCH,job_key=('repair',NAMES[0]),scope=[NAMES[0]],kind='repair')
        self.events=[];self.sent=0
        self.client=public.Client(SimpleNamespace(control=self.control),transport=self.transport)

    def transport(self,url,deadline,cancelled,denial,before_go,**options):
        before_go();self.sent+=1;observe=options['observe']
        observe('GO');observe('HEADERS',status=200);observe('RESPONSE',status=200);observe('REAPED')
        return 200,b'[]',1

    def request(self,**kwargs):
        return self.client.request('/api/v3/klines',dict(symbol='ALGOUSDT',interval='15m',limit=25,endTime=1),
            context=self.context,observe=lambda event,**fields:self.events.append((event,fields)),**kwargs)

    def test_completed_response_retires_and_records_one_call_terminal(self):
        self.assertEqual(self.request(),[])
        s=self.control.terminal_summary(EPOCH)
        self.assertEqual((s['allocated'],s['terminal'],s['attempts_allocated'],s['held']),(1,1,1,0))
        self.assertTrue(s['complete']);self.assertTrue(s['all_retired'])
        self.assertEqual([e for e,_ in self.events].count('REQUEST_END'),1)

    def test_cancel_before_admission_records_terminal_without_http_or_slot(self):
        with self.assertRaises(TimeoutError):self.request(cancelled=lambda:True)
        self.assertEqual(self.sent,0);self.assertEqual(self.control.status()['active_owners'],0)
        self.assertEqual(self.events[-1][0],'REQUEST_END')
        self.assertEqual(self.control.terminal_summary(EPOCH)['allocated'],1)

    def test_cleanup_contention_discards_response_retires_and_requeues_once(self):
        original=self.control.retire
        def busy_until_quarantine(owner):
            if owner.quarantine_id is None:raise sqlite3.OperationalError('database is locked')
            return original(owner)
        with patch.object(public,'RETIRE_GRACE_SECONDS',.05),patch.object(self.control,'retire',busy_until_quarantine):
            with self.assertRaises(public.QuarantinedRequestError) as caught:self.request()
        self.assertEqual(self.sent,1)
        self.assertEqual(self.control.status()['active_owners'],0)
        self.assertEqual(self.control.status()['charged_total'],2)
        with self.assertRaises(ScopeQuarantined):self.control.fence([NAMES[0]])
        self.assertTrue(self.control.fence([NAMES[1]]))
        self.assertEqual(self.control.claim_requeue(EPOCH,self.context['job_key']),caught.exception.requeue_id)
        self.assertIsNone(self.control.claim_requeue(EPOCH,self.context['job_key']))
        s=self.control.terminal_summary(EPOCH)
        self.assertTrue(s['complete']);self.assertEqual(s['calls'][0]['terminal'],'QUARANTINED_RETIRED')

    def test_incomplete_even_reaped_is_manual_review_and_keeps_slot_and_charge(self):
        def incomplete(url,deadline,cancelled,denial,before_go,**options):
            before_go();self.sent+=1;options['observe']('GO');options['observe']('REAPED')
            raise public.UncertainRequestError('UNKNOWN_RESPONSE')
        self.client.transport=incomplete
        with self.assertRaises(public.UncertainRequestError):self.request()
        with self.control._db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM owners').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT charged_total FROM budget').fetchone()[0],2)
        s=self.control.terminal_summary(EPOCH)
        self.assertTrue(s['complete']);self.assertFalse(s['all_retired'])
        self.assertEqual(s['attempts'][0]['terminal'],'RETAINED_MANUAL_REVIEW')

    def test_cancellation_after_go_drains_but_does_not_publish(self):
        stopped=[False]
        original=self.client.transport
        def response(*args,**kwargs):
            value=original(*args,**kwargs);stopped[0]=True;return value
        self.client.transport=response
        with self.assertRaises(TimeoutError):self.request(cancelled=lambda:stopped[0],drain_on_cancel=True)
        self.assertEqual(self.control.status()['active_owners'],0)
        self.assertEqual(self.sent,1)


if __name__=='__main__':unittest.main()
