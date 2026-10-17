"""Linux process cleanup and native matching-pong checks; literal loopback only."""
import contextlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from research import hunter_bybit_public as public
from research.hunter_bybit_stream_store import Store, Budget
import test_hunter_public_transport_process as fixtures


class HealthProcessTests(unittest.TestCase):
    setUp=fixtures.ProcessTests.setUp
    client=fixtures.ProcessTests.client
    server=fixtures.ProcessTests.server
    assert_stopped=fixtures.ProcessTests.assert_stopped

    def no_owners(self):
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_opt_in_cancel_after_go_drains_reaps_then_retires_without_returning_data(self):
        server=self.server(); client=self.client(server); cancelled=Event(); events=[]; closed_before_retire=[]
        def observe(event,**kwargs):
            events.append(event)
            if event=='GO': cancelled.set()
            if event=='REAPED': closed_before_retire.append(server.closed.wait(2))
            if event=='RETIRED': closed_before_retire.append(server.closed.is_set())
        with self.assertRaises(TimeoutError):
            client.request('/v5/market/tickers',timeout=3,cancelled=cancelled.is_set,drain_on_cancel=True,observe=observe)
        self.assertEqual(events.count('GO'),1); self.assertLess(events.index('RESPONSE'),events.index('RETIRED'))
        self.assertEqual(closed_before_retire,[True,True])
        self.assertLess(events.index('REAPED'),events.index('RETIRED')); self.no_owners()
        self.assertIsNone(client.budget.status()['halt'])

    def test_opt_in_late_403_is_durable_after_cancel_and_has_no_retry(self):
        server=self.server(status=403); client=self.client(server); cancelled=Event(); events=[]
        def observe(event,**kwargs):
            events.append(event)
            if event=='GO': cancelled.set()
        with self.assertRaisesRegex(RuntimeError,'COUNTRY_403'):
            client.request('/v5/market/tickers',timeout=3,cancelled=cancelled.is_set,drain_on_cancel=True,observe=observe)
        self.assertTrue(server.closed.wait(2)); self.assertEqual(events.count('GO'),1); self.no_owners()
        self.assertIn('COUNTRY_403',Budget(Store.reader(self.path)).status()['halt'])

    def test_cancel_before_ready_never_admits_a_child(self):
        with patch.object(public.subprocess,'Popen',side_effect=AssertionError('child forbidden')):
            with self.assertRaises(TimeoutError):
                public.Client(self.store).request('/v5/market/tickers',cancelled=lambda:True,drain_on_cancel=True)
        self.no_owners()

    def test_cancel_during_before_go_sqlite_check_is_proven_unsent(self):
        server=self.server(); client=self.client(server); stopped=Event(); real=client.budget.status; events=[]
        def status(*args,**kwargs):
            value=real(*args,**kwargs); stopped.set(); return value
        with patch.object(client.budget,'status',status),self.assertRaises(public.NotSentError):
            client.request('/v5/market/tickers',timeout=3,cancelled=stopped.is_set,drain_on_cancel=True,
                           observe=lambda event,**_:events.append(event))
        self.assertNotIn('GO',events); self.assertIn('REAPED',events); self.no_owners()
        self.assertFalse(server.ready.is_set())

    def test_opt_in_drain_still_obeys_original_deadline_and_preserves_unknown_owner(self):
        server=self.server(slow=True); client=self.client(server); started=time.monotonic(); events=[]
        with self.assertRaises(public.UncertainRequestError):
            client.request('/v5/market/tickers',timeout=1,cancelled=server.ready.is_set,drain_on_cancel=True,
                           observe=lambda event,**_:events.append(event))
        self.assertLess(time.monotonic()-started,2.5); self.assertEqual(events.count('GO'),1)
        self.assertNotIn('RETIRED',events); self.assert_stopped(server)

    def test_cleanup_busy_retries_only_sqlite_after_reap_and_releases_atomically(self):
        server=self.server(); client=self.client(server); real=self.store.db; attempts=[0]; events=[]
        @contextlib.contextmanager
        def busy(*args,**kwargs):
            with real(*args,**kwargs) as db:
                class Connection:
                    def execute(_,sql,params=()):
                        if sql.startswith('DELETE FROM requests'):
                            self.assertIn('REAPED',events); self.assertTrue(server.closed.wait(2))
                            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],1)
                            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)
                            attempts[0]+=1
                            if attempts[0]<=3: raise sqlite3.OperationalError('database is locked')
                        return db.execute(sql,params)
                yield Connection()
        with patch.object(self.store,'db',busy):
            value=client.request('/v5/market/tickers',timeout=3,drain_on_cancel=True,
                                 observe=lambda event,**_:events.append(event))
        self.assertEqual(value['retCode'],0); self.assertEqual(events.count('GO'),1); self.assertEqual(attempts[0],4)
        self.no_owners()

    def test_cleanup_timeout_retains_owner_for_manual_review(self):
        server=self.server(); client=self.client(server); real=self.store.db
        @contextlib.contextmanager
        def busy(*args,**kwargs):
            with real(*args,**kwargs) as db:
                class Connection:
                    def execute(_,sql,params=()):
                        if sql.startswith('DELETE FROM requests'): raise sqlite3.OperationalError('database is locked')
                        return db.execute(sql,params)
                yield Connection()
        with patch.object(self.store,'db',busy),self.assertRaisesRegex(public.DurableStopError,'CLEANUP_TIMEOUT'):
            client.request('/v5/market/tickers',timeout=3,drain_on_cancel=True)
        self.assert_stopped(server)

    def test_unreaped_child_retains_owner_and_no_retry(self):
        events=[]
        def transport(*args,**kwargs):
            events.append('GO'); raise public.UnreapedRequestError('fixture reap failed')
        with self.assertRaises(public.UnreapedRequestError):
            public.Client(self.store,transport).request('/v5/market/tickers',drain_on_cancel=True)
        self.assertEqual(events,['GO'])
        self.assertIn('ORPHANED',Budget(Store.reader(self.path)).status()['halt'])
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],1)


class NativePingTests(unittest.TestCase):
    def test_installed_websockets_native_ping_matches_current_connection_and_not_close(self):
        from websockets.sync.client import connect
        from websockets.sync.server import serve
        from websockets.frames import Opcode
        from websockets.sync.server import ServerConnection
        ready=Event(); stop=Event(); held=[]
        def handler(connection):
            held.append(connection); ready.set(); stop.wait(3)
        with serve(handler,'127.0.0.1',0,ping_interval=None) as server:
            port=server.socket.getsockname()[1]
            thread=Thread(target=server.serve_forever); thread.start()
            try:
                with connect(f'ws://127.0.0.1:{port}',proxy=None,ping_interval=None) as ws:
                    self.assertTrue(ready.wait(2))
                    matched=ws.ping(b'bybit-health-current-session',ack_on_close=False)
                    self.assertTrue(matched.wait(2))
                    # Disable automatic pong in this literal loopback peer only.
                    original=held[0].protocol.send_frame
                    held[0].protocol.send_frame=lambda frame:None if frame.opcode==Opcode.PONG else original(frame)
                    unmatched=ws.ping(b'not-acknowledged',ack_on_close=False)
                    self.assertFalse(unmatched.wait(.05))
                    held[0].pong(b'wrong-current-session-token')
                    self.assertFalse(unmatched.wait(.05))
                    ws.close()
                    self.assertFalse(unmatched.is_set())
            finally:
                stop.set(); server.shutdown(); thread.join(3)
                self.assertFalse(thread.is_alive())


if __name__=='__main__': unittest.main()
