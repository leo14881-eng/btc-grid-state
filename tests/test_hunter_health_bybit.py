"""Offline Bybit health contract: real SQLite, fake clocks/market transport."""
import contextlib
import copy
from concurrent.futures import Future
import datetime as dt
import io
import json
from pathlib import Path
import queue
import tempfile
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research import hunter_bybit_health as health
from research import hunter_bybit_health_store as storage
from research import hunter_bybit_stream_consumer as consumer
from research.hunter_bybit_stream_store import Store as LegacyStore, LIMITS, topic, split_topic
from test_hunter_stream import NOW, rest, message


def instruments(names, end=NOW):
    return dict(retCode=0,time=end,result=dict(category='spot',list=[
        dict(symbol=symbol,baseCoin=symbol[:-4],quoteCoin='USDT',status='Trading')
        for symbol in sorted({split_topic(name)[0] for name in names})]))


class HoldingPool:
    def __init__(self): self.calls=[]
    def submit(self, function, *args):
        future=Future(); self.calls.append((future,function,args)); return future


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=storage.Store(Path(self.temp.name)/'state.sqlite3'); self.store.enable_health()
        self.names=consumer.topics_for(['GRASS'])
        self.protocol=health.Protocol(self.store,self.names,'a'*64,NOW)
        self.epoch=self.protocol.epoch; self.name=topic('GRASSUSDT','15')
        self.ack()
        self.output=io.StringIO(); self.silence=contextlib.redirect_stdout(self.output); self.silence.__enter__()
        self.addCleanup(self.silence.__exit__,None,None,None)

    def ack(self):
        for request in self.protocol.requests:
            self.protocol.mark_sent(request,NOW)
            self.protocol.control(dict(op='subscribe',req_id=request['req_id'],success=True),NOW)

    def ingest(self, value, received=None):
        return self.store.ingest_accounted(self.epoch,[(value,value['ts'] if received is None else received)])

    def seed(self, end=NOW):
        for name in self.names:
            symbol,interval=split_topic(name)
            self.store.ingest_rest(self.epoch,name,rest(symbol,interval,end),end,end)
            self.ingest(message(symbol,interval,end+1))
        self.store.instrument_batch(self.epoch,self.names,instruments(self.names,end),end,end)
        self.store.connection_proofs(self.epoch,end+1,'MATCHING_PONG',end+1)

    def frozen(self, end=NOW+2): return self.store.freeze(self.names,end,end)

    def jobs(self, now=NOW+2, cancelled=lambda:False, client_factory=None):
        kwargs={} if client_factory is None else dict(client_factory=client_factory)
        return health.Jobs(self.store,self.protocol,self.names,HoldingPool(),lambda:now,cancelled,**kwargs)

    def test_ready_preserves_full_scope_and_sources(self):
        self.seed(); result=self.frozen()
        self.assertEqual(result['status'],'READY',result)
        self.assertEqual(set(result['windows']),set(self.names)); self.assertTrue(storage.validate_generation(result,NOW+2))
        for name,window in result['windows'].items():
            self.assertEqual(len(window['rows']),LIMITS[split_topic(name)[1]])
            self.assertEqual(window['provenance'][-1]['source'],storage.WS_SOURCE)
            self.assertEqual(window['provenance'][-1]['confirmation'],'WS_EXPLICIT')
            self.assertEqual(window['validity']['kline']['source'],storage.CACHE_SOURCE)
            self.assertEqual(window['validity']['instrument']['trading_status'],'Trading')

    def test_legacy_reader_rejects_health_marker(self):
        self.seed(); old=LegacyStore.reader(self.store.path)
        self.assertEqual(old.freeze(self.names,NOW+2,NOW+2)['status'],'UNKNOWN')
        self.assertEqual(old.window('GRASSUSDT','15',NOW+2,NOW+2)['status'],'UNKNOWN')
        self.assertEqual(self.store.metadata()['connected'],storage.CONTRACT)

    def test_legacy_generation_still_reads_without_health_opt_in(self):
        old=LegacyStore(Path(self.temp.name)/'legacy.sqlite3'); epoch=old.begin_session(self.names,'a'*64); old.ack(epoch,self.names)
        for name in self.names:
            symbol,interval=split_topic(name); old.ingest_rest(epoch,name,rest(symbol,interval),NOW,NOW)
            old.ingest_ws(epoch,message(symbol,interval,NOW+1),NOW+1)
        reader=storage.Store.reader(old.path); generation=reader.freeze(self.names,NOW+2,NOW+2)
        self.assertEqual(generation['status'],'READY'); self.assertNotIn('health_contract',generation)
        self.assertTrue(reader.completed_generation(generation,NOW+2))
        reader.enable_health(); reader.begin_health(self.names,'a'*64,NOW+3)
        self.assertFalse(reader.completed_generation(generation,NOW+3))

    def test_epoch_switch_invalidates_frozen_proofs_and_old_writes(self):
        self.seed(); generation=self.frozen(); new=self.store.begin_health(self.names,'a'*64,NOW+3)
        self.assertNotEqual(new,self.epoch); self.assertFalse(self.store.completed_generation(generation,NOW+3))
        for operation in (lambda:self.store.ingest_rest(self.epoch,self.name,rest('GRASSUSDT'),NOW,NOW),
                          lambda:self.store.save_proof(generation['windows'][self.name]['validity']['instrument']),
                          lambda:self.store.store_health(dict(epoch=self.epoch,service_status='STOPPED'))):
            with self.assertRaises(ValueError): operation()

    def test_health_metadata_cannot_authorize_a_legacy_new_epoch(self):
        self.seed(); old=LegacyStore.reader(self.store.path); old.begin_session(self.names,'a'*64)
        self.assertEqual(self.frozen()['status'],'UNKNOWN')

    def test_draining_revokes_reader_but_retains_receipts(self):
        self.seed(); generation=self.frozen(); self.store.start_draining(self.epoch)
        self.assertEqual(self.frozen()['status'],'UNKNOWN'); self.assertFalse(self.store.completed_generation(generation,NOW+3))
        self.store.unprocessed(self.epoch,[(message('GRASSUSDT',ts=NOW+3),NOW+3)],'TEST_DRAIN')
        self.assertEqual(self.store.counts(self.epoch)[self.name]['unprocessed_recorded'],1)
        with self.assertRaises(ValueError): self.ingest(message('GRASSUSDT',ts=NOW+4))
        with self.assertRaises(ValueError): self.store.ingest_rest(self.epoch,self.name,rest('GRASSUSDT'),NOW,NOW)
        self.store.disconnect(self.epoch); self.assertEqual(self.store.metadata()['connected'],'0')

    def test_read_fence_includes_final_sqlite_wait(self):
        self.seed(); generation=self.frozen()
        self.assertFalse(self.store.completed_generation(generation,NOW+2,clock=lambda:NOW+90002))
        real=self.store.db; current=[NOW+2]
        @contextlib.contextmanager
        def slow(*args,**kwargs):
            with real(*args,**kwargs) as db: yield db
            current[0]=NOW+90002
        with patch.object(self.store,'db',slow):
            self.assertFalse(self.store.completed_generation(generation,NOW+2,clock=lambda:current[0]))

    def test_actual_consumer_uses_health_fence(self):
        self.seed(); now=dt.datetime.fromtimestamp((NOW+2)/1000,dt.timezone.utc)
        signals,failures,generation=consumer.collect([dict(base='GRASS',pair='GRASSUSDT')],now,self.store,lambda:NOW+2)
        self.assertEqual(set(signals),{'GRASS'}); self.assertFalse(failures); self.assertEqual(generation['status'],'READY')
        real=self.store.freeze
        def transition(*args):
            generation=real(*args); self.store.start_draining(self.epoch); return generation
        with patch.object(self.store,'freeze',transition):
            signals,failures,generation=consumer.collect([dict(base='GRASS',pair='GRASSUSDT')],now,self.store,lambda:NOW+2)
        self.assertFalse(signals); self.assertEqual(set(failures),{'GRASSUSDT','BTCUSDT'})
        self.assertEqual(generation['reason'],'BYBIT_HEALTH_READ_FENCE_FAILED')

    def test_pong_and_rest_cannot_make_quiet_topic_ready(self):
        self.seed(); later=NOW+90002
        before={s['topic']:(s['ws_rx'],s['ws_ts']) for s in self.store.states()}
        for name in self.names:
            symbol,interval=split_topic(name); self.store.ingest_rest(self.epoch,name,rest(symbol,interval,later),later,later)
        self.store.instrument_batch(self.epoch,self.names,instruments(self.names,later),later,later)
        self.protocol.alive(later,'MATCHING_PONG')
        result=health.sample(self.store,self.protocol,self.names,self.jobs(later),later,0,later)
        self.assertEqual(result['service_status'],'DEGRADED'); self.assertEqual(result['data_status'],'UNKNOWN')
        self.assertEqual(before,{s['topic']:(s['ws_rx'],s['ws_ts']) for s in self.store.states()})

    def test_rest_newer_than_ws_keeps_original_cache_semantics(self):
        self.seed()
        for name in self.names:
            symbol,interval=split_topic(name); self.store.ingest_rest(self.epoch,name,rest(symbol,interval,NOW+10),NOW+10,NOW+10)
            self.ingest(message(symbol,interval,NOW+5),NOW+11)
        result=self.frozen(NOW+12); self.assertEqual(result['status'],'READY',result)
        for window in result['windows'].values():
            self.assertEqual(window['provenance'][-1]['source'],storage.REST_SOURCE)
            self.assertEqual(window['validity']['kline']['current_bar_source'],storage.REST_SOURCE)
            self.assertEqual(window['validity']['kline']['ws_event_time'],NOW+5)

    def test_quiet_match_is_audited_without_refreshing_ws(self):
        self.seed(); before=self.store.states()
        value=self.store.verify_quiet(self.epoch,self.name,rest('GRASSUSDT',end=NOW+2),NOW+2,NOW+2)
        self.assertEqual(value['result'],'VALID'); self.assertEqual(value['source'],storage.REST_SOURCE)
        self.assertEqual(value['comparison'],'SAME_CLOSED_NATIVE_BAR'); self.assertEqual(before,self.store.states())

    def test_mismatch_repair_requires_a_later_matching_verification(self):
        self.seed(); changed=rest('GRASSUSDT',end=NOW+2); changed['result']['list'][1][4]='102'
        value=self.store.verify_quiet(self.epoch,self.name,changed,NOW+2,NOW+2)
        self.assertEqual(value['result'],'UNKNOWN'); self.assertEqual(self.frozen(NOW+3)['reason'],'BYBIT_QUIET_REVERIFICATION_REQUIRED')
        self.ingest(message('GRASSUSDT',ts=NOW+4))
        self.assertEqual(self.frozen(NOW+4)['reason'],'BYBIT_QUIET_REVERIFICATION_REQUIRED')
        changed['time']=NOW+5
        self.assertEqual(self.store.verify_quiet(self.epoch,self.name,changed,NOW+5,NOW+5)['result'],'VALID')
        self.assertEqual(self.frozen(NOW+5)['status'],'READY')

    def test_quiet_boundary_crossing_cannot_clear_obligation(self):
        edge=(NOW//900000+1)*900000; self.store.begin_health(self.names,'a'*64,NOW)
        self.protocol=health.Protocol(self.store,self.names,'a'*64,NOW); self.epoch=self.protocol.epoch; self.ack()
        self.seed(edge-2)
        changed=rest('GRASSUSDT',end=edge-1); changed['result']['list'][1][4]='102'
        self.store.verify_quiet(self.epoch,self.name,changed,edge-1,edge-1)
        changed['time']=edge
        value=self.store.verify_quiet(self.epoch,self.name,changed,edge-1,edge)
        self.assertEqual(value['result'],'UNKNOWN'); self.assertEqual(value['reason'],'VERIFICATION_EXPIRED_OR_NATIVE_BOUNDARY')
        with self.store.db(readonly=True) as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM metadata WHERE kind=?',('bybit_quiet_reverify:'+self.name,)).fetchone())

    def test_invalid_instrument_or_stale_receipt_blocks_ready(self):
        self.seed(); body=instruments(self.names); body['result']['list'][1]['status']='Settling'
        self.store.instrument_batch(self.epoch,self.names,body,NOW,NOW)
        self.assertEqual(self.frozen()['status'],'UNKNOWN')
        body['result']['list'][1]['status']='Trading'; body['result']['list'][1]['symbolType']='xstocks'
        self.store.instrument_batch(self.epoch,self.names,body,NOW,NOW)
        self.assertEqual(self.frozen()['status'],'UNKNOWN')

    def test_directory_rejects_wrong_identity_duplicates_missing_and_pagination(self):
        for change in ('quote','base','duplicate','missing','page','stale','category'):
            body=instruments(self.names)
            if change=='quote': body['result']['list'][0]['quoteCoin']='USD'
            if change=='base': body['result']['list'][0]['baseCoin']='OTHER'
            if change=='duplicate': body['result']['list'].append(body['result']['list'][0])
            if change=='missing': body['result']['list'].pop()
            if change=='page': body['result']['nextPageCursor']='partial'
            if change=='stale': body['time']-=90001
            if change=='category': body['result']['category']='linear'
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.store.instrument_batch(self.epoch,self.names,body,NOW,NOW)

    def test_multi_bar_frame_is_one_commit_and_duplicate(self):
        self.seed(); value=message('GRASSUSDT',ts=NOW+2); step=900000
        previous=message('GRASSUSDT',ts=NOW+2,start=NOW//step*step-step,confirm=True)['data'][0]
        value['data'].insert(0,previous)
        before=self.store.counts(self.epoch)[self.name]['committed']
        self.assertEqual(self.ingest(value),['committed']); self.assertEqual(self.ingest(value,NOW+100),['duplicate'])
        self.assertEqual(self.store.counts(self.epoch)[self.name]['committed'],before+1)
        self.assertEqual(next(s for s in self.store.states() if s['topic']==self.name)['ws_rx'],NOW+2)

    def test_same_ts_conflict_and_duplicate_native_open_are_invalid(self):
        self.seed(); value=message('GRASSUSDT',ts=NOW+2); self.ingest(value)
        value['data'][0]['close']='102'; self.assertEqual(self.ingest(value),['invalid'])
        value=message('GRASSUSDT',ts=NOW+3); value['data'].append(copy.deepcopy(value['data'][0]))
        value['data'][1]['close']='102'; self.assertEqual(self.ingest(value),['invalid'])
        state=next(s for s in self.store.states() if s['topic']==self.name)
        self.assertEqual(state['ws_ts'],NOW+2); self.assertEqual(state['gap'],1)

    def test_new_event_ts_with_same_prices_is_new_observation(self):
        self.seed(); self.assertEqual(self.ingest(message('GRASSUSDT',ts=NOW+2)),['committed'])
        self.assertEqual(self.ingest(message('GRASSUSDT',ts=NOW+3)),['committed'])
        self.assertEqual(next(s for s in self.store.states() if s['topic']==self.name)['ws_ts'],NOW+3)

    def test_unknown_second_bar_receipt_cannot_be_repaired_by_one_open(self):
        self.seed(); value=message('GRASSUSDT',ts=NOW+2); value['data'].append(None)
        self.assertEqual(self.ingest(value),['invalid'])
        self.store.ingest_rest(self.epoch,self.name,rest('GRASSUSDT',end=NOW+3),NOW+3,NOW+3)
        self.assertEqual(self.frozen(NOW+3)['reason'],'BYBIT_UNRECONCILED_RECEIPT')

    def test_multibar_receipt_requires_exact_all_opens_and_later_request(self):
        self.seed(); value=message('GRASSUSDT',ts=NOW+10); step=900000
        value['data'].append(message('GRASSUSDT',ts=NOW+10,start=(NOW//step-1)*step,confirm=True)['data'][0])
        self.store.unprocessed(self.epoch,[(value,NOW+10)],'TEST')
        self.store.ingest_rest(self.epoch,self.name,rest('GRASSUSDT',end=NOW+5),NOW+5,NOW+11)
        self.assertEqual(self.frozen(NOW+11)['reason'],'BYBIT_UNRECONCILED_RECEIPT')
        self.store.ingest_rest(self.epoch,self.name,rest('GRASSUSDT',end=NOW+12),NOW+12,NOW+12)
        self.assertEqual(self.frozen(NOW+12)['status'],'READY')

    def test_bad_confirm_and_last_trade_time_do_not_supply_source_time(self):
        self.seed(); value=message('GRASSUSDT',ts=NOW+3); value['data'][0]['confirm']=1
        self.assertEqual(self.ingest(value),['invalid'])
        value=message('GRASSUSDT',ts=NOW+4); value['data'][0]['timestamp']=NOW-10000
        self.assertEqual(self.ingest(value),['committed'])
        with self.store.db(readonly=True) as db:
            row=db.execute('SELECT source_time FROM bars WHERE topic=? ORDER BY start DESC LIMIT 1',(self.name,)).fetchone()
        self.assertEqual(row[0],NOW+4)

    def test_proof_rejects_exchange_source_and_identity_changes(self):
        self.seed(); generation=self.frozen()
        for field,value in (('exchange','BINANCE'),('market','linear'),('source','OFFICIAL_BINANCE_SPOT_WS'),('epoch','b'*32)):
            corrupted=copy.deepcopy(generation); corrupted['windows'][self.name]['validity']['kline'][field]=value
            self.assertFalse(storage.validate_generation(corrupted,NOW+2))

    def test_ack_requires_current_sent_request_and_times_out(self):
        new=health.Protocol(self.store,self.names,'a'*64,NOW)
        with self.assertRaises(ValueError): new.control(dict(op='subscribe',req_id=new.requests[0]['req_id'],success=True),NOW)
        with self.assertRaisesRegex(TimeoutError,'ACK_TIMEOUT'): new.tick(NOW+30001)

    def test_application_pong_does_not_prove_health(self):
        self.protocol.control(dict(op='ping',success=True,ret_msg='pong'),NOW+1000)
        self.assertEqual(self.protocol.observed_at,0)

    def test_sample_health_requires_all_ack_and_progress(self):
        self.seed(); self.protocol.alive(NOW+2,'MATCHING_PONG'); jobs=self.jobs()
        self.assertEqual(health.sample(self.store,self.protocol,self.names,jobs,NOW+2,0,NOW+2)['service_status'],'HEALTHY')
        with self.store.db() as db: db.execute('UPDATE topics SET ack=0 WHERE topic=?',(self.name,))
        self.assertEqual(health.sample(self.store,self.protocol,self.names,jobs,NOW+2,0,NOW+2)['service_status'],'INITIALIZING')
        self.store.ack(self.epoch,self.names)
        self.assertEqual(health.sample(self.store,self.protocol,self.names,jobs,NOW+40000,1,NOW)['service_status'],'INITIALIZING')

    def test_health_sample_completion_cannot_restore_budget_with_expired_proof(self):
        self.seed(); self.protocol.alive(NOW+2,'MATCHING_PONG'); jobs=self.jobs(); now=[NOW+2]
        real=self.store.freeze
        def delayed(*args):
            result=real(*args); now[0]=NOW+90003; return result
        with patch.object(self.store,'freeze',delayed):
            result=health.sample(self.store,self.protocol,self.names,jobs,NOW+2,0,NOW+2,clock=lambda:now[0])
        self.assertEqual(result['service_status'],'INITIALIZING'); self.assertEqual(result['data_status'],'UNKNOWN')
        self.assertEqual(result['observed_at'],NOW+90003)

    def test_health_record_write_lock_wait_cannot_publish_expired_ready(self):
        self.seed(); self.protocol.alive(NOW+2,'MATCHING_PONG'); jobs=self.jobs(); now=[NOW+2]
        real=self.store.store_health
        def delayed(value,**kwargs):
            now[0]=NOW+90003; return real(value,**kwargs)
        with patch.object(self.store,'store_health',delayed):
            result=health.sample(self.store,self.protocol,self.names,jobs,NOW+2,0,NOW+2,clock=lambda:now[0])
        self.assertEqual(result['service_status'],'INITIALIZING'); self.assertEqual(result['data_status'],'UNKNOWN')
        self.assertEqual(self.store.read_health()['service_status'],'INITIALIZING')
        self.assertEqual(self.store.read_health()['data_status'],'UNKNOWN')

    def test_health_write_lock_rechecks_current_budget_and_data_gate(self):
        for kind in ('halt','reverify','gap'):
            with self.subTest(kind=kind):
                with self.store.db() as db:
                    db.execute('UPDATE budget SET halt=NULL')
                    db.execute('DELETE FROM metadata WHERE kind=?',('bybit_quiet_reverify:'+self.name,))
                self.seed(); self.protocol.alive(NOW+2,'MATCHING_PONG'); jobs=self.jobs(); real=self.store.store_health
                def changed(value,**kwargs):
                    with self.store.db() as db:
                        if kind=='halt': db.execute("UPDATE budget SET halt='COUNTRY_403_MANUAL_REVIEW'")
                        if kind=='reverify': db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('bybit_quiet_reverify:'+self.name,str(NOW+2)))
                        if kind=='gap': db.execute('UPDATE topics SET gap=1 WHERE topic=?',(self.name,))
                    return real(value,**kwargs)
                with patch.object(self.store,'store_health',changed):
                    result=health.sample(self.store,self.protocol,self.names,jobs,NOW+2,0,NOW+2,clock=lambda:NOW+2)
                self.assertEqual(result['data_status'],'UNKNOWN')
                self.assertEqual(self.store.read_health()['data_status'],'UNKNOWN')

    def test_recovery_budget_can_reset_after_300s_degraded_connection(self):
        budget=health.stream.RecoveryBudget(); budget.failed()
        for elapsed in range(0,301,10): budget.observe(True,elapsed)
        self.assertEqual(budget.failures,0)
        budget.failed(); budget.observe(True,310); budget.observe(False,320); budget.observe(True,330)
        self.assertEqual(budget.failures,1)

    def test_quiet_jobs_are_active_rate_limited_and_never_reconnect(self):
        self.seed(); now=NOW+90002; jobs=self.jobs(now); jobs.seeded=set(self.names)
        jobs.tick(now)
        self.assertLessEqual(len(jobs.active),4)
        self.assertTrue(any(kind=='quiet' for kind,_,_ in jobs.active.values()))
        key=next(key for key in jobs.active if key[0]=='quiet'); future=jobs.active[key][2]; future.set_result(now)
        jobs.finish(); self.assertFalse(jobs.submit(key,'quiet',[key[1]]))
        with self.store.db() as db: db.execute('UPDATE topics SET gap=1 WHERE topic=?',(key[1],))
        jobs.tick(now)  # Repeated gaps submit bounded repair, never session reconnect.

    def test_full_284_topics_schedule_fairly_with_four_workers(self):
        names=consumer.topics_for(sorted('A'+str(i) for i in range(141)))
        names=sorted(names); protocol=health.Protocol(self.store,names,'a'*64,NOW); self.store.ack(protocol.epoch,names)
        jobs=health.Jobs(self.store,protocol,names,HoldingPool(),lambda:NOW+1,lambda:False)
        seen=set()
        for _ in range(100):
            jobs.tick(NOW+1); self.assertLessEqual(len(jobs.active),4)
            for key,(kind,scope,future) in list(jobs.active.items()):
                if kind=='repair': seen.update(scope)
                future.set_result(NOW+1)
            if len(seen)==284: break
        self.assertEqual(seen,set(names)); self.assertEqual(len(protocol.requests),29)

    def test_directory_work_has_bybit_only_shape_and_no_cancelled_write(self):
        calls=[]; cancelled=[False]
        class Client:
            def __init__(_,store): pass
            def request(_,path,params,**kwargs):
                calls.append((path,params,kwargs)); cancelled[0]=True; return instruments(self.names)
        jobs=self.jobs(cancelled=lambda:cancelled[0],client_factory=Client)
        with self.assertRaises(TimeoutError): jobs.work('directory',self.names)
        self.assertEqual(calls[0][0],'/v5/market/instruments-info'); self.assertEqual(calls[0][1],{'category':'spot'})
        self.assertTrue(calls[0][2]['drain_on_cancel']); self.assertIsNone(self.store.get_proof('instrument',self.name))

    def test_repair_work_cancellation_never_writes_a_bar(self):
        cancelled=[False]
        class Client:
            def __init__(_,store): pass
            def request(_,path,params,**kwargs): cancelled[0]=True; return rest('GRASSUSDT')
        jobs=self.jobs(cancelled=lambda:cancelled[0],client_factory=Client)
        with self.assertRaises(TimeoutError): jobs.work('repair',[self.name])
        with self.store.db(readonly=True) as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM bars').fetchone()[0],0)

    def test_queue_shutdown_receipts_conserve_frame_partition(self):
        messages=queue.Queue(); frame=message('GRASSUSDT',ts=NOW+1)
        for value in (frame,frame,message('GRASSUSDT',ts=NOW+2)):
            decoded=self.protocol.arrived(json.dumps(value),NOW+3); messages.put((decoded,NOW+3))
        first=[messages.get(),messages.get()]; self.protocol.receive_batch(first)
        self.store.start_draining(self.epoch); health.drain(self.protocol,messages); self.store.disconnect(self.epoch)
        final=self.protocol.final('TEST'); self.assertTrue(final['complete'])
        self.assertEqual((final['received'],final['committed'],final['duplicate'],final['unprocessed_recorded']),(3,1,1,1))

    def test_non_string_topic_and_invalid_utf8_have_durable_raw_receipts(self):
        import hashlib
        bad=[b'{"topic":42}',b'\xff']
        for raw in bad:
            with self.assertRaises(ValueError): self.protocol.arrived(raw,NOW+1)
        with self.store.db(readonly=True) as db:
            receipts=db.execute('SELECT payload_sha256,reason FROM bb_protocol_invalid ORDER BY id').fetchall()
        self.assertEqual([r[0] for r in receipts],[hashlib.sha256(raw).hexdigest() for raw in bad])
        self.assertTrue(all(r[1].endswith('RAW_WIRE_BYTES_SHA256') for r in receipts))
        self.assertEqual((self.protocol.raw_frames,self.protocol.invalid_envelopes),(2,2))

    def test_drain_deadline_does_not_claim_complete_partition(self):
        value=self.protocol.arrived(json.dumps(message('GRASSUSDT',ts=NOW+1)),NOW+1)
        pending=queue.Queue(); pending.put((value,NOW+1)); self.store.start_draining(self.epoch)
        with self.assertRaisesRegex(health.DurableStopError,'DRAIN_DEADLINE'):
            health.drain(self.protocol,pending,deadline=time.monotonic()-1)
        self.store.disconnect(self.epoch)
        with self.assertRaisesRegex(health.DurableStopError,'CONSERVATION_FAILED'): self.protocol.final('DRAIN_DEADLINE')
        self.assertFalse(self.store.read_health()['final']['complete'])


class RuntimeTests(unittest.TestCase):
    setUp=HealthTests.setUp
    ack=HealthTests.ack

    def fixture(self, pong=True, ack=True, duration=3.5, virtual=None):
        started=time.monotonic(); closed=Event(); incoming=queue.Queue(); calls=[]
        clock=lambda:virtual[0] if virtual is not None else NOW+int((time.monotonic()-started)*100000)
        steady=lambda:(clock()-NOW)/1000
        class WS:
            def send(_,raw):
                request=json.loads(raw)
                if request['op']=='subscribe' and ack:
                    incoming.put(json.dumps(dict(op='subscribe',req_id=request['req_id'],success=True)))
                    for name in request['args']:
                        symbol,interval=split_topic(name)
                        incoming.put(json.dumps(message(symbol,interval,clock())))
            def recv(_,timeout):
                try: return incoming.get(timeout=.003)
                except queue.Empty: raise TimeoutError()
            def ping(_,**kwargs):
                self.assertEqual(kwargs,dict(ack_on_close=False))
                waiter=Event()
                if pong: waiter.set()
                return waiter
            def close(_): closed.set()
        class Client:
            def __init__(_,store): pass
            def request(_,path,params,**kwargs):
                calls.append((path,params))
                if path.endswith('instruments-info'): return instruments(self.names,clock())
                return rest(params['symbol'],params['interval'],params['end'])
        return dict(ws=WS(),clock=clock,steady=steady,stop=lambda:clock()-NOW>=duration*100000,
                    client=Client,calls=calls,closed=closed)

    def test_actual_session_quiet_over_90s_no_reconnect_and_final_conservation(self):
        fixture=self.fixture(duration=2.5); samples=[]
        health.session(self.store,self.names,'a'*64,'b'*40,fixture['stop'],connect=lambda:fixture['ws'],
                       clock=fixture['clock'],steady=fixture['steady'],sleep=lambda _:None,
                       client_factory=fixture['client'],observer=samples.append)
        self.assertTrue(fixture['closed'].is_set())
        later=[s for s in samples if s['observed_at']>NOW+100000]
        self.assertTrue(later); self.assertTrue(any(s['service_status']=='DEGRADED' and s['data_status']=='UNKNOWN' for s in later))
        self.assertTrue(any(s['work_progress']['quiet']['completed'] for s in later))
        self.assertTrue(self.store.read_health()['final']['complete'])
        self.assertEqual(self.store.read_health()['final']['received'],len(self.names))

    def test_actual_matching_pong_deadline_stops_and_cleans(self):
        fixture=self.fixture(pong=False,duration=3)
        with self.assertRaisesRegex(TimeoutError,'MATCHING_PONG_TIMEOUT'):
            health.session(self.store,self.names,'a'*64,'b'*40,fixture['stop'],connect=lambda:fixture['ws'],
                           clock=fixture['clock'],steady=fixture['steady'],sleep=lambda _:None,
                           client_factory=fixture['client'])
        self.assertTrue(fixture['closed'].is_set()); self.assertTrue(self.store.read_health()['final']['complete'])

    def test_actual_missing_ack_stops_despite_matching_pong(self):
        fixture=self.fixture(ack=False,duration=3)
        with self.assertRaisesRegex(TimeoutError,'ACK_TIMEOUT'):
            health.session(self.store,self.names,'a'*64,'b'*40,fixture['stop'],connect=lambda:fixture['ws'],
                           clock=fixture['clock'],steady=fixture['steady'],sleep=lambda _:None,
                           client_factory=fixture['client'])
        self.assertTrue(self.store.read_health()['final']['complete'])

    def test_actual_collect_restores_budget_on_300s_connection_not_data(self):
        virtual=[NOW]; fixture=self.fixture(duration=4.2,virtual=virtual); connects=[]; real_sample=health.sample
        def connect():
            connects.append(True)
            if len(connects)==1: raise OSError('fixture first failure')
            return fixture['ws']
        def sampled(*args,**kwargs):
            result=real_sample(*args,**kwargs); virtual[0]+=10000; return result
        with patch.object(health,'sample',sampled):
            health.collect(self.store,self.names,'a'*64,'b'*40,stop=fixture['stop'],connect=connect,
                           clock=fixture['clock'],steady=fixture['steady'],sleep=lambda _:None,
                           client_factory=fixture['client'])
        self.assertEqual(len(connects),2)
        events=[json.loads(line) for line in self.output.getvalue().splitlines()]
        restored=[e for e in events if e['event']=='BYBIT_HEALTH_RECOVERY_BUDGET_RESTORED']
        self.assertEqual(len(restored),1); self.assertEqual(restored[0]['data_status'],'UNKNOWN')

    def test_queue_full_retains_terminal_producer_error_without_reconnect(self):
        incoming=queue.Queue(); receives=[0]; connects=[]; original=health.sample; original_queue=queue.Queue
        class WS:
            def send(_,raw):
                value=json.loads(raw)
                if value['op']=='subscribe': incoming.put(json.dumps(dict(op='subscribe',req_id=value['req_id'],success=True)))
            def ping(_,**kwargs): ready=Event(); ready.set(); return ready
            def recv(_,timeout):
                receives[0]+=1
                if receives[0]==1: return incoming.get(timeout=1)
                raise health.DurableStopError('ORIGINAL_PRODUCER_TERMINAL')
            def close(_): pass
        def sample(*args,**kwargs): time.sleep(.65); return original(*args,**kwargs)
        def connect(): connects.append(True); return WS()
        class Client:
            def __init__(_,store): pass
            def request(_,path,params,**kwargs): return instruments(self.names,NOW)
        with patch.object(health.queue,'Queue',lambda _:original_queue(1)),patch.object(health,'sample',sample),\
                patch.object(self.store,'start_draining',side_effect=ValueError('fixture revoke error')):
            with self.assertRaisesRegex(health.DurableStopError,'ORIGINAL_PRODUCER_TERMINAL'):
                health.collect(self.store,self.names,'a'*64,'b'*40,connect=connect,clock=lambda:NOW,
                               sleep=lambda _:None,client_factory=Client)
        self.assertEqual(len(connects),1); self.assertTrue(self.store.read_health()['final']['complete'])

    def test_close_failure_stops_collect_without_a_second_connection(self):
        fixture=self.fixture(duration=.1); connects=[]
        def close(): raise OSError('fixture uncertain socket close')
        fixture['ws'].close=close
        def connect(): connects.append(True); return fixture['ws']
        with patch.object(self.store,'start_draining',side_effect=ValueError('fixture revoke error')),\
                self.assertRaisesRegex(health.DurableStopError,'WS_CLOSE_UNCONFIRMED'):
            health.collect(self.store,self.names,'a'*64,'b'*40,stop=fixture['stop'],connect=connect,
                           clock=fixture['clock'],steady=fixture['steady'],sleep=lambda _:None,
                           client_factory=fixture['client'])
        self.assertEqual(len(connects),1)

    def test_cleanup_epoch_error_cannot_mask_original_producer_terminal(self):
        fixture=self.fixture(duration=.8); connects=[]
        def receive(timeout): raise health.DurableStopError('ORIGINAL_PRODUCER_TERMINAL')
        fixture['ws'].recv=receive
        def connect(): connects.append(True); return fixture['ws']
        with patch.object(self.store,'disconnect',side_effect=ValueError('fixture stale epoch')),\
                self.assertRaisesRegex(health.DurableStopError,'ORIGINAL_PRODUCER_TERMINAL'):
            health.collect(self.store,self.names,'a'*64,'b'*40,stop=fixture['stop'],connect=connect,
                           clock=fixture['clock'],steady=fixture['steady'],sleep=lambda _:None,
                           client_factory=fixture['client'])
        self.assertEqual(len(connects),1)


if __name__=='__main__': unittest.main()
