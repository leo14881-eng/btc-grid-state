"""Circuit admission and restart tests through production shadow call paths."""
import contextlib
import copy
import datetime as dt
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from research import hunter_shadow_trader_v2 as eng
from research import hunter_shadow_trader as v1
from research import hunter_position_monitor as monitor
from research import hunter_tail_risk as risk
from research.hunter_policy import C, VERSION
from tests.test_hunter_tail_risk import evidence, NOW


class CircuitProductionPathTests(unittest.TestCase):
    def fixture(self, status):
        now = dt.datetime.now(dt.timezone.utc)
        old = (now - dt.timedelta(minutes=30)).isoformat()
        state = dict(schema='hunter_shadow_v2_portfolio_v2',
            mode='SIMULATION_ONLY_NO_REAL_ORDERS', closed_positions=[], events=[], decisions=[],
            open_positions=[dict(asset='X', shadow_id='fixture-X', opened_at_utc=old,
                btc_entry_price=100, mfe_pct=0, mae_pct=0, sample_cohort='NEW_VERSION_SAMPLE',
                data_provenance='LIVE_OBSERVATION', tranches=[dict(price=100, notional_usdt=1000,
                buy_slippage_bps=0, at=old, signal_evidence_id='old', signal_generation_id='old')])],
            systemic_risk=dict(level='NORMAL', raw_level='NORMAL', last_observation_id='baseline',
                risk_release_fraction=1.0, recovery_mode=False),
            circuit_breaker=dict(status=status, quarantined_cash_usdt=0 if status == 'NORMAL' else 1000,
                quarantine_base_usdt=1000, recovery_observations=0, consecutive_loss_exits=2))
        signal = dict(score=12, independent_signal_count=3, btc_relative_1h_pct=2,
                      btc_relative_4h_pct=3, relative_acceleration_pct=1, return_1h_pct=3)
        candidates = [dict(asset=a, first_discovery_price=100, trade_action='BUY',
            signal=copy.deepcopy(signal), signal_evidence=eng.stamp(a, 'current', now.isoformat()),
            execution_scenario=dict(buy_slippage_bps=10, estimated_rr=2), blockers=[])
            for a in ('X', 'Y')]
        market = {a: dict(reference_price=90, change_24h_pct=0) for a in ('X', 'Y')}
        market['BTC'] = dict(reference_price=100, change_24h_pct=1)
        scan = dict(coins=market, binance_complete=True, generation_id='current', as_of_utc=now.isoformat(),
                    venue_status={'binance': {'excluded_bstocks': ['MSFTB']}})
        review = dict(candidates=candidates, policy_version=VERSION, scan_generation_id='current')
        liq = {'snapshots': {a: dict(as_of_utc=now.isoformat(), spread_bps=10,
            bid_depth_2pct_usdt=50000, ask_depth_2pct_usdt=50000) for a in ('X', 'Y')}}
        supply = {'assets': {a: dict(tactical_supply_risk_verified=True, status='FULLY_UNLOCKED')
                            for a in ('X', 'Y')}}
        early = dict(policy_version=VERSION, scan_generation_id='current', as_of_utc=now.isoformat(),
            early=[dict(base='Y', **signal)], all_signals=[dict(base='X', **signal)])
        return now, state, scan, review, liq, supply, early

    @contextlib.contextmanager
    def isolate(self):
        names = ('STATE', 'SUMMARY', 'GUARD', 'CAPITAL_POOL_USDT', 'ENTRY_MODE',
                 'DISCOVERY_MIN_SCORE', 'DISCOVERY_MIN_INDEPENDENT', 'STRATEGY_ID',
                 'ID_PREFIX', 'EVENT_PREFIX', '_opportunity_backfill_requests', 'load')
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            for name in names: stack.enter_context(patch.object(eng, name, getattr(eng, name)))
            os.chdir(tmp)
            pathlib.Path('research/results').mkdir(parents=True)
            try: yield pathlib.Path('research/results')
            finally: os.chdir(previous)

    def used(self, state):
        return sum(t['notional_usdt'] for p in state['open_positions'] for t in p['tranches'])

    def full_lane(self, lane, status):
        now, state, scan, review, liq, supply, early = self.fixture(status)
        with self.isolate() as root:
            for name, doc in [('hunter-cex-universe-run.json', scan),
                ('hunter-tactical-capital-review.json', review), ('hunter-liquidity-probe.json', liq),
                ('hunter-tactical-supply-risk.json', supply), ('hunter-early-signals.json', early),
                ('hunter-shadow-portfolio.json', state), ('hunter-shadow-v2-portfolio.json', state),
                ('hunter-bybit-availability.json', {})]:
                eng.atomic_json_write(root / name, doc)
            monitor.configure_lane(lane == 'V1')
            # Only external market collection is stubbed. The risk gate, decision,
            # allocator, add, BUY loop, trade events and state writes remain real.
            ev = evidence('fresh-full-lane')
            ev['observed_at_utc'] = now.isoformat()
            with patch.object(eng, 'SHADOW_FREEZE', False), patch.object(risk, 'collect_systemic_evidence', return_value=ev), contextlib.redirect_stdout(io.StringIO()):
                (v1.main if lane == 'V1' else eng.main)()
            saved = eng.load(eng.STATE)
            return saved

    def test_tripped_full_v1_v2_buy_and_add_are_blocked(self):
        for lane in ('V1', 'V2'):
            with self.subTest(lane=lane):
                s = self.full_lane(lane, 'TRIPPED')
                self.assertEqual(s['systemic_risk']['level'], 'NORMAL')
                self.assertEqual([p['asset'] for p in s['open_positions']], ['X'])
                self.assertEqual(len(s['open_positions'][0]['tranches']), 1)
                self.assertEqual(s['events'], [])
                self.assertEqual(self.used(s), 1000)
                self.assertTrue(any('SYSTEMIC_RISK_ENTRY_FREEZE' in d.get('reasons', [])
                                    for d in s['decisions']))

    def test_recovering_full_v1_v2_buy_and_add_are_blocked(self):
        for lane in ('V1', 'V2'):
            with self.subTest(lane=lane):
                s = self.full_lane(lane, 'RECOVERING')
                self.assertEqual(s['systemic_risk']['level'], 'NORMAL')
                self.assertEqual([p['asset'] for p in s['open_positions']], ['X'])
                self.assertEqual(len(s['open_positions'][0]['tranches']), 1)
                self.assertEqual(s['events'], [])
                self.assertEqual(self.used(s), 1000)

    def test_normal_control_reaches_real_buy_and_add_in_both_lanes(self):
        for lane in ('V1', 'V2'):
            with self.subTest(lane=lane):
                s = self.full_lane(lane, 'NORMAL')
                self.assertEqual({p['asset'] for p in s['open_positions']}, {'X', 'Y'})
                self.assertEqual(self.used(s), 3000)
                self.assertEqual({e['type'].rsplit('_', 1)[-1] for e in s['events']}, {'BUY', 'ADD'})

    def test_real_monitor_add_gate_blocks_both_circuits_and_lanes(self):
        for status in ('TRIPPED', 'RECOVERING'):
            for lane in ('V1', 'V2'):
                with self.subTest(status=status, lane=lane), self.isolate() as root:
                    now, state, scan, review, liq, supply, _ = self.fixture(status)
                    p = root / 'temporary-ledger.json'; eng.atomic_json_write(p, state)
                    result = monitor.run_lane(p, lane, scan['coins'], review, liq, supply,
                                              now, lane == 'V1')
                    saved = eng.load(p)
                    self.assertEqual(result['added'], [])
                    self.assertEqual(result['deferred_adds'], [])
                    self.assertEqual(saved['events'], [])
                    self.assertEqual(self.used(saved), 1000)

    def test_allocator_final_gate_blocks_prepared_buy_and_add(self):
        for status in ('TRIPPED', 'RECOVERING'):
            for lane in ('V1', 'V2'):
                with self.subTest(status=status, lane=lane), self.isolate():
                    now, state, scan, review, liq, supply, _ = self.fixture(status)
                    monitor.configure_lane(lane == 'V1')
                    proposals = []
                    for c in review['candidates']:
                        kind = 'ADD' if c['asset'] == 'X' else 'BUY'
                        pos = state['open_positions'][0] if kind == 'ADD' else dict(asset='Y', tranches=[])
                        proposals.append(dict(kind=kind, asset=c['asset'], pos=pos, amount=1000,
                            price=90, evidence=eng.evidence(c, liq, supply), reasons=[], candidate=c))
                    self.assertEqual(eng.execute_capital_proposals(state, proposals, scan, now), 0)
                    self.assertEqual(state['events'], [])
                    self.assertEqual(self.used(state), 1000)

    def test_blocking_circuit_preserves_existing_fatal_exit_authority(self):
        for status in ('TRIPPED', 'RECOVERING'):
            for lane in ('V1', 'V2'):
                with self.subTest(status=status, lane=lane), self.isolate():
                    now, state, scan, review, liq, supply, _ = self.fixture(status)
                    review['candidates'][0]['blockers'] = ['CONTRACT_MISMATCH']
                    monitor.configure_lane(lane == 'V1')
                    eng.manage_existing_positions(state, scan, review, liq, supply, now)
                    self.assertEqual(state['open_positions'], [])
                    self.assertEqual(len(state['closed_positions']), 1)
                    self.assertEqual([e['type'].rsplit('_', 1)[-1] for e in state['events']], ['SELL'])
                    self.assertEqual(state['events'][0]['reason'], 'HARD_INVALIDATION')


class CircuitRestartTests(unittest.TestCase):
    def tripped(self):
        state = {'systemic_risk': {'level': 'NORMAL', 'raw_level': 'NORMAL',
            'last_observation_id': 'baseline', 'risk_release_fraction': 1.0, 'recovery_mode': False}}
        risk.record_loss_exit(state, -100, 'HARD_INVALIDATION', 1000, NOW, C)
        risk.record_loss_exit(state, -100, 'HARD_INVALIDATION', 1000, NOW, C)
        return state

    def test_independent_boundary_duplicate_and_restart(self):
        s = self.tripped(); normal = {'raw_level': 'NORMAL'}
        gap = float(C['SYSTEMIC_RECOVERY_MIN_GAP_SECONDS'])
        first = NOW + dt.timedelta(seconds=gap)
        risk.advance_circuit_breaker(s, normal, evidence('one'), first, C, True)
        self.assertEqual(s['circuit_breaker']['recovery_observations'], 1)
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / 'ledger.json'; eng.atomic_json_write(p, s)
            reloaded = eng.load(p)
            self.assertEqual(reloaded['circuit_breaker'], s['circuit_breaker'])
            self.assertEqual(reloaded['circuit_breaker']['status'], 'TRIPPED')
            risk.advance_circuit_breaker(reloaded, normal, evidence('one'), first + dt.timedelta(seconds=gap), C, True)
            self.assertEqual(reloaded['circuit_breaker']['recovery_observations'], 1)
            risk.advance_circuit_breaker(reloaded, normal, evidence('too-soon'), first + dt.timedelta(seconds=gap-1), C, True)
            self.assertEqual(reloaded['circuit_breaker']['recovery_observations'], 1)
            self.assertEqual(reloaded['circuit_breaker']['quarantined_cash_usdt'], 2000)
            risk.advance_circuit_breaker(reloaded, normal, evidence('boundary'), first + dt.timedelta(seconds=gap), C, True)
            self.assertEqual(reloaded['circuit_breaker']['recovery_observations'], 2)
            eng.atomic_json_write(p, reloaded)
            self.assertEqual(eng.load(p)['circuit_breaker'], reloaded['circuit_breaker'])

    def test_recovering_reload_cannot_release_same_observation_twice(self):
        s = self.tripped(); normal = {'raw_level': 'NORMAL'}
        gap = float(C['SYSTEMIC_RECOVERY_MIN_GAP_SECONDS'])
        wait = int(C['CIRCUIT_RECOVERY_OBSERVATIONS_BEFORE_RELEASE'])
        for n in range(1, wait+1):
            risk.advance_circuit_breaker(s, normal, evidence(str(n)), NOW + dt.timedelta(seconds=gap*n), C, True)
        self.assertEqual(s['circuit_breaker']['status'], 'RECOVERING')
        original = copy.deepcopy(s['circuit_breaker'])
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / 'ledger.json'; eng.atomic_json_write(p, s)
            reloaded = eng.load(p)
            risk.advance_circuit_breaker(reloaded, normal, evidence(str(wait)), NOW + dt.timedelta(seconds=gap*(wait+1)), C, True)
            self.assertEqual(reloaded['circuit_breaker'], original)
            self.assertTrue(risk.risk_blocks_new(reloaded))

    def test_profit_then_loss_does_not_downgrade_tripped_circuit(self):
        s = self.tripped()
        risk.record_loss_exit(s, 10, 'PROFIT_PROTECTION', 1000, NOW, C)
        self.assertEqual(s['circuit_breaker']['consecutive_loss_exits'], 0)
        self.assertEqual(s['circuit_breaker']['quarantined_cash_usdt'], 2000)
        risk.record_loss_exit(s, -1, 'HARD_INVALIDATION', 1000, NOW, C)
        self.assertEqual(s['circuit_breaker']['status'], 'TRIPPED')
        self.assertTrue(risk.risk_blocks_new(s))

    def test_legacy_count_without_timestamp_is_not_trusted(self):
        s = self.tripped(); s['circuit_breaker']['recovery_observations'] = 10
        risk.advance_circuit_breaker(s, {'raw_level': 'NORMAL'}, evidence('new'), NOW, C, True)
        self.assertEqual(s['circuit_breaker']['recovery_observations'], 1)
        self.assertEqual(s['circuit_breaker']['status'], 'TRIPPED')
        self.assertEqual(s['circuit_breaker']['quarantined_cash_usdt'], 2000)


if __name__ == '__main__':
    unittest.main()
