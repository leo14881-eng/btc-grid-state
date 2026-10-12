"""Evidence follows actual commit/rollback and remains bounded independently of readiness."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
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
        self.assertEqual(len(values),82)
        self.assertTrue(all(len(x['topics'])<=50 for x in values[2:]))
        self.assertEqual(sum(len(x['topics']) for x in values[2:]),4000)
        self.assertLess(max(len(line) for line in out.getvalue().splitlines()),16000)

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


if __name__=='__main__':unittest.main()
