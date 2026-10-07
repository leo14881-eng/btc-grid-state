import copy
import datetime as dt
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from scripts import hunter_local_job_health as local
from scripts import hunter_job_runner as runner


class LocalJobHealthTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory()
  self.addCleanup(self.temp.cleanup)
  self.root=pathlib.Path(self.temp.name)
  self.main=self.root/'main';self.main.mkdir()
  self.disk=self.root/'local'
  self.environment=patch.dict(os.environ,{'HUNTER_LOCAL_JOB_HEALTH_DIR':str(self.disk)})
  self.environment.start();self.addCleanup(self.environment.stop)
  self.now=dt.datetime(2026,10,7,18,30,tzinfo=dt.timezone.utc)
  self.doc=dict(schema='hunter_runtime_job_health_v1',job='missed-replay',status='FAILURE',
     source='VULTR_SYSTEMD',capital_authority='NONE_SHADOW_ONLY',real_trading_enabled=False,
     source_head_sha='source',started_at_utc='2026-10-07T18:20:00+00:00',
     completed_at_utc='2026-10-07T18:21:00+00:00',
     error_details={'failed_step':'Persist replay only'},
     steps=[dict(name='Persist replay only',status='FAILURE',
                 stderr_tail='RuntimeError: AUX_PUSH_RETRY_EXHAUSTED_RACE')])
 def units(self):
  return runner.auxiliary_cas_recovery_units(self.main,self.now,local_results=self.disk)
 def main_row(self,doc):
  (self.main/'hunter-runtime-missed-replay-health.json').write_text(json.dumps(doc))
 def test_failure_survives_missing_github_health(self):
  path=local.write('missed-replay',self.doc)
  self.assertEqual(self.units(),['hunter-missed-replay.service'])
  row=json.loads(path.read_text())
  self.assertTrue(row['local_publication_only'])
  self.assertFalse(row['github_health_publication_verified'])
  self.assertEqual(path.stat().st_mode&0o777,0o600)
 def test_newer_main_success_suppresses_local_failure(self):
  local.write('missed-replay',self.doc)
  newer=dict(self.doc,status='SUCCESS',started_at_utc='2026-10-07T18:25:00+00:00')
  self.main_row(newer)
  self.assertEqual(self.units(),[])
 def test_newer_local_success_suppresses_main_failure(self):
  self.main_row(self.doc)
  local.write('missed-replay',dict(self.doc,status='SUCCESS',
             started_at_utc='2026-10-07T18:25:00+00:00'))
  self.assertEqual(self.units(),[])
 def test_old_local_writer_cannot_replace_newer_success(self):
  local.write('missed-replay',dict(self.doc,status='SUCCESS',
             started_at_utc='2026-10-07T18:25:00+00:00'))
  with self.assertRaisesRegex(RuntimeError,'STALE_WRITER'):
   local.write('missed-replay',self.doc)
  self.assertEqual(self.units(),[])
 def test_same_run_conflicting_status_refused(self):
  local.write('missed-replay',self.doc)
  with self.assertRaisesRegex(RuntimeError,'CONFLICT'):
   local.write('missed-replay',dict(self.doc,status='SUCCESS'))
 def test_auth_policy_unknown_not_automatically_retried(self):
  for reason in ('AUX_PUSH_AUTHENTICATION_FAILED','AUX_PUSH_POLICY_REJECTED',
                 'AUX_PUSH_REJECTED_UNKNOWN','AUX_PUSH_RETRY_EXHAUSTED'):
   with self.subTest(reason=reason):
    row=copy.deepcopy(self.doc)
    row['steps'][0]['stderr_tail']='RuntimeError: '+reason
    local.write('missed-replay',row)
    self.assertEqual(self.units(),[])
 def test_transport_exhaustion_eligible(self):
  row=copy.deepcopy(self.doc)
  row['steps'][0]['stderr_tail']='RuntimeError: AUX_PUSH_RETRY_EXHAUSTED_TRANSPORT_FAILED'
  local.write('missed-replay',row)
  self.assertEqual(self.units(),['hunter-missed-replay.service'])
 def test_stale_future_or_wrong_step_not_retried(self):
  cases=[dict(completed_at_utc='2026-10-07T15:00:00+00:00'),
         dict(completed_at_utc='2026-10-07T19:00:00+00:00'),
         dict(error_details={'failed_step':'Validate replay output'})]
  for changes in cases:
   with self.subTest(changes=changes):
    local.write('missed-replay',dict(self.doc,**changes))
    self.assertEqual(self.units(),[])
 def test_shadow_boundaries_and_job_path_required(self):
  for changes in (dict(real_trading_enabled=True),dict(capital_authority='UNKNOWN'),
                  dict(job='../portfolio'),dict(source='CHATGPT')):
   with self.subTest(changes=changes):
    with self.assertRaisesRegex(RuntimeError,'INVALID'):
     local.write('missed-replay',dict(self.doc,**changes))
 def test_marker_in_arbitrary_message_not_execution_proof(self):
  row=copy.deepcopy(self.doc)
  row['steps'][0]['stderr_tail']='some message RuntimeError: AUX_PUSH_RETRY_EXHAUSTED_RACE'
  local.write('missed-replay',row)
  self.assertEqual(self.units(),[])
 def test_partial_json_not_recovery_proof(self):
  self.disk.mkdir()
  (self.disk/'hunter-runtime-missed-replay-health.json').write_text('{')
  self.assertEqual(self.units(),[])
