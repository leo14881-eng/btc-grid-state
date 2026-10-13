"""Opt-in collector health contract. A healthy process never grants data READY."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack
import json
import queue
import sqlite3
from threading import Event, Lock, Thread
import time

from research import hunter_binance_stream as stream
from research.hunter_binance_public import Client, DurableStopError, UncertainRequestError, UnreapedRequestError
from research.hunter_binance_stream_store import Budget, FRESH_MS, LIMITS, STEPS, split_topic, roster_hash, content_hash
from research.hunter_binance_health_store import Store, CONTRACT, proof, valid_proof
from research.hunter_binance_control import ControlError

TERMINAL=(DurableStopError,UncertainRequestError,UnreapedRequestError,sqlite3.Error,ControlError)
PING_SECONDS=30
PONG_SECONDS=20
SHUTDOWN_DRAIN_SECONDS=5


class Protocol(stream.Protocol):
    def __init__(self,store,names,manifest,now):
        super().__init__(store,names,manifest,now)
        store.begin_health(self.epoch,names,now)
        self.shards={i:dict(observed_at=0,evidence=None) for i in range(len(self.requests))}
        self.controls=0;self.processed=0;self.raw_frames=0;self.control_receipts=0;self.invalid_envelopes=0
        self.inflight=[]

    def alive(self,shard,received,evidence):
        with self.lock:
            if received>=self.shards[shard]['observed_at']:
                self.shards[shard]=dict(observed_at=received,evidence=evidence)

    def arrived(self,shard,raw,received):
        with self.lock:self.raw_frames+=1
        try:
            message=json.loads(raw)
            if not isinstance(message,dict):raise ValueError('BINANCE_ENVELOPE_NOT_OBJECT')
            if 'id' in message:
                self.note_arrival(shard,message,received)
                with self.lock:self.control_receipts+=1
            else:self.note_market_arrival(shard,message,received)
            return message
        except (ValueError,KeyError,TypeError,TimeoutError) as error:
            self.store.protocol_invalid(self.epoch,received,raw,type(error).__name__)
            with self.lock:self.invalid_envelopes+=1
            raise

    def receive_batch(self,observations):
        self.inflight=list(observations)
        while self.inflight:
            shard,message,received=self.inflight[0]
            if isinstance(message,BaseException):self.inflight.pop(0);raise message
            if 'id' in message:
                self.receive(shard,message,received);self.controls+=1;self.inflight.pop(0);continue
            group=[]
            for item in self.inflight:
                if isinstance(item[1],BaseException) or 'id' in item[1]:break
                name=stream.split_message_topic(item[1])
                if self.topic_shards.get(name)!=item[0]:raise ValueError('BINANCE_MESSAGE_SHARD_MISMATCH')
                group.append(item)
            results=self.store.ingest_accounted(self.epoch,[(m,r) for _,m,r in group])
            for (index,message,received),result in zip(group,results):
                if result=='committed':self.alive(index,received,'VALIDATED_MARKET_FRAME')
            self.processed+=len(group)
            self.inflight=self.inflight[len(group):]

    def discard_inflight(self,reason):
        rows=[(m,r) for _,m,r in self.inflight if isinstance(m,dict) and 'id' not in m]
        if rows:self.store.unprocessed(self.epoch,rows,reason)
        self.inflight=[]

    def final(self,reason):
        counts=self.store.counts(self.epoch);rows=[]
        with self.evidence.lock:
            for name,received in self.evidence.topics.items():
                row=dict(counts[name],received=received['received'],last_rx=received['last_rx'])
                row['conserved']=row['received']==sum(row[k] for k in ('committed','duplicate','invalid','unprocessed_recorded'))
                rows.append(row)
        raw_conserved=self.raw_frames==self.control_receipts+self.invalid_envelopes+sum(r['received'] for r in rows)
        complete=raw_conserved and all(row['conserved'] for row in rows)
        for row in rows:stream.emit(dict(event='BINANCE_HEALTH_TOPIC_FINAL',**row))
        result=dict(event='BINANCE_HEALTH_SESSION_FINAL',epoch=self.epoch,complete=complete,
                    controls=self.controls,control_receipts=self.control_receipts,raw_application_frames=self.raw_frames,
                    raw_frames_conserved=raw_conserved,
                    unattributed_invalid=self.invalid_envelopes,topics=len(rows),reason=reason,receipt_partition_sha256=content_hash(rows),
                    **{key:sum(r[key] for r in rows) for key in ('received','committed','duplicate','invalid','unprocessed_recorded')})
        self.store.store_health(dict(self.store.read_health() or {},epoch=self.epoch,
                                    contract=self.store.metadata().get('health_contract',CONTRACT),service_status='STOPPED',final=result))
        stream.emit(result)
        if not complete:raise DurableStopError('BINANCE_RECEIPT_CONSERVATION_FAILED')
        return result


def drain(protocol,messages,deadline,steady=time.monotonic):
    """Called only after producers stop. Deadline expiry records, never drops."""
    protocol.discard_inflight('PROCESSING_INTERRUPTED')
    while not messages.empty():
        pending=[]
        for _ in range(min(100,messages.qsize())):
            try:pending.append(messages.get_nowait())
            except queue.Empty:break
        if steady()>=deadline:
            rows=[(m,r) for _,m,r in pending if isinstance(m,dict) and 'id' not in m]
            if rows:protocol.store.unprocessed(protocol.epoch,rows,'SHUTDOWN_DRAIN_DEADLINE')
            continue
        try:protocol.receive_batch(pending)
        except TERMINAL:protocol.discard_inflight('SHUTDOWN_STORAGE_FAILURE');raise
        except Exception:protocol.discard_inflight('SHUTDOWN_INVALID_CONTROL_OR_FRAME')


class Jobs:
    """Fair bounded topic work; no consumer network fallback and no hidden retry loop."""
    def __init__(self,store,protocol,names,pool,clock,cancelled,client_factory=Client):
        self.store=store;self.protocol=protocol;self.names=names;self.pool=pool
        self.clock=clock;self.cancelled=cancelled;self.client_factory=client_factory
        self.active={};self.active_started={};self.next_due={};self.failures={};self.seeded=set();self.completed=0
        self.progress={kind:dict(completed=0,last_success=None) for kind in ('directory','repair','quiet')}
        self.last_progress=clock();self.cursor=deque(names);self.directories=[]
        symbols=sorted({split_topic(n)[0] for n in names})
        for i in range(0,len(symbols),20):
            group=symbols[i:i+20]
            self.directories.append((tuple(group),[n for n in names if split_topic(n)[0] in group]))
        self.directory_cursor=0

    def submit(self,key,kind,names):
        if key in self.active or self.failures.get(key,0)>=3 or self.clock()<self.next_due.get(key,0):return False
        self.active[key]=(kind,names,self.pool.submit(self.work,kind,names))
        self.active_started[key]=self.clock()
        return True

    def work(self,kind,names):
        if self.cancelled():raise TimeoutError('BINANCE_HEALTH_WORK_CANCELLED')
        now=self.clock();client=self.client_factory(self.store)
        epoch=self.protocol.epoch
        observer=self.protocol.evidence.request_observer(names[0])
        if kind=='directory':
            symbols=sorted({split_topic(n)[0] for n in names})
            body=client.request('/api/v3/exchangeInfo',dict(symbols=json.dumps(symbols,ensure_ascii=False,separators=(',',':')),
                showPermissionSets='false'),timeout=30,cancelled=self.cancelled,drain_on_cancel=True,observe=observer)
            if self.cancelled():raise TimeoutError('BINANCE_HEALTH_WORK_CANCELLED')
            self.store.instrument_batch(epoch,names,body,now,self.clock())
        else:
            name=names[0];symbol,interval=split_topic(name)
            body=client.request('/api/v3/klines',dict(symbol=symbol,interval=interval,
                limit=LIMITS[interval],endTime=now),timeout=30,cancelled=self.cancelled,
                drain_on_cancel=True,observe=observer)
            if self.cancelled():raise TimeoutError('BINANCE_HEALTH_WORK_CANCELLED')
            finished=self.clock()
            if kind=='quiet':self.store.verify_quiet(epoch,name,body,now,finished)
            else:self.store.ingest_rest(epoch,name,body,now,finished)
        return now

    def finish(self):
        for key,(kind,names,future) in list(self.active.items()):
            if not future.done():continue
            del self.active[key]
            del self.active_started[key]
            try:
                requested=future.result()
                self.completed+=1;self.last_progress=self.clock();self.failures[key]=0
                self.progress[kind]['completed']+=1;self.progress[kind]['last_success']=self.clock()
                self.next_due[key]=requested+(60000 if kind in ('quiet','directory') else 30000)
                if kind=='repair':self.seeded.add(names[0])
            except TERMINAL:raise
            except Exception as error:
                self.failures[key]=self.failures.get(key,0)+1
                self.next_due[key]=self.clock()+min(120000,30000*2**(self.failures[key]-1))
                meta=self.store.metadata();now=self.clock()
                for name in names:
                    record_kind='instrument' if kind=='directory' else 'quiet'
                    source='OFFICIAL_BINANCE_SPOT_REST_EXCHANGE_INFO' if record_kind=='instrument' else 'OFFICIAL_BINANCE_SPOT_REST'
                    self.store.save_proof(proof(record_kind,name,meta,source,now,now,now,
                        'UNKNOWN','VERIFICATION_FAILED_'+type(error).__name__.upper()))
                stream.emit(dict(event='BINANCE_HEALTH_WORK_FAILED',epoch=self.protocol.epoch,
                                 kind=kind,topics=names,attempt=self.failures[key],error_type=type(error).__name__))

    def tick(self,now):
        self.finish()
        # One directory job at a time; all work shares the same four-owner governor.
        if len(self.active)<4 and not any(k=='directory' for k,_,_ in self.active.values()):
            for _ in self.directories:
                symbols,names=self.directories[self.directory_cursor]
                self.directory_cursor=(self.directory_cursor+1)%len(self.directories)
                if self.submit(('directory',symbols),'directory',names):break
        states={s['topic']:s for s in self.store.states()}
        for _ in range(len(self.cursor)):
            if len(self.active)>=4:break
            name=self.cursor[0];self.cursor.rotate(-1);state=states[name]
            if not state['ack']:continue
            if any((kind,name) in self.active for kind in ('repair','quiet')):continue
            _,interval=split_topic(name)
            with self.store.db(readonly=True) as db:
                latest=db.execute('SELECT MAX(start) FROM bars WHERE topic=?',(name,)).fetchone()[0]
                unresolved=db.execute('SELECT 1 FROM bn_unprocessed WHERE topic=? AND resolved_at IS NULL LIMIT 1',(name,)).fetchone()
                reverify=db.execute('SELECT 1 FROM metadata WHERE kind=?',('quiet_reverify:'+name,)).fetchone()
            needs=(name not in self.seeded or state['gap'] or unresolved
                   or latest is None or latest<now//STEPS[interval]*STEPS[interval])
            if needs:self.submit(('repair',name),'repair',[name])
            elif reverify or now-state['ws_rx']>FRESH_MS:
                self.submit(('quiet',name),'quiet',[name])
        if any(now-started>45000 for started in self.active_started.values()):
            raise TimeoutError('BINANCE_HEALTH_WORK_NO_PROGRESS')

    def diagnostics(self,now):
        exhausted=[dict(kind=key[0],scope=key[1],attempts=count) for key,count in self.failures.items() if count>=3]
        return dict(work_progress={k:dict(v) for k,v in self.progress.items()},
            exhausted_work=exhausted[:20],exhausted_count=len(exhausted),exhausted_sha256=content_hash(exhausted),
            oldest_active_ms=max([now-start for start in self.active_started.values()]+[0]))


class CoordinatedJobs(Jobs):
    """Four-class round robin under the existing four-owner/weight governor.

    A continuously eligible class receives one turn per four eligible class
    selections. This is an admission fairness rule, not a wall-clock promise:
    acknowledgement, backoff, shared cooldown and unavailable workers can stop
    progress. Those reasons remain visible separately from queue waiting.
    """
    KINDS=('directory','seed','repair','quiet')

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.progress={kind:dict(completed=0,last_success=None,submitted=0) for kind in self.KINDS}
        self.classes=deque(self.KINDS);self.waiting={};self.candidates={};self.blocked={}
        self.observers={};self.requeues={};self.terminal_errors=[];self.scope_permissions={}
        self.submitted=0;self.finalized=0;self.last_admission_block=None

    def _context(self,key,kind,names,recovery_id=None):
        value=dict(epoch=self.protocol.epoch,job_key=key,kind=kind,scope=list(names))
        if recovery_id is not None:value['requeue_id']=recovery_id
        return value

    def submit(self,key,kind,names):
        if key in self.active or self.failures.get(key,0)>=3 or self.clock()<self.next_due.get(key,0):return False
        observer=self.protocol.evidence.request_observer(names[0],kind=kind,
            endpoint='/api/v3/exchangeInfo' if kind=='directory' else '/api/v3/klines',scope=names)
        try:future=self.pool.submit(self.work,kind,names,observer,key)
        except BaseException as error:
            observer('REQUEST_END',terminal='NOT_SUBMITTED',error_type=type(error).__name__)
            raise
        self.active[key]=(kind,names,future);self.active_started[key]=self.clock()
        self.observers[key]=observer;self.waiting.pop(key,None)
        self.progress[kind]['submitted']+=1;self.submitted+=1
        return True

    def work(self,kind,names,observer=None,key=None):
        key=key or (kind,tuple(names) if kind=='directory' else names[0])
        observer=observer or self.protocol.evidence.request_observer(names[0],kind=kind,
            endpoint='/api/v3/exchangeInfo' if kind=='directory' else '/api/v3/klines',scope=names)
        terminal='NOT_SENT';error_type=None
        try:
            if self.cancelled():raise TimeoutError('BINANCE_HEALTH_WORK_CANCELLED')
            control=getattr(self.store,'control',None)
            recovery_id=self.requeues.get(key)
            if recovery_id is not None:
                claimed=control.claim_requeue(self.protocol.epoch,key)
                if claimed!=recovery_id:raise DurableStopError('BINANCE_RECOVERY_CLAIM_UNAVAILABLE')
            now=self.clock();client=self.client_factory(self.store)
            context=self._context(key,kind,names,recovery_id)
            if kind=='directory':
                params=dict(symbols=json.dumps(sorted({split_topic(n)[0] for n in names}),
                    ensure_ascii=False,separators=(',',':')),showPermissionSets='false')
                endpoint='/api/v3/exchangeInfo'
            else:
                symbol,interval=split_topic(names[0])
                params=dict(symbol=symbol,interval=interval,limit=LIMITS[interval],endTime=now)
                endpoint='/api/v3/klines'
            body=client.request(endpoint,params,timeout=30,cancelled=self.cancelled,
                drain_on_cancel=True,observe=observer,context=context)
            terminal='HTTP_COMPLETED'
            if self.cancelled():raise TimeoutError('BINANCE_HEALTH_WORK_CANCELLED')
            self.store.apply_work(kind,self.protocol.epoch,names,body,now,self.clock(),
                                  job_key=key,recovery_id=recovery_id)
            return now
        except BaseException as error:
            error_type=type(error).__name__
            if terminal!='NOT_SENT':terminal='WORK_FAILED_AFTER_HTTP'
            raise
        finally:
            # The transport normally emits this first. Idempotence also covers
            # queued cancellation, client construction and fixture clients.
            observer('REQUEST_END',terminal=terminal,error_type=error_type)

    def _record_failure(self,key,kind,names,error,shutdown):
        recovery_id=getattr(error,'requeue_id',None)
        if recovery_id is not None:self.requeues[key]=recovery_id
        if isinstance(error,TERMINAL) or not isinstance(error,Exception):
            self.terminal_errors.append(error);return
        self.failures[key]=self.failures.get(key,0)+1
        self.next_due[key]=self.clock()+min(120000,30000*2**(self.failures[key]-1))
        if not shutdown and recovery_id is None:
            meta=self.store.metadata();now=self.clock()
            if kind=='directory':
                self.store.revoke_work(kind,self.protocol.epoch,names,type(error).__name__,now,job_key=key)
            for name in names:
                record_kind='instrument' if kind=='directory' else 'quiet'
                source='OFFICIAL_BINANCE_SPOT_REST_EXCHANGE_INFO' if kind=='directory' else 'OFFICIAL_BINANCE_SPOT_REST'
                self.store.save_proof(proof(record_kind,name,meta,source,now,now,now,
                    'UNKNOWN','VERIFICATION_FAILED_'+type(error).__name__.upper()))
        stream.emit(dict(event='BINANCE_HEALTH_WORK_FAILED',epoch=self.protocol.epoch,
            kind=kind,topics=names,attempt=self.failures[key],error_type=type(error).__name__,
            recovery_id=recovery_id))

    def finish(self,*,raise_errors=True,shutdown=False):
        first_new=len(self.terminal_errors)
        for key,(kind,names,future) in list(self.active.items()):
            if not future.done():continue
            observer=self.observers[key];error=None;state='CANCELLED_NOT_SENT'
            try:
                if not future.cancelled():
                    requested=future.result();state='COMPLETED'
                    self.completed+=1;self.last_progress=self.clock();self.failures[key]=0
                    self.progress[kind]['completed']+=1;self.progress[kind]['last_success']=self.clock()
                    self.next_due[key]=requested+(60000 if kind in ('quiet','directory') else 30000)
                    if kind in ('seed','repair'):
                        self.seeded.add(names[0])
                        self.next_due[('repair',names[0])]=requested+30000
                    self.requeues.pop(key,None)
            except BaseException as caught:
                error=caught;state='FAILED'
                try:self._record_failure(key,kind,names,caught,shutdown)
                except BaseException as recording_error:self.terminal_errors.append(recording_error)
            finally:
                observer('REQUEST_END',terminal=state,error_type=type(error).__name__ if error else None)
                stream.emit(dict(event='BINANCE_HEALTH_WORK_FINAL',epoch=self.protocol.epoch,
                    request_number=observer.request_number,kind=kind,scope_sha256=content_hash(sorted(names)),
                    terminal=state,error_type=type(error).__name__ if error else None,
                    error=str(error)[:500] if error else None))
                self.active.pop(key,None);self.active_started.pop(key,None);self.observers.pop(key,None)
                self.finalized+=1
        if raise_errors and len(self.terminal_errors)>first_new:raise self.terminal_errors[first_new]

    def _refresh(self,now):
        states=self.store.scheduler_snapshot();candidates={}
        for symbols,names in self.directories:candidates[('directory',symbols)]=('directory',names,True)
        for name in self.names:
            state=states[name];_,interval=split_topic(name)
            kind=('seed' if name not in self.seeded else 'repair'
                  if state['gap'] or state['unresolved'] or state['latest'] is None
                     or state['latest']<now//STEPS[interval]*STEPS[interval] else 'quiet'
                  if state['reverify'] or now-state['ws_rx']>FRESH_MS else None)
            if kind is not None:candidates[(kind,name)]=(kind,[name],bool(state['ack']))
        self.waiting={key:stamp for key,stamp in self.waiting.items() if key in candidates and key not in self.active}
        for key in candidates:
            if key not in self.active:self.waiting.setdefault(key,now)
        self.candidates=candidates
        control=getattr(self.store,'control',None)
        if control is not None:
            keys=list(candidates)
            permissions=control.schedule_permissions(self.protocol.epoch,
                [(candidates[key][1],self.requeues.get(key)) for key in keys])
            if len(permissions)!=len(keys):raise ControlError('CONTROL_SCHEDULE_SCOPE_MISMATCH')
            self.scope_permissions=dict(zip(keys,permissions))

    def _blocked(self,key,row,now):
        kind,names,acked=row
        if not acked:return 'ACK_PENDING'
        if key in self.active:return 'ACTIVE'
        if self.failures.get(key,0)>=3:return 'ATTEMPTS_EXHAUSTED'
        if now<self.next_due.get(key,0):return 'BACKOFF_OR_REFRESH_INTERVAL'
        if kind=='directory' and any(k=='directory' for k,_,_ in self.active.values()):return 'DIRECTORY_ACTIVE'
        if kind!='directory' and any((k,names[0]) in self.active for k in ('seed','repair','quiet')):return 'TOPIC_ACTIVE'
        if self.last_admission_block:return self.last_admission_block
        if len(self.active)>=4:return 'SHARED_WORKERS_FULL'
        if getattr(self.store,'control',None) is not None and not self.scope_permissions.get(key,False):
            return 'CONTROL_SCOPE_BLOCKED'
        return None

    def tick(self,now,admission_block=None):
        self.finish();self.last_admission_block=admission_block;self._refresh(now)
        while len(self.active)<4 and not admission_block:
            selected=None
            for _ in self.KINDS:
                kind=self.classes[0];self.classes.rotate(-1)
                keys=sorted((key for key,row in self.candidates.items() if row[0]==kind),
                            key=lambda key:(self.waiting.get(key,now),str(key)))
                selected=next((key for key in keys if not self._blocked(key,self.candidates[key],now)),None)
                if selected is not None:break
            if selected is None:break
            kind,names,_=self.candidates[selected]
            if not self.submit(selected,kind,names):break
        self.blocked={key:self._blocked(key,row,now) for key,row in self.candidates.items() if key not in self.active}
        if any(now-started>45000 for started in self.active_started.values()):
            raise TimeoutError('BINANCE_HEALTH_WORK_NO_PROGRESS')

    def diagnostics(self,now):
        result=super().diagnostics(now);waiting={}
        for kind in self.KINDS:
            keys=[key for key in self.waiting if key[0]==kind]
            reasons={}
            for key in keys:
                reason=self.blocked.get(key) or 'ELIGIBLE_CLASS_TURN'
                reasons[reason]=reasons.get(reason,0)+1
            waiting[kind]=dict(pending=len(keys),oldest_wait_ms=max([now-self.waiting[key] for key in keys]+[0]),
                reasons=reasons,minimum_share='ONE_PER_ELIGIBLE_FOUR_CLASS_ROUND',wall_clock_bound=False)
        result.update(work_waiting=waiting,work_submitted=self.submitted,work_finalized=self.finalized,
            work_unresolved=len(self.active),work_conserved=self.submitted==self.finalized+len(self.active))
        return result

    def shutdown(self,deadline,steady=time.monotonic):
        # Never short-circuit on one Future. A deadline is one common budget,
        # not a fresh timeout per worker; unresolved workers remain explicit.
        futures=[future for _,_,future in self.active.values()]
        for future in futures:future.cancel()
        self.pool.shutdown(wait=False,cancel_futures=True)
        pending_futures=[future for future in futures if not future.done()]
        if pending_futures:wait(pending_futures,timeout=max(0,deadline-steady()))
        self.finish(raise_errors=False,shutdown=True)
        pending=[]
        for key,(kind,names,future) in self.active.items():
            pending.append(dict(kind=kind,scope_sha256=content_hash(sorted(names)),
                                request_number=self.observers[key].request_number,terminal='UNRESOLVED_AT_DEADLINE'))
        if pending:self.terminal_errors.append(DurableStopError('BINANCE_HTTP_WORKERS_NOT_FINISHED'))
        result=dict(event='BINANCE_HEALTH_WORK_DRAIN',epoch=self.protocol.epoch,
                    submitted=self.submitted,finalized=self.finalized,unresolved=pending,
                    complete=not pending,errors=[dict(type=type(e).__name__,message=str(e)[:500]) for e in self.terminal_errors])
        stream.emit(result)
        return result


def sample(store,protocol,names,jobs,now,queued,queue_progress_at=None):
    states=store.states();meta=store.metadata()
    with protocol.lock:shards={k:dict(v) for k,v in protocol.shards.items()}
    store.connection_proofs(protocol,shards,now)
    all_ack=all(s['ack'] and s['epoch']==protocol.epoch for s in states)
    healthy=all_ack and all(v['observed_at']>0 and 0<=now-v['observed_at']<=FRESH_MS for v in shards.values())
    budget=Budget(store).status()
    healthy=healthy and not budget['halt'] and now>=budget['cooldown_ms']
    result=dict(contract=meta.get('health_contract',CONTRACT),epoch=protocol.epoch,roster_hash=roster_hash(names),manifest_sha256=meta['scope_hash'],
                source_head=meta.get('health_source_head'),observed_at=now,epoch_started=protocol.started,
                service_status='HEALTHY' if healthy else 'INITIALIZING',all_ack=all_ack,topics=len(names),
                shard_topics=[content_hash(sorted(n for n in names if protocol.topic_shards[n]==i)) for i in shards],
                shards=[dict(shard=i,**v) for i,v in shards.items()],queued=queued,processed=protocol.processed,
                active_repairs=len(jobs.active),seeded=len(jobs.seeded),pending_seed=len(names)-len(jobs.seeded),
                completed_work=jobs.completed,work_failures=sum(jobs.failures.values()),halt=budget['halt'],
                cooldown_active=now<budget['cooldown_ms'],publication_verified=False,
                shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY')
    result.update(jobs.diagnostics(now),queue_progress_at=now if queue_progress_at is None else queue_progress_at)
    if isinstance(jobs,CoordinatedJobs):result['market_transactions']=store.transaction_diagnostics()
    generation=store.freeze(names,now,now)
    result.update(data_status=generation['status'],data_reason=generation.get('reason'),
                  data_failed_topic=generation.get('failed_topic'),generation_end_ms=now)
    if healthy and generation['status']!='READY':result['service_status']='DEGRADED'
    store.store_health(result);stream.emit(dict(event='BINANCE_COLLECTOR_HEALTH',**result))
    return result


def coordinated_shutdown(store,protocol,jobs,messages,threads,stack,primary,reason,
                         *,deadline,drain_seconds=SHUTDOWN_DRAIN_SECONDS,steady=time.monotonic):
    """Collect every cleanup result; never replace a primary durable failure."""
    errors=[]
    def attempt(stage,call):
        try:return call()
        except BaseException as error:
            errors.append((stage,error));return None
    attempt('start_draining',lambda:store.start_draining(protocol.epoch))
    attempt('close_websockets',stack.close)
    join_deadline=min(deadline,steady()+7)
    for index,thread in enumerate(threads):
        attempt('join_websocket_'+str(index),lambda t=thread:t.join(timeout=max(0,join_deadline-steady())))
    alive=any(thread.is_alive() for thread in threads)
    if alive:errors.append(('websockets',DurableStopError('BINANCE_WS_THREAD_NOT_REAPED')))
    else:attempt('drain_receipts',lambda:drain(protocol,messages,min(deadline,steady()+drain_seconds),steady))
    work=attempt('drain_http_work',lambda:jobs.shutdown(deadline,steady))
    control=getattr(store,'control',None);control_summary=None
    if control is not None:
        if work is None or not work['complete']:
            attempt('control_latch',lambda:control.latch_stop('BINANCE_HTTP_WORKERS_NOT_FINISHED'))
        control_summary=attempt('control_terminal_summary',lambda:control.terminal_summary(protocol.epoch))
        if control_summary is None or not control_summary['complete'] or not control_summary['all_retired']:
            attempt('control_latch',lambda:control.latch_stop('BINANCE_HTTP_TERMINALS_INCOMPLETE'))
        if control_summary is not None:
            control_summary=dict(control_summary)
            for label in ('calls','attempts'):
                rows=control_summary.pop(label,[])
                control_summary[label+'_sha256']=content_hash(rows)
        attempt('end_control_epoch',lambda:control.end_epoch(protocol.epoch,reason))
    errors.extend(('http_work',error) for error in jobs.terminal_errors if error is not primary)
    attempt('disconnect',lambda:store.disconnect(protocol.epoch))
    if not alive:attempt('receipt_final',lambda:protocol.final(reason))
    audit=dict(event='BINANCE_HEALTH_SHUTDOWN_FINAL',epoch=protocol.epoch,
        primary_error=None if primary is None else dict(type=type(primary).__name__,message=str(primary)[:500]),
        secondary_errors=[dict(stage=stage,type=type(error).__name__,message=str(error)[:500]) for stage,error in errors],
        websocket_threads_alive=alive,work=work,control=control_summary,http=protocol.evidence.http_summary())
    stream.emit(audit)
    # Any incomplete teardown makes reconnect unsafe even when the initiating
    # failure was an ordinary websocket timeout. Preserve its causal chain.
    if primary is not None:
        if not isinstance(primary,TERMINAL) and errors:
            raise DurableStopError('BINANCE_SHUTDOWN_INCOMPLETE') from primary
        return audit
    if errors:raise errors[0][1]
    return audit


def session(store,names,manifest,source_head,stop,connect=stream.open_ws,clock=stream.millis,
            steady=time.monotonic,sleep=time.sleep,client_factory=Client,observer=None,
            drain_seconds=SHUTDOWN_DRAIN_SECONDS,coordinated=False,shutdown_seconds=35):
    if coordinated != (getattr(store,'control',None) is not None):
        raise ValueError('BINANCE_CONTROL_MODE_MISMATCH')
    store.enable_health()
    if coordinated:store.set_control_source(source_head)
    protocol=Protocol(store,names,manifest,clock())
    with store.db() as db:db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('health_source_head',source_head))
    cancelled=Event();messages=stream.ObservedQueue(4096);threads=[];pool=ThreadPoolExecutor(max_workers=4)
    stack=ExitStack();last_check=last_sample=0;last_clock=clock();started=steady();reason='STOP_REQUESTED'
    queue_progress_at=last_clock;last_processed=0;primary=None
    stopping=lambda:cancelled.is_set() or stop()
    jobs=(CoordinatedJobs if coordinated else Jobs)(store,protocol,names,pool,clock,stopping,client_factory)
    def pump(index,ws,requests):
        try:
            for request in requests:
                if stopping():return
                protocol.mark_sent(index,request['id'],clock());ws.send(json.dumps(request));sleep(.5)
            pending_pong=None;ping_started=0;next_ping=steady()
            while not stopping():
                mono=steady()
                if pending_pong is not None:
                    if pending_pong.is_set():
                        protocol.alive(index,clock(),'MATCHING_PONG');pending_pong=None
                    elif mono-ping_started>PONG_SECONDS:raise TimeoutError('BINANCE_SHARD_PONG_TIMEOUT')
                if pending_pong is None and mono>=next_ping:
                    pending_pong=ws.ping(ack_on_close=False);ping_started=mono;next_ping=mono+PING_SECONDS
                try:raw=ws.recv(timeout=1)
                except TimeoutError:continue
                received=clock();message=protocol.arrived(index,raw,received)
                try:messages.put((index,message,received),timeout=.5)
                except queue.Full:
                    if 'id' not in message:store.unprocessed(protocol.epoch,[(message,received)],'RECEIVE_QUEUE_FULL')
                    raise RuntimeError('BINANCE_RECEIVE_BACKPRESSURE')
        except BaseException as error:
            if not stopping():
                try:messages.put((index,error,clock()),timeout=.5)
                except queue.Full:cancelled.set()
    try:
        for index,requests in enumerate(protocol.requests):
            ws=stack.enter_context(connect())
            thread=Thread(target=pump,args=(index,ws,requests),daemon=False)
            threads.append(thread);thread.start()
        while not stopping():
            now=clock()
            if now<last_clock:raise RuntimeError('BINANCE_HEALTH_CLOCK_ROLLBACK')
            last_clock=now;protocol.tick(now)
            if now-last_check>=1000:
                last_check=now
                if not messages.qsize() or protocol.processed>last_processed:
                    queue_progress_at=now;last_processed=protocol.processed
                elif now-queue_progress_at>30000:raise TimeoutError('BINANCE_QUEUE_NO_PROGRESS')
                budget=Budget(store).status()
                if budget['halt']:raise DurableStopError(budget['halt'])
                if now<budget['cooldown_ms']:
                    if budget['cooldown_ms']-now>=590000:raise RuntimeError('BINANCE_GLOBAL_COOLDOWN')
                if coordinated:jobs.tick(now,'SHARED_COOLDOWN' if now<budget['cooldown_ms'] else None)
                elif now>=budget['cooldown_ms']:jobs.tick(now)
                with protocol.lock:shards=[dict(v) for v in protocol.shards.values()]
                if steady()-started>90 and any(not v['observed_at'] or now-v['observed_at']>FRESH_MS for v in shards):
                    raise TimeoutError('BINANCE_SHARD_HEALTH_EXPIRED')
                if now-last_sample>=10000:
                    value=sample(store,protocol,names,jobs,now,messages.qsize(),queue_progress_at);last_sample=now
                    if observer:observer(value)
            batch=[]
            try:batch.append(messages.get(timeout=.1))
            except queue.Empty:continue
            while len(batch)<100:
                try:batch.append(messages.get_nowait())
                except queue.Empty:break
            protocol.receive_batch(batch)
        if cancelled.is_set() and not stop():raise RuntimeError('BINANCE_RECEIVE_BACKPRESSURE')
    except BaseException as error:
        primary=error;reason=type(error).__name__;raise
    finally:
        cancelled.set()  # First prevent more subscriptions, HTTP admission and receives.
        if coordinated:
            coordinated_shutdown(store,protocol,jobs,messages,threads,stack,primary,reason,
                deadline=time.monotonic()+shutdown_seconds,drain_seconds=drain_seconds)
        else:
            legacy_shutdown(store,protocol,jobs,messages,threads,stack,reason,drain_seconds,steady)


def legacy_shutdown(store,protocol,jobs,messages,threads,stack,reason,drain_seconds,steady):
        close_error=None
        try:store.start_draining(protocol.epoch)  # Readers fail closed throughout cleanup.
        except BaseException as error:close_error=error
        try:stack.close()
        except BaseException as error:
            if close_error is None:close_error=error
        for thread in threads:thread.join(timeout=7)
        alive=any(thread.is_alive() for thread in threads)
        try:
            if alive:raise DurableStopError('BINANCE_WS_THREAD_NOT_REAPED')
            drain(protocol,messages,steady()+drain_seconds,steady)
        finally:
            try:
                for _,_,future in jobs.active.values():future.cancel()
                jobs.pool.shutdown(wait=True,cancel_futures=True)
                for _,_,future in jobs.active.values():
                    if not future.cancelled() and isinstance(future.exception(),TERMINAL):raise future.exception()
            finally:
                store.disconnect(protocol.epoch)
                if not alive:protocol.final(reason)
        if close_error:raise close_error


def collect(store,names,manifest,source_head,stop=lambda:False,connect=stream.open_ws,
            clock=stream.millis,sleep=time.sleep,steady=time.monotonic,client_factory=Client,coordinated=False):
    # Health recovery has its own name/contract; data READY never follows from it.
    budget=stream.RecoveryBudget()
    while budget.failures<5 and not stop():
        status=Budget(store).status()
        if status['halt'] or clock()<status['cooldown_ms']:raise DurableStopError(status['halt'] or 'BINANCE_GLOBAL_COOLDOWN')
        def observed(value):
            budget.observe(value['service_status'] in ('HEALTHY','DEGRADED'),steady())
        try:
            session(store,names,manifest,source_head,stop,connect,clock,steady,sleep,client_factory,observed,coordinated=coordinated)
            return
        except TERMINAL:raise
        except Exception as error:
            response=getattr(error,'response',None)
            status=getattr(response,'status_code',0)
            if status in (403,418):
                Budget(store).failure(403,'Binance websocket denial')
                raise DurableStopError('BINANCE_WEBSOCKET_DENIAL') from error
            if status:Budget(store).failure(status,'')
            delay=budget.failed()
            stream.emit(dict(event='BINANCE_HEALTH_RECOVERY',failures=budget.failures,error_type=type(error).__name__))
            if budget.failures<5:
                for _ in range(delay*10):
                    if stop():return
                    sleep(.1)
    if not stop():raise RuntimeError('BINANCE_CIRCUIT_OPEN_MANUAL_REVIEW')
