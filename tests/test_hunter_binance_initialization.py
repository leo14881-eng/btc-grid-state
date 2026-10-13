"""Real SQLite, pump threads, bounded queues and worker pool; no network/service IO.

The clock advances only after queued frames and due history writes drain. Public
WebSocket peers and history responses are deterministic fixtures, not mocks of
the collector, readiness decision, storage, or cancellation loop.
"""
import contextlib
import json
from pathlib import Path
import queue
import tempfile
from threading import Event, Lock
import time
import unittest
from unittest.mock import patch

from research import hunter_binance_stream as stream
from research.hunter_binance_stream_store import Store,STEPS,topic
from test_hunter_binance_cache import rows,message

BASE=1791849600000  # UTC midnight: exercise native 5m closes during bootstrap.


class Fixture:
    def __init__(self,path,names,*,first_frame=120,missing=None,history_delay=0,stop_ready=True):
        self.names=names;self.first_frame=first_frame;self.missing=missing
        self.history_delay=history_delay;self.stop_ready=stop_ready
        self.elapsed=0;self.wall_back=0;self.lock=Lock();self.stop=Event();self.peers=[]
        self.events=[];self.calls=[];self.active={};self.sent=0;self.processed=0
        self.disconnect_at=None;self.disconnected=False
        self.last_publish={};self.snapshots=[];self.started=time.monotonic()
        fixture=self
        class RecordingStore(Store):
            def begin_session(inner,*args):
                epoch=super().begin_session(*args);fixture.events.append(dict(event='SESSION',epoch=epoch));return epoch
            def ingest_ws_batch(inner,epoch,observations):
                result=super().ingest_ws_batch(epoch,observations)
                fixture.processed+=len(observations);return result
        self.store=RecordingStore(path)
        class ClockQueue(stream.ObservedQueue):
            def get(inner,*args,**kwargs):
                if kwargs.get('timeout')==.1:
                    fixture.tick()
                    return super().get(block=False)
                return super().get(*args,**kwargs)
        self.queue=ClockQueue

    def clock(self):return BASE+int(self.elapsed*1000)+self.wall_back
    def steady(self):return self.elapsed
    def sleep(self,_):time.sleep(.001)

    @contextlib.contextmanager
    def connect(self):
        fixture=self
        class Peer:
            def __init__(inner):inner.queue=queue.Queue();inner.topics=[];inner.closed=False
            def send(inner,payload):
                request=json.loads(payload);inner.topics.extend(request['params'])
                inner.queue.put(json.dumps(dict(id=request['id'],result=None)))
            def recv(inner,timeout):
                if fixture.disconnect_at is not None and fixture.elapsed>=fixture.disconnect_at and not fixture.disconnected:
                    fixture.disconnected=True;raise OSError('fixture connection closed')
                try:return inner.queue.get(timeout=.01)
                except queue.Empty:raise TimeoutError()
        peer=Peer();self.peers.append(peer)
        try:yield peer
        finally:peer.closed=True

    def repair(self,store,epoch,name,deadline,*,cancelled):
        with self.lock:self.calls.append((epoch,name));self.active[name]=self.elapsed+self.history_delay
        try:
            while self.elapsed<self.active[name]:
                if cancelled():return
                time.sleep(.001)
            if cancelled():return
            stamp=self.clock();_,interval=stream.split_topic(name)
            store.ingest_rest(epoch,name,rows(interval,stamp),stamp,stamp)
        finally:
            with self.lock:self.active.pop(name,None)

    def tick(self):
        if time.monotonic()-self.started>90:raise AssertionError('FIXTURE_WALL_TIMEOUT')
        time.sleep(.001)
        peers=[p for p in self.peers if not p.closed]
        if sum(len(p.topics) for p in peers)!=len(self.names):return
        if any(not x['ack'] for x in self.store.states()):return
        if self.processed<self.sent or any(not p.queue.empty() for p in peers):return
        with self.lock:
            if any(due<=self.elapsed for due in self.active.values()):return
        self.elapsed+=2
        if self.elapsed>=self.first_frame and int(self.elapsed-self.first_frame)%30==0:
            for peer in peers:
                for name in peer.topics:
                    if name==self.missing:continue
                    symbol,interval=stream.split_topic(name);stamp=self.clock();step=STEPS[interval]
                    start=stamp//step*step;previous=self.last_publish.get(name,start)
                    if previous<start:
                        peer.queue.put(json.dumps(message(symbol,interval,stamp-1,start=previous,closed=True)))
                        self.sent+=1
                    peer.queue.put(json.dumps(message(symbol,interval,stamp)))
                    self.sent+=1;self.last_publish[name]=start

    def emit(self,value):
        self.events.append(value)
        if value.get('event')=='BINANCE_INITIALIZATION_READY' and self.stop_ready:self.stop.set()

    def run(self,*,collect=False,initialization=None,observer=None):
        with patch.object(stream,'ObservedQueue',self.queue),patch.object(stream,'emit',side_effect=self.emit),\
             patch.object(stream,'millis',side_effect=self.clock):
            if collect:
                actual=stream.session
                def session(*args,**kwargs):kwargs['repairer']=self.repair;return actual(*args,**kwargs)
                with patch.object(stream,'session',side_effect=session):
                    return stream.collect(self.store,self.names,'a'*64,'b'*40,self.stop.is_set,
                        self.connect,self.clock,self.sleep,self.steady)
            return stream.session(self.store,self.names,'a'*64,self.stop.is_set,self.connect,
                self.clock,self.sleep,repairer=self.repair,steady=self.steady,initialization=initialization,
                observer=observer or (lambda now:self.snapshots.append(self.store.freeze(self.names,now,now))))


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'cache.sqlite3'

    def test_full_1228_topics_late_first_frames_preserve_history_until_actual_ready(self):
        names=sorted([topic('X'+str(i)+'USDT',period) for i in range(409) for period in ('15m','1h','1d')]
                     +[topic('BTCUSDT','5m')])
        f=Fixture(self.path,names,first_frame=120);f.run()
        self.assertEqual(len(f.calls),1228);self.assertEqual(len(set(f.calls)),1228)
        self.assertEqual(len([e for e in f.events if e['event']=='SESSION']),1)
        self.assertEqual(len([e for e in f.events if e['event']=='BINANCE_INITIALIZATION_READY']),1)
        self.assertGreater(f.elapsed,90);self.assertLess(f.elapsed,900)
        self.assertTrue(all(x['status']=='UNKNOWN' for x in f.snapshots if not x.get('windows')))
        self.assertEqual([len(p.topics) for p in f.peers],[500,500,228])
        self.assertTrue(all(p.closed for p in f.peers))
        print('FULL_BOOTSTRAP',dict(topics=len(names),elapsed=f.elapsed,history_calls=len(f.calls)))

    def test_permanent_missing_frame_stops_at_900_without_collect_retry(self):
        names=[topic('BTCUSDT','15m'),topic('ETHUSDT','15m')]
        f=Fixture(self.path,names,missing=names[-1])
        with self.assertRaisesRegex(stream.InitializationTimeout,'BINANCE_INITIALIZATION_DEADLINE'):f.run(collect=True)
        self.assertEqual(f.elapsed,900);self.assertEqual(len(f.calls),2)
        self.assertEqual(len([e for e in f.events if e['event']=='SESSION']),1)
        self.assertFalse(any(e['event']=='BINANCE_STREAM_RECOVERY' for e in f.events))
        self.assertFalse(any(e['event']=='BINANCE_INITIALIZATION_READY' for e in f.events))

    def test_slow_history_finishes_in_same_epoch_without_duplicate_repairs(self):
        names=[topic('BTCUSDT','15m'),topic('ETHUSDT','15m')]
        f=Fixture(self.path,names,first_frame=0,history_delay=180);f.run()
        self.assertGreaterEqual(f.elapsed,180);self.assertEqual(len(f.calls),2)
        self.assertEqual(len([e for e in f.events if e['event']=='SESSION']),1)

    def test_permanent_history_stops_and_cancels_workers(self):
        names=[topic('BTCUSDT','15m')];f=Fixture(self.path,names,first_frame=0,history_delay=1000)
        with self.assertRaises(stream.InitializationTimeout):f.run()
        self.assertEqual(f.elapsed,900);self.assertEqual(f.active,{})
        self.assertEqual(len(f.calls),1)

    def test_received_then_stale_during_history_wait_does_not_restart(self):
        names=[topic('BTCUSDT','15m'),topic('ETHUSDT','15m')]
        f=Fixture(self.path,names,first_frame=0,history_delay=180);tick=f.tick
        def pause_frames():
            tick()
            if f.elapsed==30:f.first_frame=210
        f.tick=pause_frames;f.run()
        self.assertGreaterEqual(f.elapsed,210);self.assertEqual(len(f.calls),2)
        self.assertEqual(len([e for e in f.events if e['event']=='SESSION']),1)
        self.assertFalse(any(e['event']=='BINANCE_TOPIC_STALE_DETAIL' for e in f.events))

    def test_network_reconnect_cannot_extend_initial_900_seconds(self):
        names=[topic('BTCUSDT','15m')];f=Fixture(self.path,names,first_frame=1200)
        f.disconnect_at=100
        with self.assertRaises(stream.InitializationTimeout):f.run(collect=True)
        self.assertTrue(f.disconnected);self.assertEqual(f.elapsed,900)
        self.assertEqual(len([e for e in f.events if e['event']=='SESSION']),2)
        self.assertEqual(len([e for e in f.events if e['event']=='BINANCE_STREAM_RECOVERY']),1)

    def test_wall_clock_rollback_does_not_extend_monotonic_initialization(self):
        names=[topic('BTCUSDT','15m')];f=Fixture(self.path,names,first_frame=1200);tick=f.tick
        def rewind_wall():
            tick()
            if f.elapsed==100:f.wall_back=-60000
        f.tick=rewind_wall
        with self.assertRaises(stream.InitializationTimeout):f.run()
        self.assertEqual(f.elapsed,900)

    def test_worker_admission_sees_deadline_while_main_audit_is_blocked(self):
        names=[topic('BTCUSDT','15m')];f=Fixture(self.path,names,first_frame=1200)
        started=Event();release=Event();finished=Event();cancelled_values=[]
        def repair(*args,cancelled,**kwargs):
            started.set()
            if not release.wait(5):raise AssertionError('fixture audit did not release worker')
            cancelled_values.append(cancelled());finished.set()
        f.repair=repair
        def blocked_audit(now):
            if not started.is_set():return
            f.elapsed=901;f.wall_back=-120000;release.set()
            self.assertTrue(finished.wait(5))
        with self.assertRaises(stream.InitializationTimeout):f.run(observer=blocked_audit)
        self.assertEqual(cancelled_values,[True])

    def test_existing_90_second_stale_rule_returns_after_first_ready(self):
        names=[topic('BTCUSDT','15m')];f=Fixture(self.path,names,first_frame=0,stop_ready=False)
        original=f.emit
        def emit(value):
            original(value)
            if value['event']=='BINANCE_INITIALIZATION_READY':f.missing=names[0]
        f.emit=emit
        with self.assertRaisesRegex(TimeoutError,'BINANCE_TOPIC_STALE'):f.run()
        self.assertEqual(len([e for e in f.events if e['event']=='BINANCE_INITIALIZATION_READY']),1)
        self.assertLess(f.elapsed,900)

    def test_deadline_is_monotonic_and_ready_cannot_reopen_bootstrap(self):
        window=stream.InitializationWindow(10)
        with self.assertRaises(stream.InitializationTimeout):window.check(910)
        window.ready=True;window.check(1000)

    def test_freeze_rejects_rest_current_and_stale_history_before_bootstrap_completion(self):
        name=topic('BTCUSDT','15m');store=Store(self.path)
        epoch=store.begin_session([name],'a'*64);store.ack(epoch,[name])
        store.ingest_rest(epoch,name,rows('15m',BASE),BASE,BASE)
        window=stream.InitializationWindow(0)
        window.observe(store,[name],BASE,lambda:BASE,lambda:1,epoch)
        self.assertFalse(window.ready)
        store.ingest_ws(epoch,message(stamp=BASE+1),BASE+1)
        store.ingest_rest(epoch,name,rows('15m',BASE+2),BASE+2,BASE+2)
        window.observe(store,[name],BASE+2,lambda:BASE+2,lambda:2,epoch)
        self.assertFalse(window.ready)
        store.ingest_ws(epoch,message(stamp=BASE+3),BASE+3)
        window.observe(store,[name],BASE+3,lambda:BASE+3,lambda:3,epoch)
        self.assertTrue(window.ready)

    def test_old_epoch_or_missing_closed_bar_cannot_end_bootstrap(self):
        name=topic('BTCUSDT','15m');store=Store(self.path)
        epoch=store.begin_session([name],'a'*64);store.ack(epoch,[name])
        store.ingest_rest(epoch,name,rows('15m',BASE),BASE,BASE)
        store.ingest_ws(epoch,message(stamp=BASE+1),BASE+1)
        window=stream.InitializationWindow(0)
        window.observe(store,[name],BASE+1,lambda:BASE+1,lambda:1,'b'*32)
        self.assertFalse(window.ready)
        with store.db() as db:db.execute('UPDATE revisions SET confirmed=0 WHERE start=?',(BASE-900000,))
        window.observe(store,[name],BASE+1,lambda:BASE+1,lambda:1,epoch)
        self.assertFalse(window.ready)

    def test_freeze_finishing_at_deadline_cannot_claim_initialization_ready(self):
        from unittest.mock import Mock
        window=stream.InitializationWindow(0);clock=iter([899,900]);store=Mock()
        store.freeze.return_value=dict(status='READY',epoch='a'*32)
        with self.assertRaises(stream.InitializationTimeout):
            window.observe(store,[],BASE,lambda:BASE,lambda:next(clock),'a'*32)
        self.assertFalse(window.ready)


if __name__=='__main__':unittest.main()
