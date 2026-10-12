"""Prove the diagnostic never opens the source WAL database with SQLite."""
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

from research.hunter_binance_stream_store import Store,roster,content_hash

REPO=Path(__file__).resolve().parents[1]
DEPLOYED='99836e5362340725f806d5df6df1ca9835065bdd'
BROKEN_DIAGNOSTIC='76927046867e59e2e48f588f53a234ff4ac4351e'


def diagnostic_module(shell):
    source=shell.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
    module=types.ModuleType('readonly_fixture');exec(compile(source,'<diagnostic>','exec'),module.__dict__)
    return module


class DiagnosticTests(unittest.TestCase):
    def test_wal_snapshot_maps_topics_without_changing_source_or_owners(self):
        stamp=dt.datetime(2026,10,12,tzinfo=dt.timezone.utc)
        symbols=['BTC']+['X'+str(i) for i in range(408)]
        scan=dict(as_of_utc=stamp.isoformat(),binance_complete=True,generation_id='fixture',
            coins={s:dict(pairs=[dict(venue='binance',base=s,pair=s+'USDT')]) for s in symbols})
        names=roster(scan,stamp)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'manifest.json').write_text(json.dumps(scan))
            store=Store(root/'state.sqlite3');store.begin_session(names,content_hash(scan))
            self.assert_snapshot(root,scan,names)

    @unittest.skipUnless(sys.platform=='linux','Exact deployed prepare requires Linux flock and directory fsync')
    def test_exact_deployed_prepare_artifact_with_captured_unicode_symbols(self):
        files=['scripts/hunter_binance_stream_ops.py']+['research/'+name+'.py' for name in (
            'hunter_binance_stream','hunter_binance_stream_store','hunter_binance_public',
            'hunter_binance_http_child','hunter_bybit_public','hunter_bybit_http_child',
            'hunter_bybit_stream_store')]+['research/results/hunter-cex-universe-latest.json']
        # Raw, pinned Git blobs: neither a newly invented manifest nor today's code.
        archive=subprocess.check_output(['git','-c','core.autocrlf=false','archive',DEPLOYED,*files],cwd=REPO)
        with tempfile.TemporaryDirectory() as folder:
            fixture=Path(folder);root=fixture/'state';root.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                for member in tar:
                    if member.isdir():continue
                    self.assertIn(member.name,files);self.assertTrue(member.isfile())
                    target=fixture/member.name;target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())
            # Run the actual old main()/prepare()/atomic_json() and Store. Only
            # the state destination and wall clock are redirected to the fixture.
            result=subprocess.run([sys.executable,'-E','-s','-B','-c',r'''
import contextlib,datetime as dt,io,json,sys,types
from pathlib import Path
from unittest.mock import patch
from scripts import hunter_binance_stream_ops as ops
snapshot=Path('research/results/hunter-cex-universe-latest.json')
scan=json.loads(snapshot.read_bytes());stamp=dt.datetime.fromisoformat(scan['as_of_utc'])
class CapturedTime(dt.datetime):
    @classmethod
    def now(cls,tz=None):return stamp
state=Path('state/state.sqlite3').resolve();output=io.StringIO()
with patch.object(ops,'STATE_PATH',state),patch.object(ops,'dt',types.SimpleNamespace(datetime=CapturedTime,timezone=dt.timezone)),patch.object(sys,'argv',['ops','prepare','--snapshot',str(snapshot),'--source-head',scan['source_head_sha']]),contextlib.redirect_stdout(output):
    assert ops.main()==0
report=json.loads(output.getvalue());assert report['status']=='PREPARED_NOT_STARTED'
receipt=report['receipt'];names=receipt['topics']
assert json.loads((state.parent/'manifest.json').read_bytes())==scan
assert json.loads((state.parent/'manifests'/(receipt['receipt_sha256']+'.json')).read_bytes())==dict(receipt=receipt,snapshot=scan)
ops.Store(state).begin_session(names,ops.content_hash(scan))
print(json.dumps(dict(topics=names,receipt_sha256=receipt['receipt_sha256'])))
'''],cwd=fixture,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            prepared=json.loads(result.stdout);names=prepared['topics']
            raw=(root/'manifest.json').read_bytes();scan=json.loads(raw)
            self.assertEqual(len(names),1228)
            self.assertIn('\u5e01\u5b89\u4eba\u751fusdt@kline_15m',names)
            self.assertIn('\u725b\u6765usdt@kline_1h',names)
            old=diagnostic_module(subprocess.check_output(['git','show',BROKEN_DIAGNOSTIC+':deploy/binance-topic-shards-readonly.sh'],cwd=REPO).decode())
            with self.assertRaisesRegex(ValueError,'MANIFEST_SYMBOLS_INVALID'):old.inspect(root)
            self.assert_snapshot(root,scan,names)
            print('EXACT_DEPLOYED_PREPARE_ARTIFACT_OK',DEPLOYED,'manifest_sha256='+hashlib.sha256(raw).hexdigest(),
                  'topics=1228 unicode_symbols=2 prior_diagnostic=MANIFEST_SYMBOLS_INVALID')

    def assert_snapshot(self,root,scan,names):
        diagnostic=diagnostic_module((REPO/'deploy/binance-topic-shards-readonly.sh').read_text())
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
            before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()};opened=[];connect=sqlite3.connect
            self.assertGreater(len(before['state.sqlite3-wal']),0)
            def recorded(path,*args,**kwargs):
                self.assertNotIn(root.as_posix(),str(path));opened.append(str(path))
                return connect(path,*args,**kwargs)
            with patch.object(diagnostic.sqlite3,'connect',recorded):report=diagnostic.inspect(root)
            self.assertEqual(before,{p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()})
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
