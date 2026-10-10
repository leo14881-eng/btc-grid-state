"""Offline V2 L AND B policy, production gate, legacy and restart regressions."""
import copy
import datetime as dt
import json
import unittest
from unittest.mock import patch
from research import hunter_v2_loss_freeze as loss
from research import hunter_shadow_trader_v2 as eng
from research import hunter_position_monitor as monitor
from research import hunter_tail_risk as tail
from research.hunter_policy import C
from tests.test_hunter_tail_risk import evidence, NOW
from tests.test_hunter_circuit_integration import CircuitProductionPathTests


def market(now=NOW, oid="one", shock=False):
    e = evidence(oid, btc5=-2.5 if shock else 0, neg=.85 if shock else .2,
                 loss5=.5 if shock else .05, observed_at=now)
    e.update(scan_fresh=True, scan_as_of_utc=now.isoformat())
    e["btc_short"]["last_completed_close_utc"] = now.isoformat()
    e["breadth"]["sample_count"] = 100
    e["liquidity"]["fresh_sample_count"] = 5
    e["stablecoins"]["prices"] = {"USDCUSDT": 1, "FDUSDUSDT": 1}
    return e


def state(now=NOW, shock=False):
    e = market(now, shock=shock)
    return {"systemic_risk": {"level": "NORMAL", "raw_level": "NORMAL",
             "last_observation_id": e["observation_id"], "evidence": e}}


def record(s, pnl=-100, now=NOW):
    loss.record_loss_exit(s, pnl, "USER_REQUESTED_MANUAL_STOP_LOSS", 1000, now, C)


class V2LossPolicyTests(unittest.TestCase):
    def test_l_and_b_truth_table(self):
        for l in (False, True):
            for b in (False, True):
                with self.subTest(l=l, b=b):
                    s = state(shock=b)
                    record(s)
                    if l: record(s)
                    self.assertEqual(bool(s.get("loss_freeze_episode")), l and b)
                    self.assertEqual(loss.quarantine(s), 2000 if l and b else 0)
                    self.assertEqual(loss.risk_blocks_new(s, NOW), l and b)

    def test_btc_thresholds_and_breadth_boundaries(self):
        for key, threshold in (("return_5m_pct", -2.5), ("return_15m_pct", -4),
                               ("return_1h_pct", -6)):
            for breadth_key, bound in (("negative_fraction", .85), ("loss_5pct_fraction", .5)):
                for btc_met in (False, True):
                    for breadth_met in (False, True):
                        e = market()
                        e["btc_short"][key] = threshold if btc_met else threshold + .0001
                        e["breadth"][breadth_key] = bound if breadth_met else bound - .0001
                        if breadth_key == "loss_5pct_fraction": e["breadth"]["negative_fraction"] = .6
                        self.assertEqual(loss.market_confirmation(e, NOW)["confirmed"], btc_met and breadth_met)

    def test_missing_stale_future_evidence_never_confirms_b(self):
        for path in (("observed_at_utc",), ("scan_as_of_utc",),
                     ("btc_short", "last_completed_close_utc")):
            for delta in (-901, 1):
                e = market(shock=True)
                target = e if len(path) == 1 else e[path[0]]
                target[path[-1]] = (NOW + dt.timedelta(seconds=delta)).isoformat()
                self.assertFalse(loss.market_confirmation(e, NOW)["confirmed"])
            e = market(shock=True)
            target = e if len(path) == 1 else e[path[0]]
            target.pop(path[-1])
            self.assertFalse(loss.market_confirmation(e, NOW)["confirmed"])
        for mutate in (
            lambda e: e.pop("observation_id"), lambda e: e.pop("missing_or_stale"),
            lambda e: e.update(missing_or_stale=["BTC_SHORT_TERM_MISSING"]),
            lambda e: e.update(scan_fresh=False),
            lambda e: e["btc_short"].update(return_1h_pct=None),
            lambda e: e["btc_short"].update(return_1h_pct=float("nan")),
            lambda e: e["breadth"].update(sample_count=0),
            lambda e: e["breadth"].update(negative_fraction=1.1),
        ):
            e = market(shock=True); mutate(e)
            self.assertFalse(loss.market_confirmation(e, NOW)["confirmed"])
        e = market(shock=True)
        for path in ("observed_at_utc", "scan_as_of_utc"):
            e[path] = (NOW - dt.timedelta(seconds=900)).isoformat()
        e["btc_short"]["last_completed_close_utc"] = e["scan_as_of_utc"]
        self.assertTrue(loss.market_confirmation(e, NOW)["confirmed"])

    def test_normal_losses_never_quarantine_or_watch_recover_into_freeze(self):
        for count in (1, 2):
            s = state()
            for _ in range(count): record(s)
            for i in range(1, 7):
                now = NOW + dt.timedelta(minutes=5*i)
                loss.update_risk_controls(s, market(now, str(i)), now, C)
                self.assertFalse(loss.risk_blocks_new(s, now))
                self.assertEqual(loss.quarantine(s), 0)
                self.assertNotIn("loss_freeze_episode", s)
            self.assertEqual(s["loss_control_v2"]["consecutive_loss_exits"], count)

    def test_six_hour_window_uses_evaluation_time_without_new_exit(self):
        s = state(); record(s, -1000)
        boundary = NOW + dt.timedelta(hours=6)
        loss.risk_blocks_new(s, boundary)
        self.assertEqual(s["loss_control_v2"]["rolling_realized_loss_usdt"], 1000)
        later = boundary + dt.timedelta(microseconds=1)
        s["systemic_risk"]["evidence"] = market(later, "later", True)
        self.assertFalse(loss.risk_blocks_new(s, later))
        self.assertEqual(s["loss_control_v2"]["rolling_realized_loss_usdt"], 0)
        self.assertFalse(s["loss_control_v2"]["loss_threshold_met"])

    def test_rolling_threshold_independent_of_consecutive_and_profit(self):
        for total, expected in ((999.99, False), (1000, True)):
            s = state(shock=True); record(s, -total)
            self.assertEqual(bool(s.get("loss_freeze_episode")), expected)
        s = state(); record(s, -600); record(s, 10); record(s, -400)
        s["systemic_risk"]["evidence"] = market(shock=True)
        self.assertTrue(loss.risk_blocks_new(s, NOW))

    def test_episode_recovery_duplicate_restart_and_300_second_boundary(self):
        s = state(shock=True); record(s); record(s)
        for seconds, oid, expected in ((300, "n1", 1), (599, "early", 1), (600, "n2", 2)):
            now = NOW + dt.timedelta(seconds=seconds)
            loss.update_risk_controls(s, market(now, oid), now, C)
            self.assertEqual(s["loss_freeze_episode"]["recovery_observations"], expected)
            s = json.loads(json.dumps(s))
        self.assertEqual(loss.quarantine(s), 1500)
        before = copy.deepcopy(s["loss_freeze_episode"])
        loss.update_risk_controls(s, market(NOW+dt.timedelta(seconds=600), "n2"),
                                  NOW+dt.timedelta(seconds=900), C)
        self.assertEqual(s["loss_freeze_episode"], before)
        for seconds in (900, 1200, 1500):
            now = NOW + dt.timedelta(seconds=seconds)
            loss.update_risk_controls(s, market(now, str(seconds)), now, C)
        self.assertEqual(loss.quarantine(s), 0)
        self.assertFalse(loss.risk_blocks_new(s, now))
        s["systemic_risk"]["evidence"] = market(now, "same-losses-new-shock", True)
        self.assertFalse(loss.risk_blocks_new(s, now))
        record(s, 10, now)
        self.assertFalse(loss.risk_blocks_new(s, now))

    def test_one_normal_or_future_observation_cannot_clear_episode(self):
        s = state(shock=True); record(s); record(s)
        now = NOW + dt.timedelta(minutes=5)
        loss.update_risk_controls(s, market(now, "normal"), now, C)
        self.assertEqual(loss.quarantine(s), 2000)
        future = now + dt.timedelta(seconds=1)
        loss.update_risk_controls(s, market(future, "future"), now, C)
        self.assertEqual(loss.quarantine(s), 2000)
        self.assertEqual(s["loss_freeze_episode"]["recovery_observations"], 1)

    def test_systemic_gate_independent_and_high_is_not_b(self):
        s = state(); record(s); record(s)
        s["systemic_risk"]["level"] = "HIGH"
        self.assertTrue(loss.risk_blocks_new(s, NOW))
        self.assertEqual(loss.quarantine(s), 0)
        self.assertNotIn("loss_freeze_episode", s)

    def test_existing_legacy_episode_finishes_without_relabeling(self):
        s = state()
        tail.record_loss_exit(s, -100, "OLD", 1000, NOW, C)
        tail.record_loss_exit(s, -100, "OLD", 1000, NOW, C)
        original_events = copy.deepcopy(s["circuit_breaker"]["loss_events"])
        for i in range(1, 6):
            now = NOW + dt.timedelta(minutes=5*i)
            loss.update_risk_controls(s, market(now, str(i)), now, C)
            if i < 5: self.assertNotIn("loss_control_v2", s)
        self.assertEqual(s["circuit_breaker"]["loss_events"], original_events)
        self.assertEqual(s["circuit_breaker"]["status"], "NORMAL")
        self.assertEqual(s["loss_control_v2"]["policy"], loss.POLICY)
        self.assertNotIn("loss_freeze_episode", s)

    def test_v1_accounting_unchanged_and_v2_keeps_asset_lock_and_capital_bounds(self):
        with patch.object(eng, "STRATEGY_ID", eng.LANES["V1"]["strategy"]):
            s = state()
            eng.update_loss_exit_guard(s, -100, "OLD", NOW, 1000)
            eng.update_loss_exit_guard(s, -100, "OLD", NOW, 1000)
            self.assertEqual(s["circuit_breaker"]["status"], "TRIPPED")
            self.assertEqual(s["circuit_breaker"]["quarantined_cash_usdt"], 2000)
            self.assertNotIn("loss_control_v2", s)
        with patch.object(eng, "STRATEGY_ID", eng.LANES["V2"]["strategy"]), patch.object(eng, "CAPITAL_POOL_USDT", 20000):
            s = state(); s.update(open_positions=[], closed_positions=[])
            eng.update_loss_exit_guard(s, -100, "USER_REQUESTED_MANUAL_STOP_LOSS", NOW, 1000)
            eng.register_exit_for_reentry(s, {"asset": "X", "net_pnl_usdt": -100}, 1, "USER_REQUESTED_MANUAL_STOP_LOSS", NOW)
            self.assertTrue(s["reentry_registry"]["X"]["risk_lock"])
            self.assertEqual(s["loss_exit_guard"]["realized_loss_usdt"], 100)
            cap = eng.capital_snapshot(s, {"coins": {"BTC": {"change_24h_pct": 4}, "X": {"change_24h_pct": 2}}})
            self.assertEqual((cap["capital_pool"], cap["ordinary_opportunity_cap"], cap["strategic_reserve"]), (20000, 17000, 3000))
            self.assertLessEqual(cap["max_deployable_usdt"], 20000)
            self.assertFalse(eng.capital_available(s, 17001))


class V2LossProductionGateTests(unittest.TestCase):
    def test_direct_manager_and_allocator_share_qualified_gate(self):
        helper = CircuitProductionPathTests()
        for qualified in (False, True):
            for direct in (False, True):
                with self.subTest(qualified=qualified, direct=direct), helper.isolate():
                    now, s, scan, review, liq, supply, _ = helper.fixture("NORMAL")
                    monitor.configure_lane(False)
                    s["systemic_risk"]["evidence"] = market(now, shock=qualified)
                    record(s, now=now); record(s, now=now)
                    proposals = None if direct else []
                    eng.manage_existing_positions(s, scan, review, liq, supply, now, capital_proposals=proposals)
                    if not direct: eng.execute_capital_proposals(s, proposals, scan, now)
                    self.assertEqual(helper.used(s), 1000 if qualified else 2000)

    def test_v2_buy_loop_uses_unified_gate(self):
        # Existing production full-lane fixture executes the real BUY loop.
        helper = CircuitProductionPathTests()
        original = helper.fixture
        for qualified in (False, True):
            def fixture(status):
                result = original(status)
                now, s = result[:2]
                s["systemic_risk"]["evidence"] = market(now, shock=qualified)
                record(s, now=now); record(s, now=now)
                return result
            with patch.object(helper, "fixture", side_effect=fixture):
                saved = helper.full_lane("V2", "NORMAL")
            self.assertEqual({e["type"].rsplit("_", 1)[-1] for e in saved["events"]},
                             set() if qualified else {"BUY", "ADD"})


if __name__ == "__main__":
    unittest.main()
