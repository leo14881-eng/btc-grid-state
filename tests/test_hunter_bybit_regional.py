import datetime as dt
import copy
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

FILE=pathlib.Path(__file__).resolve().parents[1]/"research/hunter_bybit_regional.py"
spec=importlib.util.spec_from_file_location("hunter_bybit_regional",FILE)
r=importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
NOW=dt.datetime(2026,9,26,10,tzinfo=dt.timezone.utc)
ROWS=[{"venue":"bybit","pair":"ABCUSDT","base":"ABC",
       "price":1.5,"volume_24h_usdt":100000.0,"change_24h_pct":5.0}]
STATUS={"active_pairs":1,"valid_pairs":1,"missing_or_invalid":[]}

def sample():
    payload={"schema":"hunter_bybit_regional_v1","source":r.SOURCE,
             "captured_at_utc":NOW.isoformat(),"egress_country":"VN",
             "rows":copy.deepcopy(ROWS),"venue_status":copy.deepcopy(STATUS)}
    payload["snapshot_sha256"]=r.checksum(payload)
    return payload

class RegionalTests(unittest.TestCase):
    def test_valid_fresh_official_snapshot(self):
        rows,status=r.validate(sample(),NOW+dt.timedelta(minutes=15))
        self.assertEqual(rows,ROWS)
        self.assertEqual(status["source"],r.SOURCE)
        self.assertEqual(status["egress_country"],"VN")

    def test_reject_stale_or_future(self):
        with self.assertRaisesRegex(ValueError,"STALE"):
            r.validate(sample(),NOW+dt.timedelta(minutes=46))
        with self.assertRaisesRegex(ValueError,"FUTURE"):
            r.validate(sample(),NOW-dt.timedelta(seconds=1))

    def test_reject_corrupt_and_untrusted(self):
        p=sample();p["rows"][0]["price"]=2
        with self.assertRaisesRegex(ValueError,"CHECKSUM"):
            r.validate(p,NOW)
        p=sample();p["source"]="THIRD_PARTY";p["snapshot_sha256"]=r.checksum(
            {k:v for k,v in p.items() if k!="snapshot_sha256"})
        with self.assertRaisesRegex(ValueError,"UNTRUSTED"):
            r.validate(p,NOW)

    def test_reject_partial_or_wrong_venue_even_with_recomputed_checksum(self):
        p=sample();p["venue_status"]["active_pairs"]=2
        p["snapshot_sha256"]=r.checksum({k:v for k,v in p.items() if k!="snapshot_sha256"})
        with self.assertRaisesRegex(ValueError,"INCOMPLETE"):
            r.validate(p,NOW)
        p=sample();p["rows"][0]["venue"]="binance"
        p["snapshot_sha256"]=r.checksum({k:v for k,v in p.items() if k!="snapshot_sha256"})
        with self.assertRaisesRegex(ValueError,"WRONG_VENUE"):
            r.validate(p,NOW)

    def test_collector_rejects_unconfirmed_or_us_country_before_fetch(self):
        with patch.dict(os.environ,{"HUNTER_BYBIT_RUNNER_COUNTRY":"VN"}):
            with self.assertRaisesRegex(RuntimeError,"COUNTRY"):
                r.collect(NOW,fetcher=lambda: self.fail("must not fetch"),country="US")
            with self.assertRaisesRegex(RuntimeError,"COUNTRY"):
                r.collect(NOW,fetcher=lambda: self.fail("must not fetch"),country="SG")

    def test_collector_accepts_explicit_matching_region(self):
        with patch.dict(os.environ,{"HUNTER_BYBIT_RUNNER_COUNTRY":"VN"}):
            p=r.collect(NOW,fetcher=lambda:(ROWS,STATUS),country="VN")
        self.assertEqual(r.validate(p,NOW)[0],ROWS)

    def test_roundtrip_from_disk(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            path=pathlib.Path(folder)/"snapshot.json"
            path.write_text(json.dumps(sample()))
            self.assertEqual(r.load(path,NOW)[0],ROWS)

if __name__=="__main__":unittest.main()
