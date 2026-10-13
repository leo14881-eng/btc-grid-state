import copy
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'research'))
from hunter_v2_capital import assess_capital

NOW = '2026-10-06T15:00:00Z'


def fixture(used=16000, amount=1000, reserve=False):
    portfolio = {'generation_id': 'g1', 'cash_usdt': 10000,
                 'realized_net_pnl_usdt': 1000000,
                 'open_positions': [{'tranches': [{'notional': used}]}]}
    proposal = {'asset': 'ABC', 'action': 'BUY', 'notional_usdt': amount,
                'decision_timestamp': NOW, 'use_strategic_reserve': reserve}
    evidence = {name: {'status': 'PASS', 'source': 'audited fixture',
                       'source_timestamp': NOW, 'evidence_id': name + '-1'}
                for name in ['market', 'systemic', 'circuit_breaker', 'marginal_edge',
                             'identity', 'execution', 'liquidity', 'scenario',
                             'btc_relative', 'freshness', 'concentration', 'supply']}
    evidence['market']['regime'] = 'STRONG'
    evidence['systemic']['freeze'] = False
    evidence['circuit_breaker']['freeze'] = False
    evidence['execution'].update(full_size_valid=True, verified_notional_usdt=amount,
                                 asset='ABC', action='BUY')
    evidence['identity'].update(verified=True, mismatch=False)
    evidence['scenario']['rr'] = 3
    evidence['btc_relative'].update(return_4h_pct=1, acceleration=.2)
    evidence['supply']['material_risk'] = False
    evidence['tail_stress'] = {'calibration_cap_usdt': 4000}
    return portfolio, proposal, evidence


class CandidateCapitalTests(unittest.TestCase):
    def test_canonical_main_11_positions_17k_snapshot(self):
        # Pinned95b91627 canonical tranche shape; not a mutable live fixture.
        p,q,e = fixture()
        sizes = [2,1,2,1,1,1,2,1,1,3,2]
        p['open_positions'] = [{'tranches': [{'notional_usdt': 1000} for _ in range(n)]} for n in sizes]
        result = assess_capital(p,q,e)
        self.assertEqual(len(p['open_positions']), 11)
        self.assertEqual(result['used_usdt'], 17000)
        self.assertIn('ORDINARY_17K_CAP_REJECT', result['reject_codes'])

    def test_dual_notional_identical_or_conflict(self):
        p,q,e = fixture()
        p['open_positions'][0]['tranches'][0]['notional_usdt'] = 16000
        self.assertTrue(assess_capital(p,q,e)['allowed'])
        p['open_positions'][0]['tranches'][0]['notional'] = 1
        self.assertIn('TRANCHE_NOTIONAL_CONFLICT', assess_capital(p,q,e)['reject_codes'])
        p['open_positions'][0]['tranches'][0]['notional_usdt'] = None
        self.assertFalse(assess_capital(p,q,e)['allowed'])

    def test_existing_reserve_cannot_free_ordinary_band(self):
        p,q,e = fixture()
        p['open_positions'][0]['tranches'] = [
            {'notional_usdt': 17000, 'capital_class': 'ORDINARY'},
            {'notional_usdt': 1000, 'capital_class': 'STRATEGIC_RESERVE',
             'qualification_evidence_id': 'historical-qualification-1'}]
        self.assertIn('ORDINARY_17K_CAP_REJECT', assess_capital(p,q,e)['reject_codes'])
        q['use_strategic_reserve'] = True
        result = assess_capital(p,q,e)
        self.assertTrue(result['allowed'])
        self.assertEqual(result['proposed_allocation'], {'ordinary_usdt': 0, 'reserve_usdt': 1000})

    def test_ordinary_boundary_and_tail_warning(self):
        inputs = fixture()
        old = copy.deepcopy(inputs)
        result = assess_capital(*inputs)
        self.assertTrue(result['allowed'])
        self.assertEqual(result['proposed_allocation'], {'ordinary_usdt': 1000, 'reserve_usdt': 0})
        self.assertEqual(result['warnings'], ['TAIL_STRESS_WARNING'])
        self.assertEqual(inputs, old)

    def test_ordinary_full_despite_cash(self):
        result = assess_capital(*fixture(17000))
        self.assertFalse(result['allowed'])
        self.assertIn('ORDINARY_17K_CAP_REJECT', result['reject_codes'])
        self.assertEqual(result['available_cash_usdt'], 10000)

    def test_strategic_crossing_splits_only_authorized_space(self):
        result = assess_capital(*fixture(16500, 1000, True))
        self.assertTrue(result['allowed'])
        self.assertTrue(result['strategic_qualified'])
        self.assertEqual(result['proposed_allocation'], {'ordinary_usdt': 500, 'reserve_usdt': 500})

    def test_profit_cannot_expand_hard_limit(self):
        result = assess_capital(*fixture(19500, 501, True))
        self.assertIn('TOTAL_20K_CAP_REJECT', result['reject_codes'])
        # Explicit candidate scenario at full market utilization still caps20K.
        params = {'market_utilization': {'STRONG': 1}}
        self.assertTrue(assess_capital(*fixture(19000, 1000, True), params=params)['allowed'])
        self.assertFalse(assess_capital(*fixture(19000, 1000.01, True), params=params)['allowed'])

    def test_market_reduction_and_systemic_apply_to_reserve(self):
        p, q, e = fixture(17000, 1000, True)
        e['market']['regime'] = 'NEUTRAL'
        self.assertIn('MARKET_REGIME_CAP_REJECT', assess_capital(p,q,e)['reject_codes'])
        e['market']['regime'] = 'STRONG'
        e['systemic']['freeze'] = True
        self.assertIn('SYSTEMIC_RISK_FREEZE', assess_capital(p,q,e)['reject_codes'])
        e['systemic']['freeze'] = False
        e['circuit_breaker']['freeze'] = True
        self.assertIn('CIRCUIT_BREAKER_FREEZE', assess_capital(p,q,e)['reject_codes'])

    def test_reserve_missing_each_domain_and_score_cannot_bypass(self):
        for name in ['identity', 'execution', 'liquidity', 'scenario', 'btc_relative',
                     'freshness', 'concentration', 'supply']:
            with self.subTest(name=name):
                p,q,e = fixture(17000,1000,True)
                e.pop(name)
                e['score'] = {'value': 100}
                self.assertIn('STRATEGIC_RESERVE_NOT_QUALIFIED', assess_capital(p,q,e)['reject_codes'])

    def test_identity_mismatch_never_qualified(self):
        p,q,e = fixture(17000,1000,True)
        e['identity']['mismatch'] = True
        self.assertFalse(assess_capital(p,q,e)['strategic_qualified'])
        e['identity'].pop('mismatch')
        self.assertFalse(assess_capital(p,q,e)['strategic_qualified'])

    def test_unknown_material_risk_and_freeze_not_pass(self):
        p,q,e = fixture(17000,1000,True)
        e['supply'].pop('material_risk')
        self.assertFalse(assess_capital(p,q,e)['strategic_qualified'])
        e['systemic'].pop('freeze')
        self.assertIn('SYSTEMIC_RISK_FREEZE', assess_capital(p,q,e)['reject_codes'])

    def test_stale_future_or_unstamped_evidence(self):
        for timestamp in [None, '2026-10-06T14:54:59Z', '2026-10-06T15:00:01Z', '2026-10-06T15:00:00']:
            p,q,e = fixture(17000,1000,True)
            e['execution']['source_timestamp'] = timestamp
            self.assertFalse(assess_capital(p,q,e)['strategic_qualified'])

    def test_full_size_symbol_action_matching(self):
        for key, value in [('verified_notional_usdt', 999), ('asset', 'OTHER'),
                           ('action', 'SELL'), ('full_size_valid', 'true')]:
            p,q,e = fixture(17000,1000,True)
            e['execution'][key] = value
            self.assertFalse(assess_capital(p,q,e)['strategic_qualified'])

    def test_concentration_marginal_independent(self):
        p,q,e = fixture()
        e['concentration']['status'] = 'UNKNOWN'
        e['marginal_edge']['status'] = 'REJECT'
        result = assess_capital(p,q,e)
        self.assertIn('CONCENTRATION_REJECT', result['reject_codes'])
        self.assertIn('MARGINAL_EDGE_REJECT', result['reject_codes'])

    def test_ordinary_execution_unknown_not_pass(self):
        p,q,e = fixture()
        e.pop('execution')
        self.assertIn('EXECUTION_EVIDENCE_UNKNOWN', assess_capital(p,q,e)['reject_codes'])

    def test_cash_not_equity_and_bad_inputs_fail_closed(self):
        p,q,e = fixture()
        p.pop('cash_usdt')
        self.assertIn('AVAILABLE_CASH_REJECT', assess_capital(p,q,e)['reject_codes'])
        p['open_positions'][0]['tranches'][0]['notional'] = float('nan')
        self.assertIn('PORTFOLIO_EXPOSURE_UNKNOWN', assess_capital(p,q,e)['reject_codes'])
        e['market'] = 'UNKNOWN'
        self.assertIn('MARKET_REGIME_EVIDENCE_UNKNOWN', assess_capital(p,q,e)['reject_codes'])

    def test_legacy_all_ordinary_duplicate_tranche_reject(self):
        p,q,e = fixture()
        result = assess_capital(p,q,e)
        self.assertEqual(result['ordinary_used_usdt'], 16000)
        self.assertEqual(result['unclassified_tranches_count'], 1)
        p['open_positions'][0]['tranches'] = [
            {'notional': 1000, 'tranche_id': 'same'}, {'notional': 1000, 'tranche_id': 'same'}]
        self.assertIn('DUPLICATE_TRANCHE_ID', assess_capital(p,q,e)['reject_codes'])

    def test_reserve_existing_requires_qualification(self):
        p,q,e = fixture(17000,1000,True)
        p['open_positions'][0]['tranches'][0]['capital_class'] = 'STRATEGIC_RESERVE'
        result = assess_capital(p,q,e)
        self.assertIn('EXISTING_RESERVE_QUALIFICATION_UNKNOWN', result['reject_codes'])
        self.assertIn('STRATEGIC_RESERVE_CAP_REJECT', result['reject_codes'])

    def test_add_same_caps_and_qualification(self):
        p,q,e = fixture(17000,1000,True)
        q['action'] = 'ADD'
        self.assertFalse(assess_capital(p,q,e)['allowed'])
        e['execution']['action'] = 'ADD'
        self.assertTrue(assess_capital(p,q,e)['allowed'])


if __name__ == '__main__':
    unittest.main()
