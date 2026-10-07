import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.sentinel_runtime import scan, stamp, merge_owned, watchdog, persist, fresh, source, collect_depth, spot_source, oi_history, treasury_daily, etf_latest_complete, UTC

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
    def test_freshness_rejects_ambiguous_timestamp_and_failed_source(self):
        for record in ({'asof': '2026-10-06T08:00:00'},
                       {'asof': stamp(self.now), 'error': 'invalid response'},
                       {'asof': 1791273600000}, None):
            with self.subTest(record=record):
                self.assertFalse(fresh(record, self.now, 600))
        self.assertTrue(fresh({'asof': '2026-10-06T15:00:00+07:00'}, self.now, 600))
    def test_failed_timestamped_source_cannot_admit_evidence_scan(self):
        evidence = {key: {'asof': stamp(self.now)} for key in
                    ('btc_spot', 'btc_structure', 'btc_oi', 'btc_funding')}
        evidence['btc_oi']['error'] = 'response schema invalid'
        result = scan(self.old, evidence, self.now)
        self.assertEqual(result['run_status'], 'DATA_STALE')
        self.assertFalse(result['freshness_gate']['valid_evidence_scan'])
        self.assertFalse(result['freshness_gate']['new_capital_action_allowed'])
    def test_depth_fallback_keeps_venue_timestamp_and_failed_primary(self):
        def get(url):
            if 'binance.com' in url:
                raise OSError('primary offline')
            return {'retCode':0,'result':{'s':'BTCUSDT','ts':1791273600000,
                    'u':123,'b':[['85000','2']],'a':[['85010','3']]}}
        result=collect_depth(get)
        self.assertEqual(result['venue'],'BYBIT_SPOT')
        self.assertTrue(result['asof'])
        self.assertIn('primary offline',result['fallback_attempts'][0]['error'])
        self.assertIsNone(result['fallback_attempts'][0]['fetched_at'])
        self.assertEqual(result['bid_depth_usdt_returned_levels'],170000)
        self.assertEqual(result['cross_venue_usage'],'EVIDENCE_ONLY_NOT_EXECUTION_VENUE_PROOF')
    def test_receipt_only_depth_does_not_pass_when_fallback_fails(self):
        def get(url):
            if 'bybit.com' in url: raise OSError('fallback offline')
            return {'lastUpdateId':123,'bids':[['85000','2']],'asks':[['85010','3']]}
        result=collect_depth(get)
        self.assertIsNone(result['asof'])
        self.assertFalse(fresh(result,self.now,600))
        self.assertEqual(len(result['fallback_attempts']),3)
    def test_wrong_symbol_fallback_is_rejected(self):
        def get(url):
            if 'binance.com' in url: raise OSError('offline')
            return {'retCode':0,'result':{'s':'ETHUSDT','ts':1791273600000}}
        result=collect_depth(get)
        self.assertIsNone(result['asof'])
        self.assertTrue(any('SYMBOL_MISMATCH' in x.get('error','') for x in result['fallback_attempts']))
    def test_official_same_venue_spot_fallback_preserves_attempts(self):
        def get(url):
            if 'api.binance.com' in url: raise OSError('451 blocked')
            return {'closeTime':1791273600000,'lastPrice':'85000'}
        result=spot_source('https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT',
            lambda d:{'asof':stamp(self.now),'price':float(d['lastPrice'])},get)
        self.assertIn('data-api.binance.vision',result['source'])
        self.assertEqual(result['source_venue'],'BINANCE')
        self.assertEqual(result['market_type'],'SPOT')
        self.assertIn('451 blocked',result['fallback_attempts'][0]['error'])
    def test_oi_history_retains_actual_window_and_units(self):
        result=oi_history([{'timestamp':1791270000000,'sumOpenInterest':'10','sumOpenInterestValue':'800000'},
                           {'timestamp':1791273600000,'sumOpenInterest':'12','sumOpenInterestValue':'960000'}])
        self.assertAlmostEqual(result['change_pct_over_returned_window'],20)
        self.assertEqual(result['source_venue'],'BINANCE')
        self.assertEqual(result['value_unit'],'USDT')
        self.assertNotEqual(result['window_start_asof'],result['window_end_asof'])
    def test_treasury_daily_is_not_fabricated_intraday_dxy(self):
        xml='<feed xmlns:d="x"><properties><d:NEW_DATE>2026-10-05T00:00:00</d:NEW_DATE><d:BC_2YEAR>4.84</d:BC_2YEAR><d:BC_10YEAR>5.31</d:BC_10YEAR><d:BC_30YEAR>5.66</d:BC_30YEAR></properties></feed>'
        result=treasury_daily(xml)
        self.assertEqual(result['asof_date'],'2026-10-05')
        self.assertIsNone(result['asof'])
        self.assertIsNone(result['dxy'])
        self.assertEqual(result['us10y_pct'],5.31)
        self.assertFalse(fresh(result,self.now,600))
    def test_etf_source_complete_row_and_pending_zero_are_separate(self):
        html='<table><tr><td></td><td>IBIT</td><td>FBTC</td><td>ARKB</td><td></td></tr><tr><td>05 Oct 2026</td><td>69.9</td><td>(74.5)</td><td>(85.2)</td><td>(89.8)</td></tr><tr><td>06 Oct 2026</td><td>-</td><td>-</td><td>-</td><td>0.0</td></tr></table>'
        result=etf_latest_complete(html,self.now)
        self.assertEqual(result['latest_complete_session']['date'],'2026-10-05')
        self.assertEqual(result['latest_complete_session']['net_flow_usd_m'],-89.8)
        self.assertEqual(result['unpublished_or_incomplete_sessions'][0]['status'],'NOT_PUBLISHED_OR_PARTIAL')
        self.assertIsNone(result['asof'])
    def test_etf_total_mismatch_is_not_silently_accepted(self):
        html='<table><tr><td></td><td>IBIT</td><td>FBTC</td><td></td></tr><tr><td>05 Oct 2026</td><td>69.9</td><td>(74.5)</td><td>(159.7)</td></tr></table>'
        with self.assertRaisesRegex(ValueError,'TOTAL_MISMATCH'): etf_latest_complete(html,self.now)
    def test_etf_all_numeric_today_before_close_is_not_complete(self):
        html='<table><tr><td></td><td>IBIT</td><td>FBTC</td><td></td></tr><tr><td>06 Oct 2026</td><td>0</td><td>0</td><td>0</td></tr></table>'
        with self.assertRaisesRegex(ValueError,'NO_COMPLETE'): etf_latest_complete(html,self.now)
    def test_secondary_failure_does_not_turn_unknown_to_pass(self):
        result=scan(self.old,{'btc_spot':{'asof':stamp(self.now)},'btc_depth':{'asof':None,'error':'timeout'}},self.now)
        self.assertFalse(result['freshness_gate']['sources']['btc_depth'])
        self.assertIn('btc_depth: timeout',result['data_gaps'])
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
