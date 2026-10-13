"""Strict private-copy opt-in: exact generation, fallback, fence and ownership."""
import copy
import importlib.util
import math
import os
from unittest import TestCase,skipUnless
from unittest.mock import patch

from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_stream_store import content_hash
from tests import test_hunter_binance_coordinated_runtime as runtime

AVAILABLE=importlib.util.find_spec('orjson') is not None


@skipUnless(AVAILABLE, 'optional orjson private codec not installed')
class OwnedOrjsonTests(TestCase):
    def env(self,enabled=True):
        return patch.dict(os.environ,{'BINANCE_EXPERIMENTAL_OWNED_CODEC':'ORJSON' if enabled else 'JSON'})

    @staticmethod
    def payload(extra):
        return dict(status='READY',windows={'btcusdt@kline_5m':{'rows':[[1,'2',3]]}},
                    extra=extra)

    def test_default_behavior_is_original_json_string(self):
        with self.env(False):
            frozen=consumer.Frozen(self.payload({'f':-0.0,'x':[None,True,123]}),checked_at=1)
        self.assertIs(type(frozen._snapshot_json),str)

    def test_private_owned_bytes_exact_hash_values_and_isolation(self):
        payload=self.payload({'numbers':[None,True,False,3,1.125,-0.0],
                              'nested':[{'key':'中文🙂','payload':[1,2]} for _ in range(400)]})
        baseline=copy.deepcopy(payload)
        digest=content_hash(payload)
        with self.env():
            frozen=consumer.Frozen(payload,checked_at=1)
            first=frozen.generation
            second=frozen.generation
        self.assertIs(type(frozen._snapshot_json),bytes)
        self.assertEqual(first,baseline)
        self.assertEqual(content_hash(first),digest)
        self.assertEqual(first,second)
        self.assertIsNot(first,second)
        self.assertIsNot(first['extra'],payload['extra'])
        payload['extra']['nested'][0]['key']='modified input'
        first['extra']['nested'][0]['payload'].append(3)
        self.assertEqual(frozen.generation['extra']['nested'][0]['key'],'中文🙂')
        self.assertEqual(frozen.generation['extra']['nested'][0]['payload'],[1,2])
        self.assertEqual(content_hash(frozen.generation),digest)
        self.assertEqual(math.copysign(1,frozen.generation['extra']['numbers'][-1]),-1.0)

    def test_bigint_and_surrogate_use_original_json_fallback(self):
        for value in (2**130,-(2**130),'\ud800','unpaired\udfff'):
            with self.subTest(value=repr(value)),self.env():
                source=self.payload({'strange':value})
                frozen=consumer.Frozen(source,checked_at=1)
                self.assertIs(type(frozen._snapshot_json),str)
                self.assertEqual(frozen.generation,source)

    def test_alias_cycle_and_non_json_types_stay_deepcopied(self):
        shared=[];cyclic=[];cyclic.append(cyclic)
        class Sub(list):pass
        for value in ({'left':shared,'right':shared},cyclic,(1,2),Sub([1]),float('inf')):
            with self.subTest(t=type(value).__name__),self.env():
                source=self.payload(value)
                frozen=consumer.Frozen(source,checked_at=1)
                self.assertIsNone(frozen._snapshot_json)
                exported=frozen.generation['extra']
                if value is cyclic:
                    self.assertIs(exported[0],exported)
                elif isinstance(value,dict):
                    self.assertIs(exported['left'],exported['right'])
                elif isinstance(value,float):
                    self.assertEqual(exported,value)
                else:
                    self.assertEqual(exported,value)
                    self.assertIs(type(exported),type(value))
                self.assertIsNot(exported,value)

    def test_external_bytes_never_enter_orjson_decode(self):
        value=self.payload({'secret':b'raw external bytes'})
        with self.env():
            frozen=consumer.Frozen(value,checked_at=1)
        self.assertIsNone(frozen._snapshot_json)
        self.assertEqual(frozen.generation,value)
        self.assertIsNot(frozen.generation,value)

    def test_real_control_fence_and_owned_error_generation(self):
        from research.hunter_binance_stream_consumer import CacheReadError
        testcase=runtime.DataCases('runTest')
        testcase.setUp()
        try:
            market=testcase.market()
            stamp=market.seed()
            with self.env():
                frozen=market.read(market.names,stamp+1)
            self.assertIs(type(frozen._snapshot_json),bytes)
            initial=frozen.generation
            frozen.check_delivery()
            market.store.start_draining(market.epoch)
            with self.assertRaises(CacheReadError) as rejected:
                frozen.check_delivery()
            self.assertEqual(rejected.exception.generation,initial)
            audit=rejected.exception.generation
            audit['generation_sha256']='tampers-only-error-copy'
            self.assertEqual(frozen.generation,initial)
            with self.assertRaises(CacheReadError):frozen.check_delivery()
        finally:
            testcase.doCleanups()


if __name__=='__main__':
    import unittest
    unittest.main()
