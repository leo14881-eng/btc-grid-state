import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import hunter_kline_start as start
from scripts import hunter_kline_install as install

GIT_ENV = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'WINDIR') if k in os.environ}
GIT_ENV.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
    GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='init.templateDir', GIT_CONFIG_VALUE_0='')
NOW = dt.datetime(2026, 10, 12, 15, 18, tzinfo=dt.timezone.utc)


def units(active=False):
    return '\n\n'.join('Id='+u+'\nLoadState=loaded\nActiveState='+('active' if active else 'inactive')+
        '\nSubState='+('running' if active else 'dead')+'\nMainPID='+('123' if active else '0')+
        '\nExecMainStatus=0' for u in start.UNITS).encode()


class StartTests(unittest.TestCase):
    def test_plan_only_never_opens_runner_or_network(self):
        with patch.object(sys, 'argv', ['start']), patch.object(start, 'Runner') as runner, \
                patch.object(start, 'probe') as probe, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(start.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['maximum_full_fetches'], 1)
        runner.assert_not_called(); probe.assert_not_called()

    def test_future_stale_and_execution_headroom_are_distinct(self):
        for delta, expected in [(1, 'FUTURE'), (-901, 'STALE'), (-601, 'INSUFFICIENT_HEADROOM'), (-10, 'FRESH')]:
            stamps = dict(bybit=(NOW+dt.timedelta(seconds=delta)).isoformat(), binance=NOW.isoformat())
            self.assertEqual(start.ages(stamps, NOW, 300)['bybit']['status'], expected)

    def test_probe_only_reads_fixed_candidate_prefixes_never_fetches(self):
        runner = Mock()
        runner.run.side_effect = [b'a'*40+b'\trefs/heads/main\n',
            json.dumps(dict(captured_at_utc=NOW.isoformat())).encode(),
            json.dumps(dict(as_of_utc=NOW.isoformat())).encode()]
        with patch.object(start, 'utcnow', return_value=NOW):
            self.assertTrue(start.probe(runner, Path('/fixture')))
        commands = [call.args[1] for call in runner.run.call_args_list]
        self.assertIn('ls-remote', commands[0])
        self.assertFalse(any('fetch' in args for args in commands))
        self.assertTrue(all('/'+'a'*40+'/' in args[-1] and '-I' in args for args in commands[1:]))

    def test_wait_is_rate_limited_and_bounded(self):
        runner = Mock(); current = [0]
        def sleep(seconds): current[0] += seconds
        with patch.object(start, 'probe', return_value=False) as probe:
            with self.assertRaisesRegex(TimeoutError, 'NO_FETCH_NO_START'):
                start.wait_for_natural(runner, Path('/fixture'), seconds=250, clock=lambda: current[0], sleep=sleep)
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(current[0], 250)
        runner.run.assert_not_called()

    def test_wait_returns_on_fresh_publication_without_extra_probe(self):
        runner = Mock(); current = [0]
        with patch.object(start, 'probe', side_effect=[False, True]) as probe:
            start.wait_for_natural(runner, Path('/fixture'), seconds=250, clock=lambda: current[0],
                                   sleep=lambda seconds: current.__setitem__(0, current[0]+seconds))
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(current[0], 120)

    def test_prepare_invokes_verified_installer_with_exact_readback(self):
        runner = Mock()
        start.prepare(runner, Path('/fixture'), 'b'*40)
        args = runner.run.call_args.args
        self.assertEqual(args[0], 'PREPARE_EXACT_READBACK')
        self.assertEqual(args[1][-2:], ['--prepare-readback', 'b'*40])
        self.assertEqual(args[1][3], start.INSTALLER)
        self.assertEqual(args[2], 180)

    def test_noninactive_or_incomplete_service_readback_blocks(self):
        for output in (units(True), units().replace(b'ActiveState=inactive', b'ActiveState=failed'), b'Id=wrong'):
            runner = Mock(); runner.run.return_value = output
            with self.assertRaises(RuntimeError): start.inactive(runner)

    def test_timeout_interrupts_installer_for_child_cleanup_and_retains_stderr(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            runner = start.Runner(directory)
            process = Mock(pid=123, returncode=1)
            process.communicate.return_value = (b'', b'private diagnostic')
            with patch.object(start.time, 'monotonic', side_effect=[0, 181]), \
                 patch('subprocess.Popen', return_value=process), patch.object(os, 'killpg', create=True) as kill:
                with self.assertRaisesRegex(TimeoutError, 'PREPARE_TIMEOUT'):
                    runner.run('PREPARE', ['fixture'], 180)
            kill.assert_called_once_with(123, start.signal.SIGINT)
            self.assertEqual((Path(directory)/'1-PREPARE.stderr').read_bytes(), b'private diagnostic')


class GuardReached(Exception): pass


class LocalGitTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.source = self.root/'source'; self.source.mkdir()
        self.checkout = self.root/'checkout'
        self.g(self.source, 'init', '-b', 'main')
        self.g(self.source, 'config', 'user.name', 'Offline Fixture')
        self.g(self.source, 'config', 'user.email', 'fixture@example.invalid')
        self.old = self.commit(NOW-dt.timedelta(hours=2))
        self.g(self.root, 'clone', '--no-hardlinks', str(self.source), str(self.checkout))
        self.new = self.commit(NOW)
        self.stack.enter_context(patch.dict(start.ENV, GIT_ENV, clear=True))
        self.stack.enter_context(patch.dict(install.ENV, GIT_ENV, clear=True))
        self.stack.enter_context(patch.object(start, 'utcnow', return_value=NOW))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.prepared = []; self.started = False; self.stages = []
        work = self.root/'evidence'; work.mkdir()
        self.runner = start.Runner(work)
        actual_run = self.runner.run
        def run(stage, args, timeout):
            self.stages.append((stage, args))
            if args[0] == 'systemctl':
                if 'start' in args:
                    self.started = True
                    return b''
                return units(self.started)
            return actual_run(stage, args, timeout)
        self.runner.run = run
        self.stack.enter_context(patch.object(start, 'prepare', side_effect=self.actual_guard))

    def g(self, directory, *args):
        result = subprocess.run(['git', '-C', str(directory), *args], env=GIT_ENV,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout.decode().strip()

    def commit(self, now):
        for _, name, key, _ in start.SOURCES:
            path = self.source/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({key: now.isoformat()}))
        self.g(self.source, 'add', '.')
        self.g(self.source, 'commit', '-m', 'synthetic natural snapshot')
        return self.g(self.source, 'rev-parse', 'HEAD')

    def actual_guard(self, runner, repo, readback):
        # Execute the ORIGINAL installer's real rev-parse + exact ref guard.
        # Stop only after that guard, before provisioning/tree/cache operations.
        with patch.object(sys, 'platform', 'linux'), patch.object(os, 'geteuid', return_value=0, create=True), \
                patch.object(install, 'code_entries', side_effect=GuardReached):
            try: install.prepare_readback(repo, readback)
            except GuardReached: self.prepared.append(readback)

    def test_old_sha_only_fetch_reproduces_original_origin_main_guard_failure(self):
        self.g(self.checkout, 'fetch', '--no-tags', 'origin', self.new)
        self.assertEqual(self.g(self.checkout, 'rev-parse', 'FETCH_HEAD'), self.new)
        self.assertEqual(self.g(self.checkout, 'rev-parse', 'origin/main'), self.old)
        with self.assertRaisesRegex(RuntimeError, 'PINNED_ORIGIN_MAIN_READBACK_REQUIRED'):
            self.actual_guard(self.runner, self.checkout, self.new)
        self.assertFalse(self.prepared); self.assertFalse(self.started)

    def test_named_main_fetch_updates_real_guard_and_prepares_same_sha(self):
        selected = start.fetch_prepare_start(self.runner, self.checkout)
        self.assertEqual(selected, self.new)
        self.assertEqual(self.prepared, [self.new]); self.assertTrue(self.started)
        fetches = [args for phase, args in self.stages if phase == 'FETCH_MAIN_ONCE']
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0][-1], 'main:refs/remotes/origin/main')
        self.assertNotIn('--force', fetches[0])

    def test_remote_advance_after_probe_uses_fetched_head_not_probe_head(self):
        candidate = self.new
        self.new = self.commit(NOW-dt.timedelta(seconds=1))
        selected = start.fetch_prepare_start(self.runner, self.checkout)
        self.assertNotEqual(selected, candidate)
        self.assertEqual(selected, self.new); self.assertEqual(self.prepared, [self.new])

    def test_fetched_stale_snapshot_does_not_prepare_or_start(self):
        self.commit(NOW-dt.timedelta(hours=2))
        with self.assertRaisesRegex(RuntimeError, 'FETCHED_SNAPSHOT_NOT_FRESH'):
            start.fetch_prepare_start(self.runner, self.checkout)
        self.assertFalse(self.prepared); self.assertFalse(self.started)

    def test_ref_movement_before_prepare_is_rejected_by_original_guard(self):
        def race(runner, repo, readback):
            self.g(self.checkout, 'update-ref', 'refs/remotes/origin/main', self.old)
            self.actual_guard(runner, repo, readback)
        with patch.object(start, 'prepare', side_effect=race), \
                self.assertRaisesRegex(RuntimeError, 'PINNED_ORIGIN_MAIN_READBACK_REQUIRED'):
            start.fetch_prepare_start(self.runner, self.checkout)
        self.assertFalse(self.prepared); self.assertFalse(self.started)

    def test_ref_movement_after_prepare_preserves_state_without_start(self):
        def race(runner, repo, readback):
            self.actual_guard(runner, repo, readback)
            self.g(self.checkout, 'update-ref', 'refs/remotes/origin/main', self.old)
        with patch.object(start, 'prepare', side_effect=race), \
                self.assertRaisesRegex(RuntimeError, 'ORIGIN_MAIN_CHANGED_AFTER_PREPARE'):
            start.fetch_prepare_start(self.runner, self.checkout)
        self.assertEqual(self.prepared, [self.new]); self.assertFalse(self.started)

    def test_prepare_failure_never_starts_or_retries_fetch(self):
        with patch.object(start, 'prepare', side_effect=RuntimeError('PREPARE_FAILED')), \
                self.assertRaisesRegex(RuntimeError, 'PREPARE_FAILED'):
            start.fetch_prepare_start(self.runner, self.checkout)
        self.assertFalse(self.started)
        self.assertEqual(sum(phase == 'FETCH_MAIN_ONCE' for phase, _ in self.stages), 1)

    def test_non_fast_forward_tracking_ref_is_preserved(self):
        self.g(self.checkout, 'config', 'user.name', 'Offline Fixture')
        self.g(self.checkout, 'config', 'user.email', 'fixture@example.invalid')
        (self.checkout/'unrelated.txt').write_text('concurrent fixture change')
        self.g(self.checkout, 'add', '.')
        self.g(self.checkout, 'commit', '-m', 'concurrent fixture change')
        concurrent = self.g(self.checkout, 'rev-parse', 'HEAD')
        self.g(self.checkout, 'update-ref', 'refs/remotes/origin/main', concurrent)
        with self.assertRaisesRegex(RuntimeError, 'FETCH_MAIN_ONCE_FAILED'):
            start.fetch_prepare_start(self.runner, self.checkout)
        self.assertEqual(self.g(self.checkout, 'rev-parse', 'origin/main'), concurrent)
        self.assertFalse(self.prepared); self.assertFalse(self.started)


if __name__ == '__main__': unittest.main()
