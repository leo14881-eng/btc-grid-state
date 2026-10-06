import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import hunter_research_rebind as r
from research import hunter_position_monitor as m


class ResearchRebindTests(unittest.TestCase):
    def command(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True).stdout

    def write(self, root, path, doc):
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc))

    def fixture(self, root, closed=False):
        for prefix, lane in [("hunter-shadow", "SHADOW_V1"),
                             ("hunter-shadow-v2", "SHADOW_V2")]:
            state = {"schema": "hunter_shadow_v2_portfolio_v2",
                     "mode": "SIMULATION_ONLY_NO_REAL_ORDERS", "updated_at_utc": "now",
                     "open_positions": [] if closed else [{"asset": "X"}],
                     "closed_positions": [{"asset": "X", "pnl": 12}] if closed else [],
                     "events": [{"type": "SELL", "pnl": 12}] if closed else [],
                     "circuit_breaker": {"status": "TRIPPED", "recovery_count": 1},
                     "asset_quarantine": {"X": {"remaining": 2}}}
            summary = {"mode": state["mode"], "capital_authority": "NONE_SHADOW_ONLY",
                       "open_positions": len(state["open_positions"]),
                       "closed_positions": len(state["closed_positions"]), "as_of_utc": "now",
                       "policy": {"capital_pool_usdt": 20000,
                                  "capital_management": {"initial_capital_usdt": 20000}}}
            self.write(root, r.PATHS[0].rsplit("/", 1)[0] + "/" + prefix + "-portfolio.json", state)
            self.write(root, "research/results/" + prefix + "-summary.json", summary)
        self.write(root, "research/results/hunter-position-monitor.json",
                   {"results": [{"lane": l, "open": 0 if closed else 1}
                                for l in ["SHADOW_V1", "SHADOW_V2"]]})
        self.write(root, "research/results/hunter-leading-risk.json",
                   {"capital_authority": "NONE_SHADOW_ONLY", "shadow_only": True,
                    "real_position_mutation": False, "current": {"level": "NORMAL"}})
        self.write(root, "research/results/hunter-scheduler-health.json",
                   {"schema": "hunter_scheduler_health_v1", "current_generation_id": "g",
                    "last_successful_monitor_generation_id": "g", "shadow_only": True,
                    "real_order_count": 0})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.origin = self.root / "origin.git"
        self.writer = self.root / "writer"
        self.checkout = self.root / "research"
        self.command(self.root, "init", "--bare", str(self.origin))
        self.command(self.root, "init", "-b", "main", str(self.writer))
        self.command(self.writer, "config", "user.name", "test")
        self.command(self.writer, "config", "user.email", "test@example.invalid")
        self.fixture(self.writer)
        self.write(self.writer, r.SCAN, {"generation_id": "scan", "binance_complete": True})
        self.write(self.writer, "research/results/hunter-forward-research.json", {"old": True})
        self.command(self.writer, "add", ".")
        self.command(self.writer, "commit", "-m", "initial")
        self.command(self.writer, "remote", "add", "origin", str(self.origin))
        self.command(self.writer, "push", "origin", "main")
        self.command(self.root, "clone", "-b", "main", str(self.origin), str(self.checkout))
        self.cwd = pathlib.Path.cwd()
        os.chdir(self.checkout)
        self.env = patch.dict(os.environ, {"GITHUB_ACTIONS": "true"})
        self.env.start()
        self.write(self.checkout, "research/results/hunter-forward-research.json", {"new_research": True})

    def tearDown(self):
        self.env.stop()
        os.chdir(self.cwd)
        self.temp.cleanup()

    def publish(self):
        self.command(self.writer, "add", ".")
        self.command(self.writer, "commit", "-m", "concurrent update")
        self.command(self.writer, "push", "origin", "main")

    def test_monitor_sell_profit_quarantine_and_recovery_survive_rebind(self):
        self.fixture(self.writer, closed=True)
        self.publish()
        base = r.rebind("scan", isolated=True)
        self.assertEqual(base, self.command(self.writer, "rev-parse", "HEAD").strip())
        p = json.loads(pathlib.Path(r.PATHS[0]).read_text())
        self.assertEqual(p["open_positions"], [])
        self.assertEqual(p["events"], [{"type": "SELL", "pnl": 12}])
        self.assertEqual(p["circuit_breaker"], {"status": "TRIPPED", "recovery_count": 1})
        self.assertEqual(p["asset_quarantine"], {"X": {"remaining": 2}})
        self.assertEqual(json.loads(pathlib.Path("research/results/hunter-forward-research.json").read_text()),
                         {"new_research": True})
        # A later Monitor update is still rejected by final CAS.
        self.fixture(self.writer, closed=False)
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "CAS_REJECTED"):
            r.check_current(base, "scan")

    def test_new_discovery_generation_rejected_without_reset(self):
        before = r.git("rev-parse", "HEAD").stdout
        self.write(self.writer, r.SCAN, {"generation_id": "new", "binance_complete": True})
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "RESEARCH_INPUT_CHANGED"):
            r.rebind("scan", isolated=True)
        self.assertEqual(r.git("rev-parse", "HEAD").stdout, before)

    def test_changed_engine_rejected(self):
        p = self.writer / "research/hunter_tail_risk.py"
        p.write_text("# new risk logic")
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "RESEARCH_INPUT_CHANGED"):
            r.rebind("scan", isolated=True)

    def test_other_research_update_rejected(self):
        self.write(self.writer, "research/results/hunter-forward-research.json", {"other": True})
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "RESEARCH_INPUT_CHANGED"):
            r.rebind("scan", isolated=True)

    def test_final_cas_rejects_changed_execution_helper(self):
        base = r.git("rev-parse", "HEAD").stdout.strip()
        p = self.writer / "scripts/hunter_research_rebind.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# concurrent code")
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "CAS_REJECTED"):
            r.check_current(base, "scan")

    def test_invalid_monitor_snapshot_rejected(self):
        self.write(self.writer, "research/results/hunter-shadow-v2-summary.json", {"mode": "REAL"})
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "MODE_MISMATCH"):
            r.rebind("scan", isolated=True)

    def test_cannot_reset_non_ci_or_unconfirmed_checkout(self):
        with self.assertRaisesRegex(RuntimeError, "DISPOSABLE_CI"):
            r.rebind("scan")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false", "HUNTER_RESEARCH_ISOLATED_CHECKOUT": "0"}):
            with self.assertRaisesRegex(RuntimeError, "DISPOSABLE_CI"):
                r.rebind("scan", isolated=True)

    def test_workflow_replays_before_rebind_and_keeps_final_cas(self):
        s = (self.cwd / ".github/workflows/hunter-research-pipeline.yml").read_text()
        self.assertLess(s.index("Run historical leading-risk replay"),
                        s.index("Bind latest authoritative monitor state"))
        self.assertLess(s.index("Bind latest authoritative monitor state"),
                        s.index("Run V1 broad-net shadow orders"))
        self.assertIn("LATEST_EXECUTION_BASE_REQUIRED", s)
        self.assertIn("HUNTER_STATE_CAS_REJECTED_STALE_WRITER", s)

    def publish_verified_monitor_metadata(self):
        import datetime as dt
        from scripts import hunter_monitor_persist as persist
        from scripts import hunter_monitor_runtime as runtime
        self.fixture(self.writer, closed=True)
        self.write(self.writer, ".github/hunter-runtime.json",
                   {"shadow_only": True, "primary_jobs": {"monitor": True}})
        health_path = self.writer / runtime.HEALTH_PATH
        h = json.loads(health_path.read_text())
        h.update(trigger_source="VULTR_SYSTEMD",
                 monitor_completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        health_path.write_text(json.dumps(h))
        self.publish()
        commit = self.command(self.writer, "rev-parse", "HEAD").strip()
        previous = pathlib.Path.cwd()
        try:
            os.chdir(self.writer)
            raw = persist.snapshot()
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
                persist.readback(raw, "g", commit)
                runtime.publish_verified(raw, "g", commit)
        finally:
            os.chdir(previous)
        self.command(self.writer, "fetch", "origin", "main")
        self.command(self.writer, "merge", "--ff-only", "origin/main")
        return commit

    def test_verified_monitor_proof_can_change_during_research_precomputation(self):
        self.publish_verified_monitor_metadata()
        base = r.rebind("scan", isolated=True)
        self.assertEqual(base, self.command(self.writer, "rev-parse", "HEAD").strip())
        self.assertEqual(json.loads(pathlib.Path(r.PROOF_PATH).read_text())["monitor_generation_id"], "g")
        self.assertEqual(json.loads(pathlib.Path("research/results/hunter-forward-research.json").read_text()),
                         {"new_research": True})
        # Final execution CAS continues rejecting any later metadata publication.
        proof = json.loads((self.writer/r.PROOF_PATH).read_text())
        proof["completed_at_utc"] = proof["completed_at_utc"].replace("+00:00", "Z")
        self.write(self.writer, r.PROOF_PATH, proof); self.publish()
        with self.assertRaisesRegex(RuntimeError, "CAS_REJECTED"):
            r.check_current(base, "scan")

    def test_new_monitor_commit_before_next_proof_uses_two_independent_validated_snapshots(self):
        self.publish_verified_monitor_metadata()
        h = json.loads((self.writer/r.HEALTH_PATH).read_text())
        h["current_generation_id"] = h["last_successful_monitor_generation_id"] = "g2"
        self.write(self.writer, r.HEALTH_PATH, h); self.publish()
        r.rebind("scan", isolated=True)
        self.assertEqual(json.loads(pathlib.Path(r.HEALTH_PATH).read_text())["current_generation_id"], "g2")
        self.assertEqual(json.loads(pathlib.Path(r.PROOF_PATH).read_text())["monitor_generation_id"], "g")

    def test_invalid_monitor_proof_is_not_an_allowlist_escape(self):
        self.publish_verified_monitor_metadata()
        good = json.loads((self.writer/r.PROOF_PATH).read_text())
        before = r.git("rev-parse", "HEAD").stdout
        for key, value in [("source", "GITHUB_ACTIONS"), ("main_readback_verified", False),
                           ("real_trading_enabled", True), ("scheduler_health_sha256", "wrong"),
                           ("monitor_generation_id", "old"), ("completed_at_utc", "2020-01-01T00:00:00Z"),
                           ("main_readback_head_sha", "a"*40)]:
            with self.subTest(key=key):
                self.write(self.writer, r.PROOF_PATH, {**good, key: value}); self.publish()
                with self.assertRaisesRegex(RuntimeError, "MONITOR_PROOF"):
                    r.rebind("scan", isolated=True)
                self.assertEqual(r.git("rev-parse", "HEAD").stdout, before)

    def test_other_runtime_auxiliary_metadata_still_conflicts(self):
        self.publish_verified_monitor_metadata()
        self.write(self.writer, "research/results/hunter-runtime-watchdog-health.json", {"status":"SUCCESS"})
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "RESEARCH_INPUT_CHANGED.*watchdog"):
            r.rebind("scan", isolated=True)

    def test_locally_mutated_monitor_proof_cannot_be_restored_over_main(self):
        self.publish_verified_monitor_metadata()
        r.rebind("scan", isolated=True)
        pathlib.Path(r.PROOF_PATH).write_text('{"fake":true}')
        with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_RESEARCH_MUTATION"):
            r.rebind("scan", isolated=True)


class MonitorTimingTests(unittest.TestCase):
    def test_observability_preserves_return_and_failure(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.timed("ok", lambda: {"pnl": 12}), {"pnl": 12})
            with self.assertRaisesRegex(ValueError, "failure"):
                m.timed("failure", lambda: (_ for _ in ()).throw(ValueError("failure")))
        rows = [json.loads(line.split(" ", 1)[1]) for line in output.getvalue().splitlines()]
        self.assertEqual([x["stage"] for x in rows], ["ok", "failure"])
        self.assertTrue(all(x["seconds"] >= 0 for x in rows))
