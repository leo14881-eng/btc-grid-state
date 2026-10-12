"""Real OS sockets/processes against literal loopback; child forbids external IO."""
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from research import hunter_binance_public as public
from research.hunter_binance_stream_store import Store, Budget

HELPER=Path(__file__).parent/'fixtures/binance_http_child.py'


class Server:
    def __init__(self,status=200,slow=False,headers_slow=False):
        self.status,self.slow,self.headers_slow=status,slow,headers_slow
        self.ready=Event();self.closed=Event();self.stop=Event();self.count=0
        self.socket=socket.socket();self.socket.bind(('127.0.0.1',0));self.socket.listen(1)
        self.port=self.socket.getsockname()[1]
        self.thread=Thread(target=self.run,daemon=True);self.thread.start()

    def run(self):
        try:
            self.socket.settimeout(4)
            connection,_=self.socket.accept()
            with connection:
                connection.settimeout(4);request=b''
                while b'\r\n\r\n' not in request:request+=connection.recv(4096)
                body=(b'country' if self.status==403 else b'[]')
                head=f'HTTP/1.1 {self.status} Fixture\r\nContent-Length: {1000000 if self.slow else len(body)}\r\nConnection: close\r\n\r\n'.encode()
                if self.headers_slow:head=b'HTTP/1.1 200 Fixture\r\nX-Slow: '
                connection.sendall(head)
                if not self.slow and not self.headers_slow:connection.sendall(body)
                self.ready.set()
                while not self.stop.is_set():
                    if select.select([connection],[],[],.03)[0] and connection.recv(4096)==b'':
                        self.closed.set();return
                    if self.slow or self.headers_slow:
                        connection.sendall(b'x');self.count+=1
        except (OSError,ConnectionError):self.closed.set()

    def close(self):
        self.stop.set();self.socket.close();self.thread.join(5)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.sqlite3';self.store=Store(self.path)

    def client(self,server,mode='normal'):
        command=lambda url:[sys.executable,'-B',str(HELPER),url,str(os.getpid()),str(server.port),self.temp.name,mode]
        self.addCleanup(patch.stopall)
        patch.object(public,'worker_command',command).start()
        return public.Client(self.store)

    def server(self,**kwargs):
        server=Server(**kwargs);self.addCleanup(server.close);return server

    def assert_stopped(self,server):
        self.assertTrue(server.closed.wait(2),'real peer socket did not close')
        self.assertIsNotNone(Budget(Store.reader(self.path)).status()['halt'])
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],1)

    def test_complete_response_closes_socket_before_lease_release(self):
        server=self.server();client=self.client(server)
        self.assertEqual(client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=3),[])
        self.assertTrue(server.closed.wait(2))
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)

    def test_slow_body_total_deadline_terminates_socket_and_preserves_unknown(self):
        server=self.server(slow=True);client=self.client(server);start=time.monotonic()
        with self.assertRaises(public.UncertainRequestError):client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=1)
        self.assertLess(time.monotonic()-start,2.5);self.assertGreater(server.count,3)
        self.assert_stopped(server)

    def test_slow_headers_are_covered_by_same_total_deadline(self):
        server=self.server(headers_slow=True);client=self.client(server)
        with self.assertRaises(public.UncertainRequestError):client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=1)
        self.assert_stopped(server)

    def test_blocked_resolution_is_killed_and_never_retried(self):
        server=self.server();client=self.client(server,'dns_stall');start=time.monotonic()
        with self.assertRaises(public.UncertainRequestError):client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=1)
        self.assertLess(time.monotonic()-start,2.5);self.assertFalse(server.ready.is_set())
        self.assertIn('ORPHANED',client.budget.status()['halt'])

    def test_403_header_stops_budget_before_slow_body_finishes(self):
        server=self.server(status=403,slow=True);client=self.client(server)
        with self.assertRaises(public.UncertainRequestError):client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=1)
        self.assertIn('403_PENDING',client.budget.status()['halt']);self.assert_stopped(server)

    def test_418_header_stops_budget_before_slow_body_finishes(self):
        server=self.server(status=418,slow=True);client=self.client(server)
        with self.assertRaises(public.UncertainRequestError):
            client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=1)
        self.assertIn('403_PENDING',client.budget.status()['halt']);self.assert_stopped(server)

    def test_lost_403_event_cannot_become_a_retry_after_reap(self):
        server=self.server(status=403);client=self.client(server,'lost403')
        with self.assertRaises(public.UncertainRequestError):client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=2)
        self.assertIn('ORPHANED',client.budget.status()['halt']);self.assert_stopped(server)

    def test_cancel_after_go_reaps_socket_and_retains_unknown(self):
        server=self.server(slow=True);client=self.client(server)
        with self.assertRaises(public.UncertainRequestError):
            client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=3,cancelled=server.ready.is_set)
        self.assert_stopped(server)

    def test_supervisor_death_kills_http_child_and_orphan_blocks_restart(self):
        server=self.server(slow=True)
        process=subprocess.Popen([sys.executable,'-B',str(HELPER),'--supervise',str(self.path),str(server.port),self.temp.name],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
        try:
            self.assertTrue(server.ready.wait(3))
            process.kill();process.wait(timeout=2)
            self.assert_stopped(server)
            with self.assertRaisesRegex(RuntimeError,'ORPHANED'):Budget(Store.reader(self.path)).acquire()
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=2)


if __name__=='__main__':unittest.main()
