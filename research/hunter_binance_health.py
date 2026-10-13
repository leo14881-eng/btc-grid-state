"""Opt-in collector health contract. A healthy process never grants data READY."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
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

TERMINAL=(DurableStopError,UncertainRequestError,UnreapedRequestError,sqlite3.Error)
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
                                    contract=CONTRACT,service_status='STOPPED',final=result))
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


def sample(store,protocol,names,jobs,now,queued,queue_progress_at=None):
    states=store.states();meta=store.metadata()
    with protocol.lock:shards={k:dict(v) for k,v in protocol.shards.items()}
    store.connection_proofs(protocol,shards,now)
    all_ack=all(s['ack'] and s['epoch']==protocol.epoch for s in states)
    healthy=all_ack and all(v['observed_at']>0 and 0<=now-v['observed_at']<=FRESH_MS for v in shards.values())
    budget=Budget(store).status()
    healthy=healthy and not budget['halt'] and now>=budget['cooldown_ms']
    result=dict(contract=CONTRACT,epoch=protocol.epoch,roster_hash=roster_hash(names),manifest_sha256=meta['scope_hash'],
                source_head=meta.get('health_source_head'),observed_at=now,epoch_started=protocol.started,
                service_status='HEALTHY' if healthy else 'INITIALIZING',all_ack=all_ack,topics=len(names),
                shard_topics=[content_hash(sorted(n for n in names if protocol.topic_shards[n]==i)) for i in shards],
                shards=[dict(shard=i,**v) for i,v in shards.items()],queued=queued,processed=protocol.processed,
                active_repairs=len(jobs.active),seeded=len(jobs.seeded),pending_seed=len(names)-len(jobs.seeded),
                completed_work=jobs.completed,work_failures=sum(jobs.failures.values()),halt=budget['halt'],
                cooldown_active=now<budget['cooldown_ms'],publication_verified=False,
                shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY')
    result.update(jobs.diagnostics(now),queue_progress_at=now if queue_progress_at is None else queue_progress_at)
    generation=store.freeze(names,now,now)
    result.update(data_status=generation['status'],data_reason=generation.get('reason'),
                  data_failed_topic=generation.get('failed_topic'),generation_end_ms=now)
    if healthy and generation['status']!='READY':result['service_status']='DEGRADED'
    store.store_health(result);stream.emit(dict(event='BINANCE_COLLECTOR_HEALTH',**result))
    return result


def session(store,names,manifest,source_head,stop,connect=stream.open_ws,clock=stream.millis,
            steady=time.monotonic,sleep=time.sleep,client_factory=Client,observer=None,
            drain_seconds=SHUTDOWN_DRAIN_SECONDS):
    store.enable_health()
    protocol=Protocol(store,names,manifest,clock())
    with store.db() as db:db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('health_source_head',source_head))
    cancelled=Event();messages=stream.ObservedQueue(4096);threads=[];pool=ThreadPoolExecutor(max_workers=4)
    stack=ExitStack();last_check=last_sample=0;last_clock=clock();started=steady();reason='STOP_REQUESTED'
    queue_progress_at=last_clock;last_processed=0
    stopping=lambda:cancelled.is_set() or stop()
    jobs=Jobs(store,protocol,names,pool,clock,stopping,client_factory)
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
                else:jobs.tick(now)
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
        reason=type(error).__name__;raise
    finally:
        cancelled.set()  # First prevent more subscriptions, HTTP admission and receives.
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
                pool.shutdown(wait=True,cancel_futures=True)
                for _,_,future in jobs.active.values():
                    if not future.cancelled() and isinstance(future.exception(),TERMINAL):raise future.exception()
            finally:
                store.disconnect(protocol.epoch)
                if not alive:protocol.final(reason)
        if close_error:raise close_error


def collect(store,names,manifest,source_head,stop=lambda:False,connect=stream.open_ws,
            clock=stream.millis,sleep=time.sleep,steady=time.monotonic,client_factory=Client):
    # Health recovery has its own name/contract; data READY never follows from it.
    budget=stream.RecoveryBudget()
    while budget.failures<5 and not stop():
        status=Budget(store).status()
        if status['halt'] or clock()<status['cooldown_ms']:raise DurableStopError(status['halt'] or 'BINANCE_GLOBAL_COOLDOWN')
        def observed(value):
            budget.observe(value['service_status'] in ('HEALTHY','DEGRADED'),steady())
        try:
            session(store,names,manifest,source_head,stop,connect,clock,steady,sleep,client_factory,observed)
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
