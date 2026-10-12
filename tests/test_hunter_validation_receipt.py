import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from scripts import hunter_validation_receipt as receipt


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        manifest = json.loads((receipt.ROOT / receipt.MANIFEST).read_text())
        for name in [*manifest['reviewed_files'], receipt.MANIFEST, 'scripts/hunter_validation_receipt.py']:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(receipt.ROOT / name, target)
        # The production manifest records Git/LF bytes. Normalize ONLY this
        # disposable fixture so Windows can exercise the dependency contract.
        for name in manifest['reviewed_files']:
            path = self.root / name
            path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
        self.runtime = mock.patch.object(receipt, 'runtime_identity', return_value={'python': 'fixture', 'lock': 'stdlib-only', 'env': {'TZ': 'UTC'}})
        self.runtime_mock = self.runtime.start()
        self.addCleanup(self.runtime.stop)

    def test_code_and_test_changes_require_reaudit(self):
        original = receipt.fingerprint(self.root)
        for name in ('research/hunter_execution_cost.py', 'tests/test_hunter_execution_cost.py'):
            path = self.root / name; raw = path.read_bytes()
            path.write_bytes(raw + b'\n# changed\n')
            with self.assertRaisesRegex(receipt.Unavailable, 'DEPENDENCY_REVIEW'): receipt.fingerprint(self.root)
            path.write_bytes(raw)
        self.assertEqual(receipt.fingerprint(self.root), original)

    def test_fixture_lock_python_and_environment_changes_invalidate(self):
        original = receipt.fingerprint(self.root)
        fixture = self.root / 'tests/fixtures/contract.json'
        fixture.parent.mkdir(); fixture.write_text('{"fixture":1}')
        first = receipt.fingerprint(self.root)
        self.assertNotEqual(original, first)
        fixture.write_text('{"fixture":2}')
        self.assertNotEqual(first, receipt.fingerprint(self.root))
        for identity in ({'python': 'different'}, {'lock': 'different'}, {'env': {'TZ': 'different'}}):
            self.runtime_mock.return_value = identity
            self.assertNotEqual(first, receipt.fingerprint(self.root))
        path = self.root / receipt.MANIFEST
        doc = json.loads(path.read_text()); doc['third_party_dependencies'] = ['unpinned-package']
        path.write_text(json.dumps(doc))
        with self.assertRaises(receipt.Unavailable): receipt.fingerprint(self.root)

    def test_dynamic_state_and_commit_identity_are_not_pure_dependencies(self):
        original = receipt.fingerprint(self.root)
        state = self.root / 'research/results/hunter-shadow-v2-portfolio.json'
        state.parent.mkdir(); state.write_text('{"generation":"new","capital":0}')
        (self.root / '.git').mkdir(); (self.root / '.git/HEAD').write_text('new-ledger-commit')
        self.assertEqual(receipt.fingerprint(self.root), original)
        # This says nothing about those states' validity: full validation and
        # dynamic workflow guards still execute (see integration cases below).

    def test_added_initializer_or_missing_dependency_is_not_certified(self):
        (self.root / 'research/__init__.py').write_text('import os')
        with self.assertRaises(receipt.Unavailable): receipt.fingerprint(self.root)
        (self.root / 'research/__init__.py').unlink()
        (self.root / 'research/hunter_execution_cost.py').unlink()
        with self.assertRaises(receipt.Unavailable): receipt.fingerprint(self.root)

    def test_default_off_never_inspects_store_or_environment(self):
        with mock.patch.object(receipt, 'fingerprint', side_effect=AssertionError):
            self.assertEqual(receipt.observe({})['mode'], 'off')
        with mock.patch.object(receipt, 'fingerprint', side_effect=receipt.Unavailable):
            row = receipt.observe({'HUNTER_VALIDATION_RECEIPTS': 'compare'})
            self.assertEqual(row['status'], 'FALLBACK_FULL_VALIDATION')
            self.assertFalse(row['hit'])

    def test_incomplete_or_malformed_manifest_cannot_certify_new_dependencies(self):
        path = self.root / receipt.MANIFEST
        original = json.loads(path.read_text())
        for doc in ([], {}, dict(original, reviewed_files={}), dict(original, reviewed_files=[]),
                    dict(original, modules=['tests.unreviewed']), dict(original, test_count=0)):
            path.write_text(json.dumps(doc))
            with self.assertRaises(receipt.Unavailable): receipt.fingerprint(self.root)

    def test_candidate_ignores_inherited_environment_and_existing_bytecode(self):
        seen = []
        def run(command, **kwargs):
            self.assertEqual(command[1:5], ['-I','-S','-B','-X'])
            cache = Path(command[5].split('=',1)[1]); seen.append(cache)
            self.assertTrue(cache.is_dir()); self.assertEqual(list(cache.iterdir()), [])
            stage = Path(kwargs['cwd'])
            self.assertNotEqual(stage, self.root)
            self.assertEqual({p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file()}, receipt.REVIEWED_FILES)
            self.assertEqual(command[6], '-c')
            self.assertEqual(kwargs['env'], {'TZ':'UTC','LC_ALL':'C.UTF-8'})
            return subprocess.CompletedProcess(command, 0, b'HUNTER_PURE_TESTS_OK 7\n')
        with mock.patch.object(receipt.subprocess, 'run', side_effect=run):
            self.assertTrue(receipt.run_candidate(self.root))
        self.assertFalse(seen[0].exists())


@unittest.skipUnless(sys.platform == 'linux', 'real isolated Linux child and extension imports')
class ImportIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        manifest = json.loads((receipt.ROOT / receipt.MANIFEST).read_text())
        for name in [*manifest['reviewed_files'], receipt.MANIFEST, 'scripts/hunter_validation_receipt.py']:
            target = self.root / name; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(receipt.ROOT / name, target)
        self.marker = self.root / 'UNREVIEWED_CODE_EXECUTED'
        self.payload = 'from pathlib import Path\nPath(%r).write_text("unreviewed")\n' % str(self.marker)

    def check_attack(self, install, dirty_count=None):
        # Fingerprint can legitimately ignore these inert extras ONLY because
        # the real child has no access to that checkout through import search.
        with mock.patch.object(receipt, 'runtime_identity', return_value={'runtime':'fixed'}):
            before = receipt.fingerprint(self.root)
            install()
            self.assertEqual(receipt.fingerprint(self.root), before)
        # Positive control reproduces the former checkout import environment.
        code = "import sys,unittest,io; sys.path.insert(0,%r); r=unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in %r)); print(r.testsRun)" % (str(self.root), receipt.MODULES)
        dirty = subprocess.run([sys.executable,'-I','-S','-B','-c',code], cwd=self.root,
                               env=receipt.CHILD_ENV, capture_output=True, text=True, timeout=30)
        self.assertEqual(dirty.returncode, 0, dirty.stderr)
        self.assertTrue(self.marker.exists(), 'positive control must execute attacker code')
        if dirty_count is not None: self.assertEqual(dirty.stdout.strip(), str(dirty_count))
        self.marker.unlink()
        # The production verifier uses a REAL subprocess, never a run() mock.
        self.assertTrue(receipt.run_candidate(self.root))
        self.assertFalse(self.marker.exists())

    def test_sourceless_package_initializer_is_excluded(self):
        import py_compile
        def install():
            source = self.root / 'payload.py'; source.write_text(self.payload)
            py_compile.compile(str(source), cfile=str(self.root/'tests/__init__.pyc'), doraise=True)
            source.unlink()
        self.check_attack(install, dirty_count=7)

    def test_same_named_test_package_cannot_replace_reviewed_tests(self):
        def install():
            package = self.root/'tests/test_hunter_execution_cost'; package.mkdir()
            reviewed = self.root/'tests/test_hunter_execution_cost.py'
            (package/'__init__.py').write_text(self.payload + 'exec(compile(Path(%r).read_bytes(), %r, "exec"), globals())\n' % (str(reviewed),str(reviewed)))
        self.check_attack(install, dirty_count=7)

    def test_same_named_research_package_cannot_replace_reviewed_function(self):
        def install():
            package = self.root/'research/hunter_v1_coverage'; package.mkdir()
            reviewed = self.root/'research/hunter_v1_coverage.py'
            (package/'__init__.py').write_text(self.payload + 'exec(compile(Path(%r).read_bytes(), %r, "exec"), globals())\n' % (str(reviewed),str(reviewed)))
        self.check_attack(install, dirty_count=7)

    def test_real_extension_module_cannot_shadow_reviewed_test(self):
        import sysconfig
        def install():
            compiler = shutil.which('cc'); self.assertIsNotNone(compiler, 'Linux contract requires C compiler')
            source = self.root/'extension.c'
            source.write_text('#include <Python.h>\n#include <stdio.h>\n'
                             'static struct PyModuleDef m = {PyModuleDef_HEAD_INIT,"test_hunter_execution_cost",NULL,-1,NULL};\n'
                             'PyMODINIT_FUNC PyInit_test_hunter_execution_cost(void) {FILE *f=fopen('+json.dumps(str(self.marker))+',"w"); if(f){fputs("unreviewed",f);fclose(f);}return PyModule_Create(&m);}\n')
            target = self.root/'tests'/('test_hunter_execution_cost'+sysconfig.get_config_var('EXT_SUFFIX'))
            result = subprocess.run([compiler,'-shared','-fPIC','-I'+sysconfig.get_path('include'),str(source),'-o',str(target)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
        self.check_attack(install, dirty_count=4)


class BenchmarkCountTests(unittest.TestCase):
    def test_count_parser_rejects_missing_summary_and_keeps_failures(self):
        from scripts.hunter_validation_benchmark import unittest_counts
        counts = unittest_counts('Ran 5 tests in 0.001s\n\nFAILED (failures=1, errors=1, skipped=1, expected failures=1)\n')
        self.assertEqual(counts, dict(tests=5, failures=1, errors=1, skipped=1,
                                     expected_failures=1, unexpected_successes=0))
        with self.assertRaises(ValueError): unittest_counts('incomplete log')

    @unittest.skipUnless(sys.platform == 'linux', 'real GNU time and unittest subprocess')
    def test_real_full_sample_retains_tests_skips_and_failures(self):
        from scripts.hunter_validation_benchmark import measure
        code = '''import unittest
class Example(unittest.TestCase):
 def test_pass(self): pass
 @unittest.skip('fixture')
 def test_skip(self): pass
 def test_fail(self): self.fail('fixture')
 def test_error(self): raise ValueError('fixture')
 @unittest.expectedFailure
 def test_expected(self): self.fail('fixture')
unittest.main()
'''
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            row = measure([sys.executable,'-I','-B','-c',code], Path(directory), full=True)
        self.assertEqual(row['returncode'], 1)
        self.assertEqual(row['test_counts'], dict(tests=5, failures=1, errors=1, skipped=1,
                                                expected_failures=1, unexpected_successes=0))
        self.assertGreater(row['peak_rss_kib'],0)


@unittest.skipUnless(sys.platform == 'linux', 'Linux permission/descriptor contract')
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'receipts'; self.directory.mkdir(mode=0o700)
        self.keys = self.root / 'keys'; self.keys.mkdir(mode=0o700)
        self.key = self.keys / 'signing-key'
        with os.fdopen(os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(os.urandom(32))
        self.fp = 'a' * 64
        self.env = dict(HUNTER_VALIDATION_RECEIPTS='compare', HUNTER_VALIDATION_RECEIPT_DIR=str(self.directory), HUNTER_VALIDATION_RECEIPT_KEY=str(self.key))

    def store(self, writable=True):
        return receipt.open_store(self.directory, self.key, writable)

    def observe(self, success=True):
        with mock.patch.object(receipt, 'fingerprint', return_value=self.fp), mock.patch.object(receipt, 'run_candidate', return_value=success) as run:
            row = receipt.observe(self.env)
            return row, run.call_count

    def seed(self):
        with self.store() as store: store.write_success(self.fp)

    def rewrite(self, change):
        with self.store() as store:
            name = store.slot(self.fp); doc = store.read(name); change(doc)
            (self.directory / name).write_bytes(store.sign(doc))

    def test_fresh_hit_is_authenticated_but_never_skips_comparison(self):
        cold, cold_calls = self.observe(); hot, hot_calls = self.observe()
        self.assertFalse(cold['hit']); self.assertTrue(hot['hit'])
        self.assertEqual((cold_calls, hot_calls), (1, 1))
        self.assertEqual(hot['tests_skipped'], 0)
        self.assertFalse(hot['production_health_evidence'])

    def test_read_only_does_not_modify_files_or_run_candidate(self):
        self.seed(); before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        self.env['HUNTER_VALIDATION_RECEIPTS'] = 'read-only'
        row, calls = self.observe()
        self.assertTrue(row['hit']); self.assertEqual(calls, 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})

    def test_absent_old_future_boot_and_mismatch_fall_back(self):
        self.env['HUNTER_VALIDATION_RECEIPTS'] = 'read-only'
        self.assertFalse(self.observe()[0]['hit'])
        self.seed()
        with self.store() as store: path = self.directory / store.slot(self.fp)
        original = path.read_bytes()
        for change in (lambda d: d.update(issued_ns=time.monotonic_ns()-receipt.TTL_NS-1),
                       lambda d: d.update(issued_ns=time.monotonic_ns()+receipt.TTL_NS),
                       lambda d: d.update(boot='previous-boot'),
                       lambda d: d.update(fingerprint='b'*64),
                       lambda d: d.update(schema='old-schema'),
                       lambda d: d.update(tests=6), lambda d: d.update(success=False)):
            self.rewrite(change)
            self.assertFalse(self.observe()[0]['hit'])
            path.write_bytes(original)

    def test_corrupt_forged_and_oversize_receipts_are_not_adopted(self):
        self.seed()
        with self.store() as store: name = store.slot(self.fp)
        path = self.directory / name; original = path.read_bytes()
        for raw in (b'partial{', b'x'*(receipt.LIMIT+1), original.replace(b'"success":true', b'"success":false'),
                    b'{"status":"SUCCESS"}'):
            path.write_bytes(raw)
            self.assertFalse(self.observe()[0]['hit'])
            self.assertEqual(path.read_bytes(), raw)
        path.write_bytes(original)
        with self.store() as store:
            with self.assertRaises(receipt.Unavailable):
                receipt.Store(store.fd, b'x'*32).lookup(self.fp)

    def test_symlink_and_hardlink_files_and_directory_fall_back(self):
        self.seed()
        with self.store() as store: name = store.slot(self.fp)
        path = self.directory / name; path.unlink()
        outside = self.root / 'user-data'; outside.write_bytes(b'preserve')
        path.symlink_to(outside)
        self.assertFalse(self.observe()[0]['hit']); self.assertEqual(outside.read_bytes(), b'preserve')
        path.unlink(); os.link(outside, path)
        self.assertFalse(self.observe()[0]['hit'])
        alias = self.root / 'alias'; alias.symlink_to(self.directory, target_is_directory=True)
        self.env['HUNTER_VALIDATION_RECEIPT_DIR'] = str(alias)
        self.assertFalse(self.observe()[0]['hit'])

    def test_unsafe_permissions_are_rejected_without_repair(self):
        # Create intentionally unsafe fixture files/directories; production code
        # never chmods an existing object.
        bad = self.root / 'unsafe'; bad.mkdir(mode=0o755)
        self.env['HUNTER_VALIDATION_RECEIPT_DIR'] = str(bad)
        self.assertFalse(self.observe()[0]['hit']); self.assertEqual(bad.stat().st_mode & 0o777, 0o755)
        self.env['HUNTER_VALIDATION_RECEIPT_DIR'] = str(self.directory)
        wrong = self.keys / 'unsafe-key'
        with os.fdopen(os.open(wrong, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), 'wb') as stream: stream.write(b'x'*32)
        self.env['HUNTER_VALIDATION_RECEIPT_KEY'] = str(wrong)
        self.assertFalse(self.observe()[0]['hit']); self.assertEqual(wrong.stat().st_mode & 0o777, 0o644)

    def test_failed_validation_is_never_cached_even_after_hit(self):
        self.seed()
        with self.assertRaisesRegex(RuntimeError, 'COMPARISON_FAILED'): self.observe(False)
        self.env['HUNTER_VALIDATION_RECEIPTS'] = 'read-only'
        self.assertFalse(self.observe()[0]['hit'])

    def test_candidate_timeout_and_failed_tombstone_write_still_fail(self):
        self.seed()
        with mock.patch.object(receipt, 'fingerprint', return_value=self.fp), mock.patch.object(receipt, 'run_candidate', side_effect=subprocess.TimeoutExpired('candidate', 30)):
            with self.assertRaisesRegex(RuntimeError, 'COMPARISON_FAILED'): receipt.observe(self.env)
        # Failed publication must not turn an actual test failure into success.
        with mock.patch.object(receipt, 'fingerprint', return_value=self.fp), mock.patch.object(receipt.Store, 'owned', return_value=True), mock.patch.object(receipt, 'run_candidate', return_value=False), mock.patch.object(receipt.Store, 'atomic_write', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'COMPARISON_FAILED'): receipt.observe(self.env)

    def test_same_input_decisions_and_real_generation_guard_match_all_modes(self):
        import copy
        from research.hunter_shadow_trader_v2 import decision
        from research.hunter_v1_coverage import partition
        from tests.test_hunter_shadow_trader_v2 import V2CapitalDecisionTests
        inputs = V2CapitalDecisionTests().base()
        def decisions():
            c, s, l, u = copy.deepcopy(inputs)
            return [decision(c,s,l,u,'ENTRY'), decision(c,s,{},u,'ENTRY'),
                    partition({'scan_generation_id':'g','early_count':0},
                              {'generation_id':'g','venue_status':{'binance':{'excluded_bstocks':['STOCK']}}})]
        baseline = decisions()
        for mode in ('off', 'compare', 'compare', 'read-only'):
            self.env['HUNTER_VALIDATION_RECEIPTS'] = mode
            self.observe()
            self.assertEqual(decisions(), baseline)
            with self.assertRaisesRegex(AssertionError, 'GENERATION_MISMATCH'):
                partition({'scan_generation_id':'new','early_count':0},
                          {'generation_id':'old','venue_status':{'binance':{'excluded_bstocks':['STOCK']}}})

    def test_concurrent_process_cannot_read_or_publish_under_active_lock(self):
        self.seed()
        script = "from scripts import hunter_validation_receipt as r; import os,json; r.fingerprint=lambda *a:'a'*64; print(json.dumps(r.observe(os.environ)))"
        with self.store():
            run = subprocess.run([sys.executable, '-B', '-c', script], cwd=receipt.ROOT,
                                 env=dict(os.environ, **self.env), text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(json.loads(run.stdout)['hit'])

    def test_killed_writer_or_partial_publication_falls_back_without_deleting(self):
        self.seed()
        child = r'''import os,sys,time
from scripts.hunter_validation_receipt import open_store
with open_store(sys.argv[1], sys.argv[2], True) as store:
 fd=os.open('.pending',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=store.fd)
 os.write(fd,b'partial');os.fsync(fd)
 print('ready',flush=True)
 time.sleep(30)
'''
        proc = subprocess.Popen([sys.executable, '-B', '-c', child, str(self.directory), str(self.key)],
                                cwd=receipt.ROOT, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'ready')
            proc.kill(); proc.wait(timeout=5)
            self.assertFalse(self.observe()[0]['hit'])
            self.assertEqual((self.directory / '.pending').read_bytes(), b'partial')
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdout.close()

    def test_storage_is_bounded_and_never_cleans_user_files(self):
        with self.store() as store:
            for i in range(64): store.write_success('%064x' % (i << 248))
        files = list(self.directory.iterdir())
        self.assertLessEqual(len(files), receipt.SLOTS+2)
        self.assertLessEqual(sum(p.stat().st_size for p in files), (receipt.SLOTS+1)*receipt.LIMIT)
        user = self.directory / 'user-data'; user.write_bytes(b'preserve')
        self.assertFalse(self.observe()[0]['hit']); self.assertEqual(user.read_bytes(), b'preserve')


@unittest.skipUnless(sys.platform == 'linux', 'runner uses POSIX locks and signals')
class WorkflowIntegrationTests(unittest.TestCase):
    def test_original_commands_and_dynamic_guards_run_on_every_hit(self):
        from scripts import hunter_job_runner as runner
        steps = runner.workflow_steps('research')
        # Both full regression AND later shadow validation are retained verbatim.
        names = [s.get('name') for s in steps]
        self.assertIn('Validate shadow engines', names)
        full = next(s for s in steps if s.get('name') == 'Test complete Hunter system before state mutation')
        self.assertIn("unittest discover -s tests -p 'test_hunter*.py'", full['run'])
        self.assertIn('HUNTER_CAPITAL_POOL_MISMATCH', full['run'])
        selected = [full, {'name': 'Current dynamic guard', 'run': 'CURRENT_DYNAMIC_GUARD'},
                    {'name': 'Persist outputs', 'run': 'PERSIST'}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'hunter-cex-universe-run.json').write_text('{"generation_id":"fixture"}')
            for mode in ('off', 'read-only', 'compare'):
                for valid in (True, False):
                    commands = []
                    def shell(code, *args, **kwargs):
                        commands.append(code)
                        return subprocess.CompletedProcess([], 9 if code == 'CURRENT_DYNAMIC_GUARD' and not valid else 0)
                    with mock.patch.object(runner, 'RESULTS', root), mock.patch.object(runner, 'workflow_steps', return_value=selected), mock.patch.object(runner, 'git') as git, mock.patch.object(runner, 'readback'), mock.patch.object(runner, 'run_shell', side_effect=shell), mock.patch.object(receipt, 'observe', return_value={'hit': True, 'tests_skipped': 0}), mock.patch.dict(os.environ, dict(HUNTER_VALIDATION_RECEIPTS=mode, GITHUB_OUTPUT=str(root/'out'), GITHUB_ENV=str(root/'env'))), contextlib.redirect_stdout(io.StringIO()):
                        git.return_value.stdout = 'fixture'
                        if valid: runner.run_job('research')
                        else:
                            with self.assertRaisesRegex(RuntimeError, 'JOB_STEP_FAILED'): runner.run_job('research')
                    self.assertEqual(commands, [full['run'], 'CURRENT_DYNAMIC_GUARD'] + (['PERSIST'] if valid else []))

    def test_health_publication_reports_failure_and_its_own_duration(self):
        from scripts import hunter_job_runner as runner
        import datetime as dt
        for fail in (False, True):
            output = io.StringIO()
            with mock.patch.object(runner, 'publish_health', side_effect=RuntimeError('fixture') if fail else None), contextlib.redirect_stdout(output):
                if fail:
                    with self.assertRaises(RuntimeError): runner.timed_publish_health('research', 'SUCCESS', dt.datetime.now(dt.timezone.utc), [], runner_started=time.monotonic()-1)
                else: runner.timed_publish_health('research', 'SUCCESS', dt.datetime.now(dt.timezone.utc), [], runner_started=time.monotonic()-1)
            event = json.loads(output.getvalue().split('HUNTER_HEALTH_PUBLICATION_END ')[1])
            self.assertEqual(event['publication_status'], 'FAILURE' if fail else 'SUCCESS')
            self.assertGreaterEqual(event['seconds'], 0)
            self.assertEqual(event['clock'], 'monotonic')

    def test_diagnostics_remove_email_credentials_and_environment_values(self):
        from scripts.hunter_job_runner import safe_diagnostic
        value = safe_diagnostic('contact@example.invalid token-value https://u:p@example.org?a=1', {'API_KEY':'token-value'})
        for secret in ('contact@example.invalid', 'token-value', 'u:p'): self.assertNotIn(secret, value)
