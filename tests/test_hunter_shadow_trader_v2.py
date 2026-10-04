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
 def test_add_needs_better_price_and_revalidation(self):
  c,s,l,u=self.base();p={"tranches":[{"price":100,"notional_usdt":1000}]};self.assertEqual(decision(c,s,l,u,"ADD",p,99)[0],"ADD")
  self.assertEqual(decision(c,s,l,u,"ADD",p,101)[0],"HOLD")
  c["signal"]["relative_acceleration_pct"]=-2;c["signal"]["btc_relative_1h_pct"]=-1
  self.assertEqual(decision(c,s,l,u,"ADD",p,96)[0],"HOLD")
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
  self.assertEqual(position_health(p,e)[0],"WEAKENING");self.assertEqual(position_health(p,e)[0],"DEGRADED");self.assertEqual(position_health(p,e)[0],"THESIS_INVALIDATED")
 def test_hard_invalidation_is_separate_from_signal_decay(self):
  p={};e={"score":12,"independent":3,"btc_rel_1h":2,"btc_rel_4h":3,"rel_accel":1,"spread_bps":250,"bid_depth_2pct_usdt":50000,"ask_depth_2pct_usdt":50000,"supply_confirmed_major_risk":False,"blockers":[]}
  self.assertEqual(position_health(p,e)[0],"HARD_INVALIDATION")
 def test_reentry_blocks_same_move_and_allows_reset_breakout(self):
  state={"reentry_registry":{"X":{"last_exit_price":100,"post_exit_low":100,"reset_seen":False,"state":"POST_EXIT_OBSERVATION"}}}
  c={"asset":"X","signal":{"independent_signal_count":3,"btc_relative_1h_pct":2,"btc_relative_4h_pct":3,"relative_acceleration_pct":1}}
  self.assertFalse(reentry_allowed(state,c,100.5)[0])
  state["reentry_registry"]["X"]["post_exit_low"]=96
  self.assertTrue(reentry_allowed(state,c,101.5)[0])
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
  self.assertEqual(eng.capture_ratio(3.0,8.0),0.375);self.assertIsNone(eng.capture_ratio(3.0,0.0));self.assertEqual(eng.capture_ratio(-2.0,10.0),-0.2)

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
         "full_opportunity_capture_ratio":.15,"observation_complete":True,"exit_evaluation":"POTENTIAL_PREMATURE_EXIT"}]
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
