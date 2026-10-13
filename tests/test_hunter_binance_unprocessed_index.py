"""Same-scope pending receipt oracle, scratch SQLite only."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from research.hunter_binance_health_store import Store
from research.hunter_binance_stream_store import topic


class UnprocessedIndexTests(unittest.TestCase):
    def setUp(self):
        scratch=tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.db_path=Path(scratch.name)/'state.sqlite3'
        self.store=Store(self.db_path)
        self.store.enable_health()

    def test_active_partial_index_exists_and_is_selected(self):
        with self.store.db(readonly=True) as db:
            ix=[dict(row) for row in db.execute("PRAGMA index_list('bn_unprocessed')")]
            self.assertTrue(any(x['name']=='bn_unprocessed_open_topic' and x['partial'] for x in ix))
            plan=' '.join(str(row[3]) for row in db.execute(
                'EXPLAIN QUERY PLAN SELECT DISTINCT topic FROM bn_unprocessed '
                'WHERE resolved_at IS NULL AND topic IN (?,?)',
                ('btcusdt@kline_15m','ethusdt@kline_1h')))
        self.assertIn('bn_unprocessed_open_topic',plan)

    def test_legacy_global_intersection_matches_indexed_scope(self):
        names=[topic('BTCUSDT','15m'),topic('ETHUSDT','1h')]
        other=topic('XRPUSDT','5m')
        with self.store.db() as db:
            db.executemany('INSERT INTO bn_unprocessed(epoch,topic,received_at,open_time,event_time,'
                'payload_sha256,reason,category,resolved_at) VALUES(?,?,?,?,?,?,?,?,?)',[
                    ('e',names[0],1,2,3,'a','TEST','unprocessed_recorded',None),
                    ('e',names[1],1,2,3,'a','TEST','unprocessed_recorded',8),
                    ('e',names[1],1,2,3,'a','TEST','unprocessed_recorded',None),
                    ('e',other,1,2,3,'a','TEST','unprocessed_recorded',None)])
        with self.store.db(readonly=True) as db:
            legacy={row[0] for row in db.execute(
                'SELECT DISTINCT topic FROM bn_unprocessed WHERE resolved_at IS NULL')}
            marks=','.join('?' for _ in names)
            current={row[0] for row in db.execute(
                'SELECT DISTINCT topic FROM bn_unprocessed WHERE resolved_at IS NULL '
                'AND topic IN ('+marks+')',names)}
            self.assertEqual(legacy & set(names),current)
            self.assertNotIn(other,current)
            self.assertEqual(current,set(names))
        with self.store.db() as db:
            db.execute('UPDATE bn_unprocessed SET resolved_at=9 WHERE topic=?',(names[1],))
        with self.store.db(readonly=True) as db:
            active={row[0] for row in db.execute(
                'SELECT DISTINCT topic FROM bn_unprocessed WHERE resolved_at IS NULL '
                'AND topic IN (?,?)',names)}
        self.assertEqual(active,{names[0]})

    def test_query_covers_400_topic_chunk_boundaries(self):
        names=[topic('S%03dUSDT'%i,'15m') for i in range(815)]
        with self.store.db() as db:
            db.executemany(
                'INSERT INTO bn_unprocessed(epoch,topic,received_at,open_time,event_time,'
                'payload_sha256,reason,category) VALUES(?,?,?,?,?,?,?,?)',
                [('e',n,1,2,3,'a','TEST','unprocessed_recorded') for i,n in enumerate(names) if i%41==0])
        found=set()
        with self.store.db(readonly=True) as db:
            baseline=set(r[0] for r in db.execute(
                'SELECT DISTINCT topic FROM bn_unprocessed WHERE resolved_at IS NULL'))
            for offset in range(0,len(names),400):
                group=names[offset:offset+400]
                marks=','.join('?' for _ in group)
                found.update(r[0] for r in db.execute(
                    'SELECT DISTINCT topic FROM bn_unprocessed WHERE resolved_at IS NULL '
                    'AND topic IN ('+marks+')',group))
        self.assertEqual(found,baseline & set(names))


if __name__=='__main__':
    unittest.main()
