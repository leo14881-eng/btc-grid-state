#!/usr/bin/env python3
"""Stock Shadow infrastructure-only 5m generation/idempotency and scheduler health."""
import argparse, datetime as dt, json, pathlib
INTERVAL_SECONDS=300
HEALTH=pathlib.Path("research/results/stock-shadow/scheduler-health-v1.json")
def utc_now(): return dt.datetime.now(dt.timezone.utc)
def parse_time(value):
    if not value: return None
    try:
        x=dt.datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
    except ValueError: return None
def generation_id(now):
    now=now.astimezone(dt.timezone.utc); minute=now.minute-(now.minute%5)
    return now.replace(minute=minute,second=0,microsecond=0).isoformat().replace("+00:00","Z")
def health_level(age_seconds):
    if age_seconds is None: return "CRITICAL"
    if age_seconds<=600: return "HEALTHY"
    if age_seconds<=900: return "DEGRADED"
    if age_seconds<=1500: return "STALE"
    return "CRITICAL"
def load_health(path=HEALTH):
    try: return json.loads(path.read_text())
    except (OSError,ValueError): return {}
def should_process(now,health):
    current=generation_id(now); previous=health.get("last_successful_monitor_generation_id")
    return current, not (isinstance(previous,str) and current<=previous)
def build_success_health(now,started_at,previous,trigger_source,state_revision=None):
    current=generation_id(now); prev_at=parse_time(previous.get("monitor_completed_at_utc"))
    completed=now.astimezone(dt.timezone.utc); started=parse_time(started_at) or completed
    interval=(completed-prev_at).total_seconds() if prev_at else None
    missed=max(0,int(interval//INTERVAL_SECONDS)-1) if interval is not None else 0
    return {"schema":"stock_shadow_scheduler_health_v1","expected_interval_seconds":INTERVAL_SECONDS,
      "current_generation_id":current,"last_successful_monitor_generation_id":current,
      "trigger_source":trigger_source,"monitor_started_at_utc":started.isoformat(),
      "monitor_completed_at_utc":completed.isoformat(),"duration_seconds":max(0.0,round((completed-started).total_seconds(),3)),
      "previous_success_at_utc":previous.get("monitor_completed_at_utc"),
      "interval_since_previous_success_seconds":None if interval is None else round(interval,3),
      "missed_bucket_count":missed,"consecutive_missed_bucket_count":missed,
      "state_revision":state_revision,"health":health_level(interval),"simulation_only":True,"real_order_count":0}
def atomic_write(path,doc):
    path=pathlib.Path(path); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(doc,indent=2,sort_keys=True)+"\n"); tmp.replace(path)
def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="cmd",required=True)
    g=sub.add_parser("gate"); g.add_argument("--now"); g.add_argument("--health",default=str(HEALTH))
    s=sub.add_parser("success"); s.add_argument("--now"); s.add_argument("--started-at",required=True); s.add_argument("--trigger-source",required=True); s.add_argument("--state-revision"); s.add_argument("--health",default=str(HEALTH))
    a=p.parse_args(); now=parse_time(a.now) if a.now else utc_now(); h=load_health(pathlib.Path(a.health))
    if a.cmd=="gate":
        gid,process=should_process(now,h); print(json.dumps({"generation_id":gid,"process":process,"status":"PROCESS" if process else "ALREADY_PROCESSED"})); return
    doc=build_success_health(now,a.started_at,h,a.trigger_source,a.state_revision); atomic_write(a.health,doc); print(json.dumps(doc))
if __name__=="__main__": main()
