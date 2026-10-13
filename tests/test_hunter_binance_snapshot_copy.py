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

    def test_plain_owned_json_matches_circular_checked_original_bytes(self):
        import json
        payload=snapshot({'windows':[[{'x':[1,True,None,'汉字','🙂']},-0.0]
                                     for _ in range(24)],'nested':{'safe':True}})
        original=json.dumps(payload,ensure_ascii=True,allow_nan=False,separators=(',',':'))
        fast=consumer._encode_json_tree(payload)
        self.assertEqual(fast,original)
        with patch.object(consumer.json,'dumps',wraps=consumer.json.dumps) as encoded:
            frozen=Frozen(payload,checked_at=1)
        self.assertTrue(encoded.call_args.kwargs.get('check_circular') is False)
        self.assertEqual(frozen.generation,json.loads(original))
        self.assertEqual(frozen._snapshot_json,original)
        self.assertIsNot(frozen.generation,frozen.generation)
        self.assertIsNot(frozen.generation['windows'],payload['windows'])

    def test_shared_alias_and_cycle_never_enter_unchecked_json(self):
        repeated=[];cycle=[];cycle.append(cycle)
        for value in (snapshot({'left':repeated,'right':repeated}),snapshot(cycle)):
            with self.subTest(case='alias' if value['extra'] is not cycle else 'cycle'):
                with patch.object(consumer.json,'dumps',wraps=consumer.json.dumps) as encoded:
                    result=consumer._encode_json_tree(value)
                self.assertIsNone(result)
                encoded.assert_not_called()

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

    def test_rejected_non_json_snapshot_keeps_aliases_and_owned_error_copy(self):
        from research.hunter_binance_stream_store import content_hash
        class RejectingReader:
            def finish_read(self,*args):raise RuntimeError('fixture revoked')
        shared=[];original=snapshot({'one':shared,'two':shared,'tuple':(1,2)})
        original['generation_end_ms']=1
        original['generation_sha256']=content_hash(original)
        frozen=Frozen(original,checked_at=1,reader=RejectingReader(),fence={},clock=lambda:1)
        self.assertIsNone(frozen._snapshot_json)
        with self.assertRaises(consumer.CacheReadError) as caught:frozen.check_delivery()
        error=caught.exception
        self.assertEqual(str(error),'fixture revoked')
        self.assertIsInstance(error.__cause__,RuntimeError)
        self.assertEqual(error.generation,original)
        self.assertIs(error.generation['extra']['one'],error.generation['extra']['two'])
        self.assertIs(type(error.generation['extra']['tuple']),tuple)
        error.generation['extra']['one'].append('changed error')
        self.assertEqual(shared,[])
        self.assertEqual(frozen.generation['extra']['one'],[])

    def test_direct_error_constructor_keeps_owned_deepcopy_graph(self):
        shared=[];shared.append(shared)
        original=snapshot({'one':shared,'two':shared,'tuple':(1,2)})
        error=consumer.CacheReadError('fixture rejection',original)
        copied=error.generation['extra']
        self.assertEqual(str(error),'fixture rejection')
        self.assertIs(copied['one'],copied['two'])
        self.assertIs(copied['one'][0],copied['one'])
        self.assertIsNot(copied['one'],shared)
        self.assertIs(type(copied['tuple']),tuple)
        copied['one'].append('changed error')
        self.assertEqual(len(shared),1)

    def test_error_factory_keeps_owned_cycle_fallback(self):
        cyclic=[];cyclic.append(cyclic)
        frozen=Frozen(snapshot(cyclic),checked_at=1)
        self.assertIsNone(frozen._snapshot_json)
        error=consumer.CacheReadError._from_frozen('fixture rejection',frozen)
        copied=error.generation['extra']
        self.assertIs(copied[0],copied)
        self.assertIsNot(copied,cyclic)
        copied.append('changed error')
        second=consumer.CacheReadError._from_frozen('fixture rejection',frozen)
        self.assertEqual(len(second.generation['extra']),1)
        self.assertIs(second.generation['extra'][0],second.generation['extra'])
        self.assertIsNot(second.generation['extra'],copied)


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

    def test_real_delivery_rejection_audit_copy_is_complete_and_independently_owned(self):
        market=self.market();stamp=market.seed();frozen=market.read(market.names,stamp+1)
        original=frozen.generation
        self.assertIsNotNone(frozen._snapshot_json)
        market.store.start_draining(market.epoch)
        with patch.object(consumer.copy,'deepcopy',wraps=consumer.copy.deepcopy) as copied:
            with self.assertRaises(consumer.CacheReadError) as caught:frozen.check_delivery()
        error=caught.exception
        self.assertEqual(str(error),str(error.__cause__))
        self.assertIsInstance(error.__cause__,ValueError)
        self.assertEqual(error.generation,original)
        self.assertTrue(all(call.args[0] is not frozen._generation for call in copied.call_args_list))
        name=market.names[0]
        error.generation['windows'][name]['rows'][0][1]='tampered error audit'
        error.generation['generation_sha256']='tampered error identity'
        self.assertEqual(frozen.generation,original)
        with self.assertRaises(consumer.CacheReadError) as second:frozen.check_delivery()
        self.assertEqual(second.exception.generation,original)
        self.assertIsNot(second.exception.generation,error.generation)


if __name__=='__main__':unittest.main()
