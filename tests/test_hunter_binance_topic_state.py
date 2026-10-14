"""PR-A isolated topic revision storage and fail-closed reading tests."""
import sqlite3
import tempfile
from pathlib import Path
import unittest

from research.hunter_binance_topic_state import install, begin, bump, key, IncrementalReader
from research.hunter_binance_stream_store import topic


class TopicStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="bn-topic-revision-")
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"state.sqlite3"
        self.db=sqlite3.connect(self.path)
        self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE bars (topic TEXT, start INTEGER, row_json TEXT)")
        install(self.db)
        self.names=[topic("BTCUSDT","15m"),topic("ETHUSDT","1h")]
        begin(self.db,self.names,"e"*32)
        self.db.commit()

    def test_monotonic_and_changed_only(self):
        self.db.execute("INSERT INTO bars VALUES(?,?,?)",(self.names[0],0,"[]"))
        bump(self.db,self.names[0],"e"*32,100)
        self.db.commit()
        r=IncrementalReader(self.path)
        with self.assertRaisesRegex(ValueError,"INVALID_OR_EXPIRED"):
            r.poll(100)
        bump(self.db,self.names[1],"e"*32,100)
        self.db.commit()
        a=r.poll(100)
        self.assertEqual(a["changed_topics"],2)
        self.assertEqual(r.poll(100)["changed_topics"],0)
        bump(self.db,self.names[0],"e"*32,110)
        self.db.commit()
        b=r.poll(110)
        self.assertEqual(b["changed_topics"],1)
        self.assertEqual(b["state"][key(self.names[0])]["revision"],2)

    def test_revision_race_refuses_old_state(self):
        for n in self.names: bump(self.db,n,"e"*32,100)
        self.db.commit()
        r=IncrementalReader(self.path)
        def mutate():
            bump(self.db,self.names[1],"e"*32,101)
            self.db.commit()
        with self.assertRaisesRegex(ValueError,"REVISION_RACE"):
            r.poll(100,before_recheck=mutate)
        self.assertEqual(r.poll(101)["changed_topics"],2)

    def test_epoch_reset_refuses_stale_reader(self):
        for n in self.names: bump(self.db,n,"e"*32,100)
        self.db.commit()
        r=IncrementalReader(self.path)
        self.assertEqual(r.poll(100)["changed_topics"],2)
        begin(self.db,self.names,"f"*32)
        self.db.commit()
        with self.assertRaisesRegex(ValueError,"INVALID_OR_EXPIRED"):
            r.poll(100)

    def test_quarantine_refusal(self):
        for n in self.names: bump(self.db,n,"e"*32,100)
        self.db.commit()
        r=IncrementalReader(self.path)
        r.poll(100)
        bump(self.db,self.names[0],"e"*32,101,status="invalid",valid_until_ms=0)
        self.db.commit()
        with self.assertRaisesRegex(ValueError,"INVALID_OR_EXPIRED"):
            r.poll(101)

    def test_spot_future_and_unicode_are_not_aliased(self):
        unicode_name=topic("比特币USDT","15m")
        self.assertEqual(key(unicode_name)[2],"比特币USDT")
        with self.assertRaises(ValueError): bump(self.db,self.names[0],"z"*32,100)
        with self.assertRaises(ValueError): bump(self.db,"btcusdt@kline_15m","f"*32,100)
        for name in self.names: bump(self.db,name,"e"*32,100)
        self.db.execute("""INSERT INTO topic_state VALUES
            ('BINANCE','futures','BTCUSDT','15m',?,?,1,100,100,'valid',1000)""",
            ("e"*32,"e"*32))
        self.db.commit()
        result=IncrementalReader(self.path).poll(100)
        self.assertEqual(result["topic_count"],2)
        self.assertNotIn(("BINANCE","futures","BTCUSDT","15m"),result["state"])


    def test_all_1228_topics_chunked_without_cross_market(self):
        # Validate the exact 400-variable split and changed-only scope.
        from research.hunter_binance_stream_store import topic
        names=[topic("BTCUSDT","5m")]+[
            topic(symbol,interval)
            for symbol in (["BTCUSDT"]+["S%03dUSDT"%i for i in range(408)])
            for interval in ("15m","1h","1d")]
        self.assertEqual(len(names),1228)
        begin(self.db,names,"f"*32)
        self.db.executemany("INSERT INTO bars VALUES(?,?,?)",
                            [(n,0,"[]") for n in names])
        for n in names: bump(self.db,n,"f"*32,100)
        self.db.commit()
        reader=IncrementalReader(self.path)
        first=reader.poll(100)
        self.assertEqual((first["topic_count"],first["changed_topics"]),(1228,1228))
        self.assertEqual(sum(map(len,first["changed"].values())),1228)
        self.assertEqual(reader.poll(100)["changed_topics"],0)
        changed=names[700]
        bump(self.db,changed,"f"*32,105)
        self.db.commit()
        result=reader.poll(105)
        self.assertEqual(result["changed_topics"],1)
        self.assertEqual(list(result["changed"]),[key(changed)])


if __name__=="__main__":
    unittest.main()
