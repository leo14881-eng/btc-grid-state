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

    def test_missing_outputs_prevent_publication(self):
        pathlib.Path(publisher.paths_for('discovery')[0]).unlink()
        with self.assertRaisesRegex(RuntimeError, 'OUTPUT_MISSING'):
            publisher.persist('discovery', self.base)


if __name__ == '__main__':
    unittest.main()
