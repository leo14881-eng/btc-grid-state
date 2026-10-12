import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import hunter_kline_install as install


def archive(name='research/example.py', kind=tarfile.REGTYPE):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.mode = 0o777
        info.size = 1 if kind == tarfile.REGTYPE else 0
        tar.addfile(info, io.BytesIO(b'x') if info.size else None)
    return out.getvalue()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = root
        for field, name in [('PREFIX_ROOT', 'opt'), ('STATE_ROOT', 'state'), ('UNIT_ROOT', 'units')]:
            directory = root / name
            directory.mkdir()
            self.stack.enter_context(patch.object(install, field, directory))
        self.entries = [('research/example.py', b'code', False)] + [
            (f'deploy/systemd/hunter-{v}-stream.service', b'fixture unit\n', False) for v in install.VENUES]
        self.calls = []
        self.stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('NO_REAL_PROCESS')))
        self.stack.enter_context(patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('NO_NETWORK')))
        self.accounts = SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_uid=123),
                                        getgrnam=lambda _: SimpleNamespace(gr_gid=456))
        self.stack.enter_context(patch.dict(sys.modules, pwd=self.accounts, grp=self.accounts))
        self.chown = self.stack.enter_context(patch.object(os, 'chown', create=True))
        self.wheel = b'fixture wheel bytes'
        self.fail = None
        self.readback = 'b' * 40

    def fake_command(self, args, **kwargs):
        args = [str(a) for a in args]
        self.calls.append((args, kwargs))
        if self.fail and self.fail(args):
            raise RuntimeError('INJECTED_FAILURE')
        if args[0] == 'git':
            if args[3] == 'rev-parse': return self.readback
            if args[3] == 'show': return b'{"fixture":true}'
            raise AssertionError(args)
        if 'venv' in args and '-m' in args:
            py = Path(args[-1]) / 'bin/python'
            py.parent.mkdir(parents=True)
            py.write_bytes(b'fixture interpreter')
        if args[0] == 'runuser' and args[-1] == 'initialize':
            state = install.STATE_ROOT / args[2]
            with (state / 'state.sqlite3').open('xb') as file:
                file.write(b'initialized fixture; preserve me')
        if args[0] == 'systemctl':
            return 'inactive' if 'ActiveState' in args else 'not-found'
        return ''

    def applying(self):
        self.stack.enter_context(patch.object(install, 'preflight', return_value=self.entries))
        self.stack.enter_context(patch.object(install, 'command', side_effect=self.fake_command))
        self.stack.enter_context(patch.object(install, 'wheel_bytes', return_value=self.wheel))
        self.stack.enter_context(patch.object(install, 'WHEEL_SHA256', hashlib.sha256(self.wheel).hexdigest()))
        self.stack.enter_context(patch.object(install, 'check_immutable_tree'))

    def host(self):
        self.stack.enter_context(patch.object(sys, 'platform', 'linux'))
        self.stack.enter_context(patch.object(os, 'geteuid', return_value=0, create=True))
        self.stack.enter_context(patch('shutil.which', return_value='/usr/bin/fixture'))
        self.stack.enter_context(patch.object(Path, 'lstat', return_value=SimpleNamespace(st_uid=0, st_mode=0o40755)))
        real_is_file = Path.is_file
        self.stack.enter_context(patch.object(Path, 'is_file', lambda p: True if p == Path('/usr/sbin/nologin') else real_is_file(p)))
        self.stack.enter_context(patch.object(install, 'command', side_effect=self.fake_command))
        self.stack.enter_context(patch.object(install, 'code_entries', return_value=self.entries))
        def absent(_): raise KeyError
        self.accounts.getpwnam = self.accounts.getgrnam = absent

    def preparing(self):
        self.applying()
        with contextlib.redirect_stdout(io.StringIO()): install.apply(self.root)
        self.calls.clear()
        self.stack.enter_context(patch.object(sys, 'platform', 'linux'))
        self.stack.enter_context(patch.object(os, 'geteuid', return_value=0, create=True))
        self.stack.enter_context(patch.object(install, 'code_entries', return_value=self.entries))

    def test_default_plan_has_no_commands_or_writes(self):
        before = sorted(str(p) for p in self.root.rglob('*'))
        with patch.object(sys, 'argv', ['installer']), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(install.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'PLAN_ONLY_NO_MUTATIONS')
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))

    def test_archive_traversal_and_links_are_rejected(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('bad\\path', tarfile.REGTYPE), ('link', tarfile.SYMTYPE), ('device', tarfile.CHRTYPE)]:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                install.archive_entries(archive(name, kind))

    def test_extraction_writes_only_code_and_readonly_modes(self):
        prefix = install.PREFIX_ROOT / 'fresh'
        prefix.mkdir()
        install.extract_code(prefix, self.entries)
        self.assertEqual((prefix / 'code/research/example.py').read_bytes(), b'code')
        self.assertEqual((prefix / 'release-head').read_text().strip(), install.RELEASE)
        self.assertFalse((prefix / 'code/research/results').exists())
        self.assertEqual((prefix / 'code/research/example.py').stat().st_mode & 0o222, 0)

    def test_wheel_digest_rejects_changed_bytes(self):
        with self.assertRaisesRegex(RuntimeError, 'SHA256_MISMATCH'):
            install.check_wheel(b'changed')

    def test_fresh_preflight_uses_no_mutating_commands(self):
        self.host()
        self.assertEqual(install.preflight(self.root), self.entries)
        self.assertTrue(all(a[:3] == ['systemctl', '--no-pager', 'show'] for a, _ in self.calls))

    def test_existing_accounts_and_groups_stop(self):
        self.host()
        for field in ('getpwnam', 'getgrnam'):
            with patch.object(self.accounts, field, return_value=object()), self.assertRaisesRegex(RuntimeError, 'EXISTING_'):
                install.preflight(self.root)
        self.assertEqual(self.calls, [])

    def test_existing_destination_or_dangling_link_is_preserved(self):
        self.host()
        for path in install.target('bybit')[2:]:
            with patch('os.path.lexists', side_effect=lambda p: p == path), self.assertRaisesRegex(RuntimeError, 'EXISTING_PATH_PRESERVED'):
                install.preflight(self.root)
        self.assertEqual(self.calls, [])

    def test_existing_vendor_or_runtime_unit_stops(self):
        self.host()
        with patch.object(install, 'command', return_value='loaded'), self.assertRaisesRegex(RuntimeError, 'EXISTING_UNIT'):
            install.preflight(self.root)

    def test_install_is_offline_after_verified_wheel_and_never_starts(self):
        self.applying()
        with contextlib.redirect_stdout(io.StringIO()) as output: install.apply(self.root)
        self.assertIn('INSTALLED_NOT_STARTED', output.getvalue())
        for venue in install.VENUES:
            user, group, prefix, state, unit = install.target(venue)
            self.assertTrue((state / 'state.sqlite3').is_file())
            self.assertEqual(unit.read_bytes(), b'fixture unit\n')
            self.assertIn((['groupadd', '--system', group], {}), self.calls)
        flattened = [word for args, _ in self.calls for word in args]
        for forbidden in ('start', 'enable', 'restart', 'reset-failed', 'chmod', 'chown', 'usermod', 'apt', '--index-url'):
            self.assertNotIn(forbidden, flattened)
        installs = [args for args, _ in self.calls if 'pip' in args]
        self.assertEqual(len(installs), 2)
        self.assertTrue(all('--no-index' in a and '--no-deps' in a for a in installs))
        self.assertEqual(self.calls[-1][0], ['systemctl', '--no-pager', 'daemon-reload'])
        self.assertEqual(self.chown.call_count, 2)

    def test_hash_failure_precedes_all_provisioning(self):
        self.applying()
        with patch.object(install, 'WHEEL_SHA256', '0' * 64), self.assertRaisesRegex(RuntimeError, 'SHA256'):
            install.apply(self.root)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(install.PREFIX_ROOT.iterdir()), [])

    def test_partial_initialization_failure_preserves_first_state_and_stops(self):
        self.applying()
        self.fail = lambda a: a[0] == 'runuser' and a[2] == 'hunter-binance-stream'
        with self.assertRaisesRegex(RuntimeError, 'INJECTED_FAILURE'): install.apply(self.root)
        state = install.target('bybit')[3] / 'state.sqlite3'
        self.assertEqual(state.read_bytes(), b'initialized fixture; preserve me')
        self.assertFalse(any('daemon-reload' in a for a, _ in self.calls))
        self.assertTrue(install.target('binance')[2].is_dir())

    def test_prepare_refuses_code_sha_as_readback(self):
        self.preparing()
        with self.assertRaisesRegex(RuntimeError, 'NOT_CODE_SHA'):
            install.prepare_readback(self.root, install.RELEASE)
        self.assertEqual(self.calls, [])

    def test_prepare_binds_both_snapshots_to_readback_not_release(self):
        self.preparing()
        with contextlib.redirect_stdout(io.StringIO()): install.prepare_readback(self.root, self.readback)
        writes = [args for args, _ in self.calls if args[0] == 'runuser']
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(a[-2:] == ['--source-head', self.readback] for a in writes))
        validations = [i for i, (a, kw) in enumerate(self.calls) if kw.get('input_data')]
        first_write = next(i for i, (a, _) in enumerate(self.calls) if a[0] == 'runuser')
        self.assertEqual(len(validations), 2)
        self.assertLess(max(validations), first_write)

    def test_second_snapshot_validation_failure_writes_neither_manifest(self):
        self.preparing()
        self.fail = lambda a: '-c' in a and 'binance' in a[a.index('-c') + 1]
        with self.assertRaisesRegex(RuntimeError, 'INJECTED_FAILURE'):
            install.prepare_readback(self.root, self.readback)
        self.assertFalse(any(a[0] == 'runuser' for a, _ in self.calls))

    def test_running_collector_blocks_preparation(self):
        self.preparing()
        normal = self.fake_command
        def active(args, **kwargs):
            if args[0] == 'systemctl': return 'active'
            return normal(args, **kwargs)
        with patch.object(install, 'command', side_effect=active), self.assertRaisesRegex(RuntimeError, 'MUST_BE_INACTIVE'):
            install.prepare_readback(self.root, self.readback)
        self.assertFalse(any(a[0] == 'runuser' for a, _ in self.calls))

    def test_changed_code_blocks_preparation(self):
        self.preparing()
        path = install.target('bybit')[2] / 'code/research/example.py'
        path.chmod(0o644)
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'INSTALLED_CODE_MISMATCH'):
            install.prepare_readback(self.root, self.readback)
        self.assertFalse(any(a[0] == 'runuser' for a, _ in self.calls))

    def test_stale_readback_ref_stops_before_snapshot_processing(self):
        self.preparing()
        with self.assertRaisesRegex(RuntimeError, 'PINNED_ORIGIN_MAIN'):
            install.prepare_readback(self.root, 'c' * 40)
        self.assertEqual(len(self.calls), 1)

    def test_child_failure_does_not_print_stderr_or_environment(self):
        secret = b'not-for-output'
        process = Mock(returncode=1)
        process.communicate.return_value = (b'', secret)
        with patch('subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, '^COMMAND_FAILED fixture exit=1$'):
                install.command(['fixture'])
        self.assertEqual(install.ENV['PIP_CONFIG_FILE'], os.devnull)
        self.assertEqual(install.ENV['GIT_CONFIG_GLOBAL'], os.devnull)
        self.assertNotIn('HOME', install.ENV)

    def test_child_timeout_kills_group_and_reaps_before_return(self):
        process = Mock(pid=12345)
        process.communicate.side_effect = [subprocess.TimeoutExpired('fixture', 180), (b'', b'')]
        with patch('subprocess.Popen', return_value=process) as popen, \
             patch.object(sys, 'platform', 'linux'), patch.object(os, 'killpg', create=True) as kill, \
             patch.object(install.signal, 'SIGKILL', 9, create=True):
            with self.assertRaisesRegex(RuntimeError, 'GROUP_REAPED'):
                install.command(['fixture'])
            kill.assert_called_once_with(12345, 9)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual(process.communicate.call_count, 2)

    def test_source_archive_uses_only_pinned_code_and_units(self):
        names = ['research/example.py', 'research/results/ledger.json', 'hunter-candidate-ledger.jsonl']
        names += [n for n, _, _ in self.entries if n.startswith('deploy/')]
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as tar:
            for name, data, _ in self.entries:
                info = tarfile.TarInfo(name); info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with patch.object(install, 'command', side_effect=[install.RELEASE, '\n'.join(names), out.getvalue()]) as run:
            entries = install.code_entries(self.root)
        self.assertEqual(entries, self.entries)
        request = run.call_args.args[0]
        self.assertIn(install.RELEASE, request)
        self.assertFalse(any('ledger' in str(a) or 'results' in str(a) for a in request))

    def test_archive_omission_cannot_silently_stage_partial_code(self):
        with patch.object(install, 'command', side_effect=[install.RELEASE, 'research/expected.py', archive()]):
            with self.assertRaisesRegex(RuntimeError, 'FILE_SET_MISMATCH'):
                install.code_entries(self.root)

    def test_immutable_tree_rejects_group_writes_or_nonroot_owner(self):
        prefix = install.PREFIX_ROOT
        for uid, mode in [(1000, 0o40755), (0, 0o40775)]:
            with patch.object(Path, 'rglob', return_value=[]), \
                 patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=uid, st_mode=mode)), \
                 self.assertRaisesRegex(RuntimeError, 'WRITABLE_INSTALL_TREE'):
                install.check_immutable_tree(prefix)


if __name__ == '__main__': unittest.main()
