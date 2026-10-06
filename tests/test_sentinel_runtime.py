import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.sentinel_runtime import scan, stamp, merge_owned, watchdog, persist, fresh, source, UTC

class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 6, 8, tzinfo=UTC)
        self.old = {'last_successful_scan_at': stamp(self.now - dt.timedelta(hours=10)), 'portfolio_ref': 'keep', 'leading_warning': {'primary_state': 'EARLY_DISTRIBUTION_RISK'}}
    def test_collection_is_not_analysis_success(self):
        evidence = {key: {'asof': stamp(self.now)} for key in ('btc_spot','btc_structure','btc_oi','btc_funding')}
        result = scan(self.old, evidence, self.now)
        self.assertEqual(result['run_status'], 'ANALYSIS_FAILED')
        self.assertNotIn('last_successful_scan_at', result)
        self.assertNotIn('primary_state', result)
        self.assertNotIn('leading_warning', result)
        self.assertFalse(result['freshness_gate']['new_capital_action_allowed'])
        self.assertEqual(merge_owned(self.old, result)['leading_warning'], self.old['leading_warning'])
    def test_market_transport_failure_is_explicit(self):
        def failed(url): raise OSError('offline')
        result=source('https://public.example.invalid',lambda d:d,get=failed)
        self.assertIsNone(result['asof'])
        self.assertIn('OSError: offline',result['error'])
    def test_secondary_failure_does_not_turn_unknown_to_pass(self):
        result=scan(self.old,{'btc_spot':{'asof':stamp(self.now)},'axs_korea':{'asof':None,'error':'timeout'}},self.now)
        self.assertFalse(result['freshness_gate']['sources']['axs_korea'])
        self.assertIn('axs_korea: timeout',result['data_gaps'])
    def test_stale_source_blocks(self):
        self.assertFalse(fresh({'asof': None}, self.now, 600))
        self.assertFalse(fresh({'asof': stamp(self.now+dt.timedelta(hours=1))},self.now,600))
        self.assertEqual(scan(self.old, {}, self.now)['run_status'], 'DATA_STALE')
    def test_stale_run_and_nonowned_rejected(self):
        with self.assertRaisesRegex(ValueError,'STALE_RUN'):
            merge_owned({'last_run_at':stamp(self.now)}, {'last_run_at':stamp(self.now-dt.timedelta(seconds=1))})
        with self.assertRaisesRegex(ValueError,'NON_OWNED'):
            merge_owned({}, {'portfolio_ref':'erase'})
    def test_undelivered_fault_remains_one_pending_event(self):
        previous=dict(self.old,watchdog={'state':'SENTINEL_DEGRADED'},notification_decision={'type':'SENTINEL_DEGRADED','delivery_status':'FAILED','event_id':'same'})
        result=scan(previous,{},self.now)
        self.assertTrue(result['notification_decision']['required'])
        self.assertEqual(result['notification_decision']['event_id'],'same')
        previous['notification_decision']['delivery_status']='DELIVERED'
        self.assertFalse(scan(previous,{},self.now)['notification_decision']['required'])
    def test_first_degraded_is_not_silently_suppressed(self):
        self.assertEqual(watchdog(self.old,False,self.now)['transition'],'SENTINEL_DEGRADED')
    def test_watchdog_deduplicates_and_recovers(self):
        previous = dict(self.old, watchdog={'state':'HEALTHY','consecutive_invalid_scans':0})
        failed = watchdog(previous,False,self.now)
        self.assertEqual(failed['transition'],'SENTINEL_DEGRADED')
        previous['watchdog']=failed
        self.assertIsNone(watchdog(previous,False,self.now)['transition'])
        self.assertEqual(watchdog(previous,True,self.now)['transition'],'SENTINEL_RECOVERED')

class GitCASTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        self.remote=self.base/'remote.git'
        self.a=self.base/'a'; self.b=self.base/'b'
        self.cmd('git','init','--bare',str(self.remote))
        self.cmd('git','clone',str(self.remote),str(self.a))
        self.cmd('git','-C',str(self.a),'checkout','-b','main')
        self.write(self.a,{'portfolio_ref':'original','last_successful_scan_at':'2026-09-26T00:00:00Z'})
        self.commit(self.a)
        self.cmd('git','-C',str(self.a),'push','origin','main')
        self.cmd('git','clone','-b','main',str(self.remote),str(self.b))
        self.mutation={'run_id':'sentinel-test','last_run_at':'2026-10-06T08:00:00Z','run_status':'ANALYSIS_FAILED','runtime':{'real_trading_enabled':False}}
    def tearDown(self): self.tmp.cleanup()
    def cmd(self,*args):
        return subprocess.check_output(args,text=True,stderr=subprocess.DEVNULL).strip()
    def write(self,root,data): (root/'sentinel-state.json').write_text(json.dumps(data)+'\n')
    def commit(self,root):
        self.cmd('git','-C',str(root),'add','.')
        self.cmd('git','-C',str(root),'-c','user.name=test','-c','user.email=test@localhost','commit','-m','test')
    def test_real_cas_retry_preserves_concurrent_nonowned(self):
        def overlap(attempt):
            if attempt==0:
                self.write(self.b,{'portfolio_ref':'new-other-window','last_successful_scan_at':'2026-09-26T00:00:00Z'})
                self.commit(self.b); self.cmd('git','-C',str(self.b),'push','origin','main')
        result=persist(self.a,self.mutation,attempt_hook=overlap)
        self.assertTrue(result['verified'])
        self.assertEqual(result['state']['portfolio_ref'],'new-other-window')
        self.assertEqual(result['state']['last_successful_scan_at'],'2026-09-26T00:00:00Z')
        self.assertTrue(persist(self.a,self.mutation)['idempotent'])
    def test_newer_run_blocks_old_after_actual_conflict(self):
        def overlap(attempt):
            if attempt==0:
                self.write(self.b,{'run_id':'newer','last_run_at':'2026-10-06T09:00:00Z'})
                self.commit(self.b); self.cmd('git','-C',str(self.b),'push','origin','main')
        with self.assertRaisesRegex(ValueError,'STALE_RUN'):
            persist(self.a,self.mutation,attempt_hook=overlap)
    def test_write_failure_does_not_claim_cas_conflict(self):
        hook=self.remote/'hooks'/'pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError,'GITHUB_WRITE_FAILED'):
            persist(self.a,self.mutation)
        self.assertNotIn('run_id',json.loads(self.run_remote_state()))
    def run_remote_state(self):
        return self.cmd('git','--git-dir',str(self.remote),'show','main:sentinel-state.json')
    def test_three_real_conflicts_exhaust(self):
        def conflict(attempt):
            self.cmd('git','-C',str(self.b),'pull','--ff-only')
            self.write(self.b,{'portfolio_ref':str(attempt)})
            self.commit(self.b); self.cmd('git','-C',str(self.b),'push','origin','main')
        with self.assertRaisesRegex(RuntimeError,'CONCURRENCY_EXHAUSTED'):
            persist(self.a,self.mutation,attempt_hook=conflict)
    def test_registry_change_revokes_admission(self):
        (self.b/'sentinel-runtime.json').write_text('{"writer":"CHATGPT_AUTOMATION"}')
        self.commit(self.b); self.cmd('git','-C',str(self.b),'push','origin','main')
        with self.assertRaisesRegex(RuntimeError,'SINGLE_WRITER_ADMISSION_CHANGED'):
            persist(self.a,self.mutation,required_registry={'writer':'VULTR_SYSTEMD'})
    def test_readback_difference_fails_closed(self):
        def corrupt_readback():
            self.cmd('git','-C',str(self.b),'pull','--ff-only')
            self.write(self.b,{'run_id':'another','last_run_at':'2026-10-06T09:00:00Z'})
            self.commit(self.b); self.cmd('git','-C',str(self.b),'push','origin','main')
            self.cmd('git','-C',str(self.a),'fetch','origin','main')
        with self.assertRaisesRegex(ValueError,'READBACK_CONTENT_MISMATCH'):
            persist(self.a,self.mutation,readback_hook=corrupt_readback)

if __name__=='__main__': unittest.main()
