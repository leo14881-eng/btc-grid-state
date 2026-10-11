import unittest
import research.hunter_shadow_trader_v2 as eng
from research.hunter_shadow_trader_v2 import decision,discovery_decision,bybit_channel,market_shock,weighted_entry,add,profit_protection,scenario_returns,position_health,reentry_allowed,REVIEW_HOURS
class V2CapitalDecisionTests(unittest.TestCase):
 def base(self):
  c={"asset":"X","signal":{"score":12,"independent_signal_count":3,"btc_relative_1h_pct":2,"btc_relative_4h_pct":3,"relative_acceleration_pct":1},
   "execution_scenario":{"buy_slippage_bps":10,"estimated_rr":2},"blockers":["PORTFOLIO_USAGE_REQUIRES_CURRENT_INPUT"]}
  scan={"coins":{"X":{"reference_price":96,"change_24h_pct":5}}}
  liq={"snapshots":{"X":{"spread_bps":10,"bid_depth_2pct_usdt":50000,"ask_depth_2pct_usdt":50000}}}
  supply={"assets":{"X":{"tactical_supply_risk_verified":True,"status":"FULLY_UNLOCKED"}}}
  return c,scan,liq,supply
 def test_entry_requires_full_evidence(self):
  c,s,l,u=self.base();self.assertEqual(decision(c,s,l,u,"ENTRY")[0],"BUY")
 def test_missing_liquidity_rejects(self):
  c,s,l,u=self.base();self.assertEqual(decision(c,s,{},u,"ENTRY")[0],"REJECT")
 def test_incomplete_supply_alone_is_nonblocking(self):
  c,s,l,u=self.base();self.assertEqual(decision(c,s,l,{},"ENTRY")[0],"BUY")
 def test_confirmed_major_supply_risk_rejects(self):
  c,s,l,u=self.base();u={"assets":{"X":{"status":"CONFIRMED_MAJOR_NEAR_TERM_UNLOCK","confirmed_major_near_term_unlock":True}}}
  self.assertEqual(decision(c,s,l,u,"ENTRY")[0],"REJECT")
 def test_relative_weakness_alone_does_not_force_exit(self):
  c,s,l,u=self.base();c["signal"]["btc_relative_1h_pct"]=-2.5;c["signal"]["btc_relative_4h_pct"]=-3.5
  act,reasons,_=decision(c,s,l,u,"HOLD")
  self.assertEqual(act,"HOLD");self.assertIn("SEVERE_BTC_RELATIVE_WEAKNESS_REVIEW",reasons)
 def test_real_evidence_blocker_rejects(self):
  c,s,l,u=self.base();c["blockers"].append("OFFICIAL_ASSET_IDENTITY_UNVERIFIED");self.assertEqual(decision(c,s,l,u,"ENTRY")[0],"REJECT")
 def test_missing_candidate_is_review_only_for_existing(self):
  c,s,l,u=self.base();self.assertEqual(decision(None,s,l,u,"HOLD")[0],"HOLD")
 def test_add_needs_material_better_price_fresh_evidence_and_recovery(self):
  import datetime as dt
  c,s,l,u=self.base();base=dt.datetime(2026,10,4,10,0,tzinfo=dt.timezone.utc);obs=base+dt.timedelta(minutes=16)
  c["signal"]["return_1h_pct"]=3;c["signal_evidence"]=eng.stamp("X","g2",obs.isoformat());l["snapshots"]["X"]["as_of_utc"]=obs.isoformat()
  p={"tranches":[{"price":100,"notional_usdt":1000,"at":base.isoformat(),"signal_evidence_id":"old","signal_generation_id":"g1"}]}
  act,reasons,_=decision(c,s,l,u,"ADD",p,98.5);self.assertEqual(act,"ADD");self.assertIn("MATERIAL_BETTER_THAN_LAST_TRANCHE",reasons)
  self.assertEqual(decision(c,s,l,u,"ADD",p,101)[0],"HOLD")
  c["signal"]["relative_acceleration_pct"]=-2;c["signal"]["btc_relative_1h_pct"]=-1
  self.assertEqual(decision(c,s,l,u,"ADD",p,96)[0],"HOLD")
 def test_axs_regression_tiny_drop_does_not_add(self):
  import datetime as dt
  c,s,l,u=self.base();base=dt.datetime(2026,10,4,10,59,16,tzinfo=dt.timezone.utc);obs=base+dt.timedelta(minutes=39)
  c["signal"].update({"return_1h_pct":3.2782,"btc_relative_1h_pct":3.2782,"btc_relative_4h_pct":-3.7119,"relative_acceleration_pct":4.2062})
  c["signal_evidence"]=eng.stamp("X","axs-g2",obs.isoformat());l["snapshots"]["X"]["as_of_utc"]=obs.isoformat()
  p={"tranches":[{"price":1.396,"notional_usdt":1000,"at":base.isoformat(),"signal_evidence_id":"axs-old","signal_generation_id":"axs-g1"}]}
  act,reasons,_=decision(c,s,l,u,"ADD",p,1.391)
  self.assertEqual(act,"HOLD");self.assertTrue(any(x.startswith("ADD_PRICE_IMPROVEMENT_TOO_SMALL_") for x in reasons))
 def test_axs_regression_third_add_cannot_be_above_last_fill(self):
  import datetime as dt
  base=dt.datetime(2026,10,4,10,59,16,tzinfo=dt.timezone.utc)
  c,s,l,u=self.base();c["signal"]["return_1h_pct"]=3
  p={"tranches":[{"price":1.396,"notional_usdt":1000,"at":base.isoformat()},{"price":1.391,"notional_usdt":1000,"at":(base+dt.timedelta(minutes=39)).isoformat()}]}
  act,reasons,_=decision(c,s,l,u,"ADD",p,1.393)
  self.assertEqual(act,"HOLD");self.assertIn("ADD_NOT_BELOW_LAST_TRANCHE",reasons)
 def test_add_rejects_reused_or_too_soon_evidence(self):
  import datetime as dt
  c,s,l,u=self.base();base=dt.datetime(2026,10,4,10,0,tzinfo=dt.timezone.utc);obs=base+dt.timedelta(minutes=5)
  c["signal"]["return_1h_pct"]=3;c["signal_evidence"]=eng.stamp("X","g1",obs.isoformat());l["snapshots"]["X"]["as_of_utc"]=obs.isoformat()
  p={"tranches":[{"price":100,"notional_usdt":1000,"at":base.isoformat(),"signal_evidence_id":c["signal_evidence"]["evidence_id"],"signal_generation_id":"g1"}]}
  act,reasons,_=decision(c,s,l,u,"ADD",p,98)
  self.assertEqual(act,"HOLD");self.assertTrue("ADD_SIGNAL_EVIDENCE_NOT_NEW" in reasons or "ADD_WAIT_NEW_15M_EVIDENCE_WINDOW" in reasons)
 def test_third_add_requires_stronger_recovery(self):
  import datetime as dt
  c,s,l,u=self.base();base=dt.datetime(2026,10,4,10,0,tzinfo=dt.timezone.utc);obs=base+dt.timedelta(minutes=40)
  c["signal"].update({"return_1h_pct":3,"btc_relative_1h_pct":2.4,"btc_relative_4h_pct":-4.4,"relative_acceleration_pct":3.5})
  c["signal_evidence"]=eng.stamp("X","g3",obs.isoformat());l["snapshots"]["X"]["as_of_utc"]=obs.isoformat()
  p={"tranches":[{"price":100,"notional_usdt":1000,"at":base.isoformat()},{"price":98,"notional_usdt":1000,"at":(base+dt.timedelta(minutes=20)).isoformat(),"signal_evidence_id":"g2-e","signal_generation_id":"g2"}]}
  act,reasons,_=decision(c,s,l,u,"ADD",p,96)
  self.assertEqual(act,"HOLD");self.assertIn("ADD_THIRD_TRANCHE_RECOVERY_NOT_STRONG_ENOUGH",reasons)
 def test_weighted_cost_falls(self):
  c,s,l,u=self.base();p={"tranches":[{"price":100,"notional_usdt":1000}]};e=decision(c,s,l,u,"ADD",p,96)[2]
  import datetime as dt;add(p,96,e,dt.datetime.now(dt.timezone.utc));self.assertLess(weighted_entry(p),100)
 def test_profit_protection_arms_after_real_mfe(self):
  p={"tranches":[{"price":100,"notional_usdt":1000}],"mfe_pct":3.0}
  self.assertTrue(profit_protection(p,100.5)["exit"])
 def test_profit_protection_does_not_fake_unprofitable_trade(self):
  p={"tranches":[{"price":100,"notional_usdt":1000}],"mfe_pct":1.5}
  self.assertFalse(profit_protection(p,99)["armed"])
 def test_profit_protection_caps_large_giveback(self):
  p={"tranches":[{"price":100,"notional_usdt":1000}],"mfe_pct":7.0}
  self.assertTrue(profit_protection(p,104)["exit"])
 def test_time_is_review_only(self):self.assertEqual(REVIEW_HOURS,(1.,6.,24.,48.,72.))
 def test_tranche_counterfactuals_compare_one_vs_two(self):
  p={"tranches":[{"price":100,"notional_usdt":1000,"buy_slippage_bps":0},{"price":90,"notional_usdt":1000,"buy_slippage_bps":0}]}
  x=scenario_returns(p,96);self.assertIn("1_tranche",x);self.assertIn("2_tranche",x)
  self.assertGreater(x["2_tranche"]["net_return_pct"],x["1_tranche"]["net_return_pct"])
 def test_strong_profitable_signal_remains_valid(self):
  c,s,l,u=self.base();self.assertEqual(decision(c,s,l,u,"HOLD")[0],"HOLD")
 def test_right_side_chase_requires_extra_edge(self):
  c,s,l,u=self.base();s["coins"]["X"]["change_24h_pct"]=25
  self.assertEqual(decision(c,s,l,u,"ENTRY")[0],"BUY")
  c["execution_scenario"]["estimated_rr"]=1.7
  self.assertEqual(decision(c,s,l,u,"ENTRY")[0],"REJECT")
 def test_left_side_entry_not_rejected_for_not_rising(self):
  c,s,l,u=self.base();s["coins"]["X"]["change_24h_pct"]=-8
  self.assertEqual(decision(c,s,l,u,"ENTRY")[0],"BUY")
 def test_discovery_gate_is_broader_than_executable_gate(self):
  c,s,l,u=self.base();l={}
  self.assertEqual(discovery_decision(c)[0],"BUY")
  self.assertEqual(decision(c,s,l,u,"ENTRY")[0],"REJECT")
 def test_bybit_channel_unknown_is_not_not_listed(self):
  self.assertEqual(bybit_channel({"spot":{"status":"UNKNOWN"},"alpha":{"status":"UNKNOWN"}},"X")["channel"],"UNKNOWN")
  self.assertEqual(bybit_channel({"spot":{"status":"OK","symbols":["X"]},"alpha":{"status":"UNKNOWN"}},"X")["channel"],"BYBIT_SPOT")
 def test_health_requires_persistent_multifactor_decay(self):
  p={};e={"score":4,"independent":1,"btc_rel_1h":-1,"btc_rel_4h":-1,"rel_accel":-2,"spread_bps":10,"bid_depth_2pct_usdt":50000,"ask_depth_2pct_usdt":50000,"supply_confirmed_major_risk":False,"blockers":[]}
  import datetime as dt
  now=dt.datetime.now(dt.timezone.utc)
  for i,expected in enumerate(("WEAKENING","DEGRADED","THESIS_INVALIDATED")):
   observed=now+dt.timedelta(seconds=i);e["signal_evidence"]=eng.stamp("X",str(i),observed.isoformat())
   self.assertEqual(position_health(p,e,observed)[0],expected)
   self.assertEqual(position_health(p,e,observed)[0],"EVIDENCE_PENDING")
 def test_hard_invalidation_is_separate_from_signal_decay(self):
  p={};e={"score":12,"independent":3,"btc_rel_1h":2,"btc_rel_4h":3,"rel_accel":1,"spread_bps":250,"bid_depth_2pct_usdt":50000,"ask_depth_2pct_usdt":50000,"supply_confirmed_major_risk":False,"blockers":[]}
  import datetime as dt
  e["book_observed_at_utc"]=dt.datetime.now(dt.timezone.utc).isoformat()
  self.assertEqual(position_health(p,e)[0],"HARD_INVALIDATION")
 def test_legacy_reset_breakout_without_source_evidence_is_unknown(self):
  state={"reentry_registry":{"X":{"last_exit_price":100,"post_exit_low":100,"reset_seen":False,"state":"POST_EXIT_OBSERVATION"}}}
  c={"asset":"X","signal":{"independent_signal_count":3,"btc_relative_1h_pct":2,"btc_relative_4h_pct":3,"relative_acceleration_pct":1}}
  self.assertFalse(reentry_allowed(state,c,100.5)[0])
  state["reentry_registry"]["X"]["post_exit_low"]=96
  self.assertFalse(reentry_allowed(state,c,101.5)[0])
  self.assertIn('REENTRY_UNKNOWN',reentry_allowed(state,c,101.5)[1])
 def test_market_shock_uses_btc_and_breadth(self):
  scan={"coins":{"BTC":{"change_24h_pct":-3},"A":{"change_24h_pct":-5},"B":{"change_24h_pct":-4},"C":{"change_24h_pct":-1}}}
  self.assertTrue(market_shock(scan)[0])
if __name__=="__main__":unittest.main()


class ObservationIntegrationRegressionTests(unittest.TestCase):
 def test_overfilter_guard_does_not_depend_on_summary_cohorts(self):
  import datetime as dt,tempfile,pathlib
  old=eng.GUARD
  try:
   with tempfile.TemporaryDirectory() as d:
    eng.GUARD=pathlib.Path(d)/"guard.json"
    out=eng.update_overfilter_guard({},{"coins":{}},{"candidates":[]},{},{},dt.datetime(2026,10,5,tzinfo=dt.timezone.utc),0)
    self.assertIn(out["status"],("NORMAL","OVER_FILTERING"))
    self.assertNotIn("opportunity_evaluation",out)
  finally:eng.GUARD=old

class OpportunityObservationTests(unittest.TestCase):
 def test_missing_due_horizon_is_pending_not_zero(self):
  import datetime as dt
  pos={"closed_at_utc":"2026-10-05T00:00:00+00:00","post_exit_tracking":{"max_rebound_from_exit_pct":0},"post_exit_observation":{}}
  eng.refresh_post_exit_status(pos,dt.datetime(2026,10,5,7,tzinfo=dt.timezone.utc))
  self.assertEqual(pos["post_exit_evaluation"]["missing_horizons"],["1h","6h"])
  self.assertIsNone(pos["post_exit_evaluation"]["max_return_from_sell_pct"])

 def test_backfill_rotates_when_budget_limited(self):
  import datetime as dt
  from unittest.mock import patch
  now=dt.datetime(2026,10,5,7,tzinfo=dt.timezone.utc)
  state={"closed_positions":[{"asset":str(i),"opened_at_utc":"2026-10-05T00:00:00+00:00",
   "closed_at_utc":"2026-10-05T01:00:00+00:00","exit_reference_price":1.,
   "tranches":[{"price":1.,"notional_usdt":1000}],"post_exit_observation":{}} for i in range(3)]}
  visited=[]
  def no_bars(asset,start,end,interval="5m"):
   eng._opportunity_backfill_requests+=1;visited.append(asset);return None
  old=eng._opportunity_backfill_requests
  try:
   with patch.object(eng,"OPPORTUNITY_BACKFILL_BUDGET",1),patch.object(eng,"binance_kline_bars",no_bars):
    for _ in range(3):
     eng._opportunity_backfill_requests=0
     eng.refresh_closed_observations(state,now)
   self.assertEqual(visited,["0","1","2"])
  finally:eng._opportunity_backfill_requests=old

 def test_historical_peak_is_authoritative_over_sampled_price(self):
  import datetime as dt
  pos={"closed_at_utc":"2026-10-05T00:00:00+00:00","post_exit_tracking":{"max_rebound_from_exit_pct":1.0},
   "post_exit_observation":{"1h":{"max_return_from_sell_pct":3.0}}}
  eng.refresh_post_exit_status(pos,dt.datetime(2026,10,5,7,tzinfo=dt.timezone.utc))
  self.assertEqual(pos["post_exit_evaluation"]["max_return_from_sell_pct"],3.0)
  self.assertEqual(pos["post_exit_evaluation"]["status"],"BACKFILL_PENDING")

 def test_pending_observation_is_not_compacted_away(self):
  from unittest.mock import patch
  state={"closed_positions":[{"shadow_id":"pending","observation_complete":False},
    {"shadow_id":"complete","observation_complete":True,"post_exit_observation":{"72h":{"max_return_from_sell_pct":3.}}}]}
  with patch.object(eng,"MAX_CLOSED_HOT",1):eng.compact_closed_history(state)
  self.assertEqual([x["shadow_id"] for x in state["closed_positions"]],["pending"])
  self.assertEqual(state["closed_trade_archive"][0]["post_exit_observation"]["72h"]["max_return_from_sell_pct"],3.)

 def test_opportunity_observation_separates_holding_and_full_mfe(self):
  import datetime as dt
  pos={"opened_at_utc":"2026-10-05T00:00:00+00:00","tranches":[{"price":1.0,"notional_usdt":1000.0}],
       "mfe_pct":8.0,"holding_mfe_pct":8.0,"holding_peak_price":1.08,"holding_peak_at_utc":"2026-10-05T01:00:00+00:00",
       "closed_at_utc":"2026-10-05T02:00:00+00:00","exit_reference_price":1.03,"net_return_pct":3.0,
       "full_opportunity_peak_price":1.08,"full_opportunity_mfe_pct":8.0,"post_exit_observation":{}}
  eng.update_post_exit(pos,1.20,dt.datetime(2026,10,5,8,tzinfo=dt.timezone.utc))
  self.assertEqual(pos["holding_mfe_pct"],8.0);self.assertEqual(pos["holding_peak_price"],1.08)
  self.assertEqual(pos["full_opportunity_mfe_pct"],20.0);self.assertEqual(pos["full_opportunity_peak_price"],1.20)

 def test_opportunity_peak_never_decreases_after_exit(self):
  import datetime as dt
  pos={"opened_at_utc":"2026-10-05T00:00:00+00:00","tranches":[{"price":1.0,"notional_usdt":1000.0}],
       "holding_mfe_pct":5.0,"holding_peak_price":1.05,"closed_at_utc":"2026-10-05T01:00:00+00:00",
       "exit_reference_price":1.03,"net_return_pct":2.0,"full_opportunity_peak_price":1.20,"full_opportunity_mfe_pct":20.0,"post_exit_observation":{}}
  eng.update_post_exit(pos,0.90,dt.datetime(2026,10,5,7,tzinfo=dt.timezone.utc))
  self.assertEqual(pos["full_opportunity_peak_price"],1.20);self.assertEqual(pos["full_opportunity_mfe_pct"],20.0)

 def test_capture_ratio_handles_zero_and_losses(self):
  self.assertEqual(eng.capture_ratio(3.0,8.0),0.375);self.assertIsNone(eng.capture_ratio(3.0,0.0));self.assertEqual(eng.capture_ratio(-2.0,10.0),0.0);self.assertEqual(eng.capture_ratio(0.0,10.0),0.0)

 def test_sample_cohort_cutoff(self):
  self.assertEqual(eng.sample_cohort({"opened_at_utc":"2026-10-04T16:59:59+00:00"}),"MIGRATION_SAMPLE")
  self.assertEqual(eng.sample_cohort({"opened_at_utc":"2026-10-04T17:00:00+00:00"}),"NEW_VERSION_SAMPLE")

 def test_backfill_updates_real_peaks_without_trade_decision(self):
  import datetime as dt
  old=eng.binance_kline_bars
  def fake(asset,start,end,interval="5m"):
   base=int(start.timestamp()*1000);closed=int(dt.datetime(2026,10,5,2,tzinfo=dt.timezone.utc).timestamp()*1000)
   return [(base,1.0),(closed-1,1.08),(closed+1,1.05),(closed+3600*1000,1.15),(closed+70*3600*1000,1.20)]
  try:
   eng.binance_kline_bars=fake
   pos={"asset":"ABC","opened_at_utc":"2026-10-05T00:00:00+00:00","closed_at_utc":"2026-10-05T02:00:00+00:00",
        "exit_reference_price":1.05,"net_return_pct":4.0,"tranches":[{"price":1.0,"notional_usdt":1000.0}],"post_exit_observation":{}}
   eng.backfill_opportunity_history(pos,dt.datetime(2026,10,8,3,tzinfo=dt.timezone.utc))
   self.assertEqual(pos["data_provenance"],"HISTORICAL_BACKFILL");self.assertEqual(pos["full_opportunity_mfe_pct"],20.0)
   self.assertEqual(pos["holding_mfe_pct"],8.0);self.assertTrue(pos["observation_complete"]);self.assertIn("72h",pos["post_exit_observation"])
  finally:eng.binance_kline_bars=old

 def test_opportunity_summary_separates_distribution(self):
  rows=[{"holding_mfe_pct":8.0,"full_opportunity_mfe_pct":20.0,"net_return_pct":3.0,"holding_profit_capture_ratio":.375,
         "full_opportunity_capture_ratio":.15,"observation_complete":True,"post_exit_observation":{"72h":{"max_return_from_sell_pct":8.}},"exit_evaluation":"POTENTIAL_PREMATURE_EXIT"}]
  out=eng.opportunity_summary(rows)
  self.assertEqual(out["evaluated_positions"],1);self.assertEqual(out["completed_72h_observations"],1)
  self.assertEqual(out["full_opportunity_mfe_distribution"]["10_20"],1)

 def test_migration_mfe_is_not_inferred_from_weighted_entry_legacy_metric(self):
  import datetime as dt
  pos={"opened_at_utc":"2026-10-04T00:00:00+00:00","tranches":[{"price":1.0,"notional_usdt":1000.0},{"price":0.8,"notional_usdt":1000.0}],
       "mfe_pct":12.0,"holding_mfe_pct":None,"holding_peak_price":None}
  eng.ensure_opportunity_observation(pos,None,dt.datetime(2026,10,5,tzinfo=dt.timezone.utc))
  self.assertIsNone(pos.get("holding_mfe_pct"));self.assertIsNone(pos.get("holding_peak_price"))

 def test_late_monitor_tick_does_not_backfill_1h_with_6h_peak(self):
  import datetime as dt
  pos={"opened_at_utc":"2026-10-05T00:00:00+00:00","tranches":[{"price":1.0,"notional_usdt":1000.0}],
       "holding_mfe_pct":5.0,"holding_peak_price":1.05,"closed_at_utc":"2026-10-05T01:00:00+00:00",
       "exit_reference_price":1.03,"net_return_pct":2.0,"full_opportunity_peak_price":1.20,"full_opportunity_mfe_pct":20.0,"post_exit_observation":{}}
  eng.update_post_exit(pos,1.20,dt.datetime(2026,10,5,7,tzinfo=dt.timezone.utc))
  self.assertNotIn("1h",pos["post_exit_observation"])
  self.assertTrue(any(x.get("status")=="HISTORICAL_BACKFILL_REQUIRED" for x in pos["post_exit_tracking"]["marks"]))

 def test_open_migration_can_be_reconstructed_from_historical_bars(self):
  import datetime as dt
  old=eng.binance_kline_bars
  try:
   eng.binance_kline_bars=lambda asset,start,end,interval="5m":[(int(start.timestamp()*1000),1.0),(int(end.timestamp()*1000),1.25)]
   pos={"asset":"ABC","opened_at_utc":"2026-10-04T00:00:00+00:00","tranches":[{"price":1.0,"notional_usdt":1000.0}]}
   eng.backfill_opportunity_history(pos,dt.datetime(2026,10,5,tzinfo=dt.timezone.utc))
   self.assertEqual(pos["holding_mfe_pct"],25.0);self.assertEqual(pos["full_opportunity_mfe_pct"],25.0)
   self.assertEqual(pos["data_provenance"],"HISTORICAL_BACKFILL");self.assertFalse(pos["observation_complete"])
  finally:eng.binance_kline_bars=old


class OpportunityWindowBoundaryTests(unittest.TestCase):
 def test_tick_after_72h_cannot_raise_full_opportunity_peak(self):
  import datetime as dt
  pos={"opened_at_utc":"2026-10-01T00:00:00+00:00","tranches":[{"price":1.0,"notional_usdt":1000.0}],
       "closed_at_utc":"2026-10-01T01:00:00+00:00","exit_reference_price":1.05,
       "holding_peak_price":1.08,"holding_mfe_pct":8.0,
       "full_opportunity_peak_price":1.20,"full_opportunity_mfe_pct":20.0,"post_exit_observation":{}}
  eng.update_post_exit(pos,1.50,dt.datetime(2026,10,4,2,0,1,tzinfo=dt.timezone.utc))
  self.assertEqual(pos["full_opportunity_peak_price"],1.20)
  self.assertEqual(pos["full_opportunity_mfe_pct"],20.0)

class OpportunityCompletionTruthTests(unittest.TestCase):

 def test_elapsed_72h_without_horizon_does_not_complete(self):
  import datetime as dt
  pos={"opened_at_utc":"2026-10-05T00:00:00+00:00","tranches":[{"price":1.0,"notional_usdt":1000.0}],
       "closed_at_utc":"2026-10-05T01:00:00+00:00","exit_reference_price":1.02,
       "holding_peak_price":1.05,"holding_mfe_pct":5.0,"full_opportunity_peak_price":1.05,
       "full_opportunity_mfe_pct":5.0,"post_exit_observation":{}}
  eng.update_post_exit(pos,1.10,dt.datetime(2026,10,8,2,tzinfo=dt.timezone.utc))
  self.assertFalse(pos["observation_complete"])
  self.assertEqual(pos["exit_evaluation"],"OBSERVING")


class OpportunityCutoffSafetyTests(unittest.TestCase):
 def test_peak_excludes_candle_that_closes_after_cutoff(self):
  import datetime as dt
  cutoff=dt.datetime(2026,10,5,2,0,0,tzinfo=dt.timezone.utc)
  base=int(dt.datetime(2026,10,5,1,55,0,tzinfo=dt.timezone.utc).timestamp()*1000)
  # First candle is fully known before cutoff. Second starts before SELL/cutoff but
  # closes afterwards; its high is future information and must not enter Holding MFE.
  bars=[
   (base,base+4*60*1000+59999,1.08),
   (base+5*60*1000,base+9*60*1000+59999,1.30),
  ]
  out=eng.peak_from_bars(bars,cutoff)
  self.assertEqual(out["peak_price"],1.08)
  self.assertEqual(out["source"],"BINANCE_SPOT_KLINES_5M_COMPLETED_ONLY")

 def test_peak_accepts_completed_candle_at_cutoff(self):
  import datetime as dt
  cutoff=dt.datetime(2026,10,5,2,0,0,tzinfo=dt.timezone.utc)
  stop=int(cutoff.timestamp()*1000)
  bars=[(stop-300000,stop,1.12)]
  self.assertEqual(eng.peak_from_bars(bars,cutoff)["peak_price"],1.12)


class CapitalAllocatorRegressionTests(unittest.TestCase):
 def setUp(self):
  self._tail_snapshot=eng.tail.tail_budget_snapshot
  eng.tail.tail_budget_snapshot=lambda state,cfg,pool:{"effective_tail_cap_usdt":float(pool) if pool is not None else None}
 def tearDown(self):
  eng.tail.tail_budget_snapshot=self._tail_snapshot
 def _state(self,used=0,closed_pnls=()):
  opens=[]
  for i in range(int(used//1000)):
   opens.append({"asset":"P"+str(i),"tranches":[{"price":1.0,"notional_usdt":1000.0}]})
  return {"open_positions":opens,"closed_positions":[{"net_pnl_usdt":x} for x in closed_pnls],"closed_trade_archive":[]}
 def _scan(self,btc,others):
  coins={"BTC":{"change_24h_pct":btc}}
  coins.update({f"C{i}":{"change_24h_pct":v} for i,v in enumerate(others)})
  return {"coins":coins}
 def _edge(self,rr=3.0,r1=2.0,r4=2.0,acc=1.0,score=12):
  return {"estimated_rr":rr,"btc_rel_1h":r1,"btc_rel_4h":r4,"rel_accel":acc,"score":score,"spread_bps":10,"buy_slippage_bps":10}
 def test_risk_off_caps_deployment_at_60pct(self):
  scan=self._scan(-3,[-2,-1,-4,1])
  self.assertEqual(eng.market_regime(scan)[0],"RISK_OFF")
  self.assertTrue(eng.marginal_capital_gate(self._state(11000),1000,self._edge(),"BUY",scan)[0])
  self.assertFalse(eng.marginal_capital_gate(self._state(12000),1000,self._edge(),"BUY",scan)[0])
 def test_neutral_caps_deployment_at_75pct(self):
  scan=self._scan(.5,[1,-1,2,-2])
  self.assertEqual(eng.market_regime(scan)[0],"NEUTRAL")
  self.assertTrue(eng.marginal_capital_gate(self._state(14000),1000,self._edge(),"BUY",scan)[0])
  self.assertFalse(eng.marginal_capital_gate(self._state(15000),1000,self._edge(),"BUY",scan)[0])
 def test_constructive_caps_deployment_at_85pct(self):
  scan=self._scan(2,[1,1,-1,1])
  self.assertEqual(eng.market_regime(scan)[0],"CONSTRUCTIVE")
  self.assertEqual(eng.capital_limit(self._state(),scan),17000.0)
 def test_strong_market_never_exceeds_95pct(self):
  scan=self._scan(4,[2,1,3,1])
  self.assertEqual(eng.market_regime(scan)[0],"STRONG")
  state=self._state(18000);state['open_positions'][-1]['tranches'][0]['strategic_notional_usdt']=1000
  self.assertTrue(eng.marginal_capital_gate(state,1000,self._edge(),"BUY",scan)[0])
  self.assertFalse(eng.marginal_capital_gate(self._state(19000),1000,self._edge(),"BUY",scan)[0])
 def test_buy_and_add_share_same_capacity_rule(self):
  scan=self._scan(.5,[1,-1,2,-2]);state=self._state(15000)
  self.assertFalse(eng.capital_available(state,1000,"BUY",scan))
  self.assertFalse(eng.capital_available(state,1000,"ADD",scan))
 def test_marginal_threshold_tightens_above_75pct(self):
  scan=self._scan(4,[2,1,3,1]);state=self._state(15000)
  ok,reasons=eng.marginal_capital_gate(state,1000,self._edge(rr=2.1,r4=.1,acc=.1),"BUY",scan)
  self.assertFalse(ok);self.assertIn("MARGINAL_EDGE_INSUFFICIENT_ABOVE_75PCT",reasons)
 def test_marginal_threshold_tightens_above_85pct(self):
  scan=self._scan(4,[2,1,3,1]);state=self._state(17000)
  ok,reasons=eng.marginal_capital_gate(state,1000,self._edge(rr=2.5,r1=2,r4=.6,acc=1),"BUY",scan)
  self.assertFalse(ok);self.assertIn("MARGINAL_EDGE_INSUFFICIENT_ABOVE_85PCT",reasons)
 def test_higher_quality_buy_outranks_weaker_add(self):
  high=self._edge(rr=3.2,r1=3,r4=4,acc=2,score=14)
  low=self._edge(rr=2.0,r1=.2,r4=.2,acc=.1,score=8)
  self.assertGreater(eng.opportunity_priority(high,"BUY"),eng.opportunity_priority(low,"ADD"))
 def test_missing_market_data_fails_closed_to_risk_off(self):
  self.assertEqual(eng.market_regime({})[0],"RISK_OFF")
  self.assertEqual(eng.capital_limit(self._state(),{}),12000.0)
 def test_realized_net_profit_compounds_equity_but_keeps_regime_cap(self):
  scan=self._scan(.5,[1,-1,2,-2]);state=self._state(14000,(500.0,))
  snap=eng.capital_snapshot(state,scan)
  self.assertEqual(snap["equity_usdt"],20500.0)
  self.assertEqual(snap["market_regime"],"NEUTRAL")
  self.assertEqual(snap["max_deployable_usdt"],15375.0)
 def test_realized_loss_reduces_equity_and_capacity(self):
  scan=self._scan(.5,[1,-1,2,-2]);state=self._state(14000,(-500.0,))
  snap=eng.capital_snapshot(state,scan)
  self.assertEqual(snap["equity_usdt"],19500.0)
  self.assertEqual(snap["max_deployable_usdt"],14625.0)
 def test_unrealized_pnl_does_not_expand_equity(self):
  scan=self._scan(4,[2,1,3,1]);state=self._state(10000)
  state["open_positions"][0]["unrealized_pnl_usdt"]=99999.0
  self.assertEqual(eng.capital_snapshot(state,scan)["equity_usdt"],20000.0)

 def test_market_capacity_rejection_is_not_mislabeled_tail(self):
  scan=self._scan(-3,[-2,-1,-4,1])
  for kind in ("BUY","ADD"):
   ok,reasons=eng.marginal_capital_gate(self._state(12000),1000,self._edge(),kind,scan)
   self.assertFalse(ok);self.assertIn("MARKET_REGIME_CAPACITY_LIMIT",reasons)
   self.assertNotIn("TAIL_RISK_OR_QUARANTINE_CAPACITY_LIMIT",reasons)
 def test_real_tail_rejection_keeps_tail_reason_for_buy_and_add(self):
  eng.tail.tail_budget_snapshot=lambda state,cfg,pool:{"effective_tail_cap_usdt":4000.0}
  scan=self._scan(4,[2,1,3,1])
  for kind in ("BUY","ADD"):
   ok,reasons=eng.marginal_capital_gate(self._state(4000),1000,self._edge(),kind,scan)
   self.assertTrue(ok);self.assertNotIn("TAIL_RISK_OR_QUARANTINE_CAPACITY_LIMIT",reasons)
 def test_market_and_tail_constraints_keep_both_reasons(self):
  eng.tail.tail_budget_snapshot=lambda state,cfg,pool:{"effective_tail_cap_usdt":4000.0}
  ok,reasons=eng.marginal_capital_gate(self._state(12000),1000,self._edge(),"BUY",self._scan(-3,[-2,-1,-4,1]))
  self.assertFalse(ok);self.assertIn("MARKET_REGIME_CAPACITY_LIMIT",reasons)
  self.assertNotIn("TAIL_RISK_OR_QUARANTINE_CAPACITY_LIMIT",reasons)
 def test_diagnostic_correction_preserves_capacity_boundary(self):
  # Check admission around both independent boundaries including floats; the
  # audit-label correction must never create or remove a capital admission.
  for cap in (None,4000.0,12000.0,20000.0):
   eng.tail.tail_budget_snapshot=lambda state,cfg,pool,cap=cap:{"effective_tail_cap_usdt":cap}
   for btc,others in ((-3,[-2,-1,-4,1]),(4,[2,1,3,1])):
    scan=self._scan(btc,others)
    for used in (0,2999,3000,4000,11000,12000,17000,18000,19000):
     for amount in (1000,1000+1e-10,1000+1e-8):
      state=self._state();state["open_positions"]=[{"asset":"X","tranches":[{"price":1,"notional_usdt":used}]}]
      limit=eng.capital_limit(state,scan);after=(used+amount)/20000
      old_rejected=after>min(eng.market_regime(scan)[1]["max_utilization"],1-eng.HARD_CASH_FLOOR_PCT)+1e-12 or used+amount>limit+1e-9 or used>17000+1e-9
      for kind in ("BUY","ADD"):
       self.assertEqual(eng.marginal_capital_gate(state,amount,self._edge(),kind,scan)[0],not old_rejected)
 def test_deferred_opportunity_keeps_actual_execution_market_snapshot(self):
  import datetime as dt
  state=self._state(19000);state.update({"decisions":[],"events":[],"systemic_risk":{"level":"NORMAL","last_observation_id":"ok"}})
  scan=self._scan(4,[2,1,3,1]);candidate={"asset":"X","trade_action":"BUY","signal":{"score":12,"independent_signal_count":3}}
  proposal={"kind":"BUY","asset":"X","pos":{"asset":"X","tranches":[]},"candidate":candidate,"amount":1000,"price":1,"evidence":self._edge(),"reasons":[]}
  self.assertEqual(eng.execute_capital_proposals(state,[proposal],scan,dt.datetime(2026,10,6,tzinfo=dt.timezone.utc)),0)
  snapshot=state["deferred_buy_opportunities"][0]["capital_snapshot"]
  self.assertEqual(snapshot["market_regime"],"STRONG")
  self.assertEqual(snapshot["market_regime_evidence"]["btc_change_24h_pct"],4)
  self.assertEqual(snapshot["market_regime_evidence"]["negative_breadth"],0)
  self.assertEqual(snapshot["max_deployable_usdt"],19000)
  self.assertEqual(state["events"],[])


class TailRiskIntegrationTests(unittest.TestCase):
 def _normal_systemic(self):
  return {"level":"NORMAL","raw_level":"NORMAL","last_observation_id":"ok","risk_release_fraction":1.0,"recovery_mode":False}
 def test_systemic_liquidity_shock_does_not_create_liquidity_only_hard_exit(self):
  import datetime as dt
  now=dt.datetime.now(dt.timezone.utc);p={}
  e={"score":12,"independent":3,"btc_rel_1h":2,"btc_rel_4h":3,"rel_accel":1,"spread_bps":250,
     "bid_depth_2pct_usdt":1000,"ask_depth_2pct_usdt":1000,"supply_confirmed_major_risk":False,"blockers":[],
     "book_observed_at_utc":now.isoformat(),"signal_evidence":eng.stamp("X","g",now.isoformat())}
  health,reasons=position_health(p,e,now,"HIGH",True)
  self.assertNotEqual(health,"HARD_INVALIDATION")
  self.assertIn("CATASTROPHIC_SPREAD",p["systemic_liquidity_suppressed_reasons"])
 def test_fail_closed_missing_systemic_data_does_not_suppress_single_asset_hard_exit(self):
  import datetime as dt
  now=dt.datetime.now(dt.timezone.utc);p={}
  e={"score":12,"independent":3,"btc_rel_1h":2,"btc_rel_4h":3,"rel_accel":1,"spread_bps":250,
     "bid_depth_2pct_usdt":1000,"ask_depth_2pct_usdt":1000,"supply_confirmed_major_risk":False,"blockers":[],
     "book_observed_at_utc":now.isoformat()}
  self.assertEqual(position_health(p,e,now,"HIGH",False)[0],"HARD_INVALIDATION")
 def test_fatal_identity_still_exits_during_systemic_crash(self):
  import datetime as dt
  now=dt.datetime.now(dt.timezone.utc);p={}
  e={"score":12,"independent":3,"btc_rel_1h":2,"btc_rel_4h":3,"rel_accel":1,"spread_bps":250,
     "bid_depth_2pct_usdt":1000,"ask_depth_2pct_usdt":1000,"supply_confirmed_major_risk":False,
     "blockers":["CONTRACT_MISMATCH"],"book_observed_at_utc":now.isoformat()}
  self.assertEqual(position_health(p,e,now,"CRITICAL")[0],"HARD_INVALIDATION")
 def test_unified_allocator_enforces_tail_budget(self):
  scan={"coins":{"BTC":{"change_24h_pct":4},"A":{"change_24h_pct":2},"B":{"change_24h_pct":2}}}
  state={"open_positions":[{"asset":"P"+str(i),"tranches":[{"price":1,"notional_usdt":1000}]} for i in range(4)],
         "closed_positions":[],"closed_trade_archive":[],"systemic_risk":self._normal_systemic()}
  edge={"estimated_rr":3,"btc_rel_1h":2,"btc_rel_4h":2,"rel_accel":1,"score":12,"spread_bps":10,"buy_slippage_bps":10}
  ok,reasons=eng.marginal_capital_gate(state,1000,edge,"BUY",scan)
  self.assertTrue(ok);self.assertNotIn("TAIL_RISK_OR_QUARANTINE_CAPACITY_LIMIT",reasons)
 def test_realized_profit_does_not_expand_tail_cap(self):
  scan={"coins":{"BTC":{"change_24h_pct":4},"A":{"change_24h_pct":2},"B":{"change_24h_pct":2}}}
  state={"open_positions":[],"closed_positions":[{"net_pnl_usdt":5000}],"closed_trade_archive":[],"systemic_risk":self._normal_systemic()}
  snap=eng.capital_snapshot(state,scan)
  self.assertEqual(snap["equity_usdt"],25000.0)
  self.assertEqual(snap["max_deployable_usdt"],20000.0)
  self.assertEqual(snap['strategic_reserve'],3000)
 def test_reentry_lock_requires_full_risk_recovery(self):
  import datetime as dt
  now=dt.datetime.now(dt.timezone.utc)
  state={"systemic_risk":{"level":"HIGH","recovery_mode":True,"last_observation_id":"shock"},"circuit_breaker":{"status":"TRIPPED"},
         "reentry_registry":{"X":{"last_exit_at_utc":now.isoformat(),"last_exit_price":100,"post_exit_low":95,"reset_seen":True,"risk_lock":True}}}
  c={"asset":"X","signal":{"independent_signal_count":3,"btc_relative_1h_pct":2,"btc_relative_4h_pct":3,"relative_acceleration_pct":1},
     "signal_evidence":eng.stamp("X","new",(now+dt.timedelta(minutes=5)).isoformat())}
  self.assertFalse(reentry_allowed(state,c,102)[0])
