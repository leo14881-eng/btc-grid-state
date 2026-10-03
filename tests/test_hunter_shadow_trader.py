import unittest
import research.hunter_shadow_trader as v1
import research.hunter_shadow_trader_v2 as core

class SharedShadowEngineTests(unittest.TestCase):
 def test_v1_uses_exact_v2_engine(self):
  self.assertIs(v1.engine,core)

 def test_v1_only_removes_capital_pool_constraint(self):
  self.assertEqual(core.CAPITAL_POOL_USDT,20000.)
  v1.configure_v1()
  self.assertIsNone(core.CAPITAL_POOL_USDT)
  core.CAPITAL_POOL_USDT=20000.

 def test_shared_entry_gate_requires_trade_quality(self):
  c={"asset":"X","signal":{"score":12,"independent_signal_count":3,"btc_relative_1h_pct":2,"btc_relative_4h_pct":3,"relative_acceleration_pct":1},
     "execution_scenario":{"buy_slippage_bps":10,"estimated_rr":2},"blockers":[]}
  scan={"coins":{"X":{"reference_price":96,"change_24h_pct":5}}}
  liq={"snapshots":{"X":{"spread_bps":10,"bid_depth_2pct_usdt":50000,"ask_depth_2pct_usdt":50000}}}
  supply={"assets":{"X":{"tactical_supply_risk_verified":True,"status":"FULLY_UNLOCKED"}}}
  self.assertEqual(core.decision(c,scan,liq,supply,"ENTRY")[0],"BUY")
  self.assertEqual(core.decision(c,scan,{},supply,"ENTRY")[0],"REJECT")

 def test_v2_capital_pool_blocks_overcommit(self):
  state={"open_positions":[{"tranches":[{"price":1,"notional_usdt":20000}]}]}
  self.assertFalse(core.capital_available(state,1000))
  state={"open_positions":[{"tranches":[{"price":1,"notional_usdt":19000}]}]}
  self.assertTrue(core.capital_available(state,1000))

 def test_no_fixed_time_or_price_stop(self):
  self.assertEqual(core.REVIEW_HOURS,(1.,6.,24.,48.,72.))
  self.assertFalse(hasattr(core,"INVALIDATION_PCT"))

if __name__=="__main__":unittest.main()
