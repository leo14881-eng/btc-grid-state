"""Snapshot copy isolation and compatibility; all validity fences stay active."""
import math
import unittest
from unittest.mock import patch

from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_stream_consumer import Frozen
from tests import test_hunter_binance_coordinated_runtime as runtime


def snapshot(value):
    return dict(status='READY',windows={'btcusdt@kline_5m':{'rows':[[1,'2',3]]}},extra=value)


class SnapshotCopyTests(unittest.TestCase):
    def test_plain_json_input_and_each_export_are_independently_owned(self):
        original=snapshot({'nested':[{'value':'before'}],'numbers':[None,True,3,1.5]})
        frozen=Frozen(original,checked_at=1)
        original['windows']['btcusdt@kline_5m']['rows'][0][1]='changed input'
        original['extra']['nested'][0]['value']='changed input'
        first=frozen.generation
        self.assertEqual(first['extra']['nested'][0]['value'],'before')
        self.assertEqual(first['windows']['btcusdt@kline_5m']['rows'][0][1],'2')
        first['extra']['nested'][0]['value']='changed export'
        first['windows']['btcusdt@kline_5m']['rows'].clear()
        self.assertEqual(frozen.generation['extra']['nested'][0]['value'],'before')
        self.assertEqual(len(frozen.generation['windows']['btcusdt@kline_5m']['rows']),1)
        self.assertIsNotNone(frozen._snapshot_json)

    def test_aliases_preserve_deepcopy_memo_without_exposing_original(self):
        shared=[];original=snapshot({'one':shared,'two':shared})
        frozen=Frozen(original,checked_at=1);export=frozen.generation
        self.assertIsNone(frozen._snapshot_json)
        self.assertIs(export['extra']['one'],export['extra']['two'])
        self.assertIsNot(export['extra']['one'],shared)
        export['extra']['one'].append(3)
        self.assertEqual(frozen.generation['extra']['two'],[])

    def test_cycles_still_use_existing_memo_copy(self):
        cyclic=[];cyclic.append(cyclic)
        frozen=Frozen(snapshot(cyclic),checked_at=1);export=frozen.generation['extra']
        self.assertIsNone(frozen._snapshot_json)
        self.assertIs(export[0],export);self.assertIsNot(export,cyclic)

    def test_tuple_and_nonstring_keys_keep_their_types(self):
        for value in ((1,2),{1:'integer',False:'boolean',('tuple',):'tuple key'}):
            with self.subTest(value=value):
                frozen=Frozen(snapshot(value),checked_at=1)
                self.assertIsNone(frozen._snapshot_json)
                self.assertEqual(frozen.generation['extra'],value)
                self.assertIs(type(frozen.generation['extra']),type(value))

    def test_nonfinite_numbers_are_not_newly_rejected_or_coerced_by_copy(self):
        for value in (float('nan'),float('inf'),-float('inf')):
            frozen=Frozen(snapshot(value),checked_at=1)
            self.assertIsNone(frozen._snapshot_json)
            actual=frozen.generation['extra']
            self.assertTrue(math.isnan(actual) if math.isnan(value) else actual==value)

    def test_subclasses_and_custom_deepcopy_keep_existing_behavior(self):
        class Values(list):pass
        class Custom:
            def __deepcopy__(self,memo):return 'custom deepcopy'
        frozen=Frozen(snapshot(Values([1])),checked_at=1)
        self.assertIsNone(frozen._snapshot_json)
        self.assertIs(type(frozen.generation['extra']),Values)
        custom=Frozen(snapshot(Custom()),checked_at=1)
        self.assertIsNone(custom._snapshot_json)
        self.assertEqual(custom.generation['extra'],'custom deepcopy')

    def test_json_encoder_limit_keeps_previous_deepcopy_acceptance(self):
        original=snapshot({'values':[1,2,3]})
        for error in (ValueError('integer encoder limit'),OverflowError('encoder overflow')):
            with self.subTest(error=type(error).__name__):
                with patch.object(consumer.json,'dumps',side_effect=error):
                    frozen=Frozen(original,checked_at=1)
                self.assertIsNone(frozen._snapshot_json)
                self.assertEqual(frozen.generation,original)
                self.assertIsNot(frozen.generation['extra'],original['extra'])


class ActualSnapshotCopyTests(unittest.TestCase):
    setUp=runtime.DataCases.setUp
    market=runtime.DataCases.market

    def test_real_snapshot_export_mutation_does_not_bypass_later_fence(self):
        market=self.market();stamp=market.seed();frozen=market.read(market.names,stamp+1)
        self.assertIsNotNone(frozen._snapshot_json)
        exported=frozen.generation
        name=market.names[0];exported['windows'][name]['rows'][0][1]='tampered'
        self.assertNotEqual(frozen.generation['windows'][name]['rows'][0][1],'tampered')
        frozen.check_delivery()
        market.store.start_draining(market.epoch)
        with self.assertRaises(ValueError):frozen.check_delivery()


if __name__=='__main__':unittest.main()
