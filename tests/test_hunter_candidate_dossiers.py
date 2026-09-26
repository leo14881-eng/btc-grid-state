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

    def test_stale_exchange_data_fails(self):
        r,s=fixtures()
        r["universe_scan_as_of_utc"]=s["as_of_utc"]="2026-09-20T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError,"stale"):
            d.build(r,s,{},NOW)

if __name__=="__main__":unittest.main()
