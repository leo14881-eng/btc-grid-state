"""Real Monitor decisions with revocable scratch cache; no portfolio/history I/O."""
import copy
import inspect
import json
from contextlib import ExitStack,redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from research import hunter_position_monitor as monitor
from research import hunter_early_signals as early
from research import hunter_binance_stream_consumer as consumer
from tests import test_hunter_binance_consumer_fence as fence_tests
from tests import test_hunter_position_monitor as monitor_tests
from tests.test_hunter_binance_cache import NOW,DATE


class SignalUseTests(unittest.TestCase):
    def setUp(self):
        fence_tests.ConsumerFenceTests.setUp(self)
        self.cached=consumer.read(self.names,DATE,self.store,clock=lambda:self.time)

    def fixture(self,price=120,*,weak=True,two=False):
        state,market,review,liq,supply=monitor_tests.PositionMonitorTests().fixture(price)
        pos=state['open_positions'][0];pos['asset']='ETH'
        pos.update(sample_cohort='NEW_VERSION_SAMPLE',data_provenance='LIVE_OBSERVATION')
        market['ETH']=market.pop('X');candidate=review['candidates'][0];candidate['asset']='ETH'
        candidate['signal_evidence']=monitor.eng.stamp('ETH','MONITOR_USE',DATE.isoformat())
        if weak:candidate['signal'].update(btc_relative_1h_pct=-1,btc_relative_4h_pct=-1,relative_acceleration_pct=-2)
        liq['snapshots']['ETH']=liq['snapshots'].pop('X');liq['snapshots']['ETH']['as_of_utc']=DATE.isoformat()
        if two:
            other=copy.deepcopy(pos);other.update(asset='BTC',shadow_id='BTC2');state['open_positions'].append(other)
            market['BTC']=copy.deepcopy(market['ETH']);row=copy.deepcopy(candidate);row['asset']='BTC'
            row['signal_evidence']=monitor.eng.stamp('BTC','MONITOR_USE',DATE.isoformat())
            review['candidates'].append(row);liq['snapshots']['BTC']=copy.deepcopy(liq['snapshots']['ETH'])
        return state,market,review,liq,supply

    def execute(self,fixture,*,identity=None,capital=None,v1=False,guarded=True):
        state,market,review,liq,supply=fixture;engine=monitor.eng
        path=Path('in-memory-consumer-use.json');writes={};refresh={'failures':{}}
        selected=tuple(c['asset'] for c in review['candidates']);guards=[(self.cached,selected)]
        # This also runs on immutable pre-hook Monitor+engine: its failure is
        # the real stale-signal action below, never an unknown-argument error.
        gate=(monitor.management_signal_guard(guards,review,refresh)
              if guarded and hasattr(monitor,'management_signal_guard') else None)
        kwargs={'signal_guard':gate} if gate and 'signal_guard' in inspect.signature(monitor.run_lane).parameters else {}
        with ExitStack() as stack:
            for name in ('STATE','SUMMARY','GUARD','CAPITAL_POOL_USDT','ENTRY_MODE','DISCOVERY_MIN_SCORE',
                         'DISCOVERY_MIN_INDEPENDENT','STRATEGY_ID','ID_PREFIX','EVENT_PREFIX','_opportunity_backfill_requests'):
                stack.enter_context(patch.object(engine,name,getattr(engine,name)))
            stack.enter_context(patch.object(monitor,'load',side_effect=lambda p:copy.deepcopy(state) if p==path else {}))
            stack.enter_context(patch.object(engine,'atomic_json_write',side_effect=lambda p,v:writes.update({p:copy.deepcopy(v)})))
            stack.enter_context(patch.object(engine,'backfill_opportunity_history',side_effect=lambda p,n:p))
            stack.enter_context(patch.object(engine,'build_summary',return_value={}))
            stack.enter_context(patch.object(engine,'_execution_source_sha',side_effect=identity or (lambda:None)))
            if capital is not None:stack.enter_context(patch.object(engine,'capital_available',side_effect=capital))
            stack.enter_context(redirect_stdout(StringIO()))
            if guarded:monitor.revalidate_management_evidence(guards,review,refresh)
            result=monitor.run_lane(path,'SHADOW_V1' if v1 else 'SHADOW_V2',market,review,liq,supply,DATE,v1,**kwargs)
        return writes[path],result,refresh

    def revoke(self):self.store.start_draining(self.epoch)

    def expire(self):self.time=NOW+90002

    def test_identity_io_revocation_blocks_real_target_momentum_exit(self):
        saved,_,refresh=self.execute(self.fixture(),identity=self.revoke)
        self.assertEqual(saved['closed_positions'],[], 'pre-I/O signal authorized a post-revocation EXIT')
        self.assertFalse(any(e['type'].endswith('_SELL') for e in saved['events']))
        self.assertIn('ETH',refresh['failures'])
        self.assertNotIn('recovery_generation_id',saved['open_positions'][0])

    def test_v2_independent_publication_fence_revokes_real_exit_after_identity_io(self):
        from tests.test_hunter_binance_coordinated_runtime import DataCases,Market
        from research.hunter_binance_stream_store import content_hash,topic
        DataCases.setUp(self)
        market=Market(self.scratch/'state.sqlite3',stamp=NOW-1000);market.seed()
        self.store=market.store;self.epoch=market.epoch;self.names=market.names
        self.cached=consumer.read(self.names,DATE,self.store,clock=lambda:self.time)
        self.assertEqual(self.cached.generation['health_contract'],consumer.CONTROL_HEALTH_CONTRACT)
        def revoke_control_scope():
            scope=[topic('ETHUSDT','15m')];body={'fixture':'pending independent publication'}
            self.store.control.publication_intent(self.epoch,'use-point-repair',scope,content_hash(body),payload=body)
        saved,_,refresh=self.execute(self.fixture(),identity=revoke_control_scope)
        self.assertEqual(self.store.metadata()['health_phase'],'ACTIVE')
        self.assertEqual(self.store.control.status()['phase'],'ACTIVE')
        self.assertEqual(saved['closed_positions'],[]);self.assertIn('ETH',refresh['failures'])
        self.assertFalse(any(e['type'].endswith('_SELL') for e in saved['events']))

    def test_identity_io_expiry_blocks_real_thesis_exit_and_recovery(self):
        fixture=self.fixture(105);pos=fixture[0]['open_positions'][0]
        pos.update(degraded_cycles=monitor.eng.DEGRADE_CONFIRM_CYCLES-1,last_health_evidence_id='prior',
                   last_health_generation_id='prior',health_state='DEGRADED')
        saved,_,refresh=self.execute(fixture,identity=self.expire)
        self.assertEqual(saved['closed_positions'],[])
        self.assertNotIn('recovery_generation_id',saved['open_positions'][0])
        self.assertIn('ETH',refresh['failures'])

    def test_capital_wait_revocation_blocks_real_v1_add(self):
        original=monitor.eng.capital_available
        def capital(*args,**kwargs):self.revoke();return original(*args,**kwargs)
        saved,result,refresh=self.execute(self.fixture(90,weak=False),capital=capital,v1=True)
        self.assertEqual(len(saved['open_positions'][0]['tranches']),1)
        self.assertEqual(result['added'],[]);self.assertIn('ETH',refresh['failures'])
        self.assertFalse(any(e['type'].endswith('_ADD') for e in saved['events']))

    def test_second_position_rechecks_expiry_and_prior_deferred_add_is_removed(self):
        saved,result,refresh=self.execute(self.fixture(90,weak=False,two=True),identity=self.expire)
        by={p['asset']:p for p in saved['open_positions']}
        self.assertNotIn('last_health_generation_id',by['BTC'])
        self.assertEqual(result['deferred_adds'],[])
        self.assertEqual(set(refresh['failures']),{'ETH','BTC'})
        self.assertTrue(all(len(p['tranches'])==1 for p in by.values()))

    def independent_exit(self,kind):
        fixture=self.fixture();pos=fixture[0]['open_positions'][0];book=fixture[3]['snapshots']['ETH']
        if kind=='supply':fixture[4]['assets']={'ETH':{'confirmed_major_near_term_unlock':True}}
        if kind=='book':book.update(spread_bps=10000,bid_depth_2pct_usdt=1,ask_depth_2pct_usdt=1)
        if kind=='protection':
            book['raw_book_evidence']=dict(exchange='binance',market='spot',symbol='ETHUSDT',
                fetched_at=DATE.isoformat(),price_unit='USDT',quantity_unit='BASE',bids=[['120','10000']],asks=[['120.01','10000']])
            pos['protection_lifecycle']=dict(schema='hunter_profit_lifecycle_v1',state='PROTECTED',
                capital_basis=1000,peak_reference_price=130,peak_net_pnl_usdt=300,
                protected_floor_usdt=280,transitions=[])
        saved,_,refresh=self.execute(fixture,identity=self.revoke)
        self.assertIn('ETH',refresh['failures']);self.assertEqual(len(saved['closed_positions']),1)
        self.assertEqual(saved['closed_positions'][0]['exit_reason'],
                         'PROFIT_PROTECTION' if kind=='protection' else 'HARD_INVALIDATION')
        self.assertTrue(any(e['type'].endswith('_SELL') for e in saved['events']))

    def test_revoked_signal_preserves_independent_supply_exit(self):self.independent_exit('supply')

    def test_revoked_signal_preserves_independent_book_exit(self):self.independent_exit('book')

    def test_revoked_signal_preserves_independent_profit_protection(self):self.independent_exit('protection')

    def test_fresh_guard_keeps_original_decisions_and_default_none_path(self):
        fixture=self.fixture(90,weak=False)
        old=self.execute(copy.deepcopy(fixture),guarded=False)
        current=self.execute(copy.deepcopy(fixture))
        for key in ('events','decisions','open_positions','closed_positions'):
            self.assertEqual(current[0][key],old[0][key],key)
        self.assertEqual(current[1:],old[1:])

    def test_rejected_frozen_generation_cannot_be_reused_after_clock_moves_back(self):
        fixture=self.fixture();refresh={'failures':{}}
        gate=monitor.management_signal_guard([(self.cached,('ETH',))],fixture[2],refresh)
        self.expire();self.assertFalse(gate('ETH','decision'))
        self.time=NOW+1;self.assertFalse(gate('ETH','add'))
        self.assertTrue(gate('UNRELATED','decision'))

    def test_early_history_rechecks_after_history_read_before_any_write(self):
        history=Mock();temporary=Mock();history.with_suffix.return_value=temporary
        def slow_read():self.expire();return '{}'
        history.read_text.side_effect=slow_read
        with patch.object(early,'HISTORY',history):
            with self.assertRaises(ValueError):early.persist_first_early({'early':[]},self.cached.check_delivery)
        temporary.write_text.assert_not_called();temporary.replace.assert_not_called()

    def test_early_history_rechecks_after_temp_read_before_replace(self):
        history=Mock(read_text=lambda:'{}');temporary=Mock();history.with_suffix.return_value=temporary
        def revoke_on_read():self.revoke();return '{}'
        temporary.read_text.side_effect=revoke_on_read
        with patch.object(early,'HISTORY',history):
            with self.assertRaises(ValueError):early.persist_first_early({'early':[]},self.cached.check_delivery)
        temporary.write_text.assert_called_once();temporary.replace.assert_not_called()


if __name__=='__main__':unittest.main()
