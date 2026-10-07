import copy
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from scripts.sentinel_runtime import scan, write_public_evidence, collect
from scripts.hunter_ops_health import read_sentinel_evidence
from scripts.hunter_ops_connector_patch import add_sentinel_reader

class EvidenceChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=pathlib.Path(self.tmp.name)/'preview.json'
        self.now=dt.datetime.now(dt.timezone.utc)
        self.mutation=scan({}, {'btc_spot':{'asof':self.now.isoformat(),'price':100}},self.now)
    def write(self):
        return write_public_evidence(self.path,self.mutation,'a'*40,'b'*40)
    def test_public_artifact_roundtrip_keeps_analysis_incomplete(self):
        original=copy.deepcopy(self.mutation);row=self.write();read=read_sentinel_evidence(self.path,self.now)
        self.assertEqual(read,row);self.assertEqual(self.mutation,original)
        self.assertEqual(read['confirmation_status'],'ANALYSIS_NOT_PORTED')
        self.assertFalse(read['freshness_gate']['new_capital_action_allowed'])
        self.assertFalse(read['formal_writer']);self.assertEqual(self.path.stat().st_mode&0o777,0o644)
    def test_public_artifact_omits_portfolio_and_unrelated_evidence(self):
        self.mutation['evidence']['portfolio']={'secret':'not public'}
        self.mutation['portfolio_state']={'balance':'private'}
        row=self.write()
        self.assertNotIn('portfolio',row['evidence']);self.assertNotIn('portfolio_state',row)
    def test_nonpreview_writer_refused(self):
        self.mutation['confirmation_status']='COMPLETE'
        with self.assertRaises(ValueError):self.write()
        self.assertFalse(self.path.exists())
    def test_stale_artifact_and_symlink_fail_closed(self):
        self.write()
        with self.assertRaises(ValueError):read_sentinel_evidence(self.path,self.now+dt.timedelta(seconds=601))
        link=self.path.with_name('link');link.symlink_to(self.path)
        with self.assertRaises(OSError):read_sentinel_evidence(link,self.now)
    def test_invalid_boundary_and_sha_refused(self):
        row=self.write()
        for field,value in [('formal_writer',True),('source_sha','UNKNOWN')]:
            changed=copy.deepcopy(row);changed[field]=value;self.path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):read_sentinel_evidence(self.path,self.now)
    def test_connector_keeps_existing_allowlist_and_auth(self):
        source='''AUTH = 'unchanged'
@mcp.tool()
def runtime_file(path):
    from pathlib import Path
    allowed=('sentinel-runtime.json',)
    if path not in allowed: raise ValueError('Path not approved')
    return {'path':path}
def untouched():
    return 'safe'
'''
        result=add_sentinel_reader(source)
        self.assertIn("AUTH = 'unchanged'",result)
        self.assertIn("allowed=('sentinel-runtime.json',)",result)
        self.assertIn("def untouched():",result)
        self.assertIn('@mcp.tool()',result)
        with self.assertRaises(ValueError):add_sentinel_reader(result)

    def test_fifteen_minute_structure_is_collected_without_strategy_change(self):
        urls=[]
        def get(url):
            urls.append(url);return {}
        evidence=collect(get)
        self.assertIn('btc_structure_15m',evidence)
        self.assertTrue(any('interval=15m' in url for url in urls))
        self.assertFalse(any('AXS' in url for url in urls))
