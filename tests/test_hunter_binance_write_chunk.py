"""Actual durable per-transaction WS cap and fail-closed partial failure."""
import json
import unittest
from unittest.mock import patch

from research import hunter_binance_health as health
from research.hunter_binance_stream_store import topic
from tests import test_hunter_binance_coordinated_runtime as fx
from tests.test_hunter_binance_cache import message


class WriterChunkTests(unittest.TestCase):
    setUp=fx.DataCases.setUp
    market=fx.DataCases.market

    def setup_frames(self, suffix):
        m=self.market(suffix)
        stamp=m.seed()
        name=topic('BTCUSDT','15m')
        shard=m.protocol.topic_shards[name]
        frames=[]
        for offset in range(1,71):
            now=stamp+offset
            body=message('BTCUSDT','15m',now)
            m.protocol.arrived(shard,json.dumps(body),now)
            frames.append((shard,body,now))
        return m,name,frames

    def test_70_receipts_split_without_changing_durable_outcomes(self):
        market,name,frames=self.setup_frames('chunk-success')
        original=market.store.ingest_accounted
        lengths=[]
        def record(epoch,entries):
            lengths.append(len(entries))
            return original(epoch,entries)
        before=market.store.counts(market.epoch)[name]
        with patch.object(market.store,'ingest_accounted',side_effect=record):
            market.protocol.receive_batch(frames)
        after=market.store.counts(market.epoch)[name]
        self.assertEqual(lengths,[24,24,22])
        self.assertEqual(after['committed']-before['committed'],70)
        self.assertEqual(market.protocol.inflight,[])
        self.assertEqual(sum(after[k]-before[k] for k in
            ('committed','duplicate','invalid','unprocessed_recorded')),70)

    def test_uncommitted_suffix_is_recorded_after_writer_failure(self):
        market,name,frames=self.setup_frames('chunk-failure')
        original=market.store.ingest_accounted
        calls=[0]
        def fail_second(epoch,entries):
            calls[0]+=1
            if calls[0]==2:raise RuntimeError('INJECTED_WRITER_ABORT')
            return original(epoch,entries)
        before=market.store.counts(market.epoch)[name]
        with patch.object(market.store,'ingest_accounted',side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError,'INJECTED_WRITER_ABORT'):
                market.protocol.receive_batch(frames)
        self.assertEqual(len(market.protocol.inflight),46)
        market.protocol.discard_inflight('INJECTED_WRITER_ABORT')
        after=market.store.counts(market.epoch)[name]
        self.assertEqual(after['committed']-before['committed'],24)
        self.assertEqual(after['unprocessed_recorded']-before['unprocessed_recorded'],46)
        self.assertEqual(market.protocol.inflight,[])
        self.assertEqual(sum(after[k]-before[k] for k in
            ('committed','duplicate','invalid','unprocessed_recorded')),70)


if __name__=='__main__':
    unittest.main()
