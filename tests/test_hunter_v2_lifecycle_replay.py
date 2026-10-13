import unittest
import json
from unittest.mock import patch
from dataclasses import dataclass
from research.hunter_v2_lifecycle_replay import CandidateParameters, close_observations, simulate_reference, build_report, inventory

class ReplayTests(unittest.TestCase):
    def position(self):
        return {'shadow_id':'p','asset':'X','opened_at_utc':'2026-01-01T00:00:00Z',
                'tranches':[{'at':'2026-01-01T00:00:00Z','price':100,'notional_usdt':1000}]}
    def observations(self, prices):
        return [{'at':f'2026-01-01T00:{i:02d}:00Z','price':p} for i,p in enumerate(prices)]
    def test_inventory_recovers_erased_positions_and_preserves_open(self):
        p=self.position(); q={**p,'shadow_id':'q','asset':'Y'}
        states={'head':{'open_positions':[q],'closed_positions':[]},'old':{'open_positions':[p],'closed_positions':[]}}
        def fake_git(repo,*args):
            if args[0]=='rev-parse': return 'head'
            if args[0]=='log': return 'old\nhead\n'
            return json.dumps(states[args[1].split(':')[0]])
        with patch('research.hunter_v2_lifecycle_replay.git',side_effect=fake_git): r=inventory('.')
        self.assertEqual(r['coverage']['current_positions'],1)
        self.assertEqual(r['coverage']['recoverable_position_ids'],2)
        self.assertEqual(r['coverage']['historical_executable_complete'],0)
        self.assertEqual(r['latest_counts']['open'],1)
    def test_future_price_not_used(self):
        p=self.position(); params=CandidateParameters()
        a=simulate_reference(p,self.observations([100,101]),params,0)
        b=simulate_reference(p,self.observations([100,101,120]),params,0)
        self.assertEqual(a['trace'],b['trace'][:2]); self.assertIsNone(a['exit'])
    def test_no_fake_executable(self):
        r=simulate_reference(self.position(),self.observations([100,104,101]),CandidateParameters(),0)
        self.assertEqual(r['execution_comparison_status'],'UNVERIFIABLE')
        self.assertEqual(r['exit']['scope'],'COUNTERFACTUAL_REFERENCE_CLOSE_NOT_EXECUTED')
    def test_add_not_counted_as_profit_or_reset(self):
        p=self.position();p['tranches'].append({'at':'2026-01-01T00:02:00Z','price':104,'notional_usdt':1000})
        r=simulate_reference(p,self.observations([100,104,104]),CandidateParameters(),0)
        self.assertEqual(r['trace'][-1]['state'],'PROFIT_PROTECTION_ARMED')
        self.assertGreaterEqual(r['trace'][-1]['protected_floor_usdt'],r['trace'][-2]['protected_floor_usdt'])
        self.assertLess(r['trace'][-1]['net_pnl_usdt'],r['trace'][-2]['net_pnl_usdt'])
        self.assertIsNone(r['exit'])
    def test_high_low_never_observed(self):
        bars=[[0,100,9999,.001,101,0,59999]]
        obs=close_observations(bars); self.assertEqual(obs[0]['price'],101)
    def test_no_ordinary_negative_exit(self):
        r=simulate_reference(self.position(),self.observations([100,104,95]),CandidateParameters(),0)
        self.assertIsNone(r['exit']); self.assertLess(r['terminal_mark_net_pnl_usdt'],0)
    def test_no_partial_horizon_claim(self):
        r=simulate_reference(self.position(),self.observations([100,104,101,110]),CandidateParameters(),0)
        self.assertIsNone(r['post_exit']['1h']['window_max_return_pct'])
    def test_future_add_not_active(self):
        p=self.position();p['tranches'].append({'at':'2026-01-02T00:00:00Z','price':50,'notional_usdt':1000})
        r=simulate_reference(p,self.observations([100]),CandidateParameters(),0)
        self.assertEqual(r['trace'][0]['invested_usdt'],1000)
    def test_real_candidate_adapter_never_forges_verified_book(self):
        @dataclass(frozen=True)
        class Params(CandidateParameters):
            allow_assumption_only: bool = False
        seen=[]
        def advance(position,execution,signal,now,generation,params):
            seen.append(execution)
            self.assertTrue(params.allow_assumption_only)
            self.assertFalse(signal['fresh'])
            return {**position,'protection_state':'UNARMED','protected_net_pnl_floor_usdt':0}, {'action':'HOLD'}
        simulate_reference(self.position(),self.observations([100,104]),CandidateParameters(),0,(advance,Params))
        self.assertEqual(seen[-1]['status'],'ASSUMPTION_ONLY')
        self.assertFalse(seen[-1]['full_quantity_verified'])
        self.assertFalse(seen[-1]['execution_verified'])
    def test_report_never_approves_missing_data(self):
        e={'positions':[],'frozen_commit':'abc','coverage':{},'latest_counts':{},'history_commits_read':0,'history_read_failures':[]}
        r=build_report(e); self.assertFalse(r['candidate_enabled'])
        self.assertEqual(r['decision'],'NOT_ENOUGH_EVIDENCE_TO_REPLACE_CURRENT_V2')
        self.assertIsNone(r['metrics']['new_buy_count']['value'])
if __name__ == '__main__': unittest.main()
