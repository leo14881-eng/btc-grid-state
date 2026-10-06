import copy
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from scripts import hunter_job_runner as runner


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.now = dt.datetime(2026, 10, 6, 23, 37, tzinfo=dt.timezone.utc)
        self.row = dict(job='blind-replay', status='FAILURE', source='VULTR_SYSTEMD',
            capital_authority='NONE_SHADOW_ONLY', real_trading_enabled=False,
            source_head_sha='old-source', completed_at_utc='2026-10-06T23:31:46+00:00',
            error_details={'failed_step':runner.AUX_RECOVERY_STEPS['blind-replay']},
            steps=[dict(name=runner.AUX_RECOVERY_STEPS['blind-replay'], status='FAILURE',
                stderr_tail='RuntimeError: AUX_CAS_REJECTED_STALE_WRITER scripts/hunter_job_runner.py')])

    def eligible(self, row=None, job='blind-replay'):
        (self.root / ('hunter-runtime-'+job+'-health.json')).write_text(json.dumps(row or self.row))
        return runner.auxiliary_cas_recovery_units(self.root,self.now)

    def test_exact_publication_cas_failure_requests_full_replay(self):
        self.assertEqual(self.eligible(),['hunter-blind-replay.service'])

    def test_missed_replay_same_guard(self):
        row=copy.deepcopy(self.row);row['job']='missed-replay'
        row['steps'][0]['name']=row['error_details']['failed_step']=runner.AUX_RECOVERY_STEPS['missed-replay']
        self.assertEqual(self.eligible(row,'missed-replay'),['hunter-missed-replay.service'])

    def test_success_not_retried(self):
        self.row['status']='SUCCESS';self.assertEqual(self.eligible(),[])

    def test_authentication_failure_not_retried(self):
        self.row['steps'][0]['stderr_tail']='RuntimeError: AUX_PUSH_AUTHENTICATION_FAILED'
        self.assertEqual(self.eligible(),[])

    def test_validator_marker_cannot_request_recovery(self):
        self.row['steps'][0]['name']='Run blind replay';self.assertEqual(self.eligible(),[])

    def test_expired_failure_not_retried(self):
        self.row['completed_at_utc']='2026-10-06T20:31:46+00:00';self.assertEqual(self.eligible(),[])

    def test_future_failure_not_retried(self):
        self.row['completed_at_utc']='2026-10-07T00:00:00+00:00';self.assertEqual(self.eligible(),[])

    def test_timestamp_requires_timezone(self):
        self.row['completed_at_utc']='2026-10-06T23:31:46';self.assertEqual(self.eligible(),[])

    def test_shadow_boundary_required(self):
        self.row['real_trading_enabled']=True;self.assertEqual(self.eligible(),[])

    def test_unknown_authority_rejected(self):
        self.row['capital_authority']='UNKNOWN';self.assertEqual(self.eligible(),[])

    def test_source_must_be_systemd(self):
        self.row['source']='CHATGPT';self.assertEqual(self.eligible(),[])

    def test_health_step_identity_must_match(self):
        self.row['error_details']['failed_step']='Other';self.assertEqual(self.eligible(),[])

    def test_missing_health_is_not_recovery_proof(self):
        self.assertEqual(runner.auxiliary_cas_recovery_units(self.root,self.now),[])

    def test_watchdog_uses_existing_full_job_unit_and_does_not_claim_recovered(self):
        rows=[]
        git_result=type('GitResult',(),{'stdout':'new-main'})()
        with patch.object(runner,'git',return_value=git_result), \
             patch.object(runner,'workflow_steps',return_value=[]), \
             patch.object(runner,'auxiliary_cas_recovery_units',return_value=['hunter-blind-replay.service']), \
             patch.object(runner,'readback'), patch.object(runner.subprocess,'run') as run:
            runner.run_job('watchdog',steps=rows)
        run.assert_called_once_with(['systemctl','start','--no-block','hunter-blind-replay.service'],check=True)
        self.assertEqual(rows[0]['action_status'],'RECOVERY_REQUESTED_NOT_VERIFIED')
        self.assertFalse(rows[0]['recovery_verified'])

    def test_preview_does_not_request_systemd_actions(self):
        with patch.object(runner,'git',return_value=type('Result',(),{'stdout':'sha'})()), \
             patch.object(runner,'workflow_steps',return_value=[]), \
             patch.object(runner,'auxiliary_cas_recovery_units') as check:
            runner.run_job('watchdog',preview=True)
        check.assert_not_called()
