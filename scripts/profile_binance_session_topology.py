"""Linux-only synthetic session-topology diagnostic, separate from strict gates.

Two 25-second legs preserve production health.session, synchronous tick/sample,
4096 receipt queue and batch100. Only bootstrap data, clock, public loopback
transport and transparent timing wrappers differ. No trading or production I/O.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
import time
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SELF = Path(__file__).resolve()
READER = ROOT/'tests/fixtures/binance_session_reader.py'
HTTP_CHILD = ROOT/'tests/fixtures/binance_ws_reconnect.py'
SECONDS = 25
INTERVAL = .8


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def write_json(path, value):
    path.write_text(canonical(value), encoding='utf-8')


def read_config(scratch):
    if not scratch.is_absolute() or scratch.resolve() != scratch or not scratch.is_dir():
        raise ValueError('SESSION_SCRATCH_MUST_BE_EXISTING_RESOLVED_ABSOLUTE')
    value = json.loads((scratch/'config.json').read_text(encoding='utf-8'))
    if set(value) != {'base_ms', 'anchor_mono', 'source_head', 'manifest', 'ws_port', 'http_port'}:
        raise ValueError('SESSION_CONFIG_KEYS')
    if (type(value['base_ms']) is not int or not isinstance(value['anchor_mono'], (int, float))
            or not 0 <= time.monotonic()-value['anchor_mono'] < 180
            or len(value['source_head']) != 40 or len(value['manifest']) != 64
            or any(type(value[k]) is not int or not 1 <= value[k] <= 65535 for k in ('ws_port','http_port'))):
        raise ValueError('SESSION_CONFIG_BINDING')
    return value


def install_guard(scratch, mode, ports):
    """One new process, one lifetime guard; exact named children only."""
    blocked = []

    def reject(reason):
        if len(blocked) < 16:
            blocked.append(reason)
        raise RuntimeError(reason)

    def path_of(path, dir_fd=None):
        if isinstance(path, int):
            return None
        raw = os.fsdecode(path)
        if raw.startswith('file:'):
            uri = urlsplit(raw)
            if uri.netloc:
                reject('SESSION_NONLOCAL_URI')
            raw = unquote(uri.path)
        target = Path(raw)
        if not target.is_absolute() and isinstance(dir_fd, int) and dir_fd >= 0:
            target = Path(os.readlink('/proc/self/fd/'+str(dir_fd)))/target
        return target.resolve()

    def writable(path, dir_fd=None):
        target = path_of(path, dir_fd)
        if target is None:
            return
        if target == Path(os.devnull) and stat.S_ISCHR(target.stat().st_mode):
            return
        if mode == 'reader':
            if target not in (scratch/'reader.ready', scratch/'reader.result.json'):
                reject('SESSION_READER_WRITE_FORBIDDEN')
        elif target != scratch and scratch not in target.parents:
            reject('SESSION_WRITE_OUTSIDE_SCRATCH')

    def guard(event, args):
        if event in ('socket.connect', 'socket.connect_ex', 'socket.bind'):
            address = args[1]
            allowed = set(ports) | ({0} if mode == 'parent' and event == 'socket.bind' else set())
            if mode == 'reader' or not isinstance(address, tuple) or address[0] != '127.0.0.1' or address[1] not in allowed:
                reject('SESSION_NETWORK_NOT_EXACT_LOOPBACK')
        if event == 'socket.getaddrinfo' and (mode == 'reader' or args[0] != '127.0.0.1' or
                args[1] not in set(ports) | ({0} if mode == 'parent' else set())):
            reject('SESSION_DNS_FORBIDDEN')
        if event in ('socket.gethostbyname', 'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg',
                     'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty') or event.startswith(('os.exec','os.spawn')):
            reject('SESSION_UNAPPROVED_NETWORK_OR_EXEC')
        if event == 'subprocess.Popen':
            command = args[1]
            ok = False
            if isinstance(command, (list, tuple)) and list(command[:2]) == [sys.executable, '-B']:
                if mode == 'parent':
                    ok = (len(command) == 5 and command[2:4] == [str(SELF),'--collector'] or
                          len(command) == 4 and command[2] == str(READER))
                    if ok:
                        target = Path(command[-1]).resolve()
                        ok = target.parent == scratch and target.name in ('no_reader','with_reader')
                elif mode == 'collector':
                    ok = (len(command) == 8 and command[2:4] == [str(HTTP_CHILD),'--http-child']
                          and command[5:] == [str(os.getpid()), str(read_config(scratch)['http_port']), str(scratch)])
                    if ok:
                        from research.hunter_binance_http_child import allowed
                        allowed(command[4])
            if not ok:
                reject('SESSION_UNAPPROVED_CHILD')
        if event == 'sqlite3.connect':
            target = path_of(args[0])
            if target not in (scratch/'state.sqlite3', scratch/'control.sqlite3'):
                reject('SESSION_SQLITE_OUTSIDE_EXACT_PAIR')
            if mode == 'reader' and (not target.is_file() or 'mode=ro' not in str(args[0])):
                reject('SESSION_SQLITE_READER_NOT_READONLY_EXISTING')
        if event == 'open':
            path, file_mode, flags = args
            if (isinstance(file_mode, str) and any(c in file_mode for c in 'wax+') or
                    isinstance(flags, int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
                writable(path)
        if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'):
            index = {'os.mkdir':2,'os.remove':1,'os.rmdir':1,'os.chmod':2,'os.utime':3}[event]
            writable(args[0], args[index] if len(args)>index else None)
        if event in ('os.rename','os.replace','os.link','os.symlink'):
            reject('SESSION_LINK_OR_RENAME_FORBIDDEN')
    sys.addaudithook(guard)
    return blocked


def summarize(histograms):
    return {'timings': {key:value.snapshot() for key,value in histograms.items()}}


def loaded_source_audit(before):
    paths = set()
    for module in tuple(sys.modules.values()):
        name = getattr(module,'__file__',None)
        if name:
            path = Path(name).resolve()
            if ROOT in path.parents and path.suffix == '.py':
                paths.add(path)
    actual = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    changed = [name for name,digest in actual.items() if before.get(name)!=digest]
    return dict(loaded_repo_sha256=actual,before_after_equal=not changed,mismatches=changed)


def native_sqlite_errors(value):
    """Count phase-native failures; expected application rejections stay separate."""
    if not isinstance(value,dict):
        return 0
    total = 0
    for key,item in value.items():
        if key == 'errors' and isinstance(item,dict):
            total += sum(count for code,count in item.items()
                         if code.split(':',1)[0] not in ('None','unknown') or 'SQLITE_' in code
                         or code.rsplit(':',1)[-1] in ('OperationalError','DatabaseError','IntegrityError',
                                                      'InternalError','ProgrammingError','InterfaceError','DataError'))
        elif isinstance(item,dict):
            total += native_sqlite_errors(item)
    return total


def collector(scratch):
    config = read_config(scratch)
    blocked = install_guard(scratch, 'collector', {config['ws_port'],config['http_port']})
    import inspect
    from unittest.mock import patch
    from websockets.sync.client import connect
    from research import hunter_binance_health as health
    from research import hunter_binance_public as public
    from research import hunter_binance_stream as stream
    from research.hunter_binance_control import Control
    from research.hunter_binance_health_store import Store
    from tests.test_hunter_binance_coordinated_runtime import directory, topics
    from tests.test_hunter_binance_coordinated_soak import fixture_rows, LatencyHistogram
    from research.hunter_binance_stream_store import split_topic
    from tests.fixtures.binance_reconnect_diagnostics import error_detail, final_state
    names = topics(['BTCUSDT']+['S%03dUSDT' % i for i in range(408)])
    clock = lambda: config['base_ms']+int((time.monotonic()-config['anchor_mono'])*1000)
    stages = {}; state = {}; arrivals = {}; arrivals_lock = Lock()
    samples = []; shutdowns = []; failures = {}; failed = None
    before = json.loads((scratch/'source-before.json').read_text(encoding='utf-8'))
    partitions = {phase:dict(batches=0,committed=0,duplicate=0,invalid=0,unprocessed_recorded=0)
                  for phase in ('setup','load','drain')}
    counter_lock = Lock()
    crossings = {}

    def phase():
        if 'load' not in state and (scratch/'load.ready').exists():
            state['load'] = json.loads((scratch/'load.json').read_text(encoding='utf-8'))
        load = state.get('load')
        if load is None or time.monotonic()<load['start_mono']:
            return 'setup'
        if time.monotonic()<load['end_mono']:
            return 'load'
        observed = time.monotonic(); cutoff = None
        with counter_lock:
            if 'load_cutoff' not in state:
                cutoff = dict(observed_mono=observed,target_end_mono=load['end_mono'],
                    completed=partitions['load'].copy(),counter_boundary='BATCH_RETURN_AFTER_SQLITE_COMMIT_NO_ADDITIONAL_SQL')
                state['load_cutoff'] = cutoff
        if cutoff is not None:
            stream.emit(dict(event='SESSION_TOPOLOGY_LOAD_CUTOFF',**cutoff))
        return 'drain'
    originals = {name: getattr(health,name) for name in ('session','sample','Protocol','CoordinatedJobs')}
    source_hashes = {name: hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
                     for name,fn in (('session',health.session),('sample',health.sample),
                         ('jobs_tick',health.CoordinatedJobs.tick),('protocol_tick',health.Protocol.tick),
                         ('freeze',Store.freeze))}

    @contextlib.contextmanager
    def timed(name):
        segment = phase()
        wall, cpu = time.perf_counter_ns(), time.thread_time_ns()
        try:
            yield
        except BaseException:
            key = segment+'.'+name
            failures[key] = failures.get(key,0)+1
            raise
        finally:
            end_segment = phase()
            if segment != end_segment:
                key = segment+'->'+end_segment+'.'+name
                crossings[key] = crossings.get(key,0)+1
            for key,value in ((segment+'.'+name+'.wall',time.perf_counter_ns()-wall),
                              (segment+'.'+name+'.thread_cpu',time.thread_time_ns()-cpu)):
                stages.setdefault(key,LatencyHistogram('us')).add(value//1000)

    class TimedStore(Store):
        def ingest_accounted(self,*args,**kwargs):
            result = super().ingest_accounted(*args,**kwargs)
            phase()  # Resolve the load marker outside the pure-counter lock.
            with counter_lock:
                stamp = time.monotonic(); load = state.get('load')
                segment = ('setup' if load is None or stamp<load['start_mono'] else
                           'load' if stamp<load['end_mono'] else 'drain')
                partition = partitions[segment]
                partition['batches'] += 1
                for outcome in result:
                    partition[outcome] += 1
            return result

        def unprocessed(self,epoch,observations,reason):
            result = super().unprocessed(epoch,observations,reason)
            phase()
            with counter_lock:
                stamp = time.monotonic(); load = state.get('load')
                segment = ('setup' if load is None or stamp<load['start_mono'] else
                           'load' if stamp<load['end_mono'] else 'drain')
                partitions[segment]['unprocessed_recorded'] += len(observations)
            return result

        def freeze(self,*args,**kwargs):
            with timed('freeze'):
                return super().freeze(*args,**kwargs)

    class MeasuredProtocol(originals['Protocol']):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            state['protocol'] = self

        def tick(self,*args,**kwargs):
            with timed('protocol_tick'):
                return super().tick(*args,**kwargs)

        def arrived(self,*args,**kwargs):
            value = super().arrived(*args,**kwargs)
            if 'id' not in value:
                with arrivals_lock:
                    arrivals[id(value)] = time.perf_counter_ns()
            return value

        def receive_batch(self,batch):
            with timed('receive_batch'):
                result = super().receive_batch(batch)
            finished = time.perf_counter_ns()
            with arrivals_lock:
                for _,value,_ in batch:
                    received = arrivals.pop(id(value),None)
                    if received is not None:
                        stages.setdefault(phase()+'.arrived_return_to_batch_completion',LatencyHistogram('us')).add((finished-received)//1000)
            if not self.pending and not (scratch/'subscriptions.ready').exists():
                (scratch/'subscriptions.ready').write_text('acknowledged',encoding='utf-8')
            return result

    class BootstrapJobs(originals['CoordinatedJobs']):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            state['jobs'] = self
            with timed('synthetic_bootstrap'):
                epoch = self.protocol.epoch
                stamp = clock()
                self.store.apply_work('directory',epoch,names,directory(names),stamp,stamp,job_key='fixture-bootstrap-directory')
                for name in names:
                    stamp = clock()
                    self.store.apply_work('seed',epoch,[name],fixture_rows(split_topic(name)[1],stamp),
                                          stamp,stamp,job_key='fixture-bootstrap-'+name)
                self.seeded = set(names)

        def tick(self,*args,**kwargs):
            with timed('jobs_tick'):
                return super().tick(*args,**kwargs)

    class ObservedQueue(stream.ObservedQueue):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            state['queue'] = self

    def sampled(*args,**kwargs):
        with timed('sample'):
            return originals['sample'](*args,**kwargs)

    emit = stream.emit
    def measured_emit(value):
        event = value.get('event')
        if event == 'BINANCE_COLLECTOR_HEALTH':
            samples.append({**{k:value.get(k) for k in ('observed_at','service_status','data_status','queued','processed')},
                            'mono':time.monotonic()})
        if event in ('BINANCE_HEALTH_SESSION_FINAL','BINANCE_HEALTH_SHUTDOWN_FINAL'):
            shutdowns.append(value)
        emit(value)

    store = TimedStore(scratch/'state.sqlite3')
    Control.provision(scratch/'control.sqlite3',store.path)
    store.enable_control()
    start = time.monotonic()
    try:
        with patch.object(health,'Protocol',MeasuredProtocol), patch.object(health,'CoordinatedJobs',BootstrapJobs), \
             patch.object(health,'sample',sampled), patch.object(stream,'ObservedQueue',ObservedQueue), \
             patch.object(stream,'emit',measured_emit), patch.object(public,'worker_command',lambda url:
                 [sys.executable,'-B',str(HTTP_CHILD),'--http-child',url,str(os.getpid()),str(config['http_port']),str(scratch)]):
            with timed('session_including_seed_and_shutdown'):
                health.session(store,names,config['manifest'],config['source_head'],
                    stop=lambda:(scratch/'collector.stop').exists() or time.monotonic()-start>150,
                    connect=lambda:connect('ws://127.0.0.1:'+str(config['ws_port']),proxy=None,ping_interval=None,
                                           open_timeout=3,close_timeout=1,max_queue=1024),
                    clock=clock,coordinated=True)
    except BaseException as error:
        failed = error_detail('health.session',error)
    finally:
        final = final_state(store,scratch)
        observed = state.get('queue'); protocol = state.get('protocol')
        audit = loaded_source_audit(before)
        result = dict(diagnostic_completed=True, session_succeeded=failed is None, problem=failed,pid=os.getpid(),
            source_head=config['source_head'],original_method_sha256=source_hashes,
            bootstrap='SYNTHETIC_DIRECTORY_AND_HISTORY_REAL_APPLY_WORK_NO_HTTP_ADMISSION_CLAIM',
            exact_wrappers=['Protocol.__init__/tick/arrived/receive_batch:delegate+timing',
                'CoordinatedJobs.__init__:delegate+synthetic_apply_work+seeded_set',
                'CoordinatedJobs.tick:delegate+timing','Store.freeze:delegate+timing',
                'Store.ingest_accounted/unprocessed:delegate+postcommit_memory_counters',
                'health.sample:delegate+timing','ObservedQueue.__init__:delegate+capture','stream.emit:delegate+capture',
                'public.worker_command:guarded_loopback_HTTP_child'],
            journal_override=False, queue_capacity=4096, batch_limit=100,
            samples=samples, failures=failures, blocked_effects=blocked,
            source_audit=audit,stage_partitions=partitions,load_cutoff=state.get('load_cutoff'),
            receipt_timing_origin='ARRIVED_RETURN_AFTER_JSON_AND_INGRESS_ACCOUNTING',
            timing_scope='METHOD_START_PHASE_INCLUSIVE_NESTED_NOT_ADDITIVE',boundary_crossing_calls=crossings,
            load_committed_per_target_second=partitions['load']['committed']/SECONDS,
            load_counter_clock='POST_COMMIT_MEMORY_ACCOUNTING_MONOTONIC_CLASSIFIED_AND_UPDATED_UNDER_ONE_LOCK',
            queue_depth=None if observed is None else observed.qsize(),
            queue_high_water=None if observed is None else observed.high_water,
            processed=None if protocol is None else protocol.processed,
            pending_receipt_timers=len(arrivals), shutdowns=shutdowns, final=final, **summarize(stages))
        try:
            result['budget_final'] = store.control.status()
        except BaseException as error:
            result['budget_error'] = error_detail('control.status.final',error)
        write_json(scratch/'collector.result.json',result)
    return 0 if failed is None and not blocked and audit['before_after_equal'] and 'budget_error' not in result else 1


def run_leg(scratch, with_reader, ports, head, source):
    from websockets.sync.server import serve
    from websockets.exceptions import ConnectionClosed
    from tests.test_hunter_binance_coordinated_soak import DynamicHTTP
    from tests.test_hunter_binance_coordinated_runtime import topics, MIDNIGHT
    from tests.test_hunter_binance_cache import message
    from research.hunter_binance_stream_store import split_topic, content_hash
    from tests.fixtures.binance_reconnect_diagnostics import error_detail
    scratch.mkdir()
    write_json(scratch/'source-before.json',source)
    names = topics(['BTCUSDT']+['S%03dUSDT' % i for i in range(408)])
    groups = [names[i:i+500] for i in range(0,len(names),500)]
    stopped = Event(); lock = Lock(); sockets = []; sent = {}; errors = []; config = {}
    clock = lambda: config['base_ms']+int((time.monotonic()-config['anchor_mono'])*1000)

    def handler(peer):
        local = []; shard = None
        try:
            while shard is None or len(local)<len(groups[shard]):
                request = json.loads(peer.recv(timeout=20))
                if request.get('method') != 'SUBSCRIBE':
                    raise AssertionError('SESSION_SUBSCRIBE_REQUIRED')
                params = request.get('params')
                if shard is None:
                    shard = next(i for i,g in enumerate(groups) if params == g[:100])
                    with lock:
                        if shard in sent:
                            raise AssertionError('SESSION_DUPLICATE_SHARD')
                        sent[shard] = dict(frames=0,rounds=0,send_seconds=0,max_round_seconds=0,
                                           first_send_mono=None,last_send_mono=None)
                        sockets.append(peer)
                if params != groups[shard][len(local):len(local)+100]:
                    raise AssertionError('SESSION_SUBSCRIPTION_SCOPE')
                local.extend(params)
                peer.send(json.dumps(dict(id=request['id'],result=None)))
            while not stopped.is_set() and not (scratch/'load.ready').exists():
                stopped.wait(.02)
            if stopped.is_set():
                return
            load = json.loads((scratch/'load.json').read_text(encoding='utf-8'))
            stats = sent[shard]
            while not stopped.is_set() and time.monotonic()<load['end_mono']:
                began = time.monotonic(); stamp = clock()
                if stats['first_send_mono'] is None:
                    stats['first_send_mono'] = began
                for name in local:
                    symbol,interval = split_topic(name)
                    peer.send(json.dumps(message(symbol,interval,stamp)))
                    stats['frames'] += 1
                    stats['last_send_mono'] = time.monotonic()
                elapsed = time.monotonic()-began
                stats['send_seconds'] += elapsed
                stats['max_round_seconds'] = max(stats['max_round_seconds'],elapsed)
                stats['rounds'] += 1
                stopped.wait(max(0,INTERVAL-elapsed))
            (scratch/('producer.'+str(shard)+'.done')).write_text('done',encoding='utf-8')
            stopped.wait(45)
        except ConnectionClosed as error:
            if not stopped.is_set():
                errors.append(error_detail('websocket.closed',error))
        except BaseException as error:
            errors.append(error_detail('websocket.handler',error))

    http = DynamicHTTP(); ports.add(http.port)
    try:
        server = serve(handler,'127.0.0.1',0,ping_interval=None,close_timeout=1)
        ports.add(server.socket.getsockname()[1])
        server_thread = Thread(target=server.serve_forever); server_thread.start()
    except BaseException:
        http.close()
        raise
    config.update(base_ms=MIDNIGHT+120000,anchor_mono=time.monotonic(),source_head=head,
                  manifest=content_hash(names),ws_port=server.socket.getsockname()[1],http_port=http.port)
    write_json(scratch/'config.json',config)
    processes = {}; handles = []; load = None; parent_error = None

    def spawn(name,command):
        out = (scratch/(name+'.stdout')).open('wb'); err = (scratch/(name+'.stderr')).open('wb')
        handles.extend([out,err])
        child = subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=out,stderr=err,
            env={k:os.environ[k] for k in ('PATH','LANG','SYSTEMROOT') if k in os.environ})
        processes[name] = child
        return child

    def wait_file(name,child,seconds):
        deadline = time.monotonic()+seconds
        while not (scratch/name).exists():
            if child.poll() is not None:
                raise RuntimeError('SESSION_CHILD_EXIT_BEFORE_'+name)
            if time.monotonic()>=deadline:
                raise TimeoutError('SESSION_WAIT_TIMEOUT_'+name)
            time.sleep(.05)

    try:
        child = spawn('collector',[sys.executable,'-B',str(SELF),'--collector',str(scratch)])
        wait_file('subscriptions.ready',child,90)
        if with_reader:
            read_child = spawn('reader',[sys.executable,'-B',str(READER),str(scratch)])
            wait_file('reader.ready',read_child,10)
        start = time.monotonic()
        load = dict(start_mono=start,end_mono=start+SECONDS,target_seconds=SECONDS)
        write_json(scratch/'load.json',load)
        (scratch/'load.ready').write_text('ready',encoding='utf-8')
        while time.monotonic()<load['end_mono'] and child.poll() is None:
            time.sleep(.05)
        load['parent_observed_end_mono'] = time.monotonic()
        (scratch/'reader.stop').write_text('stop',encoding='utf-8')
        load['reader_join_started_mono'] = time.monotonic()
        if with_reader:
            processes['reader'].wait(timeout=25)
        load['reader_join_finished_mono'] = time.monotonic()
        # Collector drains throughout reader join and this additional allowance.
        drain_started = time.monotonic()
        load['additional_drain_started_mono'] = drain_started
        while child.poll() is None and time.monotonic()-drain_started<3:
            time.sleep(.05)
        (scratch/'collector.stop').write_text('stop',encoding='utf-8')
        load['collector_stop_requested_mono'] = time.monotonic()
        child.wait(timeout=45)
        load['collector_join_finished_mono'] = time.monotonic()
    except BaseException as error:
        parent_error = error_detail('parent.leg',error)
    finally:
        def cleanup(label,call):
            try:
                call()
            except BaseException as error:
                errors.append(error_detail(label,error))
        for name in ('reader','collector'):
            cleanup('stop.'+name,lambda name=name:(scratch/(name+'.stop')).write_text('stop',encoding='utf-8'))
        for name,child in processes.items():
            if child.poll() is None:
                try:
                    child.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    cleanup('kill.'+name,child.kill)
                    cleanup('reap.'+name,lambda child=child:child.wait(timeout=5))
                    errors.append(dict(operation='parent.cleanup',type='KilledChild',message=name))
        stopped.set()
        for peer in sockets:
            cleanup('websocket.close',peer.close)
        cleanup('websocket.shutdown',server.shutdown)
        cleanup('websocket.join',lambda:server_thread.join(5))
        cleanup('http.close',http.close)
        for handle in handles:
            cleanup('child.output.close',handle.close)
    result = dict(leg=scratch.name,fixture_only=True,production_rate_claim=False,strict_gate_replacement=False,
        topology='REAL_SESSION_WRITER_TICK_SAMPLE_WITH_EXTERNAL_READER_PROCESS',
        scope='NO_READER' if not with_reader else 'CONTINUOUS_1228_TOPIC_PRESSURE_NOT_MONITOR',
        topics=len(names),selected_sha256=content_hash(names),input_target_round_seconds=INTERVAL,
        load=load,producer_by_shard=sent,parent_error=parent_error,transport_errors=errors,
        http=dict(requests=http.requests,closed=http.closed,active=http.active,errors=http.errors,thread_alive=http.thread.is_alive()),
        websocket_server_thread_alive=server_thread.is_alive(),
        children={name:dict(pid=p.pid,returncode=p.returncode) for name,p in processes.items()})
    result['child_stderr_tail'] = {name:(scratch/(name+'.stderr')).read_text(encoding='utf-8',errors='replace')[-8192:]
                                   for name in processes}
    for name in ('collector','reader'):
        path = scratch/(name+'.result.json')
        result[name] = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
    c = result['collector'] or {}; final = c.get('final',{})
    r = result['reader'] or {}
    session_finals = [v for v in c.get('shutdowns',[]) if v.get('event')=='BINANCE_HEALTH_SESSION_FINAL']
    samples = [] if load is None else [s for s in c.get('samples',[]) if load['start_mono']<=s['mono']<=load['end_mono']]
    result['health_samples_within_load'] = len(samples)
    result['load_observed_seconds'] = (None if load is None or 'parent_observed_end_mono' not in load else
        max(0,min(load['parent_observed_end_mono'],load['end_mono'])-load['start_mono']))
    committed_load = c.get('stage_partitions',{}).get('load',{}).get('committed')
    result['load_committed_per_observed_second'] = (None if not result['load_observed_seconds'] or committed_load is None
        else committed_load/result['load_observed_seconds'])
    result['native_sqlite_phase_errors'] = native_sqlite_errors(final)+native_sqlite_errors(r)
    result['producer_sent'] = sum(v['frames'] for v in sent.values())
    result['session_final_received'] = sum(v.get('received',0) for v in session_finals)
    result['session_final_partition_valid'] = bool(session_finals) and all(v.get('complete') for v in session_finals)
    result['receipt_totals'] = {key:sum(v.get(key,0) for v in session_finals)
        for key in ('received','committed','duplicate','invalid','unprocessed_recorded','unattributed_invalid')}
    result['stage_partition_matches_durable_final'] = bool(session_finals) and all(
        sum(part.get(key,0) for part in c.get('stage_partitions',{}).values())==result['receipt_totals'][key]
        for key in ('committed','duplicate','invalid','unprocessed_recorded'))
    cutoff = c.get('load_cutoff')
    result['load_cutoff_matches_load_counter'] = bool(cutoff) and cutoff['completed']==c.get('stage_partitions',{}).get('load')
    result['reader_is_separate_process'] = (not with_reader or
        r.get('pid')==processes['reader'].pid and c.get('pid')==processes['collector'].pid and r.get('pid')!=c.get('pid'))
    result['diagnostic_completed'] = bool(c) and parent_error is None and (not with_reader or result['reader'] is not None)
    result['load_succeeded'] = bool(result['diagnostic_completed'] and c.get('session_succeeded')
        and len(samples)>=2 and not errors and not http.errors and all(p.returncode==0 for p in processes.values())
        and final.get('active_owners')==0 and final.get('requests')==0 and final.get('calls')==final.get('terminals')
        and final.get('orphan_files')==0 and final.get('connected')=='0' and not c.get('blocked_effects')
        and 'budget_error' not in c and http.active==0 and http.requests==http.closed and not http.thread.is_alive()
        and final.get('attempts')==final.get('retired') and not final.get('final_read_errors')
        and not result['native_sqlite_phase_errors'] and result['session_final_partition_valid']
        and result['stage_partition_matches_durable_final'] and result['load_cutoff_matches_load_counter']
        and all(result['receipt_totals'][key]==0 for key in ('invalid','unprocessed_recorded','unattributed_invalid'))
        and result['reader_is_separate_process'] and not server_thread.is_alive()
        and c.get('source_audit',{}).get('before_after_equal')
        and result['producer_sent']==result['session_final_received']
        and (not with_reader or r.get('ready',0)>0 and r.get('diagnostic_completed'))
        and c.get('queue_depth')==0 and load['parent_observed_end_mono']>=load['end_mono'])
    result['raw_files'] = {p.name:dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                           for p in sorted(scratch.iterdir()) if p.suffix in ('.stdout','.stderr','.json')}
    return result


def main():
    if sys.platform != 'linux':
        raise RuntimeError('SESSION_TOPOLOGY_LINUX_ONLY')
    if len(sys.argv)==3 and sys.argv[1]=='--collector':
        return collector(Path(sys.argv[2]))
    if len(sys.argv)!=1:
        raise ValueError('SESSION_TOPOLOGY_EXACT_ARGUMENTS_REQUIRED')
    # Resolve provenance before installing the fixed child allowlist.
    head = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if os.environ.get('CHECKOUT_SHA') != head:
        raise ValueError('SESSION_CHECKOUT_SHA_MUST_EQUAL_GIT_HEAD')
    scratch = Path(tempfile.mkdtemp(prefix='binance-session-topology-')).resolve()
    ports = set(); blocked = install_guard(scratch,'parent',ports)
    from tests.test_hunter_binance_coordinated_soak import emit_bounded
    audit_paths = sorted({p for folder in ('research','scripts','tests') for p in (ROOT/folder).rglob('*.py')})
    source = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in audit_paths}
    results = []
    for label in ('no_reader','with_reader'):
        try:
            result = run_leg(scratch/label,label=='with_reader',ports,head,source)
        except BaseException as error:
            from tests.fixtures.binance_reconnect_diagnostics import error_detail
            result = dict(leg=label,diagnostic_completed=False,load_succeeded=False,
                          problem=error_detail('parent.run_leg',error))
        write_json(scratch/(label+'.report.json'),result)
        emit_bounded('BINANCE_SESSION_TOPOLOGY_LEG',result)
        results.append(result)
    after = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in audit_paths}
    final = dict(event='BINANCE_SESSION_TOPOLOGY_FINAL',source_head=head,checkout_sha=os.environ['CHECKOUT_SHA'],
        source_sha256=source,source_before_after_equal=source==after,parent_source_audit=loaded_source_audit(source),
        scratch=str(scratch),blocked_effects=blocked,diagnostic_completed=all(r['diagnostic_completed'] for r in results),
        load_succeeded=source==after and all(r['load_succeeded'] for r in results),
        result_meaning='DIAGNOSTIC_COMPLETION_IS_NOT_LOAD_SUCCESS_OR_PRODUCTION_ACCEPTANCE')
    write_json(scratch/'summary.json',final)
    emit_bounded('BINANCE_SESSION_TOPOLOGY_FINAL',final)
    return 0 if final['diagnostic_completed'] and final['load_succeeded'] and source==after and not blocked else 1


if __name__ == '__main__':
    raise SystemExit(main())
