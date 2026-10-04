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
   saved=m.load(p);self.assertEqual([x["asset"] for x in saved["open_positions"]],["X"]);self.assertEqual(len(saved["open_positions"][0]["tranches"]),2);self.assertEqual(out["added"],["X"])
 def test_monitor_has_no_new_entry_path(self):
  self.assertFalse(hasattr(m,"entry_allowed"));self.assertFalse(hasattr(m,"discovery_decision"))
if __name__=="__main__":unittest.main()
