import unittest
from research.hunter_shadow_trader_v2 import decision,weighted_entry,add,profit_protection,REVIEW_HOURS
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
 def test_missing_candidate_exits_existing(self):
  c,s,l,u=self.base();self.assertEqual(decision(None,s,l,u,"HOLD")[0],"EXIT")
 def test_add_needs_lower_price_and_full_revalidation(self):
  c,s,l,u=self.base();p={"tranches":[{"price":100,"notional_usdt":1000}]};self.assertEqual(decision(c,s,l,u,"ADD",p,96)[0],"ADD")
  l["snapshots"]["X"]["ask_depth_2pct_usdt"]=1000;self.assertNotEqual(decision(c,s,l,u,"ADD",p,96)[0],"ADD")
 def test_weighted_cost_falls(self):
  c,s,l,u=self.base();p={"tranches":[{"price":100,"notional_usdt":1000}]};e=decision(c,s,l,u,"ADD",p,96)[2]
  import datetime as dt;add(p,96,e,dt.datetime.now(dt.timezone.utc));self.assertLess(weighted_entry(p),100)
 def test_profit_protection_arms_after_real_mfe(self):
  p={"tranches":[{"price":100,"notional_usdt":1000}],"mfe_pct":3.0}
  self.assertTrue(profit_protection(p,101)["exit"])
 def test_profit_protection_does_not_fake_unprofitable_trade(self):
  p={"tranches":[{"price":100,"notional_usdt":1000}],"mfe_pct":1.5}
  self.assertFalse(profit_protection(p,99)["armed"])
 def test_profit_protection_caps_large_giveback(self):
  p={"tranches":[{"price":100,"notional_usdt":1000}],"mfe_pct":7.0}
  self.assertTrue(profit_protection(p,104)["exit"])
 def test_time_is_review_only(self):self.assertEqual(REVIEW_HOURS,(24.,48.,72.))
 def test_strong_profitable_signal_remains_valid(self):
  c,s,l,u=self.base();self.assertEqual(decision(c,s,l,u,"HOLD")[0],"HOLD")
if __name__=="__main__":unittest.main()
