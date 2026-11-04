"""Real collector loop/pumps/pool/SQLite with deterministic public peers and REST."""
import contextlib
import json
from pathlib import Path
import queue
import tempfile
from threading import Event,Lock,Thread
import time
import unittest
from unittest.mock import patch

from research import hunter_binance_health as health
from research import hunter_binance_stream as stream
from research.hunter_binance_health_store import Store,valid_proof
from research.hunter_binance_stream_store import topic,split_topic,STEPS
from scripts.hunter_binance_health_check import read_sample,Window
from test_hunter_binance_health import NOW,HEAD,rows,message,directory


class Fixture:
    def __init__(self,path,names,*,until=350,quiet=None,pongs=True,status='TRADING'):
        self.names=names;self.elapsed=0;self.until=until;self.quiet=set(quiet or [])
        self.pongs=pongs;self.status=status;self.lock=Lock();self.stop=Event();self.peers=[]
        self.events=[];self.samples=[];self.requests=[];self.processed=0;self.sent=0;self.last={}
        self.active=0;self.started=time.monotonic();self.close_errors=False;self.fail_quiet=False
        self.proofs=[]
        self.switch_after_mismatch=False;self.changed_closed=False
        fixture=self
        class RecordingStore(Store):
            def ingest_accounted(inner,epoch,observations):
                result=super().ingest_accounted(epoch,observations);fixture.processed+=len(observations);return result
        self.store=RecordingStore(path)
        class ClockQueue(stream.ObservedQueue):
            def get(inner,*args,**kwargs):
                if kwargs.get('timeout')==.1:
                    fixture.tick()
                    return super().get(block=False)
                return super().get(*args,**kwargs)
        self.queue=ClockQueue
        class Client:
            def __init__(inner,store):pass
            def request(inner,path,params,**kwargs):
                if kwargs['cancelled']():raise TimeoutError('CANCELLED')
                fixture.requests.append((fixture.clock(),path,dict(params)))
                if path.endswith('exchangeInfo'):
                    return directory(json.loads(params['symbols']),fixture.status)
                if fixture.fail_quiet and len(fixture.requests)>len(names)+1:raise TimeoutError('FIXTURE_HTTP_TIMEOUT')
                value=rows(params['interval'],params['endTime'])
                if fixture.switch_after_mismatch and params['endTime']>=NOW+90000:
                    value[-2][4]='102';fixture.changed_closed=True
                    fixture.quiet.clear()  # Subsequent WS stays fresh; reverify must still run.
                return value
        self.client=Client

    def clock(self):return NOW+int(self.elapsed*1000)
    def steady(self):return self.elapsed
    def sleep(self,_):time.sleep(.001)

    @contextlib.contextmanager
    def connect(self):
        fixture=self
        class Peer:
            def __init__(inner):inner.queue=queue.Queue();inner.topics=[];inner.closed=False;inner.pings=0
            def send(inner,value):
                request=json.loads(value);inner.topics.extend(request['params'])
                inner.queue.put(json.dumps(dict(id=request['id'],result=None)))
            def ping(inner,ack_on_close):
                assert ack_on_close is False
                inner.pings+=1;result=Event()
                if fixture.pongs:result.set()
                return result
            def recv(inner,timeout):
                try:return inner.queue.get(timeout=.002)
                except queue.Empty:raise TimeoutError()
        peer=Peer();self.peers.append(peer)
        try:yield peer
        finally:
            peer.closed=True
            if self.close_errors:raise OSError('FIXTURE_CLOSE_ERROR')

    def tick(self):
        if time.monotonic()-self.started>30:raise AssertionError('FIXTURE_WALL_TIMEOUT')
        time.sleep(.002)
        if sum(len(p.topics) for p in self.peers)!=len(self.names):return
        if any(not s['ack'] for s in self.store.states()):return
        if self.processed<self.sent or any(not p.queue.empty() for p in self.peers):return
        self.elapsed+=2
        if self.elapsed>=self.until:self.stop.set();return
        if self.elapsed%20:return
        for peer in self.peers:
            for name in peer.topics:
                if name in self.quiet:continue
                symbol,interval=split_topic(name);now=self.clock();step=STEPS[interval]
                start=now//step*step;prior=self.last.get(name,start)
                if start>prior:
                    peer.queue.put(json.dumps(message(symbol,interval,now-1,start=prior,closed=True)));self.sent+=1
                peer.queue.put(json.dumps(message(symbol,interval,now)));self.sent+=1;self.last[name]=start

    def run(self,**kwargs):
        def observe(value):
            self.samples.append(value)
            self.proofs.extend(v for n in self.names if (v:=self.store.get_proof('quiet',n)))
        with patch.object(stream,'ObservedQueue',self.queue),patch.object(stream,'emit',side_effect=self.events.append):
            health.session(self.store,self.names,'a'*64,HEAD,self.stop.is_set,
                self.connect,self.clock,self.steady,self.sleep,self.client,observe,**kwargs)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.path=Path(temp.name)/'state.sqlite3'

    def test_many_quiet_topics_keep_health_and_never_gain_ws_ready(self):
        names=sorted([topic('X'+str(i)+'USDT','1h') for i in range(8)])
        f=Fixture(self.path,names,quiet=names);f.run()
        self.assertTrue(any(s['service_status']=='DEGRADED' for s in f.samples))
        self.assertTrue(all(s['data_status']=='UNKNOWN' for s in f.samples))
        final=next(e for e in f.events if e.get('event')=='BINANCE_HEALTH_SESSION_FINAL')
        self.assertTrue(final['complete']);self.assertEqual(final['received'],0)
        self.assertTrue(all(p.closed and p.pings>1 for p in f.peers))
        self.assertTrue(any(e[1].endswith('klines') for e in f.requests))

    def test_entire_shard_without_pong_or_market_stops_and_conserves(self):
        name=topic('QUIETUSDT','1h');f=Fixture(self.path,[name],quiet=[name],pongs=False)
        with self.assertRaisesRegex(TimeoutError,'BINANCE_SHARD_PONG_TIMEOUT|BINANCE_SHARD_HEALTH_EXPIRED'):f.run()
        self.assertLessEqual(f.elapsed,100)
        self.assertTrue(next(e for e in f.events if e.get('event')=='BINANCE_HEALTH_SESSION_FINAL')['complete'])

    def test_live_socket_nontrading_never_becomes_data_ready(self):
        name=topic('BTCUSDT','5m');f=Fixture(self.path,[name],status='BREAK',until=100);f.run()
        self.assertTrue(any(s['service_status']=='DEGRADED' for s in f.samples))
        self.assertTrue(all(s['data_status']=='UNKNOWN' for s in f.samples))
        self.assertTrue(any(s['data_reason']=='BINANCE_TRADING_STATUS_UNKNOWN_OR_INVALID' for s in f.samples))

    def test_native_boundary_quiet_topic_has_scoped_rest_repair(self):
        name=topic('BTCUSDT','5m');f=Fixture(self.path,[name],quiet=[name],until=650);f.run()
        requests=[args for _,path,args in f.requests if path.endswith('klines')]
        self.assertTrue(any(args['endTime']>=NOW+300000 for args in requests))
        self.assertTrue(any(args['endTime']>=NOW+600000 for args in requests))
        self.assertTrue(all(s['data_status']=='UNKNOWN' for s in f.samples))

    def test_rest_timeout_revokes_quiet_proof_and_is_bounded(self):
        name=topic('QUIETUSDT','1h');f=Fixture(self.path,[name],quiet=[name],until=600)
        f.fail_quiet=True;f.run()
        failures=[e for e in f.events if e.get('event')=='BINANCE_HEALTH_WORK_FAILED' and e['kind']=='quiet']
        self.assertEqual(len(failures),3)
        value=f.proofs[-1]
        self.assertEqual(value['result'],'UNKNOWN');self.assertIn('TIMEOUTERROR',value['reason'])
        self.assertTrue(all(s['data_status']=='UNKNOWN' for s in f.samples))
        self.assertTrue(any(s['exhausted_work'] for s in f.samples))

    def test_http_cleanup_wait_cannot_leave_consumer_or_health_usable(self):
        name=topic('BTCUSDT','5m');f=Fixture(self.path,[name],until=30)
        entered=Event();release=Event();errors=[];original=f.client
        class BlockedClient(original):
            def request(inner,path,params,**kwargs):
                if path.endswith('exchangeInfo'):
                    entered.set()
                    if not release.wait(10):raise AssertionError('TEST_RELEASE_TIMEOUT')
                return super().request(path,params,**kwargs)
        f.client=BlockedClient
        def run():
            try:f.run()
            except BaseException as error:errors.append(error)
        runner=Thread(target=run);runner.start()
        try:
            self.assertTrue(entered.wait(3))
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if f.store.metadata().get('health_phase')=='DRAINING':break
                time.sleep(.005)
            self.assertEqual(f.store.metadata()['health_phase'],'DRAINING')
            self.assertTrue(runner.is_alive())
            self.assertEqual(f.store.freeze([name],f.clock(),f.clock())['status'],'UNKNOWN')
            self.assertFalse(read_sample(f.store,[name],'a'*64,HEAD,f.clock()).get('health_verified',False))
        finally:release.set();runner.join(10)
        self.assertFalse(runner.is_alive());self.assertEqual(errors,[])
        self.assertEqual(f.store.metadata()['connected'],'0')

    def test_mismatch_reverification_runs_even_after_ws_becomes_continuously_fresh(self):
        name=topic('BTCUSDT','5m');f=Fixture(self.path,[name],quiet=[name],until=280)
        f.switch_after_mismatch=True;f.run()
        self.assertTrue(f.changed_closed)
        self.assertTrue(any(p.get('reason')=='CLOSED_BAR_MISMATCH' for p in f.proofs))
        self.assertTrue(any(p.get('result')=='VALID' for p in f.proofs))
        self.assertTrue(any(s['data_status']=='READY' for s in f.samples))
        self.assertGreaterEqual(max(s['work_progress']['quiet']['completed'] for s in f.samples),2)

    def test_close_exception_still_drains_disconnects_and_emits_final(self):
        name=topic('BTCUSDT','5m');f=Fixture(self.path,[name],until=100);f.close_errors=True
        with self.assertRaisesRegex(OSError,'FIXTURE_CLOSE_ERROR'):f.run()
        self.assertEqual(f.store.metadata()['connected'],'0')
        final=next(e for e in f.events if e.get('event')=='BINANCE_HEALTH_SESSION_FINAL')
        self.assertTrue(final['complete'])


if __name__=='__main__':unittest.main()
