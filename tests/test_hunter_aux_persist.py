import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import hunter_aux_persist as publisher


class AuxPublicationTests(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.remote, self.work, self.other = [root / x for x in ('remote', 'work', 'other')]
        self.run_git('init', '--bare', str(self.remote))
        self.run_git('clone', str(self.remote), str(self.work))
        os.chdir(self.work)
        self.run_git('config', 'user.name', 'test')
        self.run_git('config', 'user.email', 'test@localhost')
        self.run_git('checkout', '-b', 'main')
        for p in publisher.paths_for('discovery'):
            target = pathlib.Path(p); target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{"generation_id":"old"}\n')
        pathlib.Path('stock.txt').write_text('original')
        self.run_git('add', '.')
        self.run_git('commit', '-m', 'baseline')
        self.run_git('push', 'origin', 'main')
        self.base = self.run_git('rev-parse', 'HEAD').stdout.strip()
        self.run_git('clone', '-b', 'main', str(self.remote), str(self.other))
        self.run_git('-C', str(self.other), 'config', 'user.name', 'other')
        self.run_git('-C', str(self.other), 'config', 'user.email', 'other@localhost')
        for p in publisher.paths_for('discovery'):
            pathlib.Path(p).write_text('{"generation_id":"new"}\n')

    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()

    def run_git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True)

    def competing_commit(self, path, content):
        target = self.other / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.run_git('-C', str(self.other), 'add', '.')
        self.run_git('-C', str(self.other), 'commit', '-m', 'competitor')
        self.run_git('-C', str(self.other), 'push', 'origin', 'main')

    def test_disjoint_commit_preserved_and_unrelated_dirty_files_never_published(self):
        self.competing_commit('stock.txt', 'updated')
        pathlib.Path('stock.txt').write_text('uncommitted unrelated mutation')
        commit = publisher.persist('discovery', self.base)
        self.assertTrue(commit)
        self.run_git('fetch', 'origin', 'main')
        self.assertEqual(self.run_git('show', 'origin/main:stock.txt').stdout, 'updated')
        self.assertIn('new', self.run_git('show', 'origin/main:' + publisher.paths_for('discovery')[0]).stdout)

    def test_newer_discovery_cannot_be_overwritten(self):
        self.competing_commit(publisher.paths_for('discovery')[0], '{"generation_id":"newer"}')
        with self.assertRaisesRegex(RuntimeError, 'CAS_REJECTED_STALE_WRITER'):
            publisher.persist('discovery', self.base)
        self.assertIn('newer', self.run_git('show', 'origin/main:' + publisher.paths_for('discovery')[0]).stdout)

    def test_same_job_race_during_push_fails_closed(self):
        original_run = subprocess.run
        raced = False
        def intercept(args, **kwargs):
            nonlocal raced
            if not raced and args[0] == 'git' and 'push' in args and 'HEAD:main' in args:
                raced = True
                self.competing_commit(publisher.paths_for('discovery')[0], '{"generation_id":"winner"}')
            return original_run(args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=intercept):
            with self.assertRaisesRegex(RuntimeError, 'CAS_REJECTED_STALE_WRITER'):
                publisher.persist('discovery', self.base)
        self.assertTrue(raced)

    def test_readback_failure_is_not_success(self):
        original_run = subprocess.run
        def intercept(args, **kwargs):
            if args[0] == 'git' and 'show' in args and args[-1].startswith('origin/main:'):
                return subprocess.CompletedProcess(args, 0, b'corrupt', b'')
            return original_run(args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=intercept):
            with self.assertRaisesRegex(RuntimeError, 'READBACK_MISMATCH'):
                publisher.persist('discovery', self.base)

    def test_checkout_auth_header_inherited_without_logging(self):
        self.run_git('config', '--local', 'http.https://github.com/.extraheader',
                     'AUTHORIZATION: test-private-value')
        original_run = subprocess.run
        verified = []
        def intercept(args, **kwargs):
            if args[0] == 'git' and 'push' in args and 'HEAD:main' in args:
                value = original_run(['git', '-C', args[2], 'config', '--local',
                                      '--get', 'http.https://github.com/.extraheader'],
                                     capture_output=True, text=True, check=True).stdout
                verified.append(value.strip())
            return original_run(args, **kwargs)
        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        with patch.object(subprocess, 'run', side_effect=intercept), redirect_stdout(output):
            publisher.persist('discovery', self.base)
        self.assertEqual(verified, ['AUTHORIZATION: test-private-value'])
        self.assertNotIn('test-private-value', output.getvalue())

    def test_auth_failure_is_not_reported_as_push_race(self):
        original_run = subprocess.run
        def intercept(args, **kwargs):
            if args[0] == 'git' and 'push' in args and 'HEAD:main' in args:
                return subprocess.CompletedProcess(args, 128, '', 'could not read Username')
            return original_run(args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=intercept):
            with self.assertRaisesRegex(RuntimeError, 'AUTHENTICATION_FAILED'):
                publisher.persist('discovery', self.base)

    def test_missing_outputs_prevent_publication(self):
        pathlib.Path(publisher.paths_for('discovery')[0]).unlink()
        with self.assertRaisesRegex(RuntimeError, 'OUTPUT_MISSING'):
            publisher.persist('discovery', self.base)

    def test_unknown_rejection_is_not_retried_or_called_a_race(self):
        original_run = subprocess.run
        pushes = []
        def intercept(args, **kwargs):
            if args[0] == 'git' and 'push' in args and 'HEAD:main' in args:
                pushes.append(args)
                return subprocess.CompletedProcess(args, 1, '', 'remote: unexpected push rejection')
            return original_run(args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=intercept):
            with self.assertRaisesRegex(RuntimeError, 'AUX_PUSH_REJECTED_UNKNOWN') as found:
                publisher.persist('discovery', self.base)
        self.assertEqual(len(pushes), 1)
        self.assertIn('unexpected push rejection', found.exception.stderr_tail)

    def test_policy_rejection_stops_without_retry(self):
        original_run = subprocess.run
        pushes = []
        def intercept(args, **kwargs):
            if args[0] == 'git' and 'push' in args and 'HEAD:main' in args:
                pushes.append(args)
                return subprocess.CompletedProcess(args, 1, '', 'remote: error: GH013 repository rule violations')
            return original_run(args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=intercept):
            with self.assertRaisesRegex(RuntimeError, 'AUX_PUSH_POLICY_REJECTED'):
                publisher.persist('discovery', self.base)
        self.assertEqual(len(pushes), 1)

    def test_exhausted_race_preserves_redacted_diagnostic(self):
        original_run = subprocess.run
        pushes = []
        def intercept(args, **kwargs):
            if args[0] == 'git' and 'push' in args and 'HEAD:main' in args:
                pushes.append(args)
                return subprocess.CompletedProcess(args, 1, '',
                    '[rejected] HEAD -> main (fetch first) token=github_pat_privatevalue')
            return original_run(args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=intercept):
            with self.assertRaisesRegex(RuntimeError, 'AUX_PUSH_RETRY_EXHAUSTED_RACE') as found:
                publisher.persist('discovery', self.base)
        self.assertEqual(len(pushes), 5)
        self.assertIn('fetch first', found.exception.stderr_tail)
        self.assertNotIn('github_pat_privatevalue', found.exception.stderr_tail)

    def test_disjoint_push_race_rebases_and_succeeds(self):
        original_run = subprocess.run
        raced = False
        def intercept(args, **kwargs):
            nonlocal raced
            if not raced and args[0] == 'git' and 'push' in args and 'HEAD:main' in args:
                raced = True
                self.competing_commit('stock.txt', 'concurrent disjoint change')
            return original_run(args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=intercept):
            commit = publisher.persist('discovery', self.base)
        self.assertTrue(commit)
        self.run_git('fetch', 'origin', 'main')
        self.assertEqual(self.run_git('show', 'origin/main:stock.txt').stdout,
                         'concurrent disjoint change')


    def test_health_policy_failure_is_preserved_without_retry(self):
        import datetime as dt
        from scripts import hunter_job_runner as runner
        original_run = subprocess.run
        pushes = []
        def intercept(args, **kwargs):
            if args[0] == 'git' and 'push' in args and 'HEAD:main' in args:
                pushes.append(args)
                return subprocess.CompletedProcess(args, 1, '',
                    'remote: error: GH013 repository rule violations')
            return original_run(args, **kwargs)
        with patch.dict(os.environ, {'RUNNER_TEMP':self.temp.name}), patch.object(
                subprocess, 'run', side_effect=intercept):
            with self.assertRaisesRegex(RuntimeError, 'JOB_HEALTH_PUSH_POLICY_REJECTED') as found:
                runner.publish_health('missed-replay','FAILURE',
                    dt.datetime.now(dt.timezone.utc), [], error='test failure')
        self.assertEqual(len(pushes),1)
        self.assertIn('GH013',runner.exception_details(found.exception)['stderr_tail'])

    def test_health_disjoint_race_preserves_competing_commit_and_reads_back(self):
        import datetime as dt
        import json
        from scripts import hunter_job_runner as runner
        original_run = subprocess.run
        raced = False
        def intercept(args, **kwargs):
            nonlocal raced
            if not raced and args[0] == 'git' and 'push' in args and 'HEAD:main' in args:
                raced = True
                self.competing_commit('stock.txt','health concurrent change')
            return original_run(args, **kwargs)
        with patch.dict(os.environ, {'RUNNER_TEMP':self.temp.name}), patch.object(
                subprocess, 'run', side_effect=intercept):
            runner.publish_health('missed-replay','FAILURE',
                dt.datetime.now(dt.timezone.utc),[],error='test failure')
        self.run_git('fetch','origin','main')
        self.assertEqual(self.run_git('show','origin/main:stock.txt').stdout,'health concurrent change')
        doc=json.loads(self.run_git('show',
            'origin/main:research/results/hunter-runtime-missed-replay-health.json').stdout)
        self.assertEqual(doc['status'],'FAILURE')
        self.assertFalse(doc['main_readback_verified'])

    def test_exact_main_ref_lock_race_is_recognized(self):
        from scripts import hunter_job_runner as runner
        diagnostic="! [remote rejected] HEAD -> main (cannot lock ref 'refs/heads/main': is at "+'a'*40+' but expected '+'b'*40+')'
        result=subprocess.CompletedProcess([],1,'',diagnostic)
        self.assertEqual(runner.push_failure_category(result),'RACE')
        for text in [diagnostic.replace('refs/heads/main','refs/heads/other'),
                     diagnostic.replace('a'*40,'missing'), 'cannot lock ref permission error']:
            self.assertEqual(runner.push_failure_category(subprocess.CompletedProcess([],1,'',text)),
                             'REJECTED_UNKNOWN')
        for prefix, category in [('GH013 repository rule violations\n','POLICY_REJECTED'),
                                 ('Permission denied\n','AUTHENTICATION_FAILED')]:
            self.assertEqual(runner.push_failure_category(subprocess.CompletedProcess([],1,'',prefix+diagnostic)),category)

    def test_main_ref_lock_race_retries_aux_and_health_without_lost_commit(self):
        import datetime as dt
        import json
        from scripts import hunter_job_runner as runner
        original_run=subprocess.run
        raced=False;push_count=0
        def intercept(args,**kwargs):
            nonlocal raced,push_count
            if args[0]=='git' and 'push' in args and 'HEAD:main' in args:
                push_count+=1
                if not raced:
                    raced=True
                    before=self.run_git('ls-remote','origin','refs/heads/main').stdout.split()[0]
                    self.run_git('-C',str(self.other),'fetch','origin','main')
                    self.run_git('-C',str(self.other),'reset','--hard','origin/main')
                    self.competing_commit('stock.txt','preserve ref-lock competitor '+str(push_count))
                    after=self.run_git('ls-remote','origin','refs/heads/main').stdout.split()[0]
                    return subprocess.CompletedProcess(args,1,'',
                        "! [remote rejected] HEAD -> main (cannot lock ref 'refs/heads/main': is at "+after+' but expected '+before+')')
            return original_run(args,**kwargs)
        with patch.dict(os.environ,{'RUNNER_TEMP':self.temp.name}),patch.object(subprocess,'run',side_effect=intercept):
            publisher.persist('discovery',self.base)
            raced=False
            runner.publish_health('blind-replay','SUCCESS',dt.datetime.now(dt.timezone.utc),[])
        self.assertGreaterEqual(push_count,4)
        self.run_git('fetch','origin','main')
        self.assertEqual(self.run_git('show','origin/main:stock.txt').stdout,'preserve ref-lock competitor 3')
        doc=json.loads(self.run_git('show','origin/main:research/results/hunter-runtime-blind-replay-health.json').stdout)
        self.assertEqual(doc['status'],'SUCCESS')
        self.assertTrue(doc['main_readback_verified'])


if __name__ == '__main__':
    unittest.main()
