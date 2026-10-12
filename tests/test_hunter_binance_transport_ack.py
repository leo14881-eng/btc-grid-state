"""Actual sockets, reader threads and session scheduling; injected dial/send clock.

JSON frames over a socket pair replace only the remote WebSocket peer. No public
network, compressed protocol frames or exchange availability is asserted here.
The virtual clock reproduces 8/9.8-second handshakes and 500ms send spacing without
sleeping for minutes. The production pump, ACK queue, deadline and teardown run.
"""
import contextlib
import json
from pathlib import Path
import socket
import sys
import tempfile
from threading import Event, Lock, Thread, get_ident
import time
import unittest

from research import hunter_binance_stream as stream
from research.hunter_binance_stream_store import Store,topic

NOW=1791670694000


class Clock:
    def __init__(self):
        self.owner=get_ident();self.now=NOW;self.local={};self.lock=Lock();self.running=False

    def __call__(self):
        with self.lock:
            if get_ident()==self.owner:
                if self.running:self.now=max([self.now,*self.local.values()])+1000
                return self.now
            return self.local.setdefault(get_ident(),self.now)

    def sleep(self,seconds):
        with self.lock:self.local[get_ident()]=self.local.setdefault(get_ident(),self.now)+int(seconds*1000)
        time.sleep(.001) # Give real socket readers a scheduling opportunity.

    def dial(self,milliseconds):
        with self.lock:self.now+=milliseconds


class Peer:
    def __init__(self,clock,count,missing=False,drop_first=False,late=False,old_id=None):
        if sys.platform!='linux':raise RuntimeError('LINUX_SOCKETPAIR_ACCEPTANCE_REQUIRED')
        self.clock=clock;self.count=count;self.expected=0 if missing else count-int(drop_first)
        self.missing=missing;self.drop_first=drop_first;self.late=late;self.old_id=old_id
        self.client,self.server=socket.socketpair()
        self.client.settimeout(.1);self.server.settimeout(.1)
        self.buffer=b'';self.requests=[];self.sent_times=[];self.received=0
        self.processed=Event();self.stopped=Event();self.thread=Thread(target=self.serve)
        self.thread.start()

    def serve(self):
        buffer=b'';number=0
        try:
            while not self.stopped.is_set():
                try:data=self.server.recv(65536)
                except TimeoutError:continue
                if not data:return
                buffer+=data
                while b'\n' in buffer:
                    raw,_,buffer=buffer.partition(b'\n');request=json.loads(raw);number+=1
                    if self.missing or self.drop_first and number==1:continue
                    request_id=self.old_id if self.old_id and number==1 else request['id']
                    self.server.sendall(json.dumps(dict(id=request_id,result=None)).encode()+b'\n')
        except OSError:
            if not self.stopped.is_set():raise

    def send(self,payload):
        self.requests.append(json.loads(payload));self.sent_times.append(self.clock())
        self.client.sendall(payload.encode()+b'\n')

    def recv(self,timeout):
        if self.received==self.expected:self.processed.set()
        if self.late and self.received==0:
            self.clock.sleep(31);self.late=False
        while b'\n' not in self.buffer:
            data=self.client.recv(65536)
            if not data:raise OSError('fixture peer closed')
            self.buffer+=data
        raw,_,self.buffer=self.buffer.partition(b'\n');self.received+=1
        return raw.decode()

    def close(self):
        self.stopped.set()
        for channel in (self.client,self.server):
            with contextlib.suppress(OSError):channel.shutdown(socket.SHUT_RDWR)
            channel.close()
        self.thread.join(2)
        if self.thread.is_alive():raise AssertionError('fixture peer thread leaked')


class RecordingStore(Store):
    def arm(self,names):self.applied=[];self.expected=len(names);self.done=Event()

    def ack(self,epoch,names):
        super().ack(epoch,names);self.applied.extend(names)
        if len(self.applied)==self.expected:self.done.set()


class AckScheduleTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=RecordingStore(Path(temp.name)/'state.sqlite3')

    def run_session(self,count,dial_ms,*,missing=False,drop_first=False,late=False,old_id=None):
        names=[topic('X'+str(i)+'USDT','1h') for i in range(count)]
        self.store.arm(names);clock=Clock();peers=[]
        shards=(count+499)//500
        @contextlib.contextmanager
        def connect():
            if peers and not peers[-1].processed.wait(3):
                raise AssertionError('previous socket did not deliver its configured ACKs')
            clock.dial(dial_ms)
            index=len(peers);number=(min(500,count-index*500)+99)//100
            peer=Peer(clock,number,missing=missing,drop_first=drop_first and index==0,
                      late=late and index==shards-1,old_id=old_id if index==shards-1 else None)
            peers.append(peer)
            if len(peers)==shards:clock.running=True
            try:yield peer
            finally:peer.close()
        def repair(store,epoch,name,deadline,*,cancelled):
            if not cancelled():
                with store.db() as db:db.execute('UPDATE topics SET gap=0 WHERE topic=?',(name,))
        try:
            stream.session(self.store,names,'a'*64,self.store.done.is_set,
                connect=connect,clock=clock,sleep=clock.sleep,repairer=repair)
        finally:
            self.peers=peers
            self.assertTrue(all(not p.thread.is_alive() for p in peers))
        return [r['id'] for p in peers for r in p.requests]

    def assert_controls(self,requests):
        self.assertEqual(len(requests),len(set(requests)))
        self.assertTrue(all(len(r)<=36 and r.isalnum() for r in requests))
        for peer in self.peers:
            self.assertTrue(all(len(r['params'])<=100 for r in peer.requests))
            self.assertTrue(all(b-a>=500 for a,b in zip(peer.sent_times,peer.sent_times[1:])))

    def test_four_shards_eight_second_dials_accept_all_sixteen_timely_acks(self):
        requests=self.run_session(1501,8000)
        self.assertEqual([len(p.requests) for p in self.peers],[5,5,5,1])
        self.assertEqual(len(self.store.applied),1501)
        self.assert_controls(requests)

    def test_current_1228_topics_three_9800ms_dials_keep_500ms_spacing(self):
        requests=self.run_session(1228,9800)
        self.assertEqual([len(p.requests) for p in self.peers],[5,5,3])
        self.assertEqual(len(self.store.applied),1228)
        self.assert_controls(requests)

    def test_genuinely_late_ack_fails_even_when_other_shards_succeed(self):
        with self.assertRaisesRegex(TimeoutError,'BINANCE_ACK_TIMEOUT'):
            self.run_session(1228,9800,late=True)
        self.assertEqual(len(self.store.applied),1000)

    def test_no_ack_has_its_own_send_deadline(self):
        with self.assertRaisesRegex(TimeoutError,'BINANCE_ACK_TIMEOUT'):
            self.run_session(1501,8000,missing=True)
        self.assertEqual(self.store.applied,[])

    def test_partial_ack_does_not_extend_other_requests_deadline(self):
        with self.assertRaisesRegex(TimeoutError,'BINANCE_ACK_TIMEOUT'):
            self.run_session(1228,9800,drop_first=True)
        self.assertGreater(len(self.store.applied),0)
        self.assertLess(len(self.store.applied),1228)

    def test_reconnect_uses_new_ids_and_rejects_old_epoch_ack(self):
        first=self.run_session(1228,9800)
        second=self.run_session(1228,9800)
        self.assertFalse(set(first)&set(second))
        with self.assertRaisesRegex(ValueError,'BINANCE_SUBSCRIPTION_ACK_INVALID'):
            self.run_session(1228,9800,old_id=first[-1])
        self.assertLess(len(self.store.applied),1228)


if __name__=='__main__':unittest.main()
