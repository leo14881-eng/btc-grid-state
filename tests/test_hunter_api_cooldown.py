import datetime as dt
import importlib.util
import pathlib
import tempfile
import unittest

spec=importlib.util.spec_from_file_location(
    "hunter_api_cooldown",
    pathlib.Path(__file__).resolve().parents[1]/"research/hunter_api_cooldown.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW=dt.datetime(2026,9,26,10,tzinfo=dt.timezone.utc)

class CooldownTests(unittest.TestCase):
    def test_default_429_cooldown_survives_persistence(self):
        state=m.record_429({},NOW,stage="source_discovery")
        with tempfile.TemporaryDirectory() as folder:
            path=pathlib.Path(folder)/"state.json"
            m.save(state,path)
            loaded=m.load(path)
        self.assertTrue(m.blocked(loaded,NOW+dt.timedelta(minutes=30)))
        self.assertFalse(m.blocked(loaded,NOW+dt.timedelta(hours=2)))

    def test_retry_after_seconds_and_cap(self):
        state=m.record_429({},NOW,"120","identity")
        self.assertTrue(m.blocked(state,NOW+dt.timedelta(seconds=119)))
        self.assertFalse(m.blocked(state,NOW+dt.timedelta(seconds=121)))
        state=m.record_429({},NOW,"999999","identity")
        self.assertFalse(m.blocked(state,NOW+dt.timedelta(hours=5)))

    def test_retry_after_http_date(self):
        state=m.record_429({},NOW,"Sat, 26 Sep 2026 10:05:00 GMT","identity")
        self.assertTrue(m.blocked(state,NOW+dt.timedelta(minutes=4)))
        self.assertFalse(m.blocked(state,NOW+dt.timedelta(minutes=6)))

    def test_corrupt_or_missing_state_fails_safe_to_unblocked(self):
        with tempfile.TemporaryDirectory() as folder:
            path=pathlib.Path(folder)/"bad.json"
            path.write_text("{")
            self.assertEqual(m.load(path),{})
            self.assertFalse(m.blocked({"blocked_until_utc":"invalid"},NOW))

if __name__=="__main__":unittest.main()
