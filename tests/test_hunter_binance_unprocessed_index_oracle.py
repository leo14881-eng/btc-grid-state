"""Offline unprocessed-recipient index oracle, strictly scratch SQLite.

No runtime schema changes. Measure indexed vs existing full scan on one exact
seed and compare returned topics/denials, including resolved and duplicate rows.
"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path


class ActiveReceiptIndexOracle(unittest.TestCase):
    def test_identical_unresolved_topics_and_planner(self):
        with tempfile.TemporaryDirectory(prefix='bn-index-') as directory:
            path=Path(directory)/'scratch.sqlite3'
            db=sqlite3.connect(path)
            try:
                db.execute('CREATE TABLE bn_unprocessed(id INTEGER PRIMARY KEY,'
                           'epoch TEXT, topic TEXT, resolved_at INTEGER)')
                names=['x%04dusdt@kline_15m'%i for i in range(1228)]
                rows=[]
                for i,name in enumerate(names):
                    for j in range(35):
                        rows.append(('a'*32,name,None if (i+j)%9==0 else 1000+j))
                db.executemany('INSERT INTO bn_unprocessed(epoch,topic,resolved_at) VALUES(?,?,?)',rows)
                db.commit()
                sql='SELECT DISTINCT topic FROM bn_unprocessed WHERE resolved_at IS NULL'
                topic_sql='SELECT 1 FROM bn_unprocessed WHERE topic=? AND resolved_at IS NULL LIMIT 1'
                before={r[0] for r in db.execute(sql)}
                before_by_topic={n:bool(db.execute(topic_sql,(n,)).fetchone()) for n in names}
                start=time.perf_counter_ns()
                for _ in range(3):list(db.execute(sql))
                baseline_us=(time.perf_counter_ns()-start)//3000
                db.execute('CREATE INDEX bn_unprocessed_active_topic '
                           'ON bn_unprocessed(topic) WHERE resolved_at IS NULL')
                db.commit()
                after={r[0] for r in db.execute(sql)}
                after_by_topic={n:bool(db.execute(topic_sql,(n,)).fetchone()) for n in names}
                self.assertEqual(before,after)
                self.assertEqual(before_by_topic,after_by_topic)
                self.assertEqual(len(rows),42980)
                plan=' '.join(r[3] for r in db.execute('EXPLAIN QUERY PLAN '+sql))
                plan_one=' '.join(r[3] for r in db.execute(
                    'EXPLAIN QUERY PLAN '+topic_sql,(names[0],)))
                self.assertIn('bn_unprocessed_active_topic',plan)
                self.assertIn('bn_unprocessed_active_topic',plan_one)
                start=time.perf_counter_ns()
                for _ in range(3):list(db.execute(sql))
                indexed_us=(time.perf_counter_ns()-start)//3000
                print('BINANCE_ACTIVE_INDEX_SCRATCH',dict(
                    rows=len(rows),topics=len(names),unresolved_topics=len(before),
                    before_us=baseline_us,indexed_us=indexed_us,
                    plan=plan,point_plan=plan_one),flush=True)
            finally:
                db.close()


if __name__=='__main__':unittest.main()
