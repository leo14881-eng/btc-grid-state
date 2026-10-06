import copy
import unittest
from research.hunter_v2_execution import estimate_full_liquidation, position_cashflows


class LiquidationTests(unittest.TestCase):
    def setUp(self):
        self.pos = {"tranches": [{"notional_usdt": 100, "effective_quantity": 10,
                    "buy_fee_usdt": 1, "buy_slippage_bps": 10,
                    "cash_convention": "CASH_INCLUSIVE", "quantity_unit": "BASE",
                    "fee_unit": "USDT", "timestamp": "2026-10-06T11:00:00Z"}]}
        self.pos["tranches"][0]["execution_evidence"] = {
            "exchange": "Binance", "market": "spot", "symbol": "ENAUSDT", "side": "BUY",
            "price_unit": "USDT", "quantity_unit": "BASE", "effective_quantity": 10,
            "source_timestamp": "2026-10-06T11:00:00Z", "fetched_at": "2026-10-06T11:00:00Z"}
        self.book = {"exchange": "Binance", "market": "spot", "symbol": "ENAUSDT", "side": "SELL",
                     "price_unit": "USDT", "quantity_unit": "BASE", "source_timestamp": "2026-10-06T12:00:00Z",
                     "fetched_at": "2026-10-06T12:00:01Z", "bids": [[12, 5], [11, 5]], "asks": [[13, 5]]}

    def estimate(self, **kwargs):
        return estimate_full_liquidation(self.pos, self.book,
                    decision_timestamp="2026-10-06T12:00:02Z", expected_exchange="Binance",
                    expected_symbol="ENAUSDT", sell_fee_bps=100, **kwargs)

    def test_full_depth_and_no_double_charge(self):
        before = copy.deepcopy((self.pos, self.book))
        result = self.estimate()
        self.assertEqual(result["status"], "VALID")
        self.assertAlmostEqual(result["net_pnl_usdt"], 13.85)
        self.assertAlmostEqual(result["estimated_full_liquidation_proceeds"], 113.85)
        self.assertAlmostEqual(result["depth_impact_cost_usdt"], 5)
        self.assertAlmostEqual(result["spread_cost_vs_mid_usdt"], 5)
        self.assertEqual((self.pos, self.book), before)

    def test_fee_additional_cash(self):
        self.pos["tranches"][0]["cash_convention"] = "FEE_ADDITIONAL"
        self.assertAlmostEqual(self.estimate()["net_pnl_usdt"], 12.85)

    def test_add_is_cash_not_profit(self):
        self.pos["tranches"].append(dict(self.pos["tranches"][0]))
        self.book["bids"] = [[12, 5], [11, 15]]
        result = self.estimate()
        self.assertEqual(result["total_invested_cash"], 200)
        self.assertEqual(result["total_effective_quantity"], 20)
        self.assertAlmostEqual(result["net_pnl_usdt"], 22.75)

    def test_depth_shortfall(self):
        self.book["bids"] = [[12, 9.99]]
        self.assertEqual(self.estimate()["reason"], "INSUFFICIENT_FULL_LIQUIDATION_DEPTH")

    def test_bad_market_identity_units_and_side(self):
        for key, value in (("exchange", "Bybit"), ("symbol", "BTCUSDT"), ("market", "perpetual"),
                           ("side", "BUY"), ("quantity_unit", None), ("price_unit", "KRW")):
            with self.subTest(key=key):
                original = self.book[key]; self.book[key] = value
                self.assertEqual(self.estimate()["status"], "UNKNOWN")
                self.book[key] = original

    def test_source_timestamp_not_fetch_timestamp(self):
        self.book["source_timestamp"] = "2026-10-05T12:00:00Z"
        self.assertEqual(self.estimate()["reason"], "STALE_EXECUTION_EVIDENCE")
        self.book["source_timestamp"] = None
        self.assertEqual(self.estimate()["reason"], "SOURCE_TIMESTAMP_UNKNOWN")

    def test_future_and_timezone(self):
        for timestamp in ("2026-10-06T12:01:00Z", "2026-10-06T12:00:00"):
            self.book["source_timestamp"] = timestamp
            self.assertEqual(self.estimate()["status"], "UNKNOWN")

    def test_future_buy_rejected(self):
        self.pos["tranches"][0]["timestamp"] = "2026-10-06T12:01:00Z"
        self.assertEqual(self.estimate()["reason"], "FUTURE_BUY_CASHFLOW")

    def test_bad_levels(self):
        for levels in ([[12, float("nan")]], [[0, 10]], [[12, -1]], [[12, None]], [[12, 5], [12, 5]], [[14, 10]]):
            self.book["bids"] = levels
            self.assertEqual(self.estimate()["status"], "UNKNOWN")

    def test_invalid_cashflow(self):
        for key, value in (("buy_fee_usdt", None), ("buy_slippage_bps", float("nan")),
                           ("effective_quantity", -2), ("notional_usdt", True), ("cash_convention", None)):
            original = self.pos["tranches"][0][key]
            self.pos["tranches"][0][key] = value
            self.assertEqual(self.estimate()["status"], "UNKNOWN")
            self.pos["tranches"][0][key] = original

    def test_legacy_requires_explicit_opt_in_and_fee(self):
        self.pos["tranches"] = [{"notional_usdt": 100, "price": 10, "buy_slippage_bps": 10}]
        self.assertEqual(self.estimate()["reason"], "EFFECTIVE_QUANTITY_UNKNOWN")
        self.assertEqual(self.estimate(allow_legacy_model=True)["status"], "UNKNOWN")
        self.pos["tranches"][0]["buy_fee_bps"] = 10
        result = self.estimate(allow_legacy_model=True)
        self.assertEqual(result["status"], "VALID")
        self.assertFalse(result["execution_verified"])
        self.assertFalse(result["full_quantity_verified"])
        self.assertEqual(result["evidence_scope"], "LEGACY_MODEL_DERIVED")
        self.assertIn("LEGACY_MODEL_DERIVED", result["quantity_provenance"])

    def test_buy_execution_unknown_and_wrong_identity_fail(self):
        self.pos["tranches"][0].pop("execution_evidence")
        self.assertEqual(self.estimate()["reason"], "BUY_EXECUTION_EVIDENCE_UNKNOWN")

    def test_stale_buy_evidence_fail(self):
        self.pos["tranches"][0]["execution_evidence"]["source_timestamp"] = "2026-10-06T10:50:00Z"
        self.assertEqual(self.estimate()["reason"], "STALE_BUY_EXECUTION_EVIDENCE")

    def test_effective_quantity_must_match_buy_record(self):
        self.pos["tranches"][0]["execution_evidence"]["effective_quantity"] = 11
        self.assertEqual(self.estimate()["reason"], "BUY_EVIDENCE_QUANTITY_MISMATCH")

    def test_fee_unknown_never_pass(self):
        result = estimate_full_liquidation(self.pos, self.book, decision_timestamp="2026-10-06T12:00:02Z",
                    expected_exchange="Binance", expected_symbol="ENAUSDT", sell_fee_bps=None)
        self.assertEqual(result["status"], "UNKNOWN")

    def test_source_after_fetch_rejected(self):
        self.book["fetched_at"] = "2026-10-06T11:59:00Z"
        self.assertEqual(self.estimate()["reason"], "SOURCE_AFTER_FETCH")

    def test_bad_buy_identity(self):
        self.pos["tranches"][0]["execution_evidence"]["exchange"] = "Upbit"
        self.assertEqual(self.estimate()["reason"], "BUY_EVIDENCE_EXCHANGE_MISMATCH")

    def test_invalid_numeric_parameters(self):
        for kwargs in ({"max_age_seconds": 0}, {"max_age_seconds": float("nan")},
                       {"future_tolerance_seconds": -1}):
            self.assertEqual(self.estimate(**kwargs)["status"], "UNKNOWN")

    def test_output_aliases(self):
        result = self.estimate()
        self.assertEqual(result["executable_net_pnl_usdt"], result["net_pnl_usdt"])
        self.assertEqual(result["execution_price"], result["estimated_sell_vwap"])
        self.assertEqual(result["evidence_at"], result["source_timestamp"])
        self.assertTrue(result["execution_verified"])
        self.assertTrue(result["full_quantity_verified"])
        self.assertEqual(result["evidence_scope"], "VERIFIED_BOOK")
        self.assertFalse(result["fill_guaranteed"])

    def test_cashflow_helper(self):
        result = position_cashflows(self.pos)
        self.assertEqual(result["total_invested_cash"], 100)
        self.assertTrue(result["buy_costs_already_in_cash_or_quantity"])


if __name__ == "__main__":
    unittest.main()
