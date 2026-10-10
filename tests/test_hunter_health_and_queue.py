import datetime as dt
import importlib.util
import pathlib
import unittest
import copy
import json
import tempfile
from unittest.mock import patch

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
            "dossiers":{"scan_as_of_utc":AT,"market_universe_size":1,"unresearched_market_count":0,
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
    def coverage_fixture(self, available=121, complete=False):
        data=fixture()
        data['scan'].update(required_venues=['binance','bybit'], bybit_complete=True,
                            bybit_signal_complete=complete, complete=complete,
                            coverage_status=('BINANCE_BYBIT_COMPLETE' if complete else
                                             'BINANCE_BYBIT_SIGNALS_INCOMPLETE'),
                            venue_status={'bybit': {'active_pairs':375, 'valid_pairs':375,
                                'signal_expected_bases':['ASSET'+str(i) for i in range(141)],
                                'signal_pairs':available, 'signal_failures':141-available}})
        return data

    def test_complete_tickers_do_not_hide_twenty_missing_signals(self):
        data=self.coverage_fixture()
        health=h.build(data,NOW)['health']
        self.assertEqual(health.get('data_status'),'PARTIAL_DATA')
        self.assertTrue(health['bybit_complete'])
        self.assertEqual(health['exchange_scope'],'BINANCE_BYBIT')
        self.assertIs(health['bybit_signal_complete'],False)
        self.assertIs(health['scan_complete'],False)
        self.assertEqual(health['coverage_status'],'BINANCE_BYBIT_SIGNALS_INCOMPLETE')
        self.assertEqual((health['bybit_signal_expected'],health['bybit_signal_available'],
                          health['bybit_signal_missing'],health['bybit_signal_failures']),
                         (141,121,20,20))

    def test_full_signal_coverage_is_explicit(self):
        health=h.build(self.coverage_fixture(141,True),NOW)['health']
        self.assertEqual(health.get('data_status'),'COMPLETE')
        self.assertIs(health['bybit_signal_complete'],True)
        self.assertEqual(health['bybit_signal_missing'],0)

    def test_legacy_coverage_is_unknown_not_zero_or_complete(self):
        health=h.build(fixture(),NOW)['health']
        self.assertEqual(health.get('data_status'),'UNKNOWN')
        self.assertEqual(health['coverage_status'],'UNKNOWN')
        for key in ('scan_complete','bybit_signal_complete','bybit_signal_expected',
                    'bybit_signal_available','bybit_signal_missing','bybit_signal_failures'):
            self.assertIsNone(health[key])

    def test_benchmark_failure_count_is_not_missing_signal_count(self):
        data=self.coverage_fixture(0)
        data['scan']['venue_status']['bybit']['signal_failures']=142
        health=h.build(data,NOW)['health']
        self.assertEqual(health.get('bybit_signal_missing'),141)
        self.assertEqual(health['bybit_signal_failures'],142)

    def test_invalid_optional_counts_remain_unknown(self):
        data=self.coverage_fixture()
        data['scan']['venue_status']['bybit'].update(signal_pairs=True,
            signal_failures=-1,signal_expected_bases=['DUP','DUP'])
        health=h.build(data,NOW)['health']
        self.assertEqual(health.get('data_status'),'PARTIAL_DATA')
        for key in ('bybit_signal_expected','bybit_signal_available',
                    'bybit_signal_missing','bybit_signal_failures'):
            self.assertIsNone(health[key])

    def test_coverage_is_observation_not_a_capital_gate(self):
        data=self.coverage_fixture()
        original=copy.deepcopy(data)
        partial=h.build(data,NOW)
        full=h.build(self.coverage_fixture(141,True),NOW)
        self.assertEqual(data,original)
        for key in ('research_priority_queue','all_candidate_blockers','capital_authority'):
            self.assertEqual(partial[key],full[key])
        for key in ('status','capital_ready','snapshot_consistent','snapshot_mismatches'):
            self.assertEqual(partial['health'][key],full['health'][key])

    def test_partial_data_main_returns_success_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder)
            for key,filename in h.PATHS.items():
                (root/filename).write_text(json.dumps(self.coverage_fixture()[key]))
            original_build=h.build
            with patch.object(h,'ROOT',root),patch.object(h,'OUT',root/'health.json'), \
                 patch.object(h,'build',side_effect=lambda data,now:original_build(data,NOW)), \
                 patch('builtins.print'):
                self.assertEqual(h.main(),0)
            self.assertEqual(json.loads((root/'health.json').read_text())['health'].get('data_status'),
                             'PARTIAL_DATA')

    def test_successful_infrastructure_does_not_claim_buy(self):
        x=h.build(fixture(),NOW)
        self.assertTrue(x["health"]["snapshot_consistent"])
        self.assertEqual(x["health"]["status"],"RESEARCH_RUNNING_CAPITAL_GATES_UNRESOLVED")
        self.assertEqual(x["health"]["capital_ready"],0)
        self.assertIn("OFFICIAL_CONTRACT_OR_PROJECT_SOURCE_MISSING",
                      x["research_priority_queue"][0]["blockers"])
        self.assertEqual(x["health"]["exchange_scope"],"BINANCE_ONLY_BYBIT_UNAVAILABLE")
        self.assertEqual(x["health"]["required_venues"],["binance"])
        self.assertNotIn("bybit_error",x["health"])
        self.assertFalse(x["health"]["bybit_complete"])

    def test_contract_only_is_not_full_fundamental_review(self):
        data=fixture()
        data["identity"]["counts"]={"THIRD_PARTY_CORROBORATED":1}
        dossier=data["dossiers"]["dossiers"][0]
        dossier["official_sources"]=["https://project.example/contract"]
        dossier["identity_status"]="THIRD_PARTY_CORROBORATED"
        x=h.build(data,NOW)
        self.assertEqual(x["health"]["market_data_screened_cached"],1)
        self.assertEqual(x["health"]["official_contract_identity_verified"],1)
        self.assertEqual(x["health"]["forward_economic_scenario_ready"],0)
        self.assertTrue(x["research_priority_queue"][0]["needs_human_primary_source_review"])
        self.assertIn("OFFICIAL_SUPPLY_VALUE_CAPTURE_AND_SCENARIO_REVIEW_PENDING",
                      x["research_priority_queue"][0]["blockers"])

    def test_reviewed_supply_risk_is_visible_in_priority_queue(self):
        data=fixture()
        data["dossiers"]["dossiers"][0]["reviewed_evidence"]={
            "review_status":"MATERIAL_UNLOCK",
            "review_action":"WAIT_FOR_UNLOCK_NOT_BUY",
            "risk_event":{"status":"PENDING_ONCHAIN_RECONCILIATION"}}
        report=h.build(data,NOW)
        self.assertEqual(report["health"]["reviewed_candidates_retained"],1)
        self.assertEqual(report["health"]["unresolved_material_supply_events"],1)
        self.assertEqual(report["research_priority_queue"][0]["review_action"],
                         "WAIT_FOR_UNLOCK_NOT_BUY")

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
