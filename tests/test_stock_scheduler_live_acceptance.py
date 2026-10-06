"""Offline acceptance-verifier regressions; no network or ledger mutations."""
import base64
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/verify_stock_scheduler_live.py"
spec = importlib.util.spec_from_file_location("stock_live_acceptance", SCRIPT)
accept = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accept)
START = "2026-10-06T17:42:00Z"
SOURCE = "a" * 40
PERSISTED = "b" * 40


def iso(minutes=0, seconds=0):
    return (dt.datetime(2026, 10, 6, 17, 43, tzinfo=dt.timezone.utc) +
            dt.timedelta(minutes=minutes, seconds=seconds)).isoformat()


def cycle(index=0, action="FRESH", **changes):
    row = {"checked_at": iso(index * 5, 2), "scheduled_at": iso(index * 5),
           "observed_at": iso(index * 5, 10), "trigger_source": "scheduled", "cron": accept.CRON,
           "action": action, "market": "OPEN", "stale": False, "monitor_age_seconds": 60,
           "http_status": 200, "scheduler_healthy": True, "simulation_only": True}
    if action in accept.DISPATCH_ACTIONS:
        row.update(dispatched=True, workflow=accept.DISPATCH_ACTIONS[action])
        if action == "DISPATCH_MONITOR":
            row["monitor_age_seconds"] = 300
        else:
            row["reason"] = "HOURLY_SCAN"
    row.update(changes)
    return row


def run(index=0, **changes):
    row = {"id": 123 + index, "head_sha": SOURCE, "head_branch": "main", "event": "workflow_dispatch",
           "path": ".github/workflows/" + accept.MONITOR, "created_at": iso(index * 5, 5),
           "status": "completed", "conclusion": "success", "run_attempt": 1}
    row.update(changes)
    return row


def inventory(*runs):
    return {"total_count": len(runs), "workflow_runs": list(runs)}


def snapshot(index=0, monitor=True, **changes):
    base = {"run_id": str(123 + index), "source_commit": SOURCE, "updated_at": iso(index * 5, 35)}
    runtime = dict(base, status="SUCCESS", simulation_only=True, real_orders=False)
    data = dict(base, status="OK", positions_before=4, positions_updated=4, errors_count=0, missing_symbols_count=0)
    names = ("monitor-run-health-v1.json", "position-monitor-v1.json") if monitor else (
        "main-run-health-v1.json", "summary-v1.json")
    row = {"main_sha": PERSISTED, "observed_at": iso(index * 5, 45), "files": {names[0]: runtime, names[1]: data}}
    row.update(changes)
    return row


def evidence(cycles=None, runs=None, snapshots=None):
    cycles = cycles if cycles is not None else [cycle(0, "DISPATCH_MONITOR"), cycle(1), cycle(2)]
    runs = runs if runs is not None else [run()]
    return {"started_at": START, "cycles": cycles, "run_correlations": accept.correlate_runs(cycles, inventory(*runs)),
            "runtime_readbacks": snapshots if snapshots is not None else [snapshot()]}


class CycleTests(unittest.TestCase):
    def test_import_does_not_read_network_environment_or_write(self):
        command = ("import importlib.util; from unittest.mock import patch; "
                   f"s=importlib.util.spec_from_file_location('check', {str(SCRIPT)!r}); "
                   "m=importlib.util.module_from_spec(s); "
                   "p=patch('urllib.request.urlopen', side_effect=AssertionError('network')); p.start(); "
                   "s.loader.exec_module(m)")
        completed = subprocess.run([sys.executable, "-B", "-c", command], env={}, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_three_errors_never_pass(self):
        ev = evidence([cycle(i, "ERROR", error="GITHUB_HTTP_401", http_status=503,
                             scheduler_healthy=False) for i in range(3)], [], [])
        verdict = accept.evaluate_evidence(ev)
        self.assertEqual(verdict["overall"], "FAIL")
        self.assertEqual(verdict["checks"]["five_minute_cadence"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["scheduler_decisions"]["status"], "FAIL")

    def test_unknown_action_fails(self):
        self.assertIn("ERROR_OR_UNKNOWN_ACTION", accept.validate_decision(cycle(action="SURPRISE")))

    def test_dispatch_consistency(self):
        variants = [cycle(action="DISPATCH_MONITOR", dispatched=False),
                    cycle(action="DISPATCH_MAIN", workflow=accept.MONITOR),
                    cycle(dispatched=True), cycle(workflow=accept.MAIN), cycle(dispatched="true"),
                    cycle(action="WAIT_ACTIVE_RUN_AFTER_RESERVATION")]
        for bad in variants:
            with self.subTest(bad=bad):
                self.assertTrue(accept.validate_decision(bad))

    def test_valid_noop_outcomes(self):
        for action in accept.NOOP_ACTIONS:
            changes = {"workflow": accept.MONITOR} if action.endswith("AFTER_RESERVATION") else {}
            if action == "MARKET_CLOSED":
                changes.update(market="CLOSED")
            self.assertEqual(accept.validate_decision(cycle(action=action, **changes)), [], action)

    def test_noop_cron_does_not_prove_dispatch_chain(self):
        verdict = accept.evaluate_evidence(evidence([cycle(i) for i in range(3)], [], []))
        self.assertEqual(verdict["checks"]["automatic_provenance"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["five_minute_cadence"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["scheduler_trigger_and_execution"]["status"], "UNVERIFIED")
        self.assertEqual(verdict["overall"], "UNVERIFIED")

    def test_missing_provenance_not_inferred_from_cadence(self):
        cycles = [cycle(i) for i in range(3)]
        for row in cycles:
            del row["trigger_source"]
        checks = accept.evaluate_cycles(cycles, START)
        self.assertEqual(checks["automatic_provenance"]["status"], "UNVERIFIED")
        self.assertEqual(checks["five_minute_cadence"]["status"], "PASS")

    def test_manual_and_diagnostic_probes_cannot_pass(self):
        for source in ("manual", "diagnostic"):
            checks = accept.evaluate_cycles([cycle(i, trigger_source=source) for i in range(3)], START)
            self.assertEqual(checks["automatic_provenance"]["status"], "FAIL")

    def test_jittered_scheduled_time_supported(self):
        rows = [cycle(i, scheduled_at=iso(i * 5, -2)) for i in range(3)]
        self.assertEqual(accept.evaluate_cycles(rows, START)["automatic_provenance"]["status"], "PASS")

    def test_jittered_event_slots_use_normalized_continuity(self):
        rows = [cycle(index, scheduled_at=iso(index * 5, jitter))
                for index, jitter in enumerate((0, -31, 0))]
        checks = accept.evaluate_cycles(rows, START)
        self.assertEqual(checks["automatic_provenance"]["status"], "PASS")
        self.assertEqual(checks["five_minute_cadence"]["status"], "PASS")

    def test_jitter_does_not_relax_actual_execution_cadence(self):
        rows = [cycle(index, scheduled_at=iso(index * 5, jitter))
                for index, jitter in enumerate((0, -31, 0))]
        rows[1]["checked_at"] = iso(5, -29)
        checks = accept.evaluate_cycles(rows, START)
        self.assertEqual(checks["automatic_provenance"]["status"], "PASS")
        self.assertEqual(checks["five_minute_cadence"]["status"], "FAIL")

    def test_bad_cron_provenance_and_duplicate_scheduled_event(self):
        for changes in ({"cron": "* * * * *"}, {"scheduled_at": iso(seconds=121)},
                        {"scheduled_at": iso(minutes=-5)}, {"scheduled_at": "invalid"}):
            rows = [cycle(i, **changes) for i in range(3)]
            self.assertNotEqual(accept.evaluate_cycles(rows, START)["automatic_provenance"]["status"], "PASS")

    def test_skipped_or_repeated_cycle_fails_cadence(self):
        for rows in ([cycle(0), cycle(1), cycle(3)], [cycle(0), cycle(0), cycle(1)]):
            self.assertEqual(accept.evaluate_cycles(rows, START)["five_minute_cadence"]["status"], "FAIL")

    def test_baseline_does_not_count(self):
        self.assertEqual(accept.evaluate_cycles([cycle(-1), cycle(0), cycle(1)], START)
                         ["five_minute_cadence"]["status"], "FAIL")

    def test_stale_recovery_decision_is_valid(self):
        self.assertEqual(accept.validate_decision(cycle(action="DISPATCH_MONITOR", stale=True,
            monitor_age_seconds=None, scheduler_healthy=False, http_status=503)), [])

    def test_inconsistent_health_or_fake_fresh_fails(self):
        for changes in ({"http_status": 503}, {"scheduler_healthy": False}, {"market": "CLOSED"},
                        {"monitor_age_seconds": None}, {"monitor_age_seconds": 300},
                        {"observed_at": iso(minutes=20)}, {"simulation_only": False},
                        {"checked_at": "2026-10-06T17:43:00"}, {"error": "REQUEST_FAILED"}):
            self.assertTrue(accept.validate_decision(cycle(**changes)), changes)


class CorrelationTests(unittest.TestCase):
    def test_unique_successful_completed_run(self):
        rows = accept.correlate_runs([cycle(action="DISPATCH_MONITOR")], inventory(run()))
        self.assertEqual(accept.execution_verdict(rows)["status"], "PASS")
        self.assertEqual(rows[0]["correlation_method"], "UNIQUE_WORKFLOW_AND_CREATION_WINDOW")

    def test_failed_cancelled_timed_out_not_success(self):
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", None):
            rows = accept.correlate_runs([cycle(action="DISPATCH_MONITOR")], inventory(run(conclusion=conclusion)))
            self.assertEqual(accept.execution_verdict(rows)["status"], "FAIL")

    def test_in_progress_is_unverified(self):
        rows = accept.correlate_runs([cycle(action="DISPATCH_MONITOR")], inventory(run(status="in_progress")))
        self.assertEqual(accept.execution_verdict(rows)["status"], "UNVERIFIED")

    def test_unique_correlation_required(self):
        rows = accept.correlate_runs([cycle(action="DISPATCH_MONITOR")], inventory(run(), run(id=999)))
        self.assertEqual(accept.execution_verdict(rows)["status"], "FAIL")

    def test_wrong_branch_event_workflow_or_time_do_not_match(self):
        for changes in ({"head_branch": "other"}, {"event": "schedule"},
                        {"path": ".github/workflows/hunter.yml"}, {"created_at": iso(seconds=100)}):
            rows = accept.correlate_runs([cycle(action="DISPATCH_MONITOR")], inventory(run(**changes)))
            self.assertEqual(rows[0]["matching_run_count"], 0)
            self.assertEqual(accept.execution_verdict(rows)["status"], "UNVERIFIED")

    def test_reused_run_not_allowed(self):
        row = accept.correlate_runs([cycle(action="DISPATCH_MONITOR")], inventory(run()))[0]
        self.assertEqual(accept.execution_verdict([row, row])["status"], "FAIL")

    def test_rerun_or_missing_attempt_is_unverified(self):
        for attempt in (2, None, True, "secret-value"):
            rows = accept.correlate_runs([cycle(action="DISPATCH_MONITOR")], inventory(run(run_attempt=attempt)))
            self.assertEqual(accept.execution_verdict(rows)["status"], "UNVERIFIED")
            self.assertNotIn("secret-value", json.dumps(rows))

    def test_truncated_or_malformed_inventory_fails_closed(self):
        for data in ({"total_count": 101, "workflow_runs": [run()]}, {"workflow_runs": []},
                     {"total_count": 0, "workflow_runs": [run()]}, inventory(run(created_at="bad")),
                     inventory(run(head_sha="not-sha"))):
            with self.assertRaises(RuntimeError):
                accept.correlate_runs([cycle(action="DISPATCH_MONITOR")], data)


class PersistenceTests(unittest.TestCase):
    def test_full_monitor_chain_passes(self):
        ev = evidence()
        self.assertEqual(accept.evaluate_evidence(ev)["overall"], "PASS")
        self.assertEqual(ev["runtime_correlations"][0]["readback_main_sha"], PERSISTED)

    def test_successful_run_without_runtime_is_unverified(self):
        verdict = accept.evaluate_evidence(evidence(snapshots=[]))
        self.assertEqual(verdict["checks"]["scheduler_trigger_and_execution"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["persisted_runtime"]["status"], "UNVERIFIED")
        self.assertEqual(verdict["overall"], "UNVERIFIED")

    def test_matching_run_and_source_required_for_both_files(self):
        for field, value in (("run_id", "999"), ("source_commit", "c" * 40), ("updated_at", START)):
            snap = snapshot()
            snap["files"]["position-monitor-v1.json"][field] = value
            verdict = accept.evaluate_evidence(evidence(snapshots=[snap]))
            self.assertEqual(verdict["checks"]["persisted_runtime"]["status"], "UNVERIFIED")

    def test_checkout_head_race_is_unverified_not_failed(self):
        snap = snapshot()
        for doc in snap["files"].values():
            doc["source_commit"] = "c" * 40
        ev = evidence(snapshots=[snap])
        self.assertEqual(accept.evaluate_evidence(ev)["overall"], "UNVERIFIED")
        self.assertEqual(ev["runtime_correlations"][0]["binding"]["reasons"], ["RUNTIME_CHECKOUT_COMMIT_NOT_ATTESTED"])

    def test_same_main_snapshot_required(self):
        first, second = snapshot(), snapshot(main_sha="c" * 40)
        del first["files"]["position-monitor-v1.json"]
        del second["files"]["monitor-run-health-v1.json"]
        self.assertEqual(accept.evaluate_evidence(evidence(snapshots=[first, second]))["overall"], "UNVERIFIED")

    def test_local_unbound_snapshot_not_proof(self):
        self.assertEqual(accept.evaluate_evidence(evidence(snapshots=[snapshot(main_sha=None)]))["overall"], "UNVERIFIED")

    def test_previous_valid_readback_survives_subsequent_overwrite(self):
        self.assertEqual(accept.evaluate_evidence(evidence(snapshots=[snapshot(), snapshot(1)]))["overall"], "PASS")

    def test_each_dispatched_run_requires_readback(self):
        rows = [cycle(i, "DISPATCH_MONITOR") for i in range(3)]
        ev = evidence(rows, [run(i) for i in range(3)], [snapshot(0), snapshot(2)])
        self.assertEqual(accept.evaluate_evidence(ev)["checks"]["persisted_runtime"]["status"], "UNVERIFIED")

    def test_partial_refresh_does_not_falsely_fail_scheduler_execution(self):
        snap = snapshot()
        snap["files"]["position-monitor-v1.json"].update(status="PARTIAL", positions_updated=3, missing_symbols_count=1)
        verdict = accept.evaluate_evidence(evidence(snapshots=[snap]))
        self.assertEqual(verdict["checks"]["scheduler_trigger_and_execution"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["persisted_runtime"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["full_position_refresh"]["status"], "DEGRADED")
        self.assertEqual(verdict["overall"], "FAIL")

    def test_refresh_false_green_fields(self):
        for changes in ({"errors_count": 1}, {"missing_symbols_count": 1}, {"status": "UNKNOWN"},
                        {"positions_before": -1}, {"positions_before": None}, {"positions_updated": 3}):
            snap = snapshot()
            snap["files"]["position-monitor-v1.json"].update(changes)
            self.assertEqual(accept.evaluate_evidence(evidence(snapshots=[snap]))["checks"]
                             ["full_position_refresh"]["status"], "DEGRADED")

    def test_runtime_execution_failed(self):
        snap = snapshot()
        snap["files"]["monitor-run-health-v1.json"]["status"] = "FAILED"
        self.assertEqual(accept.evaluate_evidence(evidence(snapshots=[snap]))["checks"]
                         ["persisted_runtime"]["status"], "FAIL")

    def test_main_requires_main_health_and_summary_with_same_run(self):
        rows = [cycle(0, "DISPATCH_MAIN"), cycle(1), cycle(2)]
        ev = evidence(rows, [run(path=".github/workflows/" + accept.MAIN)], [snapshot(monitor=False)])
        verdict = accept.evaluate_evidence(ev)
        self.assertEqual(verdict["overall"], "UNVERIFIED")
        self.assertEqual(verdict["checks"]["scheduler_trigger_and_execution"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["persisted_runtime"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["full_position_refresh"]["status"], "NOT_EXERCISED")
        ev["runtime_readbacks"][0]["files"]["summary-v1.json"]["run_id"] = "old"
        self.assertEqual(accept.evaluate_evidence(ev)["overall"], "UNVERIFIED")


    def test_market_closed_main_run_does_not_replace_full_refresh_evidence(self):
        rows = [cycle(0, "DISPATCH_MAIN", market="CLOSED"),
                cycle(1, "MARKET_CLOSED", market="CLOSED"),
                cycle(2, "MARKET_CLOSED", market="CLOSED")]
        ev = evidence(rows, [run(path=".github/workflows/" + accept.MAIN)], [snapshot(monitor=False)])
        verdict = accept.evaluate_evidence(ev)
        self.assertEqual(verdict["checks"]["scheduler_decisions"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["scheduler_trigger_and_execution"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["persisted_runtime"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["full_position_refresh"]["status"], "NOT_EXERCISED")
        self.assertEqual(verdict["overall"], "UNVERIFIED")


class SafetyAndCollectionTests(unittest.TestCase):
    def test_health_and_runtime_evidence_do_not_copy_secrets_or_error_bodies(self):
        secret = "Bearer secret-token / private response body"
        health = cycle(error=secret, reason=secret, token=secret)
        health["arbitrary_payload"] = secret
        runtime = {"error": secret, "errors": [secret], "missing_symbols": [secret], "source_commit": secret,
                   "run_id": secret, "status": secret, "token": secret}
        text = json.dumps([accept.safe_health(health, 503, iso()), accept.safe_runtime(runtime)])
        self.assertNotIn(secret, text)
        self.assertIn('"errors_count": 1', text)

    def test_reader_binds_every_file_to_one_main_commit(self):
        reader = object.__new__(accept.LiveReader)
        calls = []
        data = {"run_id": "123", "source_commit": SOURCE, "updated_at": iso(seconds=35)}
        def gh(path):
            calls.append(path)
            if path == "commits/main":
                return {"sha": PERSISTED}
            return {"encoding": "base64", "content": base64.b64encode(json.dumps(data).encode()).decode()}
        reader.gh = gh
        snap = reader.readback(iso(seconds=45))
        self.assertEqual(snap["main_sha"], PERSISTED)
        self.assertEqual(len(snap["files"]), 4)
        self.assertTrue(all(path.endswith("?ref=" + PERSISTED) for path in calls[1:]))

    def test_config_requires_stock_binding_secret_name_and_exact_cron(self):
        reader = object.__new__(accept.LiveReader)
        settings = {"bindings": [
            {"name": "STOCK_SCHEDULER", "type": "durable_object_namespace", "class_name": "StockScheduler"},
            {"name": "GITHUB_ACTIONS_TOKEN", "type": "secret_text", "text": "never-output-this"}]}
        responses = [settings, [{"cron": accept.CRON}], {"subdomain": "example"}, {"deployments": []}]
        with patch.object(reader, "cf", side_effect=responses):
            ev = {}
            self.assertEqual(reader.configure(ev), "https://stock-shadow-scheduler.example.workers.dev/health")
            self.assertNotIn("never-output-this", json.dumps(ev))
        with patch.object(reader, "cf", side_effect=[settings, [{"cron": "* * * * *"}]]):
            with self.assertRaisesRegex(RuntimeError, "STOCK_CRON_MISMATCH"):
                reader.configure({})

    def test_requests_are_get_only(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{}'
        with patch.object(accept.urllib.request, "urlopen", return_value=Response()) as urlopen:
            accept.request("https://example.invalid/health", "secret")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)

    def test_request_exceptions_do_not_leak_secret(self):
        with patch.object(accept.urllib.request, "urlopen", side_effect=RuntimeError("secret-token")):
            with self.assertRaisesRegex(RuntimeError, "^READ_REQUEST_FAILED$"):
                accept.request("https://example.invalid/health", "secret-token")

    def test_inflight_prestart_scheduled_event_is_baseline(self):
        class Clock:
            ticks = 0
            def monotonic(self):
                return self.ticks
            def sleep(self, seconds):
                self.ticks += seconds
            def now(self):
                return iso(seconds=self.ticks + 15)
        clock = Clock()
        # Observation begins between scheduled delivery and DO execution.
        # Cycle 0 has a new checked_at but its scheduled event predates start.
        rows = [cycle(0), cycle(1), cycle(2), cycle(3)]
        class Reader:
            index = 0
            def configure(self, ev):
                return "https://example.invalid/health"
            def health(self, url):
                row = rows[min(self.index, 3)]
                clock.ticks = self.index * 300
                self.index += 1
                return 200, row
            def readback(self, observed):
                return snapshot(observed_at=observed)
            def runs(self, started_at):
                return inventory()
        ev = {"started_at": iso(seconds=1), "cycles": [], "run_correlations": [], "runtime_readbacks": []}
        code = accept.collect(ev, Reader(), lambda: None, lambda line: None,
                              clock.now, clock.monotonic, clock.sleep, observation_seconds=1200)
        self.assertEqual(code, 1)  # No dispatch was exercised.
        self.assertEqual([row["checked_at"] for row in ev["cycles"]],
                         [cycle(index)["checked_at"] for index in (1, 2, 3)])
        self.assertEqual(ev["verdict"]["checks"]["automatic_provenance"]["status"], "PASS")
        self.assertEqual(ev["verdict"]["checks"]["five_minute_cadence"]["status"], "PASS")

    def test_fake_observer_excludes_cached_baseline_and_returns_nonzero_for_no_dispatch(self):
        class Clock:
            ticks = 0
            def monotonic(self):
                return self.ticks
            def sleep(self, seconds):
                self.ticks += seconds
            def now(self):
                return iso(seconds=self.ticks + 15)
        clock = Clock()
        rows = [cycle(-1), cycle(0), cycle(1), cycle(2)]
        class Reader:
            index = 0
            def configure(self, ev):
                return "https://example.invalid/health"
            def health(self, url):
                row = rows[min(self.index, 3)]
                self.index += 1
                clock.ticks = max(0, (self.index - 2) * 300)
                return 200, row
            def readback(self, observed):
                return snapshot(observed_at=observed)
            def runs(self, started_at):
                return inventory()
        ev = {"started_at": START, "cycles": [], "run_correlations": [], "runtime_readbacks": []}
        saved, emitted = [], []
        code = accept.collect(ev, Reader(), lambda: saved.append(copy.deepcopy(ev)), emitted.append,
                              clock.now, clock.monotonic, clock.sleep, observation_seconds=1000)
        self.assertEqual(code, 1)
        self.assertEqual(len(ev["cycles"]), 3)
        self.assertEqual(ev["cycles"][0]["checked_at"], cycle(0)["checked_at"])
        self.assertEqual(ev["verdict"]["overall"], "UNVERIFIED")
        self.assertTrue(saved)
        self.assertTrue(any(line.startswith("STOCK_LIVE_VERDICT=") for line in emitted))


if __name__ == "__main__":
    unittest.main()
