import copy
import unittest

from research.hunter_fast_watch import Watch, utc
from tests.test_hunter_fast_watch import position, portfolio, event, book


class LiveWindowTests(unittest.TestCase):
    def setUp(self):
        self.p=position();self.p.update(last_price=1.04,last_marked_at_utc=utc(90),holding_peak_price=9)
        self.state=portfolio(self.p);self.state['active_observation_generation_id']='MONITOR_0'
        self.w=Watch();self.w.reconcile(self.state,'source-1',100)

    def reconcile(self, at=110, generation='MONITOR_1'):
        self.state['active_observation_generation_id']=generation
        self.w.reconcile(self.state,'source-2',at)
        return self.w.ab['p']

    def test_prior_historical_peak_is_only_baseline(self):
        a=self.w.ab['p'];self.assertEqual(a['baseline_holding_peak_price'],9)
        self.assertIsNone(a['monitor_sampled_peak'])

    def test_prior_arm_not_a_new_detection_sample(self):
        self.p['protection_lifecycle']=dict(state='ARMED',armed_at_utc=utc(90))
        self.assertIsNone(self.reconcile()['monitor_first_arm_seen_at'])

    def test_new_monitor_arm_counts_inside_window(self):
        self.p['protection_lifecycle']=dict(state='ARMED',armed_at_utc=utc(105))
        self.assertEqual(self.reconcile()['monitor_first_arm_seen_at'],utc(105))

    def test_research_arm_not_a_monitor_sample(self):
        self.p['protection_lifecycle']=dict(state='ARMED',armed_at_utc=utc(105))
        self.assertIsNone(self.reconcile(generation='RESEARCH_1')['monitor_first_arm_seen_at'])

    def test_new_monitor_peak_uses_actual_last_price(self):
        self.p['last_marked_at_utc']=utc(105)
        self.assertEqual(self.reconcile()['monitor_sampled_peak'],1.04)
        self.p.update(last_price=1.03,last_marked_at_utc=utc(115))
        self.assertEqual(self.reconcile(120)['monitor_sampled_peak'],1.04)

    def test_future_monitor_peak_rejected(self):
        self.p['last_marked_at_utc']=utc(120)
        self.assertIsNone(self.reconcile()['monitor_sampled_peak'])

    def test_research_peak_not_a_monitor_peak(self):
        self.p['last_marked_at_utc']=utc(105)
        self.assertIsNone(self.reconcile(generation='RESEARCH_1')['monitor_sampled_peak'])

    def test_existing_armed_copy_not_recounted_as_new_ws_arm(self):
        self.p['protection_lifecycle']=dict(state='ARMED',armed_at_utc=utc(90),capital_basis=1000,
            peak_net_pnl_usdt=38,protected_floor_usdt=18,transitions=[])
        self.w=Watch();self.w.reconcile(self.state,'source',100)
        self.w.event(event(),100);t=self.w.pending.pop('p');self.w.review('p',t,book(),100)
        self.assertIsNone(self.w.ab['p']['ws_first_arm_seen_at'])

    def test_cashflow_add_starts_new_ab_episode(self):
        self.w.ab['p']['ws_first_arm_seen_at']=utc(100)
        self.p['tranches'].append(dict(price=1,notional_usdt=1000))
        a=self.reconcile();self.assertEqual(a['measurement_started_at'],utc(110))
        self.assertIsNone(a['ws_first_arm_seen_at'])
        self.assertEqual(self.w.history[-1]['kind'],'AB_CASHFLOW_REBASE')

    def test_pre_window_close_not_a_new_exit_pair(self):
        closed=copy.deepcopy(self.p);closed.update(closed_at_utc=utc(90),net_pnl_usdt=10)
        self.state['closed_positions']=[closed]
        self.assertIsNone(self.reconcile()['monitor_exit_review_at'])

    def test_fresh_close_requires_matching_cashflow(self):
        closed=copy.deepcopy(self.p);closed.update(closed_at_utc=utc(105),net_pnl_usdt=10)
        self.state['closed_positions']=[closed]
        self.assertEqual(self.reconcile()['monitor_exit_review_at'],utc(105))
        self.w.ab['p']['monitor_exit_review_at']=None
        closed['tranches'].append(dict(price=1,notional_usdt=1000))
        self.assertIsNone(self.reconcile()['monitor_exit_review_at'])

    def test_baseline_without_pairs_has_unknown_improvement(self):
        m=self.w.snapshot(110)['metrics']
        self.assertIsNone(m['arm_detection_improvement_seconds'])
        self.assertIsNone(m['exit_review_improvement_seconds'])
