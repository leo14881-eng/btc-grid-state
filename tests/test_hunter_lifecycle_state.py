import copy
import datetime as dt
import json
import os
import subprocess
import sys
import unittest
from research import hunter_lifecycle_state as h
from research import hunter_shadow_trader_v2 as e
from research.hunter_policy import stamp

NOW=dt.datetime(2026,10,7,tzinfo=dt.timezone.utc)
def position():return {'asset':'ENA','shadow_id':'unit-only','tranches':[{'price':100,'notional_usdt':1000,'buy_slippage_bps':0}],'mfe_pct':0,'mae_pct':0,'opened_at_utc':NOW.isoformat()}
def book(price,now):return {'exchange':'binance','market':'spot','symbol':'ENAUSDT','price_unit':'USDT','quantity_unit':'BASE','bids':[[str(price),'100']],'asks':[[str(price+.01),'100']],'fetched_at':now.isoformat(),'source_timestamp':None}
class PersistentProtectionTests(unittest.TestCase):
 def observe(self,pos,p,i,at=None):
  now=at or NOW+dt.timedelta(minutes=5*i)
  return h.protect(pos,p,e.net_pnl(pos,p),h.liquidation(pos,book(p,now),now),now,'g'+str(i),now.isoformat())
 def test_research_direct_entrypoint_imports_lifecycle_without_pythonpath(self):
  env=dict(os.environ);env.pop('PYTHONPATH',None);env['HUNTER_SHADOW_FREEZE']='1'
  r=subprocess.run([sys.executable,'research/hunter_shadow_trader_v2.py'],env=env,capture_output=True,text=True)
  self.assertEqual(r.returncode,0,r.stderr)
  self.assertEqual(json.loads(r.stdout)['writes'],0)

 def test_under_arm_and_historical_mfe_do_not_create_live_arm(self):
  p=position();p['mfe_pct']=20
  self.assertFalse(self.observe(p,101,0)['armed'])
  self.assertNotIn('armed_at_utc',p['protection_lifecycle'])
 def test_arm_new_high_and_positive_giveback_sell_intent(self):
  p=position();self.assertFalse(self.observe(p,103,0)['exit'])
  armed=p['protection_lifecycle']['armed_at_utc']
  self.assertFalse(self.observe(p,106,1)['exit'])
  self.assertTrue(self.observe(p,103,2)['exit'])
  self.assertEqual(p['protection_lifecycle']['armed_at_utc'],armed)
 def test_gap_negative_never_fakes_positive_sell(self):
  p=position();self.observe(p,103,0)
  r=self.observe(p,95,1)
  self.assertFalse(r['exit']);self.assertEqual(r['incident'],'GAPPED_THROUGH_PROTECTION_WINDOW')
 def test_restart_duplicate_and_stale(self):
  p=position();self.observe(p,103,0);p=json.loads(json.dumps(p))
  self.assertTrue(self.observe(p,100.5,1)['exit'])
  self.assertFalse(self.observe(p,100.5,1)['exit'])
  before=copy.deepcopy(p['protection_lifecycle'])
  r=h.protect(p,95,-50,h.liquidation(p,book(95,NOW),NOW),NOW+dt.timedelta(hours=2),'future',NOW.isoformat())
  self.assertFalse(r['exit']);self.assertEqual(p['protection_lifecycle'],before)
 def test_unknown_cost_does_not_arm_or_exit(self):
  p=position();r=h.protect(p,105,50,{'net_pnl_usdt':None,'status':'UNKNOWN'},NOW,'g',NOW.isoformat())
  self.assertFalse(r['armed']);self.assertFalse(r['exit'])
 def test_full_quantity_cost_and_depth_fail_closed(self):
  p=position();b=book(103,NOW);b['bids']=[['103','1']]
  self.assertEqual(h.liquidation(p,b,NOW)['status'],'UNKNOWN')
  b=book(103,NOW);b['symbol']='PENDLEUSDT'
  self.assertEqual(h.liquidation(p,b,NOW)['status'],'UNKNOWN')
 def test_different_positions_do_not_share_lifecycle(self):
  a=position();b=position();self.observe(a,103,0)
  self.assertNotIn('protection_lifecycle',b)

class HealthRecoveryTests(unittest.TestCase):
 def evidence(self,i,strong=False):return {'score':12 if strong else 1,'independent':3 if strong else 0,'btc_rel_1h':2 if strong else -2,'btc_rel_4h':2 if strong else -2,'rel_accel':1 if strong else -2,'signal_evidence':stamp('ENA','h'+str(i),(NOW+dt.timedelta(minutes=5*i)).isoformat()),'blockers':[]}
 def tick(self,p,i,strong=False):
  now=NOW+dt.timedelta(minutes=5*i);ev=self.evidence(i,strong);health,_=e.position_health(p,ev,now)
  h.recovery(p,ev,now,-20,p.get('last_health_generation_id'),health)
  return health
 def test_single_weak_reason_is_not_strong(self):
  p=position();ev=self.evidence(0,True);ev['score']=0
  self.assertEqual(e.position_health(p,ev,NOW)[0],'WEAKENING')
 def test_same_generation_different_hash_cannot_degrade_again(self):
  p=position();ev=self.evidence(0);e.position_health(p,ev,NOW)
  ev['signal_evidence']['evidence_id']='different'
  self.assertEqual(e.position_health(p,ev,NOW)[0],'EVIDENCE_PENDING');self.assertEqual(p['degraded_cycles'],1)
 def test_persistent_invalidation_observation_no_loss_sell_and_recovery_clear(self):
  p=position()
  for i in range(5):self.tick(p,i)
  self.assertEqual(p['recovery_state'],'PERSISTENT_INVALIDATION')
  self.assertFalse(p['loss_recovery_lifecycle']['risk_reduction_authorized'])
  for i in range(5,8):self.tick(p,i,True)
  self.assertEqual(p['recovery_state'],'RECOVERED')
  self.assertEqual(p['health_reasons'],[])
  self.assertEqual(p['recovery_generation_id'],p['last_health_generation_id'])
 def test_missing_observation_breaks_degrade_confirmation(self):
  p=position();self.tick(p,0);self.tick(p,1)
  self.tick(p,10)
  self.assertEqual(p['degraded_cycles'],1)
  self.assertEqual(p['health_lifecycle_snapshot']['generation_id'],p['last_health_generation_id'])

 def test_stale_keeps_joint_state_unchanged(self):
  p=position();self.tick(p,0);before=copy.deepcopy(p)
  self.assertEqual(e.position_health(p,self.evidence(1),NOW+dt.timedelta(hours=4))[0],'EVIDENCE_PENDING')
  self.assertEqual(p,before)

class CapitalMtmTests(unittest.TestCase):
 def scan(self):return {'coins':{'BTC':{'change_24h_pct':4},'A':{'change_24h_pct':2}}}
 def test_tail_diagnostic_is_not_capital_cap_and_profit_cannot_expand_pool(self):
  s={'open_positions':[],'closed_positions':[{'net_pnl_usdt':10000}]}
  self.assertEqual(e.capital_limit(s,self.scan()),20000)
  self.assertEqual(e.reserve_snapshot(s)['strategic_reserve_available'],3000)
 def test_ordinary_cannot_take_reserve_weak_edge(self):
  s={'open_positions':[{'tranches':[{'notional_usdt':17000}]}]}
  self.assertFalse(e.capital_available(s,1000,'BUY',self.scan()))
  ok,reasons=e.marginal_capital_gate(s,1000,{'estimated_rr':2,'btc_rel_1h':1,'btc_rel_4h':1,'rel_accel':1},'BUY',self.scan())
  self.assertFalse(ok);self.assertIn('STRATEGIC_RESERVE_EXISTING_HIGH_QUALITY_EDGE_REQUIRED',reasons)
 def test_mtm_closed_wins_do_not_hide_open_losses_unknown_cost(self):
  p=position();p.update(last_price=90,last_marked_at_utc=NOW.isoformat(),health_state='THESIS_INVALIDATED',recovery_state='LOSS_RECOVERY')
  m=h.mtm({'open_positions':[p],'closed_positions':[{'net_pnl_usdt':20}]},NOW,e.net_pnl,10)
  self.assertEqual(m['closed_win_rate'],1)
  self.assertLess(m['reference_mark_to_market_net_pnl_usdt'],0)
  self.assertEqual(m['estimated_exit_cost_usdt'],'UNKNOWN')
  self.assertEqual(m['open_loss_exposure_usdt'],1000)
