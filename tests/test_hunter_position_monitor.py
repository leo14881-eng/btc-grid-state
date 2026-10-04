import datetime as dt,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from research import hunter_position_monitor as m
class PositionMonitorTests(unittest.TestCase):
 def test_assets_deduplicates_v1_v2(self):
  s=[{"open_positions":[{"asset":"A"},{"asset":"B"}]},{"open_positions":[{"asset":"A"}]}]
  self.assertEqual(m.assets(s),["A","B"])
 def fixture(self,price=90):
  state={"mode":"SIMULATION_ONLY_NO_REAL_ORDERS","open_positions":[{"shadow_id":"X1","asset":"X","opened_at_utc":"2026-10-01T00:00:00+00:00","btc_entry_price":100,"tranches":[{"price":100,"notional_usdt":1000,"buy_slippage_bps":0}],"mfe_pct":0,"mae_pct":0}],"closed_positions":[],"events":[],"decisions":[]}
  market={"BTC":{"reference_price":100,"change_24h_pct":0},"X":{"reference_price":price,"change_24h_pct":0}}
  review={"candidates":[{"asset":"X","signal":{"score":12,"independent_signal_count":3,"btc_relative_1h_pct":2,"btc_relative_4h_pct":3,"relative_acceleration_pct":1},"execution_scenario":{"buy_slippage_bps":10,"estimated_rr":2},"blockers":[]}]}
  liq={"snapshots":{"X":{"spread_bps":10,"bid_depth_2pct_usdt":50000,"ask_depth_2pct_usdt":50000}}};supply={}
  return state,market,review,liq,supply
 def test_shared_manager_can_add_but_never_create_new_asset(self):
  state,market,review,liq,supply=self.fixture()
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"s.json";m.eng.atomic_json_write(p,state)
   with patch.object(m.v1,"configure_v1",lambda:None):
    out=m.run_lane(p,"V1",market,review,liq,supply,dt.datetime(2026,10,4,tzinfo=dt.timezone.utc),True)
   saved=m.load(p);summary=m.load(p.with_name(p.stem+"-summary.json"));self.assertEqual([x["asset"] for x in saved["open_positions"]],["X"]);self.assertEqual(len(saved["open_positions"][0]["tranches"]),2);self.assertEqual(out["added"],["X"]);self.assertEqual(summary["open_positions"],1);self.assertEqual(summary["closed_positions"],0)
 def test_bstock_is_quarantined_not_sold_or_managed(self):
  state,market,review,liq,supply=self.fixture()
  state["open_positions"][0]["asset"]="MSFTB";market["MSFTB"]=market.pop("X")
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"s.json";m.eng.atomic_json_write(p,state)
   with patch.object(m.v1,"configure_v1",lambda:None):
    out=m.run_lane(p,"V1",market,review,liq,supply,dt.datetime(2026,10,4,tzinfo=dt.timezone.utc),True,{"MSFTB"})
   saved=m.load(p)
   self.assertEqual(saved["open_positions"],[])
   self.assertEqual(saved["excluded_non_crypto_positions"][0]["asset"],"MSFTB")
   self.assertEqual(out["quarantined_non_crypto"],["MSFTB"])
   self.assertEqual(saved["closed_positions"],[])

 def test_monitor_compacts_decision_history(self):
  state,market,review,liq,supply=self.fixture()
  state["decisions"]=[{"asset":"X","action":"HOLD","n":i} for i in range(m.eng.MAX_DECISION_HISTORY)]
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"s.json";m.eng.atomic_json_write(p,state)
   with patch.object(m.v1,"configure_v1",lambda:None):
    m.run_lane(p,"V1",market,review,liq,supply,dt.datetime(2026,10,4,tzinfo=dt.timezone.utc),True)
   saved=m.load(p)
   self.assertEqual(len(saved["decisions"]),m.eng.MAX_DECISION_HISTORY)
   self.assertGreaterEqual(saved.get("decision_history_truncated",0),1)

 def test_bstock_history_is_quarantined_without_fake_sell(self):
  state,market,review,liq,supply=self.fixture()
  state["open_positions"]=[];state["decisions"]=[{"asset":"MSFTB","action":"REJECT"}];state["events"]=[{"asset":"MSFTB","type":"SHADOW_V2_BUY"}];state["ever_entered_assets"]=["MSFTB"]
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"s.json";m.eng.atomic_json_write(p,state)
   with patch.object(m.v1,"configure_v1",lambda:None):
    out=m.run_lane(p,"V1",market,review,liq,supply,dt.datetime(2026,10,4,tzinfo=dt.timezone.utc),True,{"MSFTB"})
   saved=m.load(p)
   self.assertEqual(saved["decisions"],[]);self.assertEqual(saved["events"],[]);self.assertEqual(saved["ever_entered_assets"],[])
   self.assertEqual(saved["excluded_non_crypto_decisions"][0]["asset"],"MSFTB")
   self.assertEqual(saved["excluded_non_crypto_events"][0]["type"],"SHADOW_V2_BUY")
   self.assertEqual(out["quarantined_non_crypto"],["MSFTB"])
   self.assertFalse(any(x.get("type","").endswith("_SELL") for x in saved.get("events") or []))

 def test_v2_configuration_resets_v1_mutated_globals(self):
  old=(m.eng.DISCOVERY_MIN_SCORE,m.eng.DISCOVERY_MIN_INDEPENDENT,m.eng.ENTRY_MODE,m.eng.SUMMARY,m.eng.GUARD)
  try:
   m.eng.DISCOVERY_MIN_SCORE=0;m.eng.DISCOVERY_MIN_INDEPENDENT=0;m.eng.ENTRY_MODE="DISCOVERY"
   m.configure_lane(False)
   self.assertEqual(m.eng.DISCOVERY_MIN_SCORE,6)
   self.assertEqual(m.eng.DISCOVERY_MIN_INDEPENDENT,2)
   self.assertEqual(m.eng.ENTRY_MODE,"EXECUTABLE")
   self.assertEqual(m.eng.SUMMARY,m.V2_SUMMARY)
   self.assertEqual(m.eng.GUARD,m.V2_GUARD)
  finally:
   m.eng.DISCOVERY_MIN_SCORE,m.eng.DISCOVERY_MIN_INDEPENDENT,m.eng.ENTRY_MODE,m.eng.SUMMARY,m.eng.GUARD=old

 def test_monitor_has_no_new_entry_path(self):
  self.assertFalse(hasattr(m,"entry_allowed"));self.assertFalse(hasattr(m,"discovery_decision"))
if __name__=="__main__":unittest.main()
