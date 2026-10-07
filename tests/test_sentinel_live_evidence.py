import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from scripts import sentinel_live_evidence as live
from scripts.sentinel_runtime import public_evidence, scan
from scripts.hunter_ops_connector_patch import add_live_sentinel_reader


class LiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        live._cache = None
        self.now = dt.datetime.now(dt.timezone.utc)
        self.evidence = {'btc_spot': {'asof': self.now.isoformat(), 'price': 100}}
        self.module = types.SimpleNamespace(collect=Mock(return_value=self.evidence), scan=scan, public_evidence=public_evidence)
        self.loader = patch.object(live, 'load_collector', return_value=(self.module, 'a'*40))
        self.loader.start(); self.addCleanup(self.loader.stop)

    def test_fresh_read_has_no_formal_success_or_writer(self):
        original = copy.deepcopy(self.evidence)
        with patch('subprocess.run', side_effect=AssertionError('no shell/git')), patch.object(Path, 'write_text', side_effect=AssertionError('no file writes')):
            r = live.read_live_evidence()
        self.assertFalse(r['formal_writer']); self.assertFalse(r['main_readback_verified'])
        self.assertEqual(r['real_order_count'], 0)
        self.assertFalse(r['real_trading_enabled'])
        self.assertEqual(r['capital_authority'], 'NONE_SHADOW_ONLY')
        self.assertEqual(r['confirmation_status'], 'ANALYSIS_NOT_PORTED')
        self.assertNotIn('last_successful_scan_at', r)
        self.assertEqual(r['evidence'], original)

    def test_cache_preserves_times_and_returns_copy(self):
        first = live.read_live_evidence(); first['evidence']['btc_spot']['price'] = 999
        second = live.read_live_evidence()
        self.assertEqual(self.module.collect.call_count, 1)
        self.assertEqual(first['generated_at'], second['generated_at'])
        self.assertEqual(second['evidence']['btc_spot']['price'], 100)

    def test_expired_cache_requires_new_collection(self):
        live.read_live_evidence()
        live._cache = (live._cache[0]-31, live._cache[1], live._cache[2])
        live.read_live_evidence(); self.assertEqual(self.module.collect.call_count, 2)

    def test_clock_future_cache_not_reused(self):
        live.read_live_evidence()
        live._cache[2]['generated_at'] = (self.now+dt.timedelta(hours=1)).isoformat()
        live.read_live_evidence(); self.assertEqual(self.module.collect.call_count, 2)

    def test_code_change_invalidates_cache(self):
        live.read_live_evidence()
        with patch.object(live, 'load_collector', return_value=(self.module, 'b'*40)):
            self.assertEqual(live.read_live_evidence()['source_sha'], 'b'*40)
        self.assertEqual(self.module.collect.call_count, 2)

    def test_failed_collection_does_not_relabel_stale_cache(self):
        live.read_live_evidence()
        live._cache = (live._cache[0]-31, live._cache[1], live._cache[2])
        self.module.collect.side_effect = TimeoutError('actual timeout')
        with self.assertRaises(TimeoutError): live.read_live_evidence()

    def test_concurrent_reads_single_collection(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            rows=list(executor.map(lambda _: live.read_live_evidence(), range(4)))
        self.assertEqual(self.module.collect.call_count, 1)
        self.assertTrue(all(r['run_id'] == rows[0]['run_id'] for r in rows))

    def test_loader_rejects_code_hash_mismatch(self):
        self.loader.stop()
        with tempfile.TemporaryDirectory() as directory, patch.object(live, 'CODE_ROOT', Path(directory)):
            root=Path(directory);(root/'sentinel_runtime.py').write_text('raise AssertionError("must not execute")')
            (root/'manifest.json').write_text(json.dumps({'source_sha':'a'*40,'collector_sha256':'0'*64}))
            with self.assertRaisesRegex(ValueError, 'CODE_HASH_MISMATCH'): live.load_collector()

    def test_missing_sources_stay_unknown(self):
        self.module.collect.return_value = {'btc_spot': {'error':'HTTP_451'}}
        r=live.read_live_evidence()
        self.assertEqual(r['run_status'], 'DATA_STALE')
        self.assertFalse(r['freshness_gate']['valid_evidence_scan'])
        self.assertFalse(r['freshness_gate']['new_capital_action_allowed'])

    def test_fixed_alias_preserves_auth_allowlist_and_hourly_reader(self):
        source='''AUTH = 'original'
@mcp.tool()
def runtime_file(path):
    from pathlib import Path
    if path == 'sentinel-evidence-current.json': return 'hourly'
    allowed=('sentinel-runtime.json',)
    if path not in allowed: raise ValueError('Path not approved')
    return {'path':path}
'''
        updated=add_live_sentinel_reader(source)
        self.assertIn("AUTH = 'original'",updated)
        self.assertIn("allowed=('sentinel-runtime.json',)",updated)
        self.assertIn("return 'hourly'",updated)
        self.assertIn('@mcp.tool()',updated)
        self.assertIn("path == 'sentinel-evidence-live.json'",updated)
        with self.assertRaises(ValueError):add_live_sentinel_reader(updated)
