"""Observed Linux SIGKILL semantics, not a claim of crash-complete receipts.

Only the dedicated guarded CI runner should execute these four tests. Real
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
import subprocess
import sys
import tempfile
import time
import unittest

from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_control import NativeLock
from research.hunter_binance_health_store import Store
from research.hunter_binance_stream_store import FRESH_MS, STEPS, split_topic

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT/'tests/fixtures/binance_process_death.py'
OUTCOMES = ('committed', 'duplicate', 'invalid', 'unprocessed_recorded')
SOURCES = ('research/hunter_binance_stream.py', 'research/hunter_binance_health.py',
           'research/hunter_binance_health_store.py', 'research/hunter_binance_stream_store.py',
           'research/hunter_binance_market_control.py', 'research/hunter_binance_control.py',
           'research/hunter_binance_stream_consumer.py', 'tests/fixtures/binance_process_death.py',
           'tests/test_hunter_binance_process_death.py')


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
    def result(operation):
        try:
            operation()
        except (ValueError, RuntimeError) as error:
            return dict(accepted=False, reason=str(error), error_type=type(error).__name__)
        return dict(accepted=True, reason=None, error_type=None)

    def probe(self, scenario):
        original_sources = source_hashes()
        report = dict(scenario=scenario, clock_mode='INJECTED_MILLISECONDS_NOT_REAL_90_SECOND_WAIT',
            control_clock_mode='REAL_MONOTONIC_NOT_ADVANCED',
            actual_source_head=os.environ.get('CHECKOUT_SHA'), source_sha256=original_sources,
            diagnostic_only=True, crash_receipt_safety_claim=False, no_http=True,
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

            def fresh():
                return consumer.read(names, dt.datetime.fromtimestamp(clock[0]/1000, dt.timezone.utc),
                                     reader=store, clock=lambda: clock[0])

            old = fresh()
            generation = old.generation
            report['generation_sha256'] = generation['generation_sha256']
            baseline = self.state(store)
            report['baseline_counts'] = baseline['market']['counts']
            child.stdin.write('STAGE\n'); child.stdin.flush()
            staged = self.event(child, 'STAGED')
            report['staged'] = staged
            clock[0] = staged['received_at']
            before_death = self.state(store)
            report['before_death'] = before_death
            with self.assertRaises(BlockingIOError):
                NativeLock(self.scratch/'collector.lock', shared=True)
            if staged['owner_token']:
                owner_path = store.control._owner_path(staged['owner_token'])
                with self.assertRaises(BlockingIOError):
                    NativeLock(owner_path, shared=True)

            def observe(label):
                row = dict(label=label, now_ms=clock[0], since_generation_ms=clock[0]-stamp,
                    old_frozen=self.result(old.check_delivery), fresh_read=self.result(fresh))
                report['observations'].append(row)
                return row

            alive = observe('child_alive_after_stage')
            kill_started = time.monotonic_ns()
            child.send_signal(signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=10)
            report['kill'] = dict(returncode=child.returncode,
                reap_wall_ns=time.monotonic_ns()-kill_started, stdout_tail=stdout, stderr=stderr)
            self.assertEqual(child.returncode, -signal.SIGKILL)
            self.assertEqual(stdout, '')
            self.assertEqual(stderr, '')
            probe = NativeLock(self.scratch/'collector.lock', shared=True)
            probe.close()
            report['collector_lock_released'] = True
            if staged['owner_token']:
                probe = NativeLock(owner_path, shared=True)
                self.assertEqual(list(probe.identity), staged['owner_identity'])
                probe.close()
                report['http_owner_lock_released'] = True
            after_death = self.state(store)
            report['after_death'] = after_death
            # SIGKILL never executes graceful disconnect/drain/final(), and
            # read-only liveness checks must not manufacture durable receipts.
            self.assertEqual(before_death['market'], after_death['market'])
            self.assertEqual(before_death['control'], after_death['control'])
            self.assertEqual(after_death['market']['meta']['connected'], '1')
            self.assertEqual(after_death['market']['meta']['health_phase'], 'ACTIVE')
            immediate = observe('immediately_after_sigkill_reap')
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
            for label, point in (('proof_expiry_minus_1', expires-1), ('proof_expiry_exact', expires),
                                 ('proof_expiry_plus_1', expires+1), ('generation_age_exact', age_limit),
                                 ('generation_age_plus_1', age_limit+1)):
                clock[0] = point
                observe(label)

            # Compare every topic's parent-observed input delta with durable
            # outcome deltas, not just the accepted queue size or success flag.
            prior = {r['topic']: r for r in baseline['market']['counts']}
            current = {r['topic']: r for r in after_death['market']['counts']}
            accounting = {}
            for name in names:
                delta = {key: current[name][key]-prior[name][key] for key in OUTCOMES}
                received = staged['received_delta'][name]
                accounting[name] = dict(received_parent_observed=received, durable_delta=delta,
                    unaccounted_after_sigkill=received-sum(delta.values()))
            report['receipt_accounting'] = accounting
            lost = sum(row['unaccounted_after_sigkill'] for row in accounting.values())
            report['unaccounted_after_sigkill'] = lost
            report['durable_receipt_partition_complete'] = lost == 0
            expected_received = 4097 if scenario.endswith('overflow') else 1
            self.assertEqual(sum(staged['received_delta'].values()), expected_received)
            self.assertEqual(staged['capacity'], 4096)
            self.assertEqual(staged['accepted'], 4096 if scenario.endswith('overflow') else 1)
            self.assertEqual(staged['queued'], staged['accepted'])
            self.assertEqual(staged['full'], scenario.endswith('overflow'))
            self.assertEqual(staged['memory_only_revoked'], scenario == 'memory-only-overflow')
            self.assertEqual(lost, expected_received-(scenario == 'durable-overflow'))
            for name, row in accounting.items():
                expected = int(scenario == 'durable-overflow' and name == staged['target_topic'])
                self.assertEqual(row['durable_delta'], dict(committed=0, duplicate=0, invalid=0,
                                                           unprocessed_recorded=expected))
            receipts = after_death['market']['receipts']
            self.assertEqual(len(receipts), int(scenario == 'durable-overflow'))
            if receipts:
                self.assertEqual(receipts[0]['reason'], 'RECEIVE_QUEUE_FULL')
                self.assertEqual(receipts[0]['topic'], staged['target_topic'])
                self.assertIsNone(receipts[0]['resolved_at'])
            owner_case = scenario == 'active-owner'
            self.assertEqual(after_death['status']['active_owners'], int(owner_case))
            self.assertEqual(after_death['status']['halt'], 'CONTROL_ORPHAN_REQUIRES_REVIEW' if owner_case else None)
            self.assertIsNone(after_death['control']['budget']['halt'])

            # These are current behavior assertions, including the unsafe READY
            # window after idle collector death. Passing does not endorse it.
            for row in report['observations']:
                now = row['now_ms']; dead = row is not alive
                if now > age_limit:
                    old_reason = 'BINANCE_FINAL_READ_STALE'
                elif scenario == 'durable-overflow':
                    old_reason = 'BINANCE_FINAL_TOPIC_REVOKED'
                elif owner_case and dead:
                    old_reason = 'CONTROL_ORPHAN_REQUIRES_REVIEW'
                elif now > expires:
                    old_reason = 'BINANCE_CACHE_VALIDITY_MISSING_EXPIRED_OR_MISMATCHED'
                else:
                    old_reason = None
                if owner_case and dead:
                    new_reason = 'CONTROL_ORPHAN_REQUIRES_REVIEW'
                elif now > expires:
                    new_reason = 'BINANCE_CACHE_UNKNOWN:BINANCE_TRADING_STATUS_UNKNOWN_OR_INVALID'
                elif scenario == 'durable-overflow':
                    new_reason = 'BINANCE_CACHE_UNKNOWN:BINANCE_UNRECONCILED_RECEIPT'
                else:
                    new_reason = None
                for key, reason in (('old_frozen', old_reason), ('fresh_read', new_reason)):
                    with self.subTest(scenario=scenario, point=row['label'], reader=key):
                        self.assertEqual(row[key]['reason'], reason)
                        self.assertEqual(row[key]['accepted'], reason is None)
            report['immediate_invalidation_observed'] = not immediate['old_frozen']['accepted']
            report['observed_contract_assertions_passed'] = True
        finally:
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
