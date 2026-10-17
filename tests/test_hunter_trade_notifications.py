import copy
import unittest
from scripts.hunter_trade_notifications import make_batch, acknowledge


def fixture():
    event = dict(type='SHADOW_V2_SELL', at='2026-10-06T16:30:20+00:00',
                 asset='ARKM', shadow_id='shadow-1', tranches=3, price=0.1377,
                 notional_usdt=3000, reason='PROFIT_STAGNATION', net_pnl_usdt=7.16)
    return dict(mode='SIMULATION_ONLY_NO_REAL_ORDERS', events=[event])


class NotificationTests(unittest.TestCase):
    def test_eigen_profit_protection_is_selected_after_stalled_cursor(self):
        # Pinned ledger identity at main 44327057a565e86faa6eb62465504e34c6fc0b6d.
        # Offline fixture only: this neither emits a message nor persists an ACK.
        p = fixture()
        p['events'] = [dict(type='SHADOW_V2_SELL',
                           at='2026-10-12T00:57:10.860950+00:00', asset='EIGEN',
                           shadow_id='SHV2-20261011T222832-EIGEN-edad39', tranches=1,
                           price=0.27, notional_usdt=1000, net_pnl_usdt=1.89,
                           reason='PROFIT_PROTECTION')]
        b = make_batch(p, [], '44327057a565e86faa6eb62465504e34c6fc0b6d',
                       after='2026-10-06T18:26:30+00:00')
        self.assertEqual([e['event_id'] for e in b['events']],
                         ['a90afddfe2b32c9c01bc5497ca2d60928e0eb5b76a0539a809e8e5c78510c767'])
        self.assertEqual(b['delivery_status'], 'PREPARED_NOT_DELIVERED')

    def test_failed_receipt_preserves_identical_retry(self):
        p = fixture()
        acknowledged = []
        b = make_batch(p, acknowledged, 'a' * 40)
        before = copy.deepcopy((p, acknowledged, b))
        with self.assertRaisesRegex(ValueError, 'DELIVERY_RECEIPT_REQUIRED'):
            acknowledge(b, None)
        self.assertEqual((p, acknowledged, b), before)
        self.assertEqual(make_batch(p, acknowledged, 'a' * 40), b)

    def test_prepared_is_not_delivered_and_does_not_mutate(self):
        source = fixture(); before = copy.deepcopy(source)
        batch = make_batch(source, [], 'a' * 40)
        self.assertEqual(batch['delivery_status'], 'PREPARED_NOT_DELIVERED')
        self.assertEqual(source, before)
        self.assertEqual(batch['events'][0]['net_pnl_usdt'], 7.16)

    def test_idempotence_and_acknowledged_only(self):
        p = fixture(); p['events'] *= 2
        b = make_batch(p, [], 'a' * 40)
        self.assertEqual(len(b['events']), 1)
        self.assertEqual(make_batch(p, [], 'b' * 40)['batch_id'], b['batch_id'])
        self.assertEqual(make_batch(p, [b['events'][0]['event_id']], 'b' * 40)['events'], [])

    def test_changed_event_cannot_silently_deduplicate(self):
        p = fixture(); other = dict(p['events'][0], price=0.15); p['events'].append(other)
        with self.assertRaisesRegex(ValueError, 'CONTENT_CONFLICT'):
            make_batch(p, [], 'a' * 40)

    def test_missing_or_invalid_evidence_fails_closed(self):
        for key, value in [('mode', 'LIVE'), ('events', None)]:
            p = fixture(); p[key] = value
            with self.assertRaises(ValueError): make_batch(p, [], 'a' * 40)
        for key, value in [('price', float('nan')), ('notional_usdt', True),
                           ('net_pnl_usdt', None), ('shadow_id', '')]:
            p = fixture(); p['events'][0][key] = value
            with self.assertRaises(ValueError): make_batch(p, [], 'a' * 40)

    def test_only_actual_matching_message_is_receipt(self):
        b = make_batch(fixture(), [], 'a' * 40)
        for receipt in [None, {'role':'assistant','message_id':'m','text':'requested'},
                        {'role':'tool','message_id':'m','text':b['batch_id']}]:
            with self.assertRaises(ValueError): acknowledge(b, receipt)
        result = acknowledge(b, {'role':'assistant','message_id':'m','text':b['batch_id']})
        self.assertEqual(result['delivery_status'], 'CHATGPT_MESSAGE_OBSERVED')
        self.assertEqual(result['device_push_received'], 'UNVERIFIED')

    def test_buy_add_included_and_v1_filtered(self):
        p = fixture()
        p['events'] += [dict(p['events'][0], type=t) for t in
                        ['SHADOW_V2_BUY','SHADOW_V2_ADD','SHADOW_V1_BUY']]
        self.assertEqual(len(make_batch(p, [], 'a'*40)['events']), 3)

    def test_cutoff_uses_instant_not_timezone_string(self):
        p = fixture(); p['events'][0]['at'] = '2026-10-06T23:30:20+07:00'
        self.assertEqual(make_batch(p, [], 'a'*40, after='2026-10-06T16:31:00Z')['events'], [])
        self.assertEqual(len(make_batch(p, [], 'a'*40, after='2026-10-06T16:30:20Z')['events']), 1)


if __name__ == '__main__': unittest.main()
