"""Small filesystem-only upgrade fixtures. No server, network, service or cache mutation."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import hunter_binance_upgrade as upgrade


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root/'binance'; self.prefix.mkdir()
        self.work = self.prefix/'upgrade-test'; self.work.mkdir()
        self.saved, self.staged, self.rolled = upgrade.code_paths(self.prefix,self.work)
        self.state = self.root/'state'; self.state.mkdir()
        self.old = {'research/a.py': (b'old\n', 0o444), 'scripts/b.py': (b'unchanged\n', 0o555)}
        self.new = dict(self.old, **{'research/a.py': (b'new\n', 0o444)})
        upgrade.stage(self.prefix/'code', self.old)
        upgrade.write_new(self.prefix/'release-head', (upgrade.OLD+'\n').encode(), 0o444)
        upgrade.stage(self.staged, self.new)
        upgrade.write_new(self.work/'new-release-head', (upgrade.NEW+'\n').encode(), 0o444)
        for name, data in {'state.sqlite3': b'opaque orphan evidence', 'state.sqlite3-wal': b'wal',
                           'manifest.json': b'{}', 'collector.lock': b'', 'request-'+('a'*32)+'.lock': b''}.items():
            (self.state/name).write_bytes(data)
        self.owner = os.getuid() if hasattr(os, 'getuid') else None

    def cleanup(self):
        # Only this test's freshly allocated temporary tree; required for the
        # immutable-file fixture on Windows and for directory removal on Linux.
        root = Path(self.temp.name).resolve()
        if root.exists():
            for path in [root, *root.rglob('*')]:
                if not path.is_symlink():
                    self.assertTrue(path.resolve() == root or root in path.resolve().parents)
                    path.chmod(0o700 if path.is_dir() else 0o600)
        self.temp.cleanup()

    def install(self):
        upgrade.switch(self.prefix, self.work, self.old, self.new, self.owner)

    def test_default_plan_has_no_io_or_commands(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['upgrade']), patch.object(upgrade, 'run') as run, contextlib.redirect_stdout(output):
            upgrade.main()
        run.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual((result['old'], result['new']), (upgrade.OLD, upgrade.NEW))
        self.assertEqual(result['status'], 'PLAN_ONLY'); self.assertFalse(result['start'])

    def test_whitelist_excludes_ledgers_and_other_outputs(self):
        for name in ('research/results/hunter-shadow-portfolio.json', 'research/results/stock-shadow/trades-v1.json',
                     '.env', 'deploy/unknown.service', 'tests/test_example.py'):
            self.assertFalse(upgrade.selected(name))
        self.assertTrue(upgrade.selected(upgrade.CONFIG))

    def test_package_delta_refuses_unreviewed_file_or_mode(self):
        with patch.object(upgrade, 'release', side_effect=[self.old, self.new]):
            with self.assertRaisesRegex(RuntimeError, 'UNREVIEWED_RUNTIME_DELTA'): upgrade.releases(self.root)
        with patch.object(upgrade, 'release', side_effect=[self.old, dict(self.new, extra=(b'x',0o444))]):
            with self.assertRaisesRegex(RuntimeError, 'FILE_SET_CHANGED'): upgrade.releases(self.root)

    def test_installed_hash_mismatch_blocks_before_swap(self):
        path = self.prefix/'code/research/a.py'; path.chmod(0o600); path.write_bytes(b'local edit')
        with self.assertRaisesRegex(RuntimeError, 'CODE_HASH_MISMATCH'): self.install()
        self.assertFalse(self.saved.exists())
        self.assertEqual((self.prefix/'release-head').read_text().strip(), upgrade.OLD)

    def test_unknown_file_blocks_before_swap(self):
        root = self.prefix/'code'; root.chmod(0o755); (root/'extra').write_bytes(b'x'); root.chmod(0o555)
        with self.assertRaisesRegex(RuntimeError, 'UNEXPECTED_CODE_FILE'): self.install()

    def test_release_head_mismatch_blocks(self):
        path = self.prefix/'release-head'; path.chmod(0o600); path.write_bytes(b'unknown\n')
        with self.assertRaisesRegex(RuntimeError, 'INSTALLED_RELEASE_MISMATCH'): self.install()
        self.assertFalse(self.saved.exists())

    def test_upgrade_retains_old_code_and_never_touches_other_venue_or_state(self):
        bybit = self.root/'bybit'; bybit.mkdir(); (bybit/'sentinel').write_bytes(b'running')
        state = {p.name:p.read_bytes() for p in self.state.iterdir()}
        with patch.object(upgrade, 'run', side_effect=AssertionError('no commands during swap')):
            self.install()
        upgrade.verify_tree(self.prefix/'code', self.new, self.owner)
        upgrade.verify_tree(self.saved, self.old, self.owner)
        self.assertEqual((self.work/'old-release-head').read_text().strip(), upgrade.OLD)
        self.assertEqual({p.name:p.read_bytes() for p in self.state.iterdir()}, state)
        self.assertEqual((bybit/'sentinel').read_bytes(), b'running')

    def test_explicit_rollback_preserves_new_code_and_restores_exact_old(self):
        self.install(); upgrade.restore(self.prefix, self.work, self.old, self.new, self.owner)
        upgrade.verify_tree(self.prefix/'code', self.old, self.owner)
        upgrade.verify_tree(self.rolled, self.new, self.owner)
        self.assertEqual((self.prefix/'release-head').read_text().strip(), upgrade.OLD)

    def test_each_swap_failure_rolls_back(self):
        real = upgrade.move
        for fail_at in range(1,5):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory(dir=self.root) as folder:
                prefix=Path(folder); work=prefix/'work'; work.mkdir()
                upgrade.stage(prefix/'code',self.old); upgrade.stage(upgrade.code_paths(prefix,work)[1],self.new)
                upgrade.write_new(prefix/'release-head',(upgrade.OLD+'\n').encode())
                upgrade.write_new(work/'new-release-head',(upgrade.NEW+'\n').encode())
                calls=[0]
                def fail(source,target):
                    calls[0]+=1
                    if calls[0]==fail_at: raise OSError('injected rename failure')
                    real(source,target)
                with patch.object(upgrade,'move',fail), self.assertRaisesRegex(OSError,'injected'):
                    upgrade.switch(prefix,work,self.old,self.new,self.owner)
                upgrade.verify_tree(prefix/'code',self.old,self.owner)
                self.assertEqual((prefix/'release-head').read_text().strip(),upgrade.OLD)
                for p in [prefix,*prefix.rglob('*')]: p.chmod(0o700 if p.is_dir() else 0o600)

    def test_interrupted_pair_switch_can_be_restored(self):
        upgrade.move(self.prefix/'code',self.saved)
        upgrade.move(self.staged,self.prefix/'code')
        # A killed process can leave the new code with the old release-head.
        upgrade.restore(self.prefix,self.work,self.old,self.new,self.owner)
        upgrade.verify_tree(self.prefix/'code',self.old,self.owner)

    def test_corrupt_backup_head_prevents_any_rollback_move(self):
        self.install(); path=self.work/'old-release-head'; path.chmod(0o600); path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError,'BACKUP_HEAD_MISMATCH'):
            upgrade.restore(self.prefix,self.work,self.old,self.new,self.owner)
        upgrade.verify_tree(self.prefix/'code',self.new,self.owner)

    def test_archive_symlink_rejected(self):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w') as archive:
            item=tarfile.TarInfo('research/escape.py'); item.type=tarfile.SYMTYPE; item.linkname='/tmp/escape'
            archive.addfile(item)
        with patch.object(upgrade,'run',side_effect=[(upgrade.OLD+'\n').encode(),b'research/escape.py\n',data.getvalue()]):
            with self.assertRaisesRegex(RuntimeError,'ARCHIVE_LINK'): upgrade.release(self.root,upgrade.OLD)

    def test_evidence_and_upgrade_leave_orphan_bytes_unchanged(self):
        original={p.name:p.read_bytes() for p in self.state.iterdir()}
        with patch.object(upgrade,'STATE',self.state):
            hashes=upgrade.state_evidence(self.work)
            self.install(); upgrade.state_unchanged(hashes)
        self.assertEqual({p.name:p.read_bytes() for p in self.state.iterdir()},original)
        self.assertEqual({p.name:p.read_bytes() for p in (self.work/'state-evidence').iterdir()},original)

    def test_state_added_or_changed_is_detected(self):
        with patch.object(upgrade,'STATE',self.state):
            hashes=upgrade.state_evidence(self.work)
            (self.state/('request-'+('b'*32)+'.lock')).write_bytes(b'')
            with self.assertRaisesRegex(RuntimeError,'STATE_CHANGED'): upgrade.state_unchanged(hashes)

    def test_active_service_is_rejected_without_stop(self):
        fields=b'ActiveState=active\nSubState=running\nMainPID=123\nControlGroup=/system.slice/hunter-binance-stream.service\n'
        with patch.object(upgrade,'run',return_value=fields) as command:
            with self.assertRaisesRegex(RuntimeError,'BINANCE_NOT_STOPPED'): upgrade.quiet()
        self.assertEqual(command.call_count,1)
        self.assertEqual(command.call_args.args[0][:4],['systemctl','--no-pager','show',upgrade.SERVICE])

    def test_live_collector_or_http_owner_lock_blocks_without_mutation(self):
        import fcntl
        original={p.name:p.read_bytes() for p in self.state.iterdir()}
        for name in ('collector.lock','request-'+('a'*32)+'.lock'):
            with self.subTest(name=name),patch.object(upgrade,'STATE',self.state), (self.state/name).open('rb') as owner:
                fcntl.flock(owner,fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError),upgrade.locked_state():
                    self.fail('live owner must block entry')
        self.assertEqual({p.name:p.read_bytes() for p in self.state.iterdir()},original)


if __name__=='__main__': unittest.main()
