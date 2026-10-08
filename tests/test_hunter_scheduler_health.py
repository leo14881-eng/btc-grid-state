import datetime as dt, json, tempfile, unittest
from pathlib import Path
from research import hunter_scheduler_health as s

UTC=dt.timezone.utc
# Isolated P0 validation: infrastructure only; no strategy mutation.
class SchedulerHealthTests(unittest.TestCase):
 def test_generation_floor(self):
  self.assertEqual(s.generation_id(dt.datetime(2026,10,6,2,27,43,tzinfo=UTC)),"2026-10-06T02:25:00Z")
 def test_same_generation_is_duplicate(self):
  now=dt.datetime(2026,10,6,2,27,tzinfo=UTC); gid=s.generation_id(now)
  self.assertEqual(s.should_process(now,{"last_successful_monitor_generation_id":gid}),(gid,False))
 def test_older_generation_is_duplicate(self):
  now=dt.datetime(2026,10,6,2,27,tzinfo=UTC)
  self.assertFalse(s.should_process(now,{"last_successful_monitor_generation_id":"2026-10-06T02:30:00Z"})[1])
 def test_next_generation_processes(self):
  self.assertTrue(s.should_process(dt.datetime(2026,10,6,2,30,tzinfo=UTC),{"last_successful_monitor_generation_id":"2026-10-06T02:25:00Z"})[1])
 def test_health_thresholds(self):
  self.assertEqual(s.health_level(300),"HEALTHY"); self.assertEqual(s.health_level(600),"HEALTHY")
  self.assertEqual(s.health_level(601),"DEGRADED"); self.assertEqual(s.health_level(900),"DEGRADED")
  self.assertEqual(s.health_level(901),"STALE"); self.assertEqual(s.health_level(1500),"STALE")
  self.assertEqual(s.health_level(1501),"CRITICAL"); self.assertEqual(s.health_level(None),"CRITICAL")
 def test_success_marks_generation_only_when_explicitly_built(self):
  previous={}; started="2026-10-06T02:25:10+00:00"; now=dt.datetime(2026,10,6,2,25,40,tzinfo=UTC)
  self.assertNotIn("last_successful_monitor_generation_id",previous)
  doc=s.build_success_health(now,started,previous,"schedule","abc")
  self.assertEqual(doc["last_successful_monitor_generation_id"],"2026-10-06T02:25:00Z")
  self.assertEqual(doc["state_revision"],"abc"); self.assertEqual(doc["real_order_count"],0)
 def test_gap_metrics(self):
  now=dt.datetime(2026,10,6,2,26,tzinfo=UTC)
  for minutes,level in [(5,"HEALTHY"),(10,"HEALTHY"),(16,"STALE"),(26,"CRITICAL")]:
   prev=(now-dt.timedelta(minutes=minutes)).isoformat()
   d=s.build_success_health(now,now-dt.timedelta(seconds=20),{"monitor_completed_at_utc":prev},"test")
   self.assertEqual(d["health"],level)
 def test_serialized_same_bucket_mutates_once(self):
  now=dt.datetime(2026,10,6,2,27,tzinfo=UTC); health={}; ledger=[]
  for source in ("schedule","cloudflare"):
   gid,process=s.should_process(now,health)
   if process:
    ledger.append(source)
    health=s.build_success_health(now,now-dt.timedelta(seconds=20),health,source)
  self.assertEqual(len(ledger),1)
  self.assertEqual(health["last_successful_monitor_generation_id"],"2026-10-06T02:25:00Z")
 def test_persistence_failure_does_not_consume_generation(self):
  now=dt.datetime(2026,10,6,2,27,tzinfo=UTC); authoritative={}
  gid,process=s.should_process(now,authoritative); self.assertTrue(process)
  local=s.build_success_health(now,now-dt.timedelta(seconds=10),authoritative,"schedule")
  self.assertEqual(local["last_successful_monitor_generation_id"],gid)
  # Simulated push/CAS failure: local candidate never becomes authoritative.
  self.assertTrue(s.should_process(now,authoritative)[1])
 def test_stale_writer_cannot_make_newer_generation_process_again(self):
  newer={"last_successful_monitor_generation_id":"2026-10-06T02:30:00Z"}
  self.assertFalse(s.should_process(dt.datetime(2026,10,6,2,29,tzinfo=UTC),newer)[1])
 def test_atomic_roundtrip(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"h.json"; s.atomic_write(p,{"x":1}); self.assertEqual(json.loads(p.read_text()),{"x":1})
 def test_missed_bucket_uses_generation_not_faster_completion(self):
  previous={'last_successful_monitor_generation_id':'2026-10-08T00:25:00Z',
            'monitor_completed_at_utc':'2026-10-08T00:25:57.190236+00:00'}
  now=dt.datetime(2026,10,8,0,35,49,160381,tzinfo=UTC)
  doc=s.build_success_health(now,'2026-10-08T00:35:02.611075+00:00',previous,'VULTR_SYSTEMD')
  self.assertEqual(doc['missed_bucket_count'],1)
  self.assertEqual(doc['missed_bucket_provenance'],'GENERATION_BUCKET_DISTANCE')
 def test_long_execution_keeps_started_bucket(self):
  previous={'last_successful_monitor_generation_id':'2026-10-08T00:25:00Z',
            'monitor_completed_at_utc':'2026-10-08T00:25:40+00:00'}
  doc=s.build_success_health(dt.datetime(2026,10,8,0,35,10,tzinfo=UTC),
                            '2026-10-08T00:34:40+00:00',previous,'VULTR_SYSTEMD')
  self.assertEqual(doc['last_successful_monitor_generation_id'],'2026-10-08T00:30:00Z')
  self.assertEqual(doc['missed_bucket_count'],0)
 def test_explicit_admission_overrides_start_clock_boundary(self):
  doc=s.build_success_health(dt.datetime(2026,10,8,0,30,45,tzinfo=UTC),
                            '2026-10-08T00:29:59+00:00',{},'VULTR_SYSTEMD',
                            admitted_generation='2026-10-08T00:30:00Z')
  self.assertEqual(doc['current_generation_id'],'2026-10-08T00:30:00Z')
 def test_same_or_old_success_does_not_rewrite_health(self):
  previous={'last_successful_monitor_generation_id':'2026-10-08T00:35:00Z'}
  for minute in [30,35]:
   with self.subTest(minute=minute),self.assertRaises(ValueError):
    s.build_success_health(dt.datetime(2026,10,8,0,35,30,tzinfo=UTC),
                           dt.datetime(2026,10,8,0,minute,10,tzinfo=UTC),previous,'test')
if __name__=="__main__": unittest.main()
