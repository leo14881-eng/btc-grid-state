"""Explicit, shadow-only Binance native UTC Kline collector. Default is plan only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import datetime as dt
import json
from pathlib import Path
import queue
import re
import signal
import sqlite3
from threading import Event, Lock, Thread
import time

from research.hunter_binance_stream_store import Store, Budget, STATE_PATH, LIMITS, FRESH_MS, roster, roster_hash, split_topic, content_hash
from research.hunter_binance_public import Client, DurableStopError, UncertainRequestError, UnreapedRequestError

WS_URL = 'wss://data-stream.binance.vision/ws'
SHARD_SIZE = 500
MAX_SHARDS = 8
INITIALIZATION_SECONDS = 900
TOPIC_PAGE_SIZE = 16
AUDIT_LOCK = Lock()
AUDIT_FAILURES = 0


def emit(value):
    global AUDIT_FAILURES
    with AUDIT_LOCK:
        try: print(json.dumps(value,sort_keys=True),flush=True)
        except Exception:
            # Diagnostics cannot mask a denial, uncertain owner or unreaped child.
            AUDIT_FAILURES+=1


class ObservedQueue(queue.Queue):
    """High-water is measured under Queue's own mutex, including concurrent drains."""
    def __init__(self, maxsize):
        super().__init__(maxsize); self.high_water=0

    def _put(self, item):
        super()._put(item); self.high_water=max(self.high_water,self._qsize())


class SessionEvidence:
    """Bounded counters only; receipt/processing observations never confer readiness."""
    def __init__(self, epoch, names):
        self.epoch=epoch; self.lock=Lock(); self.serial=0
        self.topics={name:dict(received=0,processed=0,changed=0,last_rx=0,last_processed_rx=0)
                     for name in names}
        self.batches=0; self.batch_max_us=0; self.receipt_to_processing_max_ms=0
        self.receipt_clock_anomalies=0; self.acked_batches=0
        self.http_pending={}; self.http_terminals=0; self.http_allocated=0

    def received(self, name, received):
        with self.lock:
            row=self.topics[name]; row['received']+=1; row['last_rx']=max(row['last_rx'],received)

    def committed(self, batch, changed, elapsed_ns):
        now=millis()
        with self.lock:
            self.batches+=1; self.batch_max_us=max(self.batch_max_us,elapsed_ns//1000)
            for (message,received),did_change in zip(batch,changed):
                row=self.topics[split_message_topic(message)]
                row['processed']+=1; row['changed']+=int(bool(did_change))
                row['last_processed_rx']=max(row['last_processed_rx'],received)
                if now<received:self.receipt_clock_anomalies+=1
                else:self.receipt_to_processing_max_ms=max(self.receipt_to_processing_max_ms,now-received)

    def acked(self, shard, batch, received):
        with self.lock:self.acked_batches+=1
        emit(dict(event='BINANCE_ACK_COMMITTED',epoch=self.epoch,shard=shard,
                  subscribe_batch=batch,received=received))

    def request_observer(self, name, *, kind=None, endpoint=None, scope=None):
        # A request number identifies one logical call, not its topic label or
        # each retry attempt. Allocate before submitting work so queued work
        # cancelled before admission still has exactly one terminal record.
        with self.lock:
            self.serial+=1; serial=self.serial
            identity=dict(epoch=self.epoch,request_number=serial,topic=name)
            if kind is not None:identity['kind']=kind
            if endpoint is not None:identity['endpoint']=endpoint
            if scope is not None:
                identity.update(scope_topic_count=len(scope),scope_sha256=content_hash(sorted(scope)))
            tracked=kind is not None or scope is not None
            if tracked:
                self.http_pending[serial]=identity;self.http_allocated+=1
        row=dict(attempt=0,go=False,response_complete=False,process_reaped=False,owner_retired=False)
        ended=False; row_lock=Lock()
        def observe(event, **fields):
            nonlocal ended
            with row_lock:
                if event=='REQUEST_END':
                    if ended:return
                    ended=True
                    with self.lock:
                        if tracked:
                            self.http_pending.pop(serial,None);self.http_terminals+=1
                    emit(dict(event='BINANCE_HTTP_REQUEST_FINAL',**identity,**row,
                              terminal=fields.get('terminal','CALL_FINISHED'),
                              terminal_error_type=fields.get('error_type'),
                              request_id=fields.get('request_id')))
                    return
                if ended:return
                if event=='ATTEMPT':row.clear();row.update(attempt=fields['attempt'],go=False,
                    response_complete=False,process_reaped=False,owner_retired=False)
                elif event=='GO':row['go']=True
                elif event=='HEADERS':row['http_status']=fields['status']
                elif event=='RESPONSE':row.update(response_complete=True,http_status=fields['status'])
                elif event=='REAPED':row['process_reaped']=True
                elif event=='RETIRED':row['owner_retired']=True
                elif event=='ERROR':row['error_type']=fields['error_type']
                elif event=='END':
                    emit(dict(event='BINANCE_HTTP_LIFECYCLE',**identity,**row))
        observe.request_number=serial
        return observe

    def http_summary(self):
        with self.lock:
            pending=[dict(v) for v in self.http_pending.values()]
            return dict(requests_allocated=self.http_allocated,requests_terminal=self.http_terminals,
                        requests_pending=len(pending),pending_requests=pending,
                        requests_conserved=self.http_allocated==self.http_terminals+len(pending))

    def snapshot(self, messages, active_repairs, *, final=False):
        with self.lock:
            rows=[dict(topic=name,**row) for name,row in self.topics.items()]
            summary=dict(event='BINANCE_SESSION_METRICS',epoch=self.epoch,final=final,
                topics=len(rows),received=sum(r['received'] for r in rows),
                processed=sum(r['processed'] for r in rows),zero_received=sum(r['received']==0 for r in rows),
                zero_processed=sum(r['processed']==0 for r in rows),acked_batches=self.acked_batches,
                committed_batches=self.batches,batch_commit_max_us=self.batch_max_us,
                receipt_to_commit_max_ms=self.receipt_to_processing_max_ms,
                receipt_clock_anomalies=self.receipt_clock_anomalies,queued=messages.qsize(),
                queue_high_water=messages.high_water,active_repairs=active_repairs)
            summary['audit_output_failures']=AUDIT_FAILURES
        emit(summary)
        if final:
            # Bound actual serialized bytes: legal symbols include Unicode,
            # which json.dumps escapes. A row-count bound alone is insufficient.
            offset=0
            while offset<len(rows):
                page=dict(event='BINANCE_TOPIC_METRICS',epoch=self.epoch,
                          offset=offset,total=len(rows),topics=rows[offset:offset+TOPIC_PAGE_SIZE])
                while len(json.dumps(page,sort_keys=True).encode())>4000:
                    page['topics'].pop()
                emit(page);offset+=len(page['topics'])


def millis(): return time.time_ns()//1_000_000


def plan(names):
    roster_hash(names)
    shards = (len(names)+SHARD_SIZE-1)//SHARD_SIZE
    if shards > MAX_SHARDS: raise ValueError('BINANCE_SHARD_CAP_REQUIRES_REVIEW')
    return dict(topics=len(names), sockets=shards, max_topics_per_socket=SHARD_SIZE,
                rest_requests_per_attempt=len(names), rest_weight_per_request=2,
                request_spacing_ms=200, max_inflight=4, recovery_deadline_seconds=900,
                incoming_control_budget='2 subscribe/s plus protocol pong; below 5/s',
                network_capacity_verified=False)


class RecoveryBudget:
    def __init__(self): self.failures=0; self.healthy_since=None; self.last_sample=None

    def observe(self, ready, steady):
        if not ready or (self.last_sample is not None and not 0 <= steady-self.last_sample <= 60):
            self.healthy_since=None
        self.last_sample=steady
        if not ready: return False
        if self.healthy_since is None: self.healthy_since=steady
        if self.failures and steady-self.healthy_since >= 300:
            self.failures=0
            return True
        return False

    def failed(self):
        self.healthy_since=self.last_sample=None
        self.failures+=1
        return 5*2**(self.failures-1)


class InitializationTimeout(TimeoutError):
    """An exhausted bootstrap is terminal, not a new reconnect allowance."""


class InitializationWindow:
    def __init__(self, steady):
        self.deadline=steady+INITIALIZATION_SECONDS; self.ready=False

    def check(self, steady):
        if not self.ready and steady>=self.deadline:
            raise InitializationTimeout('BINANCE_INITIALIZATION_DEADLINE')

    def observe(self, store, names, now, clock, steady, epoch):
        self.check(steady())
        frozen=store.freeze(names,now,now); finished=clock()
        self.check(steady())
        if frozen['status']=='READY' and frozen.get('epoch')==epoch and 0<=finished-now<=FRESH_MS:
            self.ready=True
            emit(dict(event='BINANCE_INITIALIZATION_READY',epoch=frozen['epoch']))


class Protocol:
    def __init__(self, store, names, manifest, now):
        plan(names)
        self.store=store; self.epoch=store.begin_session(names,manifest)
        self.started=now; self.pending={}; self.requests=[]
        self.sent={}; self.arrivals={}; self.lock=Lock()
        self.market_arrivals={}; self.topic_shards={}
        self.evidence=SessionEvidence(self.epoch,names); self.topic_batches={}
        serial=0
        for offset in range(0,len(names),SHARD_SIZE):
            batch=[]
            for start in range(offset,min(offset+SHARD_SIZE,len(names)),100):
                serial+=1
                request_id=self.epoch+f'{serial:03d}'
                args=names[start:min(start+100,offset+SHARD_SIZE,len(names))]
                self.topic_shards.update((name,offset//SHARD_SIZE) for name in args)
                self.topic_batches.update((name,(start-offset)//100) for name in args)
                self.pending[request_id]=(offset//SHARD_SIZE,args)
                batch.append(dict(method='SUBSCRIBE',params=args,id=request_id))
            self.requests.append(batch)

    def note_market_arrival(self, shard, message, received):
        # Receipt only, not validation/readiness. A queued frame must not be
        # mistaken for an idle connection while the SQLite writer catches up.
        name=split_message_topic(message)
        if self.topic_shards.get(name)!=shard: raise ValueError('BINANCE_MESSAGE_SHARD_MISMATCH')
        with self.lock:self.market_arrivals[name]=max(received,self.market_arrivals.get(name,0))
        self.evidence.received(name,received)

    def market_arrival(self, name):
        with self.lock:return self.market_arrivals.get(name,0)

    def mark_sent(self, shard, request_id, now):
        # The pump records the start of this send, not the first shard's dial.
        # A failed/blocked send still consumes its connection's bounded attempt.
        with self.lock:
            pending=self.pending.get(request_id)
            if pending is None or pending[0]!=shard or request_id in self.sent:
                raise ValueError('BINANCE_SUBSCRIPTION_SEND_INVALID')
            self.sent[request_id]=now

    def _validate_ack(self, shard, message, received):
        request_id=message['id'];pending=self.pending.get(request_id)
        sent=self.sent.get(request_id)
        if (pending is None or pending[0]!=shard or sent is None
                or message.get('result','missing') is not None):
            raise ValueError('BINANCE_SUBSCRIPTION_ACK_INVALID')
        if not 0<=received-sent<=30_000:
            raise TimeoutError('BINANCE_ACK_TIMEOUT')
        return request_id,pending

    def note_arrival(self, shard, message, received):
        # Latch receipt before queueing. A timely ACK cannot become late while
        # the main thread finishes another bounded dial or drains market data.
        with self.lock:
            request_id,_=self._validate_ack(shard,message,received)
            if request_id in self.arrivals:raise ValueError('BINANCE_DUPLICATE_ACK')
            self.arrivals[request_id]=received

    def receive(self, shard, message, now):
        if 'id' in message:
            with self.lock:
                request_id,pending=self._validate_ack(shard,message,now)
                if self.arrivals.get(request_id,now)!=now:
                    raise ValueError('BINANCE_ACK_RECEIPT_MISMATCH')
                del self.pending[request_id]
                self.arrivals.pop(request_id,None)
            self.store.ack(self.epoch,pending[1])
            self.evidence.acked(shard,self.topic_batches[pending[1][0]],now)
        else:
            from research.hunter_binance_stream_store import topic
            name=topic(message['s'],message['k']['i'])
            if self.topic_shards.get(name)!=shard:
                raise ValueError('BINANCE_MESSAGE_SHARD_MISMATCH')
            started=time.monotonic_ns(); changed=self.store.ingest_ws(self.epoch,message,now)
            self.evidence.committed([(message,now)],[changed],time.monotonic_ns()-started)

    def tick(self, now):
        with self.lock:
            if any(request_id in self.sent and request_id not in self.arrivals
                   and not 0<=now-self.sent[request_id]<=30_000 for request_id in self.pending):
                raise TimeoutError('BINANCE_ACK_TIMEOUT')
        if now-self.started >= 85_800_000: raise RuntimeError('BINANCE_PLANNED_24H_RECONNECT')

    def receive_batch(self, observations):
        from research.hunter_binance_stream_store import topic
        batch=[]
        def flush():
            if batch:
                started=time.monotonic_ns(); changed=self.store.ingest_ws_batch(self.epoch,batch)
                self.evidence.committed(batch,changed,time.monotonic_ns()-started);batch.clear()
        for shard,message,received in observations:
            if isinstance(message,BaseException):raise message
            if 'id' in message:
                flush();self.receive(shard,message,received)
            else:
                name=topic(message['s'],message['k']['i'])
                if self.topic_shards.get(name)!=shard:
                    raise ValueError('BINANCE_MESSAGE_SHARD_MISMATCH')
                batch.append((message,received))
        flush()


def split_message_topic(message):
    from research.hunter_binance_stream_store import topic
    return topic(message['s'],message['k']['i'])


def open_ws():
    from websockets.sync.client import connect
    # The library answers server PING frames with matching PONG payloads. No
    # client heartbeat/control spam; missing market events still fail freshness.
    return connect(WS_URL,proxy=None,open_timeout=10,close_timeout=5,
                   ping_interval=None,max_size=262144,max_queue=1024)


def repair(store, epoch, name, deadline, cancelled=lambda:False, clock=millis, client=None, observe=None):
    symbol,interval=split_topic(name)
    def active():
        if cancelled(): raise RuntimeError('BINANCE_RECOVERY_CANCELLED')
        with store.db(readonly=True) as db: store.check_epoch(db,epoch)
    active(); requested=clock()
    if requested>=deadline: raise TimeoutError('BINANCE_RECOVERY_DEADLINE')
    options={'observe':observe} if observe is not None else {}
    rows=(client or Client(store)).request('/api/v3/klines',dict(symbol=symbol,
        interval=interval,limit=LIMITS[interval],endTime=requested),
        timeout=min(30,(deadline-requested)/1000),cancelled=cancelled,drain_on_cancel=True,**options)
    active(); received=clock()
    if received>=deadline: raise TimeoutError('BINANCE_RECOVERY_DEADLINE')
    store.ingest_rest(epoch,name,rows,requested,received)


def session(store,names,manifest,stop,connect=open_ws,clock=millis,sleep=time.sleep,
            repairer=repair,observer=None,on_nonready=lambda *_:None,
            initialization=None,steady=time.monotonic):
    initialization=initialization or InitializationWindow(steady())
    initialization.check(steady())
    protocol=Protocol(store,names,manifest,clock())
    on_nonready('SESSION_STARTED')
    cancelled=Event(); messages=ObservedQueue(maxsize=4096); threads=[]; jobs={}; completed=set()
    deadline=clock()+int(max(0,initialization.deadline-steady())*1000)
    last_check=last_audit=0
    def stopping(): return cancelled.is_set() or stop()
    def repair_stopping():
        # Workers recheck this through shared-budget admission immediately
        # before GO, even while the main thread is blocked on SQLite/audit IO.
        return stopping() or (not initialization.ready and steady()>=initialization.deadline)
    def pump(index,ws,requests):
        try:
            for request in requests:
                if stopping(): return
                protocol.mark_sent(index,request['id'],clock())
                ws.send(json.dumps(request)); sleep(.5)
            while not stopping():
                try: raw=ws.recv(timeout=1)
                except TimeoutError: continue
                received=clock();message=json.loads(raw)
                if 'id' in message:protocol.note_arrival(index,message,received)
                else:protocol.note_market_arrival(index,message,received)
                messages.put((index,message,received),timeout=.5)
        except BaseException as error:
            if not stopping():
                try: messages.put((index,error,clock()),timeout=.5)
                except queue.Full: cancelled.set()
    pool=ThreadPoolExecutor(max_workers=4)
    stack=ExitStack()
    def drain():
        # Drain a bounded snapshot before deadline checks. Receipt latches cover
        # ACKs arriving concurrently after this snapshot; no missing ACK gets one.
        remaining=min(messages.qsize(),4096)
        while remaining:
            pending=[]
            for _ in range(min(100,remaining)):
                try:pending.append(messages.get_nowait())
                except queue.Empty:break
            if not pending:break
            protocol.receive_batch(pending);remaining-=len(pending)
    try:
        for index,requests in enumerate(protocol.requests):
            if stopping(): return
            initialization.check(steady())
            drain();protocol.tick(clock())
            ws=stack.enter_context(connect())
            thread=Thread(target=pump,args=(index,ws,requests),daemon=False)
            threads.append(thread); thread.start()
        while not stopping():
            initialization.check(steady())
            drain()
            now=clock()
            protocol.tick(now)
            if now-last_check>=1000:
                last_check=now
                state=Budget(store).status()
                if state['halt'] or now<state['cooldown_ms']:on_nonready('GLOBAL_STOP_OR_COOLDOWN')
                if state['halt'] or state['cooldown_ms']-now>=590_000:
                    raise RuntimeError(state['halt'] or 'BINANCE_GLOBAL_COOLDOWN')
                for name,job in list(jobs.items()):
                    initialization.check(steady())
                    if job.done(): job.result(); completed.add(name); del jobs[name]
                candidate=True
                for state in store.states():
                    name=state['topic']
                    if (not state['ack'] or state['gap'] or state['epoch']!=protocol.epoch
                            or any(not 0<=now-state[k]<=FRESH_MS for k in ('ws_rx','ws_ts'))):
                        candidate=False
                        on_nonready('TOPIC_ACK_GAP_OR_FRESHNESS_UNKNOWN')
                    if state['ack'] and state['gap'] and name not in jobs and len(jobs)<4:
                        initialization.check(steady())
                        if name in completed: raise RuntimeError('BINANCE_NEW_GAP_RECONNECT')
                        options={'observe':protocol.evidence.request_observer(name)} if repairer is repair else {}
                        jobs[name]=pool.submit(repairer,store,protocol.epoch,name,deadline,cancelled=repair_stopping,**options)
                    if initialization.ready and state['ack'] and now-protocol.started>FRESH_MS and now-state['ws_rx']>FRESH_MS:
                        ingress=protocol.market_arrival(name)
                        ingress_now=clock()  # Receipt can occur after the scan's now.
                        if ingress and 0<=ingress_now-ingress<=FRESH_MS:
                            on_nonready('CACHE_WRITER_LAG')  # freeze still rejects stale stored data.
                        else:
                            emit(dict(event='BINANCE_TOPIC_STALE_DETAIL',epoch=protocol.epoch,
                                topic=name,shard=protocol.topic_shards[name],session_age_ms=now-protocol.started,
                                ws_rx=state['ws_rx'],ws_ts=state['ws_ts'],ingress_rx=ingress,
                                ack=state['ack'],gap=state['gap'],queued=messages.qsize(),active_repairs=len(jobs)))
                            raise TimeoutError('BINANCE_TOPIC_STALE')
                # Topic flags alone cannot establish native closed history or a
                # current WS revision. Only the unchanged consumer freeze can
                # end bootstrap, once, for this epoch. Until then stale topics
                # remain UNKNOWN without throwing away other initialization.
                if not initialization.ready and candidate and not jobs:
                    initialization.observe(store,names,now,clock,steady,protocol.epoch)
                if observer and now-last_audit>=30_000:
                    protocol.evidence.snapshot(messages,len(jobs))
                    observer(now); last_audit=now
            try: pending=[messages.get(timeout=.1)]
            except queue.Empty: continue
            while len(pending)<100:
                try:pending.append(messages.get_nowait())
                except queue.Empty:break
            protocol.receive_batch(pending)
        if cancelled.is_set() and not stop(): raise RuntimeError('BINANCE_RECEIVE_BACKPRESSURE')
    finally:
        cancelled.set()
        on_nonready('SESSION_DISCONNECTED')
        try: store.disconnect(protocol.epoch)
        finally:
            try: stack.close()
            finally:
                for job in jobs.values(): job.cancel()
                pool.shutdown(wait=True,cancel_futures=True)
                for thread in threads: thread.join(timeout=7)
                try:
                    if any(t.is_alive() for t in threads): raise DurableStopError('BINANCE_WS_THREAD_NOT_REAPED')
                    for job in jobs.values():
                        if not job.cancelled() and isinstance(job.exception(),(DurableStopError,UncertainRequestError,UnreapedRequestError,sqlite3.Error)):
                            raise job.exception()
                finally:
                    try:protocol.evidence.snapshot(messages,sum(not job.done() for job in jobs.values()),final=True)
                    except Exception:pass  # Preserve the original stop/error even if observation fails.


def collect(store,names,manifest,source_head,stop=lambda:False,connect=open_ws,
            clock=millis,sleep=time.sleep,steady=time.monotonic):
    budget=RecoveryBudget()
    initialization=InitializationWindow(steady())
    def nonready(reason):
        interrupted=budget.healthy_since is not None
        budget.observe(False,steady())
        if interrupted:emit(dict(event='BINANCE_RECOVERY_WINDOW_RESET',reason=reason))
    def audit(now):
        frozen=store.freeze(names,now,now); finished=clock()
        ready=frozen['status']=='READY' and 0<=finished-now<=FRESH_MS
        observed=steady();restored=budget.observe(ready,observed)
        ready_seconds=max(0,int(observed-budget.healthy_since)) if ready and budget.healthy_since is not None else 0
        emit(dict(event='BINANCE_CACHE_AUDIT',source_head=source_head,
            observed_at_ms=finished,ready_window_seconds=ready_seconds,
            status='READY' if ready else 'UNKNOWN',topics=len(names),
            roster_hash=roster_hash(names),manifest_sha256=manifest,
            epoch=frozen.get('epoch'),generation_sha256=frozen.get('generation_sha256'),
            reason=frozen.get('reason'),failed_topic=frozen.get('failed_topic'),
            recovery_budget_restored=restored,shadow_only=True,
            real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY'))
    while budget.failures<5 and not stop():
        state=Budget(store).status()
        if state['halt'] or clock()<state['cooldown_ms']: raise RuntimeError(state['halt'] or 'BINANCE_COOLDOWN_ACTIVE')
        try:
            initialization.check(steady())
            session(store,names,manifest,stop,connect,clock,sleep,observer=audit,on_nonready=nonready,
                    initialization=initialization,steady=steady)
            return
        except (InitializationTimeout,DurableStopError,UncertainRequestError,UnreapedRequestError,sqlite3.Error): raise
        except Exception as error:
            response=getattr(error,'response',None)
            status=getattr(response,'status_code',0)
            if status in (403,418): Budget(store).failure(403,'Binance websocket denial')
            elif status: Budget(store).failure(status,'')
            delay=budget.failed()
            reason=str(error)
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}',reason): reason='UNCLASSIFIED'
            emit(dict(event='BINANCE_STREAM_RECOVERY',failures=budget.failures,
                error_type=type(error).__name__,error_code=reason,status='UNKNOWN'))
            # Failed initial sessions share the original monotonic deadline.
            # A previously READY session may bootstrap a new connection, while
            # the existing five-failure/300-second recovery budget still applies.
            if initialization.ready: initialization=InitializationWindow(steady())
        if budget.failures<5:
            for _ in range(delay*10):
                if stop(): return
                sleep(.1)
    if not stop(): raise RuntimeError('BINANCE_CIRCUIT_OPEN_MANUAL_REVIEW')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--source-head-file',type=Path)
    parser.add_argument('--collect',action='store_true')
    parser.add_argument('--health-supervision',action='store_true',
                        help='Opt-in health contract; never grants consumer data READY')
    parser.add_argument('--coordinated-control',action='store_true',
                        help='Opt-in provisioned control ledger; requires health supervision')
    args=parser.parse_args()
    if args.coordinated_control and not args.health_supervision:
        parser.error('--coordinated-control requires --health-supervision')
    scan=json.loads(args.manifest.read_text(encoding='utf-8'))
    names=roster(scan,dt.datetime.now(dt.timezone.utc))
    emit(plan(names))
    if not args.collect: return
    source=args.source_head_file.read_text().strip() if args.source_head_file else ''
    if not re.fullmatch('[0-9a-f]{40}',source): raise ValueError('BINANCE_SOURCE_HEAD_REQUIRED')
    import fcntl
    stop=Event()
    for signum in (signal.SIGINT,signal.SIGTERM): signal.signal(signum,lambda *_:stop.set())
    with (STATE_PATH.parent/'collector.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.health_supervision:
            from research.hunter_binance_health import collect as collect_health
            from research.hunter_binance_health_store import Store as HealthStore
            store=HealthStore.reader()
            if args.coordinated_control:store.enable_control()
            collect_health(store,names,content_hash(scan),source,stop.is_set,coordinated=args.coordinated_control)
        else:
            collect(Store.reader(),names,content_hash(scan),source,stop.is_set)


if __name__=='__main__': main()
