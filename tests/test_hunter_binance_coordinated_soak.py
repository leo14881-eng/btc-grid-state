"""Bounded whole-roster wall soak and Linux real-process WS reconnect acceptance.

Market timestamps use a declared fixed UTC origin plus actual monotonic elapsed
time. This does not establish a natural UTC rollover or production acceptance.
Only Linux cases make literal loopback sockets; every child guards itself.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import datetime as dt
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
from threading import Event, Lock, Thread
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from research import hunter_binance_health as health
from research import hunter_binance_public as public
from research import hunter_binance_stream as stream
from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_control import ControlError
from research.hunter_binance_stream_store import LIMITS, STEPS, split_topic, topic
from scripts import hunter_binance_stream_ops as ops
from tests.test_hunter_binance_cache import rows, message
from tests import test_hunter_binance_coordinated_runtime as runtime_tests

MIDNIGHT = runtime_tests.MIDNIGHT
directory = runtime_tests.directory
topics = runtime_tests.topics

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT/'tests/fixtures/binance_ws_reconnect.py'
LOAD_SECONDS = 32  # Leave startup/scheduling margin above the required 30s wall window.


def body_for(path, params):
    if path == '/api/v3/exchangeInfo':
        return directory([topic(symbol, '15m') for symbol in json.loads(params['symbols'])])
    if path == '/api/v3/klines':
        return rows(params['interval'], int(params['endTime']), int(params['limit']))
    raise ValueError('FIXTURE_PUBLIC_ENDPOINT_NOT_ALLOWED')


class BodyClient:
    """Portable data fixture only; never claims to own or reap an HTTP process."""
    def __init__(self, store):
        pass

    def request(self, path, params, *, cancelled, **kwargs):
        if cancelled():
            raise TimeoutError('FIXTURE_CANCELLED')
        return body_for(path, params)


class DynamicHTTP:
    """Linux parent fixture; existing public validation remains in its child."""
    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.lock = Lock()
        self.requests = self.active = self.closed = 0
        self.errors = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with fixture.lock:
                    fixture.requests += 1
                    fixture.active += 1
                try:
                    parsed = urlsplit(self.path)
                    params = {key: value[0] for key, value in parse_qs(parsed.query).items()}
                    body = json.dumps(body_for(parsed.path, params)).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(body)
                except BaseException as error:
                    fixture.errors.append(type(error).__name__+':'+str(error))
                finally:
                    with fixture.lock:
                        fixture.active -= 1
                        fixture.closed += 1

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class WholeRosterSoak(unittest.TestCase):
    setUp = runtime_tests.DataCases.setUp
    market = runtime_tests.DataCases.market

    def test_complete_1228_topics_during_continuous_wall_load(self):
        names = topics(['BTCUSDT']+['S%03dUSDT' % i for i in range(408)])
        self.assertEqual(len(names), 1228)
        wall_started = time.monotonic()
        clock = lambda: MIDNIGHT+120000+int((time.monotonic()-wall_started)*1000)
        market = self.market('whole-roster', names=names, stamp=clock())
        with closing(sqlite3.connect(market.path)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        # Complete history is initialized through real control intents and real
        # market transactions. Fixture bodies have no HTTP admission claim.
        market.apply('directory', names, directory(names), clock())
        for name in names:
            stamp = clock()
            market.apply('seed', [name], rows(split_topic(name)[1], stamp), stamp)
        market.frames(names, clock())
        def whole_read():
            now = dt.datetime.fromtimestamp(clock()/1000, dt.timezone.utc)
            return consumer.read(names, now, reader=market.store, clock=clock)
        seeded = whole_read()
        self.assertEqual(set(seeded.generation['windows']), set(names))
        expected_rows = sum(LIMITS[split_topic(n)[1]] for n in names)
        self.assertEqual(sum(len(w['rows']) for w in seeded.generation['windows'].values()), expected_rows)
        seed_seconds = time.monotonic()-wall_started
        self.assertLess(seed_seconds, 75, 'seed fixture exceeded freshness/CI preparation budget')
        stopped, producer_done = Event(), Event()
        observed = stream.ObservedQueue(4096)
        errors, checkpoints, read_rejections, finals = [], [], {}, []
        metrics = dict(rounds=0, queued=0, processed=0, max_ws_delay_ms=0, cumulative_ws_delay_ms=0,
                       full_ready_reads=0, reader_scope_size=1228)
        load_started = time.monotonic()
        deadline = load_started+LOAD_SECONDS
        pool = ThreadPoolExecutor(max_workers=4)
        jobs = health.CoordinatedJobs(market.store, market.protocol, names, pool, clock,
                                     stopped.is_set, public.Client if sys.platform == 'linux' else BodyClient)
        jobs.seeded = set(names)

        def producer():
            try:
                while not stopped.is_set() and time.monotonic() < deadline:
                    round_started = time.monotonic()
                    stamp = clock()
                    in_round = 0
                    for name in names:
                        if stopped.is_set():
                            break
                        symbol, interval = split_topic(name)
                        shard = market.protocol.topic_shards[name]
                        value = message(symbol, interval, stamp)
                        market.protocol.arrived(shard, json.dumps(value), stamp)
                        try:
                            observed.put((shard, value, stamp), timeout=.5)
                        except queue.Full:
                            market.store.unprocessed(market.epoch, [(value, stamp)], 'SOAK_QUEUE_FULL')
                            raise AssertionError('SOAK_QUEUE_FULL')
                        metrics['queued'] += 1
                        in_round += 1
                    if in_round == len(names):
                        metrics['rounds'] += 1
                    stopped.wait(max(0, .8-(time.monotonic()-round_started)))
            except BaseException as error:
                errors.append(('producer', type(error).__name__, str(error)))
                stopped.set()
            finally:
                producer_done.set()

        def writer():
            try:
                while not producer_done.is_set() or not observed.empty():
                    try:
                        batch = [observed.get(timeout=.05)]
                    except queue.Empty:
                        continue
                    while len(batch) < 100:
                        try:
                            batch.append(observed.get_nowait())
                        except queue.Empty:
                            break
                    market.protocol.receive_batch(batch)
                    finished = clock()
                    delays = [finished-value[2] for value in batch]
                    metrics['processed'] += len(batch)
                    metrics['max_ws_delay_ms'] = max(metrics['max_ws_delay_ms'], max(delays))
                    metrics['cumulative_ws_delay_ms'] += sum(delays)
            except BaseException as error:
                errors.append(('writer', type(error).__name__, str(error)))
                stopped.set()

        def checkpoint():
            try:
                with closing(sqlite3.connect(market.path, timeout=.25)) as db:
                    while not stopped.wait(.2):
                        checkpoints.append(tuple(db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()))
            except BaseException as error:
                errors.append(('checkpoint', type(error).__name__, str(error)))
                stopped.set()

        threads = [Thread(target=producer), Thread(target=writer), Thread(target=checkpoint)]
        next_work = 0
        http = None
        load_finished = None
        with ExitStack() as stack:
            stack.enter_context(patch.object(stream, 'emit', side_effect=lambda value: finals.append(value)
                if value.get('event') in ('BINANCE_HEALTH_SESSION_FINAL', 'BINANCE_HEALTH_WORK_DRAIN') else None))
            if sys.platform == 'linux':
                http = DynamicHTTP()
                stack.callback(http.close)
                command = lambda url: [sys.executable, '-B', str(HELPER), '--http-child', url,
                    str(os.getpid()), str(http.port), str(market.path.parent)]
                stack.enter_context(patch.object(public, 'worker_command', command))
            try:
                for thread in threads:
                    thread.start()
                while time.monotonic() < deadline and not stopped.is_set():
                    now = clock()
                    jobs.tick(now)
                    elapsed = time.monotonic()-load_started
                    if elapsed >= next_work and len(jobs.active) < 4:
                        # Exercise real quiet/history publication alongside the
                        # runtime's directory round robin, without weakening its
                        # admission, 90s gate, source labels, or topic mutex.
                        name = topic('S%03dUSDT' % int(next_work//5), '1d')
                        kind = 'quiet' if int(next_work//5) % 2 == 0 else 'repair'
                        if not any((k, name) in jobs.active for k in ('seed', 'repair', 'quiet')):
                            jobs.submit((kind, name), kind, [name])
                        next_work += 5
                    try:
                        frozen = whole_read()
                        self.assertEqual(set(frozen.generation['windows']), set(names))
                        frozen.check_delivery()
                        metrics['full_ready_reads'] += 1
                    except (ValueError, ControlError) as error:
                        key = str(error).split(':', 1)[0]
                        read_rejections[key] = read_rejections.get(key, 0)+1
                    if time.monotonic()-wall_started > 135:
                        raise AssertionError('SOAK_TOTAL_WALL_BUDGET')
                load_finished = time.monotonic()
            except BaseException as error:
                print('BINANCE_SOAK_INTERRUPTED '+json.dumps(dict(metrics,
                    seed_wall_seconds=seed_seconds, load_wall_seconds=time.monotonic()-load_started,
                    error_type=type(error).__name__, error=str(error),
                    control_phase=getattr(error, 'control_phase', None),
                    queue_depth=observed.qsize(), queue_high_water=observed.high_water,
                    thread_errors=errors, market_transactions=market.store.transaction_diagnostics()), sort_keys=True))
                raise
            finally:
                stopped.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(12)
                work = jobs.shutdown(time.monotonic()+35)
            self.assertFalse(any(thread.is_alive() for thread in threads), 'soak thread did not stop')
            self.assertEqual(errors, [])
            self.assertTrue(work['complete'])
            self.assertEqual(work['errors'], [])
            self.assertEqual(observed.qsize(), 0)
            self.assertEqual(metrics['queued'], metrics['processed'])
            self.assertGreaterEqual(load_finished-load_started, LOAD_SECONDS)
            self.assertGreaterEqual(metrics['rounds'], 3)
            self.assertGreater(metrics['full_ready_reads'], 0)
            self.assertTrue(checkpoints)
            # Final whole-scope proof is made after every writer/job has ended.
            # Refresh all directory evidence and only then take another complete
            # WS round, preserving the same original 1228-topic scope.
            market.apply('directory', names, directory(names), clock())
            market.frames(names, clock())
            final_read = whole_read()
            final_read.check_delivery()
            generation = final_read.generation
            self.assertEqual(set(generation['windows']), set(names))
            self.assertEqual(sum(len(w['rows']) for w in generation['windows'].values()), expected_rows)
            now = dt.datetime.fromtimestamp(clock()/1000, dt.timezone.utc)
            snapshot = dict(binance_complete=True, generation_id='whole-soak', as_of_utc=now.isoformat(),
                coins={symbol[:-4]: dict(pairs=[dict(venue='binance', base=symbol[:-4], pair=symbol)])
                       for symbol in sorted({split_topic(n)[0] for n in names})})
            clock_dt = lambda: dt.datetime.fromtimestamp(clock()/1000, dt.timezone.utc)
            early = ops.consumer_acceptance('early', market.store, now, snapshot=snapshot, clock=clock_dt)
            selection = dict(states=[dict(open_positions=[dict(asset='BTC'), dict(asset='S000')]),
                                     dict(open_positions=[dict(asset='S007')])], cursor=0)
            monitor = ops.consumer_acceptance('monitor', market.store, now, selection=selection, clock=clock_dt)
            self.assertEqual((early['status'], early['windows']), ('READY', 818))
            self.assertEqual((monitor['status'], monitor['windows']), ('READY', 6))
            with market.store.control._db(readonly=True) as db:
                owner_count = db.execute('SELECT COUNT(*) FROM owners').fetchone()[0]
                request_count = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
                calls, terminals = db.execute('SELECT COUNT(*),COUNT(terminal) FROM calls').fetchone()
                attempts, retired = db.execute("SELECT COUNT(*),SUM(CASE WHEN terminal='RETIRED' THEN 1 ELSE 0 END) FROM attempts").fetchone()
                charged = db.execute('SELECT charged_total FROM budget WHERE id=1').fetchone()[0]
            self.assertEqual((owner_count, request_count), (0, 0))
            self.assertEqual(calls, terminals)
            if http is not None:
                self.assertGreater(attempts, 2)
                self.assertEqual(attempts, retired)
                self.assertGreater(charged, 0)
                self.assertEqual(http.errors, [])
                self.assertEqual(http.requests, http.closed)
                self.assertEqual(http.active, 0)
            self.assertEqual(list(market.path.parent.glob('*.owner-*.lock')), [])
            market.store.start_draining(market.epoch)
            market.store.disconnect(market.epoch)
            final = market.protocol.final('WHOLE_ROSTER_WALL_SOAK_STOP')
            self.assertTrue(final['complete'])
            self.assertEqual(final['received'], final['committed']+final['duplicate']+
                             final['invalid']+final['unprocessed_recorded'])
        metrics.update(seed_wall_seconds=seed_seconds, load_wall_seconds=load_finished-load_started,
            total_wall_seconds=time.monotonic()-wall_started, initialized_topics=1228,
            initialized_rows=expected_rows, final_ready_topics=len(generation['windows']),
            early_required_topics=early['windows'], monitor_required_topics=monitor['windows'],
            queue_high_water=observed.high_water, queue_errors=len([e for e in errors if e[0] == 'producer']),
            sqlite_errors=errors, checkpoints=len(checkpoints), read_rejections=read_rejections,
            active_owners=owner_count, requests=request_count, calls=calls, terminals=terminals,
            attempts=attempts, retired=retired, charged_total=charged,
            real_http_child_verified=http is not None, market_clock='FIXED_UTC_PLUS_REAL_ELAPSED',
            natural_utc_rollover_verified=False, receipt_conserved=final['complete'])
        print('BINANCE_COMPLETE_ROSTER_WALL_SOAK '+json.dumps(metrics, sort_keys=True))


@unittest.skipUnless(sys.platform == 'linux', 'LINUX_PROCESS_ACCEPTANCE_REQUIRED_NOT_VERIFIED_ON_THIS_HOST')
class ReconnectProcess(unittest.TestCase):
    setUp = runtime_tests.DataCases.setUp

    def test_actual_collector_process_reconnects_ws_epoch_and_retires_http(self):
        from websockets.sync.server import serve
        from websockets.exceptions import ConnectionClosed
        scratch = self.scratch/'reconnect'
        scratch.mkdir()
        config = dict(base_ms=MIDNIGHT+120000, anchor_mono=time.monotonic(), source_head='d'*40, manifest='e'*64)
        (scratch/'fixture.json').write_text(json.dumps(config), encoding='utf-8')
        clock = lambda: config['base_ms']+int((time.monotonic()-config['anchor_mono'])*1000)
        names = sorted([topic('BTCUSDT', '5m'), topic('BTCUSDT', '1h'), topic('ETHUSDT', '15m')])
        drop_first, shutdown = Event(), Event()
        connections, subscriptions, ws_errors = [], [], []
        connection_lock = Lock()

        def websocket_handler(peer):
            with connection_lock:
                connections.append(peer)
                number = len(connections)
            try:
                request = json.loads(peer.recv(timeout=5))
                if request.get('method') != 'SUBSCRIBE' or sorted(request.get('params', [])) != names:
                    raise AssertionError('FIXTURE_FULL_RECONNECT_SCOPE_REQUIRED')
                subscriptions.append((number, request['id'], list(request['params'])))
                peer.send(json.dumps(dict(id=request['id'], result=None)))
                previous = {}
                while not shutdown.wait(.15):
                    if number == 1 and drop_first.is_set():
                        peer.close(code=1011, reason='fixture forced disconnect')
                        return
                    stamp = clock()
                    for name in names:
                        symbol, interval = split_topic(name)
                        start = stamp//STEPS[interval]*STEPS[interval]
                        if name in previous and previous[name] != start:
                            peer.send(json.dumps(message(symbol, interval, stamp-1, start=previous[name], closed=True)))
                        peer.send(json.dumps(message(symbol, interval, stamp)))
                        previous[name] = start
            except ConnectionClosed:
                pass
            except BaseException as error:
                ws_errors.append(type(error).__name__+':'+str(error))

        http = DynamicHTTP()
        server = serve(websocket_handler, '127.0.0.1', 0, ping_interval=None, close_timeout=1)
        server_thread = Thread(target=server.serve_forever)
        server_thread.start()
        command = [sys.executable, '-B', str(HELPER), '--collector', str(scratch),
                   str(server.socket.getsockname()[1]), str(http.port)]
        process = None
        events, lines, parse_errors = [], queue.Queue(), []
        stdout_thread = None
        wall_started = time.monotonic()
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={k: os.environ[k] for k in ('PATH', 'LANG', 'SYSTEMROOT') if k in os.environ})

            def consume():
                try:
                    for line in process.stdout:
                        value = json.loads(line)
                        events.append(value)
                        lines.put(value)
                except BaseException as error:
                    parse_errors.append(str(error))

            stdout_thread = Thread(target=consume)
            stdout_thread.start()
            ready = []
            while len(ready) < 2 and time.monotonic()-wall_started < 55:
                try:
                    value = lines.get(timeout=.25)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if value.get('event') == 'FIXTURE_EPOCH_READY':
                    ready.append(value)
                    if len(ready) == 1:
                        drop_first.set()
            (scratch/'stop').write_text('STOP\n', encoding='ascii')
            process.wait(timeout=15)
            stdout_thread.join(3)
            stderr = process.stderr.read().decode(errors='replace')
            self.assertEqual(process.returncode, 0, stderr[-4000:]+' '+json.dumps(events[-4:]))
            self.assertFalse(stdout_thread.is_alive())
            self.assertEqual(parse_errors, [])
            self.assertEqual(len(ready), 2)
            self.assertNotEqual(ready[0]['epoch'], ready[1]['epoch'])
            self.assertTrue(ready[1]['old_frozen_rejected'])
            self.assertTrue(all((r['topics'], r['seeded'], r['acked'], r['windows']) == (3, 3, 3, 3) for r in ready))
            self.assertEqual(len(subscriptions), 2)
            self.assertTrue(all(sorted(row[2]) == names for row in subscriptions))
            session_finals = [e for e in events if e.get('event') == 'BINANCE_HEALTH_SESSION_FINAL']
            shutdown_finals = [e for e in events if e.get('event') == 'BINANCE_HEALTH_SHUTDOWN_FINAL']
            self.assertEqual(len(session_finals), 2)
            self.assertEqual(len(shutdown_finals), 2)
            for final in session_finals:
                self.assertTrue(final['complete'])
                self.assertGreater(final['received'], 0)
                self.assertEqual(final['received'], final['committed']+final['duplicate']+
                                 final['invalid']+final['unprocessed_recorded'])
            for final in shutdown_finals:
                self.assertFalse(final['websocket_threads_alive'])
                self.assertEqual(final['secondary_errors'], [])
                self.assertTrue(final['work']['complete'])
                self.assertTrue(final['control']['complete'] and final['control']['all_retired'])
            summary = next(e for e in events if e.get('event') == 'FIXTURE_PROCESS_FINAL')
            self.assertEqual((summary['active_owners'], summary['requests'], summary['orphan_files']), (0, 0, 0))
            self.assertEqual(summary['calls'], summary['terminals'])
            self.assertEqual(summary['attempts'], summary['retired'])
            self.assertEqual(summary['blocked_effects'], [])
            self.assertEqual(summary['sqlite_errors'], [])
            self.assertEqual(summary['connected'], '0')
            self.assertEqual(ws_errors, [])
            self.assertEqual(http.errors, [])
            self.assertEqual(http.active, 0)
            self.assertGreater(http.requests, 6)
            self.assertLess(time.monotonic()-wall_started, 70)
            print('BINANCE_REAL_PROCESS_WS_RECONNECT '+json.dumps(dict(summary,
                websocket_connections=len(connections), subscriptions=len(subscriptions),
                session_finals=len(session_finals), http_requests=http.requests), sort_keys=True))
        finally:
            if process is not None and process.poll() is None:
                (scratch/'stop').write_text('STOP\n', encoding='ascii')
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            shutdown.set()
            for peer in connections:
                peer.close()
            server.shutdown()
            server_thread.join(3)
            http.close()
            if stdout_thread is not None:
                stdout_thread.join(3)
            if process is not None:
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
