import concurrent.futures
import unittest
from research.hunter_v2_candidate import ACTIVE_ENGINE, ReplayTransactionFixture, require_candidate_context, analyze_candidate


class CandidateBoundaryTests(unittest.TestCase):
    def test_full_depth_model_arms_then_produces_positive_exit_intent(self):
        from tests.test_hunter_v2_execution import LiquidationTests
        fixture = LiquidationTests()
        fixture.setUp()
        fixture.pos.update(position_id="integration-1", asset="ENA")
        original = dict(fixture.pos)
        first = analyze_candidate(fixture.pos, fixture.book, {}, context="UNIT_TEST",
            now="2026-10-06T12:00:02Z", generation="g1", exchange="Binance",
            symbol="ENAUSDT", sell_fee_bps=100)
        self.assertEqual(first["position"]["protection_state"], "PROFIT_PROTECTION_ARMED")
        self.assertFalse(first["persist_verified"])
        fixture.book.update(source_timestamp="2026-10-06T12:01:00Z", fetched_at="2026-10-06T12:01:01Z")
        fixture.book["bids"] = [[10.2, 10]]
        second = analyze_candidate(first["position"], fixture.book, {}, context="UNIT_TEST",
            now="2026-10-06T12:01:02Z", generation="g2", exchange="Binance",
            symbol="ENAUSDT", sell_fee_bps=100)
        self.assertEqual(second["position"]["protection_state"], "EXIT_PENDING")
        self.assertEqual(second["decision"]["action"], "SELL_INTENT")
        self.assertGreater(second["execution"]["net_pnl_usdt"], 0)
        self.assertEqual(fixture.pos, original)

    def test_live_context_is_rejected_and_legacy_remains_active(self):
        self.assertEqual(ACTIVE_ENGINE, "legacy")
        for context in ("VULTR", "SCHEDULED", "LIVE", None):
            with self.assertRaises(ValueError):
                require_candidate_context(context)

    def test_crash_atomicity_restart_and_replay(self):
        original = {"generation_sequence": 7, "protection_state": "EXIT_PENDING"}
        fixture = ReplayTransactionFixture(original, context="UNIT_TEST")
        before, sha = fixture.read()
        closed = {"protection_state": "CLOSED"}
        with self.assertRaises(RuntimeError):
            fixture.commit(sha, closed, transition_id="close-1", generation_sequence=8, fail_before_commit=True)
        self.assertEqual(fixture.read()[0], before)
        # Process restart restores the persisted pending snapshot, not UNARMED.
        fixture = ReplayTransactionFixture(fixture.read()[0], context="UNIT_TEST")
        self.assertEqual(fixture.commit(sha, closed, transition_id="close-1", generation_sequence=8), "PERSIST_VERIFIED")
        self.assertEqual(fixture.commit(sha, closed, transition_id="close-1", generation_sequence=8), "ALREADY_COMMITTED")

    def test_two_writers_only_one_cas_commit(self):
        fixture = ReplayTransactionFixture({"generation_sequence": 1}, context="UNIT_TEST")
        _, sha = fixture.read()
        def submit(n):
            try:
                return fixture.commit(sha, {"winner": n}, transition_id=str(n), generation_sequence=2)
            except ValueError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(submit, [1, 2]))
        self.assertCountEqual(results, ["PERSIST_VERIFIED", "CAS_CONFLICT"])

    def test_stale_generation_and_unknown_readback(self):
        fixture = ReplayTransactionFixture({"generation_sequence": 9}, context="UNIT_TEST")
        _, sha = fixture.read()
        with self.assertRaisesRegex(ValueError, "STALE_GENERATION"):
            fixture.commit(sha, {}, transition_id="old", generation_sequence=8)
        with self.assertRaisesRegex(RuntimeError, "UNVERIFIED"):
            fixture.commit(sha, {"protection_state": "CLOSED"}, transition_id="new", generation_sequence=10, fail_readback=True)
        self.assertEqual(fixture.read()[0]["protection_state"], "CLOSED")
        self.assertEqual(fixture.commit(sha, {}, transition_id="new", generation_sequence=10), "ALREADY_COMMITTED")


if __name__ == "__main__":
    unittest.main()
