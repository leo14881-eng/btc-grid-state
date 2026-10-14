"""Isolated writer batch-size adaptation; never uses a production database."""
from threading import Lock
import time
import unittest
from research.hunter_binance_health import Protocol
from research.hunter_binance_stream_store import topic


class FakeStore:
    def __init__(self, sleep=0):
        self.groups=[]
        self.sleep=sleep
    def ingest_accounted(self, epoch, batch):
        self.groups.append(list(batch))
        if self.sleep:time.sleep(self.sleep)
        return ["committed"] * len(batch)


def frame(t):
    return (0, dict(e='kline',s='BTCUSDT',E=t,
             k=dict(s='BTCUSDT',i='15m',t=0)), t)


class AdaptiveWriterTest(unittest.TestCase):
    def protocol(self, delay):
        p=Protocol.__new__(Protocol)
        p.store=FakeStore(delay)
        p.epoch='f'*32
        p.topic_shards={topic('BTCUSDT','15m'):0}
        p.inflight=[]
        p._market_batch_limit=100
        p.lock=Lock()
        p.shards={0:dict(observed_at=0,evidence=None)}
        p.processed=0
        p.controls=0
        return p

    def test_slow_writer_shrinks_next_transaction_without_drop(self):
        p=self.protocol(.052)
        p.receive_batch([frame(i) for i in range(180)])
        self.assertEqual(p.store.groups[0].__len__(),100)
        self.assertEqual(sum(map(len,p.store.groups)),180)
        self.assertTrue(all(1<=len(group)<=100 for group in p.store.groups))
        self.assertEqual(p.processed,180)
        self.assertEqual(p.inflight,[])
        self.assertLessEqual(p._market_batch_limit,50)

    def test_fast_writer_uses_original_upper_bound(self):
        p=self.protocol(0)
        p.receive_batch([frame(i) for i in range(217)])
        self.assertEqual([len(x) for x in p.store.groups],[100,100,17])
        self.assertEqual(p.processed,217)
        self.assertEqual(p._market_batch_limit,100)

    def test_bad_shard_refused_before_commit(self):
        p=self.protocol(0)
        bad=(1,frame(1)[1],1)
        with self.assertRaisesRegex(ValueError,'BINANCE_MESSAGE_SHARD_MISMATCH'):
            p.receive_batch([frame(0),bad])
        self.assertEqual(p.store.groups,[])
        self.assertEqual(len(p.inflight),2)


if __name__=='__main__':unittest.main()
