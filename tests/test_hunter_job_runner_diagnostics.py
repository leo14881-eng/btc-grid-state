import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import hunter_job_runner as runner


class DiagnosticsTests(unittest.TestCase):
 def setUp(self):
  self.temp = tempfile.TemporaryDirectory()
  self.root = pathlib.Path(self.temp.name)
  (self.root / 'hunter-cex-universe-run.json').write_text('{"generation_id":"g"}')
  (self.root / 'hunter-shadow-rules.json').write_text(json.dumps({
    'lanes': {'V2': {'capital_pool_usdt': 20000}},
    'capital_authority': 'NONE_SHADOW_ONLY',
    'tail_risk_phase1': {'real_trading_enabled': False}}))
  self.environment = {'GITHUB_OUTPUT': str(self.root/'out'), 'GITHUB_ENV': str(self.root/'env')}
  self.stack = contextlib.ExitStack()
  self.stack.enter_context(mock.patch.dict(os.environ, self.environment))
  self.stack.enter_context(mock.patch.object(runner, 'RESULTS', self.root))
  self.git = self.stack.enter_context(mock.patch.object(runner, 'git'))
  self.git.return_value.stdout = 'sha'

 def tearDown(self):
  self.stack.close(); self.temp.cleanup()

 def test_real_shell_failure_retains_completed_and_failed_steps_in_main_health(self):
  steps = [{'name': 'First completed step', 'run': 'true'},
           {'name': 'Actual failing validator', 'run': 'echo REAL_GUARD_REJECTION >&2; exit 7'},
           {'name': 'Persist results', 'run': 'true'}]
  with mock.patch.object(runner, 'workflow_steps', return_value=steps), \
       mock.patch.object(runner, 'enabled', return_value=True), \
       mock.patch.object(runner, 'publish_health') as publish, \
       mock.patch.object(runner, 'readback') as readback, \
       mock.patch('sys.argv', ['runner', 'research']), contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaisesRegex(RuntimeError, 'JOB_STEP_FAILED'): runner.main()
  args = publish.call_args.args
  self.assertEqual(args[1], 'FAILURE')
  rows = args[3]
  self.assertEqual([r['name'] for r in rows], ['First completed step', 'Actual failing validator'])
  self.assertEqual(rows[0]['status'], 'SUCCESS')
  self.assertEqual(rows[1]['status'], 'FAILURE')
  self.assertEqual(rows[1]['exit'], 7)
  self.assertIn('REAL_GUARD_REJECTION', rows[1]['stderr_tail'])
  self.assertEqual(args[5]['failed_step'], 'Actual failing validator')
  readback.assert_not_called()

 def test_readback_failure_is_recorded_and_never_published_as_success(self):
  with mock.patch.object(runner, 'workflow_steps', return_value=[{'name':'Completed', 'run':'true'}]), \
       mock.patch.object(runner, 'enabled', return_value=True), \
       mock.patch.object(runner, 'publish_health') as publish, \
       mock.patch.object(runner, 'readback', side_effect=RuntimeError('JOB_MAIN_READBACK_MISMATCH file')), \
       mock.patch('sys.argv', ['runner', 'research']), contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaisesRegex(RuntimeError, 'READBACK_MISMATCH'): runner.main()
  args = publish.call_args.args
  self.assertEqual(args[1], 'FAILURE')
  self.assertEqual(args[3][-1]['status'], 'FAILURE')
  self.assertEqual(args[5]['failed_step'], 'Authoritative main read-back')

 def test_timeout_retains_active_step_with_exact_guard_code(self):
  rows = []
  with mock.patch.object(runner, 'workflow_steps', return_value=[{'name':'Locked critical step', 'run':'sleep 5'}]), \
       mock.patch.object(runner, 'run_shell', side_effect=RuntimeError('PORTFOLIO_CRITICAL_PHASE_TIMEOUT')), \
       contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaisesRegex(RuntimeError, 'CRITICAL_PHASE_TIMEOUT'):
    runner.run_job('research', steps=rows)
  self.assertEqual(rows[-1]['status'], 'FAILURE')
  self.assertIsNone(rows[-1]['exit'])
  self.assertEqual(rows[-1]['error_details']['message'], 'PORTFOLIO_CRITICAL_PHASE_TIMEOUT')

 def test_real_stderr_redaction_and_bounded_capture(self):
  env = dict(os.environ, BYBIT_ALPHA_API_KEY='private-api-example', GITHUB_TOKEN='ghp_private-example')
  code = 'printf "%s\\n" "$BYBIT_ALPHA_API_KEY" "$GITHUB_TOKEN" >&2; echo SAFE_ERROR_CODE >&2; exit 3'
  out = io.StringIO()
  with contextlib.redirect_stdout(out): result = runner.run_shell(code, env, '.', 1)
  self.assertEqual(result.returncode, 3)
  self.assertIn('SAFE_ERROR_CODE', result.stderr)
  self.assertNotIn(env['BYBIT_ALPHA_API_KEY'], result.stderr + out.getvalue())
  self.assertNotIn(env['GITHUB_TOKEN'], result.stderr + out.getvalue())
  with contextlib.redirect_stdout(io.StringIO()):
   result = runner.run_shell("python -c 'import sys;sys.stderr.write(\"x\"*100000)'", env, '.', 2)
  self.assertLessEqual(len(result.stderr), 2048)

 def test_called_process_error_never_exports_raw_command(self):
  exc = subprocess.CalledProcessError(1, ['bash','-c','secret-command-with-credentials'], stderr='SAFE_GIT_FAILURE')
  detail = runner.exception_details(exc)
  self.assertNotIn('secret-command', json.dumps(detail))
  self.assertIn('SAFE_GIT_FAILURE', detail['stderr_tail'])

 def test_url_and_authorization_redaction(self):
  text = 'https://user:pass@example.com?api_key=secret Authorization: Bearer unseen-token'
  safe = runner.safe_diagnostic(text, {})
  for value in ['user:pass', '=secret', 'unseen-token']: self.assertNotIn(value, safe)
  self.assertIn('example.com', safe)

 def test_continue_on_error_is_unchanged_but_failure_is_visible(self):
  steps = [{'name':'Optional probe', 'run':'exit 4', 'continue-on-error':True},
           {'name':'Next step', 'run':'true'}]
  with mock.patch.object(runner, 'workflow_steps', return_value=steps), contextlib.redirect_stdout(io.StringIO()):
   rows = runner.run_job('research', preview=True)
  self.assertEqual(rows[0]['status'], 'CONTINUED_AFTER_ERROR')
  self.assertEqual(rows[0]['exit'], 4)
  self.assertEqual(rows[1]['status'], 'SUCCESS')

 def test_actual_readback_records_exact_conflicting_path(self):
  path = self.root / 'hunter-forward-research.json'
  path.write_text('expected')
  self.git.return_value.returncode = 0
  for returncode, code in [(0, 'JOB_MAIN_READBACK_MISMATCH'), (1, 'JOB_MAIN_READBACK_SOURCE_FAILED')]:
   result = subprocess.CompletedProcess([], returncode, stdout=b'different', stderr=b'SOURCE_DETAIL')
   with mock.patch.object(runner.subprocess, 'run', return_value=result):
    with self.assertRaisesRegex(RuntimeError, code) as raised: runner.readback('research')
   detail = runner.exception_details(raised.exception)
   self.assertEqual(detail['failed_path'], str(path))
   self.assertIn('SOURCE_DETAIL', detail['stderr_tail'])
