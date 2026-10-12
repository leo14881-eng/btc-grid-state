"""Full session -> stale -> in-flight HTTP teardown, using real local sockets.

The dedicated CI also runs these fixtures against the exact deployed package.
No exchange connection, real-time 90-second wait, or production file is used.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import select
import socket
import sys
import tempfile
from threading import Event, Lock, Thread, get_ident
import time
import unittest
from unittest.mock import patch

from research import hunter_binance_public as public
from research import hunter_binance_stream as stream
from research.hunter_binance_stream_store import Store, Budget, topic
from test_hunter_binance_transport_ack import Peer

DEPLOYED=os.getenv('BINANCE_DEPLOYED_REPRO')=='1'
HELPER=Path(__file__).parent/'fixtures/binance_http_child.py'
NOW=1791670694000


class Clock:
    def __init__(self):
        self.owner=get_ident(); self.now=NOW; self.lock=Lock(); self.armed=lambda:False

    def __call__(self):
        advance=get_ident()==self.owner and self.armed()
        with self.lock:
            if advance:self.now+=20_000
            return self.now

    def sleep(self,seconds):
        with self.lock:self.now+=int(seconds*1000)
        time.sleep(.001)

    def dial(self):
        with self.lock:self.now+=9800


class DeferredHTTP:
    def __init__(self,complete):
        self.complete=complete;self.ready=Event();self.deliver=Event();self.closed=Event();self.stop=Event()
        self.socket=socket.socket();self.socket.bind(('127.0.0.1',0));self.socket.listen(1)
        self.port=self.socket.getsockname()[1];self.requests=0
        self.thread=Thread(target=self.run);self.thread.start()

    def run(self):
        try:
            self.socket.settimeout(5);connection,_=self.socket.accept()
            with connection:
                connection.settimeout(3);request=b''
                while b'\r\n\r\n' not in request:request+=connection.recv(4096)
                self.requests+=1
                connection.sendall(b'HTTP/1.1 200 Fixture\r\nContent-Length: 2\r\nConnection: close\r\n\r\n')
                self.ready.set();sent=False
                while not self.stop.is_set():
                    if self.complete and self.deliver.is_set() and not sent:
                        # Old code kills the socket immediately on session cancel.
                        # Fixed code can drain this complete response without retry.
                        time.sleep(.15);connection.sendall(b'[]');sent=True
                    if select.select([connection],[],[],.01)[0] and connection.recv(4096)==b'':
                        self.closed.set();return
        except (OSError,ConnectionError):self.closed.set()

    def close(self):
        self.stop.set();self.socket.close();self.thread.join(5)
        if self.thread.is_alive():raise AssertionError('HTTP fixture thread leaked')


class SessionCancellationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.folder=Path(temp.name);self.store=Store(self.folder/'state.sqlite3')

    def run_chain(self,complete):
        server=DeferredHTTP(complete);self.addCleanup(server.close)
        clock=Clock();names=[topic('X'+str(i)+'USDT','1h') for i in range(1228)]
        peers=[];delays=[];session_calls=[];output=io.StringIO();children=[]
        clock.armed=lambda:server.ready.is_set() and len(peers)==3 and all(p.processed.is_set() for p in peers)
        @contextlib.contextmanager
        def connect():
            if peers:self.assertTrue(peers[-1].processed.wait(3))
            clock.dial();index=len(peers);peer=Peer(clock,(min(500,1228-index*500)+99)//100)
            peers.append(peer)
            try:yield peer
            finally:peer.close()
        class ShortClient(public.Client):
            def request(inner,*args,**kwargs):
                kwargs['timeout']=2
                return super().request(*args,**kwargs)
        client=ShortClient(self.store)
        def repairer(store,epoch,name,deadline,*,cancelled):
            if name==names[0]:
                return stream.repair(store,epoch,name,deadline,cancelled=cancelled,clock=clock,client=client)
            while not cancelled():time.sleep(.005)
        actual_session=stream.session
        def run_session(*args,**kwargs):
            session_calls.append(1)
            if len(session_calls)>1:return  # Admission itself proves no orphan blocks reconnection.
            def state(reason):
                if reason=='SESSION_DISCONNECTED':server.deliver.set()
            return actual_session(self.store,names,'a'*64,lambda:False,connect=connect,
                clock=clock,sleep=clock.sleep,repairer=repairer,on_nonready=state)
        actual_popen=public.subprocess.Popen
        def spawn(*args,**kwargs):
            child=actual_popen(*args,**kwargs);children.append(child);return child
        command=lambda url:[sys.executable,'-B',str(HELPER),url,str(os.getpid()),str(server.port),str(self.folder),'normal']
        failure=None
        with patch.object(public,'worker_command',command),patch.object(public.subprocess,'Popen',spawn),\
             patch.object(stream,'session',run_session),contextlib.redirect_stdout(output):
            try:stream.collect(self.store,names,'a'*64,'b'*40,sleep=delays.append)
            except BaseException as error:failure=error
        self.assertEqual([len(p.requests) for p in peers],[5,5,3])
        self.assertTrue(server.ready.is_set());self.assertEqual(server.requests,1)
        self.assertTrue(server.closed.wait(2));self.assertTrue(children)
        self.assertTrue(all(p.poll() is not None for p in children),'HTTP child not reaped')
        self.assertTrue(all(not p.thread.is_alive() for p in peers))
        with self.store.db(readonly=True) as db:
            counts=tuple(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('requests','leases','bars'))
        events=[json.loads(line) for line in output.getvalue().splitlines()]
        return failure,delays,session_calls,counts,events

    def test_stale_cancellation_drains_complete_http_before_reconnection(self):
        failure,delays,calls,counts,events=self.run_chain(True)
        if DEPLOYED:
            self.assertIsInstance(failure,RuntimeError);self.assertIn('ORPHANED',str(failure))
            self.assertEqual((len(calls),counts),(1,(1,1,0)))
        else:
            self.assertIsNone(failure);self.assertEqual((len(calls),counts),(2,(0,0,0)))
        self.assertAlmostEqual(sum(delays),5)
        recovery=[e for e in events if e.get('event')=='BINANCE_STREAM_RECOVERY']
        self.assertEqual(recovery[0]['error_code'],'BINANCE_TOPIC_STALE')
        print('SESSION_CANCEL_CHAIN',dict(deployed=DEPLOYED,complete=True,counts=counts,backoff=sum(delays)))

    def test_incomplete_http_stays_unknown_without_misleading_retry(self):
        failure,delays,calls,counts,events=self.run_chain(False)
        self.assertEqual((len(calls),counts),(1,(1,1,0)))
        if DEPLOYED:
            self.assertIn('ORPHANED',str(failure));self.assertAlmostEqual(sum(delays),5)
        else:
            self.assertIsInstance(failure,public.UncertainRequestError)
            self.assertEqual(delays,[])
            self.assertIn('BINANCE_TOPIC_STALE',str(failure.__context__))
        print('SESSION_CANCEL_CHAIN',dict(deployed=DEPLOYED,complete=False,counts=counts,backoff=sum(delays)))

    def test_queued_fresh_frame_is_not_a_remote_idle_timeout(self):
        clock=Clock();peers=[];stop=Event();name=topic('BTCUSDT','15m');fixture=self
        clock.armed=lambda:len(peers)==1 and peers[0].processed.is_set()
        class BlockedWriterStore(Store):
            def states(inner):
                states=super().states()
                if states[0]['ack'] and clock.now-NOW>90_000 and not getattr(inner,'injected',False):
                    inner.injected=True;received=clock.now;start=received//900_000*900_000
                    message=dict(e='kline',E=received,s='BTCUSDT',k=dict(i='15m',s='BTCUSDT',t=start,
                        T=start+899999,o='100',h='102',l='99',c='101',v='10',q='1000',n=1,V='5',Q='500',x=False))
                    peer=peers[0];peer.server.sendall(json.dumps(message).encode()+b'\n')
                    end=time.monotonic()+2
                    while peer.received<2 and time.monotonic()<end:time.sleep(.001)
                    fixture.assertEqual(peer.received,2)
                    time.sleep(.01) # Let pump latch receipt and enqueue before the blocked writer returns.
                return states
            def ingest_ws_batch(inner,epoch,observations):
                result=super().ingest_ws_batch(epoch,observations);stop.set();return result
        store=BlockedWriterStore(self.folder/'queued.sqlite3')
        @contextlib.contextmanager
        def connect():
            peer=Peer(clock,1);peers.append(peer)
            try:yield peer
            finally:peer.close()
        def repairer(*args,cancelled,**kwargs):
            while not cancelled():time.sleep(.005)
        with contextlib.redirect_stdout(io.StringIO()):
            if DEPLOYED:
                with self.assertRaisesRegex(TimeoutError,'BINANCE_TOPIC_STALE'):
                    stream.session(store,[name],'a'*64,lambda:False,connect=connect,clock=clock,sleep=clock.sleep,repairer=repairer)
            else:
                stream.session(store,[name],'a'*64,stop.is_set,connect=connect,clock=clock,sleep=clock.sleep,repairer=repairer)
                self.assertTrue(stop.is_set())
        self.assertTrue(store.injected)
        self.assertEqual(store.freeze([name],clock.now,clock.now)['status'],'UNKNOWN')
        self.assertTrue(all(not p.thread.is_alive() for p in peers))
        print('QUEUED_FRAME_STALE_REPRO',dict(deployed=DEPLOYED,readiness='UNKNOWN'))


if __name__=='__main__':unittest.main()
