"""Synthetic trajectories, not historical profitability replays."""
import copy
import datetime as dt
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from research import hunter_shadow_trader_v2 as eng
from research import hunter_early_signals as signals
from urllib.parse import parse_qs, urlparse


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
        now = self.start + dt.timedelta(minutes=n)
        at = now.isoformat()
        class SourceClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):return now
        def source(url, timeout):
            query = parse_qs(urlparse(url).query)
            interval = query['interval'][0]
            period = 3600000 if interval == '1h' else 900000
            count = 5 if interval == '1h' else 25
            last_open = int(now.timestamp()*1000)//period*period
            btc = query['symbol'][0] == 'BTCUSDT'
            last_price = (100 if strong else 120) if btc else price
            rows = []
            for i in range(count):
                o = 100 if btc else ([90, 91, 92, 94, 100][i] if count == 5 else 99)
                c = last_price if i == count-1 else o
                start = last_open-(count-1-i)*period
                rows.append([start, str(o), str(max(o,c)+1), str(min(o,c)-1), str(c), '100',
                             start+period-1, str(10000+n if i == count-1 else 10000)])
            return rows[-int(query['limit'][0]):]
        with patch.object(signals, 'get', side_effect=source), patch.object(signals.dt, 'datetime', SourceClock):
            r1 = signals.rolling(['XUSDT','BTCUSDT'], '1h')
            r4 = signals.rolling(['XUSDT','BTCUSDT'], '4h')
            micro = signals.micro(['XUSDT'])
            signal = signals.score_row('XUSDT','X',r1,r4,r1['BTCUSDT'],r4['BTCUSDT'],micro)
        c = {'asset': 'X', 'first_discovery_price': 100, 'trade_action': 'BUY',
             'signal_evidence': eng.stamp('X', 'g'+str(n), at), 'blockers': [],
             'signal': signal,
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
            self.assertFalse(self.run_observation(n, 101+n, inputs=data)[0])

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

    def test_reused_evidence_id_and_nonadjacent_generation(self):
        self.run_observation(1, 100.1)
        old_id = self.inputs(1, 100.1)[0]['signal_evidence']['evidence_id']
        data = self.inputs(2, 100.2)
        data[0]['signal_evidence']['evidence_id'] = old_id
        self.assertIn('SAME_OR_REPLAYED_CYCLE', self.run_observation(2, 100.2, inputs=data)[1])
        self.assertTrue(self.run_observation(2, 100.2)[0])
        data = self.inputs(3, 100.3)
        data[0]['signal_evidence']['generation_id'] = 'g1'
        data[1]['generation_id'] = 'g1'
        self.assertIn('SAME_OR_REPLAYED_CYCLE', self.run_observation(3, 100.3, inputs=data)[1])

    def test_first_buy_annotation_and_real_order_boundary(self):
        self.state['reentry_registry'] = {}
        self.state.update(real_trading_enabled=False, real_order_count=0, capital_authority='NONE_SHADOW_ONLY')
        before = copy.deepcopy(self.state)
        self.cycle('V1', 1, 100.1, ready=True)
        self.assertNotIn('strategy_note', self.state['events'][0])
        for key in ('real_trading_enabled', 'real_order_count', 'capital_authority', 'closed_positions'):
            self.assertEqual(before[key], self.state[key])

    def test_lane_capital_difference_at_allocator(self):
        c, scan, liq, supply = self.inputs(2, 100.2)
        e = eng.evidence(c, liq, supply)
        state = {'open_positions': [{'tranches': [{'price': 100, 'notional_usdt': 20000}]}]}
        with patch.object(eng, 'CAPITAL_POOL_USDT', None):
            self.assertTrue(eng.marginal_capital_gate(state, 1000, e, 'BUY', scan)[0])
        with patch.object(eng, 'CAPITAL_POOL_USDT', 20000):
            self.assertFalse(eng.marginal_capital_gate(state, 1000, e, 'BUY', scan)[0])

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
        for cause in ('CATASTROPHIC_DEPTH',):
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
        state_path = eng.ROOT/('hunter-shadow-portfolio.json' if lane == 'V1' else 'hunter-shadow-v2-portfolio.json')
        peer_path = eng.ROOT/('hunter-shadow-v2-portfolio.json' if lane == 'V1' else 'hunter-shadow-portfolio.json')
        values = {eng.SCAN: scan, eng.REVIEW: review, eng.LIQ: liq, eng.SUPPLY: supply, state_path: self.state,
                  peer_path: getattr(self, 'release_peer', {}),
                  eng.ROOT/'hunter-strategy-release.json': getattr(self, 'release_manifest', {})}
        with ExitStack() as stack:
            stack.enter_context(patch.object(eng, 'STATE', state_path))
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
            risk_evidence = {}
            if ready is None:
                from tests.test_hunter_loss_freeze import market
                risk_evidence = market(now, oid='cycle'+str(n))
            else:
                stack.enter_context(patch.object(eng.tail, 'update_risk_controls'))
                stack.enter_context(patch.object(eng.tail, 'risk_blocks_new', return_value=False))
                stack.enter_context(patch.object(eng, 'update_risk_controls', create=True))
                stack.enter_context(patch.object(eng, 'risk_blocks_new', lambda *args: False, create=True))
                stack.enter_context(patch.object(eng, 'loss_quarantine', return_value=0, create=True))
                if not ready:stack.enter_context(patch.object(eng, 'loss_freeze', None, create=True))
            stack.enter_context(patch.object(eng.tail, 'collect_systemic_evidence', return_value=risk_evidence))
            stack.enter_context(patch.object(eng, '_execution_source_sha', return_value='a'*40))
            stack.enter_context(patch('builtins.print'))
            if ready:
                self.state['loss_control'] = {'policy': 'LOSS_AND_MARKET_V1'}
                stack.enter_context(patch.object(eng, 'loss_freeze', SimpleNamespace(POLICY='LOSS_AND_MARKET_V1'), create=True))
                stack.enter_context(patch.object(eng, 'risk_blocks_new', lambda *args: False, create=True))
            eng.main()

    @unittest.skipUnless(hasattr(eng, 'loss_freeze'), 'requires exact combined PR102 integration checkout')
    def test_combined_real_risk_activation_new_buy_note_both_lanes(self):
        from research import hunter_strategy_release as release
        from tests.test_hunter_loss_freeze import market
        initial = copy.deepcopy(self.state)
        for lane, first_entry in (('V1',False),('V2',False),('V1',True),('V2',True)):
            self.state = copy.deepcopy(initial)
            if first_entry:self.state['reentry_registry'] = {}
            self.state['mode'] = 'SIMULATION_ONLY_NO_REAL_ORDERS'
            self.release_peer = {'mode': 'SIMULATION_ONLY_NO_REAL_ORDERS'}
            eng.loss_freeze.update_risk_controls(self.state, market(self.start, oid='init'), self.start, eng.C)
            eng.loss_freeze.update_risk_controls(self.release_peer, market(self.start, oid='peer'), self.start, eng.C)
            self.release_manifest = {'schema':'hunter_common_release_v1','release_id':'synthetic-reviewed-release',
                'enabled':True,'strategy_version':eng.reentry.STRATEGY_VERSION,'components':release.component_receipt(eng),
                'activated_at_utc':self.start.isoformat(),'real_trading_enabled':False,'real_order_count':0,
                'capital_authority':'NONE_SHADOW_ONLY','lane_activation_ids':{
                    'V1':self.start.isoformat(),'V2':self.start.isoformat()}}
            self.cycle(lane, 1, 100.1, ready=None)
            self.cycle(lane, 2, 100.2, ready=None)
            self.assertEqual(self.state['loss_control']['policy'], 'LOSS_AND_MARKET_V1')
            self.assertEqual(self.state['events'][-1]['strategy_note'], '2026.0.10.11 新策略')
            self.assertFalse(eng.risk_blocks_new(self.state, self.start+dt.timedelta(minutes=2)))

    @unittest.skipUnless(hasattr(eng, 'loss_freeze'), 'requires exact combined PR102 integration checkout')
    def test_common_release_rejects_half_rollout_old_policy_and_unmigrated_peer(self):
        from research import hunter_strategy_release as release
        self.test_combined_real_risk_activation_new_buy_note_both_lanes()
        now = self.start+dt.timedelta(minutes=2)
        original = {'manifest':self.release_manifest,'lanes':{'V2':self.state,'V1':self.release_peer}}
        self.assertTrue(release.annotation(eng, self.state, original, now))
        for kind in ('no_manifest','disabled','bad_code','old_policy','peer_migrating','bare_policy','wrong_activation','same_lane_twice'):
            context = copy.deepcopy(original)
            state = context['lanes']['V2']
            if kind == 'no_manifest':context['manifest'] = {}
            if kind == 'disabled':context['manifest']['enabled'] = False
            if kind == 'bad_code':context['manifest']['components']['code_sha256']['loss'] = '0'*64
            if kind == 'old_policy':context['lanes']['V1']['loss_control']['policy'] = 'OLD'
            if kind == 'peer_migrating':context['lanes']['V1']['loss_control']['legacy_completion'] = {'status':'RECOVERING','quarantined_cash_usdt':500}
            if kind == 'bare_policy':context['lanes']['V1']['loss_control'] = {'policy':'LOSS_AND_MARKET_V1'}
            if kind == 'wrong_activation':context['lanes']['V1']['loss_control']['activated_at_utc'] = (now-dt.timedelta(days=1)).isoformat()
            if kind == 'same_lane_twice':context['lanes']['V1'] = state
            self.assertEqual(release.annotation(eng, state, context, now), {}, kind)

    def test_new_missing_observation_interrupts_setup_but_duplicate_does_not(self):
        self.assertFalse(self.run_observation(1,100.1)[0])
        data = self.inputs(2,100.2)
        data[0]['signal'].pop('source_provenance')
        self.assertFalse(self.run_observation(2,100.2,inputs=data)[0])
        self.assertNotIn('reentry_setup',self.state['reentry_registry']['X'])
        self.assertFalse(self.run_observation(3,100.3)[0])
        self.assertFalse(self.run_observation(3,100.3)[0])
        self.assertTrue(self.run_observation(4,100.4)[0])

    def test_missing_candidate_in_actual_new_cycle_interrupts_both_lanes(self):
        initial = copy.deepcopy(self.state)
        for lane in ('V1','V2'):
            self.state = copy.deepcopy(initial)
            self.cycle(lane,1,100.1)
            self.cycle(lane,2,100.2,lambda c,*args:c.clear())
            self.cycle(lane,3,100.3)
            self.assertEqual(self.state['events'],[])
            self.cycle(lane,4,100.4)
            self.assertEqual(len(self.state['open_positions']),1)

    def test_day_gap_restarts_with_existing_freshness_contract(self):
        self.assertFalse(self.run_observation(1,100.1)[0])
        self.assertFalse(self.run_observation(1440,100.2)[0])
        self.assertTrue(self.run_observation(1441,100.3)[0])

    def test_late_packet_before_unknown_cannot_restart_confirmation(self):
        self.run_observation(1,100.1)
        data = self.inputs(3,100.3)
        data[2]['snapshots']['X'].pop('raw_book_evidence')
        self.assertFalse(self.run_observation(3,100.3,inputs=data)[0])
        self.assertFalse(self.run_observation(2,100.2)[0])
        self.assertFalse(self.run_observation(4,100.4)[0])
        self.assertTrue(self.run_observation(5,100.5)[0])

    def test_missing_generation_with_legacy_unknown_exit_generation_interrupts(self):
        self.state['reentry_registry']['X']['reentry_context']['generation'] = None
        self.run_observation(1,100.1)
        data = self.inputs(2,100.2)
        data[1].pop('generation_id')
        self.assertFalse(self.run_observation(2,100.2,inputs=data)[0])
        self.assertNotIn('reentry_setup',self.state['reentry_registry']['X'])
        self.assertFalse(self.run_observation(3,100.3)[0])

    def test_old_source_refetch_or_score_change_is_not_new_market_evidence(self):
        first = self.inputs(1,100.1)
        self.run_observation(1,100.1,inputs=first)
        data = self.inputs(2,100.2)
        data[0]['signal']['score'] += .01
        data[0]['signal']['source_provenance'] = copy.deepcopy(first[0]['signal']['source_provenance'])
        for p in data[0]['signal']['source_provenance'].values():p['observed_at_utc'] = data[1]['as_of_utc']
        self.assertIn('SOURCE_WINDOW_CONTENT_NOT_NEW',self.run_observation(2,100.2,inputs=data)[1])
        data = self.inputs(3,100.3)
        for p in data[0]['signal']['source_provenance'].values():p['observed_at_utc'] = (self.start-dt.timedelta(days=1)).isoformat()
        self.assertIn('REENTRY_UNKNOWN',self.run_observation(3,100.3,inputs=data)[1])

    def test_source_boundaries_confirmation_content_and_btc_identity_are_checked(self):
        for kind in ('old_window','confirmed','hash','btc_symbol','gap'):
            state = copy.deepcopy(self.state)
            data = self.inputs(1,100.1)
            p = data[0]['signal']['source_provenance']['btc_1h' if kind=='btc_symbol' else 'asset_1h']
            if kind == 'old_window':
                for b in p['bars']:
                    b['open_ms'] -= 86400000;b['close_ms'] -= 86400000;b['confirmed'] = True
                p['content_hash'] = eng.reentry.provenance.hash_value(p['bars'])
            if kind == 'confirmed':p['bars'][-1]['confirmed'] = True
            if kind == 'hash':p['bars'][-1]['ohlcv_quote'][3] += .001
            if kind == 'btc_symbol':p['symbol'] = 'XUSDT'
            if kind == 'gap':p['bars'][-1]['open_ms'] += 1
            self.assertIn('REENTRY_UNKNOWN',self.run_observation(1,100.1,inputs=data)[1],kind)
            self.state = state

    def test_new_window_with_same_values_advances_source_identity(self):
        first = self.inputs(59,100.1)
        self.assertFalse(self.run_observation(59,100.1,inputs=first)[0])
        data = self.inputs(60,100.1)
        for key,p in data[0]['signal']['source_provenance'].items():
            for old,b in zip(first[0]['signal']['source_provenance'][key]['bars'],p['bars']):
                b['ohlcv_quote'] = old['ohlcv_quote']
            p['content_hash'] = eng.reentry.provenance.hash_value(p['bars'])
        ok,reasons = self.run_observation(60,100.1,inputs=data)
        self.assertNotIn('SOURCE_WINDOW_CONTENT_NOT_NEW',reasons)
        self.assertNotIn('MARKET_CONTENT_NOT_NEW',reasons)
        self.assertEqual(self.state['reentry_registry']['X']['reentry_last_observation']['generation'],'g60')

    def test_contract_mismatch_requires_matching_contract_proof_not_ticker_pass(self):
        from research import hunter_identity_audit as identity
        official = {'platform':'ethereum','contract_address':'0xabc','exchange_pair':'XUSDT',
                    'official_contract_source':'https://official.example/contract',
                    'verified_at_utc':self.start.isoformat()}
        c,scan,_,_ = self.inputs(0,100)
        c['identity_audit'] = {'contract_evidence':{'contract_verified':True,'official':official}}
        self.pos.update(net_pnl_usdt=-5,health_reasons=['FATAL_IDENTITY_OR_CONTRACT:THIRD_PARTY_CONTRACT_MISMATCH'])
        eng.register_exit_for_reentry(self.state,self.pos,100,'HARD_INVALIDATION',self.start,c,scan)
        initial = copy.deepcopy(self.state)
        for strong_proof in (False,True):
            self.state = copy.deepcopy(initial)
            for n in (1,2):
                now = self.start+dt.timedelta(minutes=n)
                c,scan,liq,supply = self.inputs(n,100+n/10)
                scan.update(binance_complete=True)
                scan['coins']['X'].update(pairs=[{'pair':'XUSDT'}],venues=['binance'])
                market = {'coingecko':[{'symbol':'X','id':'x-token','platforms':{'ethereum':'0xabc'},
                    'contract_as_of_utc':now.isoformat(),'source_url':'https://independent.example/x'}]}
                registry = {'assets':{'X':{'contract_verified':strong_proof,'identity':official}}}
                audit = identity.build(scan,market,registry,now)
                self.assertTrue(audit['assets']['X']['capital_identity_pass'])
                self.state['systemic_risk'] = {'level':'NORMAL','last_observation_id':'post'+str(n),'last_observed_at_utc':now.isoformat()}
                ok,reasons = eng.reentry_allowed(self.state,c,100+n/10,scan,liq,supply,now,audit)
                self.assertEqual(ok,strong_proof and n==2)
                if not strong_proof:self.assertIn('ORIGINAL_IDENTITY_CAUSE_CLEARANCE_UNPROVEN',reasons)

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
                # Neither a callable nor a lane-local policy can attest release.
                self.assertNotIn('strategy_note', pos)
                self.assertNotIn('strategy_note', event)
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
