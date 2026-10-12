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
    def test_one_report_redacts_logs_owners_and_saves_bounded_summary(self):
        diagnostic=diagnostic_module((REPO/'deploy/binance-topic-shards-readonly.sh').read_text())
        secret='SENSITIVE_FAKE_TOKEN_012345';contact='private@example.invalid';calls=[]
        journal=[dict(__REALTIME_TIMESTAMP='1791825658000000',MESSAGE=json.dumps(dict(
            event='BINANCE_STREAM_RECOVERY',error_code='BINANCE_TOPIC_STALE',error_type='TimeoutError',failures=1,token=secret,contact=contact))),
            dict(__REALTIME_TIMESTAMP='1791825663000000',MESSAGE='RuntimeError: ORPHANED_HTTP_REQUEST_MANUAL_REVIEW'),
            dict(__REALTIME_TIMESTAMP='1791825664000000',MESSAGE='unrecognized '+secret+' '+contact)]
        def run(args,**kwargs):
            calls.append((args,kwargs))
            if args[0]=='systemctl':return b'ActiveState=failed\nSubState=failed\nMainPID=0\nControlGroup=\n'
            if args[0]=='journalctl':return b'\n'.join(json.dumps(r).encode() for r in journal)
            if args[0]=='runuser':
                self.assertEqual(kwargs['source'],b'verified fixture program')
                return json.dumps(dict(topics=1228,no_frame=145,owners=dict(requests=3,leases=3),shards=[])).encode()
            self.fail('Unexpected command')
        report=diagnostic.collect_report(b'verified fixture program',run=run,release_reader=lambda:dict(release_head=DEPLOYED))
        self.assertEqual(report['status'],'COMPLETE')
        self.assertEqual([args[0] for args,kwargs in calls],['systemctl','journalctl','runuser','systemctl'])
        self.assertEqual(report['historical_processing_evidence']['queue_depth'],'NOT_RECORDED')
        serialized=json.dumps(report);self.assertNotIn(secret,serialized);self.assertNotIn(contact,serialized)
        self.assertIn('ORPHANED_HTTP_REQUEST_MANUAL_REVIEW',serialized)
        self.assertEqual(report['incident_journal']['records'][0]['evidence']['error_type'],'TimeoutError')
        with tempfile.TemporaryDirectory(dir=REPO) as folder:
            base=Path(folder)/'report';short,checksum=diagnostic.save_report(report,base)
            full=Path(str(base)+'.report.json').read_bytes()
            self.assertEqual(checksum,hashlib.sha256(full).hexdigest())
            self.assertEqual(json.loads(full),report)
            self.assertLess(len(short.splitlines()),20)
            self.assertEqual(Path(str(base)+'.summary.txt').read_text(),short)
            if sys.platform=='linux':self.assertEqual(Path(str(base)+'.report.json').stat().st_mode&0o777,0o600)

    def test_partial_report_keeps_other_evidence_without_reading_live_cache(self):
        diagnostic=diagnostic_module((REPO/'deploy/binance-topic-shards-readonly.sh').read_text())
        calls=[]
        def run(args,**kwargs):
            calls.append(args[0])
            if args[0]=='systemctl':return b'ActiveState=active\nSubState=running\nMainPID=42\nControlGroup=\n'
            if args[0]=='journalctl':return b''
            self.fail('Active service must prevent cache child')
        def failed_release():raise PermissionError('private@example.invalid')
        report=diagnostic.collect_report(b'fixture',run=run,release_reader=failed_release)
        self.assertEqual(report['status'],'PARTIAL');self.assertNotIn('cache',report)
        self.assertNotIn('runuser',calls);self.assertNotIn('private@example.invalid',json.dumps(report))
        self.assertIn('incident_journal',report)
        self.assertIn('STOPPED_SERVICE_REQUIRED',{e['code'] for e in report['errors']})

    def test_cache_error_remains_a_saved_partial_report(self):
        diagnostic=diagnostic_module((REPO/'deploy/binance-topic-shards-readonly.sh').read_text())
        def run(args,**kwargs):
            if args[0]=='systemctl':return b'ActiveState=failed\nSubState=failed\nMainPID=0\nControlGroup=\n'
            if args[0]=='journalctl':return b''
            return b'{"diagnostic_error":{"error_type":"ValueError","code":"SOURCE_CHANGED_DURING_CAPTURE"}}'
        report=diagnostic.collect_report(b'fixture',run=run,release_reader=lambda:{})
        self.assertEqual(report['status'],'PARTIAL')
        self.assertIn('SOURCE_CHANGED_DURING_CAPTURE',{e['code'] for e in report['errors']})

    def test_rest_only_zero_frame_topic_is_bound_to_current_epoch(self):
        diagnostic=diagnostic_module((REPO/'deploy/binance-topic-shards-readonly.sh').read_text())
        stamp=dt.datetime(2026,10,12,tzinfo=dt.timezone.utc)
        scan=dict(as_of_utc=stamp.isoformat(),binance_complete=True,generation_id='fixture',
            coins=dict(BTC=dict(pairs=[dict(venue='binance',base='BTC',pair='BTCUSDT')])))
        names=roster(scan,stamp)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'manifest.json').write_text(json.dumps(scan))
            store=Store(root/'state.sqlite3');epoch=store.begin_session(names,content_hash(scan))
            with store.db() as db:
                for name,session in ((names[0],epoch),(names[1],'a'*32)):
                    db.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',(name,0,'[]',1000,999,1,'OFFICIAL_BINANCE_SPOT_REST',session))
            report=diagnostic.inspect(root)
            self.assertEqual((report['no_frame'],report['no_frame_with_current_rest_bars']),(4,1))
            states={r['topic']:r for r in report['topic_receipts']}
            self.assertEqual(states[names[0]]['current_epoch_bars_by_origin'][0]['rows'],1)
            self.assertEqual(states[names[1]]['current_epoch_bars_by_origin'],[])
            self.assertEqual({r['session']:r['rows'] for r in report['cache_counts_by_session']['bars']},{'current':1,'other':1})

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
import contextlib,datetime as dt,io,json,queue,sys,time,types
from pathlib import Path
from threading import Event,Thread
from unittest.mock import patch
from scripts import hunter_binance_stream_ops as ops
from research import hunter_binance_stream as stream
from research.hunter_binance_stream_store import STEPS,LIMITS,split_topic
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
store=ops.Store(state);now=int(stamp.timestamp()*1000)
protocol=stream.Protocol(store,names,ops.content_hash(scan),now)
for shard,requests in enumerate(protocol.requests):
    for request in requests:
        protocol.mark_sent(shard,request['id'],now)
        protocol.receive(shard,dict(id=request['id'],result=None),now)
def message(name,event):
    symbol,interval=split_topic(name);step=STEPS[interval];start=event//step*step
    return dict(e='kline',E=event,s=symbol,k=dict(i=interval,s=symbol,t=start,T=start+step-1,
        o='100',h='102',l='99',c='101',v='10',q='1000',n=1,V='5',Q='500',x=False))
pending=queue.Queue(maxsize=4096)
for index,name in enumerate(names):pending.put((index//500,message(name,now+1000),now+1001))
assert pending.qsize()==1228 and sum(s['ws_rx']>0 for s in store.states())==0
entered=Event();release=Event();errors=[]
class HeldClient:
    def request(self,path,params,**kwargs):
        entered.set();assert release.wait(10)
        step=STEPS[params['interval']];cut=params['endTime']
        return [[start,'100','102','99','101','10',start+step-1,'1000',1,'5','500','0']
            for start in sorted((cut//step-i)*step for i in range(params['limit']))]
def background_repair():
    try:stream.repair(store,protocol.epoch,names[-1],now+900000,clock=lambda:now+2000,client=HeldClient())
    except BaseException as error:errors.append(error)
worker=Thread(target=background_repair);worker.start()
try:
    assert entered.wait(3)
    started=time.perf_counter()
    while pending.qsize()>1:
        protocol.receive_batch([pending.get_nowait() for _ in range(min(100,pending.qsize()-1))])
    commit_seconds=time.perf_counter()-started
    assert worker.is_alive() and sum(s['ws_rx']>0 for s in store.states())==1227
finally:release.set();worker.join(10)
assert not worker.is_alive() and not errors,errors
last=next(s for s in store.states() if s['topic']==names[-1]);assert last['ws_rx']==0
with store.db(readonly=True) as db:
    assert db.execute('SELECT COUNT(*) FROM bars WHERE topic=?',(names[-1],)).fetchone()[0]>0
# Receipt is retained even when processing occurs much later. It is not made
# fresh by a commit-time timestamp, nor rejected solely for time in the queue.
with patch.object(time,'time',return_value=(now+100000)/1000),patch.object(time,'time_ns',return_value=(now+100000)*1000000):
    protocol.receive_batch([pending.get_nowait()])
assert all(s['ws_rx']==now+1001 for s in store.states())
protocol=stream.Protocol(store,names,ops.content_hash(scan),now)
store.ack(protocol.epoch,names)
batch=[(index//500,message(name,now+3000),now+3001) for index,name in enumerate(names[:100])]
batch[-1][1]['e']='invalid'
try:protocol.receive_batch(batch)
except ValueError as error:assert str(error)=='BINANCE_WS_ENVELOPE_INVALID'
else:raise AssertionError('invalid batch accepted')
assert all(s['ws_rx']==0 for s in store.states())
store.begin_session(names,ops.content_hash(scan))
print(json.dumps(dict(topics=names,receipt_sha256=receipt['receipt_sha256'],flow=dict(
    queued_before_commit=1228,persisted_while_http_waits=1227,rest_only_ws_rx=0,
    delayed_commit_keeps_original_receipt=True,invalid_batch_rolls_back_all_100=True,
    fixture_commit_seconds=commit_seconds,benchmark_scope='CI temporary database only; not server throughput'))))
'''],cwd=fixture,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            prepared=json.loads(result.stdout);names=prepared['topics']
            self.assertEqual(prepared['flow']['persisted_while_http_waits'],1227)
            self.assertEqual(prepared['flow']['rest_only_ws_rx'],0)
            self.assertTrue(prepared['flow']['invalid_batch_rolls_back_all_100'])
            print('EXACT_DEPLOYED_DATA_FLOW',json.dumps(prepared['flow'],sort_keys=True))
            raw=(root/'manifest.json').read_bytes();scan=json.loads(raw)
            self.assertEqual(len(names),1228)
            self.assertIn('\u5e01\u5b89\u4eba\u751fusdt@kline_15m',names)
            self.assertIn('\u725b\u6765usdt@kline_1h',names)
            old=diagnostic_module(subprocess.check_output(['git','show',BROKEN_DIAGNOSTIC+':deploy/binance-topic-shards-readonly.sh'],cwd=REPO).decode())
            with self.assertRaisesRegex(ValueError,'MANIFEST_SYMBOLS_INVALID'):old.inspect(root)
            report=self.assert_snapshot(root,scan,names)
            self.assertTrue(all(group['session']=='other' for group in report['cache_counts_by_session']['bars']))
            self.assertEqual(report['current_epoch_cache_by_origin']['bars'],[])
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
            self.assertEqual(len(report['owner_details']),3)
            serialized=json.dumps(report)
            for index in range(3):self.assertNotIn(f'{index:032x}',serialized)
            self.assertEqual(len(report['topic_receipts']),1228)
            self.assertEqual(len(report['subscription_batches']),13)
            self.assertTrue(all(row['http_outcome']=='UNKNOWN' and row['lock']['exists'] for row in report['owner_details']))
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
            return report
        finally:writer.close()


if __name__=='__main__':unittest.main()
