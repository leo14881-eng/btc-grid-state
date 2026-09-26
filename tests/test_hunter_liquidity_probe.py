import datetime as dt
import importlib.util
import pathlib
import unittest

spec=importlib.util.spec_from_file_location("hunter_liquidity_probe",pathlib.Path("research/hunter_liquidity_probe.py"))
h=importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
NOW=dt.datetime(2026,9,26,9,0,tzinfo=dt.timezone.utc)
AT=NOW.isoformat()

class LiquidityTests(unittest.TestCase):
    def test_measure_spread_and_two_sided_depth(self):
        b={"bids":[["99","10"],["97","10"]],
           "asks":[["101","10"],["103","10"]]}
        x=h.measure(b,NOW)
        self.assertEqual(x["spread_bps"],200)
        self.assertEqual(x["bid_depth_2pct_usdt"],990)
        self.assertEqual(x["ask_depth_2pct_usdt"],1010)
        self.assertTrue(x["partial_book"])

    def test_invalid_book_rejected(self):
        with self.assertRaises(ValueError):
            h.measure({"bids":[["102","1"]],"asks":[["101","1"]]},NOW)
        with self.assertRaises(ValueError):
            h.measure({"bids":[],"asks":[]},NOW)

    def test_two_lanes_and_no_duplicate_tickers(self):
        d={"early_entry_watchlist":["AAA","BBB"],
           "continuation_watchlist":["AAA","CCC"]}
        s={"coins":{sym:{"pairs":[{"venue":"binance","pair":sym+"USDT","volume_24h_usdt":1000}]}
                    for sym in ("AAA","BBB","CCC")}}
        self.assertEqual([x[0] for x in h.targets(d,s)],["AAA","BBB","CCC"])

    def test_failure_does_not_mask_other_assets(self):
        s={"as_of_utc":AT,"coins":{sym:{"pairs":[{"venue":"binance","pair":sym+"USDT",
             "volume_24h_usdt":1000}]} for sym in ("AAA","BBB")}}
        d={"market_universe_size":2,"dossiers":[],
           "early_entry_watchlist":["AAA"],"continuation_watchlist":["BBB"]}
        def fetch(url):
            if "AAA" in url:raise RuntimeError("403")
            return {"bids":[["99","10"]],"asks":[["101","10"]]}
        r=h.build(s,d,fetch,NOW)
        self.assertEqual(r["requested_count"],2)
        self.assertEqual(r["successful_count"],1)
        self.assertIn("AAA",r["failures"])
        self.assertIn("BBB",r["snapshots"])
        self.assertEqual(r["capital_authority"],"NONE__OFFICIAL_FACTS_AND_PORTFOLIO_GATES_SEPARATE")

    def test_stale_scan_is_fatal(self):
        s={"as_of_utc":(NOW-dt.timedelta(hours=3)).isoformat(),"coins":{}}
        d={"market_universe_size":0,"dossiers":[]}
        with self.assertRaises(ValueError):
            h.build(s,d,lambda url:{},NOW)

if __name__=="__main__":unittest.main()
