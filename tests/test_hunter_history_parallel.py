"""Differential coverage, page-budget and failure checks; no external IO."""
import copy
import datetime as dt
import io
import json
import threading
import time
import unittest
import urllib.parse
from unittest.mock import patch

from research import hunter_shadow_trader_v2 as eng
from tests.test_hunter_monitor_boundaries import NOW, closed_position


class ParallelHistoryTests(unittest.TestCase):
    def state(self, count=12):
        positions=[]
        for i in range(count):
            pos=closed_position();pos['asset']='ASSET'+str(i)
            pos['opened_at_utc']=(NOW-dt.timedelta(hours=250)).isoformat()
            positions.append(pos)
        return dict(closed_positions=positions,observation_backfill_cursor=3,
                    events=[{'event_id':'immutable-sell'}],decisions=[{'action':'SELL'}])

    def run_case(self, initial, workers, failure=None, budget=8, used=0):
        state=copy.deepcopy(initial);calls=[];active=[0,0];lock=threading.Lock()
        def response(req,timeout):
            q=urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
            asset=q['symbol'][0];start=int(q['startTime'][0]);stop=int(q['endTime'][0])
            with lock:
                calls.append((asset,start,stop,timeout));active[0]+=1;active[1]=max(active)
            try:
                time.sleep(.001*(1+int(asset[5:-4])%4))
                if failure=='timeout' and asset in ('ASSET3USDT','ASSET7USDT'):raise TimeoutError
                if failure=='malformed':return io.BytesIO(b'not-json')
                rows=[[t,'0',105.,'0','0','0',t+299999] for t in range(start,min(start+300000000,stop),300000)]
                if failure=='gap' and len(rows)>20:rows.pop(20)
                if failure=='conflict' and len(rows)>20:rows.append([rows[20][0],'0',999.,'0','0','0',rows[20][6]])
                return io.BytesIO(json.dumps(rows).encode())
            finally:
                with lock:active[0]-=1
        with patch.object(eng,'_opportunity_backfill_requests',used),patch.object(eng,'OPPORTUNITY_BACKFILL_BUDGET',budget),patch.object(eng.urllib.request,'urlopen',side_effect=response):
            eng.refresh_closed_observations(state,NOW,lambda _:100.,historical_workers=workers)
            consumed=eng._opportunity_backfill_requests
        return state, sorted(calls),consumed,active[1]

    def compare(self, state, **kwargs):
        serial=self.run_case(state,1,**kwargs);parallel=self.run_case(state,4,**kwargs)
        self.assertEqual(serial[:3],parallel[:3])
        self.assertLessEqual(parallel[3],4)
        self.assertEqual(parallel[0]['events'],state['events'])
        self.assertEqual(parallel[0]['decisions'],state['decisions'])
        return serial,parallel

    def test_exact_state_requests_budget_and_order_independent_completion(self):
        serial,parallel=self.compare(self.state())
        self.assertEqual(len(parallel[1]),8)
        self.assertGreater(parallel[3],1)

    def test_timeout_malformed_gap_and_conflicting_candles_match_serial(self):
        for failure in ('timeout','malformed','gap','conflict'):
            with self.subTest(failure=failure):self.compare(self.state(),failure=failure)

    def test_partial_and_exhausted_budget_match_serial(self):
        for budget,used in ((0,0),(8,8),(8,6),(2,0)):
            with self.subTest(budget=budget,used=used):self.compare(self.state(),budget=budget,used=used)

    def test_restart_continuation_and_final_horizons_match_serial(self):
        state=self.state(8)
        for _ in range(4):
            _,parallel=self.compare(state);state=json.loads(json.dumps(parallel[0]))
        self.assertTrue(all(p['observation_complete'] for p in state['closed_positions']))

    def test_venue_invalid_asset_and_recent_complete_progress_match_serial(self):
        state=self.state()
        state['closed_positions'][3]['execution_venue']='BYBIT_SPOT'
        state['closed_positions'][4]['asset']=''
        state['closed_positions'][5]['opened_at_utc']='invalid'
        self.compare(state)

    def test_invalid_worker_count_and_alias_fail_before_http(self):
        state=self.state();state['closed_positions'].append(state['closed_positions'][0])
        with patch.object(eng.urllib.request,'urlopen') as fetch:
            with self.assertRaisesRegex(ValueError,'ALIASED'):eng.refresh_closed_observations(state,NOW,historical_workers=4)
            with self.assertRaisesRegex(ValueError,'INVALID_HISTORY_WORKERS'):eng.refresh_closed_observations(self.state(),NOW,historical_workers=5)
            fetch.assert_not_called()

    def test_budget_mutation_during_fetch_aborts_before_apply(self):
        state=self.state()
        def mutate(*args):
            eng._opportunity_backfill_requests=99
            return None
        with patch.object(eng,'_opportunity_backfill_requests',0),patch.object(eng,'_fetch_binance_kline_page',side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError,'BUDGET_CHANGED'):eng.refresh_closed_observations(state,NOW,historical_workers=4)
        self.assertNotIn('opportunity_history_progress',state['closed_positions'][3])

    def test_request_plan_mismatch_aborts(self):
        state=self.state()
        def mutate(*args):
            state['closed_positions'][3]['asset']='CHANGED'
            return None
        with patch.object(eng,'_opportunity_backfill_requests',0),patch.object(eng,'_fetch_binance_kline_page',side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError,'PLAN_MISMATCH'):eng.refresh_closed_observations(state,NOW,historical_workers=4)


if __name__=='__main__':unittest.main()
