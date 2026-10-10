"""Offline synthetic receipts only; no market/network/order calls."""
import copy
import datetime as dt
import unittest
from unittest.mock import patch
from research import hunter_manual_stop_once as manual

NOW = dt.datetime(2026, 10, 10, 22, 10, tzinfo=dt.timezone.utc)
SHA = 'e6e0e8b1963d6a80bfd935174418925af30a4987'


def fixture():
    positions = []
    books = {}
    for asset, (sid, entry, slip) in manual.TARGETS.items():
        positions.append(dict(asset=asset, shadow_id=sid,
                              opened_at_utc='2026-10-05T07:51:47.857517+00:00',
                              execution_venue='BINANCE_SPOT', execution_channel={'channel': 'BYBIT_SPOT'},
                              market_symbol=asset+'USDT', market_type='spot', execution_fee_bps=10,
                              capital_authority='NONE_SHADOW_ONLY', mfe_pct=3, mae_pct=-20,
                              tranches=[dict(price=entry, notional_usdt=1000, buy_slippage_bps=slip)]))
        price = entry * .85
        books[asset] = dict(exchange='binance', market='spot', symbol=asset+'USDT',
                            price_unit='USDT', quantity_unit='BASE', fetched_at=NOW.isoformat(),
                            bids=[[price, 100], [price*.99, 10000]], asks=[[price*1.001, 10000]])
    positions.append(dict(asset='UNRELATED', shadow_id='SHV2-unrelated', sentinel='unchanged'))
    state = dict(schema='hunter_shadow_v2_portfolio_v2', mode='SIMULATION_ONLY_NO_REAL_ORDERS',
                 open_positions=positions, closed_positions=[{'shadow_id': 'old', 'net_pnl_usdt': 1}],
                 events=[{'type': 'OLD', 'sentinel': 'unchanged'}], decisions=[],
                 last_cycle_generation_id='OLD_MONITOR', active_observation_generation_id='OLD_MONITOR')
    return state, books


class ManualStopTests(unittest.TestCase):
    def run_stage(self, state, books, now=NOW):
        return manual.stage(state, manual.request(), books, now, SHA)

    def test_full_depth_cost_and_side_effects(self):
        state, books = fixture()
        before = copy.deepcopy(state)
        result, audit = self.run_stage(state, books)
        self.assertEqual(state, before)
        self.assertEqual(audit['staged_count'], 2)
        self.assertEqual(audit['status'], 'NOT_PUBLISHED')
        self.assertEqual(result['open_positions'], [before['open_positions'][-1]])
        self.assertEqual(result['closed_positions'][0], before['closed_positions'][0])
        self.assertEqual(result['events'][0], before['events'][0])
        self.assertEqual(result['last_cycle_generation_id'], 'OLD_MONITOR')
        self.assertEqual(result['active_observation_generation_id'], 'OLD_MONITOR')
        self.assertEqual(result['loss_exit_guard']['loss_exit_count'], 2)
        self.assertEqual(result['loss_exit_guard']['hard_invalidation_loss_exits'], 0)
        self.assertEqual(result['circuit_breaker']['quarantined_cash_usdt'], 2000)
        for pos, event in zip(result['closed_positions'][1:], result['events'][1:]):
            asset = pos['asset']
            entry, slip = manual.TARGETS[asset][1:]
            quantity = 1000 / (entry*(1+(slip+10)/10000))
            best, second = books[asset]['bids']
            proceeds = 100*best[0]+(quantity-100)*second[0]
            self.assertAlmostEqual(pos['exit_reference_price'], proceeds/quantity)
            self.assertLess(pos['exit_reference_price'], best[0])
            self.assertAlmostEqual(pos['exit_execution_estimate']['net_pnl_usdt'], proceeds*.999-1000)
            self.assertEqual(event['reason'], manual.REASON)
            self.assertEqual(event['request_id'], manual.REQUEST_ID)
            self.assertEqual(event['requested_at_utc'], manual.REQUESTED_AT)
            self.assertEqual(event['manual_event_id'], manual.event_id(asset))
            self.assertEqual(event['execution_venue'], 'BINANCE_SPOT')
            self.assertEqual(pos['post_exit_evaluation']['status'], 'OBSERVING')
            self.assertTrue(result['reentry_registry'][asset]['risk_lock'])

    def test_cross_cycle_retry_and_archive(self):
        state, books = fixture()
        done, _ = self.run_stage(state, books)
        for row in done['closed_positions'][1:]:row['observation_complete'] = True
        with patch.object(manual.eng, 'MAX_CLOSED_HOT', 1):
            manual.eng.compact_closed_history(done)
        self.assertEqual(len(done['closed_trade_archive']), 2)
        self.assertNotIn('manual_request_id', done['closed_trade_archive'][0])
        done['events'] = []  # bounded event history may roll over
        done['last_cycle_generation_id'] = 'LATER_MONITOR'
        again, audit = self.run_stage(done, {}, NOW+dt.timedelta(days=2))
        self.assertEqual(done, again)
        self.assertEqual(audit['staged_count'], 0)
        self.assertEqual([r['status'] for r in audit['targets']], ['ALREADY_APPLIED']*2)
        # The real archive is bounded to 5000; permanent receipts outlive it.
        done['closed_trade_archive'] = []
        again, audit = self.run_stage(done, {}, NOW+dt.timedelta(days=100))
        self.assertEqual(done, again)
        self.assertEqual(audit['staged_count'], 0)
        done['manual_exit_requests'][manual.REQUEST_ID]['receipts']['ENA']['execution']['net_pnl_usdt'] = 123
        with self.assertRaisesRegex(ValueError, 'IDEMPOTENCY_RECEIPT_COST_INVALID'):
            self.run_stage(done, {}, NOW+dt.timedelta(days=100))

    def test_partial_retry_only_remaining(self):
        state, books = fixture()
        partial, audit = self.run_stage(state, {'ENA': books['ENA']})
        self.assertEqual(audit['staged_count'], 1)
        done, audit = self.run_stage(partial, books)
        self.assertEqual(audit['staged_count'], 1)
        sells = [e for e in done['events'] if e['type'] == 'SHADOW_V2_SELL']
        self.assertEqual(len(sells), 2)
        self.assertEqual(len({e['manual_event_id'] for e in sells}), 2)

    def test_stale_future_missing_depth_crossed_and_wrong_venue(self):
        for mutation in (
            {'fetched_at': (NOW-dt.timedelta(seconds=31)).isoformat()},
            {'fetched_at': (NOW+dt.timedelta(seconds=1)).isoformat()},
            {'bids': [[.1, 1]]}, {'asks': []}, {'exchange': 'bybit'},
            {'symbol': 'WRONGUSDT'}, {'bids': [[100, 100000]]},
        ):
            with self.subTest(mutation=mutation):
                state, books = fixture()
                books['ENA'].update(mutation)
                books.pop('PENDLE')
                result, audit = self.run_stage(state, books)
                self.assertEqual(result, state)
                self.assertEqual(audit['staged_count'], 0)

    def test_scope_fees_quantity_and_lane_rejected_without_mutation(self):
        for field, value in [('execution_fee_bps', 20), ('execution_venue', 'BYBIT_SPOT'),
                             ('tranches', []), ('capital_authority', 'REAL')]:
            state, books = fixture()
            state['open_positions'][0][field] = value
            before = copy.deepcopy(state)
            with self.assertRaises(ValueError):
                self.run_stage(state, books)
            self.assertEqual(state, before)
        state, books = fixture()
        req = manual.request()
        req['reason'] = 'HARD_INVALIDATION'
        with self.assertRaises(ValueError):
            manual.stage(state, req, books, NOW, SHA)

    def test_conflicting_receipt_fails_closed(self):
        state, books = fixture()
        done, _ = self.run_stage(state, books)
        done['manual_exit_requests'][manual.REQUEST_ID]['request_sha256'] = 'tampered'
        with self.assertRaisesRegex(ValueError, 'REQUEST_ID_COLLISION'):
            self.run_stage(done, books)

    def test_already_closed_elsewhere_never_resells(self):
        state, books = fixture()
        pos = state['open_positions'].pop(0)
        state['closed_positions'].append(pos)
        done, audit = self.run_stage(state, books)
        self.assertEqual(audit['targets'][0]['status'], 'NOT_OPEN_NO_ACTION')
        self.assertEqual([e['asset'] for e in done['events'][1:]], ['PENDLE'])

    def test_notification_identity_ack_replay_is_unchanged(self):
        from scripts import hunter_trade_notifications as notifications
        state, books = fixture()
        done, _ = self.run_stage(state, books)
        batch = notifications.make_batch(done, [], SHA)
        self.assertEqual(len(batch['events']), 2)
        for event in batch['events']:
            self.assertEqual(event['event_id'], notifications.event_id(event))
            self.assertEqual(event['manual_event_id'], manual.event_id(event['asset']))
        ack = notifications.acknowledge(batch, dict(role='assistant', message_id='synthetic', text=batch['batch_id']))
        self.assertEqual(notifications.make_batch(done, ack['event_ids'], SHA)['events'], [])
        retried, _ = self.run_stage(done, {}, NOW + dt.timedelta(days=1))
        self.assertEqual(notifications.make_batch(retried, ack['event_ids'], SHA)['events'], [])


if __name__ == '__main__':
    unittest.main()
