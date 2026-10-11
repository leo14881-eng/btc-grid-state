"""Real Linux flock contention and process lifetime checks, with synthetic files."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from scripts.hunter_portfolio_lock import PortfolioLock, read_receipt


HOLDER = '''
import sys
from scripts.hunter_portfolio_lock import PortfolioLock
lock=PortfolioLock('monitor', 'g', 'base', path=sys.argv[1]).acquire()
lock.phase('evaluate')
print('READY',flush=True)
sys.stdin.readline()
lock.close()
'''


class PortfolioLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name, 'portfolio.lock'))
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None: child.kill()
            child.communicate(timeout=5)
        self.tmp.cleanup()

    def holder(self, code=HOLDER, *extra):
        p = subprocess.Popen([sys.executable, '-u', '-c', code, self.path, *extra],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        self.children.append(p)
        while True:
            row = p.stdout.readline()
            self.assertTrue(row, p.stderr.read() if p.poll() is not None else 'missing readiness')
            if row.strip() == 'READY': return p

    def test_contention_timeout_records_owner_and_preserves_inode(self):
        child = self.holder()
        inode = os.stat(self.path).st_ino
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaisesRegex(RuntimeError, 'PORTFOLIO_LOCK_WAIT_TIMEOUT'):
                PortfolioLock('research', path=self.path).acquire(.08)
        rows = [json.loads(x.split(' ', 1)[1]) for x in out.getvalue().splitlines()]
        self.assertEqual(rows[-1]['event'], 'wait_failed')
        self.assertGreaterEqual(rows[-1]['wait_seconds'], .07)
        self.assertEqual(rows[-1]['observed_receipt']['pid'], child.pid)
        self.assertEqual(rows[-1]['observed_receipt']['phase'], 'evaluate')
        self.assertFalse(rows[-1]['receipt_is_ownership_proof'])
        child.communicate('\n', timeout=5)
        with contextlib.redirect_stdout(io.StringIO()):
            lock = PortfolioLock('research', path=self.path).acquire(.5)
            lock.phase('readback', generation='new', base_sha='new-base')
            self.assertEqual(read_receipt(lock.file)['generation'], 'new')
            lock.close()
        self.assertEqual(os.stat(self.path).st_ino, inode)

    def test_sigkill_releases_kernel_lock_despite_stale_receipt(self):
        child = self.holder()
        child.kill(); child.communicate(timeout=5)
        with contextlib.redirect_stdout(io.StringIO()):
            lock = PortfolioLock('research', path=self.path).acquire(.5)
            self.assertEqual(read_receipt(lock.file)['pid'], os.getpid())
            lock.close('failed')

    def test_child_keeps_lock_when_supervisor_is_killed(self):
        marker = str(Path(self.tmp.name, 'child.pid'))
        code = '''
import pathlib,subprocess,sys
from scripts.hunter_portfolio_lock import PortfolioLock
lock=PortfolioLock('research',path=sys.argv[1]).acquire()
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],pass_fds=(lock.file.fileno(),),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
pathlib.Path(sys.argv[2]).write_text(str(child.pid))
print('READY',flush=True)
sys.stdin.readline()
'''
        parent = self.holder(code, marker)
        child_pid = int(Path(marker).read_text())
        try:
            parent.kill(); parent.communicate(timeout=5)
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'PORTFOLIO_LOCK_WAIT_TIMEOUT'):
                    PortfolioLock('monitor', path=self.path).acquire(.08)
        finally:
            os.kill(child_pid, signal.SIGKILL)
        with contextlib.redirect_stdout(io.StringIO()):
            lock = PortfolioLock('monitor', path=self.path).acquire(2)
            lock.close()

    def test_concurrent_roles_read_current_pair_and_deduplicate(self):
        # A successful lock is followed by fresh reads, never pre-lock decisions.
        target = Path(self.tmp.name, 'ledger.json')
        target.write_text(json.dumps(dict(v1=[], v2=[], generation=0)))
        code = '''
import json,pathlib,sys,time
from scripts.hunter_portfolio_lock import PortfolioLock
p=pathlib.Path(sys.argv[2])
for request in ['same','same','next']:
 lock=PortfolioLock(sys.argv[3],path=sys.argv[1]).acquire(5)
 try:
  row=json.loads(p.read_text())
  assert row['v1']==row['v2']
  if request not in row['v1']:
   row['v1'].append(request);time.sleep(.01);row['v2'].append(request);row['generation']+=1
   p.write_text(json.dumps(row))
 finally:lock.close()
'''
        for role in ('research', 'monitor'):
            p = subprocess.Popen([sys.executable, '-c', code, self.path, str(target), role],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.children.append(p)
        for p in self.children:
            out, err = p.communicate(timeout=10)
            self.assertEqual(p.returncode, 0, out+err)
            self.assertEqual(out.count('"event": "acquired"'), 3)
        row = json.loads(target.read_text())
        self.assertEqual(row, dict(v1=['same', 'next'], v2=['same', 'next'], generation=2))
