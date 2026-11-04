"""Formula-local copies with real SQLite fences; no network or load fixture.

The small coordinated fixture uses native locks on Linux and its existing
declared portable gate elsewhere. These tests do not run a Monitor lane, write
a portfolio, or claim throughput. Public delivery and use guards stay real.
"""
import datetime as dt
import json
from threading import Lock
import unittest
from unittest.mock import patch

from research import hunter_binance_stream_consumer as consumer
from research import hunter_early_signals as signals
from research import hunter_position_monitor as monitor
from research.hunter_binance_stream_store import topic
from tests import test_hunter_binance_coordinated_runtime as runtime


class LegacyCache:
    """Existing rows/packet interface, deliberately without a formula helper."""
    def __init__(self, frozen):
        self.frozen = frozen

    def rows(self, *args, **kwargs):
        return self.frozen.rows(*args, **kwargs)

    def packet(self, *args, **kwargs):
        return self.frozen.packet(*args, **kwargs)


class FormulaBoundaryTests(unittest.TestCase):
    def setUp(self):
        runtime.DataCases.setUp(self)
        self.market = runtime.Market(self.scratch/'state.sqlite3',
                                     stamp=runtime.MIDNIGHT+120000)
        self.stamp = self.market.seed()
        self.time = self.stamp
        self.store = self.market.store
        self.names = self.market.names
        self.cached = consumer.read(self.names,
            dt.datetime.fromtimestamp(self.stamp/1000, dt.timezone.utc),
            reader=self.store, clock=lambda: self.time)
        self.symbols = ['BTCUSDT', 'ETHUSDT']

    def gap(self, name, value=1):
        with self.store.db() as db:
            db.execute('UPDATE topics SET gap=? WHERE topic=?', (value, name))

    def outputs(self, cached):
        r1 = signals.rolling(self.symbols, '1h', cached)
        r4 = signals.rolling(self.symbols, '4h', cached)
        micro = signals.micro(self.symbols, cached)
        scores = {symbol: signals.score_row(symbol, symbol[:-4], r1, r4,
            r1['BTCUSDT'], r4['BTCUSDT'], micro) for symbol in self.symbols}
        return r1, r4, micro, scores

    def test_all_three_formula_outputs_match_original_public_interface(self):
        original = self.outputs(LegacyCache(self.cached))
        actual = self.outputs(self.cached)
        self.assertEqual(actual, original)
        # Returned decisions contain ordinary detached values. A Frozen/view,
        # generator, future or callable escaping through nested provenance is
        # not a serializable report, even when a superficial equality passes.
        encoded = json.dumps(actual, sort_keys=True, allow_nan=False)
        self.assertEqual(encoded, json.dumps(original, sort_keys=True, allow_nan=False))
        for result in actual[:3]:
            self.assertEqual(set(result), set(self.symbols))
            self.assertTrue(all(row['source_provenance'] for row in result.values()))

    def test_each_stage_fences_once_after_all_worker_copies_with_full_original_scope(self):
        copied = []
        lock = Lock()
        original_rows, original_packet = self.cached._rows, self.cached._packet
        original_finish = self.store.finish_read
        seen = []

        def record(label, function):
            def call(*args, **kwargs):
                value = function(*args, **kwargs)
                with lock:
                    copied.append(label)
                return value
            return call

        def finish(generation, names, checked_at, fence):
            with lock:
                seen.append((tuple(names), len(copied)))
            return original_finish(generation, names, checked_at, fence)

        with patch.object(self.cached, '_rows', side_effect=record('rows', original_rows)), \
                patch.object(self.cached, '_packet', side_effect=record('packet', original_packet)), \
                patch.object(self.store, 'finish_read', side_effect=finish):
            self.outputs(self.cached)
        self.assertEqual(seen, [(tuple(self.names), 4), (tuple(self.names), 8),
                                (tuple(self.names), 12)])
        self.assertEqual(copied.count('rows'), 6)
        self.assertEqual(copied.count('packet'), 6)

    def test_local_owned_copies_do_not_run_database_fence_until_formula_exit(self):
        expected_rows = self.cached.rows('BTCUSDT', '1h', 2)
        expected_packet = self.cached.packet('BTCUSDT', '1h', 2)
        with patch.object(self.store, 'finish_read', wraps=self.store.finish_read) as finish:
            with self.cached._formula() as view:
                rows = view.rows('BTCUSDT', '1h', 2)
                packet = view.packet('BTCUSDT', '1h', 2)
                self.assertEqual(rows, expected_rows)
                self.assertEqual(packet, expected_packet)
                rows[0][4] = '999'
                packet['transport'][0]['source'] = 'FORGED'
                self.assertEqual(view.rows('BTCUSDT', '1h', 2), expected_rows)
                self.assertEqual(view.packet('BTCUSDT', '1h', 2), expected_packet)
                finish.assert_not_called()
            finish.assert_called_once()
        self.assertEqual(self.cached.rows('BTCUSDT', '1h', 2), expected_rows)

    def test_expiry_during_copy_rejects_before_local_rows_return(self):
        original = self.cached._rows

        def delayed(*args, **kwargs):
            value = original(*args, **kwargs)
            self.time = self.stamp+90001
            return value

        with patch.object(self.cached, '_rows', side_effect=delayed):
            with self.assertRaisesRegex(consumer.CacheReadError, 'STALE|EXPIRED'):
                with self.cached._formula() as view:
                    view.rows('BTCUSDT', '1h')

    def test_packet_clock_rollback_is_sticky_after_caller_swallows_error(self):
        original = self.cached._packet

        def delayed(*args, **kwargs):
            value = original(*args, **kwargs)
            self.time = self.stamp-1
            return value

        with patch.object(self.cached, '_packet', side_effect=delayed):
            with self.assertRaisesRegex(consumer.CacheReadError, 'CLOCK_ROLLBACK'):
                with self.cached._formula() as view:
                    with self.assertRaisesRegex(consumer.CacheReadError, 'CLOCK_ROLLBACK'):
                        view.packet('BTCUSDT', '1h')
                    self.time = self.stamp
                    # Even an apparently fresh completion cannot revive this view.

    def test_swallowed_non_btc_worker_validation_error_cannot_return_partial_success(self):
        original = self.cached._validate_at
        failed = []
        lock = Lock()
        target = topic('ETHUSDT', '15m')

        def fail_once(now, names):
            with lock:
                reject = names == [target] and not failed
                if reject:
                    failed.append(True)
            if reject:
                raise ValueError('FORMULA_LOCAL_PROOF_FAILURE')
            return original(now, names)

        with patch.object(self.cached, '_validate_at', side_effect=fail_once):
            with self.assertRaisesRegex(consumer.CacheReadError, 'FORMULA_LOCAL_PROOF_FAILURE'):
                signals.micro(self.symbols, self.cached)
        self.assertEqual(failed, [True])

    def test_formula_return_rejects_revocation_of_original_scope_not_used_by_formula(self):
        target = topic('ETHUSDT', '1d')
        original = self.cached._packet
        changed = []
        lock = Lock()

        def revoke(*args, **kwargs):
            value = original(*args, **kwargs)
            with lock:
                change = not changed
                if change:
                    changed.append(True)
            if change:
                self.gap(target)
            return value

        with patch.object(self.cached, '_packet', side_effect=revoke):
            with self.assertRaisesRegex(consumer.CacheReadError, 'BINANCE_FINAL_TOPIC_REVOKED'):
                signals.micro(self.symbols, self.cached)
        self.assertEqual(changed, [True])

    def test_publication_aba_rejects_old_formula_while_new_read_is_ready(self):
        target = topic('ETHUSDT', '1d')
        with self.assertRaisesRegex(consumer.CacheReadError, 'CONTROL_READ_FENCE_CHANGED'):
            with self.cached._formula() as view:
                view.rows('BTCUSDT', '1h')
                self.market.apply('directory', [target], runtime.directory([target]), self.stamp+1)
                self.time = self.stamp+1
                fresh = self.market.read(self.names, self.time)
                self.assertEqual(fresh.generation['status'], 'READY')
                self.assertNotEqual(fresh.fence_sha256, self.cached.fence_sha256)

    def test_local_window_hash_failure_stays_rejected_after_window_restoration(self):
        name = topic('BTCUSDT', '1h')
        row = self.cached._generation['windows'][name]['rows'][0]
        original = row[4]
        with self.assertRaisesRegex(consumer.CacheReadError, 'VALIDITY_MISSING_EXPIRED_OR_MISMATCHED'):
            with self.cached._formula() as view:
                try:
                    row[4] = '102.5'
                    with self.assertRaisesRegex(consumer.CacheReadError, 'VALIDITY_MISSING_EXPIRED_OR_MISMATCHED'):
                        view.rows('BTCUSDT', '1h')
                finally:
                    row[4] = original

    def test_closed_view_cannot_be_reused_and_legacy_or_rest_has_no_new_interface_requirement(self):
        with self.cached._formula() as completed:
            completed.rows('BTCUSDT', '1h')
        error = RuntimeError('FORMULA_BODY_FAILED')
        with self.assertRaises(RuntimeError) as caught:
            with self.cached._formula() as aborted:
                raise error
        self.assertIs(caught.exception, error)
        for view in (completed, aborted):
            for method in (view.rows, view.packet):
                with self.assertRaisesRegex(ValueError, 'FORMULA_SCOPE_CLOSED'):
                    method('BTCUSDT', '1h')
        legacy = LegacyCache(self.cached)
        with signals._formula_cache(legacy) as value:
            self.assertIs(value, legacy)
        with signals._formula_cache(None) as value:
            self.assertIsNone(value)

    def test_final_fence_wait_rechecks_current_time_after_database_returns(self):
        original = self.store.finish_read

        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.time = self.stamp+90001
            return result

        with patch.object(self.store, 'finish_read', side_effect=delayed):
            with self.assertRaisesRegex(consumer.CacheReadError, 'STALE|EXPIRED'):
                with self.cached._formula() as view:
                    view.rows('BTCUSDT', '1h')

    def test_public_rows_and_packet_still_fence_during_active_private_formula(self):
        with self.assertRaisesRegex(consumer.CacheReadError, 'BINANCE_FINAL_TOPIC_REVOKED'):
            with self.cached._formula() as view:
                view.rows('BTCUSDT', '1h')
                self.gap(topic('ETHUSDT', '1d'))
                for method in (self.cached.rows, self.cached.packet):
                    with self.assertRaisesRegex(consumer.CacheReadError, 'BINANCE_FINAL_TOPIC_REVOKED'):
                        method('BTCUSDT', '1h')

    def test_formula_completion_does_not_replace_management_use_guards(self):
        self.outputs(self.cached)
        review = {'candidates': [{'asset': asset, 'signal_evidence': {'valid': True}}
                                 for asset in ('BTC', 'ETH')]}
        refresh = {'failures': {}}
        guard = monitor.management_signal_guard([(self.cached, ('BTC', 'ETH'))], review, refresh)
        with patch.object(self.cached, 'check_delivery', wraps=self.cached.check_delivery) as check:
            self.assertTrue(guard('BTC', 'decision'))
            self.gap(topic('ETHUSDT', '1d'))
            self.assertFalse(guard('BTC', 'add'))
            self.gap(topic('ETHUSDT', '1d'), 0)
            self.assertFalse(guard('ETH', 'exit'))
            self.assertTrue(guard('UNRELATED', 'decision'))
            self.assertEqual(check.call_count, 2)
        self.assertEqual(set(refresh['failures']), {'BTC', 'ETH'})
        self.assertTrue(all(row['signal_evidence'] == {} for row in review['candidates']))


if __name__ == '__main__':
    unittest.main()
