"""Offline shared V1/V2 L AND B policy, production gate, legacy and restart regressions."""
import copy
import datetime as dt
import json
import unittest
from unittest.mock import patch
from research import hunter_loss_freeze as loss
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


class SharedLossPolicyTests(unittest.TestCase):
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
            self.assertEqual(s["loss_control"]["consecutive_loss_exits"], count)

    def test_six_hour_window_uses_evaluation_time_without_new_exit(self):
        s = state(); record(s, -1000)
        boundary = NOW + dt.timedelta(hours=6)
        loss.risk_blocks_new(s, boundary)
        self.assertEqual(s["loss_control"]["rolling_realized_loss_usdt"], 1000)
        later = boundary + dt.timedelta(microseconds=1)
        s["systemic_risk"]["evidence"] = market(later, "later", True)
        self.assertFalse(loss.risk_blocks_new(s, later))
        self.assertEqual(s["loss_control"]["rolling_realized_loss_usdt"], 0)
        self.assertFalse(s["loss_control"]["loss_threshold_met"])

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
            if i < 5: self.assertNotIn("loss_control", s)
        self.assertEqual(s["circuit_breaker"]["loss_events"], original_events)
        self.assertEqual(s["circuit_breaker"]["status"], "NORMAL")
        self.assertEqual(s["loss_control"]["policy"], loss.POLICY)
        self.assertNotIn("loss_freeze_episode", s)

    def test_each_lane_books_losses_and_preserves_own_entry_and_capital_rules(self):
        helper = CircuitProductionPathTests()
        for v1 in (True, False):
            with self.subTest(v1=v1), helper.isolate():
                monitor.configure_lane(v1)
                s = state(); s.update(open_positions=[], closed_positions=[])
                for _ in range(2):
                    eng.update_loss_exit_guard(s, -100, "USER_REQUESTED_MANUAL_STOP_LOSS", NOW, 1000)
                eng.register_exit_for_reentry(s, {"asset": "X", "net_pnl_usdt": -100}, 1, "USER_REQUESTED_MANUAL_STOP_LOSS", NOW)
                self.assertTrue(s["reentry_registry"]["X"]["risk_lock"])
                self.assertEqual(s["loss_exit_guard"]["realized_loss_usdt"], 200)
                self.assertFalse(eng.risk_blocks_new(s, NOW))
                self.assertEqual(eng.loss_quarantine(s), 0)
                if v1:
                    self.assertIsNone(eng.CAPITAL_POOL_USDT)
                    self.assertTrue(eng.capital_available(s, 1000000))
                    self.assertEqual(eng.ENTRY_MODE, "DISCOVERY")
                else:
                    cap = eng.capital_snapshot(s, {"coins": {"BTC": {"change_24h_pct": 4}, "X": {"change_24h_pct": 2}}})
                    self.assertEqual((cap["capital_pool"], cap["ordinary_opportunity_cap"], cap["strategic_reserve"]), (20000, 17000, 3000))
                    self.assertFalse(eng.capital_available(s, 17001))
                    self.assertEqual(eng.ENTRY_MODE, "EXECUTABLE")

    def test_lanes_have_independent_loss_ledgers(self):
        v1, v2 = state(shock=True), state(shock=True)
        record(v1); record(v1); record(v2)
        self.assertTrue(loss.risk_blocks_new(v1, NOW))
        self.assertFalse(loss.risk_blocks_new(v2, NOW))
        self.assertEqual(loss.quarantine(v1), 2000)
        self.assertEqual(loss.quarantine(v2), 0)

    def test_expired_broken_streak_principal_cannot_be_quarantined_later(self):
        s = state(); record(s, -100); record(s, 1)
        later = NOW + dt.timedelta(hours=7)
        s["systemic_risk"]["evidence"] = market(later, "current", True)
        record(s, -1000, later)
        self.assertEqual(loss.quarantine(s), 1000)
        self.assertEqual(s["loss_control"]["rolling_realized_loss_usdt"], 1000)

    def test_unbroken_consecutive_losses_still_qualify_across_window(self):
        s = state(); record(s, -100)
        later = NOW + dt.timedelta(hours=7)
        s["systemic_risk"]["evidence"] = market(later, "current", True)
        record(s, -100, later)
        self.assertEqual(loss.quarantine(s), 2000)
        self.assertEqual(s["loss_control"]["rolling_realized_loss_usdt"], 100)

    def test_broken_expired_losses_do_not_satisfy_l(self):
        s = state(); record(s, -600); record(s, 1); record(s, -400); record(s, 1)
        later = NOW + dt.timedelta(hours=7)
        s["systemic_risk"]["evidence"] = market(later, "current", True)
        record(s, -100, later)
        self.assertFalse(loss.risk_blocks_new(s, later))
        self.assertEqual(loss.quarantine(s), 0)


class SharedLossProductionGateTests(unittest.TestCase):
    def test_direct_manager_and_allocator_share_qualified_gate(self):
        helper = CircuitProductionPathTests()
        for v1 in (True, False):
            for qualified in (False, True):
                for direct in (False, True):
                    with self.subTest(v1=v1, qualified=qualified, direct=direct), helper.isolate():
                        now, s, scan, review, liq, supply, _ = helper.fixture("NORMAL")
                        monitor.configure_lane(v1)
                        s["systemic_risk"]["evidence"] = market(now, shock=qualified)
                        record(s, now=now); record(s, now=now)
                        proposals = None if direct else []
                        eng.manage_existing_positions(s, scan, review, liq, supply, now, capital_proposals=proposals)
                        if not direct: eng.execute_capital_proposals(s, proposals, scan, now)
                        self.assertEqual(helper.used(s), 1000 if qualified else 2000)

    def test_both_buy_loops_use_unified_gate(self):
        helper = CircuitProductionPathTests()
        original = helper.fixture
        for lane in ("V1", "V2"):
            for qualified in (False, True):
                def fixture(status):
                    result = original(status)
                    now, s = result[:2]
                    s["systemic_risk"]["evidence"] = market(now, shock=qualified)
                    record(s, now=now); record(s, now=now)
                    return result
                with self.subTest(lane=lane, qualified=qualified), patch.object(helper, "fixture", side_effect=fixture):
                    saved = helper.full_lane(lane, "NORMAL")
                self.assertEqual({e["type"].rsplit("_", 1)[-1] for e in saved["events"]},
                                 set() if qualified else {"BUY", "ADD"})



class SharedLossReviewRegressionTests(unittest.TestCase):
    def activated(self, v1):
        monitor.configure_lane(v1)
        s = state(shock=True)
        s.update(open_positions=[], closed_positions=[], circuit_breaker={
            "status": "NORMAL", "quarantined_cash_usdt": 0, "historical_marker": "unchanged"})
        for _ in range(2):
            eng.update_loss_exit_guard(s, -100, "USER_REQUESTED_MANUAL_STOP_LOSS", NOW, 1000)
        return s

    def observe(self, s, seconds, oid, unknown=False):
        now = NOW + dt.timedelta(seconds=seconds)
        e = market(now, oid)
        if unknown:
            e["missing_or_stale"] = ["BTC_SHORT_TERM_MISSING"]
            e["btc_short"]["return_5m_pct"] = None
        eng.update_risk_controls(s, e, now)
        return e

    def test_both_lanes_unknown_breaks_normal_sequence_with_restart_and_replay(self):
        helper = CircuitProductionPathTests()
        for v1 in (True, False):
            with self.subTest(v1=v1), helper.isolate():
                s = self.activated(v1)
                self.observe(s, 300, "normal1")
                unknown = self.observe(s, 600, "unknown", True)
                self.assertEqual(s["loss_freeze_episode"]["recovery_observations"], 0)
                self.assertEqual(loss.quarantine(s), 2000)
                s = json.loads(json.dumps(s))
                self.observe(s, 900, "normal2")
                self.assertEqual(s["loss_freeze_episode"]["recovery_observations"], 1)
                self.assertEqual(loss.quarantine(s), 2000)
                before = copy.deepcopy(s["loss_freeze_episode"])
                eng.update_risk_controls(s, unknown, NOW+dt.timedelta(seconds=950))
                self.assertEqual(s["loss_freeze_episode"], before)
                old = market(NOW+dt.timedelta(seconds=800), "out-of-order")
                old["missing_or_stale"] = ["UNKNOWN"]
                eng.update_risk_controls(s, old, NOW+dt.timedelta(seconds=1000))
                self.assertEqual(s["loss_freeze_episode"], before)
                self.observe(s, 1200, "normal3")
                self.assertEqual(s["loss_freeze_episode"]["recovery_observations"], 2)
                self.assertEqual(loss.quarantine(s), 1500)

    def test_both_lanes_missing_or_future_source_clock_never_preserves_recovery_credit(self):
        helper = CircuitProductionPathTests()
        for v1 in (True, False):
            for clock in ("missing", "future"):
                with self.subTest(v1=v1, clock=clock), helper.isolate():
                    s = self.activated(v1)
                    self.observe(s, 300, "normal1")
                    now = NOW+dt.timedelta(seconds=600)
                    e = market(now, clock)
                    if clock == "missing":
                        e.pop("observed_at_utc")
                    else:
                        e["observed_at_utc"] = (now+dt.timedelta(seconds=1)).isoformat()
                    eng.update_risk_controls(s, e, now)
                    self.assertEqual(s["loss_freeze_episode"]["recovery_observations"], 0)
                    self.assertEqual(loss.quarantine(s), 2000)
                    self.observe(s, 900, "normal2")
                    self.assertEqual(s["loss_freeze_episode"]["recovery_observations"], 1)
                    self.assertEqual(loss.quarantine(s), 2000)

    def test_both_lanes_normal_market_new_loss_interrupts_without_extra_quarantine(self):
        helper = CircuitProductionPathTests()
        for v1 in (True, False):
            for prior_normals in (1, 2):
                with self.subTest(v1=v1, prior_normals=prior_normals), helper.isolate():
                    s = self.activated(v1)
                    for i in range(1, prior_normals+1):
                        self.observe(s, 300*i, "normal"+str(i))
                    before = copy.deepcopy(s["loss_freeze_episode"])
                    at = NOW+dt.timedelta(seconds=300*prior_normals+60)
                    eng.update_loss_exit_guard(s, -10, "USER_REQUESTED_MANUAL_STOP_LOSS", at, 1000)
                    ep = s["loss_freeze_episode"]
                    self.assertEqual(ep["episode_id"], before["episode_id"])
                    self.assertEqual(ep["quarantined_cash_usdt"], before["quarantined_cash_usdt"])
                    self.assertEqual(ep["quarantine_base_usdt"], before["quarantine_base_usdt"])
                    self.assertEqual(ep["last_loss_exit_at_utc"], at.isoformat())
                    self.assertEqual(ep["recovery_observations"], 0)
                    self.assertIsNone(ep["last_recovery_counted_at_utc"])
                    self.assertIsNone(ep["last_recovery_counted_observed_at_utc"])
                    self.assertFalse(s["loss_control"]["market_confirmation"]["confirmed"])
                    s = json.loads(json.dumps(s))
                    self.observe(s, 300*prior_normals+360, "first-after-loss")
                    self.assertEqual(s["loss_freeze_episode"]["recovery_observations"], 1)
                    self.assertEqual(loss.quarantine(s), before["quarantined_cash_usdt"])
                    self.assertTrue(eng.risk_blocks_new(s, at+dt.timedelta(seconds=300)))

    def test_both_lane_summaries_show_effective_controls_without_legacy_mutation(self):
        helper = CircuitProductionPathTests()
        for v1 in (True, False):
            with self.subTest(v1=v1), helper.isolate():
                s = self.activated(v1)
                before = copy.deepcopy(s)
                summary = eng.build_summary(s, NOW)
                capital = summary["policy"]["capital_management"]
                risk = summary["policy"]["tail_risk_phase1"]
                for view in (capital, risk):
                    self.assertEqual(view["circuit_breaker"]["status"], "TRIPPED")
                    self.assertEqual(view["circuit_breaker"]["quarantined_cash_usdt"], 2000)
                    self.assertEqual(view["effective_quarantined_cash_usdt"], 2000)
                    self.assertEqual(view["circuit_breaker_source"], "LOSS_FREEZE_EPISODE")
                    self.assertEqual(view["legacy_circuit_breaker"], before["circuit_breaker"])
                    self.assertEqual(view["legacy_circuit_breaker_role"], "INACTIVE_HISTORY")
                    self.assertTrue(view["new_risk_blocked"])
                self.assertEqual(s, before)
                self.assertEqual(summary["capital_authority"], "NONE_SHADOW_ONLY")
                if v1:
                    self.assertIsNone(capital["equity_usdt"])
                    self.assertIsNone(capital["max_deployable_usdt"])
                else:
                    self.assertEqual(capital["capital_pool"], 20000)
                    self.assertEqual(capital["ordinary_opportunity_cap"], 17000)
                    self.assertEqual(capital["strategic_reserve"], 3000)

    def test_both_lanes_no_episode_normal_loss_does_not_create_freeze(self):
        helper = CircuitProductionPathTests()
        for v1 in (True, False):
            with self.subTest(v1=v1), helper.isolate():
                monitor.configure_lane(v1)
                s = state()
                for _ in range(2):
                    eng.update_loss_exit_guard(s, -100, "USER_REQUESTED_MANUAL_STOP_LOSS", NOW, 1000)
                self.assertNotIn("loss_freeze_episode", s)
                self.assertFalse(eng.risk_blocks_new(s, NOW))
                self.assertEqual(loss.quarantine(s), 0)

if __name__ == "__main__":
    unittest.main()
