import copy
import datetime as dt
import unittest

from research.hunter_lifecycle_state import liquidation, mtm
from research.hunter_fast_watch import venue_liquidation


class BybitLifecycleCostTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc)
        self.position = dict(asset='X', execution_venue='BYBIT_SPOT',
            market_symbol='XUSDT', market_type='spot', execution_fee_bps=8,
            tranches=[dict(price=100, notional_usdt=1000, buy_slippage_bps=2)],
            last_price=105, last_marked_at_utc=self.now.isoformat())
        self.book = dict(exchange='bybit', market='spot', symbol='XUSDT',
            price_unit='USDT', quantity_unit='BASE', fetched_at=self.now.isoformat(),
            source_timestamp=int(self.now.timestamp()*1000),
            bids=[['105', '4'], ['104', '20']], asks=[['105.1', '20']])

    def test_fast_and_authoritative_cashflow_are_identical(self):
        expected = liquidation(self.position, self.book, self.now, 8)
        self.assertEqual(expected['status'], 'SHADOW_RECEIPT_ESTIMATE')
        self.assertEqual(venue_liquidation(self.position, self.book, self.now), expected)
        quantity = 1000/(100*1.001)
        proceeds = 4*105+(quantity-4)*104
        self.assertAlmostEqual(expected['net_pnl_usdt'], proceeds*.9992-1000)
        self.assertEqual(expected['book_mid'], 105.05)
        self.assertEqual(expected['venue'], 'BYBIT_SPOT')

    def test_missing_primary_fee_is_unknown(self):
        self.position.pop('execution_fee_bps')
        self.assertEqual(liquidation(self.position, self.book, self.now, 8)['status'], 'UNKNOWN')

    def test_primary_venue_and_symbol_must_match(self):
        for changes in ({'exchange': 'binance'}, {'symbol': 'YUSDT'}, {'market': 'linear'}):
            with self.subTest(changes=changes):
                book = dict(self.book, **changes)
                self.assertEqual(liquidation(self.position, book, self.now, 8)['status'], 'UNKNOWN')

    def test_source_time_cannot_be_missing_future_or_stale(self):
        for source in (None, int((self.now.timestamp()+1)*1000), int((self.now.timestamp()-601)*1000)):
            with self.subTest(source=source):
                self.assertEqual(liquidation(self.position, dict(self.book, source_timestamp=source), self.now, 8)['status'], 'UNKNOWN')

    def test_partial_depth_cannot_be_extrapolated(self):
        self.assertEqual(liquidation(self.position, dict(self.book, bids=[['105', '1']]), self.now, 8)['status'], 'UNKNOWN')

    def test_fee_mismatch_and_invalid_fee_fail_closed(self):
        for fee in (10, -1, float('nan'), 101):
            with self.subTest(fee=fee):
                self.assertEqual(liquidation(self.position, self.book, self.now, fee)['status'], 'UNKNOWN')

    def test_mtm_uses_position_fee_and_same_receipt_mid(self):
        self.position['last_exit_estimate'] = liquidation(self.position, self.book, self.now, 8)
        result = mtm(dict(open_positions=[self.position]), self.now, None, 10)
        self.assertEqual(result['exit_data_unknown_assets'], [])
        self.assertEqual(result['mark_to_market_net_pnl_usdt'], round(self.position['last_exit_estimate']['net_pnl_usdt'], 2))
        estimate = self.position['last_exit_estimate']
        expected = estimate['quantity']*(estimate['book_mid']-estimate['vwap']*.9992)
        self.assertEqual(result['estimated_exit_cost_usdt'], round(expected, 2))

    def test_mtm_rejects_other_venue_receipt_and_missing_fee(self):
        self.position['last_exit_estimate'] = liquidation(self.position, self.book, self.now, 8)
        self.position['last_exit_estimate']['venue'] = 'BINANCE_SPOT'
        self.assertEqual(mtm(dict(open_positions=[self.position]), self.now, None, 10)['mark_to_market_net_pnl_usdt'], 'UNKNOWN')
        self.position.pop('execution_fee_bps')
        self.assertEqual(mtm(dict(open_positions=[self.position]), self.now, None, 10)['open_unrealized_pnl_usdt'], 'UNKNOWN')

    def test_read_only_cost_review_does_not_change_ledger(self):
        before = copy.deepcopy(self.position)
        venue_liquidation(self.position, self.book, self.now)
        self.assertEqual(self.position, before)


if __name__ == '__main__':
    unittest.main()
