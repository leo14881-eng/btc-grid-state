import datetime as dt
import importlib.util
import pathlib
import unittest

FILE=pathlib.Path(__file__).resolve().parents[1]/"research/hunter_candidate_dossiers.py"
SPEC=importlib.util.spec_from_file_location("hunter_candidate_dossiers",FILE)
d=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)
NOW=dt.datetime(2026,9,26,9,tzinfo=dt.timezone.utc)
AT=NOW.isoformat()

def fixtures():
    def coin(s,price):
        return {"reference_price":price,"reference_venue":"binance"}
    scan={"binance_complete":True,"as_of_utc":AT,
          "coins":{"BTC":coin("BTC",80000),"RALLY":coin("RALLY",2),
                   "DOWN":coin("DOWN",.1)}}
    research={"universe_scan_as_of_utc":AT,"as_of_utc":AT,
              "triggered_researched":["RALLY"],
              "research_results":{
                "RALLY":{"researched_at_utc":AT,
                    "research_attention_signals":["7D_VOLUME_EXPANSION"],
                    "missing_facts":["unlock unknown"],
                    "observations":{"market_cap":1000000}},
                "DOWN":{"researched_at_utc":AT,
                    "research_attention_signals":[],
                    "missing_facts":["capture unknown"],
                    "observations":{"market_cap":1000000}}}}
    return research,scan

class DossierTests(unittest.TestCase):
    def test_prior_rally_is_not_auto_excluded(self):
        r,s=fixtures()
        report=d.build(r,s,{"assets":{}},NOW)
        self.assertEqual(report["dossier_count"],2)
        self.assertEqual(report["dossiers"][0]["asset"],"RALLY")
        self.assertTrue(report["dossiers"][0]["prior_rally_never_auto_rejects"])
        self.assertEqual(report["capital_ready"],[])

    def test_missing_primary_facts_never_generate_target(self):
        result,missing=d.scenario_map({"bear_market_cap_usd":10},2,NOW)
        self.assertIsNone(result)
        self.assertTrue(any("contract_verified" in x for x in missing))

    def test_verified_hypothetical_supply_dilution_arithmetic(self):
        facts={"verified_at_utc":AT,"official_sources":["https://example.org/official"],
               "contract_verified":True,"forward_supply_verified":True,
               "token_value_capture_verified":True,"credible_catalyst_verified":True,
               "supply_future":200,"bear_market_cap_usd":100,
               "base_market_cap_usd":400,"bull_market_cap_usd":1000}
        result,missing=d.scenario_map(facts,2,NOW)
        self.assertEqual(missing,[])
        self.assertEqual(result["bear_price"],.5)
        self.assertEqual(result["base_price"],2)
        self.assertEqual(result["bull_price"],5)
        self.assertEqual(result["bull_return_pct"],150)
        self.assertEqual(result["capital_authority"].split("__")[0],"NONE")

    def test_stale_or_unordered_scenarios_fail(self):
        facts={"verified_at_utc":"2026-09-01T00:00:00+00:00",
               "official_sources":["https://example.org/official"],
               "contract_verified":True,"forward_supply_verified":True,
               "token_value_capture_verified":True,"credible_catalyst_verified":True,
               "supply_future":200,"bear_market_cap_usd":100,
               "base_market_cap_usd":400,"bull_market_cap_usd":1000}
        result,missing=d.scenario_map(facts,2,NOW)
        self.assertIsNone(result)
        self.assertTrue(any("stale" in x for x in missing))

    def test_mismatched_snapshots_fail(self):
        r,s=fixtures()
        r["universe_scan_as_of_utc"]="2026-09-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError,"mismatched"):
            d.build(r,s,{},NOW)

    def test_early_flow_not_selected_alphabetically(self):
        def row(sym,vol):
            return (sym,{"observations":{"market_cap":100000000,
                "market_structure":{"return_vs_7_completed_days_pct":5,
                                    "volume_7d_ratio":vol}}},{},1,2,True)
        full=[row("AAA"+str(i),1.35) for i in range(30)]+[row("COMP",4.7)]
        selected,stats=d.balanced_candidates(full)
        self.assertEqual(selected[0][0],"COMP")
        self.assertGreaterEqual(stats["early_selected"],16)

    def test_falling_knife_not_misclassified_as_early_flow(self):
        self.assertEqual(d.classify_cohort({"observations":{"market_structure":{
            "return_vs_7_completed_days_pct":-46,
            "volume_7d_ratio":3}}}),"ROTATING_FUNDAMENTALS_OR_UNCONFIRMED")

    def test_early_and_continuation_get_separate_slots(self):
        def row(sym,change,volume):
            item={"observations":{"market_structure":{
                "return_vs_7_completed_days_pct":change,"volume_7d_ratio":volume}}}
            return (sym,item,{},1,1,True)
        full=[row("HOT"+str(i),40,3) for i in range(35)]+[
            row("EARLY"+str(i),5,2) for i in range(20)]
        selected,stats=d.balanced_candidates(full)
        self.assertEqual(len(selected),40)
        self.assertEqual(stats["early_selected"],16)
        self.assertGreaterEqual(stats["continuation_selected"],12)
        self.assertIn("EARLY0",[x[0] for x in selected])

    def test_complete_verified_capital_gate_can_become_review_eligible(self):
        scenario={"bear_return_pct":-30,"base_return_pct":50,"bull_return_pct":90}
        fact={"liquidity_verified_at_utc":AT,"portfolio_verified_at_utc":AT,
              "drawdown_budget_verified":True,"counterparty_verified":True,
              "btc_same_horizon_base_return_pct":15,"liquidity_max_spread_bps":12,
              "liquidity_orderbook_depth_2pct_usdt":30000,
              "portfolio_open_cost_usdt":1400,
              "portfolio_pending_reservations_usdt":600,
              "max_proposed_new_cost_usdt":1000}
        eligible,blockers=d.capital_gate(fact,scenario,2,NOW)
        self.assertTrue(eligible,blockers)
        fact["liquidity_orderbook_depth_2pct_usdt"]=100
        eligible,blockers=d.capital_gate(fact,scenario,2,NOW)
        self.assertFalse(eligible)
        self.assertIn("DEPTH_INSUFFICIENT",blockers)

    def test_pool_overbook_blocks_even_complete_evidence(self):
        scenario={"bear_return_pct":-30,"base_return_pct":50,"bull_return_pct":90}
        fact={"liquidity_verified_at_utc":AT,"portfolio_verified_at_utc":AT,
              "drawdown_budget_verified":True,"counterparty_verified":True,
              "btc_same_horizon_base_return_pct":15,"liquidity_max_spread_bps":12,
              "liquidity_orderbook_depth_2pct_usdt":50000,
              "portfolio_open_cost_usdt":18000,
              "portfolio_pending_reservations_usdt":1000,
              "max_proposed_new_cost_usdt":2000}
        eligible,blockers=d.capital_gate(fact,scenario,2,NOW)
        self.assertFalse(eligible)
        self.assertIn("ALT_POOL_20000_USDT_CAP",blockers)

    def test_identity_audit_is_required_even_when_every_other_gate_passes(self):
        r,s=fixtures()
        facts={"verified_at_utc":AT,"official_sources":["https://example.org/official"],
               "contract_verified":True,"forward_supply_verified":True,
               "token_value_capture_verified":True,"credible_catalyst_verified":True,
               "supply_future":100,"bear_market_cap_usd":100,
               "base_market_cap_usd":400,"bull_market_cap_usd":800,
               "liquidity_verified_at_utc":AT,"portfolio_verified_at_utc":AT,
               "drawdown_budget_verified":True,"counterparty_verified":True,
               "btc_same_horizon_base_return_pct":10,"liquidity_max_spread_bps":10,
               "liquidity_orderbook_depth_2pct_usdt":20000,
               "portfolio_open_cost_usdt":1000,
               "portfolio_pending_reservations_usdt":0,
               "max_proposed_new_cost_usdt":1000}
        r["research_results"]={"RALLY":r["research_results"]["RALLY"]}
        no_identity=d.build(r,s,{"assets":{"RALLY":facts}},NOW)
        self.assertEqual(no_identity["capital_ready"],[])
        self.assertIn("CONTRACT_IDENTITY_NOT_CORROBORATED_OR_STALE",
                      no_identity["dossiers"][0]["capital_gate_blockers"])
        identity={"scan_as_of_utc":AT,
                  "assets":{"RALLY":{"capital_identity_pass":True,
                                     "identity_status":"THIRD_PARTY_CORROBORATED"}}}
        yes_identity=d.build(r,s,{"assets":{"RALLY":facts}},NOW,identity)
        self.assertEqual(yes_identity["capital_ready"],["RALLY"])
        identity["scan_as_of_utc"]="2026-09-01T00:00:00+00:00"
        self.assertEqual(d.build(r,s,{"assets":{"RALLY":facts}},NOW,identity)["capital_ready"],[])

    def test_stale_exchange_data_fails(self):
        r,s=fixtures()
        r["universe_scan_as_of_utc"]=s["as_of_utc"]="2026-09-20T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError,"stale"):
            d.build(r,s,{},NOW)

if __name__=="__main__":unittest.main()
