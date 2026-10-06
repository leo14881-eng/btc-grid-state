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
  actual = subprocess.run(["git", "show", "origin/main:" + p], capture_output=True, check=True).stdout
  require(actual == data, "JOB_MAIN_READBACK_MISMATCH " + p)
 print("HUNTER_JOB_MAIN_READBACK_OK", job, commit, len(paths), flush=True)


def publish_health(job, status, started, steps, error=None):
 # A separate file per job avoids lost updates between independent schedulers.
 path = str(RESULTS / ("hunter-runtime-" + job + "-health.json"))
 end = dt.datetime.now(dt.timezone.utc)
 doc = {"schema": "hunter_runtime_job_health_v1", "job": job, "status": status,
        "started_at_utc": started.isoformat(), "completed_at_utc": end.isoformat(),
        "duration_seconds": round((end - started).total_seconds(), 3),
        "source": "VULTR_SYSTEMD", "steps": steps, "error": error,
        "capital_authority": "NONE_SHADOW_ONLY", "real_trading_enabled": False}
 doc["main_readback_verified"] = status == "SUCCESS"
 doc["source_head_sha"] = git("rev-parse", "HEAD").stdout.strip()
 doc["main_readback_head_sha"] = git("rev-parse", "origin/main").stdout.strip()
 if job in ("discovery", "research"):
  doc["scan_generation_id"] = json.loads((RESULTS / "hunter-cex-universe-run.json").read_text()).get("generation_id")
 raw = json.dumps(doc, indent=2, sort_keys=True) + "\n"
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
   if hgit("push", "origin", "HEAD:main", check=False).returncode == 0:
    hgit("fetch", "origin", "main")
    require(hgit("show", "origin/main:" + path).stdout == raw, "JOB_HEALTH_READBACK_MISMATCH")
    print("HUNTER_JOB_HEALTH_READBACK_OK", job, status, flush=True)
    return
 raise RuntimeError("JOB_HEALTH_PUSH_FAILED")


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


def run_shell(code, env, cwd, timeout=None):
 process = subprocess.Popen(["bash", "-euo", "pipefail", "-c", code],
                            env=env, cwd=cwd, start_new_session=True)
 try:
  process.wait(timeout=timeout)
 except subprocess.TimeoutExpired:
  os.killpg(process.pid, signal.SIGTERM)
  try: process.wait(timeout=5)
  except subprocess.TimeoutExpired:
   os.killpg(process.pid, signal.SIGKILL); process.wait()
  raise RuntimeError("PORTFOLIO_CRITICAL_PHASE_TIMEOUT")
 return subprocess.CompletedProcess(process.args, process.returncode)


def run_job(job, preview=False):
 base = git("rev-parse", "HEAD").stdout.strip()
 scan = json.loads((RESULTS / "hunter-cex-universe-run.json").read_text())
 generation = scan.get("generation_id", "")
 env = dict(os.environ)
 env["EXPECTED"] = generation
 env["HUNTER_RESEARCH_ISOLATED_CHECKOUT"] = "1"
 steps = []
 lock = None
 lock_started = None
 try:
  for step in workflow_steps(job):
   if "uses" in step: continue  # checkout and runtimes are prepared by the launcher.
   name = step.get("name") or step.get("id", "unnamed")
   if name in ("Refresh latest published baseline", "Refresh latest main baseline"):
    continue  # launcher already fetched latest main in an isolated checkout.
   if preview and ("ersist" in name or name == "Refresh admitted generation baseline"
                   or name.startswith("Bind latest") or name.startswith("Self-heal")):
    print("HUNTER_JOB_PREVIEW_SKIP", job, name, flush=True); continue
   outputs = output_file(env["GITHUB_OUTPUT"])
   if not condition(step, outputs): continue
   if job == "watchdog" and name.startswith("Self-heal"):
    units = []
    if "discovery or downstream" in name:
     if outputs.get("discovery_stale") == "true" or outputs.get("downstream_stale") == "true":
      units.append("hunter-discovery.service")
    else: units.append("hunter-position-monitor.service")
    for unit in units:
     subprocess.run(["systemctl", "start", "--no-block", unit], check=True)
    print("HUNTER_SERVER_SELF_HEAL", units, flush=True); continue
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
   started = time.monotonic()
   print("HUNTER_JOB_STEP_START", job, name, flush=True)
   remaining = None if lock_started is None else max(0.01, 90-(time.monotonic()-lock_started))
   result = run_shell(step["run"], local_env, step.get("working-directory", "."), remaining)
   row = {"name": name, "seconds": round(time.monotonic()-started,3), "exit": result.returncode}
   steps.append(row)
   print("HUNTER_JOB_STEP_END", job, json.dumps(row), flush=True)
   require(result.returncode == 0 or step.get("continue-on-error") is True, "JOB_STEP_FAILED " + name)
   env.update(output_file(env["GITHUB_ENV"]))
   if name == "Capture discovery generation": generation = output_file(env["GITHUB_OUTPUT"])["id"]
  if not preview: readback(job)
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
   rows.extend(run_job(job, args.preview))
  if not args.preview: publish_health(args.job, "SUCCESS", started, rows)
  print("HUNTER_JOB_SUCCESS", args.job, "preview=" + str(args.preview), flush=True)
 except Exception as exc:
  print("HUNTER_JOB_FAILURE", args.job, type(exc).__name__, str(exc)[:200], flush=True)
  if not args.preview:
   try: publish_health(args.job, "FAILURE", started, rows, type(exc).__name__)
   except Exception: print("HUNTER_JOB_FAILURE_HEALTH_NOT_PUBLISHED", args.job, flush=True)
  raise


if __name__ == "__main__": main()
