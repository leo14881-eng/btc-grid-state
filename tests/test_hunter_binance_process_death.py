"""Observed Linux SIGKILL semantics, not a claim of crash-complete receipts.

Only the dedicated guarded CI runner should execute these five tests. Real
process death/flock/SQLite are used; expiry uses an injected millisecond clock,
not a real 90-second wait. No service, HTTP, production path or selector runs.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_control import NativeLock
from research.hunter_binance_health_store import Store, payload_hash
from research.hunter_binance_stream_store import FRESH_MS, STEPS, split_topic, topic, roster_hash
from tests.test_hunter_binance_cache import message as fixture_message, scan as fixture_scan

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT/'tests/fixtures/binance_process_death.py'
OUTCOMES = ('committed', 'duplicate', 'invalid', 'unprocessed_recorded')
SOURCES = ('research/hunter_binance_stream.py', 'research/hunter_binance_health.py',
           'research/hunter_bybit_stream_store.py',
           'research/hunter_binance_health_store.py', 'research/hunter_binance_stream_store.py',
           'research/hunter_binance_market_control.py', 'research/hunter_binance_control.py',
           'research/hunter_binance_stream_consumer.py', 'tests/fixtures/binance_process_death.py',
           'tests/test_hunter_binance_process_death.py',
           'tests/test_hunter_binance_coordinated_runtime.py', 'tests/test_hunter_binance_cache.py')


def source_hashes():
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


@unittest.skipUnless(sys.platform == 'linux', 'real Linux SIGKILL/flock required; skips are not acceptance')
class ProcessDeathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='binance-process-death-')
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name).resolve()
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def event(self, child, expected):
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(20), 'CHILD_HANDSHAKE_TIMEOUT:'+expected)
        line = child.stdout.readline()
        if not line:
            self.fail('CHILD_HANDSHAKE_EOF:'+expected)
        value = json.loads(line)
        self.assertEqual(value['event'], expected)
        self.assertEqual(value['pid'], child.pid)
        self.assertEqual(value['blocked_effects'], 0)
        return value

    def state(self, store):
        with store.db(readonly=True) as db:
            market = dict(meta=dict(db.execute('SELECT key,value FROM meta')),
                topics=[dict(r) for r in db.execute('SELECT * FROM topics ORDER BY topic')],
                counts=[dict(r) for r in db.execute('SELECT * FROM bn_counts ORDER BY epoch,topic')],
                receipts=[dict(r) for r in db.execute('SELECT * FROM bn_unprocessed ORDER BY id')])
        with store.control._db(readonly=True) as db:
            control = dict(meta=dict(db.execute('SELECT key,value FROM meta')),
                budget=dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone()),
                owners=[dict(r) for r in db.execute('SELECT * FROM owners ORDER BY token')],
                versions=list(map(tuple, db.execute('SELECT topic,version FROM versions ORDER BY topic'))))
        return dict(market=market, control=control, status=store.control.status())

    @staticmethod
    def error_result(error):
        return dict(accepted=False, reason=str(error), error_type=type(error).__name__,
            sqlite_error=isinstance(error, sqlite3.Error),
            sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
            sqlite_errorname=getattr(error, 'sqlite_errorname', None),
            sqlite_context=getattr(error, 'sqlite_context', None), traceback=traceback.format_exc())

    @staticmethod
    def binding(value):
        generation = value.generation
        return dict(names=sorted(generation['windows']), epoch=generation['epoch'],
            generation_end_ms=generation['generation_end_ms'],
            generation_age_inclusive_until=generation['generation_end_ms']+FRESH_MS,
            proof_expiries={n: {k: w[k]['expires_at'] for k in ('validity', 'instrument_validity')}
                            for n, w in generation['windows'].items()},
            ws_event_times={n: w['validity']['ws_event_time'] for n, w in generation['windows'].items()})

    @classmethod
    def result(cls, operation):
        started = time.monotonic_ns()
        try:
            value = operation()
        except (ValueError, RuntimeError, sqlite3.Error) as error:
            return dict(cls.error_result(error), started_wall_ns=started, finished_wall_ns=time.monotonic_ns())
        out = dict(accepted=True, reason=None, error_type=None, sqlite_error=False)
        if isinstance(value, consumer.Frozen):
            out['binding'] = cls.binding(value)
        out.update(started_wall_ns=started, finished_wall_ns=time.monotonic_ns())
        return out

    def state_if_readable(self, store):
        try:
            return dict(available=True, **self.state(store))
        except (ValueError, RuntimeError, sqlite3.Error) as error:
            result = dict(available=False, error=self.error_result(error))
            # The independent control file may remain readable even when a
            # killed market writer left recovery that the RO path cannot do.
            try:
                result['independent_control_status'] = store.control.status()
            except (ValueError, RuntimeError, sqlite3.Error) as control_error:
                result['independent_control_error'] = self.error_result(control_error)
            return result

    def journal_files(self):
        out = {'existence_is_not_a_hot_journal_verdict': True}
        for suffix in ('-journal', '-wal'):
            path = self.scratch/('state.sqlite3'+suffix)
            value = dict(exists=path.exists())
            if value['exists']:
                value['bytes'] = path.stat().st_size
                with path.open('rb') as source:
                    value['header_hex'] = source.read(28).hex()
            out[suffix] = value
        return out

    def assert_transaction_read_error(self, outcome, label):
        # A killed DELETE-mode writer can require recovery that an existing-only
        # read-only connection cannot perform. Keep native evidence; accept only
        # these recovery/lock refusal classes, never arbitrary SQL/IO/corruption
        # errors as successful test observations. Python 3.10 lacks native codes.
        self.assertFalse(outcome['accepted'], label)
        self.assertTrue(outcome['sqlite_error'], label)
        self.assertEqual(outcome['error_type'], 'OperationalError', label)
        self.assertIn(outcome['reason'], ('attempt to write a readonly database', 'database is locked'), label)
        code = outcome['sqlite_errorcode']
        if code is not None:
            self.assertIn(code & 255, (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED), label)
        self.assertIn('sqlite3.OperationalError', outcome['traceback'], label)

    @staticmethod
    def first_rejections(observations):
        result = {}
        for key in ('old_frozen', 'fresh_read', 'management_inputs', 'signal_inputs'):
            previous = None
            for row in observations:
                if not row['after_sigkill_reap']:
                    continue
                if not row[key]['accepted']:
                    result[key] = dict(label=row['label'], now_ms=row['now_ms'],
                        elapsed_wall_since_reap_ns=row[key]['finished_wall_ns']-row['reaped_wall_ns'],
                        injected_ms_since_kill=row['injected_ms_since_kill'],
                        last_sampled_accepted_ms=previous, reason=row[key]['reason'],
                        error_type=row[key]['error_type'],
                        timing_claim='FIRST_SAMPLED_REJECTION_NOT_CONTINUOUS_TIME_MEASUREMENT')
                    break
                previous = row['now_ms']
            else:
                result[key] = None
        return result

    def probe(self, scenario):
        transaction_case = scenario == 'inflight-transaction'
        original_sources = source_hashes()
        report = dict(scenario=scenario, clock_mode='INJECTED_MILLISECONDS_NOT_REAL_90_SECOND_WAIT',
            control_clock_mode='REAL_MONOTONIC_NOT_ADVANCED',
            actual_source_head=os.environ.get('CHECKOUT_SHA'), source_sha256=original_sources,
            diagnostic_only=True, crash_receipt_safety_claim=False, no_http=True,
            ingress_count_semantics='CHILD_PROTOCOL_INGRESS_DELTA_RECEIVED_BY_PARENT_IN_STAGED_HANDSHAKE',
            parent_observed_individual_wire_frames=False,
            production_entry_injections='SCOPED_SELECTOR_CLOCK_AND_SCRATCH_READER_ONLY',
            explicit_writer_recovery_performed=False,
            dangerous_counterfactual=scenario == 'memory-only-overflow', observations=[])
        try:
            child = subprocess.Popen([sys.executable, '-B', str(HELPER), str(self.scratch), scenario],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.children.append(child)
            seeded = self.event(child, 'SEEDED')
            report['seeded'] = seeded
            stamp = seeded['stamp']; names = seeded['names']; clock = [stamp]
            store = Store.reader(self.scratch/'state.sqlite3')
            store.enable_control(readonly=True)
            original_read = consumer.read

            def fresh():
                return original_read(names, dt.datetime.fromtimestamp(clock[0]/1000, dt.timezone.utc),
                                     reader=store, clock=lambda: clock[0])

            def scratch_read(requested, now, *, entry_name, **kwargs):
                self.assertEqual(requested, expected_entry_scope, entry_name+':EXACT_ENTRY_SCOPE')
                if entry_name == 'signal_inputs':
                    self.assertEqual(kwargs.get('expected_roster'), roster_hash(names),
                                     'SIGNAL_INPUTS_FULL_ROSTER_BINDING_REQUIRED')
                return original_read(requested, now, reader=store, clock=lambda: clock[0], **kwargs)

            class InjectedDateTime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return dt.datetime.fromtimestamp(clock[0]/1000, tz)

            symbols = sorted({split_topic(n)[0] for n in names})
            expected_entry_scope = sorted(topic(symbol, interval) for symbol in symbols for interval in ('15m', '1h'))
            report['expected_entry_scope'] = expected_entry_scope
            report['expected_signal_roster_hash'] = roster_hash(names)
            scan = fixture_scan()
            scan['as_of_utc'] = dt.datetime.fromtimestamp(stamp/1000, dt.timezone.utc).isoformat()

            def entry(which):
                # Call the actual production entry. Avoid setting environment
                # selectors, running either CLI, or patching global datetime.
                with patch.object(consumer, 'selected', return_value='SPOT_CACHE'), \
                        patch.object(consumer, 'read', side_effect=lambda requested, now, **kwargs:
                            scratch_read(requested, now, entry_name=which, **kwargs)), \
                        patch.object(consumer, 'dt', SimpleNamespace(datetime=InjectedDateTime,
                                                                   timezone=dt.timezone)):
                    if which == 'management_inputs':
                        return consumer.management_inputs(symbols, InjectedDateTime.now(dt.timezone.utc))
                    return consumer.signal_inputs(scan, symbols)

            old = fresh()
            generation = old.generation
            report['generation_sha256'] = generation['generation_sha256']
            baseline = self.state(store)
            report['baseline_counts'] = baseline['market']['counts']
            killed = False
            reaped_at = None
            kill_clock = None

            def old_delivery():
                old.check_delivery()
                return old

            def observe(label):
                row = dict(label=label, now_ms=clock[0], since_generation_ms=clock[0]-stamp,
                    after_sigkill_reap=killed,
                    reaped_wall_ns=reaped_at,
                    injected_ms_since_kill=clock[0]-kill_clock if killed else None,
                    elapsed_wall_since_reap_ns=time.monotonic_ns()-reaped_at if killed else None,
                    old_frozen=self.result(old_delivery), fresh_read=self.result(fresh),
                    management_inputs=self.result(lambda: entry('management_inputs')),
                    signal_inputs=self.result(lambda: entry('signal_inputs')))
                report['observations'].append(row)
                return row

            initial = observe('before_stage')
            for key in ('old_frozen', 'fresh_read', 'management_inputs', 'signal_inputs'):
                self.assertTrue(initial[key]['accepted'], key+':INITIAL_ENTRY_NOT_READY')
                self.assertIn('binding', initial[key], key+':REAL_FROZEN_REQUIRED')
            report['initial_reader_bindings'] = {key: initial[key]['binding'] for key in
                ('old_frozen', 'fresh_read', 'management_inputs', 'signal_inputs')}
            self.assertEqual(len(initial['old_frozen']['binding']['names']), 7)
            for key in ('management_inputs', 'signal_inputs'):
                self.assertEqual(initial[key]['binding']['names'], expected_entry_scope)
            child.stdin.write('STAGE\n'); child.stdin.flush()
            staged = self.event(child, 'STAGED')
            report['staged'] = staged
            clock[0] = staged['received_at']
            # Observe all actual read paths BEFORE a potentially failing state
            # inspection, including while a real writer transaction is paused.
            alive = observe('child_alive_after_stage')
            report['journal_before_death'] = self.journal_files()
            before_death = self.state_if_readable(store)
            report['before_death'] = before_death
            with self.assertRaises(BlockingIOError):
                NativeLock(self.scratch/'collector.lock', shared=True)
            if staged['owner_token']:
                owner_path = store.control._owner_path(staged['owner_token'])
                with self.assertRaises(BlockingIOError):
                    NativeLock(owner_path, shared=True)

            kill_started = time.monotonic_ns()
            kill_clock = clock[0]
            child.send_signal(signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=10)
            reaped_at = time.monotonic_ns()
            killed = True
            report['kill'] = dict(returncode=child.returncode,
                reap_wall_ns=time.monotonic_ns()-kill_started, stdout_tail=stdout, stderr=stderr)
            self.assertEqual(child.returncode, -signal.SIGKILL)
            self.assertEqual(stdout, '')
            self.assertEqual(stderr, '')
            report['journal_after_reap_before_readers'] = self.journal_files()
            immediate = observe('immediately_after_sigkill_reap')
            report['journal_after_first_readers'] = self.journal_files()
            probe = NativeLock(self.scratch/'collector.lock', shared=True)
            probe.close()
            report['collector_lock_released'] = True
            if staged['owner_token']:
                probe = NativeLock(owner_path, shared=True)
                self.assertEqual(list(probe.identity), staged['owner_identity'])
                probe.close()
                report['http_owner_lock_released'] = True
            after_death = self.state_if_readable(store)
            report['after_death'] = after_death
            # SIGKILL never executes graceful disconnect/drain/final(), and
            # read-only liveness checks must not manufacture durable receipts.
            if not transaction_case:
                self.assertTrue(before_death['available'])
                self.assertTrue(after_death['available'])
                self.assertEqual(before_death['market'], after_death['market'])
                self.assertEqual(before_death['control'], after_death['control'])
                self.assertEqual(after_death['market']['meta']['connected'], '1')
                self.assertEqual(after_death['market']['meta']['health_phase'], 'ACTIVE')
            if staged['owner_token']:
                outside = [n for n in names if n != staged['target_topic']]
                report['outside_owner_scope_fence'] = self.result(lambda: store.control.fence(outside))
                self.assertEqual(report['outside_owner_scope_fence']['reason'], 'CONTROL_ORPHAN_REQUIRES_REVIEW')
            expires = min(w[k]['expires_at'] for w in generation['windows'].values()
                          for k in ('validity', 'instrument_validity'))
            age_limit = generation['generation_end_ms']+FRESH_MS
            native_boundary = min((stamp//STEPS[split_topic(n)[1]]+1)*STEPS[split_topic(n)[1]] for n in names)
            self.assertLess(expires, age_limit)
            self.assertGreater(native_boundary, age_limit+1)
            report['boundaries'] = dict(proof_expires_at=expires, generation_age_inclusive_until=age_limit,
                                        next_native_boundary=native_boundary)
            report['reader_boundaries'] = {key: dict(names=value['names'],
                initial_generation_end_ms=value['generation_end_ms'],
                initial_generation_age_inclusive_until=value['generation_age_inclusive_until'],
                earliest_initial_proof_expiry=min(t for proof in value['proof_expiries'].values()
                                                  for t in proof.values()))
                for key, value in report['initial_reader_bindings'].items()}
            # Include every actual entry's own expiry, even if a future fixture
            # gives it a different scope or observation age from the full Frozen.
            points = {expires-1, expires, expires+1, age_limit, age_limit+1}
            for value in report['reader_boundaries'].values():
                deadline = value['earliest_initial_proof_expiry']
                points.update((deadline-1, deadline, deadline+1))
            labels = {expires-1: 'proof_expiry_minus_1', expires: 'proof_expiry_exact',
                      expires+1: 'proof_expiry_plus_1', age_limit+1: 'generation_age_plus_1'}
            for point in sorted(points):
                clock[0] = point
                observe(labels.get(point, 'entry_boundary_'+str(point)))

            # Parent received these per-topic Protocol ingress deltas in the
            # child's STAGED handshake; it did not observe individual wire
            # frames. Compare those reported deltas with durable outcomes.
            prior = {r['topic']: r for r in baseline['market']['counts']}
            current = ({r['topic']: r for r in after_death['market']['counts']}
                       if after_death['available'] else None)
            accounting = {}
            for name in names:
                delta = ({key: current[name][key]-prior[name][key] for key in OUTCOMES}
                         if current is not None else None)
                received = staged['received_delta'][name]
                accounting[name] = dict(ingress_delta_reported_by_child=received, durable_delta=delta,
                    unaccounted_after_sigkill=received-sum(delta.values()) if delta is not None else None)
            report['receipt_accounting'] = accounting
            lost = sum(row['unaccounted_after_sigkill'] for row in accounting.values()) if current is not None else None
            report['unaccounted_after_sigkill'] = lost
            report['durable_receipt_partition_complete'] = lost == 0 if lost is not None else None
            report['durable_state_observation'] = 'READABLE' if current is not None else 'UNKNOWN_READONLY_SQLITE_UNAVAILABLE'
            expected_received = 4097 if scenario.endswith('overflow') else 1
            self.assertEqual(sum(staged['received_delta'].values()), expected_received)
            self.assertEqual(staged['received_delta'],
                {n: expected_received if n == staged['target_topic'] else 0 for n in names})
            self.assertEqual(staged['capacity'], 4096)
            self.assertEqual(staged['accepted'], 4096 if scenario.endswith('overflow') else 1)
            self.assertEqual(staged['queued'], staged['accepted']-int(transaction_case))
            self.assertEqual(staged['inflight'], int(transaction_case))
            self.assertEqual(staged['transaction_in_progress'], transaction_case)
            if transaction_case:
                self.assertEqual(staged['transaction_phase'], 'AFTER_REAL_PUT_BEFORE_COMMIT')
                self.assertGreater(staged['transaction_changes'], 0)
                self.assertEqual(staged['journal_mode'], 'delete')
                self.assertTrue(report['journal_before_death']['-journal']['exists'])
                self.assertGreater(report['journal_before_death']['-journal']['bytes'], 0)
            self.assertEqual(staged['full'], scenario.endswith('overflow'))
            self.assertEqual(staged['memory_only_revoked'], scenario == 'memory-only-overflow')
            if current is not None:
                self.assertEqual(lost, expected_received-(scenario == 'durable-overflow'))
            for name, row in accounting.items():
                expected = int(scenario == 'durable-overflow' and name == staged['target_topic'])
                if current is not None:
                    self.assertEqual(row['durable_delta'], dict(committed=0, duplicate=0, invalid=0,
                                                               unprocessed_recorded=expected))
            receipts = after_death['market']['receipts'] if current is not None else None
            if receipts is not None:
                self.assertEqual(len(receipts), int(scenario == 'durable-overflow'))
            if receipts:
                symbol, interval = split_topic(staged['target_topic'])
                expected_message = fixture_message(symbol, interval, staged['received_at'])
                expected_receipt = dict(epoch=seeded['epoch'], topic=staged['target_topic'],
                    received_at=staged['received_at'], open_time=expected_message['k']['t'],
                    event_time=expected_message['E'], payload_sha256=payload_hash(expected_message),
                    reason='RECEIVE_QUEUE_FULL', category='unprocessed_recorded', resolved_at=None)
                report['expected_durable_receipt'] = expected_receipt
                self.assertEqual({key: receipts[0][key] for key in expected_receipt}, expected_receipt)
            owner_case = scenario == 'active-owner'
            if after_death['available']:
                self.assertEqual(after_death['status']['active_owners'], int(owner_case))
                self.assertEqual(after_death['status']['halt'], 'CONTROL_ORPHAN_REQUIRES_REVIEW' if owner_case else None)
                self.assertIsNone(after_death['control']['budget']['halt'])
            elif not transaction_case:
                self.fail('NON_TRANSACTION_STATE_MUST_REMAIN_READABLE')
            if transaction_case:
                for label, state in (('before_death', before_death), ('after_death', after_death)):
                    if not state['available']:
                        self.assert_transaction_read_error(state['error'], label)
                        self.assertNotIn('independent_control_error', state, label)
                        self.assertEqual(state['independent_control_status']['active_owners'], 0, label)
                        self.assertIsNone(state['independent_control_status']['halt'], label)

            # These are current behavior assertions, including the unsafe READY
            # window after idle collector death. Passing does not endorse it.
            for row in report['observations']:
                now = row['now_ms']; dead = row['after_sigkill_reap']
                staged_already = row['label'] != 'before_stage'
                if now > age_limit:
                    old_reason = 'BINANCE_FINAL_READ_STALE'
                elif scenario == 'durable-overflow' and staged_already:
                    old_reason = 'BINANCE_FINAL_TOPIC_REVOKED'
                elif owner_case and dead:
                    old_reason = 'CONTROL_ORPHAN_REQUIRES_REVIEW'
                elif now > expires:
                    old_reason = 'BINANCE_CACHE_VALIDITY_MISSING_EXPIRED_OR_MISMATCHED'
                else:
                    old_reason = None
                for key in ('old_frozen', 'fresh_read', 'management_inputs', 'signal_inputs'):
                    label = scenario+':'+row['label']+':'+key
                    value = row[key]
                    if transaction_case and value['sqlite_error'] and (key != 'old_frozen' or now <= age_limit):
                        self.assert_transaction_read_error(value, label)
                        continue
                    deadline = report['reader_boundaries'][key]['earliest_initial_proof_expiry']
                    if key == 'old_frozen':
                        reason = old_reason
                    elif owner_case and dead:
                        reason = 'CONTROL_ORPHAN_REQUIRES_REVIEW'
                    elif now > deadline:
                        reason = 'BINANCE_CACHE_UNKNOWN:BINANCE_TRADING_STATUS_UNKNOWN_OR_INVALID'
                    elif scenario == 'durable-overflow' and staged_already:
                        reason = 'BINANCE_CACHE_UNKNOWN:BINANCE_UNRECONCILED_RECEIPT'
                    else:
                        reason = None
                    self.assertEqual(row[key]['reason'], reason, label)
                    self.assertEqual(row[key]['accepted'], reason is None, label)
                    if value['accepted']:
                        binding = value['binding']
                        self.assertEqual(binding['epoch'], seeded['epoch'], label)
                        self.assertEqual(binding['names'], report['initial_reader_bindings'][key]['names'], label)
                        # An uncommitted _put may never become consumer evidence.
                        self.assertEqual(set(binding['ws_event_times'].values()), {stamp}, label)
            report['immediate_invalidation_observed'] = not immediate['old_frozen']['accepted']
            report['transaction_native_read_refusals'] = sum(value[key]['sqlite_error']
                for value in report['observations']
                for key in ('old_frozen', 'fresh_read', 'management_inputs', 'signal_inputs'))
            report['observed_contract_assertions_passed'] = True
        finally:
            report['first_sampled_rejection_after_sigkill'] = self.first_rejections(report['observations'])
            report['source_hashes_unchanged'] = source_hashes() == original_sources
            print('BINANCE_PROCESS_DEATH_DIAGNOSTIC '+json.dumps(report, sort_keys=True), flush=True)
            self.assertTrue(report['source_hashes_unchanged'])

    def test_queued_uncommitted_frame_idle_collector_death(self):
        self.probe('queued')

    def test_durable_receive_queue_full_survives_death(self):
        self.probe('durable-overflow')

    def test_memory_only_overflow_is_unsafe_counterfactual(self):
        self.probe('memory-only-overflow')

    def test_http_owner_death_is_immediate_global_control_denial(self):
        self.probe('active-owner')

    def test_sigkill_inside_real_ws_transaction_preserves_read_refusals(self):
        self.probe('inflight-transaction')
