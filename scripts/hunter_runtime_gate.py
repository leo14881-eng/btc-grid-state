"""Skip GitHub backup jobs only after a verified, recent Vultr success."""
import argparse
import datetime as dt
import json
import os
import pathlib

MAX_AGE = {"discovery": 90*60, "research": 90*60, "watchdog": 45*60,
           "blind-replay": 180*60, "missed-replay": 28*3600}


def should_run(job, config, health, now, scan=None):
 if config.get("primary_jobs", {}).get(job) is not True: return True
 if (health.get("schema") != "hunter_runtime_job_health_v1" or health.get("job") != job
     or health.get("status") != "SUCCESS" or health.get("real_trading_enabled") is not False
     or health.get("capital_authority") != "NONE_SHADOW_ONLY"
     or health.get("source") != "VULTR_SYSTEMD"
     or health.get("main_readback_verified") is not True): return True
 if job in ("discovery", "research"):
  generation = (scan or {}).get("generation_id")
  if not generation or health.get("scan_generation_id") != generation: return True
 try:
  completed = dt.datetime.fromisoformat(health["completed_at_utc"].replace("Z", "+00:00"))
  age = (now-completed).total_seconds()
 except (KeyError, TypeError, ValueError): return True
 return not (0 <= age <= MAX_AGE[job])


def load(path):
 try: return json.loads(pathlib.Path(path).read_text())
 except (OSError, ValueError): return {}


def main():
 p=argparse.ArgumentParser();p.add_argument("job",choices=MAX_AGE);a=p.parse_args()
 run=should_run(a.job,load(".github/hunter-runtime.json"),
                load("research/results/hunter-runtime-"+a.job+"-health.json"),
                dt.datetime.now(dt.timezone.utc),load("research/results/hunter-cex-universe-run.json"))
 if os.environ.get("GITHUB_OUTPUT"):
  with open(os.environ["GITHUB_OUTPUT"],"a") as f:f.write("run="+str(run).lower()+"\n")
 print("HUNTER_RUNTIME_GATE",a.job,"RUN_BACKUP" if run else "VULTR_RECENT_SUCCESS_SKIP")


if __name__=="__main__":main()
