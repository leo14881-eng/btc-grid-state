"""Actual session pump rejection, receipt accounting and scoped recovery.

Only the queue capacity, peer and scheduler tick are controlled. The pump,
SQLite stores, publication/fence checks, drain and shutdown remain real. A
one-entry queue makes overflow deterministic without a throughput benchmark.
"""
import contextlib
import datetime as dt
import json
import queue
from threading import Event, Lock
import unittest
from unittest.mock import patch

from research import hunter_binance_health as health
from research import hunter_binance_stream as stream
from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_control import Control, ControlError
from research.hunter_binance_health_store import Store, payload_hash
from research.hunter_binance_stream_store import split_topic, topic
from tests.test_hunter_binance_cache import rows, message
from tests import test_hunter_binance_coordinated_runtime as runtime


class PumpFixture:
    def __init__(self, case, recover_before_drain):
        self.case = case
        self.recover_before_drain = recover_before_drain
        self.names = [topic(symbol, '1h') for symbol in ('AAAUSDT', 'BBBUSDT', 'CCCUSDT')]
        self.accepted_name, self.rejected_name, self.other_name = self.names
        self.now = runtime.MIDNIGHT+120000
        self.clock_lock = Lock()
        self.initialized = Event()
        self.recorded = Event()
        self.inspected = Event()
        self.closed = Event()
        self.events, self.accepted, self.rejected = [], [], []
        self.protocol = None
        self.checked = False
        self.store = Store(case.scratch/'queue.sqlite3')
        Control.provision(case.scratch/'control.sqlite3', self.store.path)
        self.store.enable_control()
        fixture = self

        class BoundedQueue(stream.ObservedQueue):
            def __init__(self, maxsize):
                case.assertEqual(maxsize, 4096)
                super().__init__(1)

            def put(self, item, *args, **kwargs):
                if isinstance(item[1], dict) and 'id' not in item[1]:
                    try:
                        # This is Queue's real capacity check; no fabricated
                        # queue.Full and no sleeps racing a consumer thread.
                        super().put(item, block=False)
                    except queue.Full:
                        fixture.rejected.append(item)
                        raise
                    fixture.accepted.append(item)
                    return
                return super().put(item, *args, **kwargs)

            def get(self, *args, **kwargs):
                if kwargs.get('timeout') == .1 and fixture.initialized.is_set() and not fixture.checked:
                    fixture.checked = True
                    try:
                        case.assertTrue(fixture.recorded.wait(5), 'pump did not persist its rejected frame')
                        fixture.check_before_drain()
                    finally:
                        fixture.inspected.set()
                return super().get(*args, **kwargs)

        class RecordingProtocol(health.Protocol):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                fixture.protocol = self

            def receive_batch(self, batch):
                super().receive_batch(batch)
                if any(isinstance(value, dict) and 'id' in value for _, value, _ in batch):
                    if not fixture.initialized.is_set():
                        fixture.bootstrap()
                        fixture.initialized.set()

        self.queue_type = BoundedQueue
        self.protocol_type = RecordingProtocol

    def clock(self):
        with self.clock_lock:
            return self.now

    def advance(self):
        with self.clock_lock:
            self.now += 1
            return self.now

    def apply(self, kind, names):
        stamp = self.advance()
        body = (runtime.directory(names) if kind == 'directory'
                else rows(split_topic(names[0])[1], stamp))
        self.store.apply_work(kind, self.protocol.epoch, names, body, stamp, stamp,
                              job_key='queue-fixture-'+kind+'-'+','.join(names))

    def frame(self, name):
        symbol, interval = split_topic(name)
        stamp = self.advance()
        value = message(symbol, interval, stamp)
        self.protocol.arrived(0, json.dumps(value), stamp)
        self.protocol.receive_batch([(0, value, stamp)])

    def read(self, names):
        return consumer.read(names, dt.datetime.fromtimestamp(self.clock()/1000, dt.timezone.utc),
                             reader=self.store, clock=self.clock)

    def bootstrap(self):
        self.case.assertTrue(all(state['ack'] for state in self.store.states()))
        self.apply('directory', self.names)
        for name in self.names:
            self.apply('seed', [name])
            self.frame(name)
        self.old_scope = self.read(self.names)
        self.old_rejected = self.read([self.rejected_name])
        self.old_unaffected = self.read([self.accepted_name])
        self.before = self.store.counts(self.protocol.epoch)

    @contextlib.contextmanager
    def connect(self):
        fixture = self

        class Peer:
            def __init__(self):
                self.acks = queue.Queue()
                self.sent_market = 0

            def send(self, raw):
                request = json.loads(raw)
                fixture.case.assertEqual(request['params'], fixture.names)
                self.acks.put(json.dumps(dict(id=request['id'], result=None)))

            def ping(self, ack_on_close):
                fixture.case.assertIs(ack_on_close, False)
                pong = Event()
                pong.set()
                return pong

            def recv(self, timeout):
                if not self.acks.empty():
                    return self.acks.get_nowait()
                if not fixture.initialized.wait(5):
                    raise AssertionError('QUEUE_FIXTURE_BOOTSTRAP_TIMEOUT')
                if self.sent_market < 2:
                    name = fixture.names[self.sent_market]
                    self.sent_market += 1
                    symbol, interval = split_topic(name)
                    return json.dumps(message(symbol, interval, fixture.advance()))
                raise AssertionError('pump continued receiving after queue rejection')

        try:
            yield Peer()
        finally:
            self.closed.set()

    def pending(self):
        with self.store.db(readonly=True) as db:
            return [dict(row) for row in db.execute('SELECT * FROM bn_unprocessed ORDER BY id')]

    def projected(self, name):
        symbol, interval = split_topic(name)
        with self.store.db(readonly=True) as db:
            row=db.execute("""SELECT revision,epoch,generation,status,valid_until_ms
                FROM topic_state WHERE exchange='BINANCE' AND market='spot'
                  AND symbol=? AND interval=?""",(symbol,interval)).fetchone()
            self.case.assertIsNotNone(row)
            return dict(row)

    def check_before_drain(self):
        case = self.case
        case.assertEqual(len(self.accepted), 1)
        case.assertEqual(len(self.rejected), 1)
        _, rejected, received = self.rejected[0]
        saved = self.pending()
        case.assertEqual(len(saved), 1)
        record = saved[0]
        case.assertEqual((record['topic'], record['reason'], record['category']),
                         (self.rejected_name, 'RECEIVE_QUEUE_FULL', 'unprocessed_recorded'))
        case.assertEqual((record['received_at'], record['open_time'], record['event_time'], record['payload_sha256']),
                         (received, rejected['k']['t'], rejected['E'], payload_hash(rejected)))
        case.assertIsNone(record['resolved_at'])
        blocked=self.projected(self.rejected_name)
        case.assertEqual(blocked['status'],'invalid')
        case.assertEqual(blocked['valid_until_ms'],0)
        case.assertEqual((blocked['epoch'],blocked['generation']),
                         (self.protocol.epoch,self.protocol.epoch))
        case.assertNotEqual(self.projected(self.accepted_name)['status'],'invalid')
        counted = self.store.counts(self.protocol.epoch)
        for name in self.names:
            expected = int(name == self.rejected_name)
            case.assertEqual(counted[name]['unprocessed_recorded']-self.before[name]['unprocessed_recorded'], expected)
            case.assertEqual(counted[name]['committed'], self.before[name]['committed'])
            case.assertEqual((counted[name]['invalid'], counted[name]['duplicate']), (0, 0))
        # Receipt precedes queue admission. The rejected frame is still in the
        # received total; the accepted frame has not yet reached SQLite.
        received_total = sum(value['received'] for value in self.protocol.evidence.topics.values())
        case.assertEqual(received_total, len(self.names)+len(self.accepted)+len(self.rejected))
        with case.assertRaises((ValueError, ControlError)):
            self.read([self.rejected_name])
        with case.assertRaises((ValueError, ControlError)):
            self.old_scope.rows('AAAUSDT', '1h')
        with case.assertRaises((ValueError, ControlError)):
            self.old_rejected.check_delivery()
        self.old_unaffected.check_delivery()
        case.assertEqual(self.read([self.accepted_name]).generation['status'], 'READY')

        # A separate unresolved receipt is retained to prove repair uses the
        # exact topic, not just a shared interval or an entire received range.
        symbol, interval = split_topic(self.other_name)
        stamp = self.advance()
        other = message(symbol, interval, stamp)
        self.protocol.arrived(0, json.dumps(other), stamp)
        self.store.unprocessed(self.protocol.epoch, [(other, stamp)], 'FIXTURE_OTHER_PENDING_RECEIPT')
        if self.recover_before_drain:
            self.repair_rejected_scope()

    def repair_rejected_scope(self):
        before=self.projected(self.rejected_name)
        self.apply('repair', [self.rejected_name])
        self.frame(self.rejected_name)
        after=self.projected(self.rejected_name)
        self.case.assertIn(after['status'],('valid','quiet_verified'))
        self.case.assertGreater(after['revision'],before['revision'])
        self.case.assertEqual(after['epoch'],before['epoch'])
        self.case.assertEqual(self.projected(self.other_name)['status'],'invalid')
        self.case.assertEqual(self.read([self.rejected_name]).generation['status'], 'READY')
        with self.case.assertRaises((ValueError, ControlError)):
            self.read([self.other_name])
        with self.case.assertRaises((ValueError, ControlError)):
            self.old_rejected.check_delivery()
        saved = {row['topic']: row for row in self.pending()}
        self.case.assertIsNotNone(saved[self.rejected_name]['resolved_at'])
        self.case.assertIsNone(saved[self.other_name]['resolved_at'])
        self.case.assertEqual(saved[self.rejected_name]['category'], 'unprocessed_recorded')

    def run(self):
        original = self.store.unprocessed

        def record(epoch, observations, reason):
            original(epoch, observations, reason)
            if reason == 'RECEIVE_QUEUE_FULL':
                self.recorded.set()
                if not self.inspected.wait(5):
                    raise AssertionError('QUEUE_FIXTURE_INSPECTION_TIMEOUT')

        with patch.object(health, 'Protocol', self.protocol_type), \
                patch.object(stream, 'ObservedQueue', self.queue_type), \
                patch.object(stream, 'emit', side_effect=self.events.append), \
                patch.object(self.store, 'unprocessed', side_effect=record), \
                patch.object(health.CoordinatedJobs, 'tick', return_value=None):
            # Pause scheduling only: controlled fixture publications above
            # exercise the real apply_work path; no HTTP owner is fabricated.
            with self.case.assertRaisesRegex(RuntimeError, 'BINANCE_RECEIVE_BACKPRESSURE'):
                health.session(self.store, self.names, runtime.MANIFEST, runtime.HEAD,
                    stop=lambda:False, connect=self.connect, clock=self.clock,
                    sleep=lambda _:None, coordinated=True)
        self.case.assertTrue(self.closed.is_set())
        self.case.assertTrue(self.checked)
        final = next(value for value in self.events if value.get('event') == 'BINANCE_HEALTH_SESSION_FINAL')
        per_topic = [value for value in self.events if value.get('event') == 'BINANCE_HEALTH_TOPIC_FINAL']
        self.case.assertEqual({value['topic'] for value in per_topic}, set(self.names))
        self.case.assertTrue(final['complete'])
        for value in per_topic:
            self.case.assertTrue(value['conserved'])
            self.case.assertEqual(value['received'], sum(value[key] for key in
                ('committed', 'duplicate', 'invalid', 'unprocessed_recorded')))
        counts = self.store.counts(self.protocol.epoch)
        self.case.assertEqual(counts[self.accepted_name]['committed'], self.before[self.accepted_name]['committed']+1)
        self.case.assertEqual(counts[self.rejected_name]['unprocessed_recorded'], 1)
        self.case.assertEqual(counts[self.other_name]['unprocessed_recorded'], 1)
        shutdown = next(value for value in self.events if value.get('event') == 'BINANCE_HEALTH_SHUTDOWN_FINAL')
        self.case.assertFalse(shutdown['websocket_threads_alive'])
        self.case.assertEqual(shutdown['secondary_errors'], [])
        self.case.assertTrue(shutdown['work']['complete'])
        self.case.assertEqual(self.store.metadata()['connected'], '0')


class QueueRecoveryTests(unittest.TestCase):
    setUp = runtime.DataCases.setUp

    def test_pump_queue_rejection_counts_before_admission_and_scoped_same_epoch_repair(self):
        fixture = PumpFixture(self, recover_before_drain=True)
        fixture.run()

    def test_pump_rejected_receipt_survives_reconnect_and_only_matching_scope_recovers(self):
        fixture = PumpFixture(self, recover_before_drain=False)
        fixture.run()
        prior_epoch = fixture.protocol.epoch
        fixture.protocol = health.Protocol(fixture.store, fixture.names, runtime.MANIFEST, fixture.advance())
        self.assertNotEqual(fixture.protocol.epoch, prior_epoch)
        fixture.store.ack(fixture.protocol.epoch, fixture.names)
        fixture.apply('directory', fixture.names)
        for name in fixture.names:
            fixture.frame(name)
        for name in (fixture.rejected_name, fixture.other_name):
            with self.assertRaises((ValueError, ControlError)):
                fixture.read([name])
        fixture.repair_rejected_scope()
        self.assertEqual({row['epoch'] for row in fixture.pending()}, {prior_epoch})
        with self.assertRaises((ValueError, ControlError)):
            fixture.old_scope.check_delivery()
        fixture.store.start_draining(fixture.protocol.epoch)
        fixture.store.disconnect(fixture.protocol.epoch)
        with patch.object(stream, 'emit'):
            self.assertTrue(fixture.protocol.final('QUEUE_FIXTURE_RECOVERY_STOP')['complete'])


if __name__ == '__main__':
    unittest.main()
