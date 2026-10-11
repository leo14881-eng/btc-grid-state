"""Offline reproductions and contract tests for Monitor boundary states."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from research import hunter_position_monitor as monitor
from research import hunter_shadow_trader_v2 as eng
from scripts import hunter_monitor_persist as writer

NOW = dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc)


def closed_position():
    return dict(asset='CLOSED', shadow_id='closed-fixture',
                opened_at_utc=(NOW-dt.timedelta(hours=74)).isoformat(),
                closed_at_utc=(NOW-dt.timedelta(hours=73)).isoformat(),
                exit_reference_price=100., net_return_pct=1., net_pnl_usdt=10.,
                tranches=[dict(price=100., notional_usdt=1000.)],
                observation_complete=False, post_exit_observation={})


class MonitorBoundaryTests(unittest.TestCase):
    def main_cycle(self, states, market, real_lanes=False, history_rows=None):
        docs = {monitor.V1: states[0], monitor.V2: states[1]}
        writes = {}
        def write(path, doc):
            writes[path] = copy.deepcopy(doc)
            docs[path] = copy.deepcopy(doc)
        with ExitStack() as stack:
            stack.enter_context(patch.object(monitor, 'load', side_effect=lambda p: copy.deepcopy(docs.get(p, {}))))
            stack.enter_context(patch.object(monitor, 'batch_market', return_value=market))
            stack.enter_context(patch.object(monitor, 'crypto_exclusions', return_value={'BSTOCK'}))
            stack.enter_context(patch.object(monitor, 'refresh_management_evidence', return_value=({}, {}, {'generation_id': 'fixture', 'evidence_refresh_cursor': 0})))
            stack.enter_context(patch('research.hunter_bybit_management.collect', return_value=({}, {})))
            stack.enter_context(patch.object(eng.tail, 'collect_systemic_evidence', return_value={'missing_or_stale':['offline_fixture']}))
            stack.enter_context(patch.object(monitor.leading, 'collect_evidence', return_value={}))
            stack.enter_context(patch.object(monitor.leading, 'update_state', return_value=({'level': 'NORMAL'}, False)))
            stack.enter_context(patch.object(monitor.leading, 'update_history', return_value=[]))
            stack.enter_context(patch.object(eng.tail, 'tail_budget_snapshot', return_value={}))
            stack.enter_context(patch.object(monitor.books, 'live_fetch', side_effect=RuntimeError('offline book gap')))
            stack.enter_context(patch('research.hunter_fast_reconciliation.observe', return_value={}))
            stack.enter_context(patch.object(eng, 'binance_kline_bars', return_value=history_rows))
            stack.enter_context(patch.object(eng, 'atomic_json_write', side_effect=write))
            lane_impl=monitor.run_lane if real_lanes else lambda path,label,*a: {'lane':label,'open':len(docs[path]['open_positions'])}
            lanes = stack.enter_context(patch.object(monitor, 'run_lane', side_effect=lane_impl))
            stack.enter_context(redirect_stdout(StringIO()))
            monitor.main()
        self.writes=writes
        monitor.configure_lane(False)
        return writes[monitor.OUT], lanes

    def test_closed_quote_gap_does_not_block_open_lane(self):
        state = {'open_positions': [{'asset': 'OPEN', 'tranches': [{'price': 100., 'notional_usdt': 1000.}]}], 'closed_positions': [closed_position()]}
        result, lanes = self.main_cycle([{'open_positions': [], 'closed_positions': []}, state], {'OPEN': {'reference_price': 100.}})
        self.assertEqual(lanes.call_count, 2)
        self.assertEqual(result['observation_market_data']['missing_assets'], ['CLOSED'])

    def test_empty_lanes_satisfy_unchanged_publication_guard(self):
        empty = {'open_positions': [], 'closed_positions': []}
        result, _ = self.main_cycle([empty, empty], {})
        from tests.test_hunter_monitor_persist import fixture, GEN
        raw = fixture()
        for prefix in ('hunter-shadow', 'hunter-shadow-v2'):
            for suffix in ('portfolio', 'summary'):
                name = prefix+'-'+suffix+'.json'
                row = json.loads(raw[name]); row['open_positions'] = [] if suffix == 'portfolio' else 0
                raw[name] = json.dumps(row)
        raw['hunter-position-monitor.json'] = json.dumps(result)
        self.assertEqual(writer.validate(raw, GEN), [0, 0])
        self.assertEqual(result['status'], 'NO_OPEN_POSITIONS')

    def test_two_bars_cannot_complete_72_hours(self):
        pos = closed_position()
        start = int(dt.datetime.fromisoformat(pos['opened_at_utc']).timestamp()*1000)
        sell = int(dt.datetime.fromisoformat(pos['closed_at_utc']).timestamp()*1000)
        with patch.object(eng, '_opportunity_backfill_requests', 0), patch.object(eng, 'binance_kline_bars', return_value=[(start, start+299999, 101.), (sell, sell+299999, 105.)]):
            eng.backfill_opportunity_history(pos, NOW)
        self.assertFalse(pos['observation_complete'])
        self.assertNotEqual(pos['post_exit_evaluation']['status'], 'COMPLETE')

    def test_open_quote_gap_still_blocks_even_when_other_quotes_exist(self):
        states=[{'open_positions':[{'asset':'MISSING'}],'closed_positions':[]}, {'open_positions':[],'closed_positions':[]}]
        with self.assertRaisesRegex(SystemExit,'FAST_MONITOR_MARKET_DATA_MISSING MISSING'):
            self.main_cycle(states,{'OTHER':{'reference_price':100.}})

    def test_same_asset_closed_record_cannot_exempt_open_holding(self):
        states=[{'open_positions':[],'closed_positions':[]}, {'open_positions':[{'asset':'CLOSED'}],'closed_positions':[closed_position()]}]
        with self.assertRaisesRegex(SystemExit,'FAST_MONITOR_MARKET_DATA_MISSING CLOSED'):
            self.main_cycle(states,{})

    def test_bybit_holding_does_not_require_binance_quote(self):
        states=[{'open_positions':[],'closed_positions':[]}, {'open_positions':[{'asset':'BYBIT_ONLY','execution_venue':'BYBIT_SPOT'}],'closed_positions':[]}]
        result,lanes=self.main_cycle(states,{})
        self.assertEqual(lanes.call_count,2)
        self.assertEqual(result['observation_market_data']['missing_assets'],[])

    def test_real_lane_marks_open_position_despite_closed_quote_gap(self):
        from tests.test_hunter_position_monitor import PositionMonitorTests
        state,market,_,_,_=PositionMonitorTests().fixture(price=95.)
        state.update(schema='hunter_shadow_v2_portfolio_v2',closed_positions=[closed_position()],events=[{'event_id':'historical'}])
        empty=dict(schema=state['schema'],mode=state['mode'],open_positions=[],closed_positions=[],events=[],decisions=[])
        result,_=self.main_cycle([empty,state],market,real_lanes=True)
        saved=self.writes[monitor.V2]
        self.assertEqual(saved['open_positions'][0]['last_price'],95.)
        self.assertEqual(saved['events'],state['events'])
        self.assertEqual(saved['last_cycle_generation_id'],'fixture')
        self.assertEqual(saved['closed_positions'][0]['observation_market_data']['status'],'MARKET_DATA_MISSING')
        self.assertFalse(saved['closed_positions'][0]['observation_complete'])
        policy=self.writes[monitor.V2_SUMMARY]['policy']
        self.assertEqual(policy['capital_pool_usdt'],20000)
        self.assertEqual(policy['capital_management']['ordinary_opportunity_cap'],17000)
        self.assertEqual(policy['capital_management']['strategic_reserve'],3000)
        self.assertFalse(result['real_trading_enabled'])
        self.assertEqual(result['capital_authority'],'NONE_SHADOW_ONLY')

    def test_completed_closed_only_cycle_has_empty_lanes_and_does_not_rewrite_ledgers(self):
        from tests.test_hunter_observation_fixtures import certify_horizons
        pos=certify_horizons(closed_position());pos['observation_complete']=True
        state={'open_positions':[],'closed_positions':[pos]}
        result,lanes=self.main_cycle([state,state],{})
        self.assertEqual(result['status'],'NO_OPEN_POSITIONS')
        self.assertEqual([r['open'] for r in result['results']],[0,0])
        self.assertEqual(lanes.call_count,0)
        self.assertEqual(list(self.writes),[monitor.OUT])

    def test_migration_checkpoint_is_persisted_and_resumes_next_cycle(self):
        from tests.test_hunter_position_monitor import PositionMonitorTests
        from tests.test_hunter_observation_fixtures import bars_between
        state,market,_,_,_=PositionMonitorTests().fixture(price=95.)
        state.update(schema='hunter_shadow_v2_portfolio_v2')
        empty=dict(schema=state['schema'],mode=state['mode'],open_positions=[],closed_positions=[],events=[],decisions=[])
        rows=bars_between(eng.parse(state['open_positions'][0]['opened_at_utc']),NOW)
        self.main_cycle([empty,state],market,real_lanes=True,history_rows=rows[:1000])
        saved=self.writes[monitor.V2]
        self.assertEqual(saved['open_positions'][0]['opportunity_history_progress']['bars_seen'],1000)
        self.main_cycle([empty,saved],market,real_lanes=True,history_rows=rows[1000:2000])
        self.assertEqual(self.writes[monitor.V2]['open_positions'][0]['opportunity_history_progress']['bars_seen'],2000)


if __name__ == '__main__':
    unittest.main()
