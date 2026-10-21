"""Consumer delivery fences and original required sets; scratch SQLite only."""
import copy
import datetime as dt
import json
from contextlib import ExitStack,redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_stream_store import topic,roster,roster_hash,split_topic
from research.hunter_binance_health_store import Store
from scripts import hunter_binance_stream_ops as ops
from tests.test_hunter_binance_cache import NOW,DATE,scan,rows,message


class ConsumerFenceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=Store(Path(temporary.name)/'state.sqlite3');self.store.enable_health()
        self.names=roster(scan(),DATE)
        self.epoch=self.store.begin_session(self.names,'a'*64)
        self.store.begin_health(self.epoch,self.names,NOW-1000);self.store.ack(self.epoch,self.names)
        body={'symbols':[dict(symbol=s+'USDT',baseAsset=s,quoteAsset='USDT',
                              status='TRADING',isSpotTradingAllowed=True) for s in ('BTC','ETH')]}
        self.store.instrument_batch(self.epoch,self.names,body,NOW,NOW)
        for name in self.names:
            symbol,interval=split_topic(name)
            self.store.ingest_rest(self.epoch,name,rows(interval),NOW,NOW)
            self.store.ingest_ws(self.epoch,message(symbol,interval),NOW+1)
        self.time=NOW+1

    def read(self,names=None):
        return consumer.read(names or self.names,DATE,reader=self.store,clock=lambda:self.time)

    def test_draining_after_market_snapshot_rejects_read(self):
        freeze=self.store.freeze
        def revoke(*args):
            generation=freeze(*args);self.store.start_draining(self.epoch);return generation
        with patch.object(self.store,'freeze',side_effect=revoke):
            with self.assertRaisesRegex(ValueError,'REVOKED'):self.read()

    def test_epoch_switch_after_market_snapshot_rejects_read(self):
        freeze=self.store.freeze
        def replace(*args):
            generation=freeze(*args);self.store.begin_session(self.names,'b'*64);return generation
        with patch.object(self.store,'freeze',side_effect=replace):
            with self.assertRaisesRegex(ValueError,'REVOKED'):self.read()

    def test_quiet_latch_between_read_and_rows_revokes_whole_original_scope(self):
        frozen=self.read()
        other=topic('ETHUSDT','1h')
        with self.store.db() as db:
            db.execute('INSERT INTO metadata VALUES(?,?)',('quiet_reverify:'+other,'new evidence'))
        with self.assertRaises(ValueError):frozen.rows('BTCUSDT','15m')

    def test_global_stop_between_read_and_packet_is_not_delivery(self):
        frozen=self.read()
        with self.store.db() as db:db.execute("UPDATE budget SET halt='COUNTRY_403_MANUAL_REVIEW'")
        with self.assertRaises(ValueError):frozen.packet('BTCUSDT','5m')

    def test_packet_checks_again_after_capture_work(self):
        frozen=self.read();capture=consumer.capture
        def prepare(*args):
            result=capture(*args);self.store.start_draining(self.epoch);return result
        with patch.object(consumer,'capture',side_effect=prepare):
            with self.assertRaises(ValueError):frozen.packet('BTCUSDT','15m')

    def test_normal_new_ws_does_not_mutate_or_revoke_frozen_generation(self):
        frozen=self.read();before=frozen.rows('BTCUSDT','15m')
        newer=message('BTCUSDT','15m',stamp=NOW+2);newer['k']['c']='102'
        self.store.ingest_ws(self.epoch,newer,NOW+2);self.time=NOW+2
        self.assertEqual(frozen.rows('BTCUSDT','15m'),before)
        self.assertEqual(frozen.packet('BTCUSDT','15m')['source'],'OFFICIAL_BINANCE_SPOT_CACHE')

    def test_external_generation_copy_cannot_change_later_delivery(self):
        frozen=self.read();outside=frozen.generation
        outside['windows'][topic('BTCUSDT','15m')]['rows'][-1][4]='102'
        self.assertEqual(frozen.rows('BTCUSDT','15m')[-1][4],'101.0')

    def test_v2_without_bound_control_fence_is_rejected(self):
        generation=self.store.freeze(self.names,NOW+1,NOW+1)
        generation['health_contract']=consumer.CONTROL_HEALTH_CONTRACT
        with self.assertRaisesRegex(ValueError,'CONTROL_FENCE_REQUIRED'):
            consumer.Frozen(generation,checked_at=NOW+1)

    def test_bound_generation_hash_is_checked_before_delivery(self):
        freeze=self.store.freeze
        def corrupt(*args):
            generation=freeze(*args);generation['windows'][self.names[0]]['rows'][-1][4]='102';return generation
        with patch.object(self.store,'freeze',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'GENERATION_HASH'):self.read()

    def near_expiry(self):
        for name in self.names:
            symbol,interval=split_topic(name)
            self.store.ingest_ws(self.epoch,message(symbol,interval,stamp=NOW+80000),NOW+80000)
        self.time=NOW+89999
        return dt.datetime.fromtimestamp(self.time/1000,dt.timezone.utc)

    def test_final_control_wait_cannot_outlive_instrument_proof(self):
        now=self.near_expiry();finish=self.store.finish_read
        def delayed(*args):
            result=finish(*args);self.time=NOW+90001;return result
        with patch.object(self.store,'finish_read',side_effect=delayed):
            with self.assertRaisesRegex(ValueError,'EXPIRED'):
                consumer.read(self.names,now,self.store,clock=lambda:self.time)

    def test_acceptance_has_same_proof_expiry_as_actual_reader(self):
        now=self.near_expiry();finish=self.store.finish_read
        def delayed(*args):
            result=finish(*args);self.time=NOW+90001;return result
        with patch.object(self.store,'finish_read',side_effect=delayed):
            result=ops.consumer_acceptance('early',self.store,now,snapshot=scan(),
                clock=lambda:dt.datetime.fromtimestamp(self.time/1000,dt.timezone.utc))
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(result['windows'],0)
        self.assertIn('EXPIRED',result['reason']);self.assertFalse(result['publication_verified'])

    def test_acceptance_rechecks_while_preparing_receipt(self):
        finish=self.store.finish_read;calls=[]
        def revoke(*args):
            calls.append(1)
            if len(calls)==2:self.store.start_draining(self.epoch)
            return finish(*args)
        with patch.object(self.store,'finish_read',side_effect=revoke):
            result=ops.consumer_acceptance('early',self.store,DATE,snapshot=scan(),clock=lambda:DATE)
        self.assertEqual(len(calls),2);self.assertEqual(result['status'],'UNKNOWN')

    def test_early_and_monitor_do_not_require_unrequested_daily_or_btc5m(self):
        with self.store.db() as db:
            for name in self.names:
                if split_topic(name)[1] in ('1d','5m'):db.execute('UPDATE topics SET gap=1 WHERE topic=?',(name,))
        early=ops.consumer_acceptance('early',self.store,DATE,snapshot=scan(),clock=lambda:DATE)
        selection={'states':[{'open_positions':[{'asset':'ETH'}]},{'open_positions':[{'asset':'BTC'}]}],'cursor':0}
        monitor=ops.consumer_acceptance('monitor',self.store,DATE,selection=selection,clock=lambda:DATE)
        self.assertEqual((early['status'],early['windows']),('READY',4))
        self.assertEqual((monitor['status'],monitor['windows']),('READY',4))
        self.assertEqual(ops.acceptance(scan(),self.store,DATE,lambda:DATE)['status'],'UNKNOWN')


class RequiredScopeTests(unittest.TestCase):
    def test_early_keeps_all_409_pairs_and_818_topics(self):
        value=scan()
        for number in range(407):
            asset='A'+str(number)
            value['coins'][asset]={'pairs':[{'venue':'binance','base':asset,'pair':asset+'USDT'}]}
        scope=ops.required_scope('early',DATE,snapshot=value)
        self.assertEqual(len(scope['topics']),818)
        self.assertEqual(scope['expected_roster'],roster_hash(roster(value,DATE)))
        self.assertEqual(len(roster(value,DATE)),1228)

    def test_monitor_reuses_original_selection_without_discovery_age_gate(self):
        from research.hunter_position_monitor import management_selection,C
        states=[{'open_positions':[{'asset':'A'+str(n)} for n in range(100)]},
                {'open_positions':[{'asset':'ETH'},{'asset':'ONLYBYBIT','execution_venue':'BYBIT_SPOT'}]}]
        selection={'states':states,'cursor':7};before=copy.deepcopy(selection)
        expected,cursor=management_selection(states,7,C['MONITOR_EVIDENCE_BATCH'])
        scope=ops.required_scope('monitor',DATE,selection=selection)
        self.assertEqual(scope['selected_assets'],expected);self.assertEqual(scope['next_cursor'],cursor)
        self.assertEqual(scope['topics'],sorted(topic(a+'USDT',i) for a in set(expected)|{'BTC'} for i in ('15m','1h')))
        self.assertEqual(selection,before)

    def test_monitor_explicit_smaller_symbol_list_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError,'ORIGINAL_SELECTION_REQUIRED'):
            ops.required_scope('monitor',DATE,selection={'symbols':['BTCUSDT']})

    def test_daily_reuses_rotation_and_keeps_unavailable_venue_explicit(self):
        value=scan();value['coins']['ONLYBYBIT']={'pairs':[{'venue':'bybit','pair':'ONLYBYBITUSDT'}]}
        value['as_of_utc']=(DATE-dt.timedelta(days=1)).isoformat()
        selection={'previous':{'rotation_cursor':0},'early':{'scan_generation_id':'fixture','early':[{'base':'ETH'}]}}
        result=ops.required_scope('daily',DATE,snapshot=value,selection=selection)
        self.assertEqual(result['topics'],[topic('BTCUSDT','1d'),topic('ETHUSDT','1d')])
        self.assertEqual(result['without_binance_pair'],['ONLYBYBIT'])
        self.assertEqual(set(result['selected_assets']),set(value['coins']))

    def test_daily_mixed_generation_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError,'ORIGINAL_SELECTION_REQUIRED'):
            ops.required_scope('daily',DATE,snapshot=scan(),selection={'previous':{},'early':{'scan_generation_id':'older'}})

    def test_btc_5m_is_an_independent_required_scope(self):
        result=ops.required_scope('btc',DATE)
        self.assertEqual(result['topics'],[topic('BTCUSDT','5m')])
        self.assertEqual(result['expected_topics'],1)


class ConsumerUseBoundaryTests(unittest.TestCase):
    def early_rejection(self,fail_on):
        from research import hunter_early_signals as early
        guard=Mock();guard.check_delivery.side_effect=[None]*(fail_on-1)+[ValueError('REVOKED')]
        output=Mock();history=Mock();build=Mock(return_value={'early_count':0,'early':[]})
        with ExitStack() as stack:
            stack.enter_context(patch.object(early,'SCAN',Mock(read_text=lambda:json.dumps(scan()))))
            stack.enter_context(patch.object(early,'OUT',output))
            stack.enter_context(patch.object(consumer,'signal_inputs',return_value=guard))
            stack.enter_context(patch.object(early,'rolling',return_value={}))
            stack.enter_context(patch.object(early,'micro',return_value={}))
            stack.enter_context(patch.object(early,'build',build))
            stack.enter_context(patch.object(early,'persist_first_early',history))
            with self.assertRaisesRegex(ValueError,'REVOKED'):early.main()
        output.write_text.assert_not_called();history.assert_not_called()
        self.assertEqual(build.call_count,fail_on-1)

    def test_early_revoked_after_market_calculations_never_builds_or_writes(self):
        self.early_rejection(1)

    def test_early_revoked_after_report_build_never_writes_or_persists_history(self):
        self.early_rejection(2)

    def refresh(self,guard):
        from research import hunter_position_monitor as monitor
        states=[{'open_positions':[{'asset':'ETH'}]},{'open_positions':[{'asset':'BTC'}]}]
        guards=[]
        with ExitStack() as stack:
            stack.enter_context(patch.object(monitor,'load',return_value={'evidence_refresh_cursor':0}))
            stack.enter_context(patch.object(consumer,'management_inputs',return_value=guard))
            stack.enter_context(patch.object(monitor.signals,'rolling',return_value={}))
            stack.enter_context(patch.object(monitor.signals,'micro',return_value={}))
            stack.enter_context(patch.object(monitor.signals,'score_row',return_value={'score':50}))
            fetch=stack.enter_context(patch.object(monitor.books,'live_fetch',return_value={'fixture':'book'}))
            stack.enter_context(patch.object(monitor.books,'measure',return_value={'raw_book_evidence':{'fixture':'book'}}))
            stack.enter_context(patch.object(monitor.books,'estimate',return_value={}))
            stack.enter_context(redirect_stdout(StringIO()))
            result=monitor.refresh_management_evidence(states,{}, {'candidates':[]},{},DATE,guards)
        return result,guards,fetch

    def test_monitor_revoked_after_market_calculations_invalidates_original_batch(self):
        guard=Mock();guard.check_delivery.side_effect=ValueError('REVOKED')
        (review,_,refresh),guards,fetch=self.refresh(guard)
        self.assertEqual(set(refresh['failures']),{'BTC','ETH'})
        self.assertTrue(all(row['signal_evidence']=={} for row in review['candidates']))
        self.assertEqual(guards,[]);fetch.assert_not_called()

    def test_monitor_known_legacy_reader_without_delivery_method_keeps_v1_api(self):
        from research import hunter_position_monitor as monitor
        class LegacyFrozen:
            def __init__(self,contract):
                self.generation=dict(schema='binance_stream_generation_v1',status='READY',health_contract=contract)
        for contract in (None,'binance_health_v1'):
            with self.subTest(contract=contract),patch.object(consumer,'Frozen',LegacyFrozen):
                guard=LegacyFrozen(contract)
                (review,_,refresh),guards,_=self.refresh(guard)
                self.assertFalse(refresh['failures']);self.assertEqual(len(guards),1)
                monitor.revalidate_management_evidence(guards,review,refresh)
                self.assertFalse(refresh['failures'])

    def test_monitor_missing_delivery_method_never_downgrades_control_generation(self):
        from research import hunter_position_monitor as monitor
        class MissingFence:
            def __init__(self,**contracts):
                self.generation=dict(schema='binance_stream_generation_v1',status='READY',**contracts)
        for contracts in ({'health_contract':consumer.CONTROL_HEALTH_CONTRACT},
                          {'health_contract':'binance_health_v1','control_contract':consumer.CONTROL_CONTRACT}):
            with self.subTest(contracts=contracts),patch.object(consumer,'Frozen',MissingFence):
                guard=MissingFence(**contracts)
                (review,_,refresh),guards,fetch=self.refresh(guard)
                self.assertEqual(set(refresh['failures']),{'BTC','ETH'})
                self.assertTrue(all(row['signal_evidence']=={} for row in review['candidates']))
                self.assertEqual(guards,[]);fetch.assert_not_called()
                with self.assertRaisesRegex(ValueError,'FENCE_REQUIRED'):monitor.check_cache_delivery(guard)

    def test_monitor_use_gate_clears_only_signal_evidence_and_never_serializes_guard(self):
        from research import hunter_position_monitor as monitor
        guard=Mock();guard.check_delivery.return_value=NOW+1
        (review,liquidity,refresh),guards,_=self.refresh(guard)
        books=copy.deepcopy(liquidity)
        self.assertEqual(len(guards),1);self.assertFalse(refresh['failures'])
        guard.check_delivery.side_effect=ValueError('REVOKED')
        monitor.revalidate_management_evidence(guards,review,refresh)
        self.assertEqual(set(refresh['failures']),set(refresh['attempted']))
        self.assertTrue(all(row['signal_evidence']=={} for row in review['candidates']))
        self.assertEqual(liquidity,books)
        json.dumps(refresh);json.dumps(review)  # Only ordinary audit data reaches the writer.

    def test_monitor_main_revalidates_between_v1_and_v2_without_running_lanes(self):
        from research import hunter_position_monitor as monitor
        engine=monitor.eng
        states=[{'open_positions':[{'asset':'BTC','tranches':[{'price':100,'notional_usdt':1000}]}]},
                {'open_positions':[{'asset':'ETH','tranches':[{'price':100,'notional_usdt':1000}]}]}]
        docs={monitor.V1:states[0],monitor.V2:states[1]};revoked=[False];lane_evidence=[];writes={}
        guard=Mock()
        def check():
            if revoked[0]:raise ValueError('REVOKED_AFTER_V1')
        guard.check_delivery.side_effect=check
        def refresh(states,market,review,liquidity,now,guards):
            guards.append((guard,('BTC','ETH')))
            return ({'candidates':[{'asset':a,'signal_evidence':{'fresh':True}} for a in ('BTC','ETH')]},
                    {},{'generation_id':'fixture','evidence_refresh_cursor':0,'attempted':['BTC','ETH'],'failures':{}})
        def lane(path,label,market,review,*args):
            lane_evidence.append(copy.deepcopy(review));revoked[0]=True
            return {'lane':label,'open':1}
        with ExitStack() as stack:
            stack.enter_context(patch.object(monitor,'load',side_effect=lambda path:copy.deepcopy(docs.get(path,{}))))
            stack.enter_context(patch.object(monitor,'batch_market',return_value={a:{'reference_price':100} for a in ('BTC','ETH')}))
            stack.enter_context(patch.object(monitor,'crypto_exclusions',return_value=set()))
            stack.enter_context(patch.object(monitor,'refresh_management_evidence',side_effect=refresh))
            stack.enter_context(patch('research.hunter_bybit_management.collect',return_value=({},{})))
            stack.enter_context(patch.object(engine.tail,'collect_systemic_evidence',return_value={}))
            stack.enter_context(patch.object(monitor.leading,'collect_evidence',return_value={}))
            stack.enter_context(patch.object(monitor.leading,'update_state',return_value=({'level':'NORMAL'},False)))
            stack.enter_context(patch.object(monitor.leading,'update_history',return_value=[]))
            stack.enter_context(patch.object(engine.tail,'tail_budget_snapshot',return_value={}))
            stack.enter_context(patch.object(monitor.books,'live_fetch',return_value={}))
            stack.enter_context(patch.object(monitor.books,'measure',return_value={'raw_book_evidence':{}}))
            stack.enter_context(patch('research.hunter_fast_reconciliation.observe',return_value={}))
            stack.enter_context(patch('subprocess.check_output',return_value='b'*40))
            stack.enter_context(patch.object(engine,'atomic_json_write',side_effect=lambda path,value:writes.update({path:copy.deepcopy(value)})))
            stack.enter_context(patch.object(monitor,'run_lane',side_effect=lane))
            stack.enter_context(redirect_stdout(StringIO()))
            monitor.main()
        self.assertEqual(guard.check_delivery.call_count,2)
        self.assertTrue(all(row['signal_evidence'] for row in lane_evidence[0]['candidates']))
        self.assertTrue(all(row['signal_evidence']=={} for row in lane_evidence[1]['candidates']))
        self.assertEqual(set(writes[monitor.OUT]['evidence_refresh']['failures']),{'BTC','ETH'})
        json.dumps(writes[monitor.OUT])


class HealthCheckerFenceTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the portable scratch setup, never the Linux HTTP cases.
        from tests.test_hunter_binance_coordinated_runtime import DataCases,Market,MIDNIGHT
        from research import hunter_binance_health as health
        from research import hunter_binance_stream as stream
        from scripts import hunter_binance_health_check as checker
        DataCases.setUp(self)
        self.health=health;self.stream=stream;self.checker=checker
        self.market=Market(self.scratch/'state.sqlite3',stamp=MIDNIGHT+100000)
        self.time=self.market.seed()
        self.jobs=type('Work',(),dict(active={},seeded=set(self.market.names),completed=4,failures={},
            diagnostics=lambda _,now:dict(work_progress={},exhausted_work=[],oldest_active_ms=0)))()
        self.sample()

    def sample(self):
        self.market.protocol.alive(0,self.time,'MATCHING_PONG')
        with patch.object(self.stream,'emit'):
            return self.health.sample(self.market.store,self.market.protocol,self.market.names,
                                      self.jobs,self.time,0)

    def check(self,store=None,now=None):
        from tests.test_hunter_binance_coordinated_runtime import HEAD,MANIFEST
        return self.checker.read_sample(store or self.market.store,self.market.names,MANIFEST,
                                       HEAD,self.time if now is None else now,clock=lambda:self.time)

    def pending(self):
        from research.hunter_binance_stream_store import content_hash
        name=topic('ETHUSDT','15m')
        body=dict(kind='repair',names=[name],body=rows('15m',self.time),
                  requested=self.time,received=self.time,job_key='checker-repair')
        return self.market.store.control.publication_intent(self.market.epoch,'checker-repair',
            [name],content_hash(body),payload=body)

    def test_readonly_v2_checker_binds_reader_and_keeps_publication_unverified(self):
        from tests.test_hunter_binance_coordinated_runtime import HEAD
        reader=Store.reader(self.market.path)
        self.assertIsNone(getattr(reader,'control',None))
        result=self.check(reader)
        self.assertEqual(result.get('contract'),consumer.CONTROL_HEALTH_CONTRACT,result)
        self.assertEqual((result['service_status'],result['data_status']),('HEALTHY','READY'),result)
        self.assertTrue(result['health_verified']);self.assertIsNotNone(reader.control)
        self.assertFalse(result['collector_accepted']);self.assertFalse(result['publication_verified'])
        now=dt.datetime.fromtimestamp(self.time/1000,dt.timezone.utc)
        receipt=ops.consumer_acceptance('btc',reader,now,clock=lambda:now)
        self.assertEqual(receipt['status'],'READY',receipt);self.assertEqual(receipt['source_head_sha'],HEAD)

    def test_pending_scope_rejects_data_without_rejecting_connection_health(self):
        self.pending();result=self.check()
        self.assertTrue(result.get('health_verified'),result)
        self.assertEqual((result['service_status'],result['data_status']),('DEGRADED','UNKNOWN'))

    def test_scope_intent_after_freeze_is_rejected_by_delivery_fence(self):
        freeze=self.market.store.freeze
        def pending_after_snapshot(*args):
            generation=freeze(*args);self.pending();return generation
        with patch.object(self.market.store,'freeze',side_effect=pending_after_snapshot):result=self.check()
        self.assertTrue(result.get('health_verified'),result)
        self.assertEqual((result['service_status'],result['data_status']),('DEGRADED','UNKNOWN'))

    def test_scope_revocation_at_last_delivery_never_reports_ready(self):
        finish=self.market.store.finish_read
        def pending_before_delivery(*args):self.pending();return finish(*args)
        with patch.object(self.market.store,'finish_read',side_effect=pending_before_delivery):result=self.check()
        self.assertTrue(result.get('health_verified'),result);self.assertEqual(result['data_status'],'UNKNOWN')

    def test_control_stop_after_snapshot_cannot_verify_healthy_market_metadata(self):
        freeze=self.market.store.freeze
        def stop_after_snapshot(*args):
            generation=freeze(*args);self.market.store.control.end_epoch(self.market.epoch,'fixture');return generation
        with patch.object(self.market.store,'freeze',side_effect=stop_after_snapshot):result=self.check()
        self.assertFalse(result.get('health_verified',False));self.assertEqual(result['service_status'],'UNKNOWN')
        self.assertEqual(result['reason'],'HEALTH_CONTROL_CHANGED_OR_STOPPED')

    def test_control_stop_at_last_delivery_cannot_accumulate_healthy_time(self):
        finish=self.market.store.finish_read
        def stop_before_delivery(*args):
            self.market.store.control.end_epoch(self.market.epoch,'fixture');return finish(*args)
        with patch.object(self.market.store,'finish_read',side_effect=stop_before_delivery):result=self.check()
        self.assertFalse(result.get('health_verified',False));self.assertEqual(result['service_status'],'UNKNOWN')
        self.assertEqual(result['reason'],'HEALTH_CONTROL_CHANGED_OR_STOPPED')

    def test_control_status_binding_cannot_be_replaced_by_market_binding(self):
        original=self.market.store.control.status
        for key in ('epoch','phase','source_head','manifest_sha256','roster_hash'):
            with self.subTest(field=key),patch.object(self.market.store.control,'status',
                    side_effect=lambda *a,_key=key,**k:dict(original(*a,**k),**{_key:'mismatched'})):
                result=self.check()
            self.assertFalse(result.get('health_verified',False));self.assertEqual(result['reason'],'HEALTH_CONTROL_BINDING_MISMATCH')

    def test_final_control_status_wait_is_in_health_completion_time(self):
        original=self.market.store.control.status;started=self.time;calls=[]
        def delayed(*args,**kwargs):
            value=original(*args,**kwargs);calls.append(1)
            if len(calls)>=2:self.time=started+30001
            return value
        with patch.object(self.market.store.control,'status',side_effect=delayed):result=self.check(now=started)
        self.assertFalse(result.get('health_verified',False));self.assertEqual(result['reason'],'HEALTH_READ_CHANGED_OR_EXPIRED')

    def test_control_wait_data_expiry_does_not_claim_broken_connection(self):
        start=self.market.started
        self.market.frames(self.market.names,start+80000)
        self.time=start+89999;self.sample();observed=self.time
        original=self.market.store.control.status;calls=[]
        def delayed(*args,**kwargs):
            value=original(*args,**kwargs);calls.append(1)
            if len(calls)>=2:self.time=start+90002
            return value
        with patch.object(self.market.store.control,'status',side_effect=delayed):result=self.check(now=observed)
        self.assertTrue(result.get('health_verified'),result)
        self.assertEqual((result['service_status'],result['data_status']),('DEGRADED','UNKNOWN'))
        self.assertEqual(result['health_verified_at'],start+90002)

    def test_v2_window_accepts_degraded_health_without_claiming_data_ready(self):
        self.pending();result=self.check();window=self.checker.Window()
        for elapsed in range(0,301,10):
            value=window.observe(dict(result,observed_at=self.time+elapsed*1000),elapsed)
        self.assertTrue(value['collector_accepted']);self.assertEqual(value['data_status'],'UNKNOWN')
        self.assertFalse(value['publication_verified'])
        changed=window.observe(dict(result,contract='binance_health_v1',observed_at=self.time+310000),310)
        self.assertFalse(changed['collector_accepted']);self.assertEqual(changed['healthy_seconds'],0)


if __name__=='__main__':unittest.main()
