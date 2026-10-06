import copy
import datetime as dt
import json
import pathlib
import unittest
from unittest import mock

from scripts import hunter_monitor_runtime as runtime
from tests.test_hunter_monitor_persist import GitRaceTests, fixture, GEN

NOW = dt.datetime(2026, 10, 5, 23, 0, tzinfo=dt.timezone.utc)


def documents():
    health = json.loads(fixture()['hunter-scheduler-health.json'])
    health.update(trigger_source='VULTR_SYSTEMD', monitor_completed_at_utc=(NOW-dt.timedelta(seconds=50)).isoformat())
    raw = json.dumps(health)
    proof = dict(schema='hunter_runtime_job_health_v1', job='monitor', status='SUCCESS',
                 source='VULTR_SYSTEMD', capital_authority='NONE_SHADOW_ONLY',
                 real_trading_enabled=False, main_readback_verified=True,
                 main_readback_head_sha='a'*40, monitor_generation_id=GEN,
                 scheduler_health_sha256=runtime.digest(raw), completed_at_utc=NOW.isoformat())
    return {'shadow_only': True, 'primary_jobs': {'monitor': True}}, proof, raw


class AdmissionTests(unittest.TestCase):
    def check(self, config, proof, raw, now=NOW, ancestor=True):
        return runtime.should_run(config, proof, raw, now, lambda c: ancestor)

    def test_fresh_verified_vultr_skips_all_github_trigger_types(self):
        self.assertFalse(self.check(*documents()))

    def test_unconfirmed_or_invalid_primary_runs_fallback(self):
        c, p, raw = documents()
        self.assertTrue(self.check({}, p, raw))
        self.assertTrue(self.check(dict(c, shadow_only=False), p, raw))
        self.assertTrue(self.check(c, {}, raw))
        self.assertTrue(self.check(c, p, raw, ancestor=False))
        for key, value in [('main_readback_verified', False), ('source', 'GITHUB_ACTIONS'),
                           ('status', 'FAILURE'), ('real_trading_enabled', True),
                           ('capital_authority', 'REAL'), ('monitor_generation_id', 'old'),
                           ('scheduler_health_sha256', 'wrong'), ('completed_at_utc', 'broken')]:
            with self.subTest(key=key):
                changed = dict(p, **{key: value})
                self.assertTrue(self.check(c, changed, raw))

    def test_two_missed_cycles_or_future_time_runs_fallback(self):
        c, p, raw = documents()
        self.assertTrue(self.check(c, p, raw, NOW+dt.timedelta(seconds=600)))
        self.assertTrue(self.check(c, p, raw, NOW-dt.timedelta(seconds=1)))
        h = json.loads(raw)
        h['monitor_completed_at_utc'] = (NOW-dt.timedelta(seconds=600)).isoformat()
        raw = json.dumps(h); p['scheduler_health_sha256'] = runtime.digest(raw)
        self.assertTrue(self.check(c, p, raw))

    def test_github_or_newer_scheduler_cannot_reuse_old_proof(self):
        c, p, raw = documents()
        h = json.loads(raw)
        for key, value in [('trigger_source', 'push'), ('current_generation_id', 'new'),
                           ('real_order_count', False)]:
            altered = json.dumps(dict(h, **{key: value}))
            self.assertTrue(self.check(c, p, altered))


class ProofGitTests(GitRaceTests):
    # Inherit real bare-Git race/read-back tests, then exercise proof publication.
    def setUp(self):
        super().setUp()
        self.vultr_environment = mock.patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'})
        self.vultr_environment.start()

    def tearDown(self):
        self.vultr_environment.stop()
        super().tearDown()

    def prepare_proof(self):
        raw = fixture()
        c, proof, health_raw = documents()
        raw['hunter-scheduler-health.json'] = health_raw
        for name, value in raw.items():
            pathlib.Path('research/results', name).write_text(value)
        self.run_git(self.writer, 'add', '.')
        self.run_git(self.writer, 'commit', '-m', 'Vultr snapshot')
        self.run_git(self.writer, 'push', 'origin', 'main')
        commit = self.run_git(self.writer, 'rev-parse', 'HEAD').stdout.strip()
        return raw, commit

    def test_github_cannot_advertise_primary_even_with_spoofed_trigger(self):
        raw, commit = self.prepare_proof()
        with mock.patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}):
            runtime.publish_verified(raw, GEN, commit)
        self.run_git(self.writer, 'fetch', 'origin', 'main')
        self.assertNotEqual(runtime.git('show', 'origin/main:'+runtime.PROOF_PATH, check=False).returncode, 0)

    def test_real_readback_then_proof_roundtrip(self):
        raw, commit = self.prepare_proof()
        from scripts.hunter_monitor_persist import readback
        verified = readback(raw, GEN, commit)
        runtime.publish_verified(raw, GEN, verified)
        self.run_git(self.writer, 'fetch', 'origin', 'main')
        proof = json.loads(self.run_git(self.writer, 'show', 'origin/main:'+runtime.PROOF_PATH).stdout)
        self.assertTrue(proof['main_readback_verified'])
        self.assertEqual(proof['main_readback_head_sha'], commit)
        # Publication never mutates the portfolios or scheduler generation.
        self.assertEqual(self.run_git(self.writer, 'show', 'origin/main:'+runtime.HEALTH_PATH).stdout,
                         raw['hunter-scheduler-health.json'])

    def test_old_generation_rejected_without_any_proof_write(self):
        raw, commit = self.prepare_proof()
        self.run_git(self.other, 'fetch', 'origin', 'main')
        self.run_git(self.other, 'checkout', '--detach', 'origin/main')
        path = self.other / runtime.HEALTH_PATH
        h = json.loads(path.read_text()); h['current_generation_id'] = 'new'
        path.write_text(json.dumps(h))
        self.run_git(self.other, 'add', '.'); self.run_git(self.other, 'commit', '-m', 'new generation')
        self.run_git(self.other, 'push', 'origin', 'HEAD:main')
        with self.assertRaisesRegex(RuntimeError, 'STALE_GENERATION'):
            runtime.publish_verified(raw, GEN, commit)
        self.run_git(self.writer, 'fetch', 'origin', 'main')
        result = runtime.git('show', 'origin/main:'+runtime.PROOF_PATH, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_proof_push_failure_never_writes_verified_marker(self):
        raw, commit = self.prepare_proof()
        original = runtime.subprocess.run
        def failed_push(args, **kwargs):
            if 'push' in args:
                return __import__('subprocess').CompletedProcess(args, 1, '', 'network failure')
            return original(args, **kwargs)
        with mock.patch.object(runtime.subprocess, 'run', side_effect=failed_push):
            with self.assertRaisesRegex(RuntimeError, 'RETRY_EXHAUSTED'):
                runtime.publish_verified(raw, GEN, commit)
        self.run_git(self.writer, 'fetch', 'origin', 'main')
        self.assertNotEqual(runtime.git('show', 'origin/main:'+runtime.PROOF_PATH, check=False).returncode, 0)

    def test_proof_readback_failure_is_not_success(self):
        raw, commit = self.prepare_proof()
        original = runtime.subprocess.run
        def corrupt_readback(args, **kwargs):
            if args[-2:] == ['show', 'origin/main:'+runtime.PROOF_PATH]:
                return __import__('subprocess').CompletedProcess(args, 0, '{}', '')
            return original(args, **kwargs)
        with mock.patch.object(runtime.subprocess, 'run', side_effect=corrupt_readback):
            with self.assertRaisesRegex(RuntimeError, 'READBACK_MISMATCH'):
                runtime.publish_verified(raw, GEN, commit)
