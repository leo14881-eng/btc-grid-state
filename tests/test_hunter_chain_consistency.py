import copy,datetime as dt,json,pathlib,tempfile,unittest,urllib.parse
from unittest.mock import patch
from research import hunter_policy as policy,hunter_shadow_trader_v2 as eng,hunter_tactical_capital_review as review,hunter_position_monitor as monitor,hunter_early_signals as early
NOW=dt.datetime(2026,10,5,tzinfo=dt.timezone.utc)
# Synthetic test-only provenance for in-memory positions; never a real commit.
# Patch only the external Git lookup, leaving identity admission and lifecycle live.
TEST_ONLY_SOURCE_SHA='0'*40
class ChainConsistencyTests(unittest.TestCase):
 def test_monitor_encodes_unicode_pair_and_retains_actual_book(self):
  states=[{'open_positions':[{'asset':'牛来'}]}]
  raw={'lastUpdateId':123,'bids':[['99','1000']],'asks':[['101','1000']]}
  def fetch(url):
   self.assertTrue(url.isascii())
   self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(url).query),{'symbol':['牛来USDT'],'limit':['100']})
   return raw
  signal={'score':12,'independent_signal_count':3}
  with patch.object(monitor,'load',return_value={}),patch.object(monitor.signals,'rolling',return_value={}),patch.object(monitor.signals,'micro',return_value={}),patch.object(monitor.signals,'score_row',return_value=signal),patch.object(monitor.books,'live_fetch',side_effect=fetch):
   _,liq,meta=monitor.refresh_management_evidence(states,{}, {'candidates':[]},{},NOW)
  self.assertEqual(meta['failures'],{})
  self.assertEqual(meta['observed_raw_books']['牛来']['symbol'],'牛来USDT')
  self.assertEqual(liq['snapshots']['牛来']['raw_book_evidence']['bids'],raw['bids'])

 def weak(self):
  return {'score':4,'independent':1,'btc_rel_1h':-1,'btc_rel_4h':-1,'rel_accel':-2,'blockers':[]}
 def test_same_evidence_never_counts_three_times(self):
  p={'degraded_cycles':99};e=self.weak();e['signal_evidence']=policy.stamp('X','g',NOW.isoformat())
  self.assertEqual(eng.position_health(p,e,NOW)[0],'WEAKENING')
  for _ in range(5):self.assertEqual(eng.position_health(p,e,NOW)[0],'EVIDENCE_PENDING')
  self.assertEqual(p['degraded_cycles'],1)
 def test_stale_and_out_of_order_observations_do_not_count(self):
  p={};e=self.weak();e['signal_evidence']=policy.stamp('X','g',(NOW-dt.timedelta(hours=2)).isoformat())
  self.assertEqual(eng.position_health(p,e,NOW)[0],'EVIDENCE_PENDING');self.assertNotIn('degraded_cycles',p)
  e['signal_evidence']=policy.stamp('X','g',NOW.isoformat());eng.position_health(p,e,NOW)
  e['signal_evidence']=policy.stamp('X','older',(NOW-dt.timedelta(minutes=5)).isoformat())
  self.assertEqual(eng.position_health(p,e,NOW)[0],'EVIDENCE_PENDING');self.assertEqual(p['degraded_cycles'],1)
 def test_policy_preserves_existing_parameters(self):
  self.assertEqual(eng.TRANCHES,(1000.,1000.,1000.));self.assertEqual(eng.TARGET,8)
  self.assertEqual(eng.PROTECT_ARM_PCT,2);self.assertEqual(eng.CAPITAL_POOL_USDT,20000)
  self.assertFalse(policy.POLICY['time_exit_enabled']);self.assertIsNone(policy.POLICY['max_open'])
 def capital_fixture(self,change=5,price=100,rr=1.7,stage='EARLY',anchor=100,venue=None):
  signal={'base':'X','stage':stage,'score':12,'independent_signal_count':3,'btc_relative_1h_pct':2,'btc_relative_4h_pct':3,'relative_acceleration_pct':1}
  if venue=='bybit':signal.update(source_venue='bybit',execution_supported=False)
  coin={'reference_price':price,'change_24h_pct':change}
  if venue:coin['venues']=[venue]
  scan={'generation_id':'g','as_of_utc':NOW.isoformat(),'binance_complete':True,'coins':{'X':coin}}
  data={review.SCAN:scan,review.EARLY:{'scan_generation_id':'g','as_of_utc':NOW.isoformat(),'early':[signal] if stage=='EARLY' else [],'all_signals':[signal]},review.LIQ:{'scan_as_of_utc':NOW.isoformat(),'snapshots':{'X':{'as_of_utc':NOW.isoformat(),'spread_bps':10,'bid_depth_2pct_usdt':50000,'ask_depth_2pct_usdt':50000,'execution_scenarios':{'3000':{'estimated_rr':rr,'buy_slippage_bps':10}}}}},review.IDENTITY:{'scan_as_of_utc':NOW.isoformat(),'assets':{'X':{'capital_identity_pass':True}}},review.CAPITAL:{'capital_pool_usdt':20000,'open_cost_usdt':0,'pending_reservations_usdt':0},review.HISTORY:{'assets':{'X':{'first_early_price':anchor}}},review.ROOT/'hunter-shadow-portfolio.json':{'open_positions':[{'asset':'X'}]}}
  class Clock(dt.datetime):
   @classmethod
   def now(cls,tz=None):return NOW
  with tempfile.TemporaryDirectory() as d,patch.object(review,'read',lambda p,d={}:copy.deepcopy(data.get(p,d))),patch.object(review,'OUT',pathlib.Path(d)/'review.json'),patch.object(review.dt,'datetime',Clock):
   review.main();return json.loads(review.OUT.read_text())
 def test_chase_rule_changes_authoritative_buy(self):
  low=self.capital_fixture(change=25,rr=1.7);high=self.capital_fixture(change=25,rr=2)
  self.assertEqual(low['candidates'][0]['trade_action'],'REJECT');self.assertEqual(high['executable_buy'],['X'])
 def test_anchor_distance_and_missing_anchor_fail_closed(self):
  self.assertEqual(self.capital_fixture(price=113,rr=1.7)['candidates'][0]['trade_action'],'REJECT')
  out=self.capital_fixture(anchor=None)
  self.assertEqual(out['candidates'][0]['trade_action'],'SYSTEM_BLOCKED');self.assertIn('DISCOVERY_ANCHOR_MISSING',out['candidates'][0]['blockers'])
 def test_left_side_and_watch_held_asset(self):
  self.assertEqual(self.capital_fixture(change=-10)['executable_buy'],['X'])
  out=self.capital_fixture(stage='WATCH');self.assertEqual(len(out['candidates']),1);self.assertEqual(out['executable_buy'],[])
 def test_bybit_only_early_cannot_pass_binance_execution_gate(self):
  out=self.capital_fixture(venue='bybit')
  self.assertEqual(out['executable_buy'],[])
  self.assertEqual(out['candidates'][0]['trade_action'],'SYSTEM_BLOCKED')
  self.assertIn('VENUE_SPECIFIC_EXECUTION_AND_MONITOR_NOT_INTEGRATED',out['candidates'][0]['blockers'])
 def test_expired_signal_cannot_add_or_momentum_exit(self):
  for price in (99,110):
   pos={'asset':'X','opened_at_utc':NOW.isoformat(),'tranches':[{'price':100,'notional_usdt':1000}],'mfe_pct':0}
   c={'asset':'X','signal':{'score':12,'independent_signal_count':3,'btc_relative_1h_pct':2,'btc_relative_4h_pct':3,'relative_acceleration_pct':1},'signal_evidence':policy.stamp('X','old',(NOW-dt.timedelta(hours=2)).isoformat()),'execution_scenario':{'estimated_rr':2,'buy_slippage_bps':10},'blockers':[]}
   # Admit a fresh market observation so this exercises expired signal evidence,
   # rather than the earlier missing market-generation/time guard.
   state={'open_positions':[pos]};scan={'generation_id':'current','as_of_utc':NOW.isoformat(),'coins':{'X':{'reference_price':price}}}
   liq={'snapshots':{'X':{'as_of_utc':NOW.isoformat(),'spread_bps':10,'bid_depth_2pct_usdt':50000,'ask_depth_2pct_usdt':50000}}}
   with patch.object(eng,'_execution_source_sha',return_value=TEST_ONLY_SOURCE_SHA) as source_sha:
    eng.manage_existing_positions(state,scan,{'candidates':[c]},liq,{},NOW)
    source_sha.assert_called_once_with()
   self.assertEqual(len(state['open_positions']),1);self.assertEqual(len(pos['tranches']),1);self.assertEqual(state['events'],[])
 def test_price_protection_still_operates_without_signal(self):
  p={'asset':'X','opened_at_utc':NOW.isoformat(),'tranches':[{'price':100,'notional_usdt':1000}],'mfe_pct':4}
  from research import hunter_lifecycle_state as life
  def book(price,now):return {'exchange':'binance','market':'spot','symbol':'XUSDT','price_unit':'USDT','quantity_unit':'BASE','bids':[[price,100]],'asks':[[price+.01,100]],'fetched_at':now.isoformat()}
  before=NOW-dt.timedelta(minutes=5)
  life.protect(p,104,eng.net_pnl(p,104),life.liquidation(p,book(104,before),before),before,'armed',before.isoformat())
  state={'open_positions':[p]}
  with patch.object(eng,'_execution_source_sha',return_value=TEST_ONLY_SOURCE_SHA) as source_sha:
   eng.manage_existing_positions(state,{'as_of_utc':NOW.isoformat(),'generation_id':'current','coins':{'X':{'reference_price':101}}},{}, {'snapshots':{'X':{'raw_book_evidence':book(101,NOW)}}},{},NOW)
   source_sha.assert_called_once_with()
  self.assertEqual(state['closed_positions'][0]['exit_reason'],'PROFIT_PROTECTION')
  self.assertEqual(state['closed_positions'][0]['execution_identity_proof']['source_main_sha'],TEST_ONLY_SOURCE_SHA)
 def test_monitor_refreshes_watch_holdings_without_new_entry(self):
  states=[{'open_positions':[{'asset':'X'}]}];market={'X':{'reference_price':100}}
  values={'XUSDT':{'return_pct':2},'BTCUSDT':{'return_pct':0}}
  raw={'source_timestamp':None,'execution_verified':False,'symbol':'XUSDT','bids':[['99','10']],'asks':[['101','10']]}
  book={'as_of_utc':NOW.isoformat(),'spread_bps':10,'bid_depth_2pct_usdt':50000,'ask_depth_2pct_usdt':50000,'raw_book_evidence':raw}
  with patch.object(monitor,'load',return_value={}),patch.object(monitor.signals,'rolling',return_value=values),patch.object(monitor.signals,'micro',return_value={}),patch.object(monitor.books,'live_fetch',return_value={}),patch.object(monitor.books,'measure',return_value=book),patch.object(monitor.books,'estimate',return_value={'estimated_rr':2,'buy_slippage_bps':10}):
   r,l,meta=monitor.refresh_management_evidence(states,market,{'candidates':[]},{},NOW)
  self.assertEqual(meta['attempted'],['X']);self.assertTrue(r['candidates'][0]['management_only']);self.assertIn('signal_evidence',r['candidates'][0]);self.assertIn('X',l['snapshots'])
  self.assertEqual(meta['observed_raw_books'],{'X':raw})
 def test_failed_refresh_does_not_reuse_old_signal(self):
  with patch.object(monitor,'load',return_value={}),patch.object(monitor.signals,'rolling',side_effect=RuntimeError('offline')):
   r,_,meta=monitor.refresh_management_evidence([{'open_positions':[{'asset':'X'}]}],{}, {'candidates':[{'asset':'X','signal_evidence':policy.stamp('X','old',NOW.isoformat())}]},{},NOW)
  self.assertEqual(r['candidates'][0]['signal_evidence'],{});self.assertIn('X',meta['failures'])
  self.assertEqual(meta['observed_raw_books'],{})
if __name__=='__main__':unittest.main()
