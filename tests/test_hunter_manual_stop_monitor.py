"""Synthetic integration through run_lane and the unchanged seven-file writer."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from research import hunter_manual_stop_once as manual
from research import hunter_position_monitor as monitor
from research import hunter_scheduler_health as scheduler
from scripts import hunter_monitor_persist as writer

NOW = dt.datetime(2026, 10, 10, 22, 10, tzinfo=dt.timezone.utc)
SHA = 'cb3891809653f56499849bcc47159b5f1908e417'


def fixture():
    positions=[]; snapshots={}; market={'BTC': {'reference_price': 100, 'change_24h_pct': 0}}
    for asset, (sid, entry, slip) in manual.TARGETS.items():
        positions.append(dict(asset=asset,shadow_id=sid,opened_at_utc='2026-10-05T07:51:47+00:00',
            execution_venue='BINANCE_SPOT',market_symbol=asset+'USDT',market_type='spot',
            execution_fee_bps=10,capital_authority='NONE_SHADOW_ONLY',mfe_pct=3,mae_pct=-20,
            tranches=[dict(price=entry,notional_usdt=1000,buy_slippage_bps=slip)]))
        price=entry*.85
        snapshots[asset]={'raw_book_evidence':dict(exchange='binance',market='spot',symbol=asset+'USDT',
            price_unit='USDT',quantity_unit='BASE',fetched_at=NOW.isoformat(),
            bids=[[price,100],[price*.99,10000]],asks=[[price*1.001,10000]])}
        market[asset]={'reference_price':price,'change_24h_pct':0}
    unrelated=copy.deepcopy(positions[0]);unrelated.update(asset='OTHER',shadow_id='SHV2-other',market_symbol='OTHERUSDT')
    positions.insert(1,unrelated);market['OTHER']={'reference_price':.2,'change_24h_pct':0}
    return dict(schema='hunter_shadow_v2_portfolio_v2',mode='SIMULATION_ONLY_NO_REAL_ORDERS',
        open_positions=positions,closed_positions=[],events=[],decisions=[]),market,{'snapshots':snapshots}


class MonitorHookTests(unittest.TestCase):
    def setUp(self):
        monitor.configure_lane(False)
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'v2.json'
        self.clock=patch.object(monitor.dt,'datetime',wraps=dt.datetime).start()
        self.clock.now.return_value=NOW
        self.sha=patch.object(monitor.eng,'_execution_source_sha',return_value=SHA).start()
        # The offline test deliberately excludes HTTP opportunity backfills.
        self.backfill=patch.object(monitor.eng,'backfill_opportunity_history',lambda pos,now:pos).start()
        self.addCleanup(patch.stopall);self.addCleanup(self.temp.cleanup)

    def run_cycle(self,state,market,liq,now=NOW,v1=False):
        self.path.write_text(json.dumps(state))
        out=monitor.run_lane(self.path,'SHADOW_V1' if v1 else 'SHADOW_V2',market,{'candidates':[]},liq,{},now,v1,
            regime_scan={'generation_id':'NATURAL_MONITOR','as_of_utc':now.isoformat(),'coins':market})
        return json.loads(self.path.read_text()),json.loads(self.path.with_name('v2-summary.json').read_text()),out

    def test_exact_request_runs_before_manager_and_summary_matches(self):
        state,market,liq=fixture()
        original_manager=monitor.eng.manage_existing_positions
        seen=[]
        def manager(st,*args,**kw):
            seen.extend(p['asset'] for p in st['open_positions'])
            return original_manager(st,*args,**kw)
        with patch.object(monitor.eng,'manage_existing_positions',side_effect=manager):
            saved,summary,out=self.run_cycle(state,market,liq)
        self.assertEqual(seen,['OTHER'])
        self.assertEqual(out['closed'],['ENA','PENDLE'])
        self.assertEqual(out['open'],summary['open_positions'])
        self.assertEqual(summary['closed_positions'],2)
        self.assertEqual(summary['exit_decision_cohorts']['user_manual']['count'],2)
        self.assertEqual(summary['exit_decision_cohorts']['automatic_strategy']['count'],0)
        self.assertEqual(summary['exit_decision_cohorts']['user_manual']['net_pnl_usdt'],summary['realized_net_pnl_usdt'])
        self.assertEqual(summary['realized_net_pnl_usdt'],round(sum(p['net_pnl_usdt'] for p in saved['closed_positions']),2))
        self.assertEqual(saved['last_cycle_generation_id'],'NATURAL_MONITOR')
        self.assertEqual(saved['active_observation_generation_id'],'NATURAL_MONITOR')
        self.assertEqual(out['manual_stop_request']['status'],'NOT_PUBLISHED')
        self.assertEqual(out['manual_stop_request']['staged_count'],2)
        self.assertTrue(all(e['reason']==manual.REASON for e in saved['events']))
        self.assertTrue(all(e['generation_id']=='NATURAL_MONITOR' for e in saved['events']))
        self.assertEqual(summary['policy']['capital_pool_usdt'],20000)
        self.assertEqual(summary['policy']['capital_management']['ordinary_opportunity_cap'],17000)
        self.assertEqual(summary['policy']['capital_management']['strategic_reserve'],3000)
        again,_,out2=self.run_cycle(saved,market,liq)
        self.assertEqual(again['events'],saved['events'])
        self.assertEqual(again['manual_exit_requests'],saved['manual_exit_requests'])
        self.assertNotIn('manual_stop_request',out2)

    def test_stale_after_v1_work_blocks_without_falling_into_other_sell_reason(self):
        state,market,liq=fixture()
        self.clock.now.return_value=NOW+dt.timedelta(seconds=31)
        seen=[]
        def aggressive_manager(st,*args,**kw):
            seen.extend(p['asset'] for p in st['open_positions'])
            st['active_observation_generation_id']='NATURAL_MONITOR'
        with patch.object(monitor.eng,'manage_existing_positions',side_effect=aggressive_manager):
            saved,_,out=self.run_cycle(state,market,liq)
        self.assertEqual(seen,['OTHER'])
        self.assertEqual(saved['open_positions'],state['open_positions'])
        self.assertEqual(saved['events'],[])
        self.assertEqual(out['manual_stop_request']['staged_count'],0)
        self.assertTrue(all(x['status']=='BLOCKED' for x in out['manual_stop_request']['targets']))

    def test_partial_then_new_natural_cycle_only_remaining_target(self):
        state,market,liq=fixture();pendle=liq['snapshots'].pop('PENDLE')
        saved,_,out=self.run_cycle(state,market,liq)
        self.assertEqual(out['closed'],['ENA'])
        self.assertEqual([p['asset'] for p in saved['open_positions']],['OTHER','PENDLE'])
        liq['snapshots']['PENDLE']=pendle
        done,_,out=self.run_cycle(saved,market,liq)
        self.assertEqual(out['closed'],['PENDLE'])
        self.assertEqual(len(done['events']),2)
        self.assertEqual(len({e['manual_event_id'] for e in done['events']}),2)

    def test_v1_and_same_asset_new_id_do_not_consume_request(self):
        state,market,liq=fixture()
        with patch.object(manual,'stage',side_effect=AssertionError('V1 must not call stage')):
            self.run_cycle(state,market,liq,v1=True)
        state,market,liq=fixture()
        for p in state['open_positions']:p['shadow_id']+='-new'
        with patch.object(manual,'stage',side_effect=AssertionError('new positions must not call stage')):
            self.run_cycle(state,market,liq)

    def test_seven_file_contract_natural_generation_and_exact_readback(self):
        state,market,liq=fixture();saved,summary,out=self.run_cycle(state,market,liq)
        v1=dict(schema=state['schema'],mode=state['mode'],open_positions=[],closed_positions=[],updated_at_utc=NOW.isoformat())
        s1=dict(mode=state['mode'],capital_authority='NONE_SHADOW_ONLY',open_positions=0,closed_positions=0,as_of_utc=NOW.isoformat())
        health=scheduler.build_success_health(NOW+dt.timedelta(seconds=1),NOW.isoformat(),{},'TEST_OFFLINE',SHA)
        docs={'hunter-shadow-portfolio.json':v1,'hunter-shadow-summary.json':s1,
            'hunter-shadow-v2-portfolio.json':saved,'hunter-shadow-v2-summary.json':summary,
            'hunter-position-monitor.json':{'results':[{'lane':'SHADOW_V1','open':0},out]},
            'hunter-leading-risk.json':dict(capital_authority='NONE_SHADOW_ONLY',shadow_only=True,real_position_mutation=False,current={'level':'NORMAL'}),
            'hunter-scheduler-health.json':health}
        raw={k:json.dumps(v,sort_keys=True)+'\n' for k,v in docs.items()}
        self.assertEqual(set(raw),set(writer.NAMES));self.assertEqual(len(raw),7)
        generation=health['current_generation_id']
        self.assertEqual(generation,'2026-10-10T22:10:00Z')
        self.assertEqual(writer.validate(raw,generation),[0,1])
        with patch.object(writer,'git',return_value=SimpleNamespace(returncode=0,stdout=SHA)),patch.object(writer,'snapshot',return_value=raw):
            self.assertEqual(writer.readback(raw,generation,SHA),SHA)
        bad=dict(raw);bad['hunter-shadow-v2-portfolio.json']+=' '
        with patch.object(writer,'git'),patch.object(writer,'snapshot',return_value=bad):
            with self.assertRaisesRegex(RuntimeError,'AUTHORITATIVE_SNAPSHOT_MISMATCH'):
                writer.readback(raw,generation)
        self.assertTrue(writer.protected('research/hunter_manual_stop_once.py'))
        self.assertTrue(writer.protected('tests/test_hunter_manual_stop_monitor.py'))

    def test_risk_observation_precedes_exits_and_replay_does_not_count_twice(self):
        state,market,liq=fixture();order=[]
        original_stage=manual.stage
        def risk(st,evidence,now):
            order.append('risk')
            st['systemic_risk']={'level':'NORMAL','last_observation_id':'CURRENT_OBSERVATION'}
        def stage(st,*args,**kwargs):
            order.append('manual')
            self.assertEqual(st['systemic_risk']['last_observation_id'],'CURRENT_OBSERVATION')
            return original_stage(st,*args,**kwargs)
        self.path.write_text(json.dumps(state))
        with patch.object(monitor.eng,'update_risk_controls',side_effect=risk),patch.object(manual,'stage',side_effect=stage):
            monitor.run_lane(self.path,'SHADOW_V2',market,{'candidates':[]},liq,{},NOW,False,risk_evidence={})
        saved=json.loads(self.path.read_text())
        self.assertEqual(order,['risk','manual'])
        self.assertEqual(saved['loss_control_v2']['consecutive_loss_exits'],2)
        self.assertNotIn('loss_freeze_episode',saved)
        again,_,_=self.run_cycle(saved,market,liq)
        self.assertEqual(again['loss_exit_guard'],saved['loss_exit_guard'])
        self.assertEqual(again['loss_control_v2'],saved['loss_control_v2'])
        self.assertNotIn('loss_freeze_episode',again)


if __name__=='__main__':unittest.main()
