import datetime as dt
import unittest
from scripts.sentinel_runtime import collect, scan

class SentinelBtcScopeTests(unittest.TestCase):
    def test_collector_never_requests_excluded_asset(self):
        urls=[]
        def get(url):
            urls.append(url)
            return {}
        evidence=collect(get)
        self.assertTrue(urls)
        self.assertFalse(any('AXS' in url for url in urls))
        self.assertFalse(any(key.startswith('axs_') for key in evidence))
        self.assertIn('btc_spot',evidence)
        self.assertIn('btc_oi',evidence)
    def test_legacy_inputs_do_not_resume_excluded_monitor(self):
        now=dt.datetime.now(dt.timezone.utc)
        result=scan({}, {'axs_spot':{'asof':now.isoformat(),'price':2}},now)
        self.assertNotIn('axs_monitor',result)
        self.assertFalse(any('axs' in key for key in result['evidence']))
        self.assertFalse(any('axs' in gap for gap in result['data_gaps']))
        self.assertEqual(result['confirmation_status'],'ANALYSIS_NOT_PORTED')
        self.assertFalse(result['freshness_gate']['new_capital_action_allowed'])
