"""Real OS sockets/processes against literal loopback; child forbids external IO."""
import contextlib
import io
import json
import os
from pathlib import Path
import select
import socket
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from research import hunter_binance_public as public
from research import hunter_binance_stream as stream
from research import hunter_bybit_public as coordinator
from research.hunter_binance_stream_store import Store, Budget

HELPER=Path(__file__).parent/'fixtures/binance_http_child.py'


class Server:
    def __init__(self,status=200,slow=False,headers_slow=False,defer_headers=False,retry_after=1,body=None):
        self.status,self.slow,self.headers_slow=status,slow,headers_slow
        self.defer_headers,self.retry_after=defer_headers,retry_after
        self.body=body
        self.received=Event();self.release_headers=Event();self.requests=0
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
                self.requests+=1;self.received.set()
                if self.defer_headers and not self.release_headers.wait(3):return
                body=self.body if self.body is not None else (b'country' if self.status==403 else b'[]')
                head=f'HTTP/1.1 {self.status} Fixture\r\nContent-Length: {1000000 if self.slow else len(body)}\r\nRetry-After: {self.retry_after}\r\nConnection: close\r\n\r\n'.encode()
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

    def test_directory_object_uses_same_real_child_and_retires_owner(self):
        body=dict(symbols=[dict(symbol='BTCUSDT',status='TRADING')])
        server=self.server(body=json.dumps(body).encode());client=self.client(server)
        result=client.request('/api/v3/exchangeInfo',dict(symbols='["BTCUSDT"]',showPermissionSets='false'),timeout=3)
        self.assertEqual(result,body);self.assertTrue(server.closed.wait(2));self.assert_retired()

    def assert_retired(self):
        self.assertIsNone(Budget(Store.reader(self.path)).status()['halt'])
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)
        self.assertEqual(list(self.path.parent.glob('request-*.lock')),[])

    def test_real_lifecycle_reports_reap_and_retirement_after_complete_response(self):
        server=self.server();client=self.client(server);events=[]
        self.assertEqual(client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),
            timeout=3,observe=lambda event,**fields:events.append((event,fields))),[])
        kinds=[x[0] for x in events]
        self.assertEqual(kinds,['ATTEMPT','GO','HEADERS','RESPONSE','REAPED','RETIRED','END'])
        self.assertEqual(events[2][1]['status'],200);self.assert_retired()
        self.assertFalse(any('token' in fields for _,fields in events))

    def test_real_incomplete_response_is_reaped_but_never_claims_owner_retirement(self):
        server=self.server(slow=True);client=self.client(server);events=[]
        with self.assertRaises(public.UncertainRequestError):
            client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),
                timeout=.4,observe=lambda event,**fields:events.append((event,fields)))
        kinds=[x[0] for x in events]
        self.assertIn('GO',kinds);self.assertIn('REAPED',kinds)
        self.assertNotIn('RESPONSE',kinds);self.assertNotIn('RETIRED',kinds)
        self.assertEqual(kinds[-2:],['ERROR','END']);self.assert_stopped(server)

    def test_observer_failure_does_not_replace_real_unknown_http_outcome(self):
        server=self.server(slow=True);client=self.client(server)
        def broken_observer(*args,**kwargs):raise OSError('unavailable audit sink')
        with self.assertRaises(public.UncertainRequestError):
            client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),
                timeout=.4,observe=broken_observer)
        self.assert_stopped(server)

    def test_legacy_cleanup_deadline_reproduces_timeout_then_orphan_after_five_seconds(self):
        # Exercise the deployed helper's unchanged default with real flock/DB.
        # No HTTP is needed to show that normal completion misses cleanup.
        def completed_session(*args,**kwargs):
            token=Budget(self.store).acquire();deadline=time.monotonic()+1
            with patch.object(coordinator.time,'monotonic',return_value=deadline-.5) as clock:
                with coordinator.tracked(self.store,token,deadline):
                    clock.return_value=deadline+1
        output=io.StringIO();delays=[]
        with patch.object(stream,'session',side_effect=completed_session) as sessions,\
             contextlib.redirect_stdout(output),self.assertRaisesRegex(RuntimeError,'ORPHANED_HTTP_REQUEST'):
            stream.collect(self.store,['btcusdt@kline_5m'],'a'*64,'b'*40,sleep=delays.append)
        self.assertEqual(sessions.call_count,1);self.assertAlmostEqual(sum(delays),5)
        event=json.loads(output.getvalue())
        self.assertEqual((event['failures'],event['error_type']),(1,'TimeoutError'))

    def test_complete_response_past_deadline_retires_owner_before_reconnect(self):
        server=self.server();client=self.client(server);delays=[];calls=[]
        with contextlib.ExitStack() as patches:
            def finished(*args):
                result=public.exchange(*args)
                self.assertTrue(server.closed.wait(2))
                patches.enter_context(patch.object(public.time,'monotonic',return_value=args[1]+.1))
                return result
            client.transport=finished
            def session(*args,**kwargs):
                calls.append(1)
                if len(calls)==1:
                    client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=3)
                self.assert_retired()
            with patch.object(stream,'session',session),contextlib.redirect_stdout(io.StringIO()):
                stream.collect(self.store,['btcusdt@kline_5m'],'a'*64,'b'*40,sleep=delays.append)
            self.assertEqual(len(calls),2);self.assertAlmostEqual(sum(delays),5)
        self.assert_retired()

    def test_complete_response_cancelled_after_reap_retires_without_publishing(self):
        server=self.server();client=self.client(server);cancelled=Event()
        def finished(*args):
            result=public.exchange(*args)
            self.assertTrue(server.closed.wait(2));cancelled.set()
            return result
        client.transport=finished
        with self.assertRaisesRegex(TimeoutError,'PUBLIC_REQUEST_CANCELLED_OR_EXPIRED'):
            client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),
                           timeout=3,cancelled=cancelled.is_set)
        self.assert_retired()

    def test_proven_not_sent_past_deadline_retires_owner(self):
        with contextlib.ExitStack() as patches:
            def unsent(url,deadline,*args):
                patches.enter_context(patch.object(public.time,'monotonic',return_value=deadline+.1))
                raise public.NotSentError('PUBLIC_REQUEST_NOT_SENT')
            with self.assertRaises(TimeoutError):
                public.Client(self.store,transport=unsent).request('/api/v3/klines',
                    dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=3)
        self.assert_retired()

    def test_completed_cleanup_busy_is_bounded_and_keeps_protection(self):
        server=self.server();client=self.client(server);real=self.store.db
        @contextlib.contextmanager
        def busy(*args,**kwargs):
            with real(*args,**kwargs) as db:
                class Connection:
                    def execute(_,sql,params=()):
                        if sql.startswith('DELETE FROM requests'):
                            raise sqlite3.OperationalError('database is locked')
                        return db.execute(sql,params)
                yield Connection()
        start=time.monotonic()
        with patch.object(self.store,'db',busy),self.assertRaisesRegex(public.DurableStopError,'CLEANUP_TIMEOUT'):
            client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=3)
        self.assertLess(time.monotonic()-start,2.5)
        self.assert_stopped(server)

    def test_completed_late_response_retries_only_cleanup_contention(self):
        server=self.server();client=self.client(server);real=self.store.db;remaining=[2];calls=[]
        @contextlib.contextmanager
        def busy(*args,**kwargs):
            with real(*args,**kwargs) as db:
                class Connection:
                    def execute(_,sql,params=()):
                        if sql.startswith('DELETE FROM requests') and remaining[0]:
                            remaining[0]-=1
                            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)
                            raise sqlite3.OperationalError('database is locked')
                        return db.execute(sql,params)
                yield Connection()
        with contextlib.ExitStack() as patches:
            def finished(*args):
                calls.append(1);result=public.exchange(*args)
                self.assertTrue(server.closed.wait(2))
                patches.enter_context(patch.object(public.time,'monotonic',return_value=args[1]+.1))
                return result
            client.transport=finished
            with patch.object(self.store,'db',busy),self.assertRaises(TimeoutError):
                client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),timeout=3)
        self.assertEqual((remaining,calls),([0],[1]));self.assert_retired()

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

    def test_cancelled_drain_receives_429_and_persists_cooldown_before_retiring(self):
        server=self.server(status=429,defer_headers=True,retry_after=7);client=self.client(server)
        stopped=Event();children=[];decisions=[];failure=client.budget.failure
        def cancelled():
            if server.received.is_set():
                stopped.set();server.release_headers.set()  # 429 is sent only after cancellation.
            return stopped.is_set()
        def record_failure(status,*args,**kwargs):
            decision=failure(status,*args,**kwargs)
            with self.store.db(readonly=True) as db:
                self.assertEqual(tuple(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                    for name in ('requests','leases')),(1,1))
            decisions.append((status,decision));return decision
        popen=public.subprocess.Popen
        def spawn(*args,**kwargs):
            child=popen(*args,**kwargs);children.append(child);return child
        with patch.object(public.subprocess,'Popen',spawn),patch.object(client.budget,'failure',record_failure),\
             self.assertRaisesRegex(TimeoutError,'PUBLIC_REQUEST_CANCELLED_OR_EXPIRED'):
            client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),
                timeout=3,cancelled=cancelled,drain_on_cancel=True)
        self.assertTrue(stopped.is_set());self.assertTrue(server.closed.wait(2))
        self.assertEqual((len(children),server.requests),(1,1))
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertEqual(decisions,[(429,'BOUNDED_RETRY')]);self.assert_retired()
        with self.store.db(readonly=True) as db:
            remaining=db.execute('SELECT cooldown_ms FROM budget').fetchone()[0]-time.monotonic_ns()//1_000_000
            self.assertGreater(remaining,5000)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bars').fetchone()[0],0)
        self.assertIsNone(Budget(Store.reader(self.path)).acquire())
        print('CANCEL_DRAIN_429',dict(http_requests=server.requests,retired=True,cooldown_remaining_ms=remaining,published_bars=0))

    def test_unreaped_wait_failure_retains_owner_and_stops_without_backoff(self):
        # A real child/socket is killed at its original deadline. Inject failure
        # only at the OS wait acknowledgement; the test later reaps the child.
        server=self.server(slow=True);client=self.client(server);children=[];waits=[];delays=[];calls=[]
        popen=public.subprocess.Popen
        def spawn(*args,**kwargs):
            child=popen(*args,**kwargs);children.append(child)
            class FailedWait:
                def __getattr__(self,name):return getattr(child,name)
                def wait(self,timeout):
                    waits.append(timeout);raise subprocess.TimeoutExpired(child.args,timeout)
            return FailedWait()
        def session(*args,**kwargs):
            calls.append(1)
            client.request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',limit=16,endTime=1),
                timeout=1,cancelled=server.ready.is_set,drain_on_cancel=True)
        try:
            with patch.object(public.subprocess,'Popen',spawn),patch.object(stream,'session',session),\
                 contextlib.redirect_stdout(io.StringIO()),self.assertRaisesRegex(public.UnreapedRequestError,'PROCESS_NOT_REAPED'):
                stream.collect(self.store,['btcusdt@kline_5m'],'a'*64,'b'*40,sleep=delays.append)
            self.assertEqual((len(children),calls,waits,delays),(1,[1],[1],[]))
            self.assert_stopped(server)
            with self.store.db(readonly=True) as db:
                counts=tuple(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('requests','leases','bars'))
            self.assertEqual(counts,(1,1,0));self.assertEqual(len(list(self.path.parent.glob('request-*.lock'))),1)
            with self.assertRaisesRegex(RuntimeError,'ORPHANED'):Budget(Store.reader(self.path)).acquire()
            print('UNREAPED_WAIT_FAULT',dict(counts=counts,backoff=delays,session_calls=len(calls),wait_timeout=waits))
        finally:
            for child in children:
                if child.poll() is None:child.kill()
                child.wait(timeout=2)

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
