import concurrent.futures
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import hunter_market_stream as stream
from scripts import hunter_fast_watch_bootstrap as bootstrap

class DeploymentTests(unittest.TestCase):
    def setUp(self):
        stream._READBACK_CACHE.clear()

    def test_concurrent_venues_receive_one_coherent_fetch(self):
        entered=threading.Event();release=threading.Event()
        def fetch(repo):
            entered.set();self.assertTrue(release.wait(2))
            return {'open_positions':[{'asset':'AAA'}]},'a'*40
        with patch.object(stream,'_read_main',side_effect=fetch) as fetcher:
            with concurrent.futures.ThreadPoolExecutor(3) as pool:
                first=pool.submit(stream.read_main,'/cache')
                self.assertTrue(entered.wait(2))
                others=[pool.submit(stream.read_main,'/cache') for _ in range(2)]
                release.set();rows=[first.result()]+[x.result() for x in others]
            self.assertEqual(fetcher.call_count,1)
            rows[0][0]['open_positions'][0]['asset']='CHANGED'
            self.assertEqual(rows[1][0]['open_positions'][0]['asset'],'AAA')
            self.assertEqual(rows[2][1],'a'*40)

    def test_expired_cache_fetches_new_main(self):
        with patch.object(stream,'_read_main',side_effect=[({},'a'*40),({},'b'*40)]) as fetcher:
            with patch.object(stream.time,'monotonic',return_value=0):stream.read_main('/cache')
            with patch.object(stream.time,'monotonic',return_value=10):row=stream.read_main('/cache')
            self.assertEqual(row[1],'b'*40);self.assertEqual(fetcher.call_count,2)

    def test_failed_fetch_not_cached(self):
        with patch.object(stream,'_read_main',side_effect=[RuntimeError('FETCH_FAILED'),({},'a'*40)]) as fetcher:
            with self.assertRaises(RuntimeError):stream.read_main('/cache')
            self.assertEqual(stream.read_main('/cache')[1],'a'*40)
            self.assertEqual(fetcher.call_count,2)

    def test_health_omits_counterfactual_portfolio_without_mutating_state(self):
        source={'mode':'OBSERVATION_ONLY','venues':{'BINANCE_SPOT':{
            'counterfactual':{'private':'state'},'ab':{'sample':None},
            'history':[{'kind':'REST_TAKEOVER'}]*30+[{'kind':'OTHER'}]}}}
        original=copy.deepcopy(source)
        result=stream.public_health(source,'a'*40,{'evidence_snapshot_sha':'b'*40})
        self.assertNotIn('counterfactual',result['venues']['BINANCE_SPOT'])
        self.assertEqual(len(result['venues']['BINANCE_SPOT']['history']),20)
        self.assertEqual(result['loaded_code_source_sha'],'a'*40)
        self.assertEqual(source,original)

    def test_existing_cache_requires_public_read_only_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp)/'cache';(repo/'.git').mkdir(parents=True)
            with patch.object(bootstrap.subprocess,'check_output',return_value='ssh://unexpected'):
                with patch.object(bootstrap.subprocess,'run') as run:
                    with self.assertRaisesRegex(ValueError,'PUBLIC_READBACK_ORIGIN_REQUIRED'):bootstrap.initialize(repo)
                    run.assert_not_called()

    def test_partial_clone_cannot_replace_live_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp)/'cache'
            with patch.object(bootstrap.subprocess,'run',side_effect=RuntimeError('NETWORK_FAILED')):
                with self.assertRaises(RuntimeError):bootstrap.initialize(repo)
            self.assertFalse(repo.exists())
            self.assertEqual(list(Path(temp).iterdir()),[])

    def test_non_git_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp)/'cache';repo.mkdir();(repo/'ledger').write_text('retain')
            with self.assertRaisesRegex(ValueError,'READBACK_DIRECTORY_NOT_A_GIT_CACHE'):bootstrap.initialize(repo)
            self.assertEqual((repo/'ledger').read_text(),'retain')
