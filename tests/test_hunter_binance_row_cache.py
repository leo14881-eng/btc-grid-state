"""Pure historical bar parse cache preserves strict per-row validation."""
import json
import unittest

from research import hunter_binance_stream_store as store
from tests.test_hunter_binance_cache import rows


class ParsedRowCacheTests(unittest.TestCase):
    def setUp(self):
        store._validated_json_row.cache_clear()

    def test_exact_source_bytes_are_the_cache_key(self):
        value=rows('15m',1791858120000,1)[0]
        raw=json.dumps(value)
        first=store._validated_json_row(raw,'15m')
        self.assertEqual(first,tuple(store.valid_row(json.loads(raw),'15m')))
        self.assertEqual(store._validated_json_row(raw,'15m'),first)
        self.assertEqual(store._validated_json_row.cache_info().hits,1)
        modified=list(value);modified[4]='102'
        second=store._validated_json_row(json.dumps(modified),'15m')
        self.assertNotEqual(first,second)
        self.assertEqual(store._validated_json_row.cache_info().misses,2)

    def test_no_mutable_alias_and_malformed_rejected_every_time(self):
        value=rows('15m',1791858120000,1)[0]
        original=json.dumps(value)
        copied=list(store._validated_json_row(original,'15m'))
        copied[4]='999'
        self.assertNotEqual(list(store._validated_json_row(original,'15m'))[4],'999')
        broken=list(value);broken[2]='0'
        source=json.dumps(broken)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'BINANCE_BAR_VALUES_INVALID'):
                store._validated_json_row(source,'15m')

    def test_interval_and_off_grid_must_still_validate(self):
        valid=rows('15m',1791858120000,1)[0]
        raw=json.dumps(valid)
        with self.assertRaisesRegex(ValueError,'BINANCE_BAR_TIME_OR_TRADES_INVALID'):
            store._validated_json_row(raw,'1h')
        modified=list(valid);modified[0]+=1
        with self.assertRaisesRegex(ValueError,'BINANCE_BAR_TIME_OR_TRADES_INVALID'):
            store._validated_json_row(json.dumps(modified),'15m')


if __name__=='__main__':
    unittest.main()
