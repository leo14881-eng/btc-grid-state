import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'research'))
import hunter_bybit_availability as mod


def row(base,quote='USDT',status='Trading',**extra):
    return dict(symbol=base+quote,baseCoin=base,quoteCoin=quote,status=status,**extra)


class FullSpotCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=pathlib.Path(self.tmp.name)/'cache.json'
        self.patcher=patch.object(mod,'SPOT_CACHE',self.path);self.patcher.start()
        self.addCleanup(self.patcher.stop);self.addCleanup(self.tmp.cleanup)
        self.now=dt.datetime(2026,10,5,tzinfo=dt.timezone.utc)
        self.rows=[row('BTC'),row('ETH'),row('FLUID'),row('CARV'),row('OFF',status='Suspended'),row('BTC','USDC'),row('NVDA',symbolType='xstocks')]
        self.response=dict(retCode=0,result=dict(category='spot',list=self.rows))

    def populate(self):
        with patch.object(mod,'request',return_value=self.response) as request:
            xs,meta=mod.spot_proxy_universe(self.now)
            request.assert_called_once_with(mod.SPOT_PROXY+'/bybit/spot')
        return xs,meta

    def test_bulk_and_filter(self):
        xs,meta=self.populate()
        self.assertEqual(xs,['BTC','CARV','ETH','FLUID'])
        self.assertEqual(meta['network_requests'],1)
        self.assertEqual(json.loads(self.path.read_text())['instruments'],self.rows)

    def test_repeated_lookup_no_network(self):
        self.populate()
        with patch.object(mod,'request',side_effect=AssertionError('network')):
            xs,meta=mod.spot_proxy_universe(self.now+dt.timedelta(hours=23,minutes=59))
        self.assertIn('FLUID',xs);self.assertEqual(meta['network_requests'],0)
        self.assertTrue(meta['cache_hit'])
        self.assertEqual(meta['captured_at_utc'],self.now.isoformat())

    def test_expiry_refreshes_at_24_hours(self):
        self.populate()
        with patch.object(mod,'request',return_value=self.response) as request:
            _,meta=mod.spot_proxy_universe(self.now+dt.timedelta(hours=24))
        request.assert_called_once();self.assertFalse(meta['cache_hit'])

    def test_failure_preserves_old_file(self):
        self.populate();before=self.path.read_bytes()
        with patch.object(mod,'request',side_effect=RuntimeError('HTTP_403')):
            with self.assertRaisesRegex(RuntimeError,'HTTP_403'):
                mod.spot_proxy_universe(self.now+dt.timedelta(days=1))
        self.assertEqual(before,self.path.read_bytes())

    def test_partial_error_malformed_responses_do_not_cache(self):
        for response in [dict(retCode=10001),dict(retCode=0,result=dict(category='spot',list=[row('FLUID')])),dict(retCode=0,result=dict(category='linear',list=self.rows)),dict(retCode=0,result=dict(category='spot',list=[])),dict(retCode=0,result=dict(category='spot',list=self.rows,nextPageCursor='more'))]:
            with self.subTest(response=response),patch.object(mod,'request',return_value=response):
                with self.assertRaises(RuntimeError):mod.spot_proxy_universe(self.now)
                self.assertFalse(self.path.exists())

    def test_legacy_cache_not_assumed_full(self):
        self.path.write_text(json.dumps({'BTC':{'trading':True,'as_of_utc':self.now.isoformat()}}))
        _,meta=self.populate();self.assertFalse(meta['cache_hit'])

    def test_future_cache_forces_refresh(self):
        self.populate()
        with patch.object(mod,'request',return_value=self.response) as request:
            mod.spot_proxy_universe(self.now-dt.timedelta(minutes=1))
        request.assert_called_once()

    def test_refresh_removes_delisted_symbol(self):
        self.populate()
        self.response['result']['list']=[row('BTC'),row('ETH')]
        with patch.object(mod,'request',return_value=self.response):
            xs,_=mod.spot_proxy_universe(self.now+dt.timedelta(days=1))
        self.assertNotIn('FLUID',xs)

    def test_worker_bulk_rejection_uses_explicit_candidate_fallback(self):
        review=pathlib.Path(self.tmp.name)/'review.json'
        output=pathlib.Path(self.tmp.name)/'availability.json'
        review.write_text(json.dumps({'capital_review_eligible':['FLUID']}))
        def worker(url,*args,**kwargs):
            if url.endswith('/bybit/spot'):
                raise RuntimeError('HTTP_400 body={"ok":false,"error":"INVALID_SYMBOL"}')
            symbol=url.split('symbol=')[-1]
            return {'retCode':0,'result':{'list':[row(symbol.removesuffix('USDT'))]}}
        with patch.object(mod,'REVIEW',review),patch.object(mod,'OUT',output),patch.object(mod,'request',side_effect=worker) as req,patch.object(mod,'alpha',return_value=(None,'CREDENTIALS_NOT_CONFIGURED')):
            mod.main()
        d=json.loads(output.read_text())['spot']
        self.assertEqual(d['status'],'OK_CANDIDATE_SCOPED_BYBIT_VIA_CLOUDFLARE')
        self.assertFalse(d['complete'])
        self.assertEqual(d['symbols'],['BTC','ETH','FLUID'])
        self.assertIn('HTTP_400',d['bulk_error'])
        self.assertEqual(req.call_count,4)

if __name__=='__main__':unittest.main()
