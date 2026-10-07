"""Execute the real shell admission/retry flow with isolated fake IO commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


DRIVER = r'''
import json,os,pathlib,sys
root=pathlib.Path(os.environ['FIXTURE_ROOT']);args=sys.argv[1:]
name=pathlib.Path(sys.argv[0]).name
with (root/'calls.jsonl').open('a') as f:f.write(json.dumps([name,args])+'\n')
countpath=root/'clones'
count=int(countpath.read_text()) if countpath.exists() else 0
if name=='git':
 if 'clone' in args:
  pathlib.Path(args[-1]).mkdir(parents=True);countpath.write_text(str(count+1))
 elif 'rev-parse' in args:print('base-'+str(count))
 elif 'remote' in args:print('/fixture/remote')
elif args[:3]==['-m','research.hunter_scheduler_health','gate']:
 print(json.dumps({'process':not (count>1 and os.environ.get('GATE_DUPLICATE')),'generation_id':'bucket'}))
elif args[:1]==['-c']:
 gate=json.loads(sys.stdin.read());print(str(gate['process']).lower() if 'process' in args[1] else gate['generation_id'])
elif args and args[0]=='-':sys.stdin.read()
elif args[:2]==['scripts/hunter_monitor_persist.py','validate'] and os.environ.get('VALIDATE_FAIL'):
 print('RuntimeError: VALIDATION_FAILED',file=sys.stderr);sys.exit(1)
elif args[:2]==['scripts/hunter_monitor_persist.py','persist']:
 path=root/'persists';n=int(path.read_text())+1 if path.exists() else 1;path.write_text(str(n))
 if n<=int(os.environ.get('FAIL_UNTIL','1')):
  print(os.environ.get('FAILURE','RuntimeError: SHADOW_STATE_CAS_REJECTED_STALE_WRITER research/hunter_changed.py'),file=sys.stderr)
  sys.exit(1)
 print('SHADOW_STATE_PUSH_VERIFIED')
'''


class RetryTests(unittest.TestCase):
    def run_fixture(self, **settings):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);bindir=root/'bin';bindir.mkdir()
            source=Path('scripts/hunter_monitor_runner.sh').read_text()
            source=source.replace('/run/lock/hunter-position-monitor.lock',str(root/'lock'))
            source=source.replace('/opt/shadow-runner/btc-grid-state',str(root/'source'))
            source=source.replace('/opt/shadow-runner/monitor-generation.',str(root/'generation.'))
            runner=root/'runner.sh';runner.write_text(source);runner.chmod(0o755)
            for name in ('git','python3'):
                tool=bindir/name;tool.write_text('#!'+sys.executable+'\n'+DRIVER);tool.chmod(0o755)
            env=dict(os.environ,PATH=str(bindir)+os.pathsep+os.environ['PATH'],FIXTURE_ROOT=str(root),**settings)
            env.pop('HUNTER_MONITOR_CAS_RETRY_COUNT',None)
            result=subprocess.run([str(runner)],env=env,capture_output=True,text=True,timeout=10)
            calls=[json.loads(x) for x in (root/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(list(root.glob('generation.*')),[])  # no stale worktree reuse
            return result,calls

    def test_cas_retries_entire_fresh_generation(self):
        result,calls=self.run_fixture()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('HUNTER_MONITOR_FRESH_CAS_RETRY attempt=1',result.stdout)
        self.assertEqual(sum(a[:2]==['-m','research.hunter_position_monitor'] for _,a in calls),2)
        self.assertEqual(sum('clone' in a for n,a in calls if n=='git'),2)
        self.assertEqual(sum(a[:2]==['-m','unittest'] for _,a in calls),2)
        bases=[a[a.index('--base')+1] for _,a in calls if '--base' in a]
        self.assertEqual(bases,['base-1','base-2'])

    def test_repeated_cas_is_bounded(self):
        result,calls=self.run_fixture(FAIL_UNTIL='9')
        self.assertEqual(result.returncode,1)
        self.assertEqual(sum('clone' in a for n,a in calls if n=='git'),3)
        self.assertNotIn('HUNTER_MONITOR_SUCCESS',result.stdout)

    def test_auth_policy_unknown_and_unanchored_text_do_not_retry(self):
        for failure in ['RuntimeError: AUTHENTICATION_FAILED','RuntimeError: GH013_POLICY_REJECTED',
                        'RuntimeError: UNKNOWN_ERROR','log mentions RuntimeError: SHADOW_STATE_CAS_REJECTED_STALE_WRITER']:
            with self.subTest(failure=failure):
                result,calls=self.run_fixture(FAILURE=failure)
                self.assertEqual(result.returncode,1)
                self.assertEqual(sum('clone' in a for n,a in calls if n=='git'),1)

    def test_validation_failure_is_not_retried(self):
        result,calls=self.run_fixture(VALIDATE_FAIL='1')
        self.assertEqual(result.returncode,1)
        self.assertFalse(any('--base' in a for _,a in calls))

    def test_reentry_respects_generation_already_processed(self):
        result,calls=self.run_fixture(GATE_DUPLICATE='1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('ALREADY_PROCESSED generation=bucket',result.stdout)
        self.assertEqual(sum(a[:3]==['-m','research.hunter_scheduler_health','gate'] for _,a in calls),2)
        self.assertEqual(sum(a[:2]==['-m','research.hunter_position_monitor'] for _,a in calls),1)
        self.assertEqual(sum('--base' in a for _,a in calls),1)
