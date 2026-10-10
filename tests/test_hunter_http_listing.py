"""Offline full-list diagnostics; no expiry bypass or changes to cache/Alpha behavior."""
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import pathlib
import tempfile
import types
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from research import hunter_bybit_availability as mod
from research import hunter_bybit_worker as worker
from research import hunter_http_evidence as evidence

FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures'
REFERENCE = (FIXTURES / 'hunter_bybit_availability_legacy.txt').read_bytes()
legacy = types.ModuleType('hunter_availability_legacy_reference')
exec(compile(REFERENCE, str(FIXTURES / 'hunter_bybit_availability_legacy.txt'), 'exec'), legacy.__dict__)
CONTRACT = json.loads((FIXTURES / 'hunter_http_worker_responses.json').read_text())['spot']
NOW = dt.datetime(2026, 10, 5, 18, 7, tzinfo=dt.timezone.utc)
SECRET = 'SECRET_DO_NOT_STORE'


def response(body, status=200, headers=None):
    stream = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
    stream.status = status
    stream.headers = headers or {}
    return stream


class ListingDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('NETWORK_FORBIDDEN'))
            guard.start()
            self.addCleanup(guard.stop)

    def failure(self, error, expected=RuntimeError):
        output = io.StringIO()
        with patch.object(mod, 'SPOT_PROXY', 'https://' + SECRET + '.invalid'), contextlib.redirect_stdout(output), \
                patch.object(mod.urllib.request, 'urlopen', side_effect=error) as request:
            with self.assertRaises(expected) as raised:
                mod.request(mod.SPOT_PROXY + '/bybit/spot', headers={'Authorization': SECRET})
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs['timeout'], 15)
        self.assertIsNone(request.call_args.args[0].data)
        self.assertNotIn(SECRET, output.getvalue() + str(raised.exception))
        self.assertNotIn('https://', output.getvalue() + str(raised.exception))
        record = json.loads(output.getvalue().split('HUNTER_BYBIT_HTTP_EVIDENCE ', 1)[1])
        evidence.validate_evidence(record['http_evidence'])
        return raised.exception, record['http_evidence']

    def test_direct_listing_http_codes_are_safe_and_do_not_retry(self):
        headers = Message()
        headers['CF-Ray'] = '0123456789abcdef-SIN'
        headers['Set-Cookie'] = SECRET
        for status in (403, 429, 502):
            with self.subTest(status=status):
                error = urllib.error.HTTPError('https://' + SECRET, status, SECRET, headers,
                    io.BytesIO(('<html>Access Denied ' + SECRET + '</html>').encode()))
                exc, detail = self.failure(error)
                self.assertEqual(detail['http_status'], status)
                self.assertEqual(detail['body_kind'], 'html')
                self.assertEqual(detail['headers'], {'cf-ray': '0123456789abcdef-SIN'})
                self.assertEqual(mod.classify_error(exc), {403: 'HTTP_403', 429: 'RATE_LIMIT', 502: 'UPSTREAM_ERROR'}[status])

    def test_legacy_worker_502_keeps_reported_code_without_inventing_upstream_evidence(self):
        body = {'ok': False, 'error': 'BYBIT_FETCH_FAILED', 'detail': 'Error: BYBIT_HTTP_403', 'secret': SECRET}
        error = urllib.error.HTTPError('https://' + SECRET, 502, SECRET, {}, response(body))
        exc, detail = self.failure(error)
        self.assertEqual(detail['worker_error_code'], 'BYBIT_HTTP_403')
        self.assertNotIn('upstream', detail)
        self.assertEqual(mod.classify_error(exc), 'HTTP_403')

    def test_current_worker_spot_failures_keep_both_layers_and_request_binding(self):
        for mode in ('403', '429', '502', 'non_json'):
            with self.subTest(mode=mode):
                row = CONTRACT[mode]
                error = urllib.error.HTTPError('https://worker.invalid', row['status'], 'bad', row['headers'], response(row['body']))
                _, detail = self.failure(error)
                self.assertEqual(detail['http_status'], 502)
                self.assertEqual(detail['upstream']['http_status'], 200 if mode == 'non_json' else int(mode))
                self.assertEqual(detail['headers']['x-hunter-request-id'], detail['upstream']['headers']['x-hunter-request-id'])

    def test_non_json_200_logs_parse_failure_and_remains_failure(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(mod.urllib.request, 'urlopen', return_value=response((SECRET + ' not json').encode())) as request:
            with self.assertRaisesRegex(ValueError, 'BYBIT_WORKER_NON_JSON'):
                mod.request(mod.SPOT_PROXY + '/bybit/spot')
        request.assert_called_once()
        record = json.loads(output.getvalue().split('HUNTER_BYBIT_HTTP_EVIDENCE ', 1)[1])
        self.assertEqual(record['http_evidence']['failure_kind'], 'invalid_json')
        self.assertNotIn(SECRET, output.getvalue())

    def test_truncated_body_is_bounded_and_not_saved(self):
        class Body(io.BytesIO):
            def read(self, size=-1):
                self.requested = size
                return super().read(size)
        body = Body((SECRET * 10000).encode())
        error = urllib.error.HTTPError('https://' + SECRET, 502, SECRET, {}, body)
        _, detail = self.failure(error)
        self.assertEqual(body.requested, 4097)
        self.assertEqual(detail['body_bytes_sampled'], 4096)
        self.assertTrue(detail['body_truncated'])

    def test_timeout_and_transport_are_safe_without_retries(self):
        for error, expected, kind in ((TimeoutError(SECRET), TimeoutError, 'timeout'),
                (urllib.error.URLError(SECRET), urllib.error.URLError, 'transport')):
            with self.subTest(kind=kind):
                _, detail = self.failure(error, expected)
                self.assertEqual(detail['failure_kind'], kind)

    def test_expired_cache_failure_preserves_bytes_without_stale_success(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(mod, 'SPOT_CACHE', pathlib.Path(folder) / 'cache.json'):
            with patch.object(mod, 'request', return_value=CONTRACT['success']['body']):
                mod.spot_proxy_universe(NOW)
            before = mod.SPOT_CACHE.read_bytes()
            error = urllib.error.HTTPError('https://worker.invalid', 403, SECRET, {}, io.BytesIO(SECRET.encode()))
            with patch.object(mod.urllib.request, 'urlopen', side_effect=error) as request, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'HTTP_403'):
                    mod.spot_proxy_universe(NOW + dt.timedelta(days=1, seconds=17))
            request.assert_called_once()
            self.assertEqual(mod.SPOT_CACHE.read_bytes(), before)

    def test_success_cache_bytes_ttl_and_result_match_real_legacy_implementation(self):
        self.assertEqual(hashlib.sha1(b'blob ' + str(len(REFERENCE)).encode() + b'\0' + REFERENCE).hexdigest(),
                         '8b7370553dcdb22c015c960bd1d7902328f52f28')
        results = []
        for module in (legacy, mod):
            with tempfile.TemporaryDirectory() as folder, patch.object(module, 'SPOT_CACHE', pathlib.Path(folder) / 'cache.json'), \
                    patch.object(module.urllib.request, 'urlopen', side_effect=lambda *a, **kw: response(CONTRACT['success']['body'])) as request:
                value = module.spot_proxy_universe(NOW)
                fresh = module.spot_proxy_universe(NOW + dt.timedelta(hours=23, minutes=59))
                request.assert_called_once()
                results.append((value, fresh, module.SPOT_CACHE.read_bytes()))
        self.assertEqual(results[0], results[1])

    def test_alpha_request_bytes_are_unchanged_and_public_helpers_are_shared(self):
        self.assertIs(mod.request_json, worker.request_json)
        data = b'{"tokenTag": 0}'
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(mod.urllib.request, 'urlopen', return_value=response({'retCode': 0})) as request:
            self.assertEqual(mod.request(mod.ALPHA_PROXY, data, {'X-BAPI-SIGN': 'test-signature'}), {'retCode': 0})
        self.assertEqual(request.call_args.args[0].data, data)
        self.assertEqual(request.call_args.args[0].get_header('X-bapi-sign'), 'test-signature')
        self.assertEqual(output.getvalue(), '')

    def test_forged_reported_code_and_nested_conflicts_are_not_trusted(self):
        body = {'error': 'BYBIT_FETCH_FAILED', 'detail': 'Error: BYBIT_HTTP_403 ' + SECRET}
        _, detail = self.failure(urllib.error.HTTPError('https://worker.invalid', 502, 'bad', {}, response(body)))
        self.assertNotIn('worker_error_code', detail)
        row = copy.deepcopy(CONTRACT['403'])
        row['body']['diagnostics']['http_status'] = 429
        candidate = evidence.read_evidence(response(row['body']), row['headers'], 502, 'worker_http')
        with self.assertRaisesRegex(ValueError, 'HTTP_EVIDENCE_INVALID'):
            evidence.validate_evidence(candidate)


if __name__ == '__main__':
    unittest.main()
