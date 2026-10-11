import copy
import json
import os
import pathlib
import shutil
import tempfile
import unittest

from scripts import hunter_lock_capacity as capacity


GEN = "2026-10-11T20:00:00Z"


def fixture():
    docs = {}
    for prefix in capacity.LANES:
        docs[prefix + "-portfolio.json"] = dict(
            schema="hunter_shadow_v2_portfolio_v2", mode="SIMULATION_ONLY_NO_REAL_ORDERS",
            updated_at_utc=GEN, open_positions=[dict(asset="BTC", tranches=[dict(notional_usdt=1000)])],
            closed_positions=[dict(asset="ETH", net_pnl_usdt=5, tranches=[dict(notional_usdt=100)])],
            events=[dict(asset="BTC", type="BUY"), dict(asset="ETH", type="SELL")],
            decisions=[dict(asset="ETH", action="SELL")], ever_entered_assets=["BTC", "ETH"],
            capital_state=dict(initial_capital_usdt=20000))
        docs[prefix + "-summary.json"] = dict(mode="SIMULATION_ONLY_NO_REAL_ORDERS",
            capital_authority="NONE_SHADOW_ONLY", open_positions=1, closed_positions=1, as_of_utc=GEN,
            policy=dict(capital_pool_usdt=20000, capital_management=dict(initial_capital_usdt=20000)))
    docs["hunter-position-monitor.json"] = dict(results=[dict(lane="SHADOW_V1", open=1),
                                                         dict(lane="SHADOW_V2", open=1)])
    docs["hunter-leading-risk.json"] = dict(capital_authority="NONE_SHADOW_ONLY", shadow_only=True,
        real_position_mutation=False, current=dict(level="NORMAL"))
    docs["hunter-scheduler-health.json"] = dict(schema="hunter_scheduler_health_v1",
        current_generation_id=GEN, last_successful_monitor_generation_id=GEN,
        shadow_only=True, real_order_count=0, trigger_source="VULTR_SYSTEMD")
    return capacity.serialize(docs)


class CapacityModelTests(unittest.TestCase):
    def test_one_x_preserves_exact_bytes(self):
        raw = fixture()
        raw["hunter-shadow-portfolio.json"] += "  \n"
        self.assertEqual(capacity.amplify(raw, 1), raw)

    def test_amplification_preserves_original_history_capital_and_open_positions(self):
        raw = fixture()
        original = copy.deepcopy(raw)
        for scale in (2, 5):
            expanded = capacity.amplify(raw, scale)
            before, after = capacity.parse(raw), capacity.parse(expanded)
            self.assertGreater(capacity.manifest(expanded)["total_bytes"], capacity.manifest(raw)["total_bytes"])
            for prefix in capacity.LANES:
                name = prefix + "-portfolio.json"
                for key in capacity.HISTORY_KEYS:
                    self.assertEqual(after[name][key], before[name][key] * scale)
                    self.assertEqual(after[name][key][:len(before[name][key])], before[name][key])
                for key in ("open_positions", "capital_state", "ever_entered_assets"):
                    self.assertEqual(after[name][key], before[name][key])
                self.assertEqual(after[prefix + "-summary.json"]["policy"], before[prefix + "-summary.json"]["policy"])
            self.assertEqual(capacity.validate_shadow(expanded), GEN)
        self.assertEqual(raw, original)

    def test_rejects_real_trading_and_invalid_scale_or_source(self):
        for scale in (0, -1, True, 1.5):
            with self.assertRaisesRegex(RuntimeError, "INVALID_CAPACITY_SCALE"):
                capacity.amplify(fixture(), scale)
        docs = capacity.parse(fixture())
        docs["hunter-leading-risk.json"]["real_trading_enabled"] = True
        with self.assertRaisesRegex(RuntimeError, "CAPACITY_REAL_TRADING_FORBIDDEN"):
            capacity.amplify(capacity.serialize(docs), 2)
        docs = capacity.parse(fixture())
        docs["hunter-shadow-v2-portfolio.json"]["open_positions"][0]["tranches"][0]["notional_usdt"] = 20001
        with self.assertRaisesRegex(RuntimeError, "V2_EXPOSURE_HARD_CAP_EXCEEDED"):
            capacity.amplify(capacity.serialize(docs), 2)

    def test_simulated_cycle_only_changes_generation_and_revision_metadata(self):
        raw = capacity.amplify(fixture(), 2)
        next_raw = capacity.serialize(capacity.next_snapshot(capacity.parse(raw), "2026-10-11T21:00:00Z"))
        self.assertEqual(capacity.stable_payload(raw), capacity.stable_payload(next_raw))
        self.assertEqual(capacity.validate_shadow(next_raw), "2026-10-11T21:00:00Z")


@unittest.skipUnless(shutil.which("git"), "Git is required")
class CapacitySourceTests(unittest.TestCase):
    def test_reads_pinned_tracked_snapshot_without_changing_dirty_worktree(self):
        with tempfile.TemporaryDirectory(prefix="capacity-source-test-") as directory:
            root = pathlib.Path(directory)
            capacity.git_at(root, "init", "--initial-branch=main")
            capacity.git_at(root, "config", "user.name", "capacity-test")
            capacity.git_at(root, "config", "user.email", "capacity-test@localhost")
            raw = fixture()
            capacity.write_snapshot(root, raw)
            capacity.git_at(root, "add", ".")
            capacity.git_at(root, "commit", "-m", "fixture")
            target = root / capacity.publisher.PATHS[0]
            target.write_bytes(target.read_bytes() + b"  \n")
            before = capacity.working_manifest(root)
            loaded, metadata = capacity.read_source(root, "HEAD")
            self.assertEqual(loaded, raw)
            self.assertEqual(before, capacity.working_manifest(root))
            self.assertEqual(metadata["snapshot"], capacity.manifest(raw))
            self.assertEqual(len(metadata["commit_sha"]), 40)


@unittest.skipUnless(os.name == "posix" and shutil.which("git"), "Linux/POSIX flock and Git are required")
class CapacityPublicationTests(unittest.TestCase):
    def test_two_contending_writers_rebind_publish_and_report_timings(self):
        raw = fixture()
        before = copy.deepcopy(raw)
        report = capacity.run_scale(raw, 2, iterations=2, workers=2)
        self.assertEqual(report["status"], "success", report)
        self.assertEqual(raw, before)
        self.assertEqual(report["published_commit_count"], 4)
        self.assertTrue(report["history_and_capital_unchanged"])
        self.assertEqual({row["role"] for row in report["cycles"]}, {"research", "monitor"})
        by_base = {row["base_sha"]: row for row in report["cycles"]}
        head = report["initial_sha"]
        for _ in range(4):
            row = by_base[head]
            self.assertTrue(row["history_and_capital_unchanged"])
            self.assertGreaterEqual(row["wait_seconds"], 0)
            self.assertGreater(row["hold_seconds"], 0)
            self.assertGreaterEqual(row["total_seconds"], row["hold_seconds"])
            for phase in ("parse", "serialize", "persist_including_cas_commit_push_readback",
                          "readback_including_fetch_parse_validate"):
                self.assertGreater(row["phase_seconds"][phase], 0)
            self.assertEqual(row["git_calls"]["commit"], 1)
            self.assertGreater(row["git_seconds"]["commit"], 0)
            self.assertTrue(any("SERVER_POST_PUSH_READBACK_OK" in line for line in row["audit_log"]))
            head = row["published_sha"]
        self.assertEqual(head, report["final_sha"])


if __name__ == "__main__":
    unittest.main()
