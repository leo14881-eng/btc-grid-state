"""Two independent hosts and real Git ref CAS, entirely on a local bare remote."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts import hunter_generation_admission as a


class RejectionDiagnosticsTests(unittest.TestCase):
    def test_fixed_categories_do_not_echo_raw_credentials(self):
        for message, expected in (("GH006 protected branch", "POLICY_REJECTED"),
                                  ("authentication failed secret-token", "AUTH_REJECTED"),
                                  ("non-fast-forward", "RACE"),
                                  ("timeout https://secret@example.invalid", "TRANSPORT_OR_UNKNOWN")):
            result = subprocess.CompletedProcess([], 1, stderr=message)
            self.assertEqual(a.push_rejection_category(result), expected)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "origin.git"
        self.git(self.root, "init", "--bare", str(self.remote))
        seed = self.root / "seed"
        self.git(self.root, "init", "-b", "main", str(seed))
        self.configure(seed)
        scan = seed / a.SCAN
        scan.parent.mkdir(parents=True)
        scan.write_text(json.dumps(dict(generation_id="g1", binance_complete=True,
                                        as_of_utc="2026-10-12T15:17:14Z")))
        # Complete fixture history must survive every claim and publication.
        (scan.parent / "fixture-ledger.json").write_text('{"history":["old-event"],"real":false}\n')
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-m", "fixture")
        self.git(seed, "remote", "add", "origin", str(self.remote))
        self.git(seed, "push", "origin", "main")
        self.hosts = []
        for name in ("primary", "backup"):
            path = self.root / name
            self.git(self.root, "clone", "-b", "main", str(self.remote), str(path))
            self.configure(path)
            self.hosts.append(a.ClaimStore(path))
        self.now = 10000.0

    def git(self, cwd, *args):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def configure(self, path):
        self.git(path, "config", "user.name", "test")
        self.git(path, "config", "user.email", "test@example.invalid")
        self.git(path, "config", "core.autocrlf", "false")

    def acquire(self, host=0, **kwargs):
        return a.acquire(self.hosts[host], "g1", clock=lambda: self.now, **kwargs)

    def prepare(self, host, claim, commit):
        store = self.hosts[host]
        store.git("checkout", "--detach", commit)
        # This identical complete fixture computation is used in both variants.
        raw = (store.cwd / "research/results/fixture-ledger.json").read_bytes()
        result = dict(events=json.loads(raw)["history"], decision="WAIT", real=False)
        target = store.cwd / "research/results/fixture-output.json"
        target.write_bytes((json.dumps(result, sort_keys=True) + "\n").encode())
        store.git("add", "--", "research/results/fixture-output.json")
        a.stage_publication(store, "g1", claim["owner"], clock=lambda: self.now)
        store.git("commit", "-m", "complete fixture result and publication")
        return target.read_bytes()

    def test_real_simultaneous_claims_admit_only_one_host(self):
        barrier = threading.Barrier(2)
        for store in self.hosts:
            original = store.cas
            def simultaneous(parent, claim, original=original):
                barrier.wait(timeout=20)
                return original(parent, claim)
            store.cas = simultaneous
        def contender(host):
            try:
                return self.acquire(host)[0]
            except RuntimeError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(contender, (0, 1)))
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1, results)
        self.assertIn("RESEARCH_DEFERRED_OWNER_ACTIVE", results)
        self.assertEqual(self.git(self.remote, "rev-list", "--count", "main"), "2")

    def test_crash_requires_expiry_and_force_cannot_steal_live_claim(self):
        old, _ = self.acquire()
        for force in (False, True):
            with self.assertRaisesRegex(RuntimeError, "OWNER_ACTIVE"):
                self.acquire(1, force=force)
        self.now += a.LEASE_SECONDS
        new, _ = self.acquire(1)
        self.assertNotEqual(new["owner"], old["owner"])
        with self.assertRaisesRegex(RuntimeError, "OWNERSHIP_LOST"):
            a.verify_owner(self.hosts[0], "g1", old["owner"], clock=lambda: self.now)

    def test_expiry_without_takeover_still_rejects_execution(self):
        claim, _ = self.acquire()
        self.now += a.LEASE_SECONDS
        with self.assertRaisesRegex(RuntimeError, "LEASE_EXPIRED"):
            a.verify_owner(self.hosts[0], "g1", claim["owner"], clock=lambda: self.now)

    def test_results_and_published_marker_are_one_commit_and_duplicate_is_explicit(self):
        claim, commit = self.acquire()
        expected = self.prepare(0, claim, commit)
        a.push_owned(self.hosts[0], "g1", claim["owner"], clock=lambda: self.now)
        self.assertEqual(self.acquire(1), (None, "ALREADY_PUBLISHED_NOT_A_READBACK_PROOF"))
        head, _, _, published = self.hosts[1].snapshot()
        self.assertEqual(published["state"], "PUBLISHED")
        raw = self.hosts[1].git("show", head + ":research/results/fixture-output.json").stdout.encode()
        self.assertEqual(raw, expected)
        paths = self.hosts[1].git("diff-tree", "--no-commit-id", "--name-only", "-r", head).stdout.splitlines()
        self.assertEqual(set(paths), {a.CLAIM, "research/results/fixture-output.json"})
        self.assertEqual(json.loads(self.hosts[1].git("show", head + ":research/results/fixture-ledger.json").stdout),
                         {"history": ["old-event"], "real": False})
        new, _ = self.acquire(1, force=True)
        self.assertNotEqual(new["owner"], claim["owner"])

    def test_failed_validation_leaves_claim_and_cannot_publish(self):
        claim, commit = self.acquire()
        self.hosts[0].git("checkout", "--detach", commit)
        # Validation failed; no result/PUBLISHED commit was prepared.
        with self.assertRaisesRegex(RuntimeError, "PARENT_ADVANCED"):
            a.push_owned(self.hosts[0], "g1", claim["owner"], clock=lambda: self.now)
        self.assertEqual(self.hosts[1].snapshot()[3]["state"], "RUNNING")

    def test_old_owner_prepared_results_cannot_publish_after_takeover(self):
        old, commit = self.acquire()
        self.prepare(0, old, commit)
        self.now += a.LEASE_SECONDS
        new, _ = self.acquire(1)
        with self.assertRaisesRegex(RuntimeError, "OWNERSHIP_LOST"):
            a.push_owned(self.hosts[0], "g1", old["owner"], clock=lambda: self.now)
        self.assertEqual(self.hosts[1].snapshot()[3]["owner"], new["owner"])

    def test_takeover_between_verify_and_push_is_fenced_by_real_git(self):
        old, commit = self.acquire()
        self.prepare(0, old, commit)
        original = self.hosts[0].git
        def racing(*args, **kwargs):
            if args[0] == "push":
                self.now += a.LEASE_SECONDS
                self.acquire(1)
            return original(*args, **kwargs)
        self.hosts[0].git = racing
        with self.assertRaisesRegex(RuntimeError, "OWNED_PUSH_REJECTED"):
            a.push_owned(self.hosts[0], "g1", old["owner"], clock=lambda: self.now)
        head = self.hosts[1].snapshot()[0]
        self.assertNotIn("fixture-output.json", self.hosts[1].git("ls-tree", "-r", "--name-only", head).stdout)

    def test_lost_claim_and_publication_acknowledgements_are_read_back(self):
        original = self.hosts[0].git
        def lose_ack(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == "push":
                result.returncode = 1
            return result
        self.hosts[0].git = lose_ack
        claim, commit = self.acquire()
        self.prepare(0, claim, commit)
        a.push_owned(self.hosts[0], "g1", claim["owner"], clock=lambda: self.now)
        self.assertEqual(self.acquire(1)[0], None)
        self.assertEqual(self.git(self.remote, "rev-list", "--count", "main"), "3")

    def test_scan_supersession_and_same_generation_revision_are_fenced(self):
        claim, _ = self.acquire()
        other = self.hosts[1]
        head = other.snapshot()[0]
        other.git("checkout", "--detach", head)
        path = other.cwd / a.SCAN
        scan = json.loads(path.read_text())
        scan["revision"] = 2
        path.write_text(json.dumps(scan))
        other.git("add", "--", a.SCAN)
        other.git("commit", "-m", "upstream corrected scan")
        other.git("push", "origin", "HEAD:refs/heads/main")
        with self.assertRaisesRegex(RuntimeError, "GENERATION_SUPERSEDED"):
            a.verify_owner(self.hosts[0], "g1", claim["owner"], clock=lambda: self.now)
        with self.assertRaisesRegex(RuntimeError, "GENERATION_SUPERSEDED"):
            a.acquire(other, "g2", clock=lambda: self.now)
        with self.assertRaisesRegex(RuntimeError, "OWNER_ACTIVE"):
            self.acquire(1)

    def test_malformed_claim_is_not_treated_as_free(self):
        claim, _ = self.acquire()
        for change in ({"expires": float("nan")}, {"expires": float("inf")},
                       {"owner": ""}, {"expires": claim["issued"] + 999999},
                       {"state": "FAILED"}, {"real_trading_enabled": True}):
            with self.assertRaisesRegex(RuntimeError, "CLAIM_INVALID"):
                a.decode_claim(json.dumps(dict(claim, **change)))
        with self.assertRaisesRegex(RuntimeError, "CLAIM_INVALID"):
            a.decode_claim("not-json")

    def test_unrelated_main_advance_requires_retry_from_revalidated_parent(self):
        claim, commit = self.acquire()
        self.prepare(0, claim, commit)
        other = self.hosts[1]
        other.git("checkout", "--detach", other.snapshot()[0])
        (other.cwd / "metadata.txt").write_text("metadata")
        other.git("add", "metadata.txt")
        other.git("commit", "-m", "auxiliary metadata")
        other.git("push", "origin", "HEAD:refs/heads/main")
        with self.assertRaisesRegex(RuntimeError, "PARENT_ADVANCED"):
            a.push_owned(self.hosts[0], "g1", claim["owner"], clock=lambda: self.now)
        # Caller retains the existing protected-input CAS checks before rebase.
        self.hosts[0].git("rebase", "origin/main")
        a.push_owned(self.hosts[0], "g1", claim["owner"], clock=lambda: self.now)

    def test_contender_failure_does_not_change_incumbents_protected_inputs(self):
        claim, commit = self.acquire()
        with self.assertRaisesRegex(RuntimeError, "OWNER_ACTIVE"):
            self.acquire(1)
        self.assertFalse(a.fail_owned(self.hosts[1], "g1", None, "OWNER_ACTIVE", clock=lambda: self.now))
        self.assertEqual(self.hosts[0].snapshot()[0], commit)
        a.verify_owner(self.hosts[0], "g1", claim["owner"], clock=lambda: self.now)

    def test_own_failure_is_atomic_and_allows_fresh_retry_without_losing_evidence(self):
        old, _ = self.acquire()
        self.assertTrue(a.fail_owned(self.hosts[0], "g1", old["owner"], "VALIDATION_FAILED", clock=lambda: self.now))
        failed_head, _, _, failed = self.hosts[1].snapshot()
        self.assertEqual((failed["state"], failed["failure"]), ("FAILED", "VALIDATION_FAILED"))
        new, _ = self.acquire(1)
        self.assertFalse(a.fail_owned(self.hosts[0], "g1", old["owner"], "LATE_FAILURE", clock=lambda: self.now))
        self.assertEqual(self.hosts[1].snapshot()[3]["owner"], new["owner"])
        self.assertEqual(json.loads(self.hosts[1].git("show", failed_head + ":" + a.CLAIM).stdout)["failure"],
                         "VALIDATION_FAILED")

    def test_success_health_cannot_be_rebased_over_successor(self):
        old, commit = self.acquire()
        self.prepare(0, old, commit)
        a.push_owned(self.hosts[0], "g1", old["owner"], clock=lambda: self.now)
        original = self.hosts[0].cas
        def race(parent, claim, extra=None):
            self.acquire(1, force=True)
            return original(parent, claim, extra)
        self.hosts[0].cas = race
        self.assertFalse(a.publish_owned_health(self.hosts[0], "g1", old["owner"], '{"status":"SUCCESS"}\n'))
        head = self.hosts[1].snapshot()[0]
        self.assertNotIn("hunter-runtime-research-health.json", self.hosts[1].git("ls-tree", "-r", "--name-only", head).stdout)

    def test_verified_owner_health_publishes_with_real_readback(self):
        claim, commit = self.acquire()
        self.prepare(0, claim, commit)
        a.push_owned(self.hosts[0], "g1", claim["owner"], clock=lambda: self.now)
        raw = '{"status":"SUCCESS","main_readback_verified":true}\n'
        self.assertTrue(a.publish_owned_health(self.hosts[0], "g1", claim["owner"], raw))
        head = self.hosts[1].snapshot()[0]
        self.assertEqual(self.hosts[1].git("show", head + ":research/results/hunter-runtime-research-health.json").stdout, raw)

    def test_identical_fixture_work_count_before_after(self):
        # Scheduler-only comparison; this is not a full Research/market benchmark.
        raw = (self.hosts[0].cwd / "research/results/fixture-ledger.json").read_bytes()
        def complete_fixture():
            result = dict(events=json.loads(raw)["history"], decision="WAIT", real=False)
            return (json.dumps(result, sort_keys=True) + "\n").encode()
        scan = dict(generation_id="g1", binance_complete=True)
        baseline = [complete_fixture() for _ in range(2)
                    if a.admit(scan, {}, "workflow_run", "stale", primary=True)[0]]
        started = time.monotonic()
        claim, commit = self.acquire()
        with self.assertRaisesRegex(RuntimeError, "OWNER_ACTIVE"):
            self.acquire(1)
        admission_elapsed = time.monotonic() - started
        candidate = [self.prepare(0, claim, commit)]
        self.assertEqual(len(baseline), 2)
        self.assertEqual(len(candidate), 1)
        self.assertEqual(candidate[0], baseline[0])
        print("RESEARCH_SAME_INPUT_WORK_COUNT " + json.dumps(dict(
            scope="synthetic_scheduler_fixture_not_full_research", before=2, after=1,
            input_sha256=hashlib.sha256(raw).hexdigest(), output_sha256=hashlib.sha256(candidate[0]).hexdigest(),
            candidate_two_host_admission_seconds=round(admission_elapsed, 6), production_speedup_proven=False)))

    def test_actual_cli_owner_output_and_early_check(self):
        output = self.root / "output"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                   GITHUB_OUTPUT=str(output), TRIGGER_EVENT="workflow_dispatch", FORCE="false")
        command = [sys.executable, "-B", "-X", "utf8", "-m", "scripts.hunter_generation_admission"]
        result = subprocess.run(command, cwd=self.hosts[0].cwd, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(fields["should_run"], "true")
        env.update(EXPECTED="g1", HUNTER_RESEARCH_OWNER=fields["owner"])
        check = subprocess.run(command + ["--check-owner"], cwd=self.hosts[1].cwd,
                               env=env, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        rejected = subprocess.run(command, cwd=self.hosts[1].cwd, env=env, capture_output=True, text=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("RESEARCH_DEFERRED_OWNER_ACTIVE", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
