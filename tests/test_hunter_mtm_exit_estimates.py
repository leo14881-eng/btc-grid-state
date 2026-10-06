import copy
import datetime as dt
import unittest

from research import hunter_lifecycle_state as h
from tests.test_hunter_lifecycle_state import NOW, position, book


class ExitMarkTests(unittest.TestCase):
    def setUp(self):
        self.p=position()
        self.p.update(last_price=90,last_marked_at_utc=NOW.isoformat())
        self.p['last_exit_estimate']=h.liquidation(self.p,book(91,NOW),NOW)
        self.s={'open_positions':[self.p],'closed_positions':[{'net_pnl_usdt':20}]}

    def metric(self):return h.mtm(self.s,NOW,None,10)

    def test_reference_below_bid_cannot_hide_known_net_liquidation(self):
        m=self.metric();ex=self.p['last_exit_estimate']
        self.assertEqual(m['mark_to_market_net_pnl_usdt'],round(20+ex['net_pnl_usdt'],2))
        self.assertLess(m['mark_to_market_net_pnl_usdt'],0)
        self.assertEqual(m['closed_win_rate'],1)

    def test_cost_uses_same_book_mid_not_ticker_movement(self):
        m=self.metric();self.assertGreater(m['estimated_exit_cost_usdt'],0)
        self.p['last_price']=100
        self.assertEqual(self.metric()['estimated_exit_cost_usdt'],m['estimated_exit_cost_usdt'])
        self.assertEqual(self.metric()['mark_to_market_net_pnl_usdt'],m['mark_to_market_net_pnl_usdt'])

    def test_legacy_receipt_can_have_known_mtm_and_unknown_cost(self):
        self.p['last_exit_estimate'].pop('book_mid')
        m=self.metric();self.assertIsInstance(m['mark_to_market_net_pnl_usdt'],float)
        self.assertEqual(m['estimated_exit_cost_usdt'],'UNKNOWN')
        self.assertEqual(m['estimated_exit_cost_unknown_assets'],['ENA'])

    def test_stale_exit_cannot_supply_mtm(self):
        self.p['last_exit_estimate']['fetched_at']=(NOW-dt.timedelta(minutes=11)).isoformat()
        self.assertEqual(self.metric()['mark_to_market_net_pnl_usdt'],'UNKNOWN')

    def test_unknown_receipt_status_rejected(self):
        self.p['last_exit_estimate']['status']='UNKNOWN'
        self.assertEqual(self.metric()['mark_to_market_net_pnl_usdt'],'UNKNOWN')

    def test_add_after_receipt_rejects_wrong_quantity(self):
        self.p['tranches'].append({'price':100,'notional_usdt':1000})
        self.assertEqual(self.metric()['mark_to_market_net_pnl_usdt'],'UNKNOWN')

    def test_changed_fee_model_rejected(self):
        self.p['last_exit_estimate']['fee_bps']=20
        self.assertEqual(self.metric()['mark_to_market_net_pnl_usdt'],'UNKNOWN')

    def test_tampered_positive_net_pnl_rejected(self):
        self.p['last_exit_estimate']['net_pnl_usdt']=100
        self.assertEqual(self.metric()['mark_to_market_net_pnl_usdt'],'UNKNOWN')

    def test_unknown_reference_mark_does_not_hide_fresh_exit(self):
        self.p['last_marked_at_utc']=(NOW-dt.timedelta(minutes=11)).isoformat()
        m=self.metric();self.assertEqual(m['open_unrealized_pnl_usdt'],'UNKNOWN')
        self.assertIsInstance(m['mark_to_market_net_pnl_usdt'],float)

    def test_non_finite_estimate_rejected(self):
        self.p['last_exit_estimate']['net_pnl_usdt']=float('nan')
        self.assertEqual(self.metric()['mark_to_market_net_pnl_usdt'],'UNKNOWN')

    def test_partial_portfolio_execution_unknown_cannot_be_zero(self):
        other=copy.deepcopy(self.p);other['asset']='MISSING';other.pop('last_exit_estimate')
        self.s['open_positions'].append(other)
        self.assertEqual(self.metric()['mark_to_market_net_pnl_usdt'],'UNKNOWN')
        self.assertEqual(self.metric()['exit_data_unknown_assets'],['MISSING'])

    def test_derived_fields_do_not_mutate_portfolio(self):
        before=copy.deepcopy(self.s);self.metric();self.assertEqual(self.s,before)

    def test_empty_open_portfolio_has_zero_exit_cost(self):
        self.s['open_positions']=[];m=self.metric()
        self.assertEqual(m['estimated_exit_cost_usdt'],0)
        self.assertEqual(m['mark_to_market_net_pnl_usdt'],20)
