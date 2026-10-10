"""Read actual offline Worker wire records through Python; no external effects."""
import contextlib
import copy
import io
import json
import pathlib
import threading
import time
import hashlib
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from research import hunter_http_evidence as evidence
from research import hunter_bybit_worker as worker
import test_hunter_bybit_worker as fixtures

CONTRACT = json.loads((pathlib.Path(__file__).parent / 'fixtures/hunter_observation_responses.json').read_text())
PREFIX = 'HUNTER_BYBIT_REQUEST_OBSERVATION '


def request():
    return urllib.request.Request('https://worker.invalid/bybit/early-klines',
        data=json.dumps({'symbols': ['BTCUSDT', 'FLUIDUSDT'], 'end': CONTRACT['end']}).encode())


def stream(body, headers=None, status=200):
    result = io.BytesIO(json.dumps(body).encode())
    result.status = status
    result.headers = headers or {}
    return result


class ObservationTransportTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('NETWORK_FORBIDDEN'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_grouped_lines_preserve_every_record_with_byte_bounds_and_manifest(self):
        base = CONTRACT['success']['body']['request_observations'][0]
        rows = [dict(base, upstream_job_index=index, symbol='ASSET'+str(index)+'USDT') for index in range(40)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            evidence.emit_record_groups('HUNTER_BYBIT_REQUEST_OBSERVATION', rows)
        lines = output.getvalue().splitlines()
        self.assertLess(len(lines), len(rows))
        self.assertTrue(all(len((line+'\n').encode()) <= evidence.GROUP_LINE_BYTES for line in lines))
        chunks = [json.loads(line.split(' ', 1)[1]) for line in lines]
        self.assertEqual([chunk['part'] for chunk in chunks], list(range(len(chunks))))
        self.assertTrue(all(chunk['parts'] == len(chunks) and chunk['record_total'] == 40 for chunk in chunks))
        restored = [row for chunk in chunks for row in chunk['records']]
        self.assertEqual(restored, rows)
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual({chunk['group_id'] for chunk in chunks}, {digest})

    def test_unrepresentable_log_groups_fail_explicitly_without_partial_output(self):
        for rows in ([{'value': 'x'*evidence.GROUP_LINE_BYTES}], [{}]*41):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                evidence.emit_record_groups('HUNTER_BYBIT_REQUEST_OBSERVATION', rows)
            self.assertEqual(output.getvalue(), 'HUNTER_BYBIT_OBSERVATION_DROPPED GROUP_LIMIT_OR_INVALID\n')

    def test_real_worker_success_and_partial_reach_journal_without_snapshot_changes(self):
        for mode in ('success', 'partial'):
            wire = CONTRACT[mode]
            legacy_body = {k: v for k, v in wire['body'].items() if k != 'request_observations'}
            ticker = fixtures.partial_fetch({})('/bybit/tickers', None)
            def old_fetch(path, body=None):
                return copy.deepcopy(ticker if path == '/bybit/tickers' else legacy_body)
            with contextlib.redirect_stdout(io.StringIO()):
                expected = fixtures.WorkerTests().run_collect(('BTC', 'FLUID'), old_fetch)[2]
            def opened(req, timeout):
                self.assertEqual(timeout, 60)
                if req.selector == '/bybit/tickers':
                    self.assertIsNone(req.data)
                    return stream(ticker)
                self.assertEqual(json.loads(req.data), json.loads(request().data))
                return stream(copy.deepcopy(wire['body']), wire['headers'])
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(evidence.urllib.request, 'urlopen', side_effect=opened) as fetch:
                actual = fixtures.WorkerTests().run_collect(('BTC', 'FLUID'), worker.request)[2]
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(actual, expected)
            self.assertNotIn('request_observations', json.dumps(actual))
            records = [json.loads(line[len(PREFIX):]) for line in output.getvalue().splitlines() if line.startswith(PREFIX)]
            upstream = [row for row in records if row['layer'] == 'bybit_upstream']
            self.assertEqual(len(upstream), 4)
            self.assertEqual({row['upstream_job_index'] for row in upstream}, set(range(4)))
            for row in upstream:
                self.assertEqual(row['generation_end_ms'], CONTRACT['end'])
                self.assertEqual(row['batch_id'], 0)
                self.assertEqual(row['worker_request_id'], wire['headers']['x-hunter-request-id'])
                self.assertEqual(row['started_at_utc'], '2026-10-05T18:07:00.000Z')
            self.assertEqual(sum(row['http_status'] == 403 for row in upstream), int(mode == 'partial'))

    def test_invalid_or_unbound_metadata_is_stripped_without_changing_market_data(self):
        wire = CONTRACT['success']
        changes = [{'worker_request_id': '00000000-0000-4000-8000-000000000002'},
            {'symbol': 'OTHERUSDT'}, {'upstream_job_index': 1}, {'http_status': True},
            {'duration_ms': -1}, {'ret_code': 'SECRET_DO_NOT_STORE'},
            {'started_at_utc': 'SECRET_DO_NOT_STORE'}, {'headers': {'authorization': 'SECRET_DO_NOT_STORE'}},
            {'raw_body': 'SECRET_DO_NOT_STORE'}]
        candidates = []
        for change in changes:
            body = copy.deepcopy(wire['body'])
            body['request_observations'][0].update(change)
            candidates.append(body)
        for observations in ([], wire['body']['request_observations']*11, 'SECRET_DO_NOT_STORE'):
            candidates.append({**wire['body'], 'request_observations': observations})
        expected = {k: v for k, v in wire['body'].items() if k != 'request_observations'}
        for body in candidates:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = evidence.consume_observations(body, wire['headers'], request())
            self.assertEqual(result, expected)
            self.assertEqual(output.getvalue(), 'HUNTER_BYBIT_OBSERVATION_DROPPED INVALID_OR_UNBOUND\n')

    def test_old_worker_without_metadata_remains_compatible_and_logger_failure_is_nonfatal(self):
        body = {'retCode': 0, 'result': {'category': 'spot'}}
        self.assertIs(evidence.consume_observations(body, {}, request()), body)
        wire = CONTRACT['success']
        with patch('builtins.print', side_effect=OSError('LOGGER_FAILED')):
            result = evidence.consume_observations(wire['body'], wire['headers'], request())
        self.assertEqual(result, {k: v for k, v in wire['body'].items() if k != 'request_observations'})

    def test_two_batch_contexts_do_not_cross_threads(self):
        wire = CONTRACT['success']
        barrier = threading.Barrier(2)
        class FragmentingWriter(io.StringIO):
            """Real print sink that yields between chunks, not a list.append mock."""
            def __init__(self):
                super().__init__()
                self.active = 0
                self.peak = 0
                self.guard = threading.Lock()

            def write(self, text):
                with self.guard:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                try:
                    for offset in range(0, len(text), 17):
                        super().write(text[offset:offset+17])
                        time.sleep(0)  # Permit the other real thread to write.
                    return len(text)
                finally:
                    with self.guard:
                        self.active -= 1
        output = FragmentingWriter()
        def run(index):
            with evidence.observation_context(CONTRACT['end'] + index, index):
                barrier.wait(timeout=5)
                result = evidence.request_json(request(), 60)
                self.assertEqual(result, {k: v for k, v in wire['body'].items() if k != 'request_observations'})
            self.assertEqual(evidence.REQUEST_CONTEXT.get(), {})
        with contextlib.redirect_stdout(output), patch.object(evidence.urllib.request, 'urlopen',
                side_effect=lambda *args, **kwargs: stream(wire['body'], wire['headers'])):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(run, (0, 1)))
        self.assertEqual(output.peak, 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 10)  # Eight subrequests plus two outer calls.
        self.assertTrue(all(line.startswith(PREFIX) for line in lines))
        records = [json.loads(line[len(PREFIX):]) for line in lines]
        self.assertEqual(sum(row['layer'] == 'bybit_upstream' for row in records), 8)
        for row in records:
            self.assertEqual(row['generation_end_ms'], CONTRACT['end'] + row['batch_id'])
        self.assertEqual(evidence.REQUEST_CONTEXT.get(), {})
        # Negative control: this very sink must expose concurrent writes when
        # serialization is removed; otherwise the fixture would mask the bug.
        output = FragmentingWriter()
        with contextlib.redirect_stdout(output), patch.object(evidence, 'LOG_LOCK', contextlib.nullcontext()), \
                patch.object(evidence.urllib.request, 'urlopen', side_effect=lambda *args, **kwargs: stream(wire['body'], wire['headers'])):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(run, (0, 1)))
        self.assertGreater(output.peak, 1)

    def test_failing_stream_preserves_results_errors_and_releases_output_lock(self):
        wire = CONTRACT['success']
        class FailingWriter:
            def write(self, text):
                raise OSError('SINK_UNAVAILABLE')
            def flush(self):
                raise OSError('SINK_UNAVAILABLE')
        with contextlib.redirect_stdout(FailingWriter()):
            with patch.object(evidence.urllib.request, 'urlopen', side_effect=lambda *args, **kwargs: stream(wire['body'], wire['headers'])):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: evidence.request_json(request(), 60), (0, 1)))
            self.assertEqual(results, [{k: v for k, v in wire['body'].items() if k != 'request_observations'}]*2)
            with patch.object(evidence.urllib.request, 'urlopen', side_effect=TimeoutError('secret')):
                with self.assertRaisesRegex(evidence.ObservedHTTPError, 'WORKER_TIMEOUT'):
                    evidence.request_json(request(), 60)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(evidence.emit_public_log('RECOVERED {}'))
        self.assertEqual(output.getvalue(), 'RECOVERED {}\n')

    def test_reversed_out_of_range_and_inconsistent_worker_times_are_dropped(self):
        wire = CONTRACT['success']
        changes = [
            {'started_at_utc': '2026-10-05T18:07:00.001Z'},
            {'completed_at_utc': '2026-10-05T18:09:00.001Z', 'duration_ms': 120000},
            {'duration_ms': 120001},
            {'duration_ms': 1001},
            {'started_at_utc': '2026-10-05T18:06:57.000Z', 'duration_ms': 1999},
            {'started_at_utc': '2026-10-05T18:04:59.999Z', 'completed_at_utc': '2026-10-05T18:04:59.999Z'},
            {'started_at_utc': '2026-10-05T18:07:01.001Z', 'completed_at_utc': '2026-10-05T18:07:01.001Z'},
            {'started_at_utc': '2026-13-05T18:07:00.000Z'},
        ]
        for change in changes:
            body = copy.deepcopy(wire['body'])
            body['request_observations'][0].update(change)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = evidence.consume_observations(body, wire['headers'], request())
            self.assertEqual(result, {k: v for k, v in body.items() if k != 'request_observations'})
            self.assertEqual(output.getvalue(), 'HUNTER_BYBIT_OBSERVATION_DROPPED INVALID_OR_UNBOUND\n')

    def test_worker_clock_bounds_allow_rounding_without_cross_machine_order_claim(self):
        wire = copy.deepcopy(CONTRACT['success'])
        wire['body']['request_observations'][0].update(
            started_at_utc='2026-10-05T18:06:59.000Z', duration_ms=1000)
        # Deliberately reverse arrival order and use unrelated local generation
        # clock. Neither is evidence of subrequest chronological order.
        wire['body']['request_observations'].reverse()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), evidence.observation_context(CONTRACT['end']+3600000, 0):
            result = evidence.consume_observations(wire['body'], wire['headers'], request())
        self.assertEqual(result, {k: v for k, v in wire['body'].items() if k != 'request_observations'})
        records = [json.loads(line[len(PREFIX):]) for line in output.getvalue().splitlines()]
        self.assertEqual([row['upstream_job_index'] for row in records], [3, 2, 1, 0])
        self.assertEqual(records[-1]['duration_ms'], 1000)
        self.assertTrue(all(row['generation_end_ms'] == CONTRACT['end']+3600000 for row in records))

    def test_spot_failure_transport_and_scan_generation_binding(self):
        wire = CONTRACT['spot_failure']
        error = urllib.error.HTTPError('https://worker.invalid', 502, 'failure', wire['headers'], stream(wire['body']))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), evidence.observation_context(CONTRACT['end']), \
                patch.object(evidence.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(evidence.ObservedHTTPError) as raised:
                evidence.request_json(urllib.request.Request('https://worker.invalid/bybit/spot'), 15)
            evidence.log_generation_binding('20261005T180700000000Z', dict(
                source='OFFICIAL_BYBIT_V5_VIA_WORKER', snapshot_sha256='0'*64,
                captured_at_utc='2026-10-05T18:07:00+00:00'))
        self.assertEqual(raised.exception.http_evidence['upstream']['http_status'], 403)
        lines = output.getvalue().splitlines()
        records = [json.loads(line[len(PREFIX):]) for line in lines if line.startswith(PREFIX)]
        self.assertEqual([row['layer'] for row in records], ['bybit_upstream', 'worker_http'])
        self.assertEqual(records[0]['generation_end_ms'], CONTRACT['end'])
        binding = json.loads(next(line.split(' ', 1)[1] for line in lines if line.startswith('HUNTER_BYBIT_GENERATION_BINDING ')))
        self.assertEqual(binding['scan_generation_id'], '20261005T180700000000Z')
        self.assertEqual(binding['generation_end_ms'], records[0]['generation_end_ms'])
