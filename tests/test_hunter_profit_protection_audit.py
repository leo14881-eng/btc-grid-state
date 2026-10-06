import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research import hunter_shadow_trader_v2 as eng
from research import hunter_position_monitor as monitor


class ProfitProtectionAuditTests(unittest.TestCase):
 def run_position(self, price, mfe=3.2054, health="THESIS_INVALIDATED"):
  now=dt.datetime(2026,10,6,8,tzinfo=dt.timezone.utc)
  pos={"asset":"ENA","shadow_id":"audit-ena","opened_at_utc":"2026-10-05T07:51:47+00:00",
       "tranches":[{"price":0.2527,"notional_usdt":1000,"buy_slippage_bps":5.313}],"mfe_pct":mfe,"mae_pct":0}
  state={"open_positions":[pos],"events":[],"decisions":[],"closed_positions":[]}
  scan={"generation_id":"audit-generation","coins":{"ENA":{"reference_price":price}}}
  with patch.object(eng,"ensure_opportunity_observation"),patch.object(eng,"decision",return_value=("HOLD",[],{})),patch.object(eng,"position_health",return_value=(health,[])),patch.object(eng,"refresh_post_exit_status"),patch.object(eng,"exit_analysis",return_value={}):
   eng.manage_existing_positions(state,scan,{}, {},{},now)
  return state,pos

 def test_ena_missed_window_is_persisted_without_fabricating_sell(self):
  state,pos=self.run_position(0.2533)
  self.assertEqual(state["open_positions"],[pos])
  self.assertEqual(state["closed_positions"],[])
  self.assertEqual(state["events"],[])
  blocked=[x for x in state["decisions"] if "PROFIT_PROTECTION_BLOCKED_NET_NONPOSITIVE" in x["reasons"]]
  self.assertEqual(len(blocked),1)
  audit=blocked[0]["evidence"]["profit_protection_review"]
  self.assertTrue(audit["armed"])
  self.assertTrue(audit["exit"])
  self.assertLess(audit["net_pnl_usdt"],0)
  self.assertAlmostEqual(audit["net_pnl_usdt"],eng.net_pnl(pos,0.2533))
  self.assertEqual(audit["generation_id"],"audit-generation")
  self.assertEqual(audit["fee_bps_per_side"],eng.FEE_BPS)
  self.assertEqual(audit["tranche_cost_inputs"][0]["buy_slippage_bps"],5.313)
  self.assertFalse(audit["sell_spread_and_slippage_modelled"])
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/"portfolio.json";eng.atomic_json_write(path,state)
   saved=json.loads(path.read_text())
  self.assertEqual(saved["open_positions"][0]["profit_protection_review"],audit)
  self.assertIn("LOSS_RECOVERY",[r for x in saved["decisions"] for r in x["reasons"]])

 def test_profitable_giveback_still_sells_with_same_pnl(self):
  state,pos=self.run_position(0.2555)
  self.assertEqual(state["open_positions"],[])
  self.assertEqual(state["closed_positions"],[pos])
  self.assertEqual(pos["exit_reason"],"PROFIT_PROTECTION")
  self.assertAlmostEqual(pos["net_pnl_usdt"],round(eng.net_pnl(pos,0.2555),2))
  self.assertEqual([x["type"] for x in state["events"]],["SHADOW_V2_SELL"])
  self.assertFalse(any("PROFIT_PROTECTION_BLOCKED_NET_NONPOSITIVE" in x["reasons"] for x in state["decisions"]))

 def test_monitor_persists_its_admitted_run_generation(self):
  state,pos=self.run_position(0.2533)
  now=dt.datetime(2026,10,6,8,5,tzinfo=dt.timezone.utc)
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/"v2.json";eng.atomic_json_write(path,state)
   with patch.object(eng,"ensure_opportunity_observation"),patch.object(eng,"decision",return_value=("HOLD",[],{})),patch.object(eng,"position_health",return_value=("THESIS_INVALIDATED",[])),patch.object(eng,"refresh_closed_observations"),patch.object(eng,"build_summary",return_value={}):
    monitor.run_lane(path,"SHADOW_V2",{"ENA":{"reference_price":0.2533}}, {},{}, {},now,False,regime_scan={"generation_id":"MONITOR_ACTUAL_EVIDENCE_REFRESH"})
   saved=json.loads(path.read_text())
  self.assertEqual(saved["open_positions"][0]["profit_protection_review"]["generation_id"],"MONITOR_ACTUAL_EVIDENCE_REFRESH")
  self.assertEqual(saved["events"],[])

 def test_unarmed_loss_does_not_claim_profit_protection_trigger(self):
  state,pos=self.run_position(0.24,mfe=1)
  self.assertNotIn("profit_protection_review",pos)
  self.assertFalse(any("PROFIT_PROTECTION_BLOCKED_NET_NONPOSITIVE" in x["reasons"] for x in state["decisions"]))
  self.assertEqual(state["events"],[])

 def test_hard_invalidation_is_not_misreported_as_blocked_sell(self):
  state,pos=self.run_position(0.24,health="HARD_INVALIDATION")
  self.assertEqual(pos["exit_reason"],"HARD_INVALIDATION")
  self.assertNotIn("profit_protection_review",pos)
  self.assertFalse(any("PROFIT_PROTECTION_BLOCKED_NET_NONPOSITIVE" in x["reasons"] for x in state["decisions"]))


if __name__=="__main__":unittest.main()
