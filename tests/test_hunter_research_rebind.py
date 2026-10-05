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
            from scripts.hunter_monitor_persist import cas
            cas(base)

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

    def test_invalid_monitor_snapshot_rejected(self):
        self.write(self.writer, "research/results/hunter-shadow-v2-summary.json", {"mode": "REAL"})
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "MODE_MISMATCH"):
            r.rebind("scan", isolated=True)

    def test_cannot_reset_non_ci_or_unconfirmed_checkout(self):
        with self.assertRaisesRegex(RuntimeError, "DISPOSABLE_CI"):
            r.rebind("scan")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
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


class MonitorTimingTests(unittest.TestCase):
    def test_observability_preserves_return_and_failure(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.timed("ok", lambda: {"pnl": 12}), {"pnl": 12})
            with self.assertRaisesRegex(ValueError, "failure"):
                m.timed("failure", lambda: (_ for _ in ()).throw(ValueError("failure")))
        rows = [json.loads(line.split(" ", 1)[1]) for line in output.getvalue().splitlines()]
        self.assertEqual([x["stage"] for x in rows], ["ok", "failure"])
        self.assertTrue(all(x["seconds"] >= 0 for x in rows))
