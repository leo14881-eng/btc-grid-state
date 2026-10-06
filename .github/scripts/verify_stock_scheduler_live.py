"""Read-only live acceptance. Importing this module performs no I/O.

A timestamp or a completed workflow alone is not an acceptance result. Keep cron,
execution, persistence, and quote completeness separate, and exit 0 only when all
required evidence is present. This program never dispatches or writes the ledger.
"""
import base64
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "leo14881-eng/btc-grid-state"
WORKER = "stock-shadow-scheduler"
CRON = "3,8,13,18,23,28,33,38,43,48,53,58 * * * *"
MAIN = "stock-shadow.yml"
MONITOR = "stock-shadow-position-monitor.yml"
DISPATCH_ACTIONS = {"DISPATCH_MAIN": MAIN, "DISPATCH_MONITOR": MONITOR}
NOOP_ACTIONS = {"WAIT_ACTIVE_RUN", "WAIT_ACTIVE_RUN_AFTER_RESERVATION",
                "DISPATCH_COOLDOWN", "MARKET_CLOSED", "FRESH"}
OUT = Path("/tmp/stock-scheduler-live-evidence.json")
CF = "https://api.cloudflare.com/client/v4/accounts/"
GH = f"https://api.github.com/repos/{REPO}/"
RUNTIME_FILES = ("position-monitor-v1.json", "monitor-run-health-v1.json",
                 "main-run-health-v1.json", "summary-v1.json")
SHA = re.compile(r"[a-f0-9]{40}")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("INVALID_TIMESTAMP")
    value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("INVALID_TIMESTAMP")
    return value.timestamp()


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def request(url, token=None, allow_503=False):
    headers = {"User-Agent": "stock-scheduler-read-only-acceptance"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        # GET is the only method used, including for the public health endpoint.
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        if allow_503 and exc.code == 503:
            try:
                return exc.code, json.load(exc)
            except Exception:
                pass
        raise RuntimeError("READ_HTTP_" + str(exc.code)) from None
    except Exception:
        raise RuntimeError("READ_REQUEST_FAILED") from None


def result(status, *reasons):
    return {"status": status, "reasons": list(dict.fromkeys(reasons))}


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,100}", value) else "REDACTED_OR_INVALID"


def safe_timestamp(value):
    try:
        timestamp(value)
        return value
    except (ValueError, TypeError, OverflowError):
        return None


def safe_health(health, status, observed_at):
    """Do not copy response bodies, arbitrary errors, or credentials into evidence."""
    row = {key: safe_code(health[key]) for key in
           ("action", "reason", "market", "error") if key in health}
    for key in ("checked_at", "scheduled_at"):
        row[key] = safe_timestamp(health.get(key))
    row["trigger_source"] = health.get("trigger_source") if health.get("trigger_source") in {
        "scheduled", "manual", "diagnostic"} else None
    row["cron"] = health.get("cron") if health.get("cron") == CRON else None
    row["workflow"] = health.get("workflow") if health.get("workflow") in {MAIN, MONITOR, None} else "INVALID"
    for key in ("dispatched", "stale", "scheduler_healthy", "simulation_only"):
        if key in health:
            row[key] = health[key] if type(health[key]) is bool else "INVALID"
    age = health.get("monitor_age_seconds")
    row["monitor_age_seconds"] = age if type(age) in {int, float} and 0 <= age < float("inf") else None
    row.update(http_status=status, observed_at=observed_at)
    return row


def safe_runtime(data):
    row = {}
    for key in ("run_id", "portfolio_run_id"):
        value = data.get(key)
        if isinstance(value, (int, str)) and re.fullmatch(r"[0-9]+", str(value)):
            row[key] = str(value)
    for key in ("source_commit", "portfolio_source_commit"):
        value = data.get(key)
        if isinstance(value, str) and SHA.fullmatch(value):
            row[key] = value
    for key in ("updated_at", "started_at", "portfolio_updated_at"):
        if key in data:
            row[key] = safe_timestamp(data[key])
    if "status" in data:
        row["status"] = safe_code(data["status"])
    for key in ("positions_before", "positions_updated"):
        if type(data.get(key)) is int:
            row[key] = data[key]
    for key in ("errors", "missing_symbols"):
        row[key + "_count"] = len(data[key]) if isinstance(data.get(key), list) else None
    for key in ("scan_state_stale", "simulation_only", "real_orders"):
        if type(data.get(key)) is bool:
            row[key] = data[key]
    return row


def validate_decision(cycle):
    errors = []
    action = cycle.get("action")
    workflow = cycle.get("workflow")
    dispatched = cycle.get("dispatched", False)
    if action not in DISPATCH_ACTIONS and action not in NOOP_ACTIONS:
        errors.append("ERROR_OR_UNKNOWN_ACTION")
    if "error" in cycle:
        errors.append("SCHEDULER_ERROR_PRESENT")
    if type(dispatched) is not bool:
        errors.append("INVALID_DISPATCH_FLAG")
    if action in DISPATCH_ACTIONS:
        if dispatched is not True or workflow != DISPATCH_ACTIONS[action]:
            errors.append("DISPATCH_ACTION_WORKFLOW_MISMATCH")
    elif dispatched is not False:
        errors.append("UNEXPECTED_DISPATCH")
    if action == "WAIT_ACTIVE_RUN_AFTER_RESERVATION":
        if workflow not in {MAIN, MONITOR}:
            errors.append("RESERVED_WORKFLOW_MISSING")
    elif action in NOOP_ACTIONS and workflow is not None:
        errors.append("UNEXPECTED_WORKFLOW")
    market, stale = cycle.get("market"), cycle.get("stale")
    if market not in {"OPEN", "CLOSED", "UNKNOWN"} or type(stale) is not bool:
        errors.append("INVALID_MARKET_HEALTH")
    if stale is True and market != "OPEN":
        errors.append("STALE_MARKET_MISMATCH")
    if action == "MARKET_CLOSED" and (market != "CLOSED" or stale is not False):
        errors.append("CLOSED_DECISION_MISMATCH")
    monitor_age = cycle.get("monitor_age_seconds")
    valid_age = type(monitor_age) in {int, float} and 0 <= monitor_age < float("inf")
    if market == "OPEN" and ((stale is True and valid_age and monitor_age < 600) or
                             (stale is False and (not valid_age or monitor_age > 600))):
        errors.append("MONITOR_STALENESS_MISMATCH")
    if action == "FRESH" and (market == "CLOSED" or stale is not False or not valid_age or monitor_age > 240):
        errors.append("FRESH_DECISION_MISMATCH")
    if action == "DISPATCH_MONITOR" and (market == "CLOSED" or (valid_age and monitor_age < 240)):
        errors.append("MONITOR_DISPATCH_DECISION_MISMATCH")
    if action == "DISPATCH_MAIN" and cycle.get("reason") != (
            "BOUNDED_MONITOR_RECOVERY" if stale is True else "HOURLY_SCAN"):
        errors.append("MAIN_DISPATCH_REASON_MISMATCH")
    # A stale recovery decision is valid but is not a healthy/full refresh.
    expected_status = 503 if stale is True else 200
    if cycle.get("http_status") != expected_status or cycle.get("scheduler_healthy") is not (stale is False):
        errors.append("HTTP_HEALTH_MISMATCH")
    if cycle.get("simulation_only") is not True:
        errors.append("SIMULATION_FLAG_MISSING")
    try:
        if not -5 <= timestamp(cycle["observed_at"]) - timestamp(cycle["checked_at"]) <= 600:
            errors.append("STALE_OR_FUTURE_OBSERVATION")
    except (KeyError, TypeError, ValueError, OverflowError):
        errors.append("INVALID_CYCLE_TIMESTAMP")
    return errors


def evaluate_cycles(cycles, started_at):
    decision_errors = [error for cycle in cycles for error in validate_decision(cycle)]
    decisions = result("FAIL", *decision_errors) if decision_errors else result("PASS" if len(cycles) >= 3 else "UNVERIFIED")
    try:
        times = [timestamp(cycle["checked_at"]) for cycle in cycles]
        cadence_ok = (len(times) >= 3 and all(t >= timestamp(started_at) for t in times)
                      and all(270 <= b - a <= 330 for a, b in zip(times, times[1:])))
    except (KeyError, TypeError, ValueError, OverflowError):
        cadence_ok = False
    cadence = result("PASS" if cadence_ok else "UNVERIFIED" if len(cycles) < 3 else "FAIL",
                     *([] if cadence_ok else ["THREE_NEW_CONSECUTIVE_PERIODS_NOT_PROVEN"]))
    missing, invalid, scheduled_slots = False, False, []
    for cycle in cycles:
        if cycle.get("trigger_source") is None or cycle.get("scheduled_at") is None or cycle.get("cron") is None:
            missing = True
            continue
        if cycle["trigger_source"] != "scheduled" or cycle["cron"] != CRON:
            invalid = True
            continue
        try:
            scheduled = timestamp(cycle["scheduled_at"])
            checked = timestamp(cycle["checked_at"])
            # Cloudflare can report jittered scheduledTime, including seconds near
            # the preceding minute. Do not demand exact second zero.
            slot_offset = (scheduled - 180 + 150) % 300 - 150
            slot_distance = abs(slot_offset)
            if not (-5 <= checked - scheduled <= 120 and slot_distance <= 60):
                invalid = True
            if scheduled < timestamp(started_at):
                invalid = True
            scheduled_slots.append(scheduled - slot_offset)
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid = True
    # Provenance uses consecutive intended cron slots; provider timestamp jitter
    # must not contradict the per-event ±60-second tolerance above. Actual DO
    # execution cadence remains independently bounded to 270..330 seconds.
    if len(scheduled_slots) > 1 and not all(b - a == 300 for a, b in zip(scheduled_slots, scheduled_slots[1:])):
        invalid = True
    provenance = result("FAIL", "MANUAL_OR_INVALID_SCHEDULED_EVENT") if invalid else (
        result("UNVERIFIED", "WORKER_SCHEDULED_EVENT_PROVENANCE_MISSING") if missing or len(cycles) < 3
        else result("PASS"))
    return {"scheduler_decisions": decisions, "five_minute_cadence": cadence,
            "automatic_provenance": provenance}


def correlate_runs(cycles, inventory):
    runs = inventory.get("workflow_runs")
    count = inventory.get("total_count")
    if not isinstance(runs, list) or type(count) is not int or count < len(runs):
        raise RuntimeError("RUN_INVENTORY_INVALID")
    if count > len(runs):
        raise RuntimeError("RUN_INVENTORY_TRUNCATED")
    correlations = []
    for cycle in cycles:
        if cycle.get("dispatched") is not True:
            continue
        start = timestamp(cycle["checked_at"])
        matches = []
        for run in runs:
            if (run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main"
                    or run.get("path") != ".github/workflows/" + str(cycle.get("workflow"))):
                continue
            try:
                delta = timestamp(run.get("created_at")) - start
            except (ValueError, TypeError, OverflowError):
                raise RuntimeError("RUN_INVENTORY_INVALID") from None
            if -5 <= delta <= 90:
                matches.append(run)
        row = {"checked_at": cycle["checked_at"], "workflow": cycle["workflow"],
               "correlation_method": "UNIQUE_WORKFLOW_AND_CREATION_WINDOW",
               "matching_run_count": len(matches)}
        if len(matches) == 1:
            run = matches[0]
            if not re.fullmatch(r"[0-9]+", str(run.get("id"))) or not SHA.fullmatch(str(run.get("head_sha"))):
                raise RuntimeError("RUN_INVENTORY_INVALID")
            row.update(id=str(run["id"]), head_sha=run["head_sha"], created_at=run["created_at"],
                       status=safe_code(str(run.get("status", "")).upper()),
                       conclusion=safe_code(str(run.get("conclusion", "")).upper()),
                       run_attempt=run.get("run_attempt") if type(run.get("run_attempt")) is int else None, event="workflow_dispatch")
        correlations.append(row)
    return correlations


def execution_verdict(correlations):
    if not correlations:
        return result("UNVERIFIED", "NO_DISPATCH_OBSERVED")
    ids = [row.get("id") for row in correlations if row.get("id") is not None]
    if len(set(ids)) != len(ids):
        return result("FAIL", "RUN_REUSED_ACROSS_CYCLES")
    if any(row.get("matching_run_count", 0) > 1 for row in correlations):
        return result("FAIL", "AMBIGUOUS_WORKFLOW_CORRELATION")
    if any(row.get("matching_run_count") != 1 for row in correlations):
        return result("UNVERIFIED", "DISPATCH_RUN_NOT_FOUND")
    if any(row.get("run_attempt") != 1 for row in correlations):
        return result("UNVERIFIED", "FIRST_RUN_ATTEMPT_NOT_PROVEN")
    if any(row.get("status") == "COMPLETED" and row.get("conclusion") != "SUCCESS" for row in correlations):
        return result("FAIL", "WORKFLOW_DID_NOT_SUCCEED")
    if any(row.get("status") != "COMPLETED" for row in correlations):
        return result("UNVERIFIED", "WORKFLOW_NOT_COMPLETED")
    return result("PASS")


def runtime_verdict(correlation, snapshots):
    """Read back both outputs from one immutable main snapshot, never working files."""
    if correlation.get("matching_run_count") != 1:
        return {"binding": result("UNVERIFIED", "NO_UNIQUE_RUN"), "refresh": result("UNVERIFIED")}
    monitor = correlation["workflow"] == MONITOR
    names = ("monitor-run-health-v1.json", "position-monitor-v1.json") if monitor else (
        "main-run-health-v1.json", "summary-v1.json")
    mismatch = False
    for snapshot in reversed(snapshots):
        if not SHA.fullmatch(str(snapshot.get("main_sha"))):
            continue
        docs = [snapshot.get("files", {}).get(name, {}) for name in names]
        if not all(doc.get("run_id") == correlation["id"] for doc in docs):
            continue
        if not all(doc.get("source_commit") == correlation["head_sha"] for doc in docs):
            mismatch = True
            continue
        try:
            if not all(timestamp(correlation["created_at"]) - 5 <= timestamp(doc["updated_at"])
                       <= timestamp(snapshot["observed_at"]) + 5 for doc in docs):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        binding = result("PASS") if docs[0].get("status") == "SUCCESS" else result("FAIL", "RUNTIME_EXECUTION_UNSUCCESSFUL")
        refresh = result("NOT_EXERCISED")
        if monitor:
            doc = docs[1]
            complete = (doc.get("status") == "OK" and type(doc.get("positions_before")) is int
                        and doc["positions_before"] >= 0 and doc.get("positions_updated") == doc["positions_before"]
                        and doc.get("errors_count") == 0 and doc.get("missing_symbols_count") == 0)
            refresh = result("PASS") if complete else result("DEGRADED", "POSITION_REFRESH_INCOMPLETE")
        return {"binding": binding, "refresh": refresh, "readback_main_sha": snapshot["main_sha"],
                "run_id": correlation["id"], "runtime_source_commit": correlation["head_sha"], "files": list(names)}
    return {"binding": result("UNVERIFIED", "RUNTIME_CHECKOUT_COMMIT_NOT_ATTESTED" if mismatch else
                              "MATCHING_RUNTIME_NOT_READ_BACK_FROM_MAIN"), "refresh": result("UNVERIFIED")}


def evaluate_evidence(evidence):
    checks = evaluate_cycles(evidence["cycles"], evidence["started_at"])
    correlations = evidence.get("run_correlations", [])
    execution = execution_verdict(correlations)
    if sum(c.get("dispatched") is True for c in evidence["cycles"]) != len(correlations):
        execution = result("UNVERIFIED", "DISPATCH_CORRELATIONS_INCOMPLETE")
    checks["workflow_execution"] = execution
    trigger_checks = [*checks.values()]
    checks["scheduler_trigger_and_execution"] = result(
        "FAIL" if any(c["status"] == "FAIL" for c in trigger_checks) else
        "PASS" if all(c["status"] == "PASS" for c in trigger_checks) else "UNVERIFIED")
    runtime = [runtime_verdict(c, evidence.get("runtime_readbacks", [])) for c in correlations]
    evidence["runtime_correlations"] = runtime
    bindings = [row["binding"]["status"] for row in runtime]
    checks["persisted_runtime"] = result("FAIL" if "FAIL" in bindings else "PASS" if bindings and
                                         all(s == "PASS" for s in bindings) else "UNVERIFIED")
    refreshes = [row["refresh"]["status"] for row in runtime if row["refresh"]["status"] != "NOT_EXERCISED"]
    checks["full_position_refresh"] = result("DEGRADED" if "DEGRADED" in refreshes else
        "PASS" if refreshes and all(s == "PASS" for s in refreshes) else "UNVERIFIED" if refreshes else "NOT_EXERCISED")
    statuses = [c["status"] for c in checks.values()]
    # Main-only dispatch verifies its own runtime chain, but complete acceptance
    # also requires an observed full-holdings monitor refresh. Expected market-
    # closed/no-op behavior does not substitute for that missing evidence.
    overall = "FAIL" if "FAIL" in statuses or "DEGRADED" in statuses else (
        "PASS" if all(s == "PASS" for s in statuses) else "UNVERIFIED")
    return {"overall": overall, "checks": checks,
            "limitations": ["Run attribution uses a unique workflow/time window, not a dispatch ID.",
                            "Scheduled provenance is Worker-reported; cadence alone is not provenance.",
                            "A checkout-main/head-SHA mismatch needs runtime checkout logs or artifacts."]}


class LiveReader:
    def __init__(self):
        self.account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
        if not re.fullmatch(r"[a-fA-F0-9]{32}", self.account):
            raise RuntimeError("INVALID_ACCOUNT_ID")

    def cf(self, path):
        _, data = request(CF + self.account + "/" + path, os.environ["CLOUDFLARE_API_TOKEN"])
        if not data.get("success"):
            raise RuntimeError("CF_READ_FAILED")
        return data["result"]

    def gh(self, path):
        return request(GH + path, os.environ["STOCK_AUDIT_GITHUB_TOKEN"])[1]

    def configure(self, evidence):
        settings = self.cf(f"workers/scripts/{WORKER}/settings")
        bindings = settings.get("bindings", [])
        durable = [b for b in bindings if b.get("name") == "STOCK_SCHEDULER" and
                   b.get("type") == "durable_object_namespace" and b.get("class_name") == "StockScheduler"]
        secret = any(b.get("name") == "GITHUB_ACTIONS_TOKEN" and b.get("type") == "secret_text" for b in bindings)
        if len(durable) != 1 or not secret:
            raise RuntimeError("STOCK_BINDING_OR_SECRET_MISSING")
        schedules = self.cf(f"workers/scripts/{WORKER}/schedules")
        schedules = schedules.get("schedules", []) if isinstance(schedules, dict) else schedules
        if [entry.get("cron") for entry in schedules] != [CRON]:
            raise RuntimeError("STOCK_CRON_MISMATCH")
        subdomain = self.cf("workers/subdomain").get("subdomain", "")
        if not re.fullmatch(r"[a-zA-Z0-9-]+", subdomain):
            raise RuntimeError("INVALID_WORKERS_SUBDOMAIN")
        evidence.update(health_url=f"https://{WORKER}.{subdomain}.workers.dev/health",
                        remote_binding_check="PASS", remote_secret_name_check="PASS", remote_cron_check="PASS")
        deployments = self.cf(f"workers/scripts/{WORKER}/deployments")
        # Keep deployment/version identifiers for audit, never arbitrary metadata.
        evidence["deployments"] = []
        for deployment in deployments.get("deployments", []):
            identifier = deployment.get("id")
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9-]{36}", identifier):
                continue
            versions = [v["version_id"] for v in deployment.get("versions", [])
                        if isinstance(v.get("version_id"), str) and re.fullmatch(r"[a-f0-9-]{36}", v["version_id"])]
            evidence["deployments"].append({"id": identifier, "created_on": safe_timestamp(deployment.get("created_on")),
                                            "version_ids": versions})
        return evidence["health_url"]

    def health(self, url):
        return request(url, allow_503=True)

    def readback(self, observed_at):
        sha = self.gh("commits/main")["sha"]
        if not SHA.fullmatch(str(sha)):
            raise RuntimeError("INVALID_MAIN_SHA")
        files = {}
        for name in RUNTIME_FILES:
            data = self.gh("contents/research/results/stock-shadow/" + name + "?ref=" + sha)
            if data.get("encoding") != "base64":
                raise RuntimeError("RUNTIME_FILE_ENCODING_INVALID")
            files[name] = safe_runtime(json.loads(base64.b64decode(data["content"])))
        return {"main_sha": sha, "observed_at": observed_at, "files": files}

    def runs(self, started_at):
        since = dt.datetime.fromtimestamp(timestamp(started_at) - 5, dt.timezone.utc).isoformat()
        query = urllib.parse.urlencode({"event": "workflow_dispatch", "per_page": 100, "created": ">=" + since})
        return self.gh("actions/runs?" + query)


def collect(evidence, reader, save, emit=print, now=utcnow, monotonic=time.monotonic,
            sleep=time.sleep, observation_seconds=25 * 60, completion_seconds=4 * 60):
    health_url = reader.configure(evidence)
    emit("STOCK_REMOTE_CONFIGURATION_VERIFIED")
    save()
    deadline = monotonic() + observation_seconds
    seen = set()

    def read_progress():
        snapshot = reader.readback(now())
        snapshots = evidence["runtime_readbacks"]
        if not snapshots or snapshot["main_sha"] != snapshots[-1]["main_sha"]:
            snapshots.append(snapshot)
        evidence["run_correlations"] = correlate_runs(evidence["cycles"], reader.runs(evidence["started_at"]))
        evidence["verdict"] = evaluate_evidence(evidence)
        save()

    while monotonic() < deadline:
        status, health = reader.health(health_url)
        cycle = safe_health(health, status, now())
        checked = cycle["checked_at"]
        # Cached health and an in-flight event scheduled before observation are
        # baseline, even when the Durable Object executes after we started.
        started = timestamp(evidence["started_at"])
        scheduled = cycle.get("scheduled_at")
        new_event = scheduled is None or timestamp(scheduled) >= started
        if checked and timestamp(checked) >= started and new_event and checked not in seen:
            seen.add(checked)
            evidence["cycles"].append(cycle)
            emit("STOCK_OBSERVED_CYCLE=" + json.dumps(cycle))
        # Read throughout observation so subsequent runs cannot overwrite the only
        # available runtime proof of an earlier dispatched cycle.
        if evidence["cycles"]:
            read_progress()
        if len(evidence["cycles"]) >= 3:
            break
        sleep(30)
    deadline = monotonic() + completion_seconds
    while monotonic() < deadline and any(c.get("dispatched") is True for c in evidence["cycles"]):
        read_progress()
        checks = evidence["verdict"]["checks"]
        if (checks["workflow_execution"]["status"] == "FAIL" or
                (checks["workflow_execution"]["status"] == "PASS" and checks["persisted_runtime"]["status"] == "PASS")):
            break
        sleep(30)
    if evidence["runtime_readbacks"]:
        evidence["final_main_sha"] = evidence["runtime_readbacks"][-1]["main_sha"]
    evidence["finished_at"] = now()
    evidence["live_failure_injection"] = "NOT_PERFORMED_ON_FORWARD_LEDGER"
    evidence["verdict"] = evaluate_evidence(evidence)
    evidence["three_consecutive_five_minute_periods"] = evidence["verdict"]["checks"]["five_minute_cadence"]["status"] == "PASS"
    save()
    emit("STOCK_LIVE_VERDICT=" + json.dumps(evidence["verdict"]))
    return 0 if evidence["verdict"]["overall"] == "PASS" else 1


def main():
    evidence = {"simulation_only": True, "mutations": [], "cycles": [], "run_correlations": [],
                "runtime_readbacks": [], "started_at": utcnow()}

    def save():
        OUT.write_text(json.dumps(evidence, indent=2) + "\n")

    try:
        return collect(evidence, LiveReader(), save, emit=lambda line: print(line, flush=True))
    except Exception as exc:
        evidence["error"] = safe_code(str(exc))
        evidence["verdict"] = evaluate_evidence(evidence)
        evidence["verdict"]["overall"] = "FAIL"
        evidence["finished_at"] = utcnow()
        save()
        print("STOCK_ACCEPTANCE_ERROR=" + evidence["error"], flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
