"""Generic public bootstrap tests; no site-specific values or server access."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'bootstrap.sh'
INSTALLER_SHA = '4f8ea4f32a8232f5f5a6a083d9ae85aed7b0bdc01e774aebb961e441c9f12e44'
BOOTSTRAP_SHA = '5d66ad9dd628dd8fe7225c86a110ff834336c5cb53c9c7f39cbaa7c4eb8a2fc0'
SYNTHETIC_SHA = 'a'*64
SYNTHETIC_META = '1234:2345:750'
DUMMY = b'''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$1" == audit ]]; then exit "${AUDIT_RC:-0}"; fi
exit "${INSTALL_RC:-0}"
'''
MOCK = r'''#!/usr/bin/env python3
import os, pathlib, sys
root=pathlib.Path(os.environ['TEST_ROOT']).resolve()
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
if name=='mktemp':
    assert args==['-d','/tmp/pr105-wrapper-install.XXXXXXXX']
    p=root/'install'; p.mkdir(mode=0o700); print(p)
elif name=='curl':
    expected_url='https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/657766942a5604e73d06561fdb9155bff45af664/validation/pr105-wrapper-deploy/pr105-wrapper-deploy.sh'
    assert args==['--disable','--proto','=https','--tlsv1.2','--fail','--silent','--show-error',
      '--connect-timeout','10','--max-time','60','--max-filesize','65536','--output',args[-2],expected_url]
    path=pathlib.Path(args[-2]).resolve(); assert path.is_relative_to(root)
    if os.environ.get('TRANSFER')=='fail': sys.exit(22)
    path.write_bytes(b'corrupt' if os.environ.get('TRANSFER')=='corrupt' else (root/'candidate').read_bytes())
else:
    raise AssertionError('UNEXPECTED_COMMAND')
'''


@unittest.skipUnless(sys.platform == 'linux', 'Linux CI required')
class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == BOOTSTRAP_SHA
        subprocess.run(['bash', '-n', str(SCRIPT)], check=True)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='generic-bootstrap-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.calls=self.root/'calls'
        self.mocks=self.root/'mocks'; self.mocks.mkdir()
        for name in ('mktemp','curl'):
            p=self.mocks/name; p.write_text(MOCK); p.chmod(0o755)
        self.env=dict(os.environ, PATH=f'{self.mocks}:/usr/bin:/bin',
                      TEST_ROOT=str(self.root), CALLS=str(self.calls), PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('BASH_ENV',None); self.env.pop('ENV',None)
        self.set_candidate(DUMMY)

    def set_candidate(self, value):
        (self.root/'candidate').write_bytes(value)
        source=SCRIPT.read_text()
        self.assertEqual(source.count(INSTALLER_SHA),1)
        # Only the expected download digest is adapted for an inert local fixture.
        self.fixture=self.root/'bootstrap.sh'
        self.fixture.write_text(source.replace(INSTALLER_SHA,hashlib.sha256(value).hexdigest()))

    def run_script(self, args=None, **env):
        if args is None: args=[SYNTHETIC_SHA,SYNTHETIC_META]
        return subprocess.run(['bash',str(self.fixture),*args],capture_output=True,text=True,
                              timeout=5,env=dict(self.env,**env))

    def check(self,result,rc):
        self.assertEqual(result.returncode,rc,result.stdout+result.stderr)

    def test_unchanged_generic_bootstrap_parses(self):
        self.check(subprocess.run(['bash','-n',str(SCRIPT)],capture_output=True,text=True),0)

    def test_valid_transfer_audits_before_install_and_preserves_arguments(self):
        self.check(self.run_script(),0)
        self.assertEqual(self.calls.read_text().splitlines(),['audit',f'install {SYNTHETIC_SHA} {SYNTHETIC_META}'])

    def test_invalid_parameters_stop_before_staging(self):
        for args in ([],[SYNTHETIC_SHA],['bad',SYNTHETIC_META],[SYNTHETIC_SHA,'bad']):
            self.check(self.run_script(args),2)
        self.assertFalse((self.root/'install').exists())
        self.assertFalse(self.calls.exists())

    def test_transfer_failure_does_not_execute_download(self):
        self.check(self.run_script(TRANSFER='fail'),22)
        self.assertFalse(self.calls.exists())

    def test_wrong_hash_does_not_execute_download(self):
        self.assertNotEqual(self.run_script(TRANSFER='corrupt').returncode,0)
        self.assertFalse(self.calls.exists())

    def test_shell_syntax_failure_does_not_execute_download(self):
        self.set_candidate(b'#!/bin/bash\nif then bad\n')
        self.assertNotEqual(self.run_script().returncode,0)
        self.assertFalse(self.calls.exists())

    def test_audit_failure_never_runs_install(self):
        self.check(self.run_script(AUDIT_RC='9'),9)
        self.assertEqual(self.calls.read_text().splitlines(),['audit'])

    def test_install_failure_is_reported(self):
        self.check(self.run_script(INSTALL_RC='7'),7)
        self.assertEqual(self.calls.read_text().splitlines(),['audit',f'install {SYNTHETIC_SHA} {SYNTHETIC_META}'])


if __name__=='__main__': unittest.main(verbosity=2)
