import os
import unittest
import research.hunter_shadow_trader as v1
import research.hunter_shadow_trader_v2 as core

class SharedShadowEngineTests(unittest.TestCase):
 def tearDown(self):
  core.CAPITAL_POOL_USDT=20000.
  core.ENTRY_MODE="EXECUTABLE"
  core.DISCOVERY_MIN_SCORE=6.

 def test_v1_uses_shared_management_engine(self):
  self.assertIs(v1.engine,core)

 def test_v1_is_discovery_entry_and_unlimited_capital(self):
  v1.configure_v1()
  self.assertEqual(core.ENTRY_MODE,"DISCOVERY")
  self.assertIsNone(core.CAPITAL_POOL_USDT)
  self.assertTrue(core.entry_allowed("DISCOVERY","BUY",1.0,"REJECT"))

 def test_v1_early_membership_does_not_reapply_score_six_gate(self):
  v1.configure_v1()
  c={"asset":"LOW","signal":{"score":1.25,"independent_signal_count":2},"blockers":["OFFICIAL_ASSET_IDENTITY_UNVERIFIED"]}
  self.assertEqual(core.DISCOVERY_MIN_SCORE,0.)
  self.assertEqual(core.discovery_decision(c)[0],"BUY")

 def test_v1_still_rejects_explicit_identity_mismatch(self):
  v1.configure_v1()
  c={"asset":"BAD","signal":{"score":1.25,"independent_signal_count":2},"blockers":["OFFICIAL_ASSET_IDENTITY_MISMATCH"]}
  self.assertEqual(core.discovery_decision(c)[0],"REJECT")

 def test_v2_requires_executable_buy(self):
  self.assertFalse(core.entry_allowed("EXECUTABLE","BUY",1.0,"REJECT"))
  self.assertTrue(core.entry_allowed("EXECUTABLE","BUY",1.0,"BUY"))
  self.assertFalse(core.entry_allowed("EXECUTABLE","REJECT",1.0,"BUY"))
  self.assertFalse(core.entry_allowed("EXECUTABLE","BUY",None,"BUY"))

 def test_executable_gate_requires_trade_quality(self):
  c={"asset":"X","signal":{"score":12,"independent_signal_count":3,"btc_relative_1h_pct":2,"btc_relative_4h_pct":3,"relative_acceleration_pct":1},
     "execution_scenario":{"buy_slippage_bps":10,"estimated_rr":2},"blockers":[]}
  scan={"coins":{"X":{"reference_price":96,"change_24h_pct":5}}}
  liq={"snapshots":{"X":{"spread_bps":10,"bid_depth_2pct_usdt":50000,"ask_depth_2pct_usdt":50000}}}
  supply={"assets":{"X":{"tactical_supply_risk_verified":True,"status":"FULLY_UNLOCKED"}}}
  self.assertEqual(core.discovery_decision(c)[0],"BUY")
  self.assertEqual(core.decision(c,scan,liq,supply,"ENTRY")[0],"BUY")
  self.assertEqual(core.decision(c,scan,{},supply,"ENTRY")[0],"REJECT")

 def test_v2_capital_pool_blocks_overcommit(self):
  state={"open_positions":[{"tranches":[{"price":1,"notional_usdt":20000}]}]}
  self.assertFalse(core.capital_available(state,1000,"BUY"))
  self.assertFalse(core.capital_available(state,1000,"ADD"))
  state={"open_positions":[{"tranches":[{"price":1,"notional_usdt":19000}]}]}
  self.assertFalse(core.capital_available(state,1000,"BUY"))
  self.assertTrue(core.capital_available(state,1000,"ADD"))

 def test_freeze_is_fail_closed_by_default(self):
  self.assertTrue(core.SHADOW_FREEZE)

 def test_no_fixed_time_or_price_stop(self):
  self.assertEqual(core.REVIEW_HOURS,(1.,6.,24.,48.,72.))
  self.assertFalse(hasattr(core,"INVALIDATION_PCT"))

if __name__=="__main__":unittest.main()
