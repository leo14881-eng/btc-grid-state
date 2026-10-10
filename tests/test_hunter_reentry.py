"""Synthetic trajectories, not historical profitability replays."""
import copy
import datetime as dt
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from research import hunter_shadow_trader_v2 as eng


class ReentryTests(unittest.TestCase):
    def setUp(self):
        lane = patch.object(eng, 'ENTRY_MODE', 'EXECUTABLE')
        lane.start()
        self.addCleanup(lane.stop)
        self.start = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
        self.state = {'open_positions': [], 'closed_positions': [], 'events': [], 'decisions': []}
        self.pos = {'asset': 'X', 'tranches': [{'price': 90, 'notional_usdt': 1000}],
                    'net_pnl_usdt': 100, 'protection_lifecycle': {'protected_floor_usdt': 90}}
        c, scan, _, _ = self.inputs(0, 100)
        eng.register_exit_for_reentry(self.state, self.pos, 100, 'PROFIT_PROTECTION', self.start, c, scan)

    def inputs(self, n, price, strong=True):
        at = (self.start + dt.timedelta(minutes=n)).isoformat()
        c = {'asset': 'X', 'first_discovery_price': 100, 'trade_action': 'BUY',
             'signal_evidence': eng.stamp('X', 'g'+str(n), at), 'blockers': [],
             'signal': {'score': 12+n/100, 'independent_signal_count': 3,
                        'btc_relative_1h_pct': 2 if strong else -2,
                        'btc_relative_4h_pct': 3 if strong else -3,
                        'relative_acceleration_pct': 1 if strong else -2},
             'execution_scenario': {'buy_slippage_bps': 10, 'estimated_rr': 2}}
        scan = {'generation_id': 'g'+str(n), 'as_of_utc': at,
                'coins': {'X': {'reference_price': price, 'change_24h_pct': 3}}}
        book = {'fetched_at': at, 'exchange': 'binance', 'market': 'spot', 'symbol': 'XUSDT',
                'price_unit': 'USDT', 'quantity_unit': 'BASE',
                'bids': [[price-.01, 10000]], 'asks': [[price+.01, 10000]]}
        liq = {'snapshots': {'X': {'as_of_utc': at, 'raw_book_evidence': book,
                                'spread_bps': 10, 'bid_depth_2pct_usdt': 50000,
                                'ask_depth_2pct_usdt': 50000}}}
        supply = {'assets': {'X': {'tactical_supply_risk_verified': True, 'status': 'FULLY_UNLOCKED'}}}
        return c, scan, liq, supply

    def run_observation(self, n, price, strong=True, inputs=None):
        c, scan, liq, supply = inputs or self.inputs(n, price, strong)
        return eng.reentry_allowed(self.state, c, price, scan, liq, supply,
                                   self.start + dt.timedelta(minutes=n))

    def test_continued_strength_without_reset_or_one_percent_breakout(self):
        self.assertFalse(self.run_observation(1, 100.1)[0])
        ok, reasons = self.run_observation(2, 100.2)
        self.assertTrue(ok)
        self.assertIn('CONTINUED_STRENGTH', reasons)
        self.assertFalse(self.state['reentry_registry']['X']['reset_seen'])

    def test_real_pullback_recovery_without_fixed_percentage(self):
        self.assertFalse(self.run_observation(1, 99.7, False)[0])
        ok, reasons = self.run_observation(2, 99.8)
        self.assertTrue(ok)
        self.assertIn('PULLBACK_RECOVERY', reasons)

    def test_false_breakout_then_fall(self):
        self.assertFalse(self.run_observation(1, 102)[0])
        self.assertFalse(self.run_observation(2, 101)[0])

    def test_protection_original_floor_and_fees_not_reset_by_buy(self):
        self.pos['protection_lifecycle']['protected_floor_usdt'] = 111
        c, scan, _, _ = self.inputs(0, 100)
        eng.register_exit_for_reentry(self.state, self.pos, 100, 'PROFIT_PROTECTION', self.start, c, scan)
        self.assertFalse(self.run_observation(1, 100.01)[0])
        ok, reasons = self.run_observation(2, 100.02)
        self.assertFalse(ok)
        self.assertIn('ORIGINAL_PROTECTION_STILL_BREACHED_AFTER_COSTS', reasons)

    def test_same_source_with_new_timestamps_is_not_new_evidence(self):
        base = self.inputs(0, 100)[0]['signal']
        for n in (1, 2):
            data = self.inputs(n, 101+n)
            data[0]['signal'] = base
            self.assertIn('SIGNAL_CONTENT_NOT_NEW', self.run_observation(n, 101+n, inputs=data)[1])

    def test_replay_after_restart_and_same_cycle_sell_buy_rejected(self):
        self.assertFalse(self.run_observation(0, 100)[0])
        self.run_observation(1, 100.1)
        self.state = json.loads(json.dumps(self.state))
        self.assertFalse(self.run_observation(1, 100.1)[0])
        self.assertTrue(self.run_observation(2, 100.2)[0])
        self.assertFalse(self.run_observation(2, 100.2)[0])

    def test_timestamp_only_market_update_rejected(self):
        self.run_observation(1, 100.1)
        self.assertIn('MARKET_CONTENT_NOT_NEW', self.run_observation(2, 100.1)[1])

    def test_missing_stale_future_and_mixed_generation(self):
        for kind in ('missing', 'stale', 'future', 'mixed', 'naive'):
            with self.subTest(kind=kind):
                data = self.inputs(2, 101)
                meta = data[0]['signal_evidence']
                if kind == 'missing':meta.pop('observed_at_utc')
                if kind == 'stale':meta['observed_at_utc'] = (self.start-dt.timedelta(hours=1)).isoformat()
                if kind == 'future':meta['observed_at_utc'] = (self.start+dt.timedelta(hours=1)).isoformat()
                if kind == 'mixed':meta['generation_id'] = 'other'
                if kind == 'naive':meta['observed_at_utc'] = '2026-10-10T12:01:00'
                self.assertFalse(self.run_observation(2, 101, inputs=data)[0])

    def test_unknown_legacy_exit_does_not_invent_history(self):
        self.state['reentry_registry']['X'].pop('reentry_context')
        self.assertIn('REENTRY_UNKNOWN', self.run_observation(1, 101)[1])
        self.assertEqual(self.state['closed_positions'], [])
        self.assertEqual(self.state['events'], [])

    def test_loss_hard_lock_requires_normal_new_market_and_cause_clearance(self):
        self.pos.update(net_pnl_usdt=-5, health_reasons=['CATASTROPHIC_DEPTH'])
        c, scan, _, _ = self.inputs(0, 100)
        eng.register_exit_for_reentry(self.state, self.pos, 100, 'HARD_INVALIDATION', self.start, c, scan)
        self.assertFalse(self.run_observation(1, 100.1)[0])
        self.state['systemic_risk'] = {'level': 'NORMAL', 'last_observation_id': 'new',
                                      'last_observed_at_utc': (self.start+dt.timedelta(minutes=2)).isoformat()}
        data = self.inputs(2, 100.2)
        data[2]['snapshots']['X']['bid_depth_2pct_usdt'] = 0
        self.assertFalse(self.run_observation(2, 100.2, inputs=data)[0])
        self.assertTrue(self.state['reentry_registry']['X']['risk_lock'])

    def test_unresolved_fatal_identity_and_manual_exit_remain_locked(self):
        for reason in ('HARD_INVALIDATION', 'USER_MANUAL_STOP'):
            self.pos.update(net_pnl_usdt=-5, health_reasons=['FATAL_IDENTITY_OR_CONTRACT:MISMATCH'])
            c, scan, _, _ = self.inputs(0, 100)
            eng.register_exit_for_reentry(self.state, self.pos, 100, reason, self.start, c, scan)
            self.state['systemic_risk'] = {'level': 'NORMAL', 'last_observation_id': 'new',
                                          'last_observed_at_utc': (self.start+dt.timedelta(minutes=1)).isoformat()}
            self.assertFalse(self.run_observation(1, 101)[0])
            self.assertFalse(self.run_observation(2, 102)[0])

    def test_full_buy_gates_and_early_anchor_still_apply(self):
        self.run_observation(1, 100.1)
        self.assertTrue(self.run_observation(2, 100.2)[0])
        for kind in ('identity', 'supply', 'liquidity', 'rr', 'anchor'):
            data = self.inputs(3, 101)
            c, scan, liq, supply = data
            if kind == 'identity':c['blockers'] = ['OFFICIAL_ASSET_IDENTITY_UNVERIFIED']
            if kind == 'supply':supply['assets']['X']['confirmed_major_near_term_unlock'] = True
            if kind == 'liquidity':liq['snapshots']['X']['spread_bps'] = 10000
            if kind == 'rr':c['execution_scenario']['estimated_rr'] = 0
            if kind == 'anchor':c['first_discovery_price'] = 1;c['execution_scenario']['estimated_rr'] = 1.7
            self.assertEqual(eng.decision(c, scan, liq, supply)[0], 'REJECT', kind)

    def test_no_capital_or_history_mutation_and_pool_unchanged(self):
        before = copy.deepcopy(self.state)
        self.run_observation(1, 100.1)
        self.run_observation(2, 100.2)
        for key in ('open_positions', 'closed_positions', 'events', 'decisions'):
            self.assertEqual(self.state[key], before[key])
        reserve = eng.reserve_snapshot(self.state)
        self.assertEqual([reserve[k] for k in ('capital_pool', 'ordinary_opportunity_cap', 'strategic_reserve')], [20000, 17000, 3000])
        self.assertFalse(eng.marginal_capital_gate(self.state, 21000, {}, 'BUY', {})[0])

    def test_same_book_new_price_id_and_timestamp_rejected(self):
        self.run_observation(1, 100.1)
        data = self.inputs(2, 100.1)
        data[1]['coins']['X']['reference_price'] = 101
        self.assertIn('MARKET_CONTENT_NOT_NEW', self.run_observation(2, 101, inputs=data)[1])

    def test_source_sequence_reversal_and_source_at_exit(self):
        c, scan, liq, _ = self.inputs(0, 100)
        book = liq['snapshots']['X']['raw_book_evidence']
        book['last_update_id'] = 100
        eng.register_exit_for_reentry(self.state, self.pos, 100, 'PROFIT_PROTECTION', self.start, c, scan, book)
        data = self.inputs(1, 101)
        data[2]['snapshots']['X']['raw_book_evidence']['last_update_id'] = 99
        self.assertIn('SOURCE_SEQUENCE_NOT_NEW', self.run_observation(1, 101, inputs=data)[1])

    def test_legacy_context_read_only_preserves_exited_protection(self):
        pos = copy.deepcopy(self.pos)
        pos.update(closed_at_utc=self.start.isoformat(), exit_reason='PROFIT_PROTECTION')
        pos['protection_lifecycle']['state'] = 'EXITED'
        self.state['closed_positions'] = [pos]
        self.state['reentry_registry']['X'].pop('reentry_context')
        before = copy.deepcopy(pos)
        self.assertFalse(self.run_observation(1, 100.1)[0])
        self.assertTrue(self.run_observation(2, 100.2)[0])
        self.assertEqual(pos, before)

    def test_cleared_liquidity_hard_exit_and_identity_clearance(self):
        for cause in ('CATASTROPHIC_DEPTH', 'FATAL_IDENTITY_OR_CONTRACT:MISMATCH'):
            self.state['systemic_risk'] = {'last_observation_id': 'exit'}
            self.pos.update(net_pnl_usdt=-5, health_reasons=[cause])
            c, scan, _, _ = self.inputs(0, 100)
            eng.register_exit_for_reentry(self.state, self.pos, 100, 'HARD_INVALIDATION', self.start, c, scan)
            for n in (1, 2):
                c, scan, liq, supply = self.inputs(n, 100+n/10)
                self.state['systemic_risk'] = {'level': 'NORMAL', 'last_observation_id': 'new'+str(n),
                                              'last_observed_at_utc': scan['as_of_utc']}
                identity = {'scan_generation_id': scan['generation_id'], 'as_of_utc': scan['as_of_utc'],
                            'assets': {'X': {'capital_identity_pass': True, 'blockers': []}}}
                ok, _ = eng.reentry_allowed(self.state, c, 100+n/10, scan, liq, supply,
                                            self.start+dt.timedelta(minutes=n), identity)
                self.assertEqual(ok, n == 2, cause)

    def cycle(self, lane, n, p, mutate=None, ready=False):
        """Run the actual shared main/allocator with in-memory IO and no network."""
        now = self.start+dt.timedelta(minutes=n)
        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):return now
        c, scan, liq, supply = self.inputs(n, p)
        if mutate:mutate(c, scan, liq, supply)
        scan.update(binance_complete=True, venue_status={'binance': {'excluded_bstocks': ['FAKE_STOCK']}})
        scan['coins']['BTC'] = {'reference_price': 60000, 'change_24h_pct': 1}
        review = {'policy_version': eng.VERSION, 'scan_generation_id': scan['generation_id'], 'candidates': [c]}
        values = {eng.SCAN: scan, eng.REVIEW: review, eng.LIQ: liq, eng.SUPPLY: supply, eng.STATE: self.state}
        with ExitStack() as stack:
            for name, value in {'ENTRY_MODE': 'DISCOVERY' if lane == 'V1' else 'EXECUTABLE',
                                'CAPITAL_POOL_USDT': None if lane == 'V1' else 20000,
                                'DISCOVERY_MIN_SCORE': eng.LANES[lane]['discovery_min_score'],
                                'EVENT_PREFIX': 'SHADOW_'+lane, 'SHADOW_FREEZE': False}.items():
                stack.enter_context(patch.object(eng, name, value))
            stack.enter_context(patch.object(eng.dt, 'datetime', Clock))
            stack.enter_context(patch.object(eng, 'load', side_effect=lambda path, default=None: values.get(path, default or {})))
            stack.enter_context(patch.object(eng, 'atomic_json_write'))
            stack.enter_context(patch.object(eng, 'refresh_closed_observations'))
            stack.enter_context(patch.object(eng, 'build_summary', return_value={}))
            stack.enter_context(patch.object(eng, 'update_overfilter_guard', return_value={'status': 'NORMAL'}))
            stack.enter_context(patch.object(eng.tail, 'collect_systemic_evidence', return_value={}))
            stack.enter_context(patch.object(eng.tail, 'update_risk_controls'))
            stack.enter_context(patch.object(eng.tail, 'risk_blocks_new', return_value=False))
            stack.enter_context(patch.object(eng, '_execution_source_sha', return_value='a'*40))
            stack.enter_context(patch('builtins.print'))
            if ready:
                self.state['loss_control'] = {'policy': 'LOSS_AND_MARKET_V1'}
                stack.enter_context(patch.object(eng, 'loss_freeze', SimpleNamespace(POLICY='LOSS_AND_MARKET_V1'), create=True))
                stack.enter_context(patch.object(eng, 'risk_blocks_new', lambda *args: False, create=True))
            eng.main()

    def test_shared_main_lane_gate_differences_and_no_early_anchor_reset(self):
        initial = copy.deepcopy(self.state)
        def missing_rr(c, scan, liq, supply):
            c['trade_action'] = 'WAIT'
            c['execution_scenario']['estimated_rr'] = None
        for lane in ('V1', 'V2'):
            self.state = copy.deepcopy(initial)
            self.cycle(lane, 1, 100.1, missing_rr)
            self.cycle(lane, 2, 100.2, missing_rr)
            self.assertEqual(len(self.state['open_positions']), int(lane == 'V1'))
            if lane == 'V2':
                self.assertIn('REENTRY_BUY_REVIEW_FAILED', self.state['decisions'][-1]['reasons'])
        self.state = copy.deepcopy(initial)
        def chase(c, scan, liq, supply):
            c['first_discovery_price'] = 1
            c['execution_scenario']['estimated_rr'] = 1.7
        self.cycle('V2', 1, 100.1, chase)
        self.cycle('V2', 2, 100.2, chase)
        self.assertEqual(self.state['open_positions'], [])
        self.assertIn('TOO_FAR_ABOVE_DISCOVERY_FOR_REMAINING_RR', self.state['decisions'][-1]['reasons'])

    def test_new_buy_note_only_after_both_components_ready_and_never_add(self):
        initial = copy.deepcopy(self.state)
        for lane in ('V1', 'V2'):
            for ready in (False, True):
                self.state = copy.deepcopy(initial)
                self.cycle(lane, 1, 100.1, ready=ready)
                self.cycle(lane, 2, 100.2, ready=ready)
                pos = self.state['open_positions'][0]
                event = self.state['events'][-1]
                self.assertEqual('strategy_note' in pos, ready)
                self.assertEqual('strategy_note' in event, ready)
                if ready:
                    self.assertEqual(event['strategy_note'], '2026.0.10.11 新策略')
                    self.assertEqual(event['strategy_version'], eng.reentry.STRATEGY_VERSION)
                eng.trade_event(self.state, pos, 'ADD', self.start, 100)
                self.assertNotIn('strategy_note', self.state['events'][-1])

    def test_shared_main_restart_replay_and_sellbuy_mutual_exclusion(self):
        for lane in ('V1', 'V2'):
            self.state['open_positions'] = []
            self.state['events'] = []
            c, scan, _, _ = self.inputs(0, 100)
            eng.register_exit_for_reentry(self.state, self.pos, 100, 'PROFIT_PROTECTION', self.start, c, scan)
            self.cycle(lane, 0, 100)
            self.assertEqual(self.state['events'], [])
            self.cycle(lane, 1, 100.1)
            self.state = json.loads(json.dumps(self.state))
            self.cycle(lane, 1, 100.1)
            self.assertEqual(self.state['events'], [])
            self.cycle(lane, 2, 100.2)
            self.cycle(lane, 2, 100.2)
            buys = [e for e in self.state['events'] if e['type'].endswith('_BUY')]
            self.assertEqual(len(buys), 1)


if __name__ == '__main__':
    unittest.main()
