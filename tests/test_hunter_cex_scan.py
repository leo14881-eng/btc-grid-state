import importlib.util
import pathlib
import unittest
from unittest.mock import patch

FILE=pathlib.Path(__file__).resolve().parents[1]/"research/hunter_cex_scan.py"
SPEC=importlib.util.spec_from_file_location("hunter_cex_scan",FILE)
scan=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)

class CexUniverseTests(unittest.TestCase):
    def test_pump_not_dropped_by_up_suffix(self):
        self.assertTrue(scan.valid("PUMP"))
        self.assertTrue(scan.valid("BTC"))
        self.assertFalse(scan.valid("USDC"))

    def test_binance_detects_missing_ticker_and_includes_pump(self):
        metadata={"symbols":[
            {"symbol":"PUMPUSDT","baseAsset":"PUMP","quoteAsset":"USDT","status":"TRADING","isSpotTradingAllowed":True},
            {"symbol":"NEWUSDT","baseAsset":"NEW","quoteAsset":"USDT","status":"TRADING"},
            {"symbol":"OLDUSDT","baseAsset":"OLD","quoteAsset":"USDT","status":"BREAK"}]}
        ticks=[{"symbol":"PUMPUSDT","lastPrice":"0.003","quoteVolume":"50000","priceChangePercent":"-3"}]
        with patch.object(scan,"fetch",side_effect=[metadata,ticks]):
            rows,status=scan.binance()
        self.assertEqual([r["base"] for r in rows],["PUMP"])
        self.assertEqual(rows[0]["change_24h_pct"],-3)
        self.assertEqual(status["active_pairs"],2)
        self.assertEqual(status["missing_or_invalid"],["NEWUSDT"])

    def test_bybit_spot_and_negative_price_change(self):
        instruments={"retCode":0,"result":{"list":[
            {"symbol":"ABCUSDT","baseCoin":"ABC","quoteCoin":"USDT","status":"Trading"},
            {"symbol":"USDCUSDT","baseCoin":"USDC","quoteCoin":"USDT","status":"Trading"}]}}
        tickers={"retCode":0,"result":{"list":[
            {"symbol":"ABCUSDT","lastPrice":"1.2","turnover24h":"60000","price24hPcnt":"-0.11"}]}}
        with patch.object(scan,"fetch",side_effect=[instruments,tickers]):
            rows,status=scan.bybit()
        self.assertEqual(status["active_pairs"],1)
        self.assertEqual(rows[0]["change_24h_pct"],-11)

    def test_cross_venue_union_and_existing_baseline(self):
        b={"venue":"binance","pair":"ABCUSDT","base":"ABC","price":1.03,"volume_24h_usdt":50000,"change_24h_pct":5}
        y={"venue":"bybit","pair":"ABCUSDT","base":"ABC","price":1.02,"volume_24h_usdt":10000,"change_24h_pct":4}
        r=scan.build({"binance":[b],"bybit":[y]},{"coins":{"ABC":{"venue_prices":{"binance":1,"bybit":1}}}},"2026-09-26T00:00:00Z")
        self.assertEqual(r["unique_base_tickers"],1)
        self.assertEqual(r["coins"]["ABC"]["stage"],"PRE_MOVE_WATCH")
        self.assertTrue(r["coins"]["ABC"]["contract_identity_unverified"])

    def test_all_stages_enter_forward_upside_queue_even_after_rally(self):
        changes={"RALLY":95,"FLAT":0,"DOWN":-35,"MID":9}
        rows=[{"venue":"binance","pair":base+"USDT","base":base,
               "price":1.0,"volume_24h_usdt":100000,"change_24h_pct":pct}
              for base,pct in changes.items()]
        report=scan.build({"binance":rows},{},"2026-09-26T00:00:00Z")
        self.assertEqual(report["research_coverage_count"],4)
        self.assertEqual({r["base"] for r in report["research_leads"]},set(changes))
        self.assertTrue(all(r["prior_rally_auto_reject"] is False for r in report["research_leads"]))
        self.assertEqual(report["coins"]["RALLY"]["stage"],"POST_MOVE")
        self.assertEqual(report["coins"]["DOWN"]["stage"],"BASELINE")

if __name__=="__main__":unittest.main()
