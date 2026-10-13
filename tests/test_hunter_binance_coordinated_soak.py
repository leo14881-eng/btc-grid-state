"""Bounded whole-roster wall soak and Linux real-process WS reconnect acceptance.

Market timestamps use a declared fixed UTC origin plus actual monotonic elapsed
time. This does not establish a natural UTC rollover or production acceptance.
Only Linux cases make literal loopback sockets; every child guards itself.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from bisect import bisect_left
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
from threading import Event, Lock, Thread
import time
import traceback
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


def soak_seconds(value):
    if value not in ('32', '3600'):
        raise ValueError('BINANCE_COORDINATED_SOAK_SECONDS_MUST_BE_32_OR_3600')
    return int(value)


# Explicit long-mode opt-in; ordinary unittest discovery remains 32 seconds.
LOAD_SECONDS = soak_seconds(os.environ.get('BINANCE_COORDINATED_SOAK_SECONDS', '32'))


class LatencyHistogram:
    """All observations, fixed storage; p99 is a bucket upper bound, not exact."""
    def __init__(self, unit):
        self.unit = unit
        self.bounds = (0, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000,
                       10000, 20000, 50000, 100000, 200000, 500000, 1000000,
                       2000000, 5000000, 10000000, 30000000, 60000000)
        self.buckets = [0]*(len(self.bounds)+1)
        self.count = self.total = self.maximum = 0
        self.lock = Lock()

    def add(self, value):
        if value < 0:
            raise ValueError('SOAK_NEGATIVE_MONOTONIC_LATENCY')
        with self.lock:
            self.buckets[bisect_left(self.bounds, value)] += 1
            self.count += 1
            self.total += value
            self.maximum = max(self.maximum, value)

    def snapshot(self):
        with self.lock:
            target = (self.count*99+99)//100
            cumulative = 0
            upper = None
            if target:
                for bound, count in zip(self.bounds, self.buckets):
                    cumulative += count
                    if cumulative >= target:
                        upper = bound
                        break
            return dict(unit=self.unit, count=self.count, total=self.total, maximum=self.maximum,
                p99_upper=upper, p99_overflow=bool(target and upper is None),
                bounds=list(self.bounds), buckets=list(self.buckets), all_observations=True)


def fixture_rows(interval, stamp, count=None):
    body = rows(interval, stamp, count)
    if LOAD_SECONDS == 3600:
        # The old short fixture varies a historical close by its index within
        # the requested window. Long mode needs an immutable bar body across
        # rolling REST windows and explicit WS close frames (close='101.0').
        for row in body:
            row[4] = '101.0'
    return body


def error_detail(operation, error):
    return dict(operation=operation, type=type(error).__name__, message=str(error),
        market_phase=getattr(error, 'market_phase', None),
        control_phase=getattr(error, 'control_phase', None),
        sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
        sqlite_errorname=getattr(error, 'sqlite_errorname', None),
        sqlite_context=getattr(error, 'sqlite_context', None),
        traceback=''.join(traceback.format_exception(type(error), error, error.__traceback__)))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def audit_hash(value):
    return hashlib.sha256(canonical(value).encode('ascii')).hexdigest()


def emit_bounded(event, value):
    """Bound every physical log line, including an unexpectedly large traceback."""
    raw = canonical(value)
    if len(event)+1+len(raw) <= 28000:
        print(event+' '+raw, flush=True)
        return
    digest = hashlib.sha256(raw.encode('ascii')).hexdigest()
    pieces = [raw[index:index+6000] for index in range(0, len(raw), 6000)]
    for index, piece in enumerate(pieces):
        print('BINANCE_SOAK_AUDIT_CHUNK '+canonical(dict(source_event=event,
            sha256=digest, chunk=index, chunks=len(pieces), encoding='CANONICAL_JSON_TEXT_FRAGMENT',
            json_fragment=piece)), flush=True)
    print(event+' '+canonical(dict(fragmented=True, sha256=digest, chunks=len(pieces),
        status=value.get('status'), phase=value.get('phase'), event=value.get('event'))), flush=True)


def emit_pages(event, records, *, source_event, phase):
    pages = [records[index:index+16] for index in range(0, len(records), 16)]
    hashes = []
    for index, page in enumerate(pages):
        digest = audit_hash(page)
        hashes.append(digest)
        emit_bounded(event, dict(source_event=source_event, phase=phase, page=index,
            pages=len(pages), records=page, page_sha256=digest))
    return dict(records=len(records), pages=len(pages), page_sha256=hashes,
                records_sha256=audit_hash(records))


def emit_soak_report(report):
    # The runner retains the complete unpaged report for strict assertions.
    # Only this shallow rendering copy receives page manifests and field pops.
    report = dict(report)
    event = report['event']
    for field, page_event in (('_topic_receipts', 'BINANCE_SOAK_TOPIC_RECEIPTS'),
                              ('_quiet_jobs', 'BINANCE_SOAK_QUIET_JOBS')):
        records = report.pop(field, None)
        if records is not None:
            report[field[1:]+'_audit'] = emit_pages(page_event, records, source_event=event, phase=report['phase'])
    for field in ('market_transactions', 'control_transactions'):
        values = report.pop(field, None)
        if values is not None:
            records = [dict(key=key, value=value) for key, value in sorted(values.items())]
            report[field+'_audit'] = emit_pages('BINANCE_SOAK_TRANSACTION_METRICS', records,
                source_event=event+':'+field, phase=report['phase'])
    emit_bounded(event, report)


def soak_drain_metrics(state, *, final=True):
    """Failure-safe observations after the existing bounded drain; no DB writes."""
    now = time.monotonic()
    result = dict(event='BINANCE_SOAK_DRAIN_METRICS', phase=state.get('phase'),
        status='FAILED' if state.get('error') else ('PASSED' if final else 'RUNNING'), error=state.get('error'),
        load_target_seconds=LOAD_SECONDS, queue_capacity=4096, queue_put_timeout_seconds=.5,
        topics_per_round=1228, minimum_round_interval_seconds=.8,
        total_wall_seconds=now-state['wall_started'],
        market_clock='FIXED_UTC_PLUS_REAL_ELAPSED', natural_utc_rollover_verified=False,
        metrics=dict(state.get('metrics', {})), thread_errors=list(state.get('errors', [])),
        thread_error_details=list(state.get('thread_error_details', [])),
        read_rejections=dict(state.get('read_rejections', {})),
        jobs_shutdown=state.get('work'), diagnostic_errors=[])
    result['load_error'] = state.get('load_error')
    result['jobs_shutdown_error'] = state.get('jobs_shutdown_error')
    result['latencies'] = {key: value.snapshot() for key, value in state.get('histograms', {}).items()}
    if 'quiet_lock' in state:
        with state['quiet_lock']:
            quiet = {key: dict(value) for key, value in state['quiet_by_key'].items()}
    else:
        quiet = {}
    result['quiet_jobs'] = dict(keys=len(quiet), keys_sha256=audit_hash(sorted(quiet)),
        **{key:sum(value[key] for value in quiet.values()) for key in ('submitted','completed','failed','cancelled')},
        pending=sum(value['submitted']-value['completed']-value['failed']-value['cancelled'] for value in quiet.values()),
        max_submit_to_done_ms=max([value['max_submit_to_done_ms'] for value in quiet.values()]+[0]))
    if final:
        result['_quiet_jobs'] = [dict(job_key=key, **quiet[key]) for key in sorted(quiet)]
    result['quiet_candidate_to_submit_max_ms'] = state.get('quiet_candidate_to_submit_max_ms', 0)
    result['quiet_waiting_observed_max_ms'] = state.get('quiet_waiting_observed_max_ms', 0)
    result['read_intervals_15m'] = {key: dict(value) for key, value in state.get('read_intervals', {}).items()}
    attempts = result['metrics'].get('read_attempts', 0)
    result['read_rejection_ratio'] = None if not attempts else result['metrics'].get('read_rejected', 0)/attempts
    unresolved_unready = (0 if state.get('unready_since') is None else
                          (state.get('load_finished') or now)-state['unready_since'])
    result['max_observed_nonready_seconds'] = max(state.get('max_nonready_seconds', 0), unresolved_unready)
    for key in ('seed_wall_seconds', 'initialized_topics', 'initialized_rows'):
        result[key] = state.get(key)
    for label, start, finish in (
            ('load_wall_seconds', 'load_started', 'load_finished'),
            ('receipt_drain_wall_seconds', 'drain_started', 'drain_finished'),
            ('jobs_shutdown_wall_seconds', 'jobs_shutdown_started', 'jobs_shutdown_finished')):
        result[label] = None if state.get(start) is None else (state.get(finish) or now)-state[start]
    result['threads'] = [dict(name=t.name, started=t.ident is not None, alive=t.is_alive())
                         for t in state.get('threads', [])]
    observed = state.get('observed')
    result.update(queue_depth=None if observed is None else observed.qsize(),
                  queue_high_water=None if observed is None else observed.high_water)
    checkpoint_rows = list(state.get('checkpoints', []))
    result['checkpoints'] = dict(count=len(checkpoint_rows), last=checkpoint_rows[-1] if checkpoint_rows else None,
        busy_results=sum(row[0] != 0 for row in checkpoint_rows),
        max_log_pages=max([row[1] for row in checkpoint_rows]+[0]),
        max_checkpointed_pages=max([row[2] for row in checkpoint_rows]+[0]))
    result['checkpoint_wal_bytes_max'] = state.get('checkpoint_wal_bytes_max')
    result['thread_cpu_clock_resolution_seconds'] = time.get_clock_info('thread_time').resolution
    market, jobs = state.get('market'), state.get('jobs')
    for label, read in (
            ('market_transactions', lambda: market.store.transaction_diagnostics()),
            ('control_transactions', lambda: getattr(market.store.control, 'transaction_diagnostics',
                lambda: dict(available=False, reason='CONTROL_DIAGNOSTICS_API_UNAVAILABLE'))()),
            ('jobs_at_report', lambda: jobs.diagnostics(state['clock']()))):
        if (label.endswith('_transactions') and market is None) or (label == 'jobs_at_report' and jobs is None):
            result[label] = None
            continue
        try:
            result[label] = read()
        except BaseException as error:
            result['diagnostic_errors'].append(error_detail(label, error))
    result['session_finals'] = list(state.get('finals', []))
    if final and market is not None:
        try:
            counts = market.store.counts(market.epoch)
            with market.protocol.evidence.lock:
                received = {key: dict(value) for key, value in market.protocol.evidence.topics.items()}
            partition = []
            for name in sorted(received):
                row = dict(counts[name], received=received[name]['received'])
                row['conserved'] = row['received'] == sum(row[key] for key in
                    ('committed', 'duplicate', 'invalid', 'unprocessed_recorded'))
                partition.append(row)
            result['_topic_receipts'] = partition
            result['topic_receipt_summary'] = dict(topics=len(partition), records_sha256=audit_hash(partition),
                violation_count=sum(not row['conserved'] for row in partition),
                violations=[row for row in partition if not row['conserved']][:16],
                **{key:sum(row[key] for row in partition) for key in
                   ('received','committed','duplicate','invalid','unprocessed_recorded')})
            result['all_topic_receipts_conserved'] = all(row['conserved'] for row in partition)
            result['partition_after_all_workers_stopped'] = (not any(t.is_alive() for t in state.get('threads', []))
                and bool(state.get('work', {}).get('complete')))
        except BaseException as error:
            result['diagnostic_errors'].append(error_detail('topic_receipt_partition', error))
        try:
            with market.store.control._db(readonly=True, timeout=.05) as db:
                result['control_terminal_counts'] = dict(
                    owners=db.execute('SELECT COUNT(*) FROM owners').fetchone()[0],
                    requests=db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],
                    calls=db.execute('SELECT COUNT(*) FROM calls').fetchone()[0],
                    terminal_calls=db.execute('SELECT COUNT(terminal) FROM calls').fetchone()[0],
                    attempts=db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],
                    retired_attempts=db.execute("SELECT COUNT(*) FROM attempts WHERE terminal='RETIRED'").fetchone()[0])
        except BaseException as error:
            result['diagnostic_errors'].append(error_detail('control_terminal_counts', error))
    if final and result['diagnostic_errors']:
        result['status'] = 'FAILED'
    return result


def body_for(path, params):
    if path == '/api/v3/exchangeInfo':
        return directory([topic(symbol, '15m') for symbol in json.loads(params['symbols'])])
    if path == '/api/v3/klines':
        return fixture_rows(params['interval'], int(params['endTime']), int(params['limit']))
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
        from socketserver import TCPServer
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

        class LiteralLoopbackServer(ThreadingHTTPServer):
            def server_bind(self):
                # HTTPServer's default calls reverse DNS for server_name.
                # The fixture needs only its literal, already-bound address.
                TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address

        self.server = LiteralLoopbackServer(('127.0.0.1', 0), Handler)
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
        state = dict(wall_started=time.monotonic(), phase='PREPARATION')
        try:
            self._complete_1228_topics_during_continuous_wall_load(state)
        except BaseException as error:
            state['error'] = error_detail(state['phase'], error)
            raise
        finally:
            # This runs after post-drain assertions as well as early exceptions.
            # A diagnostic error must never replace the primary test failure.
            try:
                report = soak_drain_metrics(state)
                self.soak_report = report
                emit_soak_report(report)
                if not state.get('error'):
                    self.assertEqual(report['diagnostic_errors'], [], 'soak final evidence incomplete')
            except BaseException as error:
                emit_bounded('BINANCE_SOAK_DIAGNOSTIC_FAILED', error_detail('drain_report', error))
                if not state.get('error'):
                    raise

    def _complete_1228_topics_during_continuous_wall_load(self, state):
        names = topics(['BTCUSDT']+['S%03dUSDT' % i for i in range(408)])
        self.assertEqual(len(names), 1228)
        wall_started = state['wall_started']
        clock = lambda: MIDNIGHT+120000+int((time.monotonic()-wall_started)*1000)
        market = self.market('whole-roster', names=names, stamp=clock())
        state.update(market=market, clock=clock)
        with closing(sqlite3.connect(market.path)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        # Complete history is initialized through real control intents and real
        # market transactions. Fixture bodies have no HTTP admission claim.
        market.apply('directory', names, directory(names), clock())
        state.update(phase='HISTORY_FIXTURE_SEED', initialized_topics=0, initialized_rows=0)
        for name in names:
            stamp = clock()
            market.apply('seed', [name], fixture_rows(split_topic(name)[1], stamp), stamp)
            state['initialized_topics'] += 1
            state['initialized_rows'] += LIMITS[split_topic(name)[1]]
        state['phase'] = 'INITIAL_FULL_SCOPE_READ'
        market.frames(names, clock())
        def whole_read():
            now = dt.datetime.fromtimestamp(clock()/1000, dt.timezone.utc)
            return consumer.read(names, now, reader=market.store, clock=clock)
        seeded = whole_read()
        self.assertEqual(set(seeded.generation['windows']), set(names))
        expected_rows = sum(LIMITS[split_topic(n)[1]] for n in names)
        self.assertEqual(sum(len(w['rows']) for w in seeded.generation['windows'].values()), expected_rows)
        seed_seconds = time.monotonic()-wall_started
        state['seed_wall_seconds'] = seed_seconds
        self.assertLess(seed_seconds, 75, 'seed fixture exceeded freshness/CI preparation budget')
        stopped, producer_done, load_gate = Event(), Event(), Event()
        observed = stream.ObservedQueue(4096)
        errors, checkpoints, read_rejections, finals, thread_error_details = [], [], {}, [], []
        metrics = dict(rounds=0, queued=0, processed=0, max_ws_delay_ms=0, cumulative_ws_delay_ms=0,
                       full_ready_reads=0, reader_scope_size=1228, explicit_close_frames=0,
                       read_attempts=0, read_rejected=0,
                       crossed_boundaries={interval: 0 for interval in STEPS})
        histograms = dict(receipt_to_commit=LatencyHistogram('ms'),
                          receive_batch_wall=LatencyHistogram('us'), receive_batch_cpu=LatencyHistogram('us'),
                          consumer_read_and_delivery_wall=LatencyHistogram('us'),
                          consumer_read_and_delivery_cpu=LatencyHistogram('us'),
                          quiet_submit_to_done=LatencyHistogram('ms'))
        receipt_started, quiet_by_key, quiet_lock = {}, {}, Lock()
        load_started = deadline = None
        pool = ThreadPoolExecutor(max_workers=4)
        jobs = health.CoordinatedJobs(market.store, market.protocol, names, pool, clock,
                                     stopped.is_set, public.Client if sys.platform == 'linux' else BodyClient)
        jobs.seeded = set(names)
        state.update(metrics=metrics, errors=errors, thread_error_details=thread_error_details,
            checkpoints=checkpoints, read_rejections=read_rejections, finals=finals,
            observed=observed, jobs=jobs, load_started=load_started, histograms=histograms,
            quiet_by_key=quiet_by_key, quiet_lock=quiet_lock)
        state.update(read_intervals={}, unready_since=None, max_nonready_seconds=0)
        original_submit = jobs.submit

        def measured_submit(key, kind, scope):
            submitted = time.perf_counter_ns()
            queued_since = jobs.waiting.get(key)
            accepted = original_submit(key, kind, scope)
            if accepted and kind == 'quiet':
                if queued_since is not None:
                    state['quiet_candidate_to_submit_max_ms'] = max(state.get('quiet_candidate_to_submit_max_ms', 0),
                        clock()-queued_since)
                label = str(key)
                with quiet_lock:
                    summary = quiet_by_key.setdefault(label, dict(submitted=0, completed=0,
                        failed=0, cancelled=0, max_submit_to_done_ms=0))
                    summary['submitted'] += 1

                def completed(future):
                    elapsed = (time.perf_counter_ns()-submitted)//1000000
                    histograms['quiet_submit_to_done'].add(elapsed)
                    outcome = 'cancelled' if future.cancelled() else ('failed' if future.exception() else 'completed')
                    with quiet_lock:
                        summary[outcome] += 1
                        summary['max_submit_to_done_ms'] = max(summary['max_submit_to_done_ms'], elapsed)

                jobs.active[key][2].add_done_callback(completed)
            return accepted

        jobs.submit = measured_submit

        def producer():
            load_gate.wait()
            previous = {}
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
                        start = stamp//STEPS[interval]*STEPS[interval]
                        frames = []
                        if name in previous and previous[name] != start:
                            frames.append(message(symbol, interval, stamp-1, start=previous[name], closed=True))
                            metrics['explicit_close_frames'] += 1
                            metrics['crossed_boundaries'][interval] += 1
                        frames.append(message(symbol, interval, stamp))
                        for value in frames:
                            receipt_started[id(value)] = time.perf_counter_ns()
                            market.protocol.arrived(shard, json.dumps(value), stamp)
                            try:
                                observed.put((shard, value, stamp), timeout=.5)
                            except queue.Full:
                                receipt_started.pop(id(value), None)
                                market.store.unprocessed(market.epoch, [(value, stamp)], 'SOAK_QUEUE_FULL')
                                raise AssertionError('SOAK_QUEUE_FULL')
                            metrics['queued'] += 1
                        previous[name] = start
                        in_round += 1
                    if in_round == len(names):
                        metrics['rounds'] += 1
                    stopped.wait(max(0, .8-(time.monotonic()-round_started)))
            except BaseException as error:
                errors.append(('producer', type(error).__name__, str(error)))
                thread_error_details.append(error_detail('producer', error))
                stopped.set()
            finally:
                producer_done.set()

        def writer():
            load_gate.wait()
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
                    batch_started = time.perf_counter_ns()
                    batch_cpu_started = time.thread_time_ns()
                    market.protocol.receive_batch(batch)
                    committed = time.perf_counter_ns()
                    batch_cpu_finished = time.thread_time_ns()
                    histograms['receive_batch_cpu'].add((batch_cpu_finished-batch_cpu_started)//1000)
                    histograms['receive_batch_wall'].add((committed-batch_started)//1000)
                    for _, value, _ in batch:
                        histograms['receipt_to_commit'].add((committed-receipt_started.pop(id(value)))//1000000)
                    finished = clock()
                    delays = [finished-value[2] for value in batch]
                    metrics['processed'] += len(batch)
                    metrics['max_ws_delay_ms'] = max(metrics['max_ws_delay_ms'], max(delays))
                    metrics['cumulative_ws_delay_ms'] += sum(delays)
            except BaseException as error:
                errors.append(('writer', type(error).__name__, str(error)))
                thread_error_details.append(error_detail('writer', error))
                stopped.set()

        def checkpoint():
            load_gate.wait()
            try:
                with closing(sqlite3.connect(market.path, timeout=.25)) as db:
                    while not stopped.wait(.2):
                        checkpoints.append(tuple(db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()))
                        try:
                            size = Path(str(market.path)+'-wal').stat().st_size
                        except FileNotFoundError:
                            size = 0
                        state['checkpoint_wal_bytes_max'] = max(state.get('checkpoint_wal_bytes_max', 0), size)
            except BaseException as error:
                errors.append(('checkpoint', type(error).__name__, str(error)))
                thread_error_details.append(error_detail('checkpoint', error))
                stopped.set()

        threads = [Thread(target=producer, name='soak-producer'), Thread(target=writer, name='soak-writer'),
                   Thread(target=checkpoint, name='soak-checkpoint')]
        state['threads'] = threads
        next_work = 0
        next_heartbeat = None
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
                state['phase'] = 'CONTINUOUS_LOAD'
                for thread in threads:
                    thread.start()
                # Setup and thread creation are outside the promised load
                # duration. All three workers start from this shared gate.
                load_started = time.monotonic()
                deadline = load_started+LOAD_SECONDS
                next_heartbeat = load_started+60
                state.update(load_started=load_started, unready_since=load_started)
                load_gate.set()
                while time.monotonic() < deadline and not stopped.is_set():
                    now = clock()
                    jobs.tick(now)
                    quiet_waits = [now-started for key, started in jobs.waiting.items() if key[0] == 'quiet']
                    state['quiet_waiting_observed_max_ms'] = max([state.get('quiet_waiting_observed_max_ms', 0)]+quiet_waits)
                    elapsed = time.monotonic()-load_started
                    if elapsed >= next_work and len(jobs.active) < 4:
                        # Exercise real quiet/history publication alongside the
                        # runtime's directory round robin, without weakening its
                        # admission, 90s gate, source labels, or topic mutex.
                        name = topic('S%03dUSDT' % (int(next_work//5) % 408), '1d')
                        kind = 'quiet' if int(next_work//5) % 2 == 0 else 'repair'
                        if not any((k, name) in jobs.active for k in ('seed', 'repair', 'quiet')):
                            jobs.submit((kind, name), kind, [name])
                        next_work += 5
                    read_started = time.monotonic()
                    read_wall_started = time.perf_counter_ns()
                    read_cpu_started = time.thread_time_ns()
                    try:
                        period = str(clock()//STEPS['15m']*STEPS['15m'])
                        interval_read = state['read_intervals'].setdefault(period, dict(attempts=0, ready=0, rejected=0))
                        interval_read['attempts'] += 1
                        metrics['read_attempts'] += 1
                        frozen = whole_read()
                        self.assertEqual(set(frozen.generation['windows']), set(names))
                        frozen.check_delivery()
                        metrics['full_ready_reads'] += 1
                        interval_read['ready'] += 1
                        if state['unready_since'] is not None:
                            state['max_nonready_seconds'] = max(state['max_nonready_seconds'],
                                time.monotonic()-state['unready_since'])
                            state['unready_since'] = None
                    except (ValueError, ControlError) as error:
                        key = str(error).split(':', 1)[0]
                        read_rejections[key] = read_rejections.get(key, 0)+1
                        metrics['read_rejected'] += 1
                        interval_read['rejected'] += 1
                        if state['unready_since'] is None:
                            state['unready_since'] = read_started
                    finally:
                        histograms['consumer_read_and_delivery_wall'].add((time.perf_counter_ns()-read_wall_started)//1000)
                        histograms['consumer_read_and_delivery_cpu'].add((time.thread_time_ns()-read_cpu_started)//1000)
                    if time.monotonic() >= next_heartbeat:
                        report = soak_drain_metrics(state, final=False)
                        report['event'] = 'BINANCE_SOAK_HEARTBEAT'
                        emit_soak_report(report)
                        next_heartbeat += 60
                    if time.monotonic()-wall_started > LOAD_SECONDS+103:
                        raise AssertionError('SOAK_TOTAL_WALL_BUDGET')
                load_finished = time.monotonic()
            except BaseException as error:
                state['load_error'] = error_detail(state['phase'], error)
                emit_bounded('BINANCE_SOAK_INTERRUPTED', dict(metrics,
                    seed_wall_seconds=seed_seconds,
                    load_wall_seconds=None if load_started is None else time.monotonic()-load_started,
                    error_type=type(error).__name__, error=str(error),
                    control_phase=getattr(error, 'control_phase', None),
                    queue_depth=observed.qsize(), queue_high_water=observed.high_water,
                    thread_errors=errors, load_error=state['load_error']))
                raise
            finally:
                state.update(load_finished=time.monotonic(), phase='RECEIPT_DRAIN', drain_started=time.monotonic())
                stopped.set()
                load_gate.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(12)
                state.update(drain_finished=time.monotonic(), phase='JOBS_SHUTDOWN', jobs_shutdown_started=time.monotonic())
                work = None
                try:
                    work = jobs.shutdown(time.monotonic()+35)
                except BaseException as error:
                    state['jobs_shutdown_error'] = error_detail('jobs.shutdown', error)
                    work = dict(complete=False, errors=[state['jobs_shutdown_error']])
                    if state.get('load_error') is None:
                        raise
                finally:
                    state.update(work=work, jobs_shutdown_finished=time.monotonic())
            state['phase'] = 'POST_DRAIN_ASSERTIONS'
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
            self.assertEqual(histograms['receipt_to_commit'].snapshot()['count'], metrics['processed'])
            self.assertTrue(callable(getattr(market.store.control, 'transaction_diagnostics', None)),
                            'candidate requires real Control transaction diagnostics')
            if LOAD_SECONDS == 3600:
                self.assertGreaterEqual(metrics['crossed_boundaries']['15m'], 4*409)
                self.assertGreaterEqual(metrics['crossed_boundaries']['1h'], 409)
                self.assertGreaterEqual(len(state['read_intervals']), 5)
                self.assertTrue(all(value['ready'] > 0 for value in state['read_intervals'].values()),
                                'whole scope never recovered READY within a sampled 15m interval')
            # Final whole-scope proof is made after every writer/job has ended.
            # Refresh all directory evidence and only then take another complete
            # WS round, preserving the same original 1228-topic scope.
            state['phase'] = 'FINAL_FULL_SCOPE_READ'
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
        state['phase'] = 'COMPLETE'
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
        print('BINANCE_COMPLETE_ROSTER_WALL_SOAK '+json.dumps(metrics, sort_keys=True), flush=True)


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
        try:
            server = serve(websocket_handler, '127.0.0.1', 0, ping_interval=None, close_timeout=1)
        except BaseException:
            http.close()
            raise
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
            # Preserve the original child failure before the assertion stops
            # this test. Full phase histograms/tracebacks can exceed CI's line
            # limit; the existing chunk format retains their content hash.
            for index, value in enumerate(events):
                if value.get('event') == 'FIXTURE_PROCESS_FINAL' or index >= len(events)-4:
                    emit_bounded('BINANCE_RECONNECT_CHILD_AUDIT', dict(index=index, child_event=value))
            if stderr:
                emit_bounded('BINANCE_RECONNECT_CHILD_STDERR', dict(stderr=stderr))
            self.assertEqual(process.returncode, 0, 'collector failed; see bounded BINANCE_RECONNECT_CHILD_AUDIT/STDERR')
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
            emit_bounded('BINANCE_REAL_PROCESS_WS_RECONNECT', dict(summary,
                websocket_connections=len(connections), subscriptions=len(subscriptions),
                session_finals=len(session_finals), http_requests=http.requests))
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


def load_tests(loader, tests, pattern):
    # The final real-process reconnect must follow the load, including the
    # 3,600-second mode. Default class-name ordering would run it first.
    return unittest.TestSuite((loader.loadTestsFromTestCase(WholeRosterSoak),
                               loader.loadTestsFromTestCase(ReconnectProcess)))


if __name__ == '__main__':
    unittest.main()
