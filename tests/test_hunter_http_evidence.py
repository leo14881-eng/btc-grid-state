"""Offline public HTTP diagnostics. All network and production writes forbidden."""
import copy
import contextlib
import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from research import hunter_http_evidence as evidence
from research import hunter_bybit_worker as worker
import test_hunter_bybit_worker as fixtures


def headers():
    result = Message()
    result['CF-Ray'] = '0123456789abcdef-SIN'
    result['Retry-After'] = '37'
    result['Authorization'] = 'Bearer SECRET_DO_NOT_STORE'
    result['Set-Cookie'] = 'SECRET_DO_NOT_STORE'
    return result


class HTTPDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('NETWORK_FORBIDDEN_IN_TEST'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_outer_http_errors_preserve_safe_evidence_without_body_or_url(self):
        for status in (403, 429, 502):
            err = urllib.error.HTTPError('https://example.invalid/?key=SECRET_DO_NOT_STORE', status,
                'SECRET_DO_NOT_STORE', headers(), io.BytesIO(b'<html>Access Denied SECRET_DO_NOT_STORE</html>'))
            with self.subTest(status=status), patch.object(worker.urllib.request, 'urlopen', side_effect=err):
                with self.assertRaises(evidence.ObservedHTTPError) as raised:
                    worker.request('/bybit/tickers')
            value = raised.exception.http_evidence
            self.assertEqual(value['http_status'], status)
            self.assertEqual(value['layer'], 'worker_http')
            self.assertEqual(value['body_kind'], 'html')
            self.assertEqual(value['body_markers'], ['ACCESS_DENIED_TEXT'])
            self.assertEqual(value['headers'], {'cf-ray': '0123456789abcdef-SIN', 'retry-after': '37'})
            self.assertNotIn('SECRET_DO_NOT_STORE', str(raised.exception))
            self.assertLess(len(str(raised.exception)), 1200)

    def test_body_read_is_bounded_and_sensitive_values_are_not_retained(self):
        class Body(io.BytesIO):
            def read(self, size=-1):
                self.requested = size
                return super().read(size)
        body = Body(b'SECRET_DO_NOT_STORE' * 10000)
        value = evidence.read_evidence(body, headers(), 403, 'worker_http')
        self.assertEqual(body.requested, 4097)
        self.assertEqual(value['body_bytes_sampled'], 4096)
        self.assertTrue(value['body_truncated'])
        self.assertNotIn('SECRET_DO_NOT_STORE', json.dumps(value))

    def test_json_scalar_and_non_json_failure_bodies_are_safe(self):
        for raw, kind, code in [(b'null', 'json', None), (b'broken SECRET_DO_NOT_STORE', 'text', None),
                               (b'{"retCode":10006,"api_key":"SECRET_DO_NOT_STORE"}', 'json', 10006)]:
            with self.subTest(raw=raw):
                value = evidence.read_evidence(io.BytesIO(raw), {}, 429, 'worker_http')
                self.assertEqual(value['body_kind'], kind)
                self.assertEqual(value['ret_code'], code)
                self.assertNotIn('SECRET_DO_NOT_STORE', json.dumps(value))

    def test_timeout_and_transport_do_not_expose_exception_reason(self):
        for error, code in [(TimeoutError('SECRET_DO_NOT_STORE'), 'WORKER_TIMEOUT'),
                            (urllib.error.URLError(TimeoutError('secret')), 'WORKER_TIMEOUT'),
                            (urllib.error.URLError('SECRET_DO_NOT_STORE'), 'WORKER_TRANSPORT_ERROR')]:
            with self.subTest(code=code), patch.object(worker.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaises(evidence.ObservedHTTPError) as raised:
                    worker.request('/bybit/early-klines', {'symbols': ['BTCUSDT'], 'end': fixtures.END})
            detail = worker.exception_detail(raised.exception)
            self.assertEqual(detail['code'], 'WORKER_TIMEOUT' if code == 'WORKER_TIMEOUT' else 'WORKER_KLINE_FAILURE')
            self.assertEqual(detail['http_evidence']['http_status'], 0)
            self.assertNotIn('SECRET_DO_NOT_STORE', json.dumps(detail))

    def test_non_json_success_response_is_not_success(self):
        response = io.BytesIO(b'SECRET_DO_NOT_STORE not json')
        response.headers = headers()
        response.status = 200
        with patch.object(worker.urllib.request, 'urlopen', return_value=response):
            with self.assertRaises(evidence.ObservedHTTPError) as raised:
                worker.request('/bybit/tickers')
        self.assertEqual(raised.exception.diagnostic_code, 'BYBIT_WORKER_NON_JSON')
        self.assertEqual(raised.exception.http_evidence['failure_kind'], 'invalid_json')
        self.assertNotIn('SECRET_DO_NOT_STORE', str(raised.exception))

    def test_outer_error_retains_allowlisted_nested_worker_evidence(self):
        upstream = evidence.read_evidence(io.BytesIO(b'Access Denied'), {}, 403, 'bybit_upstream')
        value = evidence.read_evidence(io.BytesIO(json.dumps({'diagnostics': upstream}).encode()), {}, 502, 'worker_http')
        self.assertEqual(value['upstream'], upstream)
        evidence.validate_evidence(value)
        value['upstream']['upstream'] = copy.deepcopy(upstream)
        with self.assertRaisesRegex(ValueError, 'HTTP_EVIDENCE_INVALID'):
            evidence.validate_evidence(value)

    def test_read_failure_and_unknown_headers_never_add_arbitrary_text(self):
        class Broken:
            def read(self, count):
                raise OSError('SECRET_DO_NOT_STORE')
        value = evidence.read_evidence(Broken(), {'cf-ray': 'secret-url', 'retry-after': 'SECRET_DO_NOT_STORE'}, 502, 'worker_http')
        self.assertTrue(value['body_read_failed'])
        self.assertEqual(value['body_kind'], 'unavailable')
        self.assertEqual(value['headers'], {})

    def test_correlation_mismatch_and_non_whitelisted_shapes_fail_closed(self):
        upstream = evidence.read_evidence(None, {}, 403, 'bybit_upstream')
        outer = evidence.read_evidence(None, {}, 502, 'worker_http')
        outer['headers']['x-hunter-request-id'] = '00000000-0000-0000-0000-000000000001'
        outer['upstream'] = upstream
        with self.assertRaisesRegex(ValueError, 'HTTP_EVIDENCE_INVALID'):
            evidence.validate_evidence(outer)
        upstream['headers'] = dict(outer['headers'])
        evidence.validate_evidence(outer)
        for changed in ({'schema': 'unknown'}, {'body_bytes_sampled': 4097},
                        {'body_markers': ['ACCESS_DENIED_TEXT', 'ACCESS_DENIED_TEXT']},
                        {'ret_code': True}, {'raw_body': 'SECRET_DO_NOT_STORE'}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'HTTP_EVIDENCE_INVALID'):
                evidence.validate_evidence({**upstream, **changed})

    def test_diagnostics_go_to_logs_and_keep_legacy_snapshot_partial_semantics(self):
        legacy = fixtures.partial_fetch({'FLUIDUSDT': {'15': 'Error: BYBIT_HTTP_403'}})
        def fetch(path, body):
            response = legacy(path, body)
            if path == '/bybit/early-klines':
                response['result']['failure_diagnostics'] = {'FLUIDUSDT': {'15':
                    evidence.read_evidence(io.BytesIO(b'Access Denied'), headers(), 403, 'bybit_upstream')}}
            return response
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            _, status, snapshot, _ = fixtures.WorkerTests().run_collect(('BTC', 'FLUID'), fetch)
        self.assertFalse(status['signal_complete'])
        self.assertNotIn('FLUID', snapshot['early_signals'])
        detail = snapshot['signal_failure_details']['FLUIDUSDT']['15']
        self.assertEqual(detail['stage'], 'worker_response')
        self.assertNotIn('http_evidence', detail)
        record = json.loads(log.getvalue().split('HUNTER_BYBIT_HTTP_EVIDENCE ', 1)[1])
        self.assertEqual(record['http_evidence']['http_status'], 403)
        self.assertNotIn('SECRET_DO_NOT_STORE', log.getvalue())
        snapshot['signal_failure_details']['FLUIDUSDT']['15']['code'] = 'BYBIT_HTTP_429'
        with self.assertRaisesRegex(ValueError, 'CHECKSUM'):
            worker.validate(snapshot, fixtures.NOW)
        snapshot['snapshot_sha256'] = worker.digest({k: v for k, v in snapshot.items() if k != 'snapshot_sha256'})
        snapshot['signal_failure_details']['FLUIDUSDT']['15']['http_evidence'] = record['http_evidence']
        snapshot['snapshot_sha256'] = worker.digest({k: v for k, v in snapshot.items() if k != 'snapshot_sha256'})
        with self.assertRaisesRegex(ValueError, 'FAILURE_DETAIL_INVALID'):
            worker.validate(snapshot, fixtures.NOW)

    def test_success_scope_interval_and_status_conflicts_fail_closed(self):
        diagnostic = evidence.read_evidence(io.BytesIO(b'denied'), {}, 403, 'bybit_upstream')
        base = dict(end=fixtures.END, klines={}, failures={'FLUIDUSDT': {'15': 'Error: BYBIT_HTTP_403'}})
        for changed in [
            {'failure_diagnostics': {'OTHERUSDT': {'15': diagnostic}}},
            {'failure_diagnostics': {'FLUIDUSDT': {'60': diagnostic}}},
            {'failure_diagnostics': {'FLUIDUSDT': {'15': {**diagnostic, 'http_status': 429}}}},
            {'klines': {'FLUIDUSDT': {'15': fixtures.candle('FLUIDUSDT', '15', 25)}}},
            {'failure_diagnostics': {'FLUIDUSDT': {'15': {**diagnostic, 'headers': {'authorization': 'secret'}}}}},
        ]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                worker.batch_parts({**base, **changed}, ['FLUIDUSDT'], fixtures.END)

    def test_ticker_502_never_relabels_old_snapshot_as_current_success(self):
        _, _, snapshot, _ = fixtures.WorkerTests().run_collect()
        old = json.dumps(snapshot).encode()
        with tempfile.TemporaryDirectory() as directory, patch.object(worker, 'DEFAULT', pathlib.Path(directory)/'snapshot.json'):
            worker.DEFAULT.write_bytes(old)
            error = urllib.error.HTTPError('https://worker.invalid', 502, 'bad', headers(), io.BytesIO(b'bad gateway'))
            with patch.object(worker.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaises(evidence.ObservedHTTPError):
                    worker.collect({'BTC'}, fixtures.NOW + dt.timedelta(hours=1), worker.request,
                                   lambda: (['BTC', 'FLUID'], {'cache_hit': True}))
            self.assertEqual(worker.DEFAULT.read_bytes(), old)
            with self.assertRaisesRegex(ValueError, 'STALE_OR_FUTURE'):
                worker.validate(json.loads(old), fixtures.NOW + dt.timedelta(hours=1))


if __name__ == '__main__':
    unittest.main()
