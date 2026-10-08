import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1]/'scripts/hunter_scheduled_job.sh'


class AuxFreshRetryTests(unittest.TestCase):
    def run_wrapper(self,job='discovery',failure='AUX_CAS_REJECTED_STALE_WRITER',always=False,nested=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir()
            def git(*args):
                return subprocess.run(['git','-C',str(source),*args],check=True,capture_output=True,text=True).stdout.strip()
            git('init','-b','main');git('config','user.name','fixture');git('config','user.email','fixture@example.test')
            (source/'version').write_text('old');git('add','version');git('commit','-m','old');git('remote','add','origin',str(source))
            old=git('rev-parse','HEAD')
            fake=root/'bin';fake.mkdir();records=root/'records.json'
            program=fake/'python3'
            program.write_text('''#!/usr/bin/python3
import json,os,pathlib,subprocess,sys
if 'scripts.hunter_market_stream' in sys.argv:sys.exit(0)
path=pathlib.Path(os.environ['TEST_RECORDS'])
rows=json.loads(path.read_text()) if path.exists() else []
rows.append({'cwd':os.getcwd(),'source':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'args':sys.argv[1:]})
path.write_text(json.dumps(rows))
if len(rows)==1 and os.environ['TEST_NESTED']=='1':
 result=subprocess.run(['bash',os.environ['TEST_RUNNER'],sys.argv[3]],capture_output=True,text=True,timeout=5)
 print(result.stdout)
 if result.returncode:sys.exit(24)
if len(rows)==1:
 source=pathlib.Path(os.environ['TEST_SOURCE'])
 (source/'version').write_text('new')
 subprocess.run(['git','-C',str(source),'add','version'],check=True,capture_output=True)
 subprocess.run(['git','-C',str(source),'commit','-m','new'],check=True,capture_output=True)
if len(rows)==1 or os.environ['TEST_ALWAYS']=='1':
 print('RuntimeError: '+os.environ['TEST_FAILURE']+' changed-code.py',file=sys.stderr)
 sys.exit(23)
print('FULL_JOB_RECOMPUTED_FROM_FRESH_MAIN')
''');program.chmod(0o755)
            script=root/'runner.sh';script.write_text(SCRIPT.read_text().replace('/opt/shadow-runner/btc-grid-state',str(source)).replace('/opt/shadow-runner/job-generation.',str(root/'generation.')).replace('/opt/shadow-runner/job-retry.',str(root/'retry.')).replace('/run/lock/hunter-job-',str(root/'lock-')))
            env=dict(os.environ,PATH=str(fake)+':'+os.environ['PATH'],TEST_RECORDS=str(records),TEST_SOURCE=str(source),TEST_FAILURE=failure,TEST_ALWAYS='1' if always else '0',TEST_NESTED='1' if nested else '0',TEST_RUNNER=str(script))
            run=subprocess.run(['bash',str(script),job,'--fixture-argument'],env=env,capture_output=True,text=True,timeout=30)
            rows=json.loads(records.read_text()) if records.exists() else []
            return run,rows,old,git('rev-parse','HEAD'),list(root.glob('generation.*')),list(root.glob('retry.*'))

    def test_discovery_recomputes_from_new_source_and_keeps_arguments(self):
        run,rows,old,new,dirs,logs=self.run_wrapper()
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        self.assertEqual(len(rows),2);self.assertEqual(rows[0]['source'],old);self.assertEqual(rows[1]['source'],new)
        self.assertNotEqual(rows[0]['cwd'],rows[1]['cwd'])
        self.assertEqual(rows[1]['args'],['-m','scripts.hunter_job_runner','discovery','--fixture-argument'])
        self.assertIn('HUNTER_AUX_FRESH_SOURCE_RETRY',run.stdout)
        self.assertEqual(dirs,[]);self.assertEqual(logs,[])

    def test_blind_and_missed_replay_retry_whole_job(self):
        for job in ('blind-replay','missed-replay'):
            with self.subTest(job=job):
                run,rows,*_=self.run_wrapper(job)
                self.assertEqual(run.returncode,0,run.stderr);self.assertEqual(len(rows),2)

    def test_exact_aux_race_and_transport_errors_can_retry(self):
        for code in ('AUX_PUSH_RETRY_EXHAUSTED_RACE','AUX_PUSH_RETRY_EXHAUSTED_TRANSPORT_FAILED'):
            with self.subTest(code=code):
                run,rows,*_=self.run_wrapper(failure=code)
                self.assertEqual(run.returncode,0);self.assertEqual(len(rows),2)

    def test_auth_data_validator_and_unknown_errors_never_retry(self):
        for code in ('AUX_AUTHENTICATION_FAILED','SIGNAL_DATA_MISSING','VALIDATION_FAILED','UNKNOWN','AUX_CAS_REJECTED_STALE_WRITER_EXTRA'):
            with self.subTest(code=code):
                run,rows,*_=self.run_wrapper(failure=code)
                self.assertEqual(run.returncode,23);self.assertEqual(len(rows),1)

    def test_retry_limit_and_exit_status_are_preserved(self):
        run,rows,*_=self.run_wrapper(always=True)
        self.assertEqual(run.returncode,23);self.assertEqual(len(rows),3)
        self.assertEqual(run.stdout.count('HUNTER_AUX_FRESH_SOURCE_RETRY'),2)

    def test_research_and_watchdog_do_not_enter_aux_retry(self):
        for job in ('research','watchdog'):
            with self.subTest(job=job):
                run,rows,*_=self.run_wrapper(job)
                self.assertEqual(run.returncode,23);self.assertEqual(len(rows),1)

    def test_concurrent_service_admission_cannot_run_a_second_job(self):
        run,rows,*_=self.run_wrapper(failure='AUTH_FAILURE',nested=True)
        self.assertEqual(run.returncode,23,run.stdout+run.stderr)
        self.assertEqual(len(rows),1)
        self.assertIn('HUNTER_JOB_ALREADY_RUNNING discovery',run.stdout)


if __name__=='__main__':unittest.main()
