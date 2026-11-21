"""Admission fairness and complete Future evidence without network or runtime files."""
import contextlib
from concurrent.futures import Future,ThreadPoolExecutor
import json
from threading import Event
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from research import hunter_binance_health as health
from research import hunter_binance_stream as stream
from research.hunter_binance_stream_store import STEPS,topic
from research.hunter_binance_public import QuarantinedRequestError

NOW=1800000000000


class Pool:
    def __init__(self):self.calls=[];self.closed=False
    def submit(self,fn,*args):
        future=Future();self.calls.append((fn,args,future));return future
    def shutdown(self,wait,cancel_futures):self.closed=(wait,cancel_futures)


class Store:
    control=None
    def __init__(self,names):
        self.states={name:dict(topic=name,ack=True,gap=0,ws_rx=NOW,
            latest=NOW//STEPS[name.split('_')[-1]]*STEPS[name.split('_')[-1]],
            unresolved=False,reverify=False) for name in names}
        self.proofs=[];self.revocations=[];self.operations=[]
    def scheduler_snapshot(self):return {name:dict(value) for name,value in self.states.items()}
    def metadata(self):return dict(epoch='epoch',scope_hash='a'*64,roster_hash='b'*64,health_epoch_started=str(NOW))
    def revoke_work(self,kind,epoch,names,reason,now,*,job_key):
        self.operations.append('revoke')
        self.revocations.append(dict(kind=kind,epoch=epoch,names=list(names),reason=reason,now=now,job_key=job_key))
    def save_proof(self,value):
        self.operations.append('proof');self.proofs.append(value)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock=[NOW];self.names=[topic('X'+str(i)+'USDT','15m') for i in range(8)]
        self.store=Store(self.names);self.pool=Pool();self.events=[]
        self.protocol=SimpleNamespace(epoch='epoch',evidence=stream.SessionEvidence('epoch',self.names))
        self.jobs=health.CoordinatedJobs(self.store,self.protocol,self.names,self.pool,
            lambda:self.clock[0],lambda:False)
        self.patch=patch.object(stream,'emit',side_effect=self.events.append);self.patch.start();self.addCleanup(self.patch.stop)

    def all_classes(self):
        self.jobs.seeded.update(self.names[1:])
        self.store.states[self.names[1]]['gap']=1
        self.store.states[self.names[2]]['ws_rx']=NOW-90001

    def test_seed_repair_quiet_and_directory_each_get_a_turn(self):
        self.all_classes();self.jobs.tick(NOW)
        self.assertEqual({kind for kind,_,_ in self.jobs.active.values()},set(self.jobs.KINDS))
        self.assertEqual(len(self.jobs.active),4)
        self.assertEqual(sum(kind=='directory' for kind,_,_ in self.jobs.active.values()),1)
        self.assertEqual(self.jobs.seeded,set(self.names[1:]))

    def test_quiet_progress_during_large_seed_backlog_and_wait_is_observed(self):
        names=[topic('X'+str(i)+'USDT',interval) for i in range(409) for interval in ('15m','1h','1d')]+[topic('BTCUSDT','5m')]
        store=Store(names);jobs=health.CoordinatedJobs(store,self.protocol,names,self.pool,lambda:self.clock[0],lambda:False)
        jobs.seeded.add(names[-1]);store.states[names[-1]]['ws_rx']=NOW-90001
        jobs.tick(NOW)
        self.assertIn(('quiet',names[-1]),jobs.active)
        self.clock[0]+=5000;jobs.tick(self.clock[0])
        diag=jobs.diagnostics(self.clock[0])
        self.assertGreater(diag['work_waiting']['seed']['pending'],1200)
        self.assertEqual(diag['work_waiting']['seed']['oldest_wait_ms'],5000)
        self.assertIn('SHARED_WORKERS_FULL',diag['work_waiting']['seed']['reasons'])
        self.assertFalse(diag['work_waiting']['seed']['wall_clock_bound'])

    def test_each_eligible_class_keeps_progress_over_successive_busy_rounds(self):
        self.jobs.seeded.update(self.names[1:3])
        self.store.states[self.names[1]]['gap']=1
        self.store.states[self.names[2]]['ws_rx']=NOW-90001
        for index in range(5):
            self.clock[0]=NOW+index*60000
            self.jobs.tick(self.clock[0])
            self.assertEqual({kind for kind,_,_ in self.jobs.active.values()},set(self.jobs.KINDS))
            for _,_,future in self.jobs.active.values():future.set_result(self.clock[0])
        self.jobs.finish()
        self.assertEqual({kind:row['completed'] for kind,row in self.jobs.progress.items()},
                         {kind:5 for kind in self.jobs.KINDS})
        self.assertEqual(self.jobs.submitted,20)

    def test_control_permissions_are_one_snapshot_per_tick(self):
        calls=[]
        self.store.control=SimpleNamespace(schedule_permissions=lambda epoch,scopes:
            calls.append((epoch,scopes)) or [True for _ in scopes])
        self.jobs.tick(NOW)
        self.jobs.diagnostics(NOW)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(calls[0][1]),len(self.names)+len(self.jobs.directories))

    def test_directory_cannot_take_more_than_one_slot_or_bypass_sixty_seconds(self):
        self.jobs.tick(NOW)
        key=next(key for key in self.jobs.active if key[0]=='directory')
        self.jobs.active[key][2].set_result(NOW)
        self.clock[0]=NOW+1000;self.jobs.tick(self.clock[0])
        self.assertNotIn(key,self.jobs.active)
        self.assertEqual(self.jobs.next_due[key],NOW+60000)
        self.assertEqual(self.jobs.blocked[key],'BACKOFF_OR_REFRESH_INTERVAL')

    def test_repair_need_never_becomes_quiet_when_backed_off_or_exhausted(self):
        name=self.names[0];self.jobs.seeded.add(name)
        self.store.states[name].update(latest=NOW-STEPS['15m'],reverify=True,ws_rx=NOW-90001)
        self.jobs.next_due[('repair',name)]=NOW+30000;self.jobs.tick(NOW)
        self.assertNotIn(('repair',name),self.jobs.active);self.assertNotIn(('quiet',name),self.jobs.active)
        self.assertEqual(self.jobs.blocked[('repair',name)],'BACKOFF_OR_REFRESH_INTERVAL')
        self.jobs.failures[('repair',name)]=3;self.jobs.tick(NOW)
        self.assertEqual(self.jobs.blocked[('repair',name)],'ATTEMPTS_EXHAUSTED')

    def test_shared_cooldown_does_not_claim_a_wall_clock_fairness_bound(self):
        self.all_classes();self.jobs.tick(NOW,'SHARED_COOLDOWN')
        self.clock[0]+=120000;self.jobs.tick(self.clock[0],'SHARED_COOLDOWN')
        self.assertEqual(self.jobs.active,{})
        for row in self.jobs.diagnostics(self.clock[0])['work_waiting'].values():
            self.assertEqual(row['oldest_wait_ms'],120000)
            self.assertIn('SHARED_COOLDOWN',row['reasons'])

    def test_directory_failure_revokes_entire_batch_before_unknown_proofs(self):
        names=self.names[:4];key=('directory',('X0USDT','X1USDT','X2USDT','X3USDT'))
        self.jobs.submit(key,'directory',names)
        self.jobs.active[key][2].set_exception(TimeoutError('HTTP_TIMEOUT'))
        self.jobs.finish()
        self.assertEqual(self.store.operations,['revoke']+['proof']*len(names))
        self.assertEqual(self.store.revocations,[dict(kind='directory',epoch='epoch',names=names,
            reason='TimeoutError',now=NOW,job_key=key)])
        self.assertEqual(len(self.store.proofs),len(names))
        self.assertTrue(all(value['result']=='UNKNOWN' and value['kind']=='instrument' for value in self.store.proofs))
        self.assertEqual(self.jobs.failures[key],1)
        self.assertEqual(self.jobs.next_due[key],NOW+30000)

    def test_failed_directory_revocation_is_terminal_without_proof_write_or_lost_futures(self):
        self.all_classes();self.jobs.tick(NOW)
        def revoke(*args,**kwargs):
            self.store.operations.append('revoke')
            raise health.ControlError('REVOCATION_UNCONFIRMED')
        self.store.revoke_work=revoke
        for kind,names,future in self.jobs.active.values():
            if kind=='directory':future.set_exception(TimeoutError('HTTP_TIMEOUT'))
            else:future.set_result(NOW)
        with self.assertRaisesRegex(health.ControlError,'REVOCATION_UNCONFIRMED'):
            self.jobs.finish()
        self.assertEqual(self.store.operations,['revoke']);self.assertEqual(self.store.proofs,[])
        self.assertEqual(self.jobs.finalized,4);self.assertEqual(self.jobs.active,{})
        self.assertEqual(self.jobs.completed,3)
        self.assertEqual(self.protocol.evidence.http_summary()['requests_terminal'],4)

    def test_finish_collects_all_done_futures_before_primary_exception(self):
        self.all_classes();self.jobs.tick(NOW)
        futures=[future for _,_,future in self.jobs.active.values()]
        futures[0].set_exception(health.DurableStopError('FIRST'))
        futures[1].set_exception(health.UncertainRequestError('SECOND'))
        futures[2].set_result(NOW);futures[3].cancel()
        with self.assertRaisesRegex(health.DurableStopError,'FIRST'):self.jobs.finish()
        self.assertEqual(self.jobs.active,{})
        self.assertEqual(self.jobs.finalized,4)
        self.assertEqual([str(e) for e in self.jobs.terminal_errors],['FIRST','SECOND'])
        self.assertEqual(len([e for e in self.events if e['event']=='BINANCE_HEALTH_WORK_FINAL']),4)
        self.assertEqual(self.protocol.evidence.http_summary()['requests_pending'],0)

    def test_shutdown_common_deadline_and_unresolved_worker_remain_explicit(self):
        self.all_classes();self.jobs.tick(NOW)
        futures=[future for _,_,future in self.jobs.active.values()]
        futures[0].set_exception(health.DurableStopError('FIRST'))
        futures[1].set_exception(health.UncertainRequestError('SECOND'))
        futures[2].set_result(NOW);futures[3].set_running_or_notify_cancel()
        started=time.monotonic();result=self.jobs.shutdown(started+.04)
        self.assertLess(time.monotonic()-started,.5)
        self.assertFalse(result['complete']);self.assertEqual(len(result['unresolved']),1)
        self.assertEqual(self.jobs.finalized,3)
        self.assertEqual(len(result['errors']),3)
        self.assertEqual(self.pool.closed,(False,True))
        self.assertEqual(self.protocol.evidence.http_summary()['requests_pending'],1)
        futures[3].set_result(NOW);self.jobs.finish(raise_errors=False,shutdown=True)
        self.assertEqual(self.protocol.evidence.http_summary()['requests_pending'],0)

    def test_real_workers_wait_without_starving_the_fourth_class(self):
        self.all_classes();release=Event();entered={kind:Event() for kind in self.jobs.KINDS}
        pool=ThreadPoolExecutor(max_workers=4);self.jobs.pool=pool
        def work(kind,names,observer,key):
            entered[kind].set()
            if not release.wait(2):raise AssertionError('test release missing')
            return NOW
        self.jobs.work=work
        try:
            self.jobs.tick(NOW)
            self.assertTrue(all(event.wait(1) for event in entered.values()))
            self.assertTrue(all(not future.done() for _,_,future in self.jobs.active.values()))
            release.set();result=self.jobs.shutdown(time.monotonic()+2)
            self.assertTrue(result['complete']);self.assertEqual(result['finalized'],4)
        finally:release.set();pool.shutdown(wait=True,cancel_futures=True)

    def test_shutdown_retains_primary_and_records_all_secondary_cleanup_failures(self):
        self.all_classes();self.jobs.tick(NOW)
        for _,_,future in self.jobs.active.values():future.set_result(NOW)
        calls=[]
        def bad_draining(epoch):calls.append('draining');raise OSError('draining failed')
        def bad_close():calls.append('close');raise RuntimeError('close failed')
        def bad_final(reason):calls.append('final');raise ValueError('final failed')
        store=SimpleNamespace(start_draining=bad_draining,disconnect=lambda epoch:calls.append('disconnect'))
        protocol=SimpleNamespace(epoch='epoch',evidence=self.protocol.evidence,final=bad_final,
            discard_inflight=lambda reason:None)
        import queue
        primary=health.DurableStopError('PRIMARY')
        result=health.coordinated_shutdown(store,protocol,self.jobs,queue.Queue(),[],
            SimpleNamespace(close=bad_close),primary,'DurableStopError',deadline=time.monotonic()+1)
        self.assertEqual(result['primary_error']['message'],'PRIMARY')
        self.assertEqual([r['stage'] for r in result['secondary_errors']],
                         ['start_draining','close_websockets','receipt_final'])
        self.assertEqual(calls,['draining','close','disconnect','final'])
        self.assertTrue(result['work']['complete'])
        self.assertEqual(result['http']['requests_terminal'],4)

    def test_incomplete_shutdown_upgrades_retryable_error_without_losing_cause(self):
        import queue
        def bad_close():raise OSError('close failed')
        store=SimpleNamespace(start_draining=lambda epoch:None,disconnect=lambda epoch:None)
        protocol=SimpleNamespace(epoch='epoch',evidence=self.protocol.evidence,final=lambda reason:None,
            discard_inflight=lambda reason:None)
        primary=TimeoutError('SOCKET_TIMEOUT')
        with self.assertRaisesRegex(health.DurableStopError,'BINANCE_SHUTDOWN_INCOMPLETE') as caught:
            health.coordinated_shutdown(store,protocol,self.jobs,queue.Queue(),[],
                SimpleNamespace(close=bad_close),primary,'TimeoutError',deadline=time.monotonic()+1)
        self.assertIs(caught.exception.__cause__,primary)

    def test_control_error_is_terminal_and_all_other_futures_are_still_recorded(self):
        self.all_classes();self.jobs.tick(NOW)
        futures=[future for _,_,future in self.jobs.active.values()]
        futures[0].set_exception(health.ControlError('CONTROL_FAILED'))
        for future in futures[1:]:future.cancel()
        with self.assertRaisesRegex(health.ControlError,'CONTROL_FAILED'):self.jobs.finish()
        self.assertEqual(self.jobs.finalized,4)
        self.assertEqual(self.protocol.evidence.http_summary()['requests_terminal'],4)

    def test_quarantined_work_requires_its_unique_claim_before_a_new_http_call(self):
        name=self.names[0];key=('seed',name);claims=[];requests=[];applied=[]
        def claim(epoch,job_key):
            claims.append(job_key)
            return 'recovery-one' if len(claims)==1 else None
        self.store.control=SimpleNamespace(claim_requeue=claim)
        self.store.apply_work=lambda *args,**kwargs:applied.append((args,kwargs))
        class Client:
            def __init__(inner,store):pass
            def request(inner,endpoint,params,**kwargs):requests.append((endpoint,kwargs));return []
        self.jobs.client_factory=Client;self.jobs.requeues[key]='recovery-one'
        self.assertEqual(self.jobs.work('seed',[name],key=key),NOW)
        self.assertEqual(requests[0][1]['context']['requeue_id'],'recovery-one')
        self.assertEqual(applied[0][1]['recovery_id'],'recovery-one')
        with self.assertRaisesRegex(health.DurableStopError,'RECOVERY_CLAIM_UNAVAILABLE'):
            self.jobs.work('seed',[name],key=key)
        self.assertEqual(len(requests),1)
        self.assertEqual(self.protocol.evidence.http_summary()['requests_terminal'],2)

    def quarantined_candidate(self,kind,names=None):
        names=list(names or [self.names[0]])
        key=(kind,tuple(sorted({name.split('@')[0].upper() for name in names})) if kind=='directory' else names[0])
        self.jobs.seeded.update(self.names)
        recovery='quarantine-'+kind;original_scope=frozenset(names)
        def permissions(epoch,scopes):
            return [not set(scope).intersection(original_scope) or (identity==recovery and set(scope)==original_scope)
                    for scope,identity in scopes]
        self.store.control=SimpleNamespace(schedule_permissions=permissions)
        self.jobs.submit(key,kind,names)
        self.jobs.active[key][2].set_exception(QuarantinedRequestError(recovery))
        self.jobs.finish()
        self.clock[0]=NOW+30000
        return key,names,recovery

    def test_quiet_reverification_survives_fresh_websocket_removing_market_need(self):
        key,names,recovery=self.quarantined_candidate('quiet')
        self.store.states[names[0]].update(ws_rx=self.clock[0],reverify=False)
        self.jobs.tick(self.clock[0])
        self.assertIn(key,self.jobs.active)
        self.assertEqual(self.jobs.active[key][:2],('quiet',names))
        directory=next(k for k in self.jobs.candidates if k[0]=='directory')
        self.assertEqual(self.jobs.blocked[directory],'CONTROL_SCOPE_BLOCKED')
        self.assertEqual(self.jobs.requeues[key],recovery)

    def test_repair_reverification_survives_websocket_resolving_gap_and_current_bar(self):
        key,names,recovery=self.quarantined_candidate('repair')
        self.store.states[names[0]].update(gap=0,unresolved=False,latest=NOW,ws_rx=self.clock[0],reverify=False)
        self.jobs.tick(self.clock[0])
        self.assertIn(key,self.jobs.active)
        self.assertEqual(self.jobs.active[key][:2],('repair',names))
        self.assertEqual(self.jobs.requeues[key],recovery)

    def run_queued(self,key):
        future=self.jobs.active[key][2]
        fn,args,_=next(row for row in self.pool.calls if row[2] is future)
        self.assertTrue(future.set_running_or_notify_cancel())
        try:future.set_result(fn(*args))
        except BaseException as error:future.set_exception(error)
        return future

    def test_quiet_recovery_at_native_boundary_keeps_original_kind_and_topic_mutex(self):
        key,names,recovery=self.quarantined_candidate('quiet')
        self.clock[0]=NOW+STEPS['15m']
        self.store.states[names[0]].update(ws_rx=self.clock[0],latest=NOW,gap=1)
        other=('repair',names[0]);self.jobs.submit(other,'repair',names)
        self.jobs.tick(self.clock[0])
        self.assertNotIn(other,self.jobs.candidates)
        self.assertEqual(self.jobs.candidates[key],('quiet',names,True))
        self.assertEqual(self.jobs.blocked[key],'TOPIC_ACTIVE')
        self.assertNotIn(key,self.jobs.active)
        self.jobs.active[other][2].cancel();self.jobs.tick(self.clock[0])
        self.assertIn(key,self.jobs.active)
        self.assertEqual(self.jobs.active[key][0],'quiet')
        self.assertEqual(self.jobs.requeues[key],recovery)
        self.assertFalse(any(k[0] in ('seed','repair') and k[1]==names[0] for k in self.jobs.active))

    def test_directory_recovery_retains_complete_immutable_scope(self):
        key,names,recovery=self.quarantined_candidate('directory',self.names)
        original=tuple(names);names.pop()
        self.store.states[original[-1]]['ack']=False
        self.jobs.tick(self.clock[0]);self.assertEqual(self.jobs.blocked[key],'ACK_PENDING')
        self.assertNotIn(key,self.jobs.active)
        self.store.states[original[-1]]['ack']=True
        self.jobs.tick(self.clock[0])
        self.assertEqual(self.jobs.active[key][:2],('directory',list(original)))
        self.assertEqual(self.jobs.recovery_work[key],('directory',original))
        self.assertEqual(self.jobs.requeues[key],recovery)
        self.assertEqual(sum(kind=='directory' for kind,_,_ in self.jobs.active.values()),1)

    def test_pending_recovery_respects_ack_backoff_and_four_worker_capacity(self):
        key,names,_=self.quarantined_candidate('quiet')
        self.store.states[names[0]]['ack']=False
        self.jobs.tick(self.clock[0]);self.assertEqual(self.jobs.blocked[key],'ACK_PENDING')
        self.store.states[names[0]]['ack']=True;self.clock[0]-=1
        self.jobs.tick(self.clock[0]);self.assertEqual(self.jobs.blocked[key],'BACKOFF_OR_REFRESH_INTERVAL')
        self.clock[0]+=1
        for name in self.names[1:5]:self.jobs.submit(('repair',name),'repair',[name])
        self.jobs.tick(self.clock[0]);self.assertEqual(self.jobs.blocked[key],'SHARED_WORKERS_FULL')
        other=next(iter(self.jobs.active));self.jobs.active[other][2].cancel()
        self.jobs.tick(self.clock[0]);self.assertIn(key,self.jobs.active)
        self.assertEqual(len(self.jobs.active),4)

    def test_recovery_claim_success_uses_original_context_and_unblocks_directory_once(self):
        key,names,recovery=self.quarantined_candidate('quiet')
        state=dict(claimed=False,resolved=False);claims=[];requests=[];applied=[]
        permissions=self.store.control.schedule_permissions
        self.store.control.schedule_permissions=lambda epoch,scopes:([True for _ in scopes]
            if state['resolved'] else permissions(epoch,scopes))
        def claim(epoch,job_key):
            self.assertEqual(job_key,key);claims.append(job_key)
            if state['claimed']:return None
            state['claimed']=True;return recovery
        self.store.control.claim_requeue=claim
        def apply(kind,epoch,scope,body,requested,received,**kwargs):
            self.assertEqual((kind,list(scope),kwargs['job_key'],kwargs['recovery_id']),('quiet',names,key,recovery))
            self.assertIn(key,self.jobs.recovery_work)
            applied.append(kwargs);state['resolved']=True
        self.store.apply_work=apply
        class Client:
            def __init__(inner,store):pass
            def request(inner,endpoint,params,**kwargs):requests.append(kwargs['context']);return []
        self.jobs.client_factory=Client
        self.jobs.tick(self.clock[0]);self.run_queued(key);self.jobs.finish()
        self.assertEqual(len(claims),1);self.assertEqual(len(requests),1);self.assertEqual(len(applied),1)
        self.assertEqual(requests[0],dict(epoch='epoch',job_key=key,kind='quiet',scope=names,requeue_id=recovery))
        self.assertNotIn(key,self.jobs.requeues);self.assertNotIn(key,self.jobs.recovery_work)
        self.assertNotIn(key,self.jobs.claimed_requeues);self.assertNotIn(key,self.jobs.failed_requeues)
        self.jobs.tick(self.clock[0]);self.assertNotIn(key,self.jobs.candidates)
        self.assertTrue(any(kind=='directory' for kind,_,_ in self.jobs.active.values()))
        final=self.jobs.shutdown(time.monotonic()+1)
        self.assertTrue(final['complete']);self.assertEqual(final['submitted'],final['finalized'])
        self.assertEqual(self.protocol.evidence.http_summary()['requests_pending'],0)

    def test_claimed_recovery_failure_stops_and_never_claims_or_sends_again(self):
        key,names,recovery=self.quarantined_candidate('repair');claims=[];requests=[]
        self.store.control.claim_requeue=lambda epoch,job_key:claims.append(job_key) or recovery
        class Client:
            def __init__(inner,store):pass
            def request(inner,*args,**kwargs):requests.append(kwargs);raise TimeoutError('RECOVERY_HTTP_FAILED')
        self.jobs.client_factory=Client
        self.jobs.tick(self.clock[0]);future=self.run_queued(key)
        with self.assertRaisesRegex(health.DurableStopError,'REVERIFICATION_FAILED_MANUAL_REVIEW') as caught:
            self.jobs.finish()
        self.assertIs(caught.exception.__cause__,future.exception())
        self.clock[0]+=120000;self.jobs.tick(self.clock[0])
        self.assertEqual(self.jobs.blocked[key],'REVERIFICATION_FAILED_MANUAL_REVIEW')
        self.assertEqual((len(claims),len(requests)),(1,1));self.assertNotIn(key,self.jobs.active)
        self.assertEqual(self.jobs.requeues[key],recovery);self.assertIn(key,self.jobs.recovery_work)
        final=self.jobs.shutdown(time.monotonic()+1)
        self.assertTrue(final['complete']);self.assertEqual(final['submitted'],final['finalized'])
        self.assertEqual(self.jobs.diagnostics(self.clock[0])['failed_reverifications'],1)

    def test_publication_failure_after_recovery_claim_keeps_gate_without_second_claim(self):
        key,names,recovery=self.quarantined_candidate('quiet');claims=[];requests=[]
        self.store.control.claim_requeue=lambda epoch,job_key:claims.append(job_key) or recovery
        def apply(*args,**kwargs):raise ValueError('PUBLICATION_NOT_VALID')
        self.store.apply_work=apply
        class Client:
            def __init__(inner,store):pass
            def request(inner,*args,**kwargs):requests.append(kwargs);return []
        self.jobs.client_factory=Client
        self.jobs.tick(self.clock[0]);future=self.run_queued(key)
        with self.assertRaisesRegex(health.DurableStopError,'REVERIFICATION_FAILED_MANUAL_REVIEW') as caught:
            self.jobs.finish()
        self.assertIs(caught.exception.__cause__,future.exception())
        self.assertEqual(str(caught.exception.__cause__),'PUBLICATION_NOT_VALID')
        self.clock[0]+=120000;self.jobs.tick(self.clock[0])
        self.assertEqual(self.jobs.blocked[key],'REVERIFICATION_FAILED_MANUAL_REVIEW')
        self.assertEqual(self.jobs.requeues[key],recovery);self.assertIn(key,self.jobs.recovery_work)
        self.assertEqual((len(claims),len(requests)),(1,1))
        final=self.jobs.shutdown(time.monotonic()+1)
        self.assertTrue(final['complete']);self.assertEqual(final['submitted'],final['finalized'])
        self.assertEqual(self.protocol.evidence.http_summary()['requests_pending'],0)

    def test_cancellation_after_claim_preserves_gate_and_is_terminal(self):
        key,names,recovery=self.quarantined_candidate('quiet');claims=[]
        self.store.control.claim_requeue=lambda epoch,job_key:claims.append(job_key) or recovery
        jobs=self.jobs
        class Client:
            def __init__(inner,store):pass
            def request(inner,*args,**kwargs):jobs.cancelled=lambda:True;return []
        self.jobs.client_factory=Client
        self.store.apply_work=lambda *args,**kwargs:self.fail('cancelled recovery published')
        self.jobs.tick(self.clock[0]);self.run_queued(key)
        with self.assertRaisesRegex(health.DurableStopError,'REVERIFICATION_FAILED_MANUAL_REVIEW'):
            self.jobs.finish()
        self.assertEqual(len(claims),1);self.assertEqual(self.jobs.requeues[key],recovery)
        self.assertIn(key,self.jobs.failed_requeues)
        self.assertEqual(self.protocol.evidence.http_summary()['requests_pending'],0)

    def test_third_failure_quarantine_preserves_attempt_limit_with_explicit_stop(self):
        name=self.names[0];key=('quiet',name);self.jobs.failures[key]=2
        self.jobs.submit(key,'quiet',[name])
        self.jobs.active[key][2].set_exception(QuarantinedRequestError('third-failure'))
        with self.assertRaisesRegex(health.DurableStopError,'RECOVERY_ATTEMPTS_EXHAUSTED'):
            self.jobs.finish()
        self.assertEqual(self.jobs.failures[key],3)
        self.assertEqual(self.jobs.requeues[key],'third-failure')
        self.assertEqual(self.jobs.recovery_work[key],('quiet',(name,)))
        self.clock[0]+=120000;self.jobs.tick(self.clock[0])
        self.assertNotIn(key,self.jobs.active)
        self.assertEqual(self.jobs.blocked[key],'REVERIFICATION_FAILED_MANUAL_REVIEW')
        self.assertEqual(self.jobs.progress['quiet']['submitted'],1)

    def test_pre_admission_cancellation_records_request_without_constructing_client(self):
        self.jobs.cancelled=lambda:True
        self.jobs.client_factory=lambda store:self.fail('cancelled work constructed HTTP client')
        with self.assertRaisesRegex(TimeoutError,'WORK_CANCELLED'):
            self.jobs.work('seed',[self.names[0]])
        final=next(e for e in self.events if e['event']=='BINANCE_HTTP_REQUEST_FINAL')
        self.assertEqual(final['terminal'],'NOT_SENT')
        self.assertEqual(final['attempt'],0);self.assertFalse(final['go'])
        self.assertEqual(self.protocol.evidence.http_summary()['requests_pending'],0)

    def test_request_identity_distinguishes_directory_from_same_topic_repair(self):
        name=self.names[0];evidence=self.protocol.evidence
        directory=evidence.request_observer(name,kind='directory',endpoint='/api/v3/exchangeInfo',scope=self.names)
        repair=evidence.request_observer(name,kind='repair',endpoint='/api/v3/klines',scope=[name])
        for observer in (directory,repair):
            observer('REQUEST_END',terminal='NOT_SENT');observer('REQUEST_END',terminal='DUPLICATE')
        records=[e for e in self.events if e['event']=='BINANCE_HTTP_REQUEST_FINAL']
        self.assertEqual(len(records),2)
        self.assertNotEqual(records[0]['request_number'],records[1]['request_number'])
        self.assertNotEqual(records[0]['scope_sha256'],records[1]['scope_sha256'])
        self.assertEqual({e['endpoint'] for e in records},{'/api/v3/klines','/api/v3/exchangeInfo'})
        self.assertTrue(evidence.http_summary()['requests_conserved'])

    def test_queued_cancel_still_has_one_request_terminal(self):
        self.all_classes();self.jobs.tick(NOW)
        result=self.jobs.shutdown(time.monotonic()+1)
        self.assertTrue(result['complete']);self.assertEqual(result['finalized'],4)
        records=[e for e in self.events if e['event']=='BINANCE_HTTP_REQUEST_FINAL']
        self.assertEqual(len(records),4);self.assertTrue(all(e['terminal']=='CANCELLED_NOT_SENT' for e in records))
        self.assertTrue(all(e['attempt']==0 and not e['go'] for e in records))


if __name__=='__main__':unittest.main()
