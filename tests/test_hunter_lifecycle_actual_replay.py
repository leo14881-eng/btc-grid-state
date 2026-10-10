"""Recorded main inputs. No generated market prices, candles or order books."""
import copy
import datetime as dt
import hashlib
import json
import pathlib
import unittest
from unittest.mock import patch
from research import hunter_shadow_trader_v2 as engine
from research import hunter_lifecycle_state as lifecycle


FIXTURE=pathlib.Path(__file__).parent/'fixtures/hunter_v2_prom_actual_monitor.json'


def context(row, asset):
    # Adapt exact saved scalar evidence into the manager's existing input schema.
    evidences=[d.get('evidence') or {} for d in row['actual_decisions'] if d.get('asset')==asset]
    ev=next((e for e in evidences if e.get('signal_evidence')), {})
    mapping={'score':'score','independent_signal_count':'independent','btc_relative_1h_pct':'btc_rel_1h',
             'btc_relative_4h_pct':'btc_rel_4h','relative_acceleration_pct':'rel_accel',
             'return_1h_pct':'return_1h','return_4h_pct':'return_4h'}
    candidate=dict(asset=asset,signal={k:ev[v] for k,v in mapping.items() if v in ev},
        signal_evidence=ev.get('signal_evidence'),blockers=ev.get('blockers',[]),
        execution_scenario={k:ev.get(k) for k in ('buy_slippage_bps','estimated_rr')})
    liquid={k:ev.get(k) for k in ('spread_bps','bid_depth_2pct_usdt','ask_depth_2pct_usdt')}
    liquid.update(as_of_utc=ev.get('book_observed_at_utc'),raw_book_evidence=row['raw_exit_book'])
    scan=dict(generation_id=row['generation_id'],as_of_utc=row['market_as_of_utc'],
              coins={asset:dict(reference_price=row['reference_price'])})
    return scan,dict(candidates=[candidate]),dict(snapshots={asset:liquid}),{}


class ActualProfitProtectionReplayTests(unittest.TestCase):
    def test_report_hashes_match_portable_lf_git_contents(self):
        root=pathlib.Path(__file__).resolve().parents[1]
        report=json.loads((root/'docs/hunter-v2-lifecycle-evidence/profit_protection_actual_replay.json').read_text(encoding='utf-8'))
        for path,sha_key,blob_key in [(FIXTURE,'fixture_sha256','fixture_git_blob_sha1'),
                (root/'research/hunter_lifecycle_state.py','current_lifecycle_source_sha256','current_lifecycle_source_git_blob_sha1')]:
            content=path.read_bytes().replace(b'\r\n',b'\n')
            self.assertEqual(hashlib.sha256(content).hexdigest(),report[sha_key])
            self.assertEqual(hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest(),report[blob_key])

    def setUp(self):
        self.fixture=json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertFalse(self.fixture['synthetic_price_or_book_inputs'])

    def test_recorded_depth_arm_peak_floor_and_exactly_one_manager_sell(self):
        f=self.fixture
        state=dict(open_positions=[copy.deepcopy(f['initial_position'])],closed_positions=[],
                   decisions=[],events=copy.deepcopy(f['initial_target_events']))
        prefix=copy.deepcopy(state['events']);exits=0
        with patch.object(engine,'ENTRY_MODE','EXECUTABLE'),patch.object(engine,'EVENT_PREFIX','SHADOW_V2'),patch.object(engine,'backfill_opportunity_history',side_effect=lambda p,n:p):
            for row in f['cycles']:
                now=dt.datetime.fromisoformat(row['evaluated_at_utc'])
                args=context(row,f['asset']);before=len(state['events'])
                engine.manage_existing_positions(state,*args,now,capital_proposals=[])
                exits+=len(state['events'])-before
                p=(state['open_positions'] or state['closed_positions'])[-1]
                # Interleaved old research marks may now be correctly rejected;
                # every current Monitor row still matches recorded PP state.
                if row['cycle_kind']=='MONITOR':
                    self.assertEqual(p['protection_lifecycle'],row['expected_protection_lifecycle'])
                persisted=json.loads(json.dumps(state))
                engine.manage_existing_positions(state,*args,now,capital_proposals=[])
                self.assertEqual(state,persisted)
        self.assertEqual(exits,1);self.assertEqual(state['events'][:-1],prefix)
        self.assertEqual(state['events'][-1]['reason'],'PROFIT_PROTECTION')
        self.assertEqual(state['closed_positions'][0]['last_monitor_decision']['action'],'SELL_PROFIT_PROTECTION')
        self.assertEqual(state['events'][-1]['net_pnl_usdt'],16.2)
        self.assertEqual(state['open_positions'],[])

    def test_liquidation_and_persistent_state_match_all_nine_actual_inputs(self):
        f=self.fixture;p=copy.deepcopy(f['initial_position']);count=0
        for row in f['cycles']:
            now=dt.datetime.fromisoformat(row['evaluated_at_utc']);params=row['parameters']
            actual=lifecycle.liquidation(p,row['raw_exit_book'],now,engine.FEE_BPS)
            self.assertEqual(actual,row['expected_exit_estimate'])
            result=lifecycle.protect(p,row['reference_price'],engine.net_pnl(p,row['reference_price']),actual,
                now,row['generation_id'],row['market_as_of_utc'])
            if result['exit']:
                count+=1;p['protection_lifecycle'].update(state='EXITED',exited_at_utc=now.isoformat(),exited_generation_id=row['generation_id'])
            self.assertEqual(p['protection_lifecycle'],row['expected_protection_lifecycle'])
        self.assertEqual(count,1)


if __name__=='__main__':unittest.main()
