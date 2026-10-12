"""Batch transport faults must never become a successful main readback."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import hunter_monitor_persist as m
from scripts.hunter_lock_capacity import git_at


def batch(payloads=None):
    payloads = payloads or {n: b'{"value": 1}\n' for n in m.NAMES}
    return b''.join(n.encode() + b' ' + b'a' * 40 + b' blob ' +
                    str(len(payloads[n])).encode() + b'\n' + payloads[n] + b'\n'
                    for n in m.NAMES)


class FramingTests(unittest.TestCase):
    def test_multibyte_and_embedded_newlines_match_text_mode(self):
        payloads = {n: '{\r\n"value": "汉字 🚦",\r"x": 2\n}\n'.encode() for n in m.NAMES}
        # The deployed Linux locale and Python UTF-8 mode use UTF-8. Test it
        # explicitly without changing the process locale for unrelated tests.
        with mock.patch.object(m.locale, 'getpreferredencoding', return_value='utf-8'):
            got = m.parse_batch_snapshot(batch(payloads))
        self.assertEqual(got, {n: '{\n"value": "汉字 🚦",\n"x": 2\n}\n' for n in m.NAMES})

    def test_framing_faults_fail_closed(self):
        valid = batch()
        faults = [b'', valid[:-1], valid + b'extra',
                  valid.replace(b' blob ', b' tree ', 1),
                  valid.replace(b'a' * 40, b'z' * 40, 1),
                  valid.replace(b' blob 13\n', b' blob -1\n', 1),
                  valid.replace(b' blob 13\n', b' blob 999999999\n', 1),
                  valid.replace(m.NAMES[0].encode(), m.NAMES[1].encode(), 1),
                  b'a' * 300 + b'\n' + valid,
                  b'HEAD:research/results/missing.json missing\n']
        for value in faults:
            with self.subTest(value=value[:80]):
                with self.assertRaises(RuntimeError):
                    m.parse_batch_snapshot(value)

    def test_invalid_encoded_text_is_not_replaced_silently(self):
        with mock.patch.object(m.locale, 'getpreferredencoding', return_value='utf-8'):
            with self.assertRaises(UnicodeDecodeError):
                m.parse_batch_snapshot(batch({n: b'\xff' for n in m.NAMES}))

    def test_batch_failure_never_reports_readback_success(self):
        for failure in ('missing', 'process_failure'):
            out = io.StringIO()
            def fake_git(*args, **kwargs):
                if args[0] == 'rev-parse':
                    return subprocess.CompletedProcess(args, 0, 'a' * 40 + '\n', '')
                if args[0] == 'cat-file':
                    if failure == 'process_failure':
                        raise subprocess.CalledProcessError(1, ['git', 'cat-file'])
                    return subprocess.CompletedProcess(args, 0, b'missing missing\n', b'')
                return subprocess.CompletedProcess(args, 0, '', '')
            with mock.patch.object(m, 'git', side_effect=fake_git), contextlib.redirect_stdout(out):
                with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                    m.readback({}, 'fixture', 'b' * 40)
            self.assertNotIn('SERVER_POST_PUSH_READBACK_OK', out.getvalue())


class SnapshotGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        git_at(self.root, 'init', '--initial-branch=main')
        git_at(self.root, 'config', 'user.name', 'snapshot-fixture')
        git_at(self.root, 'config', 'user.email', 'fixture@localhost')
        for i, n in enumerate(m.NAMES):
            p = self.root / m.RESULTS / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes((' {"index": %s, "text": "line\\nvalue"}\n' % i).encode())
        self.commit()
        self.sha = git_at(self.root, 'rev-parse', 'HEAD').stdout.strip()
        patcher = mock.patch.object(m, 'git', side_effect=self.git)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, **kwargs):
        return git_at(self.root, *args, **kwargs)

    def commit(self):
        git_at(self.root, 'add', '.')
        git_at(self.root, 'commit', '-m', 'fixture')

    def legacy(self, ref):
        return {n: self.git('show', ref + ':' + m.RESULTS + n).stdout for n in m.NAMES}

    def test_batch_matches_seven_show_calls_and_pins_mutable_ref(self):
        expected = self.legacy(self.sha)
        (self.root / m.PATHS[0]).write_text('{"new": true}\n')
        self.commit()
        newer = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('update-ref', 'refs/heads/main', self.sha)
        calls = []
        def advancing_git(*args, **kwargs):
            calls.append(args[0])
            result = self.git(*args, **kwargs)
            if args[0] == 'rev-parse':
                self.git('update-ref', 'refs/heads/main', newer)
            return result
        with mock.patch.object(m, 'git', side_effect=advancing_git):
            self.assertEqual(m.snapshot('main'), expected)
        self.assertEqual(calls, ['rev-parse', 'cat-file'])
        # A later generation must resolve again, never return cached old data.
        self.assertEqual(m.snapshot('main'), self.legacy(newer))
        self.assertNotEqual(m.snapshot('main'), expected)

    def test_missing_blob_fails_instead_of_returning_partial_snapshot(self):
        (self.root / m.PATHS[-1]).unlink()
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'INVALID_SNAPSHOT_BATCH_OBJECT'):
            m.snapshot('main')

    def test_directory_and_invalid_ref_fail_closed(self):
        p = self.root / m.PATHS[0]
        p.unlink(); p.mkdir(); (p / 'child').write_text('tree')
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'INVALID_SNAPSHOT_BATCH_OBJECT'):
            m.snapshot('main')
        for ref in ('does-not-exist', '--help', self.sha + '\nHEAD'):
            with self.subTest(ref=ref), self.assertRaises(subprocess.CalledProcessError):
                m.snapshot(ref)

    def test_dirty_worktree_does_not_replace_committed_readback(self):
        expected = self.legacy(self.sha)
        (self.root / m.PATHS[0]).write_text('corrupt local draft')
        self.assertEqual(m.snapshot('main'), expected)


if __name__ == '__main__':
    unittest.main()
