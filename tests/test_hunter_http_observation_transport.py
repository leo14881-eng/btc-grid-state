"""Read actual offline Worker wire records through Python; no external effects."""
import contextlib
import copy
import io
import json
import pathlib
import threading
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
        output = []
        def run(index):
            with evidence.observation_context(CONTRACT['end'] + index, index):
                barrier.wait(timeout=5)
                evidence.consume_observations(wire['body'], wire['headers'], request())
            self.assertEqual(evidence.REQUEST_CONTEXT.get(), {})
        with patch('builtins.print', side_effect=lambda line, **kwargs: output.append(line)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(run, (0, 1)))
        records = [json.loads(line[len(PREFIX):]) for line in output]
        self.assertEqual(len(records), 8)
        for row in records:
            self.assertEqual(row['generation_end_ms'], CONTRACT['end'] + row['batch_id'])
        self.assertEqual(evidence.REQUEST_CONTEXT.get(), {})

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
