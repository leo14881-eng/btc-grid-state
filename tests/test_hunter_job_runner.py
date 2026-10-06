import datetime as dt
import os
import pathlib
import tempfile
import unittest
import time
from unittest.mock import patch
from scripts import hunter_job_runner as runner
from scripts import hunter_runtime_gate as gate


class RuntimeGateTests(unittest.TestCase):
 def test_backups_remain_active_until_primary_verified(self):
  now=dt.datetime(2026,10,6,tzinfo=dt.timezone.utc)
  healthy={"schema":"hunter_runtime_job_health_v1","job":"research","status":"SUCCESS",
           "completed_at_utc":now.isoformat(),"real_trading_enabled":False,
           "capital_authority":"NONE_SHADOW_ONLY"}
  config={"primary_jobs":{"research":True}}
  self.assertTrue(gate.should_run("research",{},healthy,now))
  self.assertFalse(gate.should_run("research",config,healthy,now))
  for patch_doc in [{},{"status":"FAILURE"},{"real_trading_enabled":True},
                    {"completed_at_utc":(now-dt.timedelta(hours=2)).isoformat()},
                    {"completed_at_utc":(now+dt.timedelta(seconds=1)).isoformat()},
                    {"completed_at_utc":"invalid"},{"job":"discovery"}]:
   doc=patch_doc if not patch_doc else {**healthy,**patch_doc}
   self.assertTrue(gate.should_run("research",config,doc,now))
 def test_each_job_cadence_boundary(self):
  now=dt.datetime(2026,10,6,tzinfo=dt.timezone.utc)
  for job,max_age in gate.MAX_AGE.items():
   h={"schema":"hunter_runtime_job_health_v1","job":job,"status":"SUCCESS",
      "real_trading_enabled":False,"capital_authority":"NONE_SHADOW_ONLY",
      "completed_at_utc":(now-dt.timedelta(seconds=max_age)).isoformat()}
   c={"primary_jobs":{job:True}}
   self.assertFalse(gate.should_run(job,c,h,now))
   self.assertTrue(gate.should_run(job,c,h,now+dt.timedelta(seconds=1)))


class WorkflowAdapterTests(unittest.TestCase):
 def test_all_existing_workflow_steps_supported_without_strategy_rewrite(self):
  for job in runner.JOBS:
   steps=runner.workflow_steps(job)
   self.assertTrue(any("run" in s for s in steps))
   for s in steps:
    for value in s.get("env",{}).values():
     runner.resolve(value,{},"generation","sha")
    if "if" in s:
     runner.condition(s,{"discovery_stale":"true","downstream_stale":"false","monitor_stale":"false"})
 def test_unknown_templates_and_conditions_fail_closed(self):
  with self.assertRaisesRegex(RuntimeError,"UNSUPPORTED"):
   runner.resolve("${{ unexpected.expression }}",{},"g","s")
  with self.assertRaisesRegex(RuntimeError,"UNSUPPORTED"):
   runner.condition({"if":"unknown"}, {})
 def test_secret_resolution_is_only_environment_lookup(self):
  self.assertEqual(runner.resolve("${{ secrets.BYBIT_ALPHA_API_KEY }}",
                                 {"BYBIT_ALPHA_API_KEY":"private"},"g","s"),"private")
  self.assertEqual(runner.resolve("${{ secrets.MISSING }}",{},"g","s"),"")
 def test_auxiliary_jobs_never_publish_portfolios(self):
  for job in ("discovery","watchdog","blind-replay","missed-replay"):
   self.assertFalse(set(runner.PORTFOLIOS)&set(runner.output_paths(job)))
 def test_preview_cannot_run_persistence_or_server_self_heal(self):
  with tempfile.TemporaryDirectory() as d:
   pathlib.Path(d,"hunter-cex-universe-run.json").write_text('{"generation_id":"g"}')
   env={"GITHUB_OUTPUT":str(pathlib.Path(d,"out")),"GITHUB_ENV":str(pathlib.Path(d,"env"))}
   with patch.dict(os.environ,env),patch.object(runner,"RESULTS",pathlib.Path(d)),\
        patch.object(runner,"git") as git,patch.object(runner,"workflow_steps",return_value=[
         {"name":"Persist results","run":"git push origin HEAD:main"},
         {"name":"Self-heal stale monitor","run":"systemctl start monitor"}]),\
        patch.object(runner,"run_shell") as process,patch.object(runner,"readback") as readback:
    git.return_value.stdout="sha"
    self.assertEqual(runner.run_job("research",preview=True),[])
    process.assert_not_called();readback.assert_not_called()
 def test_required_step_failure_never_reaches_persistence(self):
  with tempfile.TemporaryDirectory() as d:
   pathlib.Path(d,"hunter-cex-universe-run.json").write_text('{"generation_id":"g"}')
   env={"GITHUB_OUTPUT":str(pathlib.Path(d,"out")),"GITHUB_ENV":str(pathlib.Path(d,"env"))}
   with patch.dict(os.environ,env),patch.object(runner,"RESULTS",pathlib.Path(d)),\
        patch.object(runner,"git") as git,patch.object(runner,"workflow_steps",return_value=[
         {"name":"Run replay","run":"false"},
         {"name":"Persist replay","if":"always()","run":"git push"}]),\
        patch.object(runner,"run_shell") as process,patch.object(runner,"readback") as readback:
    git.return_value.stdout="sha";process.return_value.returncode=1
    with self.assertRaisesRegex(RuntimeError,"JOB_STEP_FAILED"):runner.run_job("blind-replay")
    self.assertEqual(process.call_count,1);readback.assert_not_called()

class CriticalTimeoutTests(unittest.TestCase):
 def test_timeout_terminates_process_group(self):
  started=time.monotonic()
  with self.assertRaisesRegex(RuntimeError,'CRITICAL_PHASE_TIMEOUT'):
   runner.run_shell("sleep 5",dict(os.environ),'.',0.05)
  self.assertLess(time.monotonic()-started,2)
