import json
import unittest
from research.hunter_v2_protection import (
    initialize, advance, complete_exit, check_add, record_incident, ProtectionParams)

NOW = "2026-10-06T15:00:00Z"


def evidence(pnl=30, cash=1000, qty=10, now=NOW, **extras):
    return dict(status="VALID", executable_net_pnl_usdt=pnl,
                executable_net_return_pct=pnl / cash * 100, execution_price=103,
                source_timestamp=now, quantity=qty, total_invested_cash=cash,
                full_quantity_verified=True, execution_verified=True,
                evidence_scope="VERIFIED_BOOK", **extras)


def armed():
    return advance({"position_id": "p1", "asset": "ENA", "mfe_pct": 100},
                   evidence(), {}, NOW, "g1")[0]


class ProtectionTests(unittest.TestCase):
    def test_persistent_arm_restart_generation(self):
        p = json.loads(json.dumps(armed()))
        p, d = advance(p, evidence(20, now="2026-10-06T15:05:00Z"), {},
                       "2026-10-06T15:05:00Z", "g2")
        self.assertEqual(p["protection_state"], "PROFIT_PROTECTION_ARMED")
        self.assertEqual(p["armed_generation_id"], "g1")
        self.assertEqual(p["peak_executable_net_pnl_usdt"], 30)

    def test_historical_mfe_does_not_arm(self):
        p, _ = advance({"position_id": "p1", "mfe_pct": 100}, evidence(1), {}, NOW, "g")
        self.assertEqual(p["protection_state"], "UNARMED")

    def test_unknown_does_not_seed_peak(self):
        p, d = advance({"position_id": "p1"}, {"status": "UNKNOWN"}, {}, NOW, "g")
        self.assertIsNone(p["peak_executable_net_pnl_usdt"])
        self.assertEqual(d["reason"], "EXECUTION_EVIDENCE_UNKNOWN")

    def test_unknown_preserves_floor_and_deduplicates(self):
        p = armed()
        p, _ = advance(p, {}, {}, "2026-10-06T15:05:00Z", "g2")
        p, _ = advance(p, {}, {}, "2026-10-06T15:10:00Z", "g3")
        self.assertEqual(p["protected_net_pnl_floor_usdt"], 7.5)
        self.assertEqual(len(p["protection_incidents"]), 1)
        self.assertEqual(p["protection_state"], "PROFIT_PROTECTION_ARMED")

    def test_stale_book_rejected(self):
        p, d = advance(armed(), evidence(), {}, "2026-10-06T15:05:00Z", "g")
        self.assertEqual(d["reason"], "EXECUTION_EVIDENCE_UNKNOWN")
        self.assertEqual(p["last_executable_net_pnl_usdt"], 30)

    def test_missing_fullsize_or_verification_rejected(self):
        for key in ("execution_verified", "full_quantity_verified", "quantity"):
            e = evidence()
            e.pop(key)
            _, d = advance({"position_id": "p"}, e, {}, NOW, "g")
            self.assertEqual(d["reason"], "EXECUTION_EVIDENCE_UNKNOWN")

    def test_future_book_rejected(self):
        _, d = advance({"position_id": "p"}, evidence(now="2026-10-06T15:01:00Z"), {}, NOW, "g")
        self.assertEqual(d["reason"], "EXECUTION_EVIDENCE_UNKNOWN")

    def test_runner_requires_all_fresh_evidence(self):
        sig = dict(fresh=True, btc_relative_positive=True, relative_1h_positive=True,
                   relative_4h_positive=True, acceleration_positive=True, liquidity_valid=True,
                   source_timestamp=NOW, fetched_at=NOW, source="research", evidence_id="r1")
        p, _ = advance({"position_id": "p"}, evidence(90), sig, NOW, "g")
        self.assertEqual(p["protection_state"], "RUNNER")
        self.assertEqual(p["protected_net_pnl_floor_usdt"], 54)
        sig.pop("relative_4h_positive")
        p, _ = advance({"position_id": "p"}, evidence(90), sig, NOW, "g")
        self.assertEqual(p["protection_state"], "PROFIT_PROTECTION_ARMED")

    def test_runner_never_demotes_or_relaxes(self):
        sig = {k: True for k in ("fresh", "btc_relative_positive", "relative_1h_positive",
                                "relative_4h_positive", "acceleration_positive", "liquidity_valid")}
        sig.update(source_timestamp=NOW, fetched_at=NOW, source="research", evidence_id="r1")
        p, _ = advance({"position_id": "p"}, evidence(90), sig, NOW, "g")
        p, _ = advance(p, evidence(70, now="2026-10-06T15:05:00Z"), {},
                       "2026-10-06T15:05:00Z", "g2")
        self.assertEqual(p["protection_state"], "RUNNER")
        self.assertEqual(p["protected_net_pnl_floor_usdt"], 54)

    def test_positive_breach_pending_and_crash_restart(self):
        p, d = advance(armed(), evidence(5, now="2026-10-06T15:05:00Z"), {},
                       "2026-10-06T15:05:00Z", "g2")
        self.assertEqual(d["action"], "SELL_INTENT")
        p = json.loads(json.dumps(p))
        p, d = advance(p, evidence(10, now="2026-10-06T15:10:00Z"), {},
                       "2026-10-06T15:10:00Z", "g3")
        self.assertEqual(p["protection_state"], "EXIT_PENDING")
        self.assertEqual(d["action"], "SELL_INTENT")

    def test_negative_breach_no_force_sell_and_incident(self):
        p, d = advance(armed(), evidence(-50, now="2026-10-06T15:05:00Z"), {},
                       "2026-10-06T15:05:00Z", "g2")
        self.assertEqual(d["action"], "HOLD")
        self.assertEqual(d["reason"], "MISSED_PROFIT_EXIT_NET_NONPOSITIVE")
        p, _ = advance(p, evidence(-60, now="2026-10-06T15:10:00Z"), {},
                       "2026-10-06T15:10:00Z", "g3")
        self.assertEqual(len(p["protection_incidents"]), 1)

    def test_validated_add_keeps_peak_floor_and_state(self):
        p = armed()
        after = evidence(20, cash=2000, qty=20, now="2026-10-06T15:05:00Z")
        self.assertTrue(check_add(p, evidence(now="2026-10-06T15:05:00Z"), after,
                                  "2026-10-06T15:05:00Z")["allowed"])
        p, d = advance(p, after, {"add_preflight_verified": True},
                       "2026-10-06T15:05:00Z", "g2")
        self.assertEqual(p["protection_state"], "PROFIT_PROTECTION_ARMED")
        self.assertEqual(p["peak_executable_net_pnl_usdt"], 30)
        self.assertEqual(p["protected_net_pnl_floor_usdt"], 7.5)
        self.assertEqual(p["protected_floor_basis_cash_usdt"], 1000)
        self.assertEqual(p["protected_net_return_floor_pct"], 0.75)

    def test_costly_add_rejected(self):
        result = check_add(armed(), evidence(), evidence(2, cash=2000, qty=20), NOW)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "PROTECTION_FLOOR_WOULD_BE_BREACHED")

    def test_external_add_does_not_mathematically_sell(self):
        p, d = advance(armed(), evidence(2, cash=2000, qty=20, now="2026-10-06T15:05:00Z"), {},
                       "2026-10-06T15:05:00Z", "g2")
        self.assertEqual(d["action"], "HOLD")
        self.assertEqual(p["protection_state"], "PROFIT_PROTECTION_ARMED")
        self.assertEqual(p["protected_net_pnl_floor_usdt"], 7.5)

    def test_raw_arm_is_explicit_candidate(self):
        p, _ = advance({"position_id": "p"}, evidence(5), {"observed_price_return_pct": 3},
                       NOW, "g", ProtectionParams(arm_basis="raw"))
        self.assertEqual(p["protection_state"], "PROFIT_PROTECTION_ARMED")

    def test_giveback_floor_monotonic_after_add(self):
        par = ProtectionParams(floor_mode="giveback_pct")
        p, _ = advance({"position_id": "p"}, evidence(30), {}, NOW, "g", par)
        self.assertEqual(p["protected_net_pnl_floor_usdt"], 15)
        p, _ = advance(p, evidence(20, cash=2000, qty=20, now="2026-10-06T15:05:00Z"),
                       {"add_preflight_verified": True}, "2026-10-06T15:05:00Z", "g2", par)
        self.assertEqual(p["protected_net_pnl_floor_usdt"], 15)

    def test_no_close_from_quote_or_negative_receipt(self):
        p, _ = advance(armed(), evidence(5, now="2026-10-06T15:05:00Z"), {},
                       "2026-10-06T15:05:00Z", "g2")
        with self.assertRaises(ValueError):
            complete_exit(p, evidence(5), "2026-10-06T15:05:01Z", "g2")

    def test_complete_exactly_once_after_persist_reload(self):
        p, _ = advance(armed(), evidence(5, now="2026-10-06T15:05:00Z"), {},
                       "2026-10-06T15:05:00Z", "g2")
        receipt = dict(status="SHADOW_EXECUTED", position_id="p1", generation_id="g2",
                       transition_id=p["last_transition_id"], execution_id="fill1",
                       full_quantity=True, evidence_scope="VERIFIED_BOOK", net_pnl_usdt=5)
        p, d = complete_exit(p, receipt, "2026-10-06T15:05:01Z", "g2")
        self.assertEqual(d["action"], "CLOSED_TRANSITION")
        version = p["state_version"]
        p, d = complete_exit(json.loads(json.dumps(p)), receipt, "2026-10-06T15:05:02Z", "g2")
        self.assertEqual(d["action"], "NOOP")
        self.assertEqual(p["state_version"], version)

    def test_cas_rejection_is_incident_not_close(self):
        p = record_incident(armed(), "CAS_OR_GENERATION_REJECTED", NOW, "g2", reason="STALE")
        self.assertEqual(p["protection_state"], "PROFIT_PROTECTION_ARMED")
        self.assertEqual(p["protection_incidents"][0]["readback_result"], "NOT_VERIFIED")

    def test_duplicate_observation_and_deepcopy(self):
        original = armed()
        p, d = advance(original, evidence(5), {}, NOW, "g2")
        self.assertEqual(d["reason"], "STALE_OR_DUPLICATE_OBSERVATION")
        p["asset"] = "OTHER"
        self.assertEqual(original["asset"], "ENA")

    def test_assumption_replay_cannot_close(self):
        e = evidence()
        e["evidence_scope"] = "ASSUMPTION_ONLY"
        e["status"] = "ASSUMPTION_ONLY"
        e["full_quantity_verified"] = False
        e["execution_verified"] = False
        _, d = advance({"position_id": "p"}, e, {}, NOW, "g")
        self.assertEqual(d["reason"], "EXECUTION_EVIDENCE_UNKNOWN")
        p, _ = advance({"position_id": "p"}, e, {}, NOW, "g",
                       ProtectionParams(allow_assumption_only=True))
        self.assertEqual(p["protection_evidence_scope"], "ASSUMPTION_ONLY")

    def test_reentry_starts_new_lifecycle(self):
        p = initialize({"position_id": "new", "asset": "ENA"})
        self.assertEqual(p["protection_state"], "UNARMED")
        self.assertIsNone(p["peak_executable_net_pnl_usdt"])

    def test_runner_timestamp_and_source_required(self):
        base = {k: True for k in ("fresh", "btc_relative_positive", "relative_1h_positive",
                                 "relative_4h_positive", "acceleration_positive", "liquidity_valid")}
        base.update(source_timestamp=NOW, fetched_at=NOW, source="research", evidence_id="r1")
        variants = []
        for key in ("source_timestamp", "fetched_at", "source", "evidence_id"):
            sig = dict(base)
            sig.pop(key)
            variants.append(sig)
        variants.append(dict(base, source_timestamp="2026-10-06T14:00:00Z"))
        variants.append(dict(base, fetched_at="2026-10-06T15:01:00Z"))
        for sig in variants:
            p, _ = advance({"position_id": "p"}, evidence(90), sig, NOW, "g")
            self.assertEqual(p["protection_state"], "PROFIT_PROTECTION_ARMED")

    def test_invalid_parameters_and_missing_position_id(self):
        with self.assertRaises(ValueError):
            ProtectionParams(capture_mid=2)
        with self.assertRaises(ValueError):
            initialize({"asset": "ENA"})


if __name__ == "__main__":
    unittest.main()
