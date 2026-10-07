import datetime as dt
import json
import pathlib
import tempfile
import unittest
from scripts.hunter_ops_health import read_health, enrich_deployment


class OpsHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / 'health.json'
        self.now = dt.datetime.now(dt.timezone.utc)
        self.health = dict(generated_at=self.now.isoformat(), mode='OBSERVATION_ONLY',
                          real_order_count=0, real_trading_enabled=False,
                          capital_authority='NONE_SHADOW_ONLY', formal_writer=False,
                          source_sha='a'*40, deployment_evidence={
                              'evidence_snapshot_sha': 'a'*40,
                              'base_checkout_sha': 'WRONG',
                              'authoritative_main_readback_sha': 'b'*40})
    def read(self):
        self.path.write_text(json.dumps(self.health))
        return read_health(self.path, self.now)
    def test_fresh_read_and_base_remains_distinct(self):
        h=self.read()
        result=enrich_deployment({'head':{'returncode':0,'stdout':'c'*40},
                                 'cached_origin_main':{'returncode':0,'stdout':'d'*40}}, h)
        self.assertEqual(result['base_checkout_sha'], 'c'*40)
        self.assertEqual(result['authoritative_main_readback_sha'], 'b'*40)
        self.assertFalse(result['cached_origin_is_live_lookup'])
    def test_stale_and_future_fail_closed(self):
        for seconds in (-61, 1):
            self.health['generated_at']=(self.now+dt.timedelta(seconds=seconds)).isoformat()
            with self.assertRaises(ValueError): self.read()
    def test_shadow_boundary_fail_closed(self):
        for key,value in [('real_order_count',1),('real_trading_enabled',True),
                          ('capital_authority','LIVE'),('formal_writer',True),('mode','LIVE')]:
            old=self.health[key]; self.health[key]=value
            with self.assertRaises(ValueError): self.read()
            self.health[key]=old
    def test_sha_mismatch_fail_closed(self):
        self.health['source_sha']='b'*40
        with self.assertRaises(ValueError): self.read()
    def test_symlink_rejected(self):
        self.read(); link=self.path.with_name('link.json');link.symlink_to(self.path)
        with self.assertRaises(OSError): read_health(link,self.now)
    def test_missing_health_never_claims_verified(self):
        row=enrich_deployment({})
        self.assertEqual(row['runtime_evidence_status'],'UNAVAILABLE')
        self.assertEqual(row['last_monitor_source_sha'],'UNKNOWN')
        self.assertEqual(row['authoritative_main_readback_sha'],'UNKNOWN')
    def test_oversized_rejected(self):
        self.path.write_bytes(b' '*1000001)
        with self.assertRaisesRegex(ValueError,'TOO_LARGE'): read_health(self.path,self.now)

class ConnectorPatchTests(unittest.TestCase):
    def test_patch_keeps_auth_and_decorators(self):
        from scripts.hunter_ops_connector_patch import patch
        source='''AUTH_POLICY = "unchanged"
_OPS_UNITS = ('hunter-watchdog.service',)
@mcp.tool()
def deployment_status() -> dict:
    return {"head": {}}
@mcp.tool()
def runtime_file(path: str) -> dict:
    from pathlib import Path
    if path != 'sentinel-runtime.json': raise ValueError('Path not approved')
    return {"path": path}
'''
        updated=patch(source)
        self.assertIn('AUTH_POLICY = "unchanged"',updated)
        self.assertEqual(updated.count('@mcp.tool()'),2)
        self.assertIn("if path != 'sentinel-runtime.json'",updated)
        self.assertIn('hunter-market-stream.service',updated)
        with self.assertRaises(ValueError): patch(updated)
