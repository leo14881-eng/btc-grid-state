"""Evidence follows actual commit/rollback and remains bounded independently of readiness."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from research import hunter_binance_stream as stream
from research.hunter_binance_stream_store import Store,topic
from test_hunter_binance_cache import message,NOW


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]);self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)/'cache.sqlite3')
        self.names=[topic('BTCUSDT','15m'),topic('ETHUSDT','15m')]
        self.protocol=stream.Protocol(self.store,self.names,'a'*64,NOW)
        self.store.ack(self.protocol.epoch,self.names)

    def test_ingress_and_queue_are_not_committed_or_ready(self):
        msg=message();self.protocol.note_market_arrival(0,msg,NOW+2)
        q=stream.ObservedQueue(4);q.put((0,msg,NOW+2));q.get_nowait()
        self.assertEqual(q.high_water,1)
        row=self.protocol.evidence.topics[self.names[0]]
        self.assertEqual((row['received'],row['processed']),(1,0))
        self.assertEqual(self.store.states()[0]['ws_rx'],0)
        self.assertEqual(self.store.freeze(self.names,NOW+2,NOW+2)['status'],'UNKNOWN')

    def test_invalid_batch_rolls_back_and_never_counts_as_committed(self):
        good=message();bad=message('ETHUSDT');bad['e']='invalid'
        for msg in (good,bad):self.protocol.note_market_arrival(0,msg,NOW+2)
        with self.assertRaisesRegex(ValueError,'ENVELOPE_INVALID'):
            self.protocol.receive_batch([(0,good,NOW+2),(0,bad,NOW+2)])
        self.assertEqual(self.protocol.evidence.batches,0)
        self.assertEqual(sum(x['processed'] for x in self.protocol.evidence.topics.values()),0)
        self.assertTrue(all(x['ws_rx']==0 for x in self.store.states()))

    def test_duplicate_processing_is_not_a_new_bar_or_freshened_receipt(self):
        msg=message()
        with patch.object(stream,'millis',return_value=NOW+100):
            for received in (NOW+2,NOW+3):
                self.protocol.note_market_arrival(0,msg,received)
                self.protocol.receive_batch([(0,msg,received)])
        row=self.protocol.evidence.topics[self.names[0]]
        self.assertEqual((row['received'],row['processed'],row['changed']),(2,2,1))
        self.assertEqual(self.store.states()[0]['ws_rx'],NOW+2)
        self.assertEqual(self.protocol.evidence.receipt_to_processing_max_ms,98)

    def test_final_pages_have_fixed_bounds_and_periodic_summary_has_no_topic_dump(self):
        evidence=stream.SessionEvidence('a'*32,[topic('X'+str(i)+'USDT','1h') for i in range(4000)])
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            evidence.snapshot(stream.ObservedQueue(4096),4)
            evidence.snapshot(stream.ObservedQueue(4096),0,final=True)
        values=[json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(values),252)
        self.assertTrue(all(len(x['topics'])<=16 for x in values[2:]))
        self.assertEqual(sum(len(x['topics']) for x in values[2:]),4000)
        self.assertLess(max(len(line.encode()) for line in out.getvalue().splitlines()),4096)

    def test_topic_pages_fit_journal_json_with_maximum_symbols_and_counters(self):
        for character in ('X','币','𐐀'):
            evidence=stream.SessionEvidence('a'*32,[topic(character*26+str(i).zfill(4)+'USDT','15m') for i in range(33)])
            for value in evidence.topics.values():
                for key in value:value[key]=2**63-1
            out=io.StringIO()
            with contextlib.redirect_stdout(out):evidence.snapshot(stream.ObservedQueue(4096),0,final=True)
            self.assertTrue(all(len(line.encode())<4096 for line in out.getvalue().splitlines()))
            pages=[json.loads(line) for line in out.getvalue().splitlines()[1:]]
            self.assertEqual(sum(len(x['topics']) for x in pages),33)

    def test_ack_evidence_is_emitted_only_after_store_commit(self):
        request=self.protocol.requests[0][0];self.protocol.mark_sent(0,request['id'],NOW)
        with patch.object(self.store,'ack',side_effect=RuntimeError('storage')),patch.object(stream,'emit') as emit:
            with self.assertRaises(RuntimeError):self.protocol.receive(0,dict(id=request['id'],result=None),NOW+1)
        emit.assert_not_called();self.assertEqual(self.protocol.evidence.acked_batches,0)

    def test_http_summary_does_not_invent_completion_or_include_unknown_fields(self):
        observe=self.protocol.evidence.request_observer(self.names[0]);out=io.StringIO()
        with contextlib.redirect_stdout(out):
            observe('ATTEMPT',attempt=1);observe('GO',token='secret-must-not-appear')
            observe('ERROR',error_type='UncertainRequestError');observe('END')
            observe('ATTEMPT',attempt=2);observe('GO');observe('RESPONSE',status=200)
            observe('REAPED');observe('RETIRED');observe('END')
        first,last=map(json.loads,out.getvalue().splitlines())
        self.assertFalse(first['response_complete']);self.assertFalse(first['owner_retired'])
        self.assertTrue(last['response_complete'] and last['process_reaped'] and last['owner_retired'])
        self.assertNotIn('error_type',last);self.assertNotIn('secret',out.getvalue())

    def test_concurrent_audit_rows_remain_individual_json_lines(self):
        class YieldingOutput(io.StringIO):
            def write(self,value):
                result=super().write(value);time.sleep(.0001);return result
        out=YieldingOutput()
        with contextlib.redirect_stdout(out):
            threads=[Thread(target=lambda:[stream.emit(dict(event='FIXTURE',n=i)) for i in range(30)]) for _ in range(3)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(3);self.assertFalse(thread.is_alive())
        rows=[json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(rows),90)

    def test_failed_output_does_not_override_an_existing_stop(self):
        previous=stream.AUDIT_FAILURES
        with patch('builtins.print',side_effect=OSError('sink')),self.assertRaises(stream.UncertainRequestError):
            try:raise stream.UncertainRequestError('UNKNOWN')
            finally:stream.emit(dict(event='FINAL'))
        self.assertEqual(stream.AUDIT_FAILURES,previous+1)

    def test_ready_window_uses_runtime_nonready_resets_between_audits(self):
        tick=[0];events=[]
        def session(*args,**kwargs):
            for stamp in range(0,121,30):tick[0]=stamp;kwargs['observer'](NOW)
            tick[0]=150;kwargs['on_nonready']('FIXTURE_GAP')
            for stamp in range(180,481,30):tick[0]=stamp;kwargs['observer'](NOW)
        with patch.object(stream,'session',side_effect=session),patch.object(stream,'Budget') as budget,\
             patch.object(self.store,'freeze',return_value=dict(status='READY',epoch='b'*32)),\
             patch.object(stream,'emit',side_effect=events.append):
            budget.return_value.status.return_value=dict(halt=None,cooldown_ms=0)
            stream.collect(self.store,self.names,'a'*64,'c'*40,clock=lambda:NOW,steady=lambda:tick[0])
        audits=[x for x in events if x['event']=='BINANCE_CACHE_AUDIT']
        self.assertEqual([x['ready_window_seconds'] for x in audits[:6]],[0,30,60,90,120,0])
        self.assertEqual(audits[-1]['ready_window_seconds'],300)
        self.assertTrue(all(x['observed_at_ms']==NOW for x in audits))


if __name__=='__main__':unittest.main()
