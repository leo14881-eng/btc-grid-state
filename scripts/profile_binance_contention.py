"""Diagnostic only: paired 8s writer/no-reader vs continuous-full-reader.

One fresh scratch seed; both legs restore the SAME SQLite backups and clone the
same in-memory Protocol receipt baseline. No HTTP, scheduler, sockets or child
processes. Optional full-reader legs compare revision copying, interpreter
scheduling or owned-copy codecs on the same runner and restored seed. This is
not the 32s/1h acceptance fixture or production evidence.
"""
import sys
sys.dont_write_bytecode = True
import argparse
import copy
import datetime as dt
from contextlib import ExitStack, closing, contextmanager, nullcontext
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import tempfile
from threading import Event, Lock, Thread, get_native_id
import time
import traceback
from urllib.parse import unquote, urlsplit
from unittest.mock import patch


BLOCKED = []


def audit(event, args):
    if (event.startswith(('socket.','os.exec','os.spawn'))
            or event in ('subprocess.Popen','os.system','os.posix_spawn','os.fork','os.forkpty')):
        BLOCKED.append(event)
        raise AssertionError('PAIRED_DIAGNOSTIC_FORBIDS_NETWORK_AND_CHILDREN:'+event)


sys.addaudithook(audit)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', required=True, type=Path)
parser.add_argument('--source-head', required=True, help='Caller-verified Git HEAD label; file hashes also recorded')
parser.add_argument('--probe-switch-interval', type=float, choices=(.0005,),
                    help='Diagnostic-only third full-reader leg; restores the original interval on exit')
parser.add_argument('--probe-snapshot-codec', action='store_true',
                    help='Diagnostic-only owned-copy codec oracle and full-reader leg at the original interval')
parser.add_argument('--probe-consumer-cost', action='store_true',
                    help='Measure actual Research/Monitor read segments on the initialized scratch cache')
parser.add_argument('--probe-revision-copy', action='store_true',
                    help='Compare the original row copier with the candidate on the same restored full-reader seed')
args = parser.parse_args()
repo = args.repo.resolve(strict=True)
if sys.platform != 'linux':
    raise SystemExit('LINUX_REQUIRED: this diagnostic has no portable execution path')
if repo != Path.cwd().resolve() or repo != Path(__file__).resolve().parents[1]:
    raise SystemExit('PROFILE_REQUIRES_CURRENT_CHECKOUT')
if (not re.fullmatch('[0-9a-f]{40}',args.source_head)
        or args.source_head != os.environ.get('CHECKOUT_SHA')):
    raise SystemExit('CHECKOUT_SHA_MUST_MATCH_SOURCE_HEAD_ARGUMENT')
sys.path.insert(0, str(repo))

from research import hunter_binance_health as health
from research import hunter_binance_stream as stream
from research import hunter_binance_stream_consumer as consumer
from research import hunter_binance_stream_store as market_store_module
from research.hunter_binance_health_store import Store, payload_hash
from research.hunter_binance_control import ControlError
from research.hunter_binance_stream_store import split_topic
from tests import test_hunter_binance_coordinated_runtime as runtime
from tests.test_hunter_binance_cache import rows, message
if args.probe_snapshot_codec:
    from tests.fixtures import binance_snapshot_codec_probe as codec_probe
if args.probe_consumer_cost:
    from tests.fixtures import binance_consumer_cost_probe as consumer_probe

SECONDS = 8
STAMP = runtime.MIDNIGHT + 120000
NAMES = runtime.topics(['BTCUSDT'] + ['S%03dUSDT' % i for i in range(408)])
assert len(NAMES) == 1228
MAX_RECORDS = 20000
TICKS_PER_SECOND = os.sysconf('SC_CLK_TCK')


def scratch_guard(root):
    def inside(value):
        if isinstance(value,int):
            return value in (1,2)  # Existing stdout/stderr are the only FD output targets.
        path=Path(os.fsdecode(value)).resolve()
        return path==root or root in path.parents

    def reject(event):
        BLOCKED.append(event)
        raise AssertionError('PAIRED_DIAGNOSTIC_WRITE_OUTSIDE_SCRATCH:'+event)

    def guarded(event,values):
        if event=='open':
            path,mode,flags=values
            writing=(isinstance(mode,str) and any(m in mode for m in ('w','a','x','+'))
                     or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
            if writing and not inside(path):reject(event)
        elif event=='sqlite3.connect':
            address=os.fsdecode(values[0])
            if address.startswith('file:'):address=unquote(urlsplit(address).path)
            if not inside(address):reject(event)
        elif event in ('os.mkdir','os.rmdir','os.remove','os.chmod','os.chown','os.utime','os.truncate'):
            if not inside(values[0]):reject(event)
        elif event in ('os.rename','os.link','os.symlink'):
            if not inside(values[0]) or not inside(values[1]):reject(event)
    sys.addaudithook(guarded)


def append_bounded(target,value):
    if len(target)>=MAX_RECORDS:
        raise AssertionError('PAIRED_DIAGNOSTIC_RECORD_BOUND')
    target.append(value)


def task_cpu(native_id):
    # Reading proc is safe after thread exit; pthread_getcpuclockid on an expired
    # thread ID is not. Check starttime as well, so a recycled TID is never used.
    fields=Path('/proc/self/task/%d/stat'%native_id).read_text().rsplit(')',1)[1].split()
    return int(fields[11])+int(fields[12]),int(fields[19])


def clone_protocol(original, store):
    clone = object.__new__(type(original))
    clone.__dict__.update({k:copy.deepcopy(v) for k,v in original.__dict__.items()
                           if k not in ('store','lock','evidence')})
    clone.store = store
    clone.lock = Lock()
    clone.evidence = object.__new__(type(original.evidence))
    clone.evidence.__dict__.update({k:copy.deepcopy(v) for k,v in original.evidence.__dict__.items()
                                    if k != 'lock'})
    clone.evidence.lock = Lock()
    assert clone.epoch == original.epoch
    return clone


def backup(source, target):
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def distribution(values):
    ordered = sorted(values)
    return dict(count=len(ordered), total=sum(ordered), maximum=max(ordered,default=0),
                p99=ordered[max(0,(len(ordered)*99+99)//100-1)] if ordered else None,
                unit='microseconds')


class AccountingQueue(stream.ObservedQueue):
    """Count acceptance/dequeue under the existing Queue mutex."""
    def __init__(self, capacity):
        super().__init__(capacity)
        self.accepted = self.dequeued = 0

    def _put(self, item):
        super()._put(item)
        self.accepted += 1

    def _get(self):
        item = super()._get()
        self.dequeued += 1
        return item

    def counters(self):
        with self.mutex:
            return dict(queued=self.accepted, dequeued=self.dequeued,
                        qdepth=self._qsize(), high_water=self.high_water)


@contextmanager
def phases(result):
    # Only per-read boundaries. Inclusive/nested measurements MUST NOT be added.
    guard = Lock()
    def wrap(original, label):
        @wraps(original)
        def measured(*pos, **kwargs):
            wall = time.perf_counter_ns(); cpu = time.thread_time_ns()
            failed = True
            try:
                value = original(*pos, **kwargs)
                failed = False
                return value
            finally:
                elapsed = (time.perf_counter_ns()-wall)//1000
                used = (time.thread_time_ns()-cpu)//1000
                with guard:
                    item = result.setdefault(label, dict(count=0, exceptions=0,
                        wall_total_us=0, wall_max_us=0, thread_cpu_total_us=0,
                        thread_cpu_max_us=0))
                    item['count'] += 1; item['exceptions'] += int(failed)
                    item['wall_total_us'] += elapsed; item['thread_cpu_total_us'] += used
                    item['wall_max_us'] = max(item['wall_max_us'], elapsed)
                    item['thread_cpu_max_us'] = max(item['thread_cpu_max_us'], used)
        return measured
    targets = ((Store, '_freeze_snapshot'), (Store, 'freeze'),
               (consumer, '_encode_json_tree'), (consumer.Frozen, '__init__'),
               (consumer.Frozen, '_validate_at'), (consumer.Frozen, 'check_delivery'))
    with ExitStack() as stack:
        for owner, name in targets:
            label = ('consumer' if owner is consumer else owner.__name__)+'.'+name
            stack.enter_context(patch.object(owner, name, wrap(getattr(owner, name), label)))
        yield


class QueueSaturated(RuntimeError):
    pass


@contextmanager
def original_revision_copy():
    # Only row extraction from e8b0b8f5 changes. Both sides use the candidate's
    # query/transaction and post-transaction map assembly/validation/fences.
    # This does not replay the complete earlier freeze implementation.
    with patch.object(market_store_module, '_copy_revision_rows',
                      market_store_module._copy_revision_rows_legacy):
        yield


def run_leg(market, seed_root, with_reader):
    # No prior thread may survive into restoration. Backup writes the existing
    # files in place: control's legacy-path/dev/inode binding remains identical.
    identities = {name: (market.path.with_name(name).stat().st_dev,
                         market.path.with_name(name).stat().st_ino)
                  for name in ('state.sqlite3', 'control.sqlite3', 'control.sqlite3.gate')}
    for filename in ('state.sqlite3', 'control.sqlite3'):
        target = market.path.with_name(filename)
        with closing(sqlite3.connect(target)) as db:
            assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        backup(seed_root/filename, target)
    assert identities == {name: (market.path.with_name(name).stat().st_dev,
                                 market.path.with_name(name).stat().st_ino)
                          for name in identities}, 'RESTORE_REPLACED_IDENTITY'
    assert market.path.with_name('control.sqlite3.gate').read_bytes() == b'0'
    store = Store.reader(market.path)
    store.enable_control()
    store.set_control_source(runtime.HEAD)
    protocol = clone_protocol(market.protocol, store)
    assert store.metadata()['epoch'] == protocol.epoch
    with store.control._db(readonly=True) as db:
        meta = dict(db.execute('SELECT key,value FROM meta'))
        assert meta['epoch'] == protocol.epoch and meta['phase'] == 'ACTIVE'
        assert meta['source_head'] == runtime.HEAD and meta['manifest_sha256'] == runtime.MANIFEST
        assert all(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] == 0
                   for table in ('owners', 'requests', 'calls', 'attempts', 'quarantines'))
    consumer.read(NAMES, dt.datetime.fromtimestamp((STAMP+1)/1000, dt.timezone.utc),
                  reader=store, clock=lambda: STAMP+1).check_delivery()
    with store.db(readonly=True) as db:
        assert db.execute('SELECT COUNT(*) FROM bn_unprocessed WHERE epoch=? AND reason=?',
                          (protocol.epoch, 'PAIRED_QUEUE_FULL')).fetchone()[0] == 0
    initial_received = sum(item['received'] for item in protocol.evidence.topics.values())
    initial_raw = protocol.raw_frames

    observed = AccountingQueue(4096)
    stop, produced, gate = Event(), Event(), Event()
    metrics = Lock()
    received_at = {}
    errors, batch_wall, batch_cpu, delivery_latency, checkpoints = [], [], [], [], []
    batches, samples, read_wall, read_cpu, phase_totals = [], [], [], [], {}
    capacity_refusals = []
    read_rejections, thread_states = {}, {}
    totals = dict(rounds=0, producer_received=0, processed=0, read_attempts=0, ready=0,
                  queue_full_unprocessed=0, sampled_wal_peak=0)
    started = deadline = process_started = None

    def clock():
        return STAMP+2+int((time.monotonic()-started)*1000)

    def relative_us():
        return int((time.monotonic()-started)*1_000_000)

    def fail(where, error):
        with metrics:
            append_bounded(errors, dict(operation=where, type=type(error).__name__,
                message=str(error), expected_queue_full=isinstance(error, QueueSaturated),
                sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
                sqlite_errorname=getattr(error, 'sqlite_errorname', None),
                traceback=''.join(traceback.format_exception(type(error), error, error.__traceback__))))
        stop.set()

    def sample_wal():
        try:
            size = Path(str(market.path)+'-wal').stat().st_size
        except FileNotFoundError:
            size = 0
        with metrics:
            totals['sampled_wal_peak'] = max(totals['sampled_wal_peak'], size)
        return size

    def cpu_samples():
        with metrics:
            states = {name: dict(value) for name, value in thread_states.items()}
        result = {}
        for name, state in states.items():
            if state.get('done'):
                result[name] = dict(status='finished_exact', cpu_us=state['cpu_total_us'])
                continue
            try:
                ticks, identity = task_cpu(state['native_id'])
                if identity != state['proc_starttime']:
                    raise RuntimeError('PROC_THREAD_ID_RECYCLED')
                result[name] = dict(status='proc_ticks', cpu_us=int(
                    (ticks-state['proc_ticks_at_registration'])*1_000_000/TICKS_PER_SECOND))
            except (FileNotFoundError, ProcessLookupError):
                # Exit can race this read. Never report a missing thread as zero.
                with metrics:
                    final = dict(thread_states[name])
                result[name] = (dict(status='finished_exact', cpu_us=final['cpu_total_us'])
                                if final.get('done') else dict(status='unavailable', cpu_us=None))
        return result

    def sample(label, checkpoint_result=None):
        sample_start = relative_us()
        counters = observed.counters()
        with metrics:
            snapshot = dict(totals)
        value = dict(t_us=sample_start, label=label, **counters,
            processed=snapshot['processed'], producer_received=snapshot['producer_received'],
            producer_accepted=counters['queued'], read_attempts=snapshot['read_attempts'],
            ready=snapshot['ready'], wal_bytes=sample_wal(),
            process_cpu_us=(time.process_time_ns()-process_started)//1000,
            thread_cpu=cpu_samples(), checkpoint=checkpoint_result)
        value['sample_end_us'] = relative_us()
        with metrics:
            append_bounded(samples, value)
        return value

    def producer():
        try:
            while not stop.is_set() and time.monotonic() < deadline:
                round_started = time.monotonic(); stamp = clock(); completed = 0
                for name in NAMES:
                    if stop.is_set() or time.monotonic() >= deadline:
                        break
                    symbol, interval = split_topic(name)
                    value = message(symbol, interval, stamp)
                    shard = protocol.topic_shards[name]
                    received_at[id(value)] = time.perf_counter_ns()
                    protocol.arrived(shard, json.dumps(value), stamp)
                    with metrics:
                        totals['producer_received'] += 1
                    try:
                        observed.put((shard, value, stamp), timeout=.5)
                    except queue.Full:
                        received_at.pop(id(value), None)
                        with metrics:
                            append_bounded(capacity_refusals, dict(topic=name, received_at=stamp,
                                open_time=value['k']['t'], event_time=value['E'],
                                payload_sha256=payload_hash(value), reason='PAIRED_QUEUE_FULL',
                                category='unprocessed_recorded'))
                        store.unprocessed(protocol.epoch, [(value, stamp)], 'PAIRED_QUEUE_FULL')
                        with metrics:
                            totals['queue_full_unprocessed'] += 1
                        raise QueueSaturated('PAIRED_QUEUE_FULL')
                    completed += 1
                if completed == len(NAMES):
                    with metrics:
                        totals['rounds'] += 1
                stop.wait(max(0, .8-(time.monotonic()-round_started)))
        finally:
            produced.set()

    def writer():
        while not produced.is_set() or not observed.empty():
            try:
                batch = [observed.get(timeout=.05)]
            except queue.Empty:
                continue
            while len(batch) < 100:
                try:
                    batch.append(observed.get_nowait())
                except queue.Empty:
                    break
            wall = time.perf_counter_ns(); cpu = time.thread_time_ns()
            record = dict(start_us=relative_us(), size=len(batch), outcome='ERROR')
            try:
                protocol.receive_batch(batch)
                committed = time.perf_counter_ns()
                record['outcome'] = 'PROCESSED'
                for _, value, _ in batch:
                    append_bounded(delivery_latency, (committed-received_at.pop(id(value)))//1000)
                with metrics:
                    totals['processed'] += len(batch)
            finally:
                record.update(end_us=relative_us(), wall_us=(time.perf_counter_ns()-wall)//1000,
                              thread_cpu_us=(time.thread_time_ns()-cpu)//1000)
                append_bounded(batches, record)
                append_bounded(batch_cpu, record['thread_cpu_us'])
                append_bounded(batch_wall, record['wall_us'])

    def checkpoint():
        with closing(sqlite3.connect(market.path, timeout=.25)) as db:
            next_sample = started+.2
            while not stop.wait(max(0, next_sample-time.monotonic())):
                begin = relative_us(); sample_wal()
                result = tuple(db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())
                append_bounded(checkpoints, dict(start_us=begin, end_us=relative_us(),
                    busy=result[0], log_frames=result[1], checkpointed_frames=result[2]))
                sample('periodic', result)
                # Missed intervals are recorded as gaps, not replayed in a burst.
                next_sample = max(next_sample+.2, time.monotonic()+.001)

    def reader():
        while not stop.is_set() and time.monotonic() < deadline:
            wall = time.perf_counter_ns(); cpu = time.thread_time_ns()
            with metrics:
                totals['read_attempts'] += 1
            try:
                frozen = consumer.read(NAMES, dt.datetime.fromtimestamp(clock()/1000, dt.timezone.utc),
                                       reader=store, clock=clock)
                assert set(frozen.generation['windows']) == set(NAMES)
                frozen.check_delivery()
                with metrics:
                    totals['ready'] += 1
            except (consumer.CacheReadError, ControlError) as error:
                reason = str(error)
                # Structured fail-closed read outcomes are evidence, not successful
                # delivery. All other errors (including invariant failures) are fatal.
                with metrics:
                    capacity_revoked = reason in (
                        'BINANCE_FINAL_TOPIC_REVOKED',
                        'BINANCE_CACHE_UNKNOWN:BINANCE_UNRECONCILED_RECEIPT') and bool(capacity_refusals)
                # freeze() can convert SQLite errors into UNKNOWN. Only the
                # exact receipt denial caused by this leg's capacity refusal
                # is expected here; generic UNAVAILABLE/CORRUPT remains fatal.
                if not (capacity_revoked or reason in
                        ('CONTROL_PUBLICATION_PENDING', 'CONTROL_READ_FENCE_CHANGED')):
                    raise
                read_rejections[reason] = read_rejections.get(reason, 0)+1
            finally:
                append_bounded(read_cpu, (time.thread_time_ns()-cpu)//1000)
                append_bounded(read_wall, (time.perf_counter_ns()-wall)//1000)

    ready_events = []
    def make_thread(name, operation):
        ready = Event(); ready_events.append(ready)
        def run():
            cpu_start = None
            try:
                native = get_native_id(); ticks, identity = task_cpu(native)
                with metrics:
                    thread_states[name] = dict(native_id=native, proc_starttime=identity,
                        proc_ticks_at_registration=ticks, done=False)
                ready.set(); gate.wait(); cpu_start = time.thread_time_ns()
                operation()
            except BaseException as error:
                fail(name, error)
            finally:
                if name == 'producer':
                    produced.set()
                if cpu_start is not None:
                    with metrics:
                        thread_states[name].update(done=True,
                            cpu_total_us=(time.thread_time_ns()-cpu_start)//1000)
                ready.set()
        return Thread(target=run, name='paired-'+name, daemon=True)

    threads = [make_thread('producer', producer), make_thread('writer', writer),
               make_thread('checkpoint', checkpoint)]
    if with_reader:
        threads.append(make_thread('reader', reader))
    with phases(phase_totals):
        for thread in threads:
            thread.start()
        for ready in ready_events:
            if not ready.wait(5):
                fail('thread_start', RuntimeError('THREAD_NOT_READY'))
        started = time.monotonic(); deadline = started+SECONDS
        process_started = time.process_time_ns(); gate.set()
        sample('load_start')
        stop.wait(max(0, deadline-time.monotonic()))
        load_finished = time.monotonic(); load_process_cpu = time.process_time_ns()-process_started
        stop.set(); load_stop_observation = sample('load_stop')
        join_deadline = time.monotonic()+15
        for thread in threads:
            thread.join(max(0, join_deadline-time.monotonic()))
        alive = [thread.name for thread in threads if thread.is_alive()]
    # Do not inspect DBs or restore the next leg while a previous thread is live.
    if alive:
        return dict(with_full_reader=with_reader, target_seconds=SECONDS,
            load_seconds=load_finished-started, fatal='DO_NOT_RESTORE_WITH_LIVE_THREADS',
            live_threads=alive, errors=errors, samples=sorted(samples, key=lambda x:x['t_us']),
            batches=batches, per_topic_conserved=False, raw_frames_conserved=False)
    if protocol.inflight or not observed.empty():
        try:
            health.drain(protocol, observed, time.monotonic()+5)
        except BaseException as error:
            fail('shutdown_drain', error)
    drain_end_observation = sample('drain_end')
    counts = store.counts(protocol.epoch)
    with store.db(readonly=True) as db:
        capacity_receipts = [dict(row) for row in db.execute('''SELECT topic,received_at,open_time,
            event_time,payload_sha256,reason,category FROM bn_unprocessed
            WHERE epoch=? AND reason=? ORDER BY id''', (protocol.epoch, 'PAIRED_QUEUE_FULL'))]
    capacity_conserved = (capacity_receipts == capacity_refusals
        and len(capacity_refusals) == totals['queue_full_unprocessed'])
    receipts = []
    for name, seen in protocol.evidence.topics.items():
        count = counts[name]
        receipts.append(dict(topic=name, received=seen['received'],
            **{k:count[k] for k in ('committed', 'duplicate', 'invalid', 'unprocessed_recorded')},
            conserved=seen['received'] == sum(count[k] for k in
                ('committed', 'duplicate', 'invalid', 'unprocessed_recorded'))))
    conserved = all(item['conserved'] for item in receipts)
    raw_conserved = protocol.raw_frames == (protocol.control_receipts+protocol.invalid_envelopes
                                           +sum(item['received'] for item in receipts))
    queue_final = observed.counters()
    producer_conserved = (capacity_conserved and
        totals['producer_received'] == queue_final['queued']+totals['queue_full_unprocessed']
        == sum(item['received'] for item in receipts)-initial_received
        == protocol.raw_frames-initial_raw)
    per_thread = {name: dict(thread_states.get(name, {'status':'not_started'}))
                  for name in ('producer', 'writer', 'checkpoint', 'reader')}
    report = dict(with_full_reader=with_reader, target_seconds=SECONDS,
        switch_interval_seconds=sys.getswitchinterval(),
        load_seconds=load_finished-started, reached_target_seconds=load_finished >= deadline,
        total_seconds=time.monotonic()-started, epoch=protocol.epoch, totals=totals,
        process_cpu_load_us=load_process_cpu//1000,
        process_cpu_total_us=(time.process_time_ns()-process_started)//1000,
        per_thread_total=per_thread, errors=errors, live_threads=[],
        load_stop_observation=load_stop_observation, drain_end_observation=drain_end_observation,
        restored_file_identity=identities, queue_capacity=4096, queue_final=queue_final,
        queue_high_water=observed.high_water, batch_limit=100, round_interval_seconds=.8,
        queue_put_timeout_seconds=.5, batch_wall=distribution(batch_wall),
        batch_cpu=distribution(batch_cpu), batches=batches,
        receipt_to_commit=distribution(delivery_latency), read_wall=distribution(read_wall),
        read_cpu=distribution(read_cpu), read_rejections=read_rejections,
        phase_totals=phase_totals, phase_metric_semantics='INCLUSIVE_NESTED_DO_NOT_SUM; current calling thread CPU; seed/warmup excluded',
        checkpoint_interval_seconds=.2, checkpoints=checkpoints,
        samples=sorted(samples, key=lambda item:item['t_us']),
        sample_semantics='Actual elapsed timestamps; sampled fields are not a cross-thread atomic snapshot; qdepth excludes dequeued/inflight; proc ticks since thread registration; finished CPU uses exact thread_time_ns since gate',
        proc_cpu_tick_seconds=1/TICKS_PER_SECOND,
        sampled_wal_peak_bytes=totals['sampled_wal_peak'],
        per_topic_conserved=conserved, raw_frames_conserved=raw_conserved,
        producer_conserved=producer_conserved, initial_received=initial_received,
        capacity_refusals=capacity_refusals, capacity_receipts=capacity_receipts,
        capacity_refusal_receipts_match=capacity_conserved,
        final_topic_revoked_explained=(not read_rejections.get('BINANCE_FINAL_TOPIC_REVOKED')
            or bool(capacity_refusals) and capacity_conserved and conserved and raw_conserved),
        initial_raw_frames=initial_raw, receipts=receipts,
        transaction_metrics_scope='Fresh Store per leg, including pre-load identity/warmup checks and final receipt reads; not seed or prior leg',
        market_transactions=store.transaction_diagnostics(),
        control_transactions=store.control.transaction_diagnostics())
    print('PAIRED8_LEG', json.dumps({k:v for k,v in report.items() if k not in
        ('receipts', 'market_transactions', 'control_transactions', 'checkpoints', 'batches', 'samples')},
        sort_keys=True), flush=True)
    return report


def main():
    original_switch_interval = sys.getswitchinterval()
    scratch = Path(tempfile.mkdtemp(prefix='binance-paired8-')).resolve()
    scratch_guard(scratch)
    active = scratch/'active'; seed_root = scratch/'seed'
    active.mkdir(); seed_root.mkdir()
    # Include every loaded repository Python dependency, plus this script. The
    # external checkout action supplies the SHA; this script never spawns git.
    source_files = {'scripts/profile_binance_contention.py'}
    # Hash lazy-loaded research helpers before running any fixture as well.
    source_files.update(path.relative_to(repo).as_posix() for path in (repo/'research').glob('*.py'))
    if args.probe_consumer_cost:
        source_files.add('research/results/hunter-shadow-rules.json')
    for module in list(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if filename:
            path = Path(filename).resolve()
            if repo in path.parents and path.suffix == '.py':
                source_files.add(path.relative_to(repo).as_posix())
    def hashes():
        return {name:hashlib.sha256((repo/name).read_bytes()).hexdigest()
                for name in sorted(source_files)}
    source_hashes = hashes()
    target = scratch/'paired8-report.json'
    print('PAIRED8_START', json.dumps(dict(scratch=str(scratch), report=str(target),
        checkout_source_head=args.source_head, source_head_authority='CHECKOUT_SHA environment equals CLI; checkout action must pin this SHA',
        source_file_count=len(source_hashes), scope='DIAGNOSTIC_NO_HTTP_NO_SCHEDULER_NOT_ACCEPTANCE',
        fixed_market_stamp=STAMP)), flush=True)
    report = dict(scope='DIAGNOSTIC_NO_HTTP_NO_SCHEDULER_NOT_ACCEPTANCE', python=sys.version,
        sqlite=sqlite3.sqlite_version, switch_interval_seconds=original_switch_interval,
        probe_switch_interval_seconds=args.probe_switch_interval,
        probe_snapshot_codec=args.probe_snapshot_codec,
        probe_consumer_cost=args.probe_consumer_cost,
        probe_revision_copy=args.probe_revision_copy,
        checkout_source_head=args.source_head, source_file_sha256_before=source_hashes,
        fixed_market_stamp=STAMP, fixture_source_head=runtime.HEAD,
        fixture_manifest=runtime.MANIFEST, fixture_identity_is_synthetic=True,
        comparison_limitations='Fixed no-reader, optional original-revision-copy reader, then candidate full-reader order, followed by optional scheduling/copy-codec probes; same runner/seed but OS page cache is not cooled; instrumented; neither diagnostic improvement nor copy speed alone establishes acceptance',
        legs=[], fatal_errors=[])
    try:
        started = time.monotonic()
        market = runtime.Market(active/'state.sqlite3', names=NAMES, stamp=STAMP-1)
        with closing(sqlite3.connect(market.path)) as db:
            assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        market.apply('directory', NAMES, runtime.directory(NAMES), STAMP)
        for name in NAMES:
            market.apply('seed', [name], rows(split_topic(name)[1], STAMP), STAMP)
        market.frames(NAMES, STAMP+1)
        initial = market.read(NAMES, STAMP+1)
        assert len(initial.generation['windows']) == 1228
        initial.check_delivery()
        report['initialization_seconds_before_optional_probes'] = time.monotonic()-started
        if args.probe_revision_copy:
            with original_revision_copy():
                original = market.read(NAMES, STAMP+1)
                original.check_delivery()
            assert original.generation == initial.generation, 'REVISION_COPY_GENERATION_CHANGED'
            assert original.fence_sha256 == initial.fence_sha256, 'REVISION_COPY_FENCE_CHANGED'
            report['revision_copy_probe'] = dict(
                original_copy_head='e8b0b8f5f7aad66147a31164d5779810bb4ff909',
                scope='REVISION_COPY_ONLY_SAME_QUERY_TRANSACTION_AND_VALIDATION',
                shared_map_assembly='AFTER_READ_TRANSACTION_ON_BOTH_SIDES',
                initial_generation_equal=True, initial_fence_equal=True,
                generation_sha256=initial.generation['generation_sha256'])
        if args.probe_snapshot_codec:
            report['snapshot_codec_probe'] = codec_probe.run_codec_probe(initial.generation)
        if args.probe_consumer_cost:
            with consumer_probe.original_formula_delivery():
                report['formula_cost_baseline'] = consumer_probe.run_consumer_cost_probe(
                    market, stamp=STAMP+1, scratch_root=scratch)
            with consumer_probe.original_delivery_queries():
                report['consumer_cost_baseline'] = consumer_probe.run_consumer_cost_probe(
                    market, stamp=STAMP+1, scratch_root=scratch)
            report['consumer_cost_probe'] = consumer_probe.run_consumer_cost_probe(
                market, stamp=STAMP+1, scratch_root=scratch)
            report['delivery_query_comparison'] = consumer_probe.compare_delivery_cost(
                report['consumer_cost_baseline'], report['consumer_cost_probe'])
            report['formula_boundary_comparison'] = consumer_probe.compare_formula_cost(
                report['formula_cost_baseline'], report['consumer_cost_probe'])
            if not report['delivery_query_comparison']['measured_segments_not_slower']:
                report['fatal_errors'].append('MEASURED_CONSUMER_SEGMENT_PERFORMANCE_REGRESSION')
        for filename in ('state.sqlite3', 'control.sqlite3'):
            source = active/filename
            with closing(sqlite3.connect(source)) as db:
                assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
            backup(source, seed_root/filename)
        report.update(seed_seconds=time.monotonic()-started, epoch=market.epoch,
            seed_seconds_semantics='PRE_LOAD_SETUP_INCLUDING_OPTIONAL_PROBES_AND_BACKUP_NOT_PURE_INITIALIZATION',
            seed_file_sha256={name:hashlib.sha256((seed_root/name).read_bytes()).hexdigest()
                              for name in ('state.sqlite3', 'control.sqlite3')})
        conditions = [(False, original_switch_interval, 'json', 'candidate')]
        if args.probe_revision_copy:
            conditions.append((True, original_switch_interval, 'json', 'original_rows'))
        conditions.append((True, original_switch_interval, 'json', 'candidate'))
        if args.probe_switch_interval is not None:
            conditions.append((True, args.probe_switch_interval, 'json', 'candidate'))
        if args.probe_snapshot_codec:
            conditions.append((True, original_switch_interval, 'marshal_owned_memory', 'candidate'))
        for with_reader, switch_interval, codec, revision_copy in conditions:
            prior_error_count = len(report['fatal_errors'])
            sys.setswitchinterval(switch_interval)
            with (codec_probe.installed_codec() if codec != 'json' else nullcontext()), \
                    (original_revision_copy() if revision_copy == 'original_rows' else nullcontext()):
                leg = run_leg(market, seed_root, with_reader)
            leg['snapshot_copy_codec'] = codec
            leg['revision_copy'] = revision_copy
            report['legs'].append(leg)
            if leg.get('live_threads'):
                report['fatal_errors'].append('LIVE_THREADS')
                break
            if not all(leg.get(key) for key in
                       ('per_topic_conserved', 'raw_frames_conserved', 'producer_conserved',
                        'capacity_refusal_receipts_match', 'final_topic_revoked_explained')):
                report['fatal_errors'].append('RECEIPT_CONSERVATION_FAILED')
            if any(not item['expected_queue_full'] for item in leg['errors']):
                report['fatal_errors'].append('NON_QUEUE_FULL_EXECUTION_ERROR')
            if (not leg['reached_target_seconds']
                    and not any(item['expected_queue_full'] for item in leg['errors'])):
                report['fatal_errors'].append('EARLY_STOP_WITHOUT_QUEUE_FULL')
            current_seed = {name:hashlib.sha256((seed_root/name).read_bytes()).hexdigest()
                            for name in ('state.sqlite3', 'control.sqlite3')}
            if current_seed != report['seed_file_sha256']:
                report['fatal_errors'].append('SEED_CHANGED')
            # Keep a prior consumer-cost failure red, but finish independent
            # load observations. Only a new leg safety/execution failure stops
            # the next leg; no acceptance condition is removed.
            if len(report['fatal_errors']) > prior_error_count:
                break
    except BaseException as error:
        if hasattr(error, 'consumer_cost_probe_report'):
            report['consumer_cost_probe'] = error.consumer_cost_probe_report
        report['fatal_errors'].append(dict(type=type(error).__name__, message=str(error),
            traceback=''.join(traceback.format_exception(type(error), error, error.__traceback__))))
    finally:
        sys.setswitchinterval(original_switch_interval)
        report['restored_switch_interval_seconds'] = sys.getswitchinterval()
        report['source_file_sha256_after'] = hashes()
        report['source_files_unchanged'] = source_hashes == report['source_file_sha256_after']
        report['blocked_audit_events'] = list(BLOCKED)
        if not report['source_files_unchanged']:
            report['fatal_errors'].append('SOURCE_CHANGED_DURING_PROFILE')
        if BLOCKED:
            report['fatal_errors'].append('AUDIT_GUARD_BLOCKED_SIDE_EFFECT')
        if len(report['legs']) != (2+int(args.probe_switch_interval is not None)
                                  +int(args.probe_snapshot_codec)+int(args.probe_revision_copy)):
            report['fatal_errors'].append('PAIR_INCOMPLETE')
        report['diagnostic_completed_without_fatal_error'] = not report['fatal_errors']
        target.write_text(json.dumps(report, sort_keys=True, indent=2), encoding='utf-8')
        print('PAIRED8_REPORT', str(target), flush=True)
        print('PAIRED8_END', json.dumps(dict(checkout_source_head=args.source_head,
            diagnostic_completed_without_fatal_error=report['diagnostic_completed_without_fatal_error'],
            fatal_errors=report['fatal_errors'], blocked_audit_events=BLOCKED)), flush=True)
    if report['fatal_errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
