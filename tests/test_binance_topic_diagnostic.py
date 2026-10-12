"""Prove the diagnostic never opens the source WAL database with SQLite."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

from research.hunter_binance_stream_store import Store,roster,content_hash


class DiagnosticTests(unittest.TestCase):
    def test_wal_snapshot_maps_topics_without_changing_source_or_owners(self):
        shell=(Path(__file__).resolve().parents[1]/'deploy/binance-topic-shards-readonly.sh').read_text()
        source=shell.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
        diagnostic=types.ModuleType('readonly_fixture');exec(compile(source,'<diagnostic>','exec'),diagnostic.__dict__)
        stamp=dt.datetime(2026,10,12,tzinfo=dt.timezone.utc)
        symbols=['BTC']+['X'+str(i) for i in range(408)]
        scan=dict(as_of_utc=stamp.isoformat(),binance_complete=True,generation_id='fixture',
            coins={s:dict(pairs=[dict(venue='binance',base=s,pair=s+'USDT')]) for s in symbols})
        names=roster(scan,stamp)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'manifest.json').write_text(json.dumps(scan))
            store=Store(root/'state.sqlite3');store.begin_session(names,content_hash(scan))
            writer=sqlite3.connect(root/'state.sqlite3')
            try:
                writer.execute('PRAGMA journal_mode=WAL')
                for name in names[145:]:
                    writer.execute('UPDATE topics SET ws_rx=?,ws_ts=? WHERE topic=?',(1791825643310,1791825643192,name))
                for index in range(3):
                    token=f'{index:032x}'
                    writer.execute('INSERT INTO requests(token) VALUES(?)',(token,))
                    writer.execute('INSERT INTO leases(token,acquired_ms) VALUES(?,?)',(token,index))
                    (root/('request-'+token+'.lock')).write_bytes(b'unresolved fixture')
                writer.commit()
                before={p.name:p.read_bytes() for p in root.iterdir()};opened=[];connect=sqlite3.connect
                self.assertGreater(len(before['state.sqlite3-wal']),0)
                def recorded(path,*args,**kwargs):
                    self.assertNotIn(root.as_posix(),str(path));opened.append(str(path))
                    return connect(path,*args,**kwargs)
                with patch.object(diagnostic.sqlite3,'connect',recorded):report=diagnostic.inspect(root)
                self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})
                self.assertEqual(len(opened),1);self.assertIn('binance-readonly-snapshot-',opened[0])
                self.assertEqual((report['topics'],report['no_frame']),(1228,145))
                self.assertEqual(report['owners'],dict(requests=3,leases=3))
                self.assertEqual([s['total'] for s in report['shards']],[500,500,228])
                affected={r['topic']:r for g in report['affected_symbols'] for r in g['intervals']}
                for index,name in enumerate(names[:145]):
                    self.assertEqual((affected[name]['ordinal'],affected[name]['shard']),(index,index//500))
                self.assertEqual(report['ws_10s_buckets'],[dict(rx_ms=0,ts_ms=0,n=145),
                    dict(rx_ms=1791825640000,ts_ms=1791825640000,n=1083)])
                (root/'manifest.json').write_text(json.dumps(dict(scan,generation_id='mismatch')))
                with self.assertRaisesRegex(ValueError,'SNAPSHOT_MANIFEST_OR_EPOCH_MISMATCH'):
                    diagnostic.inspect(root)
                print('DIAGNOSTIC_WAL_SOURCE_UNCHANGED',dict(topics=1228,no_frame=145,requests=3,leases=3))
            finally:writer.close()


if __name__=='__main__':unittest.main()
