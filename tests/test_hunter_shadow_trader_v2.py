import unittest
from research.hunter_shadow_trader_v2 import decision,discovery_decision,bybit_channel,market_shock,weighted_entry,add,profit_protection,REVIEW_HOURS
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
 def test_unverified_supply_rejects(self):
  c,s,l,u=self.base();self.assertEqual(decision(c,s,l,{},"ENTRY")[0],"REJECT")
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
 def test_time_is_review_only(self):self.assertEqual(REVIEW_HOURS,(24.,48.,72.))
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
 def test_market_shock_uses_btc_and_breadth(self):
  scan={"coins":{"BTC":{"change_24h_pct":-3},"A":{"change_24h_pct":-5},"B":{"change_24h_pct":-4},"C":{"change_24h_pct":-1}}}
  self.assertTrue(market_shock(scan)[0])
if __name__=="__main__":unittest.main()
