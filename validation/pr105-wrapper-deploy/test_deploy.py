"""Linux-only, offline PR105 installer state-machine tests. Never use on a server.

The original artifact is hash-checked and parsed unchanged. Only a temporary test
copy receives six explicit fixture adaptations: SOURCE, TARGET, PARENT, backup
path regex, EUID gate and backup UID. All control flow remains byte-identical.
Real GNU cp/mv/chmod, kernel flock and Python fsync run on temporary files.
systemctl and curl are strict local mocks. No sudo, network or production writes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
ORIGINAL = HERE / 'pr105-wrapper-deploy.sh'
CANDIDATE = HERE / 'hunter-monitor-runner.sh'
ORIGINAL_SHA = '4f8ea4f32a8232f5f5a6a083d9ae85aed7b0bdc01e774aebb961e441c9f12e44'
NEW_SHA = 'fb44444bea78386bd283e8d062c3b22c814b5dc336573813a6cd77033c9bbb49'
URL = 'https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/0fce78b355ced25cd6cebe7fb08b9ac72d095860/scripts/hunter_monitor_runner.sh'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
root=pathlib.Path(os.environ['FIXTURE_ROOT']).resolve()
target=pathlib.Path(os.environ['FIXTURE_TARGET'])
with (root/'calls.jsonl').open('a') as f:
    f.write(json.dumps([name,args])+'\n')
def safe(p):
    p=pathlib.Path(p).resolve()
    assert p.is_relative_to(root), 'OUTSIDE_FIXTURE_REFUSED'
    return p
if name=='systemctl':
    assert args[0]=='show', 'MUTATING_SYSTEMCTL_REFUSED'
    if '--value' not in args:
        print('ActiveState=inactive\nResult=success\nMainPID=0'); sys.exit(0)
    key=args[args.index('-p')+1]
    mode=os.environ.get('LAYOUT','ok')
    entry='{ path='+str(target)+' ; argv[]='+str(target)+' ; ignore_errors=no ; }'
    if mode=='args': entry=entry.replace(' ; ignore_errors', ' secret=DO_NOT_PRINT ; ignore_errors')
    if mode=='multiple': entry += ' { path=/other ; argv[]=/other ; }'
    if mode=='entry': entry='{ path=/wrong ; argv[]=/wrong ; }'
    values={'LoadState':'not-found' if mode=='missing' else 'loaded',
      'WorkingDirectory':'/wrong' if mode=='workdir' else os.environ['FIXTURE_SOURCE'],
      'ExecStart':entry,'DropInPaths':'/unknown.conf' if mode=='dropin' else '',
      'FragmentPath':'/fixture/hunter-position-monitor.service'}
    print(values[key])
elif name=='curl':
    assert args==['--disable','--proto','=https','--tlsv1.2','--fail','--silent','--show-error',
      '--connect-timeout','10','--max-time','60','--max-filesize','65536',
      '--output',args[-2],os.environ['EXPECTED_URL']], 'UNEXPECTED_DOWNLOAD_ARGUMENTS'
    out=safe(args[-2]); mode=os.environ.get('DOWNLOAD','ok')
    if mode=='fail': sys.exit(22)
    if mode=='hold':
        (root/'download-entered').touch()
        deadline=time.monotonic()+15
        while not (root/'release-download').exists():
            if time.monotonic()>deadline: sys.exit(28)
            time.sleep(.02)
    out.write_bytes(b'bad download' if mode=='bad' else pathlib.Path(os.environ['CANDIDATE']).read_bytes())
    if mode=='change': target.write_text('#!/bin/bash\nexit 17\n')
elif name=='mv':
    assert args[0:2]==['-fT','--'] and len(args)==4
    safe(args[2]); safe(args[3])
    mode=os.environ.get('MOVE','ok')
    if mode=='before': sys.exit(17)
    result=subprocess.run(['/usr/bin/mv',*args]);
    if result.returncode: sys.exit(result.returncode)
    if mode=='after': sys.exit(18)
    if mode=='corrupt': target.write_text('unknown post-rename content\n')
elif name in ('ssh','scp','git','sudo','wget'):
    sys.exit('FORBIDDEN_EXTERNAL_OR_PRIVILEGED_COMMAND')
else:
    sys.exit('UNEXPECTED_MOCK_COMMAND')
'''


@unittest.skipUnless(sys.platform == 'linux', 'requires unprivileged Linux CI')
class DeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() == 0:
            raise RuntimeError('Run this fixture as an ordinary Linux user, never root')
        assert sha(ORIGINAL) == ORIGINAL_SHA
        assert sha(CANDIDATE) == NEW_SHA
        subprocess.run(['bash', '-n', str(ORIGINAL)], check=True)
        subprocess.run(['bash', '-n', str(CANDIDATE)], check=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pr105-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.parent = self.root / 'usr/local/bin'
        self.source = self.root / 'opt/shadow-runner/btc-grid-state'
        self.parent.mkdir(parents=True)
        self.source.mkdir(parents=True)
        self.target = self.parent / 'hunter-monitor-runner.sh'
        self.old = b'#!/usr/bin/env bash\nprintf "MUST_NEVER_EXECUTE_OLD"\n'
        self.target.write_bytes(self.old)
        self.target.chmod(0o751)
        self.oldsha = sha(self.target)
        self.meta = f'{os.getuid()}:{os.getgid()}:751'
        self.ledger = self.source / 'ledger-sentinel'
        self.ledger.write_bytes(b'immutable fixture history\n')
        self.ledgerhash = sha(self.ledger)
        self.ledgerstat = self.ledger.stat()
        self.mockbin = self.root / 'mocks'
        self.mockbin.mkdir()
        for name in ['systemctl', 'curl', 'mv', 'ssh', 'scp', 'git', 'sudo', 'wget']:
            p = self.mockbin / name
            p.write_text(MOCK)
            p.chmod(0o755)
        original = ORIGINAL.read_text()
        adaptations = {
            'readonly SOURCE=/opt/shadow-runner/btc-grid-state': f'readonly SOURCE={self.source}',
            'readonly TARGET=/usr/local/bin/hunter-monitor-runner.sh': f'readonly TARGET={self.target}',
            'readonly PARENT=/usr/local/bin': f'readonly PARENT={self.parent}',
            r'^/usr/local/bin/\.hunter-monitor-pr105\.': rf'^{self.parent}/\.hunter-monitor-pr105\.',
            '(( EUID == 0 ))': f'(( EUID == {os.getuid()} ))',
            '== 0:700 ]]': f'== {os.getuid()}:700 ]]',
        }
        for old, new in adaptations.items():
            self.assertEqual(original.count(old), 1, old)
            original = original.replace(old, new)
        self.script = self.root / 'fixture-deploy.sh'
        self.script.write_text(original)
        self.env = dict(os.environ, PATH=f'{self.mockbin}:/usr/bin:/bin',
                        FIXTURE_ROOT=str(self.root), FIXTURE_TARGET=str(self.target),
                        FIXTURE_SOURCE=str(self.source), EXPECTED_URL=URL,
                        CANDIDATE=str(CANDIDATE), PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('BASH_ENV', None)
        self.env.pop('ENV', None)

    def tearDown(self):
        self.assertEqual(sha(self.ledger), self.ledgerhash)
        current = self.ledger.stat()
        self.assertEqual((current.st_ino, current.st_mtime_ns, current.st_mode),
                         (self.ledgerstat.st_ino, self.ledgerstat.st_mtime_ns, self.ledgerstat.st_mode))
        self.assertEqual(list(self.source.iterdir()), [self.ledger])
        for name, args in self.calls():
            self.assertIn(name, ['systemctl', 'curl', 'mv'])
            if name == 'systemctl':
                self.assertEqual(args[0], 'show')

    def calls(self):
        p = self.root / 'calls.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def run_script(self, *args, env=None, original=False):
        return subprocess.run(['bash', str(ORIGINAL if original else self.script), *args],
                              env=dict(self.env, **(env or {})), capture_output=True,
                              text=True, timeout=20)

    def check_result(self, result, success, text=''):
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        self.assertIn(text, result.stdout + result.stderr)
        self.assertNotIn('MUST_NEVER_EXECUTE_OLD', result.stdout + result.stderr)
        self.assertNotIn('secret=DO_NOT_PRINT', result.stdout + result.stderr)

    def install(self, **env):
        return self.run_script('install', self.oldsha, self.meta, env=env)

    def backup(self, result):
        return Path(re.search(r'^BACKUP_DIR=(.+)$', result.stdout, re.M).group(1))

    def assert_old(self):
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o751)

    def test_original_bytes_and_linux_bash_parse(self):
        self.assertEqual(sha(ORIGINAL), ORIGINAL_SHA)
        self.assertEqual(sha(CANDIDATE), NEW_SHA)
        self.check_result(subprocess.run(['bash', '-n', str(ORIGINAL)], capture_output=True, text=True), True)

    def test_default_and_explicit_audit_are_read_only(self):
        before = [(p.name, p.stat().st_ino) for p in self.parent.iterdir()]
        for args in [(), ('audit',)]:
            result = self.run_script(*args)
            self.check_result(result, True, f'OLD_SHA256={self.oldsha}')
            self.assertIn(f'OLD_META={self.meta}', result.stdout)
        self.assertEqual(before, [(p.name, p.stat().st_ino) for p in self.parent.iterdir()])
        self.assertTrue(all(name == 'systemctl' for name, _ in self.calls()))
        self.assert_old()

    def test_bad_arguments_refuse_before_writes(self):
        for args in [('unknown',), ('audit', 'extra'), ('install',),
                     ('install', 'PASTE_OLD_SHA256', 'PASTE_UID:GID:MODE'),
                     ('install', self.oldsha, 'bad'), ('rollback',),
                     ('install', NEW_SHA, self.meta)]:
            self.check_result(self.run_script(*args), False, 'REFUSED:')
        self.assertEqual(list(self.parent.iterdir()), [self.target])

    def test_original_root_guard_rejects_without_touching_real_paths(self):
        self.check_result(self.run_script('install', self.oldsha, self.meta, original=True),
                          False, 'EXISTING_ROOT_SHELL_REQUIRED')
        self.assertEqual(self.calls(), [])

    def test_unexpected_unit_layouts_refuse(self):
        for mode in ['missing', 'workdir', 'args', 'multiple', 'entry', 'dropin']:
            with self.subTest(mode=mode):
                self.check_result(self.run_script('audit', env={'LAYOUT': mode}), False, 'REFUSED:')
        self.assertEqual(list(self.parent.iterdir()), [self.target])

    def test_symlink_and_hardlink_refuse(self):
        link = self.root / 'linked'
        self.target.rename(link)
        self.target.symlink_to(link)
        self.check_result(self.run_script('audit'), False, 'NOT_REGULAR_NON_SYMLINK')
        self.target.unlink()
        os.link(link, self.target)
        self.check_result(self.run_script('audit'), False, 'HARDLINKED_TARGET')

    def test_stale_old_hash_or_metadata_refuse_before_backup(self):
        for oldsha, meta in [('a'*64, self.meta), (self.oldsha, self.meta[:-3]+'755')]:
            self.check_result(self.run_script('install', oldsha, meta), False, 'TARGET_')
        self.assertEqual(list(self.parent.iterdir()), [self.target])
        self.assert_old()

    def test_download_failures_preserve_original_and_backup(self):
        for mode in ['fail', 'bad']:
            with self.subTest(mode=mode):
                result = self.install(DOWNLOAD=mode)
                self.check_result(result, False, 'atomic_replace_state=not_attempted')
                self.assert_old()
                self.assertEqual((self.backup(result)/'old-wrapper').read_bytes(), self.old)

    def test_install_and_rollback_preserve_bytes_owner_mode(self):
        oldinode = self.target.stat().st_ino
        result = self.install()
        self.check_result(result, True, 'INSTALL_COMPLETE_BACKUP_RETAINED=')
        backup = self.backup(result)
        self.assertEqual(sha(self.target), NEW_SHA)
        self.assertNotEqual(self.target.stat().st_ino, oldinode)
        self.assertEqual(self.target.stat().st_uid, os.getuid())
        self.assertEqual(self.target.stat().st_gid, os.getgid())
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o751)
        self.assertEqual((backup/'old-wrapper').read_bytes(), self.old)
        result = self.run_script('rollback', str(backup), self.oldsha, self.meta)
        self.check_result(result, True, 'WRAPPER_ROLLBACK_COMPLETE')
        self.assert_old()
        self.assertTrue(list(self.parent.glob('.hunter-monitor-pr105-rollback.*/pr105-wrapper-before-rollback')))

    def test_repeat_install_refuses_without_new_backup(self):
        self.check_result(self.install(), True)
        before = sorted(p.name for p in self.parent.iterdir())
        self.check_result(self.install(), False, 'TARGET_HASH_CHANGED')
        self.assertEqual(before, sorted(p.name for p in self.parent.iterdir()))
        self.assertEqual(sha(self.target), NEW_SHA)

    def test_rollback_refuses_unknown_current_target(self):
        backup = self.backup(self.install())
        unknown = b'unknown newer version\n'
        self.target.write_bytes(unknown)
        self.check_result(self.run_script('rollback', str(backup), self.oldsha, self.meta), False, 'TARGET_HASH_CHANGED')
        self.assertEqual(self.target.read_bytes(), unknown)

    def test_rollback_refuses_bad_backup_and_metadata(self):
        backup = self.backup(self.install())
        self.check_result(self.run_script('rollback', '/outside', self.oldsha, self.meta), False, 'INVALID_BACKUP_DIRECTORY')
        backup.chmod(0o755)
        self.check_result(self.run_script('rollback', str(backup), self.oldsha, self.meta), False, 'UNSAFE_BACKUP_DIRECTORY_METADATA')
        backup.chmod(0o700)
        (backup/'old-wrapper').write_bytes(b'corrupt')
        self.check_result(self.run_script('rollback', str(backup), self.oldsha, self.meta), False, 'COPY_HASH_MISMATCH')
        self.assertEqual(sha(self.target), NEW_SHA)

    def test_target_change_during_download_is_not_overwritten(self):
        result = self.install(DOWNLOAD='change')
        self.check_result(result, False, 'TARGET_HASH_CHANGED')
        self.assertEqual(self.target.read_bytes(), b'#!/bin/bash\nexit 17\n')
        self.assertEqual((self.backup(result)/'old-wrapper').read_bytes(), self.old)

    def test_rename_failure_preserves_old_or_retains_recoverable_backup(self):
        before = self.install(MOVE='before')
        self.check_result(before, False, 'atomic_replace_state=attempting')
        self.assert_old()
        after = self.install(MOVE='after')
        self.check_result(after, False, 'atomic_replace_state=attempting')
        self.assertEqual(sha(self.target), NEW_SHA)
        self.check_result(self.run_script('rollback', str(self.backup(after)), self.oldsha, self.meta), True)
        self.assert_old()

    def test_post_rename_unknown_content_is_retained_not_blindly_rolled_back(self):
        result = self.install(MOVE='corrupt')
        self.check_result(result, False, 'atomic_replace_state=completed')
        self.assertEqual(self.target.read_text(), 'unknown post-rename content\n')
        backup = self.backup(result)
        self.assertEqual((backup/'old-wrapper').read_bytes(), self.old)
        self.check_result(self.run_script('rollback', str(backup), self.oldsha, self.meta), False, 'TARGET_HASH_CHANGED')

    def held_process(self):
        process = subprocess.Popen(['bash', str(self.script), 'install', self.oldsha, self.meta],
                                   env=dict(self.env, DOWNLOAD='hold'), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        def cleanup():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic()+8
        while not (self.root/'download-entered').exists():
            if process.poll() is not None or time.monotonic()>deadline:
                self.fail('Installer did not enter mocked download while holding flock')
            time.sleep(.02)
        return process

    def test_concurrent_installer_refuses_and_lock_releases_after_success(self):
        process = self.held_process()
        self.check_result(self.install(), False, 'ANOTHER_WRAPPER_INSTALL_OR_ROLLBACK_IS_ACTIVE')
        (self.root/'release-download').touch()
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, out+err)
        result = subprocess.CompletedProcess([], process.returncode, out, err)
        self.check_result(self.run_script('rollback', str(self.backup(result)), self.oldsha, self.meta), True)
        self.assert_old()

    def test_terminated_process_group_preserves_old_and_releases_lock(self):
        process = self.held_process()
        os.killpg(process.pid, signal.SIGTERM)
        out, err = process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0, out+err)
        self.assert_old()
        self.assertTrue(list(self.parent.glob('.hunter-monitor-pr105.*/old-wrapper')))
        self.check_result(self.install(), True, 'INSTALL_COMPLETE_BACKUP_RETAINED=')


if __name__ == '__main__':
    print(json.dumps({'original_sha256': sha(ORIGINAL), 'candidate_sha256': sha(CANDIDATE),
                      'linux': sys.platform, 'uid': os.getuid() if hasattr(os, 'getuid') else None,
                      'scope': 'OFFLINE_FIXTURE_ONLY_NO_SERVER_NO_STRATEGY'}), flush=True)
    unittest.main(verbosity=2)
