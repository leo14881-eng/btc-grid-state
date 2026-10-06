import datetime as dt
from research import stock_shadow_scheduler_health as s
UTC=dt.timezone.utc
def test_generation_floor(): assert s.generation_id(dt.datetime(2026,10,6,2,27,43,tzinfo=UTC))=="2026-10-06T02:25:00Z"
def test_duplicate_and_next_generation():
 now=dt.datetime(2026,10,6,2,27,tzinfo=UTC); gid=s.generation_id(now); assert not s.should_process(now,{"last_successful_monitor_generation_id":gid})[1]; assert s.should_process(dt.datetime(2026,10,6,2,30,tzinfo=UTC),{"last_successful_monitor_generation_id":gid})[1]
def test_health_thresholds():
 assert s.health_level(600)=="HEALTHY"; assert s.health_level(601)=="DEGRADED"; assert s.health_level(901)=="STALE"; assert s.health_level(1501)=="CRITICAL"
def test_serialized_dual_trigger_mutates_once():
 now=dt.datetime(2026,10,6,2,27,tzinfo=UTC); health={}; ledger=[]
 for source in ("schedule","cloudflare"):
  gid,process=s.should_process(now,health)
  if process: ledger.append(source); health=s.build_success_health(now,now-dt.timedelta(seconds=20),health,source)
 assert len(ledger)==1
def test_failed_persistence_does_not_consume_generation():
 now=dt.datetime(2026,10,6,2,27,tzinfo=UTC); authoritative={}; gid,process=s.should_process(now,authoritative); assert process
 local=s.build_success_health(now,now-dt.timedelta(seconds=10),authoritative,"schedule"); assert local["last_successful_monitor_generation_id"]==gid; assert s.should_process(now,authoritative)[1]

# trigger isolated validation

# retry after runner cancellation
