"""Coordinated recovery integration, using real scratch SQLite and runtime code.

Data cases are portable and prohibit network through the dedicated CI guard.
LinuxProcessCases additionally use real HTTP children, loopback sockets and
abrupt process exit. Run that class only on Linux; a skipped Windows case is
not acceptance. No production paths, services, ledger or consumer selectors
are changed by this module.
"""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from research import hunter_binance_health as health
from research import hunter_binance_public as public
from research import hunter_binance_stream as stream
from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_health_store import Store
from research.hunter_binance_control import ControlError
from research.hunter_binance_stream_store import STEPS, topic, split_topic, content_hash
from tests.test_hunter_binance_cache import rows, message


HEAD = 'd' * 40
MANIFEST = 'e' * 64
MIDNIGHT = int(dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp() * 1000)
ROOT = Path(__file__).resolve().parents[1]
CRASH_HELPER = ROOT / 'tests/fixtures/binance_coordinated_crash.py'


def topics(symbols):
    return sorted({topic(s, i) for s in symbols for i in ('15m', '1h', '1d')}
                  | {topic('BTCUSDT', '5m')})


def directory(names, *, status='TRADING'):
    symbols = sorted({split_topic(n)[0] for n in names})
    return {'symbols': [dict(symbol=s, baseAsset=s[:-4], quoteAsset='USDT',
        status=status, isSpotTradingAllowed=True, permissions=['SPOT'], filters=[
            dict(filterType='PRICE_FILTER', minPrice='0.01', maxPrice='1000000', tickSize='0.01'),
            dict(filterType='LOT_SIZE', minQty='0.001', maxQty='1000000', stepSize='0.001'),
            dict(filterType='MIN_NOTIONAL', minNotional='1', applyToMarket=True, avgPriceMins=5)])
        for s in symbols]}


class PortableGate:
    """Windows data-fixture guard only; it never represents an HTTP owner/flock.

    Linux tests use the production NativeLock with no replacement. Portable
    tests still exercise real control SQLite fences and market transactions.
    """
    def __init__(self, path, *, create=False, shared=False):
        if create or not shared or not str(path).endswith('.gate'):
            raise AssertionError('PORTABLE_CASE_MUST_NOT_CREATE_OR_SIMULATE_OWNER')
        self.fd = os.open(path, os.O_RDONLY)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Market:
    """Real store/control/protocol. Only exchange data and the clock are fixtures."""
    def __init__(self, path, names=None, stamp=MIDNIGHT-15000):
        from research.hunter_binance_control import Control
        self.path = path
        self.names = names or topics(('BTCUSDT', 'ETHUSDT'))
        self.started = stamp
        self.store = Store(path)
        Control.provision(path.with_name('control.sqlite3'), path)
        self.store.enable_control()
        self.store.set_control_source(HEAD)
        self.store.enable_health()
        self.protocol = health.Protocol(self.store, self.names, MANIFEST, stamp)
        self.epoch = self.protocol.epoch
        self.store.ack(self.epoch, self.names)
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('health_source_head', HEAD))

    def apply(self, kind, names, body, stamp):
        return self.store.apply_work(kind, self.epoch, names, body, stamp, stamp,
                                    job_key='integration-'+kind+'-'+content_hash(sorted(names))[:16])

    def seed(self, names=None, stamp=None):
        names = names or self.names
        stamp = self.started+1 if stamp is None else stamp
        self.apply('directory', names, directory(names), stamp)
        for name in names:
            self.apply('repair', [name], rows(split_topic(name)[1], stamp), stamp)
        self.frames(names, stamp+1)
        return stamp+1

    def frames(self, names, stamp, *, previous=None):
        batch = []
        for name in names:
            symbol, interval = split_topic(name)
            shard = self.protocol.topic_shards[name]
            if previous is not None and previous//STEPS[interval] != stamp//STEPS[interval]:
                batch.append((shard, message(symbol, interval, stamp-1,
                    start=previous//STEPS[interval]*STEPS[interval], closed=True), stamp-1))
            batch.append((shard, message(symbol, interval, stamp), stamp))
        for shard, value, received in batch:
            self.protocol.arrived(shard, json.dumps(value), received)
        for offset in range(0, len(batch), 100):
            self.protocol.receive_batch(batch[offset:offset+100])
        return len(batch)

    def read(self, names, stamp):
        return consumer.read(names, dt.datetime.fromtimestamp(stamp/1000, dt.timezone.utc),
                             reader=self.store, clock=lambda: stamp)

    def snapshot(self):
        with self.store.db(readonly=True) as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
                    for table in ('bars', 'revisions', 'bn_publications')}


class DataCases(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='binance-coordinated-runtime-')
        self.addCleanup(temp.cleanup)
        self.scratch = Path(temp.name)
        if sys.platform != 'linux':
            from research import hunter_binance_control as control
            original = control.Control.__init__
            def portable(instance, *args, **kwargs):
                kwargs['lock_factory'] = PortableGate
                original(instance, *args, **kwargs)
            for item in (patch.object(control, 'boot_id', return_value='fixture-portable-boot'),
                         patch.object(control.Control, '__init__', portable)):
                item.start(); self.addCleanup(item.stop)

    def market(self, name='market', **kwargs):
        path = self.scratch/name
        path.mkdir()
        return Market(path/'state.sqlite3', **kwargs)

    def test_utc_midnight_vietnam_seven_and_native_boundaries_require_new_ws(self):
        for label, boundary in (('utc00_vietnam07', MIDNIGHT),
                                ('15m', MIDNIGHT+900000), ('1h', MIDNIGHT+3600000)):
            with self.subTest(boundary=label):
                market = self.market(label, stamp=boundary-15000)
                before = market.seed()
                self.assertEqual(market.read(market.names, before).generation['status'], 'READY')
                with self.assertRaises((ValueError, ControlError)):
                    market.read(market.names, boundary+2)
                market.frames(market.names, boundary+2, previous=before)
                frozen = market.read(market.names, boundary+2)
                self.assertEqual(set(frozen.generation['windows']), set(market.names))
                self.assertEqual(frozen.generation['generation_end_ms'], boundary+2)
                self.assertEqual({split_topic(n)[1] for n in market.names}, set(STEPS))
                local = dt.datetime.fromtimestamp(MIDNIGHT/1000,
                    dt.timezone(dt.timedelta(hours=7)))
                self.assertEqual((local.hour, local.minute), (7, 0))

    def test_required_set_never_shrinks_and_unaffected_scope_remains_readable(self):
        market = self.market()
        stamp = market.seed()
        btc = sorted(n for n in market.names if split_topic(n)[0] == 'BTCUSDT')
        eth = sorted(n for n in market.names if split_topic(n)[0] == 'ETHUSDT')
        with self.assertRaises((ValueError, ControlError)):
            market.apply('directory', eth, directory(eth, status='BREAK'), stamp+1)
        self.assertEqual(market.read(btc, stamp+1).generation['status'], 'READY')
        with self.assertRaises((ValueError, ControlError)):
            market.read(market.names, stamp+1)
        for name in eth:
            with self.assertRaises((ValueError, ControlError)):
                market.read([name], stamp+1)
        market.apply('directory', eth, directory(eth), stamp+2)
        self.assertEqual(market.read(market.names, stamp+2).generation['status'], 'READY')

    def test_directory_batch_scope_includes_every_symbol_period_and_does_not_extend_old_proof(self):
        market = self.market()
        stamp = market.seed()
        old = {n: market.store.get_proof('instrument', n) for n in market.names}
        incomplete = directory(market.names)
        incomplete['symbols'].pop()
        with self.assertRaises((ValueError, ControlError)):
            market.apply('directory', market.names, incomplete, stamp+1)
        for name in market.names:
            with self.assertRaises((ValueError, ControlError)):
                market.read([name], stamp+2)
            value = market.store.get_proof('instrument', name)
            self.assertTrue(value is None or value['expires_at'] <= old[name]['expires_at'])

    def test_directory_missing_filters_invalidates_the_complete_dependency_batch(self):
        market = self.market()
        stamp = market.seed()
        invalid = directory(market.names)
        invalid['symbols'][-1]['filters'] = []
        with self.assertRaises((ValueError, ControlError)):
            market.apply('directory', market.names, invalid, stamp+1)
        for name in market.names:
            with self.assertRaises((ValueError, ControlError)):
                market.read([name], stamp+2)

    def test_control_intent_inserted_after_freeze_is_seen_by_read_finish(self):
        market = self.market()
        stamp = market.seed()
        original = market.store.freeze
        name = topic('ETHUSDT', '15m')
        def freeze_then_change(*args, **kwargs):
            result = original(*args, **kwargs)
            body = dict(kind='repair', names=[name], body=rows('15m', stamp+1),
                        requested=stamp+1, received=stamp+1, job_key='read-race')
            market.store.control.publication_intent(market.epoch, 'read-race', [name],
                content_hash(body), payload=body)
            return result
        with patch.object(market.store, 'freeze', side_effect=freeze_then_change):
            with self.assertRaises((ValueError, ControlError)):
                market.read(market.names, stamp+2)
        self.assertEqual(market.read([topic('BTCUSDT', '5m')], stamp+2).generation['status'], 'READY')

    def test_quiet_changed_closed_bar_requires_second_verification_and_current_ws(self):
        market = self.market(stamp=MIDNIGHT+120000)
        stamp = market.seed()
        name = topic('ETHUSDT', '1h')
        changed = rows('1h', stamp+10)
        changed[-2][4] = '102'
        with self.assertRaisesRegex(ValueError, 'BINANCE_PUBLICATION_NOT_VALID'):
            market.apply('quiet', [name], changed, stamp+10)
        with market.store.db(readonly=True) as db:
            receipt = dict(db.execute("SELECT * FROM bn_publications WHERE outcome='UNKNOWN'").fetchone())
            corrected = json.loads(db.execute('SELECT row_json FROM bars WHERE topic=? AND start=?',
                (name, changed[-2][0])).fetchone()[0])
        self.assertEqual(corrected[4], '102', 'corrective REST bar rolled back with its UNKNOWN receipt')
        self.assertEqual(market.store.get_proof('quiet', name)['reason'], 'CLOSED_BAR_MISMATCH')
        with self.assertRaisesRegex(ValueError, 'BINANCE_PUBLICATION_REJECTED_RECEIPT'):
            market.store.apply_intent(market.store.control.get_intent(receipt['intent_id']))
        market.frames([name], stamp+11)
        with self.assertRaises((ValueError, ControlError)):
            market.read([name], stamp+11)
        self.assertEqual(market.read([topic('BTCUSDT', '5m')], stamp+11).generation['status'], 'READY')
        market.apply('quiet', [name], changed, stamp+12)
        frozen = market.read(market.names, stamp+12)
        self.assertEqual(frozen.generation['status'], 'READY')
        proof = market.store.get_proof('quiet', name)
        self.assertEqual((proof['result'], proof['comparison']), ('VALID', 'SAME_CLOSED_NATIVE_BAR'))
        self.assertEqual(proof['source'], 'OFFICIAL_BINANCE_SPOT_REST')
        # Quiet verification remains incapable of extending the 90-second WS
        # receipt freshness, even after it closes the correction obligation.
        late = stamp+90012
        market.apply('directory', [name], directory([name]), late)
        market.apply('quiet', [name], changed, late)
        with self.assertRaises((ValueError, ControlError)):
            market.read([name], late)

    def test_frozen_rows_and_packet_reject_scope_revocation_after_read(self):
        market = self.market()
        stamp = market.seed()
        frozen = market.read(market.names, stamp)
        with self.assertRaises((ValueError, ControlError)):
            market.apply('directory', market.names, directory(market.names, status='BREAK'), stamp+1)
        with self.assertRaises((ValueError, ControlError)):
            frozen.rows('ETHUSDT', '15m')
        with self.assertRaises((ValueError, ControlError)):
            frozen.packet('BTCUSDT', '1h')

    def test_shared_cooldown_revokes_new_and_previously_frozen_reads(self):
        market = self.market()
        stamp = market.seed()
        frozen = market.read(market.names, stamp)
        self.assertEqual(market.store.control.failure(429, retry_after=60), 'BOUNDED_RETRY')
        with self.assertRaises((ValueError, ControlError)):
            market.read(market.names, stamp+1)
        with self.assertRaises((ValueError, ControlError)):
            frozen.rows('BTCUSDT', '5m')
        # Ending a cooldown cannot make a previously revoked Frozen valid again.
        after_cooldown = market.store.control.clock()+61
        with patch.object(market.store.control, 'clock', return_value=after_cooldown):
            self.assertEqual(market.read(market.names, stamp+1).generation['status'], 'READY')
            with self.assertRaises((ValueError, ControlError)):
                frozen.packet('BTCUSDT', '5m')

    def test_failed_directory_job_revokes_frozen_scope_before_unknown_proof_and_after_recovery(self):
        market = self.market()
        stamp = market.seed()
        frozen = market.read(market.names, stamp)
        dependent = sorted(n for n in market.names if split_topic(n)[0] == 'ETHUSDT')
        key = 'integration-directory-'+content_hash(dependent)[:16]
        identity = market.store.revoke_work('directory', market.epoch, dependent,
            'FIXTURE_DIRECTORY_TIMEOUT', stamp+1, job_key=key)
        self.assertEqual(market.store.control.get_intent(identity)['state'], 'FAILED')
        # The independent fence closes before any market proof write succeeds.
        with self.assertRaises((ValueError, ControlError)):
            frozen.rows('BTCUSDT', '5m')
        for name in dependent:
            value = market.store.get_proof('instrument', name)
            value.update(result='UNKNOWN', reason='FIXTURE_DIRECTORY_TIMEOUT')
            market.store.save_proof(value)
        with self.assertRaises((ValueError, ControlError)):
            frozen.packet('ETHUSDT', '1h')
        self.assertEqual(market.read([topic('BTCUSDT', '5m')], stamp+1).generation['status'], 'READY')
        with self.assertRaises((ValueError, ControlError)):
            market.read(market.names, stamp+1)
        market.apply('directory', dependent, directory(dependent), stamp+2)
        self.assertEqual(market.read(market.names, stamp+2).generation['status'], 'READY')
        with self.assertRaises((ValueError, ControlError)):
            frozen.rows('ETHUSDT', '1h')

    def test_daily_quiet_keeps_original_fifteen_bar_minimum(self):
        for count in (14, 15, 20, 35):
            with self.subTest(days=count):
                name = topic('ETHUSDT', '1d')
                market = self.market('daily-'+str(count), names=[name], stamp=MIDNIGHT+120000)
                stamp = market.started+1
                market.apply('directory', [name], directory([name]), stamp)
                body = rows('1d', stamp, count)
                market.apply('repair', [name], body, stamp)
                market.frames([name], stamp+1)
                if count < 15:
                    with self.assertRaises((ValueError, ControlError)):
                        market.read([name], stamp+1)
                    with self.assertRaisesRegex(ValueError, 'BINANCE_QUIET_WINDOW_INVALID'):
                        market.apply('quiet', [name], body, stamp+2)
                else:
                    self.assertEqual(len(market.read([name], stamp+1).rows('ETHUSDT', '1d')), count)
                    market.apply('quiet', [name], body, stamp+2)
                    self.assertEqual(market.store.get_proof('quiet', name)['result'], 'VALID')
                    self.assertEqual(market.read([name], stamp+2).generation['status'], 'READY')

    def test_stop_revokes_all_scopes_before_final_and_new_epoch_requires_new_evidence(self):
        market = self.market()
        stamp = market.seed()
        market.store.start_draining(market.epoch)
        for name in market.names:
            with self.assertRaises((ValueError, ControlError)):
                market.read([name], stamp+1)
        market.store.disconnect(market.epoch)
        with patch.object(stream, 'emit'):
            final = market.protocol.final('FIXTURE_STOP')
        self.assertTrue(final['complete'])
        self.assertEqual(final['received'], final['committed']+final['duplicate']+
                         final['invalid']+final['unprocessed_recorded'])
        protocol = health.Protocol(market.store, market.names, MANIFEST, stamp+2)
        self.assertNotEqual(protocol.epoch, market.epoch)
        market.store.ack(protocol.epoch, market.names)
        with self.assertRaises((ValueError, ControlError)):
            market.read(market.names, stamp+3)

    def test_real_session_ws_disconnect_closes_scopes_and_conserves_receipts(self):
        # Reuse only the deterministic peer/clock fixture. Session, pump threads,
        # pool, coordinated scheduler, intent publication and SQLite are real.
        with patch.object(sys, 'path', [str(ROOT/'tests')]+sys.path):
            from tests.test_hunter_binance_health_runtime import Fixture
        from research.hunter_binance_control import Control
        names = topics(('BTCUSDT', 'ETHUSDT'))
        fixture = Fixture(self.scratch/'session.sqlite3', names, until=100)
        Control.provision(fixture.store.path.with_name('control.sqlite3'), fixture.store.path)
        fixture.store.enable_control()
        original_client = fixture.client
        class DirectoryClient(original_client):
            def request(self, path, params, **kwargs):
                result = super().request(path, params, **kwargs)
                if path.endswith('exchangeInfo'):
                    result = directory([n for n in names if split_topic(n)[0]
                                        in json.loads(params['symbols'])])
                return result
        fixture.client = DirectoryClient
        original_connect = fixture.connect
        @contextlib.contextmanager
        def disconnected_peer():
            with original_connect() as peer:
                receive = peer.recv
                def recv(*args, **kwargs):
                    if fixture.elapsed >= 40:
                        raise OSError('FIXTURE_WS_DISCONNECT')
                    return receive(*args, **kwargs)
                peer.recv = recv
                yield peer
        fixture.connect = disconnected_peer
        with self.assertRaisesRegex(OSError, 'FIXTURE_WS_DISCONNECT'):
            fixture.run(coordinated=True)
        self.assertTrue(all(peer.closed for peer in fixture.peers))
        self.assertEqual(fixture.store.metadata()['connected'], '0')
        final = next(e for e in fixture.events if e.get('event') == 'BINANCE_HEALTH_SESSION_FINAL')
        self.assertTrue(final['complete'])
        self.assertGreater(final['received'], 0)
        self.assertEqual(final['received'], final['committed']+final['duplicate']+
                         final['invalid']+final['unprocessed_recorded'])
        self.assertEqual(fixture.store.control.status()['active_owners'], 0)
        self.assertEqual(fixture.store.freeze(names, fixture.clock(), fixture.clock())['status'], 'UNKNOWN')

    def test_unconfirmed_draining_latches_before_waiting_for_resource_cleanup(self):
        market=self.market()
        stamp=market.seed()
        frozen=market.read(market.names,stamp)
        with patch.object(market.store.control,'begin_draining',side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaisesRegex(ControlError,'DRAINING_UNCONFIRMED'):
                market.store.start_draining(market.epoch)
        self.assertEqual(market.store.control.gate_path.read_bytes(),b'1')
        with self.assertRaises((ValueError,ControlError)):
            frozen.check_delivery()


@unittest.skipUnless(sys.platform == 'linux', 'LINUX_PROCESS_ACCEPTANCE_REQUIRED_NOT_VERIFIED_ON_THIS_HOST')
class LinuxProcessCases(unittest.TestCase):
    setUp = DataCases.setUp
    market = DataCases.market

    def test_real_retirement_contention_discards_result_gates_scope_and_reverifies_once(self):
        from tests.test_hunter_binance_transport_process import Server, HELPER
        market = self.market()
        stamp = market.seed()
        dependent = sorted(n for n in market.names if split_topic(n)[0] == 'ETHUSDT')
        unaffected = [topic('BTCUSDT', '5m')]
        body = directory(dependent)
        context = dict(epoch=market.epoch, job_key='directory-contention',
                       kind='directory', scope=dependent)
        server = Server(body=json.dumps(body).encode())
        self.addCleanup(server.close)
        locked, release = Event(), Event()
        holder_errors, results, failures, events, armed = [], [], [], [], []
        control_path = market.path.with_name('control.sqlite3')
        def hold_control_writer():
            try:
                with sqlite3.connect(control_path, timeout=2) as db:
                    db.execute('BEGIN IMMEDIATE')
                    locked.set()
                    if not release.wait(4):
                        raise AssertionError('FIXTURE_CONTROL_LOCK_RELEASE_TIMEOUT')
            except BaseException as error:
                holder_errors.append(error)
                locked.set()
        holder = Thread(target=hold_control_writer)
        prepare = market.store.control.prepare_retire
        def arm_then_hold(owner, deadline):
            prepare(owner, deadline)
            armed.append((owner, deadline))
            holder.start()
            if not locked.wait(2):
                raise AssertionError('FIXTURE_CONTROL_LOCK_NOT_HELD')
        command = lambda url: [sys.executable, '-B', str(HELPER), url,
            str(os.getpid()), str(server.port), str(market.path.parent), 'normal']
        def request():
            try:
                results.append(public.Client(market.store).request('/api/v3/exchangeInfo',
                    dict(symbols=json.dumps(['ETHUSDT']), showPermissionSets='false'),
                    timeout=5, context=context,
                    observe=lambda event, **fields: events.append((event, fields))))
            except BaseException as error:
                failures.append(error)
        before = market.snapshot()
        runner = Thread(target=request)
        try:
            with patch.object(public, 'worker_command', command), \
                    patch.object(public, 'RETIRE_GRACE_SECONDS', .15), \
                    patch.object(public, 'CLEANUP_RETRY_SECONDS', 3), \
                    patch.object(market.store.control, 'prepare_retire', side_effect=arm_then_hold):
                runner.start()
                self.assertTrue(locked.wait(3), 'real request never reached retirement')
                self.assertEqual(holder_errors, [])
                self.assertTrue(server.closed.wait(2))
                owner, deadline = armed[0]
                # Reader fences use real monotonic time and the durable expiry
                # while the independent control writer is genuinely blocked.
                # Exceed both grace and one complete bounded SQLite attempt;
                # releasing inside that attempt could legitimately retire late
                # without taking the contention-recovery branch under test.
                Event().wait(max(0, deadline-time.monotonic())+.35)
                self.assertTrue(owner.complete and owner.reaped)
                self.assertFalse(owner.retired)
                with sqlite3.connect(control_path) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM owners').fetchone()[0], 1)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)
                    charged = db.execute('SELECT charged_total FROM budget WHERE id=1').fetchone()[0]
                    self.assertGreaterEqual(charged, 40)
                for name in dependent:
                    with self.assertRaises((ValueError, ControlError)):
                        market.read([name], stamp+1)
                self.assertEqual(market.read(unaffected, stamp+1).generation['status'], 'READY')
                release.set()
                runner.join(5)
                self.assertFalse(runner.is_alive())
        finally:
            release.set()
            if holder.ident is not None: holder.join(5)
            if runner.ident is not None: runner.join(6)
        self.assertEqual(holder_errors, [])
        self.assertEqual(results, [], 'quarantined HTTP body escaped to the caller')
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], public.QuarantinedRequestError)
        self.assertEqual(market.snapshot(), before, 'discarded body changed the market database')
        kinds = [item[0] for item in events]
        self.assertLess(kinds.index('RESPONSE'), kinds.index('REAPED'))
        self.assertLess(kinds.index('REAPED'), kinds.index('RETIRED'))
        self.assertEqual(kinds[-1], 'REQUEST_END')
        self.assertEqual(events[-1][1]['terminal'], 'QUARANTINED_RETIRED')
        with sqlite3.connect(control_path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM owners').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT charged_total FROM budget WHERE id=1').fetchone()[0], charged)
        with self.assertRaises((ValueError, ControlError)):
            market.read(dependent, stamp+2)
        recovery_id = market.store.control.claim_requeue(market.epoch, context['job_key'])
        self.assertEqual(recovery_id, failures[0].requeue_id)
        self.assertIsNone(market.store.control.claim_requeue(market.epoch, context['job_key']))
        second = Server(body=json.dumps(body).encode())
        self.addCleanup(second.close)
        command = lambda url: [sys.executable, '-B', str(HELPER), url,
            str(os.getpid()), str(second.port), str(market.path.parent), 'normal']
        with patch.object(public, 'worker_command', command):
            result = public.Client(market.store).request('/api/v3/exchangeInfo',
                dict(symbols=json.dumps(['ETHUSDT']), showPermissionSets='false'), timeout=5,
                context=dict(context, requeue_id=recovery_id))
        self.assertTrue(second.closed.wait(2))
        with self.assertRaises((ValueError, ControlError)):
            market.read(dependent, stamp+3)
        market.store.apply_work('directory', market.epoch, dependent, result, stamp+3, stamp+3,
            job_key=context['job_key'], recovery_id=recovery_id)
        self.assertEqual(market.read(market.names, stamp+3).generation['status'], 'READY')
        with sqlite3.connect(control_path) as db:
            self.assertEqual(db.execute('SELECT charged_total FROM budget WHERE id=1').fetchone()[0], charged+40)
            self.assertEqual(db.execute('SELECT state FROM quarantines WHERE id=?', (recovery_id,)).fetchone()[0], 'RESOLVED')

    def test_real_crash_before_and_after_market_commit_reconciles_exactly_once(self):
        for phase, code in (('before-market', 81), ('after-market', 82)):
            with self.subTest(phase=phase):
                market = self.market(phase)
                stamp = market.seed()
                name = topic('ETHUSDT', '15m')
                work = dict(source_head=HEAD, kind='repair', epoch=market.epoch,
                    names=[name], body=rows('15m', stamp+10), requested=stamp+10,
                    received=stamp+10, job_key='crash-'+phase)
                (market.path.parent/'crash-work.json').write_text(json.dumps(work), encoding='utf-8')
                before = market.snapshot()
                child = subprocess.run([sys.executable, '-B', str(CRASH_HELPER),
                    str(market.path.parent), phase], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                    env={k: os.environ[k] for k in ('PATH', 'LANG', 'SYSTEMROOT') if k in os.environ})
                self.assertEqual(child.returncode, code, child.stderr.decode(errors='replace'))
                reopened = Store.reader(market.path)
                reopened.enable_control()
                reopened.set_control_source(HEAD)
                pending = reopened.control.pending_intents(epoch=market.epoch)
                self.assertEqual(len(pending), 1)
                self.assertEqual(market.snapshot() == before, phase == 'before-market')
                with self.assertRaises((ValueError, ControlError)):
                    market.read([name], stamp+11)
                self.assertEqual(market.read([topic('BTCUSDT', '5m')], stamp+11).generation['status'], 'READY')
                reopened.apply_intent(pending[0])
                after = market.snapshot()
                reopened.apply_intent(reopened.control.get_intent(pending[0]['intent_id']))
                self.assertEqual(market.snapshot(), after, 'replay wrote market data twice')
                self.assertEqual(reopened.control.pending_intents(epoch=market.epoch), [])
                # Restored REST history alone cannot replace the current WS proof.
                with self.assertRaises((ValueError, ControlError)):
                    market.read([name], stamp+11)
                market.frames([name], stamp+12)
                self.assertEqual(market.read(market.names, stamp+12).generation['status'], 'READY')

    def test_full_1228_topic_writes_checkpoint_and_real_http_retirement_are_independent(self):
        from tests.test_hunter_binance_transport_process import Server, HELPER
        names = topics(['BTCUSDT']+['S%03dUSDT' % i for i in range(408)])
        self.assertEqual(len(names), 1228)
        market = self.market(names=names)
        with sqlite3.connect(market.path) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        required = [n for n in names if split_topic(n)[0] in ('BTCUSDT', 'S000USDT')]
        stamp = market.seed(required)
        body = directory(required)
        server = Server(body=json.dumps(body).encode())
        self.addCleanup(server.close)
        stop, started = Event(), Event()
        errors, checkpoints = [], []
        def pressure():
            started.set()
            try:
                market.frames(names, stamp+20)
            except BaseException as error:
                errors.append(error)
        def checkpoint():
            try:
                with sqlite3.connect(market.path, timeout=.1) as db:
                    while not stop.wait(.01):
                        checkpoints.append(tuple(db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()))
            except BaseException as error:
                errors.append(error)
        writer = Thread(target=pressure)
        checkpointer = Thread(target=checkpoint)
        command = lambda url: [sys.executable, '-B', str(HELPER), url,
            str(os.getpid()), str(server.port), str(market.path.parent), 'normal']
        context = dict(epoch=market.epoch, job_key='directory-under-market-lock',
                       kind='directory', scope=required)
        with sqlite3.connect(market.path.with_name('control.sqlite3')) as control:
            charged_before = control.execute('SELECT charged_total FROM budget WHERE id=1').fetchone()[0]
        try:
            with market.store.db() as held, patch.object(public, 'worker_command', command):
                # Hold an actual market writer while HTTP completes and retires in
                # the independent control database. Then replay the complete WS
                # scope concurrently with checkpoints, without inventing an
                # unbounded retry around the runtime's bounded market writer.
                held.execute("SELECT COUNT(*) FROM bars").fetchone()
                checkpointer.start()
                result = public.Client(market.store).request('/api/v3/exchangeInfo',
                    dict(symbols=json.dumps(['BTCUSDT', 'S000USDT']), showPermissionSets='false'),
                    timeout=5, context=context)
                self.assertEqual(result, body)
                self.assertTrue(server.closed.wait(2))
                with sqlite3.connect(market.path.with_name('control.sqlite3')) as control:
                    self.assertEqual(control.execute('SELECT COUNT(*) FROM owners').fetchone()[0], 0)
                    self.assertEqual(control.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)
                    self.assertEqual(control.execute('SELECT charged_total FROM budget WHERE id=1').fetchone()[0], charged_before+40)
            writer.start()
            self.assertTrue(started.wait(2))
            writer.join(30)
            self.assertFalse(writer.is_alive(), 'full-scope writer did not finish')
        finally:
            stop.set()
            if writer.ident is not None: writer.join(12)
            if checkpointer.ident is not None: checkpointer.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(checkpoints, 'no real WAL checkpoint was attempted')
        with sqlite3.connect(market.path) as db:
            self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
        self.assertEqual(market.read(required, stamp+20).generation['status'], 'READY')
        market.store.start_draining(market.epoch)
        market.store.disconnect(market.epoch)
        with patch.object(stream, 'emit'):
            final = market.protocol.final('FULL_SCOPE_CHECKPOINT_STOP')
        self.assertTrue(final['complete'])
        self.assertGreaterEqual(final['received'], 1228)
        self.assertEqual(final['received'], final['committed']+final['duplicate']+
                         final['invalid']+final['unprocessed_recorded'])


if __name__ == '__main__':
    unittest.main()
