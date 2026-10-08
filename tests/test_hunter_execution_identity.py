import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research import hunter_position_monitor as monitor
from research import hunter_shadow_trader_v2 as engine
from research.hunter_fast_watch import DualWatch
from research.hunter_lifecycle_state import liquidation
import test_hunter_position_monitor as monitor_tests

NOW = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)
SHA = 'a' * 40


def receipt(price=90, **changes):
    return dict(exchange='binance', market='spot', symbol='XUSDT',
                fetched_at=NOW.isoformat(), price_unit='USDT', quantity_unit='BASE',
                bids=[[str(price), '10000']], asks=[[str(price+.01), '10000']], **changes)


class ExecutionIdentityTests(unittest.TestCase):
    def run_monitor(self, mutate=None):
        state, market, review, liq, supply = monitor_tests.PositionMonitorTests().fixture()
        state['open_positions'][0]['capital_authority'] = 'NONE_SHADOW_ONLY'
        state['open_positions'][0]['execution_channel'] = {'channel': 'BYBIT_SPOT', 'spot': True}
        liq['snapshots']['X']['raw_book_evidence'] = receipt()
        if mutate:
            mutate(state, liq)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'v2.json'
            engine.atomic_json_write(path, state)
            with patch.object(engine, '_execution_source_sha', return_value=SHA, create=True), \
                 patch.object(engine, 'backfill_opportunity_history', side_effect=lambda p,n:p):
                monitor.run_lane(path, 'SHADOW_V2', market, review, liq, supply, NOW, False)
                saved = monitor.load(path)
                before = copy.deepcopy(saved['open_positions'][0].get('execution_identity_proof'))
                monitor.run_lane(path, 'SHADOW_V2', market, review, liq, supply,
                                 NOW+dt.timedelta(minutes=5), False,
                                 regime_scan={'generation_id': 'next', 'as_of_utc': (NOW+dt.timedelta(minutes=5)).isoformat()})
                restarted = monitor.load(path)
        return saved, restarted, before

    def test_authoritative_monitor_persists_current_model_without_fake_history(self):
        saved, restarted, proof = self.run_monitor()
        position = saved['open_positions'][0]
        self.assertEqual(position.get('execution_venue'), 'BINANCE_SPOT')
        self.assertEqual(position['market_symbol'], 'XUSDT')
        self.assertEqual(position['market_type'], 'spot')
        self.assertEqual(position['historical_entry_execution_venue'], 'UNKNOWN')
        self.assertTrue(proof['formal_portfolio_mutated'])
        self.assertEqual(proof['generation_id'], saved['active_observation_generation_id'])
        self.assertFalse(proof['historical_execution_verified'])
        self.assertEqual(restarted['open_positions'][0]['execution_identity_proof'], proof)
        self.assertEqual(saved['events'], [])
        self.assertEqual(saved['closed_positions'], [])
        self.assertEqual(len(position['tranches']), 1)
        dual = DualWatch()
        dual.reconcile(restarted, SHA, NOW.timestamp()+301)
        self.assertEqual(dual.unroutable, [])
        self.assertEqual(set(dual.watches['BINANCE_SPOT'].symbols), {'BTCUSDT', 'XUSDT'})
        self.assertEqual(dual.watches['BYBIT_SPOT'].symbols, {})

    def test_stale_receipt_does_not_fill_venue_from_availability(self):
        saved, _, _ = self.run_monitor(lambda s,l:l['snapshots']['X']['raw_book_evidence'].update(
            fetched_at=(NOW-dt.timedelta(seconds=601)).isoformat()))
        self.assertNotIn('execution_venue', saved['open_positions'][0])

    def test_full_quantity_unknown_stays_unknown(self):
        saved, _, _ = self.run_monitor(lambda s,l:l['snapshots']['X']['raw_book_evidence'].update(bids=[['90','0.001']]))
        self.assertNotIn('execution_venue', saved['open_positions'][0])

    def test_v1_never_receives_v2_identity_metadata(self):
        state, market, review, liq, supply = monitor_tests.PositionMonitorTests().fixture()
        liq['snapshots']['X']['raw_book_evidence'] = receipt()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'v1.json'
            engine.atomic_json_write(path, state)
            with patch.object(engine, 'backfill_opportunity_history', side_effect=lambda p,n:p):
                monitor.run_lane(path, 'SHADOW_V1', market, review, liq, supply, NOW, True)
            monitor.configure_lane(False)
            self.assertNotIn('execution_venue', monitor.load(path)['open_positions'][0])

    def test_explicit_bybit_position_rejects_binance_liquidation(self):
        state, *_ = monitor_tests.PositionMonitorTests().fixture()
        position = state['open_positions'][0]
        position.update(execution_venue='BYBIT_SPOT', market_symbol='XUSDT', market_type='spot')
        self.assertEqual(liquidation(position, receipt(), NOW)['status'], 'UNKNOWN')

    def test_partial_identity_fails_closed(self):
        state, *_ = monitor_tests.PositionMonitorTests().fixture()
        position = state['open_positions'][0]
        position['execution_venue'] = 'BINANCE_SPOT'
        self.assertEqual(liquidation(position, receipt(), NOW)['status'], 'UNKNOWN')

    def test_new_buy_records_actual_shadow_model_metadata_on_event(self):
        state = dict(open_positions=[], closed_positions=[], events=[], decisions=[])
        position = dict(shadow_id='new', asset='X', tranches=[], capital_authority='NONE_SHADOW_ONLY')
        proposal = dict(kind='BUY', pos=position, asset='X', amount=1000, price=90,
                        evidence={'buy_slippage_bps':0}, reasons=[], entry_book=receipt())
        with patch.object(engine.tail, 'risk_blocks_new', return_value=False), \
             patch.object(engine, 'marginal_capital_gate', return_value=(True, [])), \
             patch.object(engine, '_execution_source_sha', return_value=SHA, create=True):
            count = engine.execute_capital_proposals(state, [proposal], {'generation_id':'new-generation'}, NOW)
        self.assertEqual(count, 1)
        self.assertEqual(position.get('execution_venue'), 'BINANCE_SPOT')
        self.assertEqual(position.get('shadow_entry_execution_venue'), 'BINANCE_SPOT')
        self.assertEqual(state['events'][0].get('execution_venue'), 'BINANCE_SPOT')
        self.assertEqual(state['events'][0]['market_symbol'], 'XUSDT')
        self.assertEqual(state['events'][0]['notional_usdt'], 1000)

    def test_unmatched_primary_venue_cannot_fall_back_to_reference_profit_sell(self):
        state, market, review, liq, supply = monitor_tests.PositionMonitorTests().fixture(price=120)
        position = state['open_positions'][0]
        position.update(execution_venue='BYBIT_SPOT', market_symbol='XUSDT', market_type='spot')
        review['candidates'][0]['signal'].update(btc_relative_1h_pct=-3, btc_relative_4h_pct=-4,
                                               relative_acceleration_pct=-1)
        liq['snapshots']['X']['raw_book_evidence'] = receipt(120)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'v2.json'
            engine.atomic_json_write(path, state)
            with patch.object(engine, 'backfill_opportunity_history', side_effect=lambda p,n:p):
                monitor.run_lane(path, 'SHADOW_V2', market, review, liq, supply, NOW, False)
            saved = monitor.load(path)
        self.assertEqual(len(saved['open_positions']), 1)
        self.assertEqual(saved['closed_positions'], [])
        self.assertEqual(saved['events'], [])
        self.assertNotIn('last_health_generation_id', saved['open_positions'][0])
        self.assertTrue(any('PRIMARY_VENUE_MANAGEMENT_EVIDENCE_UNAVAILABLE' in d['reasons']
                            for d in saved['decisions']))

    def test_missing_entry_book_does_not_change_existing_buy_gate(self):
        state = dict(open_positions=[], closed_positions=[], events=[], decisions=[])
        position = dict(shadow_id='new', asset='X', tranches=[])
        proposal = dict(kind='BUY', pos=position, asset='X', amount=1000, price=90, evidence={}, reasons=[])
        with patch.object(engine.tail, 'risk_blocks_new', return_value=False), \
             patch.object(engine, 'marginal_capital_gate', return_value=(True, [])):
            count = engine.execute_capital_proposals(state, [proposal], {'generation_id':'g'}, NOW)
        self.assertEqual(count, 1)
        self.assertNotIn('execution_venue', position)


if __name__ == '__main__':
    unittest.main()
