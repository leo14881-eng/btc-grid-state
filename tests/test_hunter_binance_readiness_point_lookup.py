"""Scratch SQLite oracle: indexed per-topic readiness preserves legacy windows."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from research.hunter_binance_stream_store import Store, topic


class ReadinessPointLookupTests(unittest.TestCase):
    @staticmethod
    def seed(store, count=410):
        names=[topic('S%03dUSDT' % i,'15m') for i in range(count)]
        with store.db() as db:
            values=[]
            for i,name in enumerate(names):
                # Same observed timestamp with distinct id, future excluded,
                # and missing topics. All transactions commit before the snapshot.
                if i % 17 == 0:
                    continue
                values.extend([
                    (name,100,'epoch',1,99,99,0),
                    (name,200,'epoch',1,199,199,0),
                    (name,200,'epoch',0,200,200,1),
                    (name,300,'epoch',1,300,300,0),
                    (name,400,'epoch',1,400,400,0)
                ])
            db.executemany('''INSERT INTO readiness
                (topic,observed,epoch,ack,ws_rx,ws_ts,gap)
                VALUES(?,?,?,?,?,?,?)''',values)
        return names

    @staticmethod
    def legacy(store,names,cut):
        expected={}
        with store.db(readonly=True) as db:
            for offset in range(0,len(names),400):
                group=names[offset:offset+400]
                marks=','.join('?' for _ in group)
                query=('SELECT * FROM (SELECT *,ROW_NUMBER() OVER '
                       '(PARTITION BY topic ORDER BY observed DESC,id DESC) AS chosen '
                       'FROM readiness WHERE observed<=? AND topic IN ('+marks+')) '
                       'WHERE chosen=1')
                expected.update({r['topic']:dict(r) for r in db.execute(query,(cut,*group))})
        return expected

    def test_full_internal_snapshot_matches_legacy_for_chunked_scope_and_cuts(self):
        for cut in (99,100,199,200,201,299,300,400,401):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as root:
                store=Store(Path(root)/'state.sqlite3')
                names=self.seed(store)
                expected=self.legacy(store,names,cut)
                actual=store._freeze_snapshot(cut,names)[2]
                self.assertEqual(actual,expected)
                if cut==200:
                    name=names[1]
                    self.assertEqual(actual[name]['observed'],200)
                    self.assertEqual(actual[name]['gap'],1)
                    self.assertEqual(actual[name]['chosen'],1)

    def test_readiness_planner_uses_existing_topic_observed_index(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/'state.sqlite3')
            self.seed(store,4)
            query=('EXPLAIN QUERY PLAN WITH requested(topic) AS (VALUES (?),(?)) '
                   'SELECT r.*,1 AS chosen FROM requested JOIN readiness r ON r.id=('
                   'SELECT prior.id FROM readiness prior WHERE prior.topic=requested.topic '
                   'AND prior.observed<=? ORDER BY prior.observed DESC,prior.id DESC LIMIT 1)')
            with store.db(readonly=True) as db:
                plan=' '.join(str(row[3]) for row in db.execute(query,('s001usdt@kline_15m',
                                                            's002usdt@kline_15m',200)))
            self.assertIn('readiness_topic',plan)

    def test_no_extra_writer_or_journal_mutation_during_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/'state.sqlite3')
            names=self.seed(store,3)
            with closing(sqlite3.connect(store.path)) as db:
                before=db.execute('PRAGMA journal_mode').fetchone()[0]
                changes=db.total_changes
            store._freeze_snapshot(200,names)
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],before)
                self.assertEqual(db.total_changes,changes)


if __name__ == '__main__':
    unittest.main()
