"""Offline readback boundaries; real Linux objects exist only in private scratch."""
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('readback', Path(__file__).resolve().parents[1] /
                                            'scripts/hunter_binance_predeploy_readback.py')
readback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readback)


class ReadbackBoundaries(unittest.TestCase):
    def test_mutating_and_arbitrary_commands_are_rejected_before_spawn(self):
        with patch.object(readback.subprocess, 'run', side_effect=AssertionError('must not spawn')):
            for args in (['systemctl', 'restart', 'hunter-binance-stream.service'],
                         ['systemctl', '--no-pager', 'show', 'stock-monitor.service'],
                         ['sh', '-c', 'id'], ['getent', 'shadow'],
                         ['systemctl', '--no-pager', 'show', readback.UNITS[0]],
                         ['getent', 'passwd', 'root'], ['timedatectl', 'set-ntp', 'true'],
                         ['getent', 'passwd', 'hunter-binance-stream', '--help']):
                with self.assertRaises(ValueError): readback.command(args)

    def test_fixed_commands_have_clean_environment_and_bounded_subprocess_options(self):
        commands = [['systemctl', '--no-pager', 'show', unit] +
                    [x for prop in readback.PROPERTIES for x in ('-p', prop)] for unit in readback.UNITS]
        commands += [['getent', 'passwd', 'hunter-binance-stream'],
                     ['getent', 'group', 'hunter-binance-data'],
                     ['timedatectl', 'show', '-p', 'NTPSynchronized']]
        result = subprocess.CompletedProcess([], 0, stdout=b'ok', stderr=b'private-stderr')
        with patch.dict(os.environ, {'SECRET_TOKEN': 'hidden', 'LD_PRELOAD': '/private.so'}), \
                patch.object(readback.subprocess, 'run', return_value=result) as run:
            for args in commands:
                self.assertEqual(readback.command(args), b'ok')
                actual, options = run.call_args
                self.assertEqual(actual, (args,))
                self.assertEqual(options['env'], {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                                                  'LANG': 'C.UTF-8', 'SYSTEMD_PAGER': ''})
                self.assertEqual(options['stdin'], subprocess.DEVNULL)
                self.assertEqual(options['stdout'], subprocess.PIPE)
                self.assertEqual(options['stderr'], subprocess.PIPE)
                self.assertEqual(options['timeout'], 10)
                self.assertNotIn('shell', options)

    def test_fixed_show_rejects_added_reordered_or_missing_properties(self):
        command = ['systemctl', '--no-pager', 'show', readback.UNITS[0]] + [
            x for prop in readback.PROPERTIES for x in ('-p', prop)]
        changed = list(command); changed[-1], changed[-3] = changed[-3], changed[-1]
        with patch.object(readback.subprocess, 'run', side_effect=AssertionError('must not spawn')):
            for args in (command + ['-p', 'PassEnvironment'], command[:-2], changed):
                with self.assertRaises(ValueError): readback.command(args)

    def test_command_failure_oversize_and_timeout_do_not_export_stderr(self):
        args = ['getent', 'passwd', 'hunter-binance-stream']
        for result in (subprocess.CompletedProcess(args, 1, stdout=b'', stderr=b'SECRET'),
                       subprocess.CompletedProcess(args, 0, stdout=b'12345', stderr=b'SECRET')):
            with patch.object(readback, 'BYTE_LIMIT', 4), \
                    patch.object(readback.subprocess, 'run', return_value=result):
                with self.assertRaises(ValueError) as raised: readback.command(args)
                self.assertNotIn('SECRET', str(raised.exception))
        with patch.object(readback.subprocess, 'run', side_effect=subprocess.TimeoutExpired(args, 10)):
            with self.assertRaises(subprocess.TimeoutExpired): readback.command(args)

    def test_selector_whitelist_does_not_export_secrets_or_execstart(self):
        value = readback.sanitize_show(b'User=shadow\nEnvironment=SECRET_TOKEN=hidden HUNTER_BINANCE_KLINE_SOURCE=REST HUNTER_SERVER_ISOLATED_CHECKOUT=1\nExecStart=/bin/tool --token hidden\nEnvironmentFiles=/secret/env\n')
        self.assertNotIn('hidden', json.dumps(value))
        self.assertNotIn('/secret/env', json.dumps(value))
        self.assertEqual(value['configured_selector']['HUNTER_BINANCE_KLINE_SOURCE'], 'REST')
        self.assertTrue(value['environment_files_present'])
        self.assertTrue(value['effective_runtime_selector'].startswith('UNKNOWN'))

    def test_unset_selector_is_not_assumed_effective_rest(self):
        value = readback.sanitize_show(b'Environment=OTHER=one\n')
        self.assertEqual(value['configured_selector']['HUNTER_BINANCE_KLINE_SOURCE'], 'UNSET_IN_UNIT_ENVIRONMENT')

    def test_unknown_selector_values_and_unknown_properties_are_redacted(self):
        raw = b'Environment="HUNTER_BINANCE_KLINE_SOURCE=SECRET one" HUNTER_SERVER_ISOLATED_CHECKOUT=SECRET OTHER=SECRET\nUnrequested=SECRET\n'
        value = readback.sanitize_show(raw)
        self.assertNotIn('SECRET', json.dumps(value))
        self.assertEqual(set(value['configured_selector'].values()), {'UNRECOGNIZED_VALUE_REDACTED'})

    def test_show_values_with_spaces_and_equals_keep_only_approved_enums(self):
        raw = b'Environment="PRIVATE=a=b c" HUNTER_BINANCE_KLINE_SOURCE=SPOT_CACHE HUNTER_SERVER_ISOLATED_CHECKOUT=1\nExecStart={ path=/bin/tool ; argv[]=tool --secret=a=b --health-supervision ; }\n'
        value = readback.sanitize_show(raw)
        self.assertEqual(value['configured_selector'], {'HUNTER_BINANCE_KLINE_SOURCE': 'SPOT_CACHE',
                                                       'HUNTER_SERVER_ISOLATED_CHECKOUT': '1'})
        self.assertTrue(value['collector_health_flag_present'])
        self.assertNotIn('a=b', json.dumps(value))

    def test_old_manifest_has_full_scope_but_is_not_fresh_or_ready(self):
        value = {'as_of_utc': '2026-10-13T00:00:00Z', 'generation_id': 'g', 'binance_complete': True,
                 'coins': {s: {'pairs': [{'venue': 'binance', 'base': s, 'pair': s + 'USDT'}]} for s in ('BTC', 'TEST')}}
        report = readback.manifest_summary(json.dumps(value).encode(), dt.datetime(2026, 10, 13, 4, tzinfo=dt.timezone.utc))
        self.assertEqual(report['topics'], 7)
        self.assertEqual(report['discovery_topics'], 4)
        self.assertFalse(report['fresh_complete_at_read'])
        self.assertEqual(report['consumer_ready'], 'NOT_ASSESSED')
        self.assertNotIn('testusdt@kline_5m', report['topics_full'])

    def test_duplicate_or_cross_asset_manifest_fails(self):
        value = {'as_of_utc': '2026-10-13T00:00:00Z', 'generation_id': 'g', 'binance_complete': True,
                 'coins': {'BTC': {'pairs': [{'venue': 'binance', 'base': 'BTC', 'pair': 'ETHUSDT'}]}}}
        with self.assertRaises(ValueError):
            readback.manifest_summary(json.dumps(value).encode(), dt.datetime.now(dt.timezone.utc))

    def test_no_main_execution_or_alarm_when_imported(self):
        self.assertEqual(readback.HEAD, '33abf19c7a91855ba2efc2e65e1d383055494d75')
        self.assertEqual(len(readback.GUARDS), 2)

    def test_main_without_explicit_readback_flag_never_collects(self):
        with patch.object(sys, 'argv', ['readback']), patch.object(readback, 'collect') as collect, \
                contextlib.redirect_stderr(__import__('io').StringIO()):
            with self.assertRaises(SystemExit) as raised: readback.main()
        self.assertEqual(raised.exception.code, 2)
        collect.assert_not_called()


@unittest.skipUnless(sys.platform == 'linux', 'Real dir_fd/O_NOFOLLOW/link/FIFO tests require Linux')
class LinuxFileBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='binance-readback-boundaries-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.parent = self.root / 'code'
        self.parent.mkdir()
        self.file = self.parent / 'guard.json'
        self.file.write_bytes(b'{"consumed":true}')

    @contextlib.contextmanager
    def after_read(self, mutate):
        original = os.fdopen
        @contextlib.contextmanager
        def wrapped(fd, *args, **kwargs):
            with original(fd, *args, **kwargs) as handle:
                class Reader:
                    def read(inner, size):
                        data = handle.read(size)
                        mutate()
                        return data
                yield Reader()
        with patch.object(readback.os, 'fdopen', side_effect=wrapped):
            yield

    def test_regular_read_preserves_content_inode_mode_and_modification_time(self):
        before = self.file.stat()
        self.assertEqual(readback.regular_bytes(self.file), b'{"consumed":true}')
        after = self.file.stat()
        for field in ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
            self.assertEqual(getattr(before, field), getattr(after, field), field)

    def test_leaf_symlink_rejected_before_body_read(self):
        link = self.parent / 'leaf-link'
        link.symlink_to(self.file)
        with patch.object(readback.os, 'fdopen', side_effect=AssertionError('must not read')):
            with self.assertRaises(OSError): readback.regular_bytes(link)

    def test_parent_symlink_rejected_before_body_read(self):
        link = self.root / 'parent-link'
        link.symlink_to(self.parent, target_is_directory=True)
        with patch.object(readback.os, 'fdopen', side_effect=AssertionError('must not read')):
            with self.assertRaises(OSError): readback.regular_bytes(link / self.file.name)

    def test_nested_parent_symlink_rejected_before_body_read(self):
        nested = self.root / 'nested'; nested.mkdir()
        (nested / 'link').symlink_to(self.parent, target_is_directory=True)
        with patch.object(readback.os, 'fdopen', side_effect=AssertionError('must not read')):
            with self.assertRaises(OSError): readback.regular_bytes(nested / 'link' / self.file.name)

    def test_hardlinked_file_rejected_before_body_read(self):
        alias = self.parent / 'alias'; os.link(self.file, alias)
        self.assertEqual(self.file.stat().st_nlink, 2)
        with patch.object(readback.os, 'fdopen', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ValueError, 'REGULAR_SINGLE_LINK'): readback.regular_bytes(self.file)

    def test_fifo_rejected_without_blocking_or_reading(self):
        fifo = self.parent / 'fifo'; os.mkfifo(fifo)
        started = time.monotonic()
        with patch.object(readback.os, 'fdopen', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ValueError, 'REGULAR_SINGLE_LINK'): readback.regular_bytes(fifo)
        self.assertLess(time.monotonic() - started, 2)

    def test_directory_rejected_before_body_read(self):
        with patch.object(readback.os, 'fdopen', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ValueError, 'REGULAR_SINGLE_LINK'): readback.regular_bytes(self.parent)

    def test_exact_size_limit_allowed_and_oversize_rejected_before_read(self):
        self.file.write_bytes(b'12345678')
        with patch.object(readback, 'BYTE_LIMIT', 8):
            self.assertEqual(readback.regular_bytes(self.file), b'12345678')
            self.file.write_bytes(b'123456789')
            with patch.object(readback.os, 'fdopen', side_effect=AssertionError('must not read')):
                with self.assertRaisesRegex(ValueError, 'FILE_SIZE_LIMIT'): readback.regular_bytes(self.file)

    def test_growth_during_read_rejected(self):
        with self.after_read(lambda: self.file.write_bytes(b'x' * 32)):
            with self.assertRaisesRegex(ValueError, 'FILE_CHANGED'): readback.regular_bytes(self.file)

    def test_same_size_rewrite_during_read_rejected(self):
        def mutate():
            old = self.file.stat()
            self.file.write_bytes(b'x' * old.st_size)
            os.utime(self.file, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
        with self.after_read(mutate):
            with self.assertRaisesRegex(ValueError, 'FILE_CHANGED'): readback.regular_bytes(self.file)

    def test_same_content_different_inode_replacement_during_read_rejected(self):
        replacement = self.parent / 'replacement'; replacement.write_bytes(self.file.read_bytes())
        with self.after_read(lambda: replacement.replace(self.file)):
            with self.assertRaisesRegex(ValueError, 'FILE_CHANGED'): readback.regular_bytes(self.file)

    def test_hardlink_added_during_read_rejected(self):
        with self.after_read(lambda: os.link(self.file, self.parent / 'late-link')):
            with self.assertRaisesRegex(ValueError, 'FILE_CHANGED'): readback.regular_bytes(self.file)

    def test_leaf_replaced_by_symlink_during_read_rejected(self):
        target = self.root / 'different'; target.write_bytes(b'never read')
        def mutate():
            self.file.unlink(); self.file.symlink_to(target)
        with self.after_read(mutate):
            with self.assertRaisesRegex(ValueError, 'FILE_CHANGED'): readback.regular_bytes(self.file)

    def test_parent_replaced_after_open_does_not_traverse_new_symlink(self):
        target = self.root / 'replacement-dir'; target.mkdir()
        (target / self.file.name).write_bytes(b'never read')
        original_open = os.open; original_fdopen = os.fdopen; read_payloads = []
        switched = False
        def opening(path, flags, *args, **kwargs):
            nonlocal switched
            if path == self.file.name and not switched:
                switched = True
                self.parent.rename(self.root / 'held-parent')
                self.parent.symlink_to(target, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)
        @contextlib.contextmanager
        def reading(fd, *args, **kwargs):
            with original_fdopen(fd, *args, **kwargs) as handle:
                class Reader:
                    def read(inner, size):
                        value = handle.read(size); read_payloads.append(value); return value
                yield Reader()
        with patch.object(readback.os, 'open', side_effect=opening), \
                patch.object(readback.os, 'fdopen', side_effect=reading):
            with self.assertRaisesRegex(ValueError, 'PARENT_CHANGED'): readback.regular_bytes(self.file)
        self.assertEqual(read_payloads, [b'{"consumed":true}'])

    def test_parent_replaced_with_real_directory_during_read_rejected(self):
        def mutate():
            self.parent.rename(self.root / 'held-parent')
            self.parent.mkdir()
            (self.parent / self.file.name).write_bytes(b'{"consumed":true}')
        with self.after_read(mutate):
            with self.assertRaisesRegex(ValueError, 'PARENT_CHANGED'): readback.regular_bytes(self.file)

    def test_relative_parent_traversal_and_root_are_rejected_before_open(self):
        with patch.object(readback.os, 'open', side_effect=AssertionError('must not open')):
            for path in (Path('guard.json'), self.root / '..' / 'outside', Path('/')):
                with self.assertRaisesRegex(ValueError, 'ABSOLUTE_CANONICAL'): readback.regular_bytes(path)

    def test_descriptors_close_on_rejection(self):
        alias = self.parent / 'alias'; os.link(self.file, alias)
        opened = []; original_open = os.open
        def opening(*args, **kwargs):
            fd = original_open(*args, **kwargs); opened.append(fd); return fd
        with patch.object(readback.os, 'open', side_effect=opening):
            with self.assertRaises(ValueError): readback.regular_bytes(self.file)
        self.assertGreater(len(opened), 1)
        for fd in opened:
            with self.assertRaises(OSError): os.fstat(fd)


if __name__ == '__main__':
    unittest.main()
