"""Deterministic admission and ingress races; real HTTP child, loopback only."""
import contextlib
import io
from pathlib import Path
import os
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from research import hunter_binance_public as public
from research import hunter_binance_stream as stream
from research.hunter_binance_stream_store import Store
import test_hunter_binance_transport_process as process_fixture


class AdmissionRaceTests(unittest.TestCase):
    def check_cancel(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state.sqlite3');cancelled=Event()
            server=process_fixture.Server()
            try:
                command=lambda url:[sys.executable,'-B',str(process_fixture.HELPER),url,
                    str(os.getpid()),str(server.port),directory,'normal']
                client=public.Client(store);status=client.budget.status;calls=[];children=[]
                def status_then_cancel(**kwargs):
                    result=status(**kwargs);calls.append('status')
                    if boundary=='status':cancelled.set()
                    return result
                def transport(url,deadline,check,on_denial,before_go,**kwargs):
                    def checked():
                        before_go()
                        if boundary=='go':cancelled.set()
                    return public.exchange(url,deadline,check,on_denial,checked,**kwargs)
                client.transport=transport
                popen=public.subprocess.Popen
                def spawn(*args,**kwargs):
                    child=popen(*args,**kwargs);children.append(child);return child
                with patch.object(client.budget,'status',status_then_cancel),\
                     patch.object(public,'worker_command',command),patch.object(public.subprocess,'Popen',spawn):
                    with self.assertRaises(public.NotSentError):
                        client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),
                            timeout=3,cancelled=cancelled.is_set,drain_on_cancel=True)
                self.assertEqual(calls,['status']);self.assertTrue(cancelled.is_set())
                self.assertFalse(server.ready.is_set(),'cancelled admission sent an HTTP request')
                self.assertTrue(children);self.assertTrue(all(child.poll() is not None for child in children))
                with store.db(readonly=True) as db:
                    self.assertEqual(tuple(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                        for name in ('requests','leases','bars')),(0,0,0))
                self.assertEqual(list(Path(directory).glob('request-*.lock')),[])
            finally:server.close()

    def test_cancel_during_budget_status_never_sends_go(self):
        self.check_cancel('status')

    def test_cancel_after_admission_callback_never_sends_go(self):
        self.check_cancel('go')


class IngressRaceTests(unittest.TestCase):
    def check_ingress(self, age, accepted):
        # The receiver advances the shared clock after session sampled its scan
        # time. No scheduling sleep can affect this exact interleaving.
        now=[100001];stopped=Event();name='btcusdt@kline_15m';reasons=[]
        protocol=Mock(epoch='e',started=0,requests=[],topic_shards={name:0})
        def arrival(topic):
            self.assertEqual(topic,name);now[0]=100002
            return now[0]-age
        protocol.market_arrival.side_effect=arrival
        store=Mock();store.states.return_value=[dict(topic=name,ack=1,gap=0,epoch='e',ws_rx=0,ws_ts=0)]
        def nonready(reason):
            reasons.append(reason)
            if reason=='CACHE_WRITER_LAG':stopped.set()
        with patch.object(stream,'Protocol',return_value=protocol),\
             patch.object(stream,'Budget') as budget,contextlib.redirect_stdout(io.StringIO()):
            budget.return_value.status.return_value=dict(halt=None,cooldown_ms=0)
            if accepted:
                stream.session(store,[name],'a'*64,stopped.is_set,clock=lambda:now[0],on_nonready=nonready)
                self.assertIn('CACHE_WRITER_LAG',reasons)
            else:
                with self.assertRaisesRegex(TimeoutError,'BINANCE_TOPIC_STALE'):
                    stream.session(store,[name],'a'*64,stopped.is_set,clock=lambda:now[0],on_nonready=nonready)
                self.assertNotIn('CACHE_WRITER_LAG',reasons)
        self.assertIn('TOPIC_ACK_GAP_OR_FRESHNESS_UNKNOWN',reasons)
        store.disconnect.assert_called_once_with('e')

    def test_ingress_after_scan_timestamp_is_recent(self):
        self.check_ingress(0,True)

    def test_ingress_still_requires_nonfuture_and_90_second_limit(self):
        for age,accepted in ((-1,False),(90000,True),(90001,False)):
            with self.subTest(age=age):self.check_ingress(age,accepted)


if os.getenv('BINANCE_REVIEWED_REPRO')=='1':
    # CI runs the same assertions against the rejected fixed SHA first. Every
    # test must demonstrate its known failure there, then pass on the candidate.
    for case in (AdmissionRaceTests,IngressRaceTests):
        for name in unittest.defaultTestLoader.getTestCaseNames(case):
            setattr(case,name,unittest.expectedFailure(getattr(case,name)))

if __name__=='__main__':unittest.main()
