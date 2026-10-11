"""Research runner lock acceptance using Linux flock, real Bash and temp files.

Only workflow discovery and Git transport are replaced. The runner, shell child
lifecycle, portfolio lock and final readback implementation remain real. The
publication marker represents entry to a publish step; no Git remote is used.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

LINUX = sys.platform.startswith("linux") and shutil.which("bash") is not None
if LINUX:
    import fcntl
    from scripts import hunter_job_runner as runner
    from scripts.hunter_portfolio_lock import PortfolioLock


PROBE = r'''
import fcntl,json,os,pathlib,sys
path=pathlib.Path(os.environ['RESEARCH_TEST_LOCK'])
label,expect_held=sys.argv[1],sys.argv[2]=='held'
if expect_held:
    target=path.stat()
    inherited=[]
    for name in os.listdir('/proc/self/fd'):
        try:
            fd=int(name)
            info=os.fstat(fd)
            if fd>2 and (info.st_dev,info.st_ino)==(target.st_dev,target.st_ino):
                inherited.append(fd)
        except (OSError,ValueError):
            pass
    assert inherited,'BASH_CHILD_DID_NOT_INHERIT_PORTFOLIO_FD'
with path.open('a+') as contender:
    try:
        fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
        held=False
    except BlockingIOError:
        held=True
    assert held==expect_held,(label,held,expect_held)
with pathlib.Path(os.environ['RESEARCH_TEST_STAGES']).open('a') as out:
    out.write(label+'\n')
if label=='persist':
    pathlib.Path(os.environ['RESEARCH_TEST_PUBLISHED']).write_text('published')
'''


# Deliberately small IO/strategy adapter for the REAL monitor shell below. It
# never invokes Git/network or either trading engine. The production lock helper
# is exec'ed unchanged, sharing Bash's FD 9 and the real Research runner's flock.
COMBINED_IO = r'''
import fcntl,json,os,pathlib,sys,time
root=pathlib.Path(os.environ['COMBINED_ROOT'])
args=sys.argv[1:];name=pathlib.Path(sys.argv[0]).name
names=('hunter-shadow-portfolio.json','hunter-shadow-v2-portfolio.json')
def audit(event,**fields):
 with (root/'combined-audit.jsonl').open('a') as f:
  f.write(json.dumps(dict(event=event,**fields))+'\n')
def pair(directory):
 docs=[json.loads((directory/n).read_text()) for n in names]
 assert docs[0]==docs[1],'MIXED_LEDGER_GENERATION_OR_HISTORY'
 return docs[0]
def write_pair(directory,doc):
 raw=json.dumps(doc,sort_keys=True)
 for n in names:
  (directory/n).write_text(raw)
  time.sleep(.01)
def held():
 with (root/'portfolio.lock').open('a+') as f:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return
  raise AssertionError('ENTRYPOINT_MUTATION_WITHOUT_SHARED_LOCK')
def has(doc,marker):return any(e['id']==marker for e in doc['events'])
shared=root/'shared'
if name=='git':
 if 'clone' in args:pathlib.Path(args[-1]).mkdir(parents=True)
 elif 'rev-parse' in args:print('base-'+str(pair(shared)['fixture_generation']))
 elif 'remote' in args:print('/fixture-only-no-network')
 elif args[0] not in ('fetch','checkout','config'):raise AssertionError(args)
elif args[:2]==['-m','scripts.hunter_portfolio_lock']:
 os.execv(sys.executable,[sys.executable,os.environ['COMBINED_LOCK_HELPER'],*args[2:]])
elif args[:2]==['-m','unittest']:
 pass
elif args[:2]==['scripts/hunter_monitor_persist.py','rebind']:
 held();doc=pair(shared)
 (root/'monitor-bound.json').write_text(json.dumps(doc))
 audit('monitor_bind',generation=doc['fixture_generation'])
elif args[:3]==['-m','research.hunter_scheduler_health','gate']:
 held();doc=pair(shared)
 print(json.dumps(dict(process=not has(doc,'monitor:g1'),generation_id='fixture-monitor-g1')))
elif args[:1]==['-c']:
 gate=json.loads(sys.stdin.read())
 print(str(gate['process']).lower() if 'process' in args[1] else gate['generation_id'])
elif args[:2]==['-m','research.hunter_position_monitor']:
 held();audit('monitor_evaluate')
 (root/'monitor-evaluating').write_text('ready')
 end=time.monotonic()+10
 while not (root/'research-waiting').exists():
  assert time.monotonic()<end,'RESEARCH_DID_NOT_REACH_BIND'
  time.sleep(.01)
elif args[:3]==['-m','research.hunter_scheduler_health','success']:
 held()
elif args[:2]==['scripts/hunter_monitor_persist.py','validate']:
 held()
elif args[:2]==['scripts/hunter_monitor_persist.py','persist']:
 held();doc=json.loads((root/'monitor-bound.json').read_text())
 assert pair(shared)==doc,'FIXTURE_CAS_CONFLICT'
 assert not has(doc,'monitor:g1'),'DUPLICATE_MONITOR_MARKER'
 doc['events'].append({'id':'monitor:g1'});doc['fixture_generation']+=1
 write_pair(shared,doc);audit('monitor_publish',generation=doc['fixture_generation'])
 assert pair(shared)==doc
 audit('monitor_readback',generation=doc['fixture_generation'])
 print('FIXTURE_MONITOR_POST_PUBLISH_READBACK_OK')
else:raise AssertionError((name,args))
'''


COMBINED_RESEARCH = r'''
import fcntl,json,os,pathlib,sys,time
root=pathlib.Path(os.environ['COMBINED_ROOT']);mode=sys.argv[1]
names=('hunter-shadow-portfolio.json','hunter-shadow-v2-portfolio.json')
def pair(directory):
 docs=[json.loads((directory/n).read_text()) for n in names]
 assert docs[0]==docs[1],'MIXED_LEDGER_GENERATION_OR_HISTORY'
 return docs[0]
def write_pair(directory,doc):
 for n in names:
  (directory/n).write_text(json.dumps(doc,sort_keys=True));time.sleep(.01)
def audit(event,doc):
 with (root/'combined-audit.jsonl').open('a') as f:
  f.write(json.dumps(dict(event=event,generation=doc['fixture_generation']))+'\n')
with (root/'portfolio.lock').open('a+') as f:
 try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:pass
 else:raise AssertionError('RESEARCH_STEP_WITHOUT_SHARED_LOCK')
shared=root/'shared';work=root/'results'
if mode=='bind':
 doc=pair(shared)
 assert any(e['id']=='monitor:g1' for e in doc['events']),'BOUND_OLD_PREPARED_SNAPSHOT'
 write_pair(work,doc);audit('research_bind',doc)
elif mode=='execute':
 doc=pair(work)
 if not any(e['id']=='research:g1' for e in doc['events']):
  doc['events'].append({'id':'research:g1'});doc['fixture_generation']+=1
 write_pair(work,doc)
elif mode=='persist':
 doc=pair(work);before=pair(shared)
 if before!=doc:
  assert doc['fixture_generation']==before['fixture_generation']+1,'FIXTURE_CAS_CONFLICT'
  assert doc['events'][:-1]==before['events'],'HISTORY_REWRITE'
  write_pair(shared,doc);audit('research_publish',doc)
 else:audit('research_duplicate',doc)
else:raise AssertionError(mode)
'''


@unittest.skipUnless(LINUX, "Linux flock, /proc and Bash are required")
class ResearchLockAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-lock-acceptance-")
        self.root = Path(self.temp.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.lock_path = self.root / "portfolio.lock"
        self.stage_path = self.root / "stages"
        self.published = self.root / "published"
        (self.results / "hunter-cex-universe-run.json").write_text(
            json.dumps({"generation_id": "fixture-generation"}))
        self.original_ledgers = {}
        for name in runner.PORTFOLIOS:
            raw = json.dumps({"events": [{"id": "existing-history"}],
                              "open_positions": [], "mode": "SIMULATION_ONLY_NO_REAL_ORDERS"}).encode()
            path = self.results / name
            path.write_bytes(raw)
            self.original_ledgers[str(path)] = raw
        self.environment = {
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_ENV": str(self.root / "env"),
            "RESEARCH_TEST_LOCK": str(self.lock_path),
            "RESEARCH_TEST_STAGES": str(self.stage_path),
            "RESEARCH_TEST_PUBLISHED": str(self.published),
        }
        self.locks = []
        self.git_calls = []
        self.readback_paths = []
        self.readback_started = False
        self.mismatch_readback = False
        self.readback_pause = 0
        self.elapsed_at_acquire = 0
        self.rows = []
        self.log = io.StringIO()

    def tearDown(self):
        for lock in self.locks:
            if not lock.file.closed:
                lock.close("test-cleanup")
        self.temp.cleanup()

    def assert_lock_held(self):
        # A distinct open file description must contend even in this process.
        # Do not call PortfolioLock.acquire here: readback has an active timer.
        with self.lock_path.open("a+") as contender:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def assert_lock_released(self):
        self.assertTrue(self.locks)
        self.assertTrue(all(lock.file.closed for lock in self.locks))
        with self.lock_path.open("a+") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with contextlib.redirect_stdout(io.StringIO()):
            successor = PortfolioLock("monitor", path=str(self.lock_path)).acquire(.5)
            successor.close()

    def lock_factory(self, role, generation, base):
        test = self

        class FixtureLock(PortfolioLock):
            def acquire(self, timeout=120):
                super().acquire(timeout)
                # Simulate time already spent under the real lock without a
                # ninety-second test. All runner deadline calculations stay real.
                self.acquired -= test.elapsed_at_acquire
                return self

        lock = FixtureLock(role, generation, base, path=str(self.lock_path))
        self.locks.append(lock)
        return lock

    def fake_git(self, *args, **kwargs):
        self.git_calls.append(args)
        if args == ("fetch", "origin", "main"):
            self.readback_started = True
            self.assert_lock_held()
            if self.readback_pause:
                time.sleep(self.readback_pause)
        elif self.locks and not self.locks[-1].file.closed:
            self.assert_lock_held()
        if args == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(["git", *args], 0, "a" * 40 + "\n", "")
        if args == ("fetch", "origin", "main") or args[:1] == ("merge-base",):
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        self.fail("Unexpected Git operation: " + repr(args))

    def fake_git_show(self, args, **kwargs):
        # readback's byte comparison is exercised, not mocked away.
        self.assertEqual(args[:2], ["git", "show"])
        self.assertTrue(args[2].startswith("origin/main:"))
        path = args[2].split(":", 1)[1]
        self.assertIn(path, self.original_ledgers)
        self.assert_lock_held()
        self.readback_paths.append(path)
        raw = b"different-authoritative-state" if self.mismatch_readback else self.original_ledgers[path]
        return subprocess.CompletedProcess(args, 0, raw, b"")

    def probe_step(self, name, label, held=True):
        code = " ".join(shlex.quote(x) for x in
                        (sys.executable, "-c", PROBE, label, "held" if held else "free"))
        return {"name": name, "run": code}

    def execute(self, steps):
        with mock.patch.dict(os.environ, self.environment), \
             mock.patch.object(runner, "RESULTS", self.results), \
             mock.patch.object(runner, "workflow_steps", return_value=steps), \
             mock.patch.object(runner, "git", side_effect=self.fake_git), \
             mock.patch.object(runner.subprocess, "run", side_effect=self.fake_git_show), \
             mock.patch.object(runner, "PortfolioLock", side_effect=self.lock_factory), \
             contextlib.redirect_stdout(self.log):
            return runner.run_job("research", steps=self.rows)

    def test_real_shell_and_both_ledger_readbacks_keep_the_lock(self):
        steps = [self.probe_step("Prepare tested code", "prepare", held=False),
                 self.probe_step("Bind latest authoritative monitor state", "bind"),
                 self.probe_step("Revalidate capital decisions", "revalidate"),
                 self.probe_step("Run shadow execution", "execute"),
                 self.probe_step("Persist accepted state", "persist")]
        rows = self.execute(steps)
        self.assertEqual(self.stage_path.read_text().splitlines(),
                         ["prepare", "bind", "revalidate", "execute", "persist"])
        self.assertEqual(set(self.readback_paths), set(self.original_ledgers))
        self.assertEqual(rows[-1]["name"], "Authoritative main read-back")
        self.assertTrue(all(row["status"] == "SUCCESS" for row in rows))
        self.assertGreaterEqual(rows[1]["lock_wait_seconds"], 0)
        self.assertTrue(self.published.exists())
        self.assert_lock_released()

    def test_actual_readback_mismatch_marks_failure_and_releases(self):
        self.mismatch_readback = True
        with self.assertRaisesRegex(RuntimeError, "JOB_MAIN_READBACK_MISMATCH"):
            self.execute([self.probe_step("Bind latest authoritative monitor state", "bind")])
        self.assertTrue(self.readback_started)
        self.assertEqual(self.rows[-1]["name"], "Authoritative main read-back")
        self.assertEqual(self.rows[-1]["status"], "FAILURE")
        self.assertIn("JOB_MAIN_READBACK_MISMATCH", self.rows[-1]["error_details"]["message"])
        self.assertNotIn("HUNTER_JOB_MAIN_READBACK_OK", self.log.getvalue())
        self.assert_lock_released()

    def test_readback_consumes_remaining_ninety_second_budget(self):
        self.elapsed_at_acquire = 88.5
        self.readback_pause = 3
        handler_before = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "PORTFOLIO_CRITICAL_PHASE_TIMEOUT"):
            # Real Bash runs here; ':' avoids interpreter startup consuming the
            # short remainder before the actual readback deadline is exercised.
            self.execute([{"name": "Bind latest authoritative monitor state", "run": ":"}])
        self.assertTrue(self.readback_started)
        self.assertLess(time.monotonic() - started, 2.75)
        self.assertEqual(self.rows[-1]["name"], "Authoritative main read-back")
        self.assertEqual(self.rows[-1]["status"], "FAILURE")
        self.assertEqual(self.rows[-1]["error_details"]["message"],
                         "PORTFOLIO_CRITICAL_PHASE_TIMEOUT")
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual(signal.getsignal(signal.SIGALRM), handler_before)
        self.assertEqual(self.readback_paths, [])
        self.assert_lock_released()

    def test_failed_real_process_never_reaches_publication_or_changes_history(self):
        steps = [self.probe_step("Bind latest authoritative monitor state", "bind"),
                 {"name": "Failing shadow process", "run": "kill -KILL $$"},
                 self.probe_step("Persist accepted state", "persist")]
        with self.assertRaisesRegex(RuntimeError, "JOB_STEP_FAILED Failing shadow process"):
            self.execute(steps)
        self.assertEqual(self.rows[-1]["name"], "Failing shadow process")
        self.assertEqual(self.rows[-1]["status"], "FAILURE")
        self.assertEqual(self.rows[-1]["exit"], -signal.SIGKILL)
        self.assertEqual(self.stage_path.read_text().splitlines(), ["bind"])
        self.assertFalse(self.published.exists())
        self.assertFalse(self.readback_started)
        for path, raw in self.original_ledgers.items():
            self.assertEqual(Path(path).read_bytes(), raw)
        self.assert_lock_released()

    def test_real_research_and_monitor_entrypoints_share_lock_and_fresh_pair(self):
        """Real entrypoint contention; synthetic IO/decisions, not strategy replay."""
        shared = self.root / "shared"
        source = self.root / "source"
        bindir = self.root / "bin"
        for directory in (shared, source, bindir):
            directory.mkdir()
        initial = dict(fixture_generation=0, events=[{"id": "existing-history"}],
                       real_trading_enabled=False, capital_authority="NONE_SHADOW_ONLY")
        for directory in (shared, self.results):
            for name in runner.PORTFOLIOS:
                (directory / name).write_text(json.dumps(initial, sort_keys=True))
        repository = Path(__file__).resolve().parents[1]
        monitor_source = (repository / "scripts/hunter_monitor_runner.sh").read_text()
        monitor_source = monitor_source.replace("/run/lock/hunter-position-monitor.lock", str(self.lock_path))
        monitor_source = monitor_source.replace("/opt/shadow-runner/btc-grid-state", str(source))
        monitor_source = monitor_source.replace("/opt/shadow-runner/monitor-generation.",
                                                str(self.root / "monitor-generation."))
        wrapper = self.root / "monitor.sh"
        wrapper.write_text(monitor_source)
        for name in ("git", "python3"):
            command = bindir / name
            command.write_text("#!" + sys.executable + "\n" + COMBINED_IO)
            command.chmod(0o755)
        env = dict(os.environ, **self.environment, COMBINED_ROOT=str(self.root),
                   COMBINED_LOCK_HELPER=str(repository / "scripts/hunter_portfolio_lock.py"))
        env["PATH"] = str(bindir) + os.pathsep + os.environ["PATH"]
        env.pop("HUNTER_MONITOR_CAS_RETRY_COUNT", None)
        steps = [{"name": name, "run": " ".join(shlex.quote(s) for s in
                  (sys.executable, "-c", COMBINED_RESEARCH, mode))}
                 for name, mode in [("Bind latest authoritative monitor state", "bind"),
                                    ("Run isolated shadow decision", "execute"),
                                    ("Persist isolated accepted state", "persist")]]
        original_factory = self.lock_factory

        def contending_lock(*args):
            lock = original_factory(*args)
            original_acquire = lock.acquire

            def acquire(timeout=120):
                if not (self.root / "research-waiting").exists():
                    # Monitor cannot finish its fixture evaluation until this
                    # marker appears, so contention is established by the
                    # kernel, not a scheduler-sensitive minimum elapsed time.
                    self.assert_lock_held()
                (self.root / "research-waiting").write_text("ready")
                return original_acquire(min(timeout, 10))

            lock.acquire = acquire
            return lock

        def authoritative_show(args, **kwargs):
            self.assertEqual(args[:2], ["git", "show"])
            self.assert_lock_held()
            docs = [(shared / n).read_bytes() for n in runner.PORTFOLIOS]
            self.assertEqual(docs[0], docs[1])
            self.assertEqual(json.loads(docs[0])["fixture_generation"], 2)
            path = Path(args[2].split(":", 1)[1])
            self.assertIn(path.name, runner.PORTFOLIOS)
            with (self.root / "combined-audit.jsonl").open("a") as audit:
                audit.write(json.dumps(dict(event="research_readback", generation=2)) + "\n")
            return subprocess.CompletedProcess(args, 0, (shared / path.name).read_bytes(), b"")

        def research_run():
            rows = []
            with mock.patch.dict(os.environ, {**self.environment, "COMBINED_ROOT": str(self.root)}), \
                 mock.patch.object(runner, "RESULTS", self.results), \
                 mock.patch.object(runner, "workflow_steps", return_value=steps), \
                 mock.patch.object(runner, "git", side_effect=self.fake_git), \
                 mock.patch.object(runner.subprocess, "run", side_effect=authoritative_show), \
                 mock.patch.object(runner, "PortfolioLock", side_effect=contending_lock), \
                 contextlib.redirect_stdout(self.log):
                runner.run_job("research", steps=rows)
            self.assertTrue(all(row["status"] == "SUCCESS" for row in rows), rows)

        monitor = subprocess.Popen([shutil.which("bash"), str(wrapper)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            end = time.monotonic() + 10
            while not (self.root / "monitor-evaluating").exists():
                self.assertIsNone(monitor.poll(), "Monitor exited before evaluation")
                self.assertLess(time.monotonic(), end, "Monitor did not reach evaluation")
                time.sleep(.01)
            research_run()
            out, err = monitor.communicate(timeout=10)
            self.assertEqual(monitor.returncode, 0, out + err)
            self.assertGreaterEqual(self.locks[0].wait_seconds, 0)
            self.assertIn("HUNTER_MONITOR_SUCCESS", out)
        finally:
            try:
                os.killpg(monitor.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            monitor.communicate(timeout=5)

        # Repeat each REAL entrypoint: fixture generation/marker guards must
        # prevent another logical publication and retain the original history.
        duplicate = subprocess.Popen([shutil.which("bash"), str(wrapper)], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, start_new_session=True)
        try:
            duplicate_out, duplicate_err = duplicate.communicate(timeout=20)
            self.assertEqual(duplicate.returncode, 0, duplicate_out + duplicate_err)
            self.assertIn("ALREADY_PROCESSED", duplicate_out)
        finally:
            try:
                os.killpg(duplicate.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            duplicate.communicate(timeout=5)
        research_run()
        final = [json.loads((shared / n).read_text()) for n in runner.PORTFOLIOS]
        self.assertEqual(final[0], final[1])
        self.assertEqual(final[0]["fixture_generation"], 2)
        self.assertEqual(final[0]["events"],
                         [{"id": "existing-history"}, {"id": "monitor:g1"}, {"id": "research:g1"}])
        audit = [json.loads(line) for line in (self.root / "combined-audit.jsonl").read_text().splitlines()]
        events = [row["event"] for row in audit]
        for event in ("monitor_evaluate", "monitor_publish", "research_publish", "research_duplicate"):
            self.assertEqual(events.count(event), 1, audit)
        self.assertLess(events.index("monitor_readback"), events.index("research_bind"))
        self.assertLess(events.index("research_publish"), events.index("research_readback"))
        self.assertEqual([row["generation"] for row in audit if row["event"] == "research_bind"], [1, 2])
        self.assertEqual(list(self.root.glob("monitor-generation.*")), [])
        self.assert_lock_released()


if __name__ == "__main__":
    unittest.main()
