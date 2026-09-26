import datetime as dt
import importlib.util
import pathlib
import unittest

spec=importlib.util.spec_from_file_location(
    "hunter_health_and_queue",
    pathlib.Path(__file__).resolve().parents[1]/"research/hunter_health_and_queue.py")
h=importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
NOW=dt.datetime(2026,9,26,10,tzinfo=dt.timezone.utc)
AT=NOW.isoformat()

def fixture():
    return {"scan":{"as_of_utc":AT,"binance_complete":True,"bybit_complete":False,
                    "errors":{"bybit":"403"},"coins":{"ABC":{}}},
            "research":{"universe_scan_as_of_utc":AT,"deep_research_total_cached":1},
            "identity":{"scan_as_of_utc":AT,"counts":{}},
            "dossiers":{"market_universe_size":1,"unresearched_market_count":0,
                "dossiers":[{"asset":"ABC","opportunity_cohort":"EARLY_FLOW_ATTENTION",
                             "status":"RESEARCH_INCOMPLETE","identity_status":"UNVERIFIED",
                             "capital_gate_blockers":["EVIDENCE_GATED_SCENARIO_UNAVAILABLE"],
                             "official_sources":[],"scenario_map":None,"capital_ready":False}]},
            "liquidity":{"scan_as_of_utc":AT,"requested_count":1,"successful_count":1,
                         "snapshots":{"ABC":{"as_of_utc":AT,"spread_bps":5,
                                            "bid_depth_2pct_usdt":20000,
                                            "ask_depth_2pct_usdt":15000}},
                         "failures":{}},
            "audit":{"market_snapshot_as_of_utc":AT,
                     "outcomes":{"24h":{"research_event_n":0}}}}

class HealthQueueTests(unittest.TestCase):
    def test_successful_infrastructure_does_not_claim_buy(self):
        x=h.build(fixture(),NOW)
        self.assertTrue(x["health"]["snapshot_consistent"])
        self.assertEqual(x["health"]["status"],"RESEARCH_RUNNING_CAPITAL_GATES_UNRESOLVED")
        self.assertEqual(x["health"]["capital_ready"],0)
        self.assertIn("OFFICIAL_TOKENOMICS_AND_VALUE_CAPTURE_MISSING",
                      x["research_priority_queue"][0]["blockers"])
        self.assertEqual(x["health"]["bybit_error"],"403")

    def test_mismatched_scan_is_fatal(self):
        data=fixture();data["identity"]["scan_as_of_utc"]="2026-09-01T00:00:00+00:00"
        x=h.build(data,NOW)
        self.assertEqual(x["health"]["status"],"PIPELINE_INCONSISTENT")
        self.assertIn("identity",x["health"]["snapshot_mismatches"])

    def test_stale_book_and_weak_depth_flagged(self):
        data=fixture()
        data["liquidity"]["snapshots"]["ABC"].update(
            as_of_utc="2026-09-26T08:00:00+00:00",bid_depth_2pct_usdt=100)
        x=h.build(data,NOW)
        self.assertIn("ORDERBOOK_STALE",x["all_candidate_blockers"][0]["blockers"])
        self.assertIn("TWO_SIDED_VISIBLE_DEPTH_LT_10000_USDT",
                      x["all_candidate_blockers"][0]["blockers"])

    def test_early_and_continuation_both_queued(self):
        data=fixture()
        data["scan"]["coins"]["XYZ"]={}
        data["dossiers"]["market_universe_size"]=2
        data["dossiers"]["dossiers"].append({
            "asset":"XYZ","opportunity_cohort":"CONTINUATION_FORWARD_UPSIDE_ATTENTION",
            "status":"RESEARCH_INCOMPLETE","capital_gate_blockers":[],
            "official_sources":[],"scenario_map":None,"capital_ready":False})
        x=h.build(data,NOW)
        self.assertEqual([r["asset"] for r in x["research_priority_queue"]],["ABC","XYZ"])

if __name__=="__main__":unittest.main()
