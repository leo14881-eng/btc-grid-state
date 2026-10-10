import datetime as dt,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from research import hunter_position_monitor as m
class PositionMonitorTests(unittest.TestCase):
 def test_v2_health_is_refreshed_every_bucket_without_v1_discovery_change(self):
  states=[{'open_positions':[{'asset':str(i)} for i in range(200)]},{'open_positions':[{'asset':'ENA'},{'asset':'PENDLE'}]}]
  cursor=0;rotated=set()
  for _ in range(4):
   selected,cursor=m.management_selection(states,cursor,80)
   self.assertEqual(len(selected),80)
   self.assertIn('ENA',selected);self.assertIn('PENDLE',selected)
   rotated.update(set(selected)-{'ENA','PENDLE'})
  self.assertEqual(len(rotated),200)

 def test_assets_deduplicates_v1_v2(self):
  s=[{"open_positions":[{"asset":"A"},{"asset":"B"}]},{"open_positions":[{"asset":"A"}]}]
  self.assertEqual(m.assets(s),["A","B"])
 def fixture(self,price=90):
  state={"mode":"SIMULATION_ONLY_NO_REAL_ORDERS","open_positions":[{"shadow_id":"X1","asset":"X","opened_at_utc":"2026-10-01T00:00:00+00:00","btc_entry_price":100,"tranches":[{"price":100,"notional_usdt":1000,"buy_slippage_bps":0,"at":"2026-10-03T23:30:00+00:00","signal_evidence_id":"old-evidence","signal_generation_id":"old-generation"}],"mfe_pct":0,"mae_pct":0}],"closed_positions":[],"events":[],"decisions":[],"systemic_risk":{"level":"NORMAL","raw_level":"NORMAL","last_observation_id":"fixture-risk","risk_release_fraction":1.0,"recovery_mode":False}}
  market={"BTC":{"reference_price":100,"change_24h_pct":0},"X":{"reference_price":price,"change_24h_pct":0}}
  review={"candidates":[{"asset":"X","signal":{"score":12,"independent_signal_count":3,"btc_relative_1h_pct":2,"btc_relative_4h_pct":3,"relative_acceleration_pct":1,"return_1h_pct":3},"execution_scenario":{"buy_slippage_bps":10,"estimated_rr":2},"blockers":[]}]}
  review["candidates"][0]["signal_evidence"]=m.eng.stamp("X","MONITOR_20261004T000000000000Z","2026-10-04T00:00:00+00:00")
  liq={"snapshots":{"X":{"as_of_utc":"2026-10-04T00:00:00+00:00","spread_bps":10,"bid_depth_2pct_usdt":50000,"ask_depth_2pct_usdt":50000}}};supply={}
  return state,market,review,liq,supply
 def test_shared_manager_can_add_but_never_create_new_asset(self):
  state,market,review,liq,supply=self.fixture()
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"s.json";m.eng.atomic_json_write(p,state)
   with patch.object(m.v1,"configure_v1",lambda:None),patch.object(m.eng,'ENTRY_MODE','DISCOVERY'):
    out=m.run_lane(p,"V1",market,review,liq,supply,dt.datetime(2026,10,4,tzinfo=dt.timezone.utc),True)
   saved=m.load(p);summary=m.load(p.with_name(p.stem+"-summary.json"));self.assertEqual([x["asset"] for x in saved["open_positions"]],["X"]);self.assertEqual(len(saved["open_positions"][0]["tranches"]),2);self.assertEqual(out["added"],["X"]);self.assertEqual(summary["open_positions"],1);self.assertEqual(summary["closed_positions"],0)
 def test_v2_monitor_defers_add_to_full_capital_allocator(self):
  state,market,review,liq,supply=self.fixture()
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"v2.json";m.eng.atomic_json_write(p,state)
   out=m.run_lane(p,"SHADOW_V2",market,review,liq,supply,dt.datetime(2026,10,4,tzinfo=dt.timezone.utc),False)
   saved=m.load(p)
   self.assertEqual(len(saved["open_positions"][0]["tranches"]),1)
   self.assertEqual(out["added"],[])
   self.assertEqual(out["deferred_adds"],["X"])
   self.assertTrue(any("CAPITAL_ALLOCATION_PENDING" in (x.get("reasons") or []) for x in saved.get("decisions") or []))

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

class ClosedOpportunityMonitorTests(unittest.TestCase):
 def test_assets_includes_incomplete_closed_observation(self):
  states=[{"open_positions":[{"asset":"OPEN"}],"closed_positions":[
   {"asset":"CLOSED_PENDING","closed_at_utc":"2026-10-05T00:00:00+00:00","observation_complete":False},
   {"asset":"CLOSED_DONE","closed_at_utc":"2026-10-04T00:00:00+00:00","observation_complete":True}]}]
  self.assertEqual(m.assets(states),["CLOSED_PENDING","OPEN"])

 def test_closed_observation_is_updated_without_creating_trade(self):
  state,market,review,liq,supply=PositionMonitorTests().fixture(price=120)
  pos=state["open_positions"].pop()
  pos.update({"closed_at_utc":"2026-10-05T01:00:00+00:00","exit_reference_price":103.0,
              "net_return_pct":2.0,"holding_mfe_pct":5.0,"holding_peak_price":105.0,
              "full_opportunity_peak_price":105.0,"full_opportunity_mfe_pct":5.0,
              "post_exit_observation":{},"observation_complete":False})
  state["closed_positions"]=[pos]
  now=dt.datetime(2026,10,5,7,tzinfo=dt.timezone.utc)
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"s.json";m.eng.atomic_json_write(p,state)
   with patch.object(m.v1,"configure_v1",lambda:None), patch.object(m.eng,"backfill_opportunity_history",lambda pos,now:pos):
    out=m.run_lane(p,"V1",market,review,liq,supply,now,True)
   saved=m.load(p);closed=saved["closed_positions"][0]
   self.assertEqual(out["open"],0)
   self.assertEqual(closed["holding_peak_price"],105.0)
   self.assertEqual(closed["full_opportunity_peak_price"],120)
   self.assertEqual(closed["full_opportunity_mfe_pct"],20.0)
   self.assertFalse(closed["observation_complete"])
   self.assertEqual(saved.get("events",[]),[])

if __name__=="__main__":unittest.main()
