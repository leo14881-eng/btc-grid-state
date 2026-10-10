"""Independent-review counterexamples. Synthetic inputs, no historical-fill claim."""
import copy
import datetime as dt
import unittest
from unittest.mock import patch
from test_hunter_lifecycle_v2 import NOW, inputs, position, receipt, strong_source
from research import hunter_shadow_trader_v2 as engine
from research import hunter_lifecycle_v2 as lifecycle


class ReviewRegressions(unittest.TestCase):
    def run_position(self, pos, args, now=NOW):
        state=dict(open_positions=[pos],closed_positions=[],events=[],decisions=[])
        with patch.object(engine,'ENTRY_MODE','EXECUTABLE'),patch.object(engine,'EVENT_PREFIX','SHADOW_V2'),patch.object(engine,'backfill_opportunity_history',side_effect=lambda p,n:p),patch.object(engine,'_execution_source_sha',return_value='a'*40),patch('urllib.request.urlopen',side_effect=AssertionError('OFFLINE')):
            engine.manage_existing_positions(state,*args,now,capital_proposals=[])
        return state, pos['last_monitor_decision']

    def test_missing_candidate_and_supply_never_authorize_risk_exit(self):
        args=inputs();args[1].pop('as_of_utc')
        state,d=self.run_position(position(),args)
        self.assertEqual(d['action'],'HOLD')
        self.assertFalse(d['rebound_review']['exit'])
        self.assertFalse(d['rebound_review']['conditions']['candidate_evidence_current'])
        self.assertFalse(d['rebound_review']['conditions']['fundamental_supply_current'])
        self.assertEqual(state['events'],[])

    def test_rewrapping_one_closed_window_does_not_confirm_three_times(self):
        pos=position();pos['degraded_cycles']=0
        for key in ('last_health_evidence_id','last_health_generation_id','last_health_observed_at_utc'):pos.pop(key)
        rows=receipt()['rows'];counts=[]
        for i in range(3):
            now=NOW+dt.timedelta(minutes=5*i);args=inputs(now,'wrapper'+str(i))
            args[1]['candidates'][0]['v2_lifecycle_evidence']['micro_receipt']['rows']=copy.deepcopy(rows)
            state,d=self.run_position(pos,args,now);counts.append(pos['degraded_cycles'])
            self.assertEqual(state['events'],[])
        self.assertLessEqual(counts[-1],1)

    def test_old_relative_source_is_not_refreshed_by_new_stamp(self):
        args=inputs();args[1]['candidates'][0]['signal']['source_closed_at_utc']=(NOW-dt.timedelta(days=1)).isoformat()
        state,d=self.run_position(position(),args)
        self.assertEqual(d['thesis_review']['checks']['signal'],'UNKNOWN')
        self.assertEqual(d['action'],'HOLD');self.assertEqual(state['events'],[])

    def test_live_recovery_veto_is_retained_without_counting_partial_bar_weak(self):
        now=NOW+dt.timedelta(minutes=5);args=inputs(now,'live-recovery',price=97)
        r=args[1]['candidates'][0]['v2_lifecycle_evidence']['micro_receipt'];start=int(NOW.timestamp()*1000)
        r['rows'].append([start,93,98,93,97,10,start+899999,1000,3,2,900,0])
        state,d=self.run_position(position(),args,now)
        tech=d['thesis_review']['technical']
        self.assertTrue(tech['live_recovery_veto'])
        self.assertEqual(tech['close'],93)
        self.assertEqual(d['action'],'HOLD');self.assertEqual(state['events'],[])

    def test_fresh_hard_book_is_not_hidden_by_stale_signal(self):
        args=inputs();args[1]['candidates'][0]['signal_evidence']['observed_at_utc']=(NOW-dt.timedelta(hours=2)).isoformat()
        args[2]['snapshots']['ENA'].update(spread_bps=1000,bid_depth_2pct_usdt=1000,ask_depth_2pct_usdt=1000)
        state,d=self.run_position(position(),args)
        self.assertEqual(d['thesis_status'],'HARD_INVALIDATION')
        self.assertEqual(d['action'],'SELL_HARD_INVALIDATION')
        self.assertEqual(len(state['events']),1)

    def test_geometry_and_numeric_entry_values_are_not_validated_risk_models(self):
        state,d=self.run_position(position(),inputs())
        review=d['rebound_review']
        self.assertEqual(review['risk_comparison']['status'],'UNKNOWN')
        self.assertFalse(review['conditions']['holding_risk_higher'])
        self.assertFalse(review['conditions']['original_thesis_revalidated'])
        self.assertFalse(review['exit']);self.assertEqual(state['events'],[])

    def test_stale_btc_underlying_rows_are_unknown_despite_new_fetch_stamp(self):
        args=inputs();packets=args[1]['candidates'][0]['v2_lifecycle_evidence']['relative_receipts']
        for key in ('btc_1h','btc_4h'):
            for row in packets[key]['rows']:row[0]-=86400000;row[6]-=86400000
        state,d=self.run_position(position(),args)
        self.assertEqual(d['thesis_review']['checks']['signal'],'UNKNOWN')
        self.assertEqual(state['events'],[])

    def test_new_closed_windows_confirm_but_repeat_wrappers_do_not(self):
        pos=position();pos['degraded_cycles']=0
        for key in ('last_health_evidence_id','last_health_generation_id','last_health_observed_at_utc'):pos.pop(key)
        counts=[]
        for i in range(3):
            now=NOW+dt.timedelta(minutes=15*i)
            state,d=self.run_position(pos,inputs(now,'closed'+str(i)),now)
            counts.append(pos['degraded_cycles']);self.assertEqual(state['events'],[])
        self.assertEqual(counts,[1,2,3])
        self.assertEqual(d['thesis_status'],'THESIS_INVALIDATED')

    def test_allocator_final_action_and_rejection_survive_duplicate(self):
        for accepted in (True,False):
            with self.subTest(accepted=accepted):
                args=inputs();pos=position();state,d=self.run_position(pos,args)
                q=dict(kind='ADD',pos=pos,asset='ENA',amount=2000,evidence={},price=93,reasons=['REVALIDATED_ADD'])
                with patch.object(engine,'ENTRY_MODE','EXECUTABLE'),patch.object(engine,'EVENT_PREFIX','SHADOW_V2'),patch.object(engine,'CAPITAL_POOL_USDT',20000),patch.object(engine,'marginal_capital_gate',return_value=(accepted,['CAPITAL_OK' if accepted else 'CAPITAL_LIMIT'])),patch.object(engine.tail,'risk_blocks_new',return_value=False):
                    engine.execute_capital_proposals(state,[q],args[0],NOW)
                    self.assertEqual(pos['last_monitor_decision']['action'],'ADD' if accepted else 'HOLD')
                    self.assertEqual(lifecycle.projection(pos)['last_monitor_action'],'ADD' if accepted else 'HOLD')
                    self.assertEqual(len(pos['tranches']),2 if accepted else 1)
                    self.assertEqual(len(state['events']),1 if accepted else 0)
                    self.assertIn('CAPITAL_OK' if accepted else 'CAPITAL_LIMIT',pos['last_monitor_decision']['reasons'])
                    before=copy.deepcopy(state)
                    engine.execute_capital_proposals(state,[q],args[0],NOW)
                    self.assertEqual(state,before)

    def test_cross_hour_relative_packets_do_not_compare_different_windows(self):
        now=NOW+dt.timedelta(minutes=1);args=inputs(now,'cross-hour')
        packets=args[1]['candidates'][0]['v2_lifecycle_evidence']['relative_receipts']
        for key in ('btc_1h','btc_4h'):
            packets[key]['fetched_at']=(NOW-dt.timedelta(minutes=1)).isoformat()
            for r in packets[key]['rows']:r[0]-=3600000;r[6]-=3600000
        state,d=self.run_position(position(),args,now)
        self.assertEqual(d['thesis_review']['relative_sources']['reason'],'RELATIVE_SOURCE_WINDOWS_NOT_ALIGNED')
        self.assertEqual(state['events'],[])

    def test_recovery_uses_consecutive_closed_windows_and_resets_on_gap(self):
        pos=position();pos['degraded_cycles']=0
        for i in range(5):
            now=NOW+dt.timedelta(minutes=15*i)
            self.run_position(pos,inputs(now,'weak'+str(i)),now)
        self.assertEqual(pos['recovery_state'],'PERSISTENT_INVALIDATION')
        self.assertEqual(pos['loss_recovery_lifecycle']['persistent_invalidation_count'],3)
        for i in range(3):
            now=NOW+dt.timedelta(minutes=75+15*i);generation='recovered'+str(i);args=inputs(now,generation)
            c=args[1]['candidates'][0];c['signal'].update(score=12,independent_signal_count=3,btc_relative_1h_pct=2,btc_relative_4h_pct=3,relative_acceleration_pct=1.25)
            c['v2_lifecycle_evidence']=strong_source(now,generation)
            self.run_position(pos,args,now)
        self.assertEqual(pos['recovery_state'],'RECOVERED')
        now+=dt.timedelta(minutes=30)
        self.run_position(pos,inputs(now,'gap'),now)
        self.assertEqual(pos['degraded_cycles'],1)
        self.assertEqual(pos['loss_recovery_lifecycle']['recovery_observations'],0)


if __name__=='__main__':unittest.main()
