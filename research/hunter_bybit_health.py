"""Opt-in Bybit collector supervision, independent of full-roster data READY."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import queue
import sqlite3
from threading import Event, Lock, Thread
import time

from research import hunter_bybit_stream as stream
from research.hunter_bybit_public import Client, DurableStopError, UncertainRequestError, UnreapedRequestError, coordinate
from research.hunter_bybit_stream_store import Budget, FRESH_MS, LIMITS, split_topic, roster_hash, content_hash
from research.hunter_bybit_health_store import Store, CONTRACT, proof, validate_generation

TERMINAL = (DurableStopError, UncertainRequestError, UnreapedRequestError, sqlite3.Error)
PING_SECONDS = 20
PONG_SECONDS = 20


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def plan(names):
    return dict(contract=CONTRACT,topics=len(names),symbols=len({split_topic(n)[0] for n in names}),
                subscriptions=len(stream.subscriptions(names,'plan')),max_active_jobs=4,request_timeout_seconds=30,
                active_job_progress_timeout_seconds=45,max_failures_per_work_key=3,
                verification_interval_seconds=60,recovery_connection_health_seconds=300,
                data_freshness_ms=FRESH_MS,
                note='Continuous full-roster repair/quiet supervision; no total bootstrap deadline or measured completion SLA.')


class Protocol:
    def __init__(self, store, names, manifest, now):
        self.store = store; self.names = names; self.started = now
        self.epoch = store.begin_health(names,manifest,now)
        self.requests = stream.subscriptions(names,self.epoch)
        self.pending = {r['req_id']:r['args'] for r in self.requests}
        self.sent = {}; self.lock = Lock(); self.observed_at = 0; self.evidence = None
        self.raw_frames = self.control_receipts = self.invalid_envelopes = self.controls = self.processed = 0
        self.receipts = {n:dict(received=0,last_rx=0) for n in names}; self.inflight = []
        self.http = dict(attempts=0,go=0,responses=0,reaped=0,retired=0,ended=0)

    def mark_sent(self, request, now):
        with self.lock: self.sent[request['req_id']] = now

    def alive(self, received, evidence):
        with self.lock:
            if received >= self.observed_at:
                self.observed_at = received; self.evidence = evidence

    def request_observer(self, event, **details):
        mapping = dict(ATTEMPT='attempts',GO='go',RESPONSE='responses',REAPED='reaped',RETIRED='retired',END='ended')
        if event in mapping:
            with self.lock: self.http[mapping[event]] += 1

    def arrived(self, raw, received):
        with self.lock: self.raw_frames += 1
        try:
            message = json.loads(raw)
            if not isinstance(message,dict): raise ValueError('BYBIT_ENVELOPE_NOT_OBJECT')
            if 'topic' in message:
                name = message['topic']
                if not isinstance(name,str): raise ValueError('BYBIT_TOPIC_NOT_STRING')
                split_topic(name)
                if name not in self.receipts: raise ValueError('BYBIT_RECEIPT_SCOPE')
                with self.lock:
                    self.receipts[name]['received'] += 1
                    self.receipts[name]['last_rx'] = received
            elif message.get('op') in ('subscribe','ping','pong'):
                with self.lock: self.control_receipts += 1
            else: raise ValueError('BYBIT_UNEXPECTED_ENVELOPE')
            return message
        except (ValueError,KeyError,TypeError) as error:
            self.store.protocol_invalid(self.epoch,received,raw,type(error).__name__)
            with self.lock: self.invalid_envelopes += 1
            raise

    def control(self, message, received):
        if message.get('op') == 'subscribe':
            request_id = message.get('req_id')
            with self.lock:
                sent = self.sent.get(request_id)
                if (sent is None or request_id not in self.pending or not 0 <= received-sent <= 30000
                        or message.get('success') is not True): raise ValueError('BYBIT_SUBSCRIPTION_ACK_INVALID')
                names = self.pending[request_id]
            self.store.ack(self.epoch,names)
            with self.lock: del self.pending[request_id]
        elif not (message.get('op') == 'pong' or
                  message.get('op') == 'ping' and message.get('ret_msg') == 'pong' and message.get('success') is True):
            raise ValueError('BYBIT_CONTROL_INVALID')
        # Spot application pong may omit req_id. It is counted, but only the
        # native ping's matching waiter proves connection health, never data.

    def tick(self, now):
        with self.lock:
            if any(now-self.sent.get(key,self.started) > 30000 for key in self.pending):
                raise TimeoutError('BYBIT_SUBSCRIPTION_ACK_TIMEOUT')

    def receive_batch(self, observations):
        self.inflight = list(observations)
        while self.inflight:
            message,received = self.inflight[0]
            if isinstance(message,BaseException): self.inflight.pop(0); raise message
            if 'topic' not in message:
                self.control(message,received); self.controls += 1; self.inflight.pop(0); continue
            group = []
            for item in self.inflight:
                if isinstance(item[0],BaseException) or 'topic' not in item[0]: break
                group.append(item)
            results = self.store.ingest_accounted(self.epoch,group)
            for (_,received),result in zip(group,results):
                if result == 'committed': self.alive(received,'VALIDATED_MARKET_FRAME')
            self.processed += len(group); self.inflight = self.inflight[len(group):]

    def discard(self, observations, reason, deadline=None, steady=time.monotonic):
        market = [(m,r) for m,r in observations if isinstance(m,dict) and 'topic' in m]
        if market:
            if deadline is None: self.store.unprocessed(self.epoch,market,reason)
            else:
                coordinate(lambda:self.store.unprocessed(self.epoch,market,reason,
                    timeout=min(.1,max(0,deadline-steady()))),deadline)

    def final(self, reason):
        meta=self.store.metadata()
        if meta.get('epoch')!=self.epoch: raise ValueError('STALE_SESSION')
        counts = self.store.counts(self.epoch); rows = []
        with self.lock:
            for name,receipt in self.receipts.items():
                row = dict(counts[name],**receipt)
                row['conserved'] = row['received'] == sum(row[k] for k in ('committed','duplicate','invalid','unprocessed_recorded'))
                rows.append(row)
            raw_conserved = self.raw_frames == self.control_receipts+self.invalid_envelopes+sum(r['received'] for r in rows)
            http = dict(self.http)
        for row in rows: emit(dict(event='BYBIT_HEALTH_TOPIC_FINAL',**row))
        result = dict(event='BYBIT_HEALTH_SESSION_FINAL',epoch=self.epoch,reason=reason,
                      source_head_sha=meta.get('health_source_head'),manifest_sha256=meta['scope_hash'],
                      roster_hash=meta['roster_hash'],epoch_started=self.started,
                      complete=raw_conserved and all(r['conserved'] for r in rows),
                      raw_application_frames=self.raw_frames,control_receipts=self.control_receipts,
                      raw_frames_conserved=raw_conserved,unattributed_invalid=self.invalid_envelopes,
                      topics=len(rows),receipt_partition_sha256=content_hash(rows),http=http,
                      **{key:sum(row[key] for row in rows) for key in ('received','committed','duplicate','invalid','unprocessed_recorded')})
        self.store.store_health(dict(epoch=self.epoch,contract=CONTRACT,epoch_started=self.started,
                                     source_head_sha=meta.get('health_source_head'),manifest_sha256=meta['scope_hash'],
                                     roster_hash=meta['roster_hash'],service_status='STOPPED',data_status='UNKNOWN',final=result))
        emit(result)
        if not result['complete']: raise DurableStopError('BYBIT_RECEIPT_CONSERVATION_FAILED')
        return result


def drain(protocol, messages, deadline=None, steady=time.monotonic):
    """After producers stop, persist receipts; cancelled data never writes bars."""
    deadline=steady()+5 if deadline is None else deadline
    protocol.discard(protocol.inflight,'PROCESSING_INTERRUPTED',deadline,steady); protocol.inflight = []
    while not messages.empty():
        if steady()>=deadline: raise DurableStopError('BYBIT_RECEIPT_DRAIN_DEADLINE_MANUAL_REVIEW')
        batch = []
        for _ in range(min(100,messages.qsize())):
            try: batch.append(messages.get_nowait())
            except queue.Empty: break
        try: protocol.discard(batch,'SESSION_DRAINING',deadline,steady)
        except TimeoutError as error:
            raise DurableStopError('BYBIT_RECEIPT_DRAIN_DEADLINE_MANUAL_REVIEW') from error


class Jobs:
    """Fair full-roster jobs, maximum four owners, three bounded attempts per key."""
    def __init__(self, store, protocol, names, pool, clock, cancelled, client_factory=Client):
        self.store=store; self.protocol=protocol; self.names=names; self.pool=pool
        self.clock=clock; self.cancelled=cancelled; self.client_factory=client_factory
        self.active={}; self.started={}; self.next_due={}; self.failures={}; self.seeded=set()
        self.completed=0; self.cursor=deque(names)
        self.progress={k:dict(completed=0,last_success=None) for k in ('directory','repair','quiet')}

    def submit(self, key, kind, names):
        if (self.cancelled() or len(self.active)>=4 or key in self.active or self.failures.get(key,0)>=3
                or self.clock()<self.next_due.get(key,0)): return False
        self.active[key]=(kind,names,self.pool.submit(self.work,kind,names)); self.started[key]=self.clock()
        return True

    def work(self, kind, names):
        if self.cancelled(): raise TimeoutError('BYBIT_HEALTH_WORK_CANCELLED')
        requested=self.clock(); client=self.client_factory(self.store); epoch=self.protocol.epoch
        options=dict(timeout=30,cancelled=self.cancelled,drain_on_cancel=True,observe=self.protocol.request_observer)
        if kind=='directory':
            # Spot instruments-info has no pagination request or symbol batch here.
            body=client.request('/v5/market/instruments-info',dict(category='spot'),**options)
            if self.cancelled(): raise TimeoutError('BYBIT_HEALTH_WORK_CANCELLED')
            self.store.instrument_batch(epoch,names,body,requested,self.clock())
        else:
            name=names[0]; symbol,interval=split_topic(name)
            body=client.request('/v5/market/kline',dict(category='spot',symbol=symbol,interval=interval,
                                                     limit=LIMITS[interval],end=requested),**options)
            if self.cancelled(): raise TimeoutError('BYBIT_HEALTH_WORK_CANCELLED')
            if kind=='quiet': self.store.verify_quiet(epoch,name,body,requested,self.clock())
            else: self.store.ingest_rest(epoch,name,body,requested,self.clock())
        return requested

    def finish(self):
        for key,(kind,names,future) in list(self.active.items()):
            if not future.done(): continue
            del self.active[key]; del self.started[key]
            try:
                requested=future.result(); self.completed+=1; self.failures[key]=0
                self.progress[kind]['completed']+=1; self.progress[kind]['last_success']=self.clock()
                self.next_due[key]=requested+(60000 if kind in ('quiet','directory') else 30000)
                if kind=='repair': self.seeded.add(names[0])
            except TERMINAL: raise
            except Exception as error:
                self.failures[key]=self.failures.get(key,0)+1
                self.next_due[key]=self.clock()+min(120000,30000*2**(self.failures[key]-1))
                meta=self.store.metadata(); now=self.clock(); record_kind='instrument' if kind=='directory' else 'quiet'
                for name in names:
                    self.store.save_proof(proof(record_kind,name,meta,now,now,now,'UNKNOWN',
                                                'VERIFICATION_FAILED_'+type(error).__name__.upper()))
                emit(dict(event='BYBIT_HEALTH_WORK_FAILED',epoch=self.protocol.epoch,kind=kind,
                          topics=names,attempt=self.failures[key],error_type=type(error).__name__))

    def tick(self, now):
        self.finish()
        self.submit(('directory','spot'),'directory',self.names)
        states={s['topic']:s for s in self.store.states()}
        for _ in range(len(self.cursor)):
            if len(self.active)>=4: break
            name=self.cursor[0]; self.cursor.rotate(-1); state=states[name]
            if not state['ack'] or any((kind,name) in self.active for kind in ('repair','quiet')): continue
            _,interval=split_topic(name); step=int(interval)*60000
            with self.store.db(readonly=True) as db:
                latest=db.execute('SELECT MAX(start) FROM bars WHERE topic=?',(name,)).fetchone()[0]
                unresolved=db.execute('SELECT 1 FROM bb_unprocessed WHERE topic=? AND resolved_at IS NULL LIMIT 1',(name,)).fetchone()
                reverify=db.execute('SELECT 1 FROM metadata WHERE kind=?',('bybit_quiet_reverify:'+name,)).fetchone()
            needs=name not in self.seeded or state['gap'] or unresolved or latest is None or latest<now//step*step
            if needs: self.submit(('repair',name),'repair',[name])
            elif reverify or any(not 0<=now-state[k]<=FRESH_MS for k in ('ws_rx','ws_ts')):
                self.submit(('quiet',name),'quiet',[name])
        if any(now-start>45000 for start in self.started.values()): raise TimeoutError('BYBIT_HEALTH_WORK_NO_PROGRESS')

    def diagnostics(self, now):
        exhausted=[dict(kind=key[0],scope=key[1],attempts=n) for key,n in self.failures.items() if n>=3]
        return dict(work_progress=self.progress,exhausted_work=exhausted[:20],exhausted_count=len(exhausted),
                    exhausted_sha256=content_hash(exhausted),oldest_active_ms=max([now-start for start in self.started.values()]+[0]))


def sample(store, protocol, names, jobs, now, queued, queue_progress_at, clock=None):
    states=store.states(); meta=store.metadata()
    budget=Budget(store).status()
    with protocol.lock:
        observed,evidence=protocol.observed_at,protocol.evidence; pending=bool(protocol.pending); http=dict(protocol.http)
        # A pump can advance evidence while the main loop is reading SQLite.
        # Sample the actual clock AFTER taking that evidence snapshot; using
        # the loop's earlier cut invents future pongs and resets healthy runs.
        if clock is not None: now=clock()
    store.connection_proofs(protocol.epoch,observed,evidence,now)
    all_ack=not pending and len(states)==len(names) and all(s['ack'] and s['epoch']==protocol.epoch for s in states)
    generation=store.freeze(names,now,now)
    if generation['status']=='READY' and not store.completed_generation(generation,now,clock=clock):
        generation=dict(generation,status='UNKNOWN',windows={},reason='BYBIT_HEALTH_READ_FENCE_FAILED')
    budget=Budget(store).status()
    finished=clock() if clock is not None else now
    if generation['status']=='READY' and (finished<now or not validate_generation(generation,finished)):
        generation=dict(generation,status='UNKNOWN',windows={},reason='BYBIT_HEALTH_SAMPLE_FINISHED_STALE')
    timely=0<=finished-now<=30000
    healthy=(timely and all_ack and observed>0 and 0<=finished-observed<=FRESH_MS and not budget['halt']
             and finished>=budget['cooldown_ms'] and (not queued or 0<=finished-queue_progress_at<=30000)
             and all(0<=finished-start<=45000 for start in jobs.started.values()))
    result=dict(contract=CONTRACT,epoch=protocol.epoch,roster_hash=roster_hash(names),manifest_sha256=meta['scope_hash'],
                source_head_sha=meta.get('health_source_head'),observed_at=finished,epoch_started=protocol.started,
                health_reason=None if timely else 'SAMPLE_FINISHED_STALE_OR_CLOCK_ROLLBACK',
                service_status=('HEALTHY' if generation['status']=='READY' else 'DEGRADED') if healthy else 'INITIALIZING',
                data_status=generation['status'],data_reason=generation.get('reason'),data_failed_topic=generation.get('failed_topic'),
                generation_end_ms=now,all_ack=all_ack,topics=len(names),connection_observed_at=observed,connection_evidence=evidence,
                queued=queued,queue_progress_at=queue_progress_at,processed=protocol.processed,active_repairs=len(jobs.active),
                seeded=len(jobs.seeded),pending_seed=len(names)-len(jobs.seeded),completed_work=jobs.completed,
                work_failures=sum(jobs.failures.values()),halt=budget['halt'],cooldown_active=finished<budget['cooldown_ms'],
                http=http,publication_verified=False,shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY',
                **jobs.diagnostics(finished))
    last_budget=[budget]
    def finalize(value,current_budget):
        # Recheck AFTER the health record's write lock is acquired, and again
        # after commit before emitting/returning evidence to recovery observers.
        last_budget[0]=current_budget
        actual=clock() if clock is not None else now
        timely=0<=actual-now<=30000
        ready=(generation['status']=='READY' and actual>=now and validate_generation(generation,actual))
        healthy=(timely and all_ack and observed>0 and 0<=actual-observed<=FRESH_MS
                 and not current_budget['halt'] and actual>=current_budget['cooldown_ms']
                 and (not queued or 0<=actual-queue_progress_at<=30000)
                 and all(0<=actual-start<=45000 for start in jobs.started.values()))
        return dict(value,observed_at=actual,service_status=('HEALTHY' if ready else 'DEGRADED') if healthy else 'INITIALIZING',
                    data_status='READY' if ready else 'UNKNOWN',
                    data_reason=None if ready else generation.get('reason','BYBIT_HEALTH_SAMPLE_FINISHED_STALE'),
                    health_reason=None if timely else 'SAMPLE_FINISHED_STALE_OR_CLOCK_ROLLBACK',
                    halt=current_budget['halt'],cooldown_active=actual<current_budget['cooldown_ms'],
                    oldest_active_ms=max([actual-start for start in jobs.started.values()]+[0]))
    result=store.store_health(result,finalizer=finalize)
    result=finalize(result,last_budget[0])
    emit(dict(event='BYBIT_COLLECTOR_HEALTH',**result))
    return result


def session(store, names, manifest, source_head, stop, connect=stream.open_ws, clock=stream.millis,
            steady=time.monotonic, sleep=time.sleep, client_factory=Client, observer=None):
    store.enable_health(); protocol=Protocol(store,names,manifest,clock())
    with store.db() as db: db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('health_source_head',source_head))
    emit(dict(event='BYBIT_HEALTH_SESSION_STARTED',epoch=protocol.epoch,epoch_started=protocol.started,
              source_head_sha=source_head,manifest_sha256=manifest,roster_hash=roster_hash(names),
              topics=len(names),subscriptions=len(protocol.requests),contract=CONTRACT,
              shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY'))
    cancelled=Event(); messages=queue.Queue(4096); pool=ThreadPoolExecutor(max_workers=4)
    stopping=lambda:cancelled.is_set() or stop()
    jobs=Jobs(store,protocol,names,pool,clock,stopping,client_factory)
    last_clock=clock(); last_sample=last_check=0; queue_progress_at=last_clock; last_processed=0
    begun=steady(); reason='STOP_REQUESTED'; ws=None; worker=None; producer_errors=[]

    def pump():
        try:
            for request in protocol.requests:
                if stopping(): return
                protocol.mark_sent(request,clock()); ws.send(json.dumps(request)); sleep(.1)
            waiter=None; ping_started=0; next_ping=steady()
            while not stopping():
                mono=steady()
                if waiter is not None:
                    if waiter.is_set(): protocol.alive(clock(),'MATCHING_PONG'); waiter=None
                    elif mono-ping_started>PONG_SECONDS: raise TimeoutError('BYBIT_MATCHING_PONG_TIMEOUT')
                if waiter is None and mono>=next_ping:
                    waiter=ws.ping(ack_on_close=False); ping_started=mono; next_ping=mono+PING_SECONDS
                    ws.send(json.dumps(dict(op='ping',req_id=protocol.epoch+'-ping')))
                try: raw=ws.recv(timeout=1)
                except TimeoutError: continue
                received=clock(); message=protocol.arrived(raw,received)
                try: messages.put((message,received),timeout=.5)
                except queue.Full:
                    protocol.discard([(message,received)],'RECEIVE_QUEUE_FULL')
                    raise RuntimeError('BYBIT_RECEIVE_BACKPRESSURE')
        except BaseException as error:
            # A full data queue must never erase a durable stop or turn it into
            # a reconnectable backpressure exception. Inspect after join too.
            producer_errors.append(error)
            if not stopping():
                try: messages.put((error,clock()),timeout=.5)
                except queue.Full: cancelled.set()

    try:
        ws=connect(); worker=Thread(target=pump,daemon=False); worker.start()
        while not stopping():
            now=clock()
            if now<last_clock: raise RuntimeError('BYBIT_HEALTH_CLOCK_ROLLBACK')
            last_clock=now; protocol.tick(now)
            if now-last_check>=1000:
                last_check=now
                if not messages.qsize() or protocol.processed>last_processed:
                    queue_progress_at=now; last_processed=protocol.processed
                elif now-queue_progress_at>30000: raise TimeoutError('BYBIT_QUEUE_NO_PROGRESS')
                budget=Budget(store).status()
                if budget['halt']: raise DurableStopError(budget['halt'])
                if now<budget['cooldown_ms']:
                    if budget['cooldown_ms']-now>=590000: raise RuntimeError('BYBIT_GLOBAL_COOLDOWN')
                else: jobs.tick(now)
                with protocol.lock: observed=protocol.observed_at
                if steady()-begun>90 and (not observed or now-observed>FRESH_MS): raise TimeoutError('BYBIT_CONNECTION_HEALTH_EXPIRED')
                if now-last_sample>=10000:
                    value=sample(store,protocol,names,jobs,now,messages.qsize(),queue_progress_at,clock=clock); last_sample=now
                    if observer: observer(value)
            batch=[]
            try: batch.append(messages.get(timeout=.1))
            except queue.Empty: continue
            while len(batch)<100:
                try: batch.append(messages.get_nowait())
                except queue.Empty: break
            protocol.receive_batch(batch)
        if cancelled.is_set() and not stop(): raise RuntimeError('BYBIT_RECEIVE_BACKPRESSURE')
    except BaseException as error:
        reason=type(error).__name__; raise
    finally:
        cancelled.set()  # Prevent admission before waiting for any database lock.
        cleanup_errors=[]
        try: store.start_draining(protocol.epoch)
        except BaseException as error: cleanup_errors.append(error)
        try:
            if ws is not None: ws.close()
        except BaseException as error:
            cleanup_errors.append(DurableStopError('BYBIT_WS_CLOSE_UNCONFIRMED_MANUAL_REVIEW'))
        if worker is not None: worker.join(timeout=7)
        alive=worker is not None and worker.is_alive()
        producer_terminal=next((e for e in producer_errors if isinstance(e,TERMINAL)),None)
        if producer_terminal is not None: reason=type(producer_terminal).__name__
        try:
            if alive: raise DurableStopError('BYBIT_WS_THREAD_NOT_REAPED')
            drain(protocol,messages)
        except BaseException as error: cleanup_errors.append(error)
        try:
            for _,_,future in jobs.active.values(): future.cancel()
            pool.shutdown(wait=True,cancel_futures=True)
            for _,_,future in jobs.active.values():
                if not future.cancelled() and isinstance(future.exception(),TERMINAL): cleanup_errors.append(future.exception())
        except BaseException as error: cleanup_errors.append(error)
        try: store.disconnect(protocol.epoch)
        except BaseException as error: cleanup_errors.append(error)
        if not alive:
            try: protocol.final(reason)
            except BaseException as error: cleanup_errors.append(error)
        # Cleanup failures can never downgrade a durable stop into a retryable
        # epoch/value/close exception. A healthy socket is not assumed closed.
        if producer_terminal is not None: raise producer_terminal
        terminal=next((e for e in cleanup_errors if isinstance(e,TERMINAL)),None)
        if terminal is not None: raise terminal
        if cleanup_errors:
            raise DurableStopError('BYBIT_SESSION_CLEANUP_INCOMPLETE_MANUAL_REVIEW') from cleanup_errors[0]


def collect(store, names, manifest, source_head, stop=lambda:False, connect=stream.open_ws,
            clock=stream.millis, sleep=time.sleep, steady=time.monotonic, client_factory=Client):
    recovery=stream.RecoveryBudget()
    while recovery.failures<5 and not stop():
        status=Budget(store).status()
        if status['halt'] or clock()<status['cooldown_ms']: raise DurableStopError(status['halt'] or 'BYBIT_GLOBAL_COOLDOWN')
        def observed(value):
            finished=clock()
            current=(value['service_status'] in ('HEALTHY','DEGRADED')
                     and 0<=finished-value['observed_at']<=10000
                     and 0<=finished-value['connection_observed_at']<=FRESH_MS
                     and (not value['queued'] or 0<=finished-value['queue_progress_at']<=30000)
                     and value['oldest_active_ms']+finished-value['observed_at']<=45000)
            if recovery.observe(current,steady()):
                emit(dict(event='BYBIT_HEALTH_RECOVERY_BUDGET_RESTORED',epoch=value['epoch'],
                          reason='SAMPLED_CONNECTION_HEALTH_300_SECONDS',data_status=value['data_status']))
        try:
            session(store,names,manifest,source_head,stop,connect,clock,steady,sleep,client_factory,observed)
            return
        except TERMINAL: raise
        except Exception as error:
            response=getattr(error,'response',None); status=getattr(response,'status_code',0)
            if status==403:
                Budget(store).failure(403,bytes(getattr(response,'body',b'')).decode('utf-8','replace'))
                raise DurableStopError('BYBIT_WEBSOCKET_DENIAL') from error
            if status: Budget(store).failure(status,'')
            delay=recovery.failed()
            emit(dict(event='BYBIT_HEALTH_RECOVERY',failures=recovery.failures,error_type=type(error).__name__))
            if recovery.failures<5:
                for _ in range(delay*10):
                    if stop(): return
                    sleep(.1)
    if not stop(): raise RuntimeError('BYBIT_CIRCUIT_OPEN_MANUAL_REVIEW')
