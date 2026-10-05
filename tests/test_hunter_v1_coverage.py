import copy
import unittest
from research.hunter_v1_coverage import partition

class V1CoverageTests(unittest.TestCase):
    def data(self):
        signals=[{"base":"BTC","stage":"EARLY"},{"base":"GAIB","stage":"EARLY","source_venue":"bybit","execution_supported":False}]
        early={"scan_generation_id":"GEN","early_count":2,"early":signals,"all_signals":copy.deepcopy(signals)}
        universe={"generation_id":"GEN","venue_status":{"binance":{"excluded_bstocks":["MSFTB"]}},"coins":{"BTC":{"venues":["binance","bybit"]},"GAIB":{"venues":["bybit"]}}}
        return early,universe

    def test_supported_coverage_stays_required_unsupported_samples_are_recorded(self):
        e,u=self.data()
        self.assertEqual(partition(e,u),({"BTC"},{"GAIB"}))

    def test_research_missing_from_record_fails(self):
        e,u=self.data();e["all_signals"]=e["all_signals"][:1]
        with self.assertRaisesRegex(AssertionError,"RESEARCH_SAMPLE_MISSING"):partition(e,u)

    def test_binance_signal_cannot_be_silently_exempted(self):
        e,u=self.data();u["coins"]["GAIB"]["venues"]=["binance","bybit"]
        with self.assertRaisesRegex(AssertionError,"SCOPE_INVALID"):partition(e,u)

    def test_mixed_generation_fails(self):
        e,u=self.data();u["generation_id"]="OLD"
        with self.assertRaisesRegex(AssertionError,"GENERATION_MISMATCH"):partition(e,u)
