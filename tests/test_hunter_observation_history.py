"""Synthetic candle evidence only: no exchange access and no ledger writes."""
import copy
import datetime as dt
import json
import unittest
from unittest.mock import patch, MagicMock

from research import hunter_shadow_trader_v2 as eng
from research import hunter_observation_history as history
from test_hunter_monitor_boundaries import NOW, closed_position
from test_hunter_observation_fixtures import bars_between


class HistoryCoverageTests(unittest.TestCase):
    def setUp(self):
        self.budget = patch.object(eng, '_opportunity_backfill_requests', 0)
        self.budget.start()
        self.addCleanup(self.budget.stop)

    def backfill(self, pos, rows, now=NOW):
        with patch.object(eng, 'binance_kline_bars', return_value=rows):
            eng.backfill_opportunity_history(pos, now)

    def complete_rows(self, pos):
        return bars_between(eng.parse(pos['opened_at_utc']), eng.parse(pos['closed_at_utc'])+dt.timedelta(hours=72))

    def test_full_72h_coverage_completes_and_repeated_cycle_preserves_events(self):
        pos = closed_position(); rows = self.complete_rows(pos)
        state = dict(closed_positions=[pos], events=[{'event_id':'historical-sell','price':100}], decisions=[{'action':'SELL'}])
        events, decisions = copy.deepcopy(state['events']), copy.deepcopy(state['decisions'])
        with patch.object(eng, 'binance_kline_bars', return_value=rows) as fetch:
            eng.refresh_closed_observations(state, NOW)
            first = copy.deepcopy(pos)
            eng.refresh_closed_observations(state, NOW)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(pos, first)
        self.assertEqual(state['events'], events); self.assertEqual(state['decisions'], decisions)
        self.assertTrue(pos['observation_complete'])
        cov = pos['post_exit_observation']['72h']['coverage']
        self.assertEqual(cov['expected_bars'], 864); self.assertEqual(cov['missing_bars'], 0)
        self.assertEqual(pos['post_exit_evaluation']['status'], 'COMPLETE')

    def test_internal_gap_is_retried_then_completes_after_restart(self):
        pos = closed_position(); rows = self.complete_rows(pos)
        removed = rows.pop(40)
        self.backfill(pos, rows)
        self.assertFalse(pos['observation_complete'])
        self.assertEqual(pos['opportunity_history_progress']['next_open_ms'], removed[0])
        pos = json.loads(json.dumps(pos))
        # Resumed evidence starts at the gap, including subsequent candles.
        self.backfill(pos, [removed]+rows[40:])
        self.assertTrue(pos['observation_complete'])
        self.assertEqual(pos['opportunity_history_progress']['bars_seen'], len(rows)+1)

    def test_missing_head_or_tail_never_completes(self):
        for missing in (0, -1):
            with self.subTest(missing=missing):
                pos=closed_position(); rows=self.complete_rows(pos); rows.pop(missing)
                self.backfill(pos, rows)
                self.assertFalse(pos['observation_complete'])
                self.assertEqual(pos['post_exit_evaluation']['status'], 'BACKFILL_PENDING')

    def test_duplicates_cannot_replace_missing_candles(self):
        pos=closed_position(); rows=self.complete_rows(pos)
        rows[50]=rows[49]
        self.backfill(pos, rows)
        self.assertEqual(pos['opportunity_history_progress']['bars_seen'], 50)
        self.assertFalse(pos['observation_complete'])

    def test_conflicting_duplicate_nonfinite_and_invalid_close_are_gaps(self):
        for kind in ('conflict', 'nan', 'inf', 'zero', 'unfinished', 'legacy'):
            with self.subTest(kind=kind):
                pos=closed_position(); rows=self.complete_rows(pos); t,c,h=rows[20]
                if kind=='conflict':rows.append((t,c,h+1))
                elif kind=='unfinished':rows[20]=(t,c+1,h)
                elif kind=='legacy':rows[20]=(t,h)
                else:rows[20]=(t,c,{'nan':float('nan'),'inf':float('inf'),'zero':0}[kind])
                self.backfill(pos, rows)
                self.assertEqual(pos['opportunity_history_progress']['bars_seen'],20)
                self.assertFalse(pos['observation_complete'])

    def test_future_and_pre_entry_prices_do_not_enter_peaks(self):
        pos=closed_position(); rows=self.complete_rows(pos); first,last=rows[0][0],rows[-1][0]
        self.backfill(pos, [(first-300000,first-1,99999.)]+rows+[(last+300000,last+599999,99999.)])
        self.assertEqual(pos['full_opportunity_peak_price'],105.)
        self.assertTrue(pos['observation_complete'])

    def test_sub_candle_edges_are_explicit_without_future_high(self):
        pos=closed_position()
        for field in ('opened_at_utc','closed_at_utc'):
            pos[field]=(eng.parse(pos[field])+dt.timedelta(seconds=30)).isoformat()
        rows=self.complete_rows(pos); self.backfill(pos,rows)
        cov=pos['post_exit_observation']['72h']['coverage']
        self.assertEqual(cov['start_edge_excluded_ms'],270000)
        self.assertEqual(cov['end_edge_excluded_ms'],30000)
        self.assertEqual(cov['expected_bars'],863)
        self.assertTrue(pos['observation_complete'])

    def test_legacy_complete_record_requires_new_coverage_proof(self):
        pos=closed_position(); pos['observation_complete']=True
        pos['post_exit_observation']={'72h':{'max_return_from_sell_pct':50.}}
        eng.ensure_opportunity_observation(pos,100.,NOW)
        eng.refresh_post_exit_status(pos,NOW)
        self.assertFalse(pos['observation_complete'])
        self.assertIn('72h',pos['post_exit_evaluation']['missing_horizons'])

    def test_bound_to_position_window_and_venue(self):
        pos=closed_position(); self.backfill(pos,self.complete_rows(pos))
        pos['closed_at_utc']=(eng.parse(pos['closed_at_utc'])+dt.timedelta(hours=1)).isoformat()
        self.assertFalse(eng.observation_complete(pos,NOW))
        pos['execution_venue']='BYBIT_SPOT'
        with patch.object(eng,'binance_kline_bars',side_effect=AssertionError('wrong venue')):
            eng.backfill_opportunity_history(pos,NOW)
        self.assertEqual(pos['historical_opportunity_execution_status'],'UNKNOWN_VENUE_MATCHED_HISTORY_NOT_INTEGRATED')

    def test_one_http_page_per_budget_unit_and_resume_cursor(self):
        pos=closed_position(); pos['opened_at_utc']=(NOW-dt.timedelta(hours=250)).isoformat()
        requests=[]
        def response(req,timeout):
            import urllib.parse
            query=urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
            start=int(query['startTime'][0]); stop=int(query['endTime'][0])
            requests.append(start)
            rows=[[t,'0',105.,'0','0','0',t+299999] for t in range(start,min(start+300000000,stop),300000)]
            result=MagicMock(); result.__enter__.return_value.read.return_value=json.dumps(rows).encode()
            return result
        with patch.object(eng,'OPPORTUNITY_BACKFILL_BUDGET',2),patch.object(eng.urllib.request,'urlopen',side_effect=response):
            for _ in range(3):eng.backfill_opportunity_history(pos,NOW)
            self.assertEqual(len(requests),2)
            self.assertEqual(requests[1]-requests[0],1000*300000)
            self.assertFalse(pos['observation_complete'])
            eng._opportunity_backfill_requests=0
            pos=json.loads(json.dumps(pos)); eng.backfill_opportunity_history(pos,NOW)
        self.assertTrue(pos['observation_complete'])
        self.assertEqual(len(requests),3)

    def test_http_failure_remains_pending_without_advancing_or_rewriting_sell(self):
        pos=closed_position(); original=copy.deepcopy(pos)
        with patch.object(eng.urllib.request,'urlopen',side_effect=TimeoutError):
            eng.backfill_opportunity_history(pos,NOW)
        self.assertEqual(pos['opportunity_history_progress']['bars_seen'],0)
        self.assertFalse(pos['observation_complete'])
        for key in ('closed_at_utc','exit_reference_price','tranches','net_pnl_usdt','net_return_pct'):
            self.assertEqual(pos[key],original[key])

    def test_partial_horizons_advance_only_when_due_and_covered(self):
        pos=closed_position(); sold=eng.parse(pos['closed_at_utc'])
        now=sold+dt.timedelta(hours=2)
        self.backfill(pos,bars_between(eng.parse(pos['opened_at_utc']),now),now)
        self.assertEqual(pos['post_exit_evaluation']['completed_horizons'],['1h'])
        self.assertEqual(pos['post_exit_evaluation']['status'],'OBSERVING')
        self.assertFalse(pos['observation_complete'])
        self.backfill(pos,bars_between(now,sold+dt.timedelta(hours=72)))
        self.assertTrue(pos['observation_complete'])


if __name__=='__main__':unittest.main()
