"""Real PR88 collector/validator versus the diagnostic-only change; offline fixtures."""
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
from unittest.mock import patch

from research import hunter_bybit_worker as current
from research import hunter_http_evidence as evidence
from research import hunter_bybit_signal_capture as signal_capture
import test_hunter_bybit_worker as fixtures

ROOT = pathlib.Path(__file__).resolve().parent / 'fixtures'
REFERENCE = (ROOT / 'hunter_bybit_worker_pr88.txt').read_bytes()
LEGACY_BLOB = '6a0c3d67f19cfa470a3f304ddf09e425f72ae354'
legacy = types.ModuleType('hunter_bybit_worker_pr88_reference')
exec(compile(REFERENCE, str(ROOT / 'hunter_bybit_worker_pr88.txt'), 'exec'), legacy.__dict__)
CONTRACT = json.loads((ROOT / 'hunter_http_worker_responses.json').read_text())
FIELDS = {'code', 'detail_redacted', 'detail_truncated', 'batch_id', 'symbol', 'interval', 'generation_end_ms', 'stage'}


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('NETWORK_FORBIDDEN'))
            guard.start()
            self.addCleanup(guard.stop)

    def collect(self, module, mode='partial', old_worker=False, error=None, response_body=None):
        class FixtureClock(dt.datetime):
            @classmethod
            def now(cls,tz=None):return fixtures.NOW
        def fetch(path, body=None):
            if path == '/bybit/tickers':
                return fixtures.partial_fetch({})(path, body)
            self.assertEqual(body, {'symbols': ['BTCUSDT', 'FLUIDUSDT'], 'end': fixtures.END})
            if error is not None:
                raise error
            response = copy.deepcopy(CONTRACT[mode] if response_body is None else response_body)
            if old_worker:
                response['result'].pop('failure_diagnostics', None)
            return response
        log = io.StringIO()
        # The retained PR88 collector calls today's shared signal producer. Its
        # new provenance timestamp must use this fixture's clock, not wall time;
        # keep the exact full-snapshot compatibility comparison below intact.
        with patch.object(signal_capture.dt,'datetime',FixtureClock), tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(log), patch.object(
                module, 'DEFAULT', pathlib.Path(directory) / 'snapshot.json'):
            module.collect({'BTC'}, fixtures.NOW, fetch, lambda: (['BTC', 'FLUID'], {'cache_hit': True}))
            snapshot = json.loads(module.DEFAULT.read_text())
        legacy.validate(snapshot, fixtures.NOW)
        current.validate(snapshot, fixtures.NOW)
        for values in snapshot.get('signal_failure_details', {}).values():
            for detail in values.values():
                self.assertEqual(set(detail), FIELDS)
                self.assertTrue(legacy.SAFE_WORKER_ERROR.fullmatch(detail['code']) or detail['code'] in legacy.LOCAL_ERRORS)
        self.assertNotIn('http_evidence', json.dumps(snapshot))
        return snapshot, log.getvalue()

    def test_reference_is_exact_reviewed_git_blob(self):
        self.assertEqual(hashlib.sha1(b'blob ' + str(len(REFERENCE)).encode() + b'\0' + REFERENCE).hexdigest(), LEGACY_BLOB)
        self.assertEqual(current.LOCAL_ERRORS, legacy.LOCAL_ERRORS)

    def test_success_is_identical_for_both_collectors(self):
        expected, _ = self.collect(legacy, 'success')
        actual, log = self.collect(current, 'success')
        self.assertEqual(actual, expected)
        self.assertEqual(log, '')

    def test_old_worker_new_python_partial_matches_old_python(self):
        expected, _ = self.collect(legacy, old_worker=True)
        actual, log = self.collect(current, old_worker=True)
        self.assertEqual(actual, expected)
        self.assertEqual(log, '')

    def test_real_new_worker_partial_is_accepted_by_old_python(self):
        expected, _ = self.collect(legacy)
        actual, log = self.collect(current)
        self.assertEqual(actual, expected)
        self.assertFalse(actual['venue_status']['signal_complete'])
        self.assertNotIn('FLUID', actual['early_signals'])
        self.assertEqual(log.count('HUNTER_BYBIT_HTTP_EVIDENCE '), 1)

    def test_real_new_worker_entire_failed_batch_matches_old_python(self):
        expected, _ = self.collect(legacy, 'all_failed')
        actual, log = self.collect(current, 'all_failed')
        self.assertEqual(actual, expected)
        self.assertEqual(actual['early_signals'], {})
        self.assertEqual(log.count('HUNTER_BYBIT_HTTP_EVIDENCE '), 4)

    def compare_exception_pair(self, mode, code, kind):
        pair = CONTRACT['body_failure_pairs'][mode]
        expected, _ = self.collect(legacy, response_body=pair['legacy'])
        # Both supported consumers of the real new Worker response must agree.
        for module in (legacy, current):
            actual, log = self.collect(module, response_body=pair['current'])
            self.assertEqual(actual, expected)
            self.assertEqual(actual['signal_failure_details']['FLUIDUSDT']['15']['code'], code)
            self.assertEqual(actual['signal_failures']['FLUIDUSDT'],
                             'ValueError:BYBIT_WORKER_KLINE_FAILURE:FLUIDUSDT:15:' + code)
            self.assertFalse(actual['venue_status']['signal_complete'])
            self.assertNotIn('FLUID', actual['early_signals'])
            if module is current:
                record = json.loads(log.split(' ', 1)[1])
                self.assertEqual(record['http_evidence']['failure_kind'], kind)
                self.assertEqual(record['http_evidence']['http_status'], 0 if mode == 'fetch_abort' else 200)

    def test_body_and_fetch_abort_preserve_legacy_canonical_code_and_error_string(self):
        for mode in ('body_abort', 'fetch_abort'):
            with self.subTest(mode=mode):
                self.compare_exception_pair(mode, 'WORKER_ABORTED', 'timeout')

    def test_body_transport_preserves_legacy_canonical_code_and_error_string(self):
        self.compare_exception_pair('body_transport', 'WORKER_KLINE_FAILURE', 'transport')

    def test_body_non_json_preserves_legacy_canonical_code_and_error_string(self):
        self.compare_exception_pair('body_non_json', 'WORKER_KLINE_FAILURE', 'invalid_json')

    def test_new_outer_error_categories_use_legacy_snapshot_codes(self):
        for code, kind, status, canonical in [
                ('WORKER_TRANSPORT_ERROR', 'transport', 0, 'WORKER_KLINE_FAILURE'),
                ('BYBIT_WORKER_NON_JSON', 'invalid_json', 200, 'WORKER_KLINE_FAILURE'),
                ('WORKER_TIMEOUT', 'timeout', 0, 'WORKER_TIMEOUT'),
                ('BYBIT_HTTP_502', 'http', 502, 'BYBIT_HTTP_502')]:
            with self.subTest(code=code):
                error = evidence.ObservedHTTPError(code, evidence.read_evidence(None, {}, status, 'worker_http', kind))
                snapshot, log = self.collect(current, error=error)
                self.assertEqual(snapshot['signal_failure_details']['FLUIDUSDT']['15']['code'], canonical)
                self.assertEqual(json.loads(log.splitlines()[0].split(' ', 1)[1])['http_evidence']['failure_kind'], kind)

    def test_both_validators_reject_diagnostic_field_in_authoritative_snapshot(self):
        snapshot, _ = self.collect(current)
        snapshot['signal_failure_details']['FLUIDUSDT']['15']['http_evidence'] = {}
        snapshot['snapshot_sha256'] = current.digest({k: v for k, v in snapshot.items() if k != 'snapshot_sha256'})
        for module in (legacy, current):
            with self.assertRaisesRegex(ValueError, 'FAILURE_DETAIL_INVALID'):
                module.validate(snapshot, fixtures.NOW)

    def test_logs_are_bounded_redacted_and_generation_bound(self):
        snapshot, log = self.collect(current, 'all_failed')
        for forbidden in ('SECRET_DO_NOT_STORE', 'https://', 'authorization', 'cookie', 'retMsg'):
            self.assertNotIn(forbidden, log)
        for line in log.splitlines():
            self.assertLess(len(line), 2400)
            record = json.loads(line.split(' ', 1)[1])
            self.assertEqual(record['captured_at_utc'], snapshot['captured_at_utc'])
            self.assertEqual(record['generation_end_ms'], fixtures.END)
            self.assertIn(record['symbol'], ('BTCUSDT', 'FLUIDUSDT'))
            self.assertIn(record['interval'], ('15', '60'))
            evidence.validate_evidence(record['http_evidence'])


if __name__ == '__main__':
    unittest.main()
