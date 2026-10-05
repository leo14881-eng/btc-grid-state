import datetime as dt, json, tempfile, unittest
from pathlib import Path
from research import hunter_scheduler_health as s

UTC=dt.timezone.utc
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
 def test_atomic_roundtrip(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"h.json"; s.atomic_write(p,{"x":1}); self.assertEqual(json.loads(p.read_text()),{"x":1})
if __name__=="__main__": unittest.main()
