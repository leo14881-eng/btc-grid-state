"""Transport/storage failure boundaries, all fixtures and temporary SQLite."""
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from research import hunter_bybit_stream as stream
from research.hunter_bybit_stream_store import Store, Budget, topic, FRESH_MS
from test_hunter_stream import NOW


class FaultTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'state.sqlite3')
        self.topics=[topic('BTCUSDT','15')]
        self.epoch=self.store.begin_session(self.topics,'a'*64)

    def no_leases(self):
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_known_403_survives_failed_or_oversized_body_and_halts(self):
        for mode in ('timeout','oserror','incomplete','oversize'):
            class Response:
                code=403
                headers={}
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self,size):
                    if mode=='timeout':raise TimeoutError('fixture')
                    if mode=='oserror':raise OSError('fixture')
                    if mode=='incomplete':raise stream.http.client.IncompleteRead(b'fixture',20)
                    return b'x'*size
            class Opener:
                def open(self,*args,**kwargs):return Response()
            store=Store(Path(self.temp.name)/(mode+'.sqlite3'))
            epoch=store.begin_session(self.topics,'b'*64)
            with patch.object(stream.urllib.request,'build_opener',return_value=Opener()) as opener:
                with self.assertRaisesRegex(RuntimeError,'UNKNOWN_403_MANUAL_REVIEW'):
                    stream.repair(store,epoch,self.topics[0],NOW+1000,get=stream.direct_get,clock=lambda:NOW)
            self.assertEqual(opener.call_count,1)
            self.assertEqual(Budget(store).status()['halt'],'UNKNOWN_403_MANUAL_REVIEW')

    def test_cancelled_budget_wait_never_sends_or_waits_for_cooldown(self):
        stopped=Event();clock=[NOW];calls=[]
        Budget(self.store).failure(429,'',NOW,300)
        def sleep(seconds):clock[0]+=50;stopped.set()
        with self.assertRaisesRegex(RuntimeError,'RECOVERY_CANCELLED'):
            stream.repair(self.store,self.epoch,self.topics[0],NOW+600000,
                lambda url:calls.append(url),lambda:clock[0],sleep,cancelled=stopped.is_set)
        self.assertEqual(calls,[])
        self.assertEqual(clock[0],NOW+50)
        self.no_leases()

    def test_cancellation_or_deadline_after_lease_releases_without_request(self):
        for reason in ('cancel','deadline'):
            clock=[NOW];stopped=Event();calls=[]
            original=Budget.acquire
            def acquire(budget,now_ms=None):
                token=original(budget,now_ms)
                if reason=='cancel':stopped.set()
                else:clock[0]=NOW+1000
                return token
            with patch.object(Budget,'acquire',acquire):
                with self.assertRaisesRegex((RuntimeError,TimeoutError),'RECOVERY_CANCELLED|RECOVERY_DEADLINE'):
                    stream.repair(self.store,self.epoch,self.topics[0],NOW+1000,
                        lambda url:calls.append(url),lambda:clock[0],lambda seconds:None,cancelled=stopped.is_set)
            self.assertEqual(calls,[])
            self.no_leases()
            # Use a later timestamp for the second independent scenario.
            with self.store.db() as db:db.execute('UPDATE budget SET next_ms=0')

    def test_disconnected_epoch_stops_without_network(self):
        self.store.disconnect(self.epoch)
        calls=[]
        with self.assertRaisesRegex(ValueError,'STALE_SESSION'):
            stream.repair(self.store,self.epoch,self.topics[0],NOW+1000,
                lambda url:calls.append(url),lambda:NOW,lambda seconds:None)
        self.assertEqual(calls,[])

    def test_already_inflight_403_persists_after_cancellation(self):
        stopped=Event();calls=[]
        def get(url):
            calls.append(url);stopped.set()
            return 403,b'country',1
        with self.assertRaisesRegex(RuntimeError,'COUNTRY_403_MANUAL_REVIEW'):
            stream.repair(self.store,self.epoch,self.topics[0],NOW+1000,get,
                lambda:NOW,lambda seconds:None,cancelled=stopped.is_set)
        self.assertEqual(len(calls),1)
        self.assertEqual(Budget(self.store).status()['halt'],'COUNTRY_403_MANUAL_REVIEW')
        self.no_leases()

    def test_session_disconnect_cancels_running_repair_before_retry(self):
        started=Event();closed=Event();calls=[];sent=[];attempt=[0]
        original=self.store.disconnect
        def disconnect(epoch):
            try:original(epoch)
            finally:closed.set()
        class WS:
            def send(self,payload):sent.append(json.loads(payload))
            def recv(self,timeout):
                attempt[0]+=1
                if attempt[0]==1:
                    return json.dumps(dict(op='subscribe',req_id=sent[0]['req_id'],success=True))
                if not started.wait(2):raise AssertionError('repair did not begin')
                raise OSError('fixture disconnect')
        def get(url):
            calls.append(url);started.set()
            if not closed.wait(2):raise AssertionError('session did not disconnect')
            return 503,b'fixture',300
        def repairer(store,epoch,name,deadline,*,cancelled):
            return stream.repair(store,epoch,name,deadline,get,lambda:NOW,cancelled=cancelled)
        with patch.object(self.store,'disconnect',disconnect):
            with self.assertRaisesRegex(OSError,'fixture disconnect'):
                stream.session(self.store,self.topics,'a'*64,WS(),lambda:False,
                    lambda:NOW,lambda seconds:None,repairer)
        self.assertEqual(len(calls),1)
        self.no_leases()

    def test_storage_failure_stops_connections_without_reconnect(self):
        opened=[]
        @contextlib.contextmanager
        def connect():
            opened.append(1)
            yield object()
        with patch.object(stream,'session',side_effect=sqlite3.OperationalError('database or disk is full')):
            with self.assertRaisesRegex(RuntimeError,'CACHE_STORAGE_UNAVAILABLE'):
                stream.collect(self.store,self.topics,'a'*64,connect,lambda:NOW,lambda seconds:None)
        self.assertEqual(len(opened),1)

    def test_unpersisted_denial_is_explicit_and_never_reconnects(self):
        opened=[]
        @contextlib.contextmanager
        def connect():
            opened.append(1)
            yield object()
        def failed_session(*args,**kwargs):
            stream.record_failure(Budget(self.store),403,'country',NOW)
        with patch.object(Budget,'failure',side_effect=sqlite3.OperationalError('disk is full')), \
             patch.object(stream,'session',side_effect=failed_session):
            with self.assertRaisesRegex(stream.DurableStopError,'DENIAL_STOP_NOT_DURABLE_MANUAL_REVIEW'):
                stream.collect(self.store,self.topics,'a'*64,connect,lambda:NOW,lambda seconds:None)
        self.assertEqual(len(opened),1)
        self.assertIsNone(Budget(self.store).status()['halt'])  # Never claim a durable stop was written.

    def test_disconnect_does_not_hide_late_unpersisted_denial_in_future(self):
        started=Event();closed=Event();opened=[];sent=[];reads=[0]
        original_disconnect=self.store.disconnect
        real_session=stream.session
        def disconnect(epoch):
            try:original_disconnect(epoch)
            finally:closed.set()
        class WS:
            def send(self,payload):sent.append(json.loads(payload))
            def recv(self,timeout):
                reads[0]+=1
                if reads[0]==1:
                    return json.dumps(dict(op='subscribe',req_id=sent[0]['req_id'],success=True))
                if not started.wait(2):raise AssertionError('repair did not begin')
                raise OSError('fixture WS disconnected before HTTP completed')
        @contextlib.contextmanager
        def connect():opened.append(1);yield WS()
        def get(url):
            started.set()
            if not closed.wait(2):raise AssertionError('session did not disconnect')
            return 403,b'country',1
        def repairer(store,epoch,name,deadline,*,cancelled):
            return stream.repair(store,epoch,name,deadline,get,lambda:NOW,cancelled=cancelled)
        def session(store,topics,scope,ws,stop,clock,sleep,**kwargs):
            return real_session(store,topics,scope,ws,stop,clock,sleep,repairer,**kwargs)
        with patch.object(self.store,'disconnect',disconnect),patch.object(stream,'session',session), \
             patch.object(Budget,'failure',side_effect=sqlite3.OperationalError('disk is full')):
            with self.assertRaisesRegex(stream.DurableStopError,'DENIAL_STOP_NOT_DURABLE_MANUAL_REVIEW'):
                stream.collect(self.store,self.topics,'a'*64,connect,lambda:NOW,lambda seconds:None)
        self.assertEqual(len(opened),1)
        # Explicit fixture clock cannot become a production clock after restart.
        self.assertIn('MIGRATION',Budget(self.store).status()['halt'])
        self.no_leases()

    def test_denial_and_lease_release_failure_preserves_durable_stop_diagnosis(self):
        with patch.object(Budget,'failure',side_effect=sqlite3.OperationalError('disk is full')), \
             patch.object(Budget,'release',side_effect=sqlite3.OperationalError('release also failed')):
            with self.assertRaisesRegex(stream.DurableStopError,'DENIAL_STOP_NOT_DURABLE_MANUAL_REVIEW'):
                stream.repair(self.store,self.epoch,self.topics[0],NOW+1000,
                    lambda url:(403,b'country',1),lambda:NOW,lambda seconds:None)
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],1)
        self.assertIn('MIGRATION',Budget(self.store).status()['halt'])

    def test_stop_before_connect_and_during_backoff(self):
        calls=[];stopped=Event()
        def connect():calls.append(1);raise OSError('fixture')
        stream.collect(self.store,self.topics,'a'*64,connect,lambda:NOW,lambda seconds:None,lambda:True)
        self.assertEqual(calls,[])
        stream.collect(self.store,self.topics,'a'*64,connect,lambda:NOW,lambda seconds:stopped.set(),stopped.is_set)
        self.assertEqual(len(calls),1)


if __name__=='__main__':unittest.main()
