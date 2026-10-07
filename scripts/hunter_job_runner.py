"""Run existing Hunter workflow steps in disposable Vultr checkouts."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import time
import tempfile

JOBS = {
 "discovery": ("hunter-cex-universe.yml", "discovery"),
 "research": ("hunter-research-pipeline.yml", "research"),
 "watchdog": ("hunter-stale-watchdog.yml", "check"),
 "blind-replay": ("hunter-blind-replay.yml", "replay"),
 "missed-replay": ("hunter-missed-opportunity-replay.yml", "replay"),
}
RESULTS = pathlib.Path("research/results")
PORTFOLIOS = ["hunter-shadow-portfolio.json", "hunter-shadow-v2-portfolio.json"]
CONFIG = pathlib.Path(".github/hunter-runtime.json")


def git(*args, check=True):
 return subprocess.run(["git", *args], text=True, capture_output=True, check=check)


def require(ok, message):
 if not ok: raise RuntimeError(message)


def workflow_steps(job):
 name, key = JOBS[job]
 lines = pathlib.Path(".github/workflows", name).read_text().splitlines()
 begin = next(i for i,line in enumerate(lines) if line == "  " + key + ":")
 end = next((i for i in range(begin+1,len(lines))
             if re.match(r"^  [A-Za-z0-9_-]+:$",lines[i])),len(lines))
 section = lines[begin+1:end]
 start = next(i for i,line in enumerate(section) if line == "    steps:")
 rows=[]; current=None; i=start+1
 def literal(value):
  value=value.strip()
  return value[1:-1] if len(value)>=2 and value[0]==value[-1] and value[0] in ("'",'"') else value
 while i<len(section):
  line=section[i]
  if not line.strip() or line.lstrip().startswith("#"):i+=1;continue
  if line.startswith("      - "):
   current={};rows.append(current);field=line[8:]
  elif line.startswith("        ") and not line.startswith("          "):
   field=line[8:]
  else:
   # Only action setup parameters can be omitted: launcher supplies checkout/runtime.
   require(current is not None and "uses" in current,"UNSUPPORTED_WORKFLOW_INDENT")
   i+=1;continue
  require(current is not None and ":" in field,"UNSUPPORTED_WORKFLOW_STEP")
  field_key,value=field.split(":",1);field_key=field_key.strip();value=value.strip()
  require(field_key in ("uses","id","name","run","env","if","continue-on-error","working-directory","with"),
          "UNSUPPORTED_WORKFLOW_FIELD")
  if field_key=="run" and value=="|":
   body=[];i+=1
   while i<len(section) and (not section[i].strip() or section[i].startswith("          ")):
    body.append(section[i][10:] if section[i].strip() else "");i+=1
   current["run"]="\n".join(body).rstrip("\n")+"\n";continue
  if field_key=="env":
   require(not value,"UNSUPPORTED_INLINE_ENV");current["env"]={};i+=1
   while i<len(section) and section[i].startswith("          "):
    env_key,env_value=section[i].strip().split(":",1)
    current["env"][env_key]=literal(env_value);i+=1
   continue
  if field_key=="with":
   require("uses" in current,"UNSUPPORTED_WORKFLOW_SETUP")
  elif field_key=="continue-on-error":
   require(value in ("true","false"),"UNSUPPORTED_CONTINUE_POLICY")
   current[field_key]=value=="true"
  else:current[field_key]=literal(value)
  i+=1
 return rows

def resolve(value, env, generation, sha):
 def replace(match):
  key = match.group(1).strip()
  if re.fullmatch(r"secrets\.[A-Za-z0-9_]+", key): return env.get(key.split(".", 1)[1], "")
  if key in ("steps.generation.outputs.id", "needs.bind.outputs.generation"): return generation
  if key == "github.sha": return sha
  raise RuntimeError("UNSUPPORTED_WORKFLOW_EXPRESSION")
 return re.sub(r"\$\{\{(.*?)\}\}", replace, str(value))


def output_file(path):
 try: return dict(line.split("=", 1) for line in pathlib.Path(path).read_text().splitlines() if "=" in line)
 except FileNotFoundError: return {}


def output_paths(job):
 if job == "discovery":
  return ["hunter-cex-universe-run.json", "hunter-cex-universe-latest.json",
          "hunter-cex-universe-summary.json", "hunter-universe-history.json",
          "hunter-bybit-spot-cache.json", "hunter-bybit-worker-snapshot.json"]
 if job == "research":
  return ["hunter-forward-research.json", "hunter-early-signals.json", "hunter-early-signal-history.json",
          "hunter-market-enrichment.json", "hunter-identity-audit.json", "hunter-forward-audit.json",
          "hunter-contract-corroboration-cache.json", "hunter-candidate-dossiers.json",
          "hunter-liquidity-probe.json", "hunter-health-and-queue.json", "hunter-tactical-capital-review.json",
          "hunter-tactical-supply-risk.json", "hunter-research-completion.json",
          "hunter-evidence-enrichment.json", "hunter-primary-source-leads.json", "hunter-primary-source-cache.json",
          "hunter-api-cooldown.json", *PORTFOLIOS, "hunter-shadow-summary.json", "hunter-shadow-v2-summary.json",
          "hunter-shadow-v2-overfilter-guard.json", "hunter-tail-risk-replay.json",
          "hunter-leading-risk-replay.json", "hunter-bybit-availability.json", "hunter-bybit-spot-cache.json",
          "hunter-shadow-candidate-ledger.json", "hunter-shadow-rules.json", "hunter-shadow-optimizer-audit.jsonl"]
 if job == "blind-replay":
  return ["hunter-blind-replay-summary.json", "hunter-blind-replay-events.csv",
          "hunter-blind-replay-controls.csv", "hunter-archive-symbol-manifest.json"]
 if job == "missed-replay": return ["hunter-missed-opportunity-replay.json"]
 return []


def readback(job):
 paths = [str(RESULTS / p) for p in output_paths(job) if (RESULTS / p).exists()]
 if job == "blind-replay" and pathlib.Path("hunter-replay-v1.json").exists():
  paths.append("hunter-replay-v1.json")
 expected = {p: pathlib.Path(p).read_bytes() for p in paths}
 commit = git("rev-parse", "HEAD").stdout.strip()
 git("fetch", "origin", "main")
 require(git("merge-base", "--is-ancestor", commit, "origin/main", check=False).returncode == 0,
         "JOB_COMMIT_NOT_AUTHORITATIVE")
 for p, data in expected.items():
  result = subprocess.run(["git", "show", "origin/main:" + p], capture_output=True)
  if result.returncode or result.stdout != data:
   code = "JOB_MAIN_READBACK_SOURCE_FAILED" if result.returncode else "JOB_MAIN_READBACK_MISMATCH"
   exc = RuntimeError(code + " " + p)
   exc.failed_path = p
   exc.stderr_tail = safe_diagnostic(result.stderr.decode("utf-8", errors="replace"))
   raise exc
 print("HUNTER_JOB_MAIN_READBACK_OK", job, commit, len(paths), flush=True)


def publish_health(job, status, started, steps, error=None, error_details=None):
 # A separate file per job avoids lost updates between independent schedulers.
 path = str(RESULTS / ("hunter-runtime-" + job + "-health.json"))
 end = dt.datetime.now(dt.timezone.utc)
 doc = {"schema": "hunter_runtime_job_health_v1", "job": job, "status": status,
        "started_at_utc": started.isoformat(), "completed_at_utc": end.isoformat(),
        "duration_seconds": round((end - started).total_seconds(), 3),
        "source": "VULTR_SYSTEMD", "steps": steps, "error": error,
        "capital_authority": "NONE_SHADOW_ONLY", "real_trading_enabled": False}
 if error_details:
  doc["error_details"] = error_details
 if job == "watchdog":
  from scripts.hunter_market_stream import watchdog
  doc["fast_watch_health"] = watchdog("/var/lib/hunter-fast-watch/state.json", end.timestamp())
 doc["main_readback_verified"] = status == "SUCCESS"
 doc["source_head_sha"] = git("rev-parse", "HEAD").stdout.strip()
 doc["main_readback_head_sha"] = git("rev-parse", "origin/main").stdout.strip()
 if job in ("discovery", "research"):
  doc["scan_generation_id"] = json.loads((RESULTS / "hunter-cex-universe-run.json").read_text()).get("generation_id")
 # Preserve real job result even if subsequent GitHub health publication fails.
 # This local receipt is diagnostic input for watchdog, not main readback proof.
 from scripts import hunter_local_job_health
 try:
  hunter_local_job_health.write(job, doc)
 except (OSError, ValueError, KeyError, RuntimeError) as exc:
  print("HUNTER_LOCAL_JOB_HEALTH_FAILED", job, safe_diagnostic(str(exc)), flush=True)
 raw = json.dumps(doc, indent=2, sort_keys=True) + "\n"
 last_push = None
 for attempt in range(5):
  git("fetch", "origin", "main")
  # Publish only this health document from a clean current-main auxiliary checkout.
  import tempfile
  with tempfile.TemporaryDirectory(prefix="health-", dir=os.environ["RUNNER_TEMP"]) as d:
   subprocess.run(["git", "clone", "--quiet", "--shared", ".", d], check=True)
   remote = git("remote", "get-url", "origin").stdout.strip()
   def hgit(*a, check=True):
    return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, check=check)
   hgit("remote", "set-url", "origin", remote); hgit("fetch", "origin", "main")
   hgit("checkout", "--detach", "origin/main")
   previous = pathlib.Path(d, path)
   if previous.exists():
    old = json.loads(previous.read_text())
    old_started = dt.datetime.fromisoformat(old["started_at_utc"].replace("Z", "+00:00"))
    require(old_started <= started, "JOB_HEALTH_REJECTED_STALE_WRITER")
   target = pathlib.Path(d, path); target.write_text(raw)
   hgit("config", "user.name", "hunter-vultr-shadow")
   hgit("config", "user.email", "hunter-vultr-shadow@localhost")
   hgit("add", "-f", "--", path); hgit("commit", "-m", "ops: Hunter " + job + " runtime health")
   pushed = hgit("push", "origin", "HEAD:main", check=False)
   if pushed.returncode:
    category = push_failure_category(pushed)
    last_push = pushed
    if category not in ("RACE", "TRANSPORT_FAILED"):
     raise push_failure_exception("JOB_HEALTH_PUSH", pushed, category)
    print("HUNTER_JOB_HEALTH_PUSH_RETRY", job, attempt + 1, category,
          safe_diagnostic(pushed.stderr), flush=True)
   else:
    hgit("fetch", "origin", "main")
    require(hgit("show", "origin/main:" + path).stdout == raw, "JOB_HEALTH_READBACK_MISMATCH")
    print("HUNTER_JOB_HEALTH_READBACK_OK", job, status, flush=True)
    return
 raise push_failure_exception("JOB_HEALTH_PUSH_RETRY_EXHAUSTED", last_push)


def condition(step, outputs):
 value = step.get("if")
 if not value: return True
 if value == "always()": return True
 if "steps.observe.outputs." in value:
  keys = re.findall(r"steps.observe.outputs\.(\w+)\s*==\s*'true'", value)
  require(bool(keys), "UNSUPPORTED_WATCHDOG_CONDITION")
  return any(outputs.get(k) == "true" for k in keys)
 raise RuntimeError("UNSUPPORTED_STEP_CONDITION")


def enabled(job):
 try: return json.loads(CONFIG.read_text()).get("primary_jobs", {}).get(job) is True
 except (OSError, ValueError): return False


def safe_diagnostic(value, env=None, limit=2048):
 """Persist bounded diagnostic text, never raw shell arguments or credentials."""
 text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
 for key, value in (os.environ if env is None else env).items():
  if value and re.search(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTHORIZATION|PRIVATE|API_KEY", key, re.I):
   for part in [value, *str(value).splitlines()]:
    if len(part) >= 4: text = text.replace(part, "[REDACTED]")
 text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
               "[REDACTED_PRIVATE_KEY]", text, flags=re.S)
 text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
 text = re.sub(r"(?i)(authorization[\s:=]+(?:bearer|basic)\s+)[^\s'\"]+",
               r"\1[REDACTED]", text)
 text = re.sub(r"(?i)([?&](?:token|key|api_key|access_token|secret|password)=)[^&\s'\"]+",
               r"\1[REDACTED]", text)
 text = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[REDACTED]", text)
 return text[-limit:]


def push_failure_category(result):
 """Classify actual Git diagnostics; unknown/policy errors are not CAS races."""
 text = (str(result.stderr or "") + "\n" + str(result.stdout or "")).lower()
 if any(x in text for x in ("authentication failed", "could not read username",
                            "permission denied", "error: 403", "http 403",
                            "write access to repository not granted")):
  return "AUTHENTICATION_FAILED"
 if any(x in text for x in ("gh001", "gh006", "gh013", "protected branch",
                            "repository rule violations", "file size limit",
                            "pre-receive hook declined")):
  return "POLICY_REJECTED"
 if any(x in text for x in ("(fetch first)", "(non-fast-forward)", "(stale info)")):
  return "RACE"
 if any(x in text for x in ("connection reset", "connection timed out",
                            "operation timed out", "temporary failure in name resolution",
                            "could not resolve host", "connection closed",
                            "error: 502", "error: 503", "error: 504")):
  return "TRANSPORT_FAILED"
 return "REJECTED_UNKNOWN"


def push_failure_exception(prefix, result, category=None):
 category = category or push_failure_category(result)
 exc = RuntimeError(prefix + "_" + category)
 exc.stderr_tail = safe_diagnostic(str(result.stderr or "") + "\n" + str(result.stdout or ""))
 return exc


def exception_details(exc):
 # CalledProcessError.__str__ includes the whole shell command. Report stderr
 # and exit code instead; command source and argv are never sent to main.
 if isinstance(exc, subprocess.CalledProcessError):
  message = "SUBPROCESS_FAILED exit=" + str(exc.returncode)
  stderr = safe_diagnostic(exc.stderr)
 else:
  message = safe_diagnostic(str(exc))
  stderr = getattr(exc, "stderr_tail", "")
 result = {"type": type(exc).__name__, "message": message}
 if getattr(exc, "failed_path", None): result["failed_path"] = exc.failed_path
 if stderr: result["stderr_tail"] = stderr
 return result


def run_shell(code, env, cwd, timeout=None):
 # Temporary file avoids pipe deadlock and unbounded in-memory stderr capture.
 # stdout retains its existing live journal stream.
 with tempfile.TemporaryFile() as errors:
  process = subprocess.Popen(["bash", "-euo", "pipefail", "-c", code],
                             env=env, cwd=cwd, start_new_session=True, stderr=errors)
  timed_out = False
  try:
   process.wait(timeout=timeout)
  except subprocess.TimeoutExpired:
   timed_out = True
   os.killpg(process.pid, signal.SIGTERM)
   try: process.wait(timeout=5)
   except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGKILL); process.wait()
  errors.seek(0, os.SEEK_END)
  errors.seek(max(0, errors.tell()-65536))
  tail = safe_diagnostic(errors.read().decode("utf-8", errors="replace"), env)
  if tail: print("HUNTER_JOB_STDERR_TAIL", tail, flush=True)
  if timed_out:
   exc = RuntimeError("PORTFOLIO_CRITICAL_PHASE_TIMEOUT")
   exc.stderr_tail = tail
   raise exc
  return subprocess.CompletedProcess(process.args, process.returncode, stderr=tail)


AUX_CAS_RECOVERY_MAX_AGE_SECONDS = 3600
AUX_RECOVERY_STEPS = {
 "blind-replay": "Persist replay outputs from clean latest main",
 "missed-replay": "Persist replay only",
}


def auxiliary_cas_recovery_units(results, now, local_results=None):
 """Retry complete replay from a fresh checkout; never retry stale publication.

 The existing installed scheduled-job wrapper owns each job's flock and fetches
 main into a disposable checkout. Only exact fresh CAS/race/transport publication
 failures are eligible. Local receipts cover failed GitHub health publication;
 authentication, policy, unknown, market-data and validator failures remain failures.
 """
 units = []
 for job, expected_step in AUX_RECOVERY_STEPS.items():
  try:
   candidates = []
   for root in (results, local_results):
    if root is None: continue
    try:
     candidate = json.loads((root / ('hunter-runtime-'+job+'-health.json')).read_text())
     if root == local_results and (candidate.get('local_publication_only') is not True or
          candidate.get('schema') != 'hunter_runtime_job_health_v1'):
      continue
     order = dt.datetime.fromisoformat(candidate.get('started_at_utc') or candidate['completed_at_utc'])
     if order.tzinfo is None: continue
     candidates.append((order, candidate))
    except (OSError, ValueError, KeyError, TypeError):
     continue
   if not candidates: continue
   # Newer success suppresses an older local failure; never resurrect old work.
   row = max(candidates, key=lambda item:item[0])[1]
   if (row.get('status') != 'FAILURE' or row.get('job') != job or
       row.get('source') != 'VULTR_SYSTEMD' or row.get('capital_authority') != 'NONE_SHADOW_ONLY' or
       row.get('real_trading_enabled') is not False or not row.get('source_head_sha')):
    continue
   completed = dt.datetime.fromisoformat(row['completed_at_utc'])
   if completed.tzinfo is None or not 0 <= (now-completed).total_seconds() <= AUX_CAS_RECOVERY_MAX_AGE_SECONDS:
    continue
   failed = [step for step in row.get('steps', []) if step.get('status') == 'FAILURE']
   if len(failed) != 1 or failed[0].get('name') != expected_step:
    continue
   if row.get('error_details', {}).get('failed_step') != expected_step:
    continue
   eligible_errors = ('AUX_CAS_REJECTED_STALE_WRITER',
                      'AUX_PUSH_RETRY_EXHAUSTED_RACE',
                      'AUX_PUSH_RETRY_EXHAUSTED_TRANSPORT_FAILED')
   if not any(re.search(r'(?m)^RuntimeError: '+code+r'(?:\s|$)',
                        failed[0].get('stderr_tail', '')) for code in eligible_errors):
    continue
   units.append('hunter-'+job+'.service')
  except (OSError, ValueError, KeyError, TypeError):
   continue
 return units


def run_job(job, preview=False, steps=None):
 # Share this collector with main so failures retain all completed/failed steps.
 if steps is None: steps = []
 base = git("rev-parse", "HEAD").stdout.strip()
 scan = json.loads((RESULTS / "hunter-cex-universe-run.json").read_text())
 generation = scan.get("generation_id", "")
 env = dict(os.environ)
 env["EXPECTED"] = generation
 env["HUNTER_RESEARCH_ISOLATED_CHECKOUT"] = "1"
 lock = None
 lock_started = None
 try:
  if job == "watchdog" and not preview:
   from scripts import hunter_local_job_health
   units = auxiliary_cas_recovery_units(RESULTS, dt.datetime.now(dt.timezone.utc),
                                      local_results=hunter_local_job_health.directory())
   if units:
    row = dict(job=job, name="Request fresh-source auxiliary CAS recovery", status="RUNNING",
               exit=None, started_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
               units=units, recovery_mode="FULL_JOB_FROM_LATEST_MAIN", recovery_verified=False)
    steps.append(row)
    try:
     for unit in units:
      subprocess.run(["systemctl", "start", "--no-block", unit], check=True)
     row.update(status="SUCCESS", exit=0, action_status="RECOVERY_REQUESTED_NOT_VERIFIED")
    except Exception as exc:
     row.update(status="FAILURE", error_details=exception_details(exc))
     raise
    finally:
     row["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
  for step in workflow_steps(job):
   if "uses" in step: continue
   name = step.get("name") or step.get("id", "unnamed")
   if name in ("Refresh latest published baseline", "Refresh latest main baseline"):
    continue
   if preview and ("ersist" in name or name == "Refresh admitted generation baseline"
                   or name.startswith("Bind latest") or name.startswith("Self-heal")):
    print("HUNTER_JOB_PREVIEW_SKIP", job, name, flush=True); continue
   # Include condition/lock/env-resolution failures in the active step record.
   started = time.monotonic()
   row = {"job": job, "name": name, "status": "RUNNING", "exit": None,
          "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
   steps.append(row)
   try:
    outputs = output_file(env["GITHUB_OUTPUT"])
    if not condition(step, outputs):
     row["status"] = "SKIPPED"
     continue
    if job == "watchdog" and name.startswith("Self-heal"):
     units = []
     if "discovery or downstream" in name:
      if outputs.get("discovery_stale") == "true" or outputs.get("downstream_stale") == "true":
       units.append("hunter-discovery.service")
     else: units.append("hunter-position-monitor.service")
     for unit in units:
      subprocess.run(["systemctl", "start", "--no-block", unit], check=True)
     print("HUNTER_SERVER_SELF_HEAL", units, flush=True)
     row.update(status="SUCCESS", exit=0)
     continue
    if job == "research" and name.startswith("Bind latest"):
     lock = open("/run/lock/hunter-position-monitor.lock", "a")
     waiting = time.monotonic()
     while True:
      try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
      except BlockingIOError:
       require(time.monotonic() - waiting < 120, "PORTFOLIO_LOCK_WAIT_TIMEOUT")
       time.sleep(0.2)
     lock_started = time.monotonic()
     print("HUNTER_PORTFOLIO_LOCK_ACQUIRED", round(time.monotonic()-waiting,3), flush=True)
    local_env = {**env, **{k: resolve(v, env, generation, base) for k,v in step.get("env", {}).items()}}
    print("HUNTER_JOB_STEP_START", job, name, flush=True)
    remaining = None if lock_started is None else max(0.01, 90-(time.monotonic()-lock_started))
    result = run_shell(step["run"], local_env, step.get("working-directory", "."), remaining)
    row["exit"] = result.returncode
    stderr = getattr(result, "stderr", None)
    if isinstance(stderr, str) and stderr: row["stderr_tail"] = safe_diagnostic(stderr, local_env)
    if result.returncode != 0 and step.get("continue-on-error") is True:
     row["status"] = "CONTINUED_AFTER_ERROR"
    else:
     require(result.returncode == 0, "JOB_STEP_FAILED " + name)
     row["status"] = "SUCCESS"
    env.update(output_file(env["GITHUB_ENV"]))
    if name == "Capture discovery generation": generation = output_file(env["GITHUB_OUTPUT"])["id"]
   except Exception as exc:
    row["status"] = "FAILURE"
    row["error_details"] = exception_details(exc)
    raise
   finally:
    row["seconds"] = round(time.monotonic()-started, 3)
    row["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    print("HUNTER_JOB_STEP_END", job, json.dumps(row), flush=True)
  if not preview:
   started = time.monotonic()
   row = {"job": job, "name": "Authoritative main read-back", "status": "RUNNING", "exit": None,
          "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
   steps.append(row)
   try:
    readback(job)
    row.update(status="SUCCESS", exit=0)
   except Exception as exc:
    row.update(status="FAILURE", error_details=exception_details(exc))
    raise
   finally:
    row["seconds"] = round(time.monotonic()-started,3)
    row["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
  return steps
 finally:
  if lock is not None: lock.close()


def main():
 parser = argparse.ArgumentParser()
 parser.add_argument("job", choices=[*JOBS, "pipeline"])
 parser.add_argument("--preview", action="store_true")
 parser.add_argument("--acceptance", action="store_true")
 args = parser.parse_args()
 require(args.job != "pipeline" or args.preview, "PIPELINE_PREVIEW_ONLY")
 if not args.preview and not args.acceptance and not enabled(args.job):
  print("HUNTER_SERVER_JOB_DISABLED", args.job); return
 rules = json.loads((RESULTS / "hunter-shadow-rules.json").read_text())
 require(rules["lanes"]["V2"]["capital_pool_usdt"] == 20000 and
         rules["capital_authority"] == "NONE_SHADOW_ONLY" and
         rules["tail_risk_phase1"]["real_trading_enabled"] is False, "SHADOW_BOUNDARY")
 started = dt.datetime.now(dt.timezone.utc)
 rows = []
 try:
  for job in (["discovery", "research"] if args.job == "pipeline" else [args.job]):
   run_job(job, args.preview, rows)
  if not args.preview: publish_health(args.job, "SUCCESS", started, rows)
  print("HUNTER_JOB_SUCCESS", args.job, "preview=" + str(args.preview), flush=True)
 except Exception as exc:
  details = exception_details(exc)
  details["failed_step"] = next((r["name"] for r in reversed(rows) if r.get("status") == "FAILURE"), "INITIALIZATION_OR_HEALTH_PUBLICATION")
  print("HUNTER_JOB_FAILURE", args.job, json.dumps(details), flush=True)
  if not args.preview:
   try: publish_health(args.job, "FAILURE", started, rows, type(exc).__name__, details)
   except Exception as health_exc:
    print("HUNTER_JOB_FAILURE_HEALTH_NOT_PUBLISHED", args.job, json.dumps(exception_details(health_exc)), flush=True)
  raise RuntimeError("HUNTER_JOB_FAILED " + details["message"]) from None


if __name__ == "__main__": main()
