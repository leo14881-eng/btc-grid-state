"""Read-only live acceptance. Never dispatch a workflow or modify the stock ledger."""
import base64
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "leo14881-eng/btc-grid-state"
WORKER = "stock-shadow-scheduler"
OUT = Path("/tmp/stock-scheduler-live-evidence.json")
CF = "https://api.cloudflare.com/client/v4/accounts/"
GH = f"https://api.github.com/repos/{REPO}/"
evidence = {"simulation_only": True, "mutations": [], "cycles": [],
            "run_correlations": [], "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}


def request(url, token=None, allow_503=False):
    headers = {"User-Agent": "stock-scheduler-read-only-acceptance"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        if allow_503 and e.code == 503:
            return e.code, json.load(e)
        raise RuntimeError("READ_HTTP_" + str(e.code)) from None
    except Exception:
        raise RuntimeError("READ_REQUEST_FAILED") from None


def cf(path):
    _, data = request(CF + account + "/" + path, os.environ["CLOUDFLARE_API_TOKEN"])
    if not data.get("success"):
        raise RuntimeError("CF_READ_FAILED")
    return data["result"]


def gh(path):
    return request(GH + path, os.environ["STOCK_AUDIT_GITHUB_TOKEN"])[1]


def file(path, sha):
    data = gh("contents/research/results/stock-shadow/" + path + "?ref=" + sha)
    return json.loads(base64.b64decode(data["content"]))


def save():
    OUT.write_text(json.dumps(evidence, indent=2) + "\n")


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


try:
    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
        raise RuntimeError("INVALID_ACCOUNT_ID")
    settings = cf(f"workers/scripts/{WORKER}/settings")
    bindings = settings.get("bindings", [])
    do = [b for b in bindings if b.get("name") == "STOCK_SCHEDULER"
          and b.get("type") == "durable_object_namespace" and b.get("class_name") == "StockScheduler"]
    secret = any(b.get("name") == "GITHUB_ACTIONS_TOKEN" and b.get("type") == "secret_text" for b in bindings)
    if len(do) != 1 or not secret:
        raise RuntimeError("STOCK_BINDING_OR_SECRET_MISSING")
    schedules = cf(f"workers/scripts/{WORKER}/schedules")
    schedules = schedules.get("schedules", []) if isinstance(schedules, dict) else schedules
    if [x.get("cron") for x in schedules] != ["3,8,13,18,23,28,33,38,43,48,53,58 * * * *"]:
        raise RuntimeError("STOCK_CRON_MISMATCH")
    subdomain = cf("workers/subdomain").get("subdomain", "")
    if not re.fullmatch(r"[a-zA-Z0-9-]+", subdomain):
        raise RuntimeError("INVALID_WORKERS_SUBDOMAIN")
    health_url = f"https://{WORKER}.{subdomain}.workers.dev/health"
    evidence["health_url"] = health_url
    evidence["remote_binding_check"] = "PASS"
    evidence["remote_secret_name_check"] = "PASS"
    evidence["remote_cron_check"] = "PASS"
    deployments = cf(f"workers/scripts/{WORKER}/deployments")
    evidence["deployments"] = [{k: d.get(k) for k in ("id", "created_on", "strategy", "versions")}
                               for d in deployments.get("deployments", [])]
    print("STOCK_REMOTE_CONFIGURATION_VERIFIED", flush=True)
    print("STOCK_HEALTH_URL=" + health_url, flush=True)
    save()
    deadline = time.monotonic() + 25 * 60
    seen = set()
    while time.monotonic() < deadline:
        status, health = request(health_url, allow_503=True)
        checked = health.get("checked_at")
        if checked and checked not in seen:
            seen.add(checked)
            # Public health contains scheduler decisions only, never credentials.
            row = {k: health.get(k) for k in ("checked_at", "action", "workflow", "dispatched", "reason",
                   "market", "stale", "monitor_age_seconds", "error", "scheduler_healthy")}
            row["http_status"] = status
            row["observed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            sha = gh("commits/main")["sha"]
            monitor = file("position-monitor-v1.json", sha)
            row["observed_main_sha"] = sha
            row["observed_monitor"] = {k: monitor.get(k) for k in ("run_id", "source_commit", "updated_at",
                 "status", "positions_before", "positions_updated", "missing_symbols", "errors")}
            evidence["cycles"].append(row)
            print("STOCK_AUTO_CYCLE=" + json.dumps(row), flush=True)
            save()
        if len(evidence["cycles"]) >= 3:
            break
        time.sleep(30)
    if len(evidence["cycles"]) < 3:
        raise RuntimeError("THREE_AUTOMATIC_PERIODS_NOT_OBSERVED")
    times = [timestamp(x["checked_at"]) for x in evidence["cycles"][-3:]]
    evidence["three_consecutive_five_minute_periods"] = all(270 <= b-a <= 330 for a,b in zip(times,times[1:]))
    # Read-only run correlation is explicit: API returns 204 without run IDs.
    # Require one uniquely matching workflow_dispatch run in the dispatch window.
    deadline = time.monotonic() + 4 * 60
    while time.monotonic() < deadline:
        runs = gh("actions/runs?event=workflow_dispatch&per_page=100")["workflow_runs"]
        correlations = []
        for cycle in evidence["cycles"]:
            if not cycle.get("dispatched"):
                correlations.append({"checked_at": cycle["checked_at"], "decision": cycle["action"], "run_id": None})
                continue
            start = timestamp(cycle["checked_at"])
            matches = [r for r in runs if r.get("head_branch") == "main"
                       and r.get("path") == ".github/workflows/" + cycle["workflow"]
                       and -5 <= timestamp(r["created_at"]) - start <= 90]
            row = {"checked_at": cycle["checked_at"], "correlation_method": "UNIQUE_WORKFLOW_AND_CREATION_WINDOW",
                   "matching_run_count": len(matches)}
            if len(matches) == 1:
                r = matches[0]
                row.update({k: r.get(k) for k in ("id", "created_at", "head_sha", "event", "status", "conclusion", "html_url")})
            correlations.append(row)
        evidence["run_correlations"] = correlations
        save()
        if all(not c.get("dispatched") or (r.get("matching_run_count") == 1 and r.get("status") == "completed")
               for c,r in zip(evidence["cycles"],correlations)):
            break
        time.sleep(30)
    sha = gh("commits/main")["sha"]
    evidence["final_main_sha"] = sha
    for name in ("position-monitor-v1.json", "main-run-health-v1.json", "summary-v1.json"):
        data = file(name, sha)
        keys = ("run_id", "source_commit", "updated_at", "status", "positions_before", "positions_updated",
                "missing_symbols", "errors", "scan_state_stale", "portfolio_updated_at", "portfolio_source_commit")
        evidence[name] = {k: data.get(k) for k in keys if k in data}
    evidence["live_failure_injection"] = "NOT_PERFORMED_ON_FORWARD_LEDGER"
    evidence["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save()
    print("STOCK_LIVE_EVIDENCE=" + json.dumps(evidence), flush=True)
    if not evidence["three_consecutive_five_minute_periods"]:
        raise RuntimeError("AUTOMATIC_PERIOD_GAP")
except Exception as e:
    evidence["error"] = str(e) if re.fullmatch(r"[A-Z0-9_]+", str(e)) else "ACCEPTANCE_READ_FAILED"
    save()
    print("STOCK_ACCEPTANCE_ERROR=" + evidence["error"], flush=True)
    raise SystemExit(1) from None
