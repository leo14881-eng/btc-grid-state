import datetime as dt
import importlib.util
import pathlib
import unittest
from unittest.mock import patch

FILE=pathlib.Path(__file__).resolve().parents[1]/"research/hunter_forward_research.py"
SPEC=importlib.util.spec_from_file_location("hunter_forward_research",FILE)
engine=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)
NOW=dt.datetime(2026,9,26,9,0,tzinfo=dt.timezone.utc)

def coin(base,change,day):
    return {"stage":"POST_MOVE" if day>=20 else "BASELINE",
            "reference_price":1.0,"change_24h_pct":day,
            "change_since_previous_scan_pct":change,
            "pairs":[{"venue":"binance","pair":base+"USDT",
                      "volume_24h_usdt":1000000,"price":1.0}]}

class ForwardResearchTests(unittest.TestCase):
    def test_ambiguous_symbols_never_get_other_project_fundamentals(self):
        rows=[{"symbol":"ABC","id":"abc-one"},{"symbol":"ABC","id":"abc-two"},
              {"symbol":"XYZ","id":"xyz"}]
        unique,ambiguous=engine.unique_symbols(rows,"symbol")
        self.assertNotIn("ABC",unique)
        self.assertEqual(ambiguous,["ABC"])
        self.assertEqual(unique["XYZ"]["id"],"xyz")

    def test_rotation_includes_prior_rallies_declines_and_flat_assets(self):
        coins={"RALLY":coin("RALLY",5,200),"DOWN":coin("DOWN",-1,-90),
               "FLAT":coin("FLAT",0,0)}
        with patch.object(engine,"ROTATION",3),patch.object(engine,"TRIGGER_LIMIT",0):
            chosen,cursor,triggers=engine.select_rotation(coins,{})
        self.assertEqual(set(chosen),set(coins))
        self.assertEqual(cursor,0)
        self.assertIn("RALLY",triggers)
        self.assertIn("DOWN",triggers)

    def test_rotation_advances_and_trigger_backlog_is_visible(self):
        coins={s:coin(s,10,0) for s in ("A","B","C","D")}
        with patch.object(engine,"ROTATION",2),patch.object(engine,"TRIGGER_LIMIT",1):
            chosen,cursor,triggered=engine.select_rotation(coins,{"rotation_cursor":2})
        self.assertEqual(cursor,0)
        self.assertEqual(chosen,["A","C","D"])
        self.assertEqual(len(triggered),4)

    def test_klines_compare_only_completed_candles(self):
        rows=[[i*86400000,"1","2","0.5",str(1+i/100),"100","0",str(100+i*10)]
              for i in range(35)]
        result,err=engine.candle_features(rows,50000)
        self.assertIsNone(err)
        self.assertEqual(result["last_7d_quote_volume"],sum(100+i*10 for i in range(27,34)))
        self.assertGreater(result["volume_7d_ratio"],1)
        self.assertEqual(result["volume_24h_usdt"],50000)

    def test_short_klines_do_not_create_signals(self):
        self.assertIsNone(engine.candle_features([],500)[0])

    def test_no_fundamentals_cannot_generate_buy_or_fake_terminal_prices(self):
        c=coin("RALLY",30,150)
        with patch.object(engine,"get",return_value=[]):
            result=engine.research_one("RALLY",c,{}, {},NOW)
        self.assertFalse(result["capital_ready"])
        self.assertNotIn("target_price",result)
        self.assertIn("Primary-source catalyst and causal tokenholder value capture unverified",
                      result["missing_facts"])
        self.assertTrue(result["past_return_not_an_eligibility_gate"])

    def test_fdv_supply_warning_not_automatic_rejection_of_rally(self):
        c=coin("RALLY",30,150)
        cg={"RALLY":{"id":"rally","market_cap":100,"fully_diluted_valuation":500}}
        with patch.object(engine,"get",return_value=[]):
            result=engine.research_one("RALLY",c,cg,{},NOW)
        self.assertTrue(any("FDV >=3x" in x for x in result["missing_facts"]))
        self.assertFalse(result["capital_ready"])

    def test_all_universe_scanned_even_when_deep_research_is_batched(self):
        coins={"RALLY":coin("RALLY",0,200),"DOWN":coin("DOWN",0,-90),
               "FLAT":coin("FLAT",0,0)}
        scan={"coins":coins,"coverage_status":"BINANCE_COMPLETE_BYBIT_UNAVAILABLE"}
        with patch.object(engine,"ROTATION",2),patch.object(engine,"TRIGGER_LIMIT",0),\
             patch.object(engine,"research_one",side_effect=lambda s,c,cg,dl,n:{"symbol":s,"researched_at_utc":n.isoformat()}):
            report=engine.build_report(scan,{}, {},NOW)
        self.assertEqual(report["lightweight_universe_review_count"],3)
        self.assertEqual(report["deep_research_this_cycle"],2)
        self.assertEqual(report["capital_ready"],[])
        self.assertEqual(report["buy_proposals"],[])

    def test_empty_universe_fails_loudly(self):
        with self.assertRaisesRegex(ValueError,"No CEX coins"):
            engine.build_report({"coins":{}},{},{},NOW)

    def test_cache_compaction_keeps_only_relevant_minimal_fields(self):
        market={"coingecko":[{"symbol":"AAA","id":"aaa","market_cap":10,"image":"huge"},
                             {"symbol":"ZZZ","id":"zzz","market_cap":20}],
                "defillama":[{"symbol":"AAA","name":"AAA","tvl":50,"audit":"huge"}]}
        result=engine.compact_market(market,{"AAA":{}})
        self.assertEqual(len(result["coingecko"]),1)
        self.assertNotIn("image",result["coingecko"][0])
        self.assertNotIn("audit",result["defillama"][0])
        self.assertEqual(result["cache_universe_count"],1)

    def test_refresh_failure_retains_timestamp_not_fresh_fake(self):
        old={"as_of_utc":"2026-09-20T00:00:00+00:00",
             "coingecko":[{"symbol":"AAA","id":"aaa"}],"defillama":[]}
        errors={}
        with patch.object(engine,"get",side_effect=RuntimeError("quota")):
            market=engine.enrichment(NOW,old,errors)
        self.assertEqual(market["coingecko_as_of_utc"],old["as_of_utc"])
        self.assertIn("coingecko",errors)
        self.assertEqual(market["coingecko"][0]["id"],"aaa")

if __name__=="__main__":unittest.main()
