import datetime as dt
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import hunter_job_runner as runner
from research import hunter_scheduler_health as health


class MonitorWorkflowTests(unittest.TestCase):
    def test_completion_in_new_bucket_preserves_admitted_generation(self):
        source = pathlib.Path(__file__).resolve().parents[1]
        with patch.dict(runner.JOBS, {"monitor": ("hunter-position-monitor.yml", "monitor")}):
            step = next(s for s in runner.workflow_steps("monitor")
                        if s.get("name") == "Mark successful generation for atomic persistence")
        now = dt.datetime.now(dt.timezone.utc)
        admitted = health.generation_id(now - dt.timedelta(minutes=6))
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                subprocess.run(["git", *args], cwd=directory, check=True,
                               capture_output=True)
            git("init", "-b", "main")
            git("config", "user.name", "test")
            git("config", "user.email", "test@localhost")
            target = pathlib.Path(directory, "research/results/hunter-scheduler-health.json")
            target.parent.mkdir(parents=True)
            target.write_text("{}")
            git("add", ".")
            git("commit", "-m", "fixture")
            env = {**os.environ, "PYTHONPATH": str(source),
                   "MONITOR_STARTED_AT": (now - dt.timedelta(minutes=6)).isoformat(),
                   "TRIGGER_SOURCE": "GITHUB_BACKUP", "ADMITTED_GENERATION": admitted}
            subprocess.run(["bash", "-euo", "pipefail", "-c", step["run"]],
                           cwd=directory, env=env, check=True, capture_output=True)
            doc = json.loads(target.read_text())
            self.assertEqual(doc["current_generation_id"], admitted)
            self.assertEqual(doc["last_successful_monitor_generation_id"], admitted)
            self.assertGreater(doc["monitor_completed_at_utc"], now.isoformat())
            self.assertEqual(doc["real_order_count"], 0)
