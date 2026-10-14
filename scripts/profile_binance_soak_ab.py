"""Five isolated, 300s minimum, unmodified-admission SOAK bottleneck diagnostics.

Never deploy. This fixture uses scratch SQLite, a fake offline exchange and NO
real orders/network. Failure is reported, never converted to a passing soak.
"""
import argparse
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import statistics
import subprocess
import sys
from threading import Event, Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.hunter_binance_stream_store import Store, STEPS, LIMITS, split_topic
from research import hunter_binance_stream as stream
from research import hunter_binance_stream_consumer as consumer
from tests import test_hunter_binance_coordinated_runtime as fx
from tests.test_hunter_binance_coordinated_soak import fixture_rows
from tests.test_hunter_binance_cache import message


def pct(values, percentage):
    if not values: return None
    data = sorted(values)
    return round(data[max(0, (len(data)*percentage+99)//100-1)], 4)


def summary(values):
    return dict(count=len(values), p50_ms=pct(values, 50), p99_ms=pct(values, 99),
                max_ms=round(max(values),4) if values else None)


def sql_copy(store, names, cut):
    """SQL retrieval only; no JSON decoding, proof, hash or post-parse validation."""
    count = 0
    with store.db(readonly=True) as db:
        count += len(db.execute('SELECT * FROM topics').fetchall())
        count += len(db.execute('SELECT key,value FROM meta').fetchall())
        for offset in range(0, len(names), 400):
            group = names[offset:offset+400]
            marks = ','.join('?' for _ in group)
            values = ','.join('(?)' for _ in group)
            read_q = ('WITH requested(topic) AS (VALUES '+values+') '
                'SELECT r.* FROM requested JOIN readiness r ON r.id=('
                'SELECT prior.id FROM readiness prior WHERE prior.topic=requested.topic '
                'AND prior.observed<=? ORDER BY prior.observed DESC,prior.id DESC LIMIT 1)')
            count += len(db.execute(read_q, (*group,cut)).fetchall())
            revision_q = ('''SELECT r.* FROM revisions r
                JOIN (SELECT topic,start,MAX(source_time) AS latest FROM revisions
                WHERE observed<=? AND source_time<=? AND topic IN ('''+marks+''')
                GROUP BY topic,start) chosen ON r.topic=chosen.topic AND
                r.start=chosen.start AND r.source_time=chosen.latest''')
            count += len(db.execute(revision_q,(cut,cut,*group)).fetchall())
    return count


def reader_loop(config, *, process=False):
    path = Path(config['database'])
    names = config['names']
    base = config['base_ms']; anchor = config['anchor_mono']
    seconds = config['seconds']; mode = config['mode']
    store = Store.reader(path)
    freeze_errors = {}
    def observe_freeze(err_type, message, sqlite_code, sqlite_name):
        key = str((err_type,message,sqlite_code,sqlite_name))
        freeze_errors[key] = freeze_errors.get(key,0)+1
    store._freeze_error_probe = observe_freeze
    clock = lambda: base+int((time.monotonic()-anchor)*1000)
    results = []; cpu = 0.; errors = {}
    count_rows = 0
    duration = time.monotonic()+seconds
    target = time.monotonic()
    while time.monotonic()<duration:
        now = base if mode == 'C' else clock()
        start = time.perf_counter()
        thread_start = time.thread_time()
        try:
            if mode == 'E':
                count_rows = sql_copy(store,names,now)
            else:
                obj=consumer.read(names,dt.datetime.fromtimestamp(now/1000,dt.timezone.utc),
                                  reader=store,clock=lambda:now)
                obj.check_delivery()
                count_rows = len(obj.generation['windows'])
        except BaseException as error:
            label = type(error).__name__+':'+str(error)[:200]
            errors[label]=errors.get(label,0)+1
        elapsed=(time.perf_counter()-start)*1000
        results.append(elapsed)
        cpu += time.thread_time()-thread_start
        target += .8
        wait = target-time.monotonic()
        if wait>0: time.sleep(wait)
        else: target=time.monotonic()
    return dict(read=summary(results), read_cpu_seconds=round(cpu,3),
                read_errors=errors, freeze_errors=freeze_errors,
                read_accepted=len(results)-sum(errors.values()), last_row_count=count_rows)


def run(config, output):
    mode=config['mode']
    seconds=config['seconds']
    names=config['names']
    base=config['base_ms']
    anchor=config['anchor_mono']
    clock=lambda:base+int((time.monotonic()-anchor)*1000)
    writer_stats = [];writer_cpu = [0.]
    received_count = committed_count = 0
    batch_count = 0
    queue_full = 0
    producer_cpu = [0.]
    directory_stats = []
    wal_metrics = dict(samples=0,last_bytes=0,max_bytes=0)
    errors=[]
    observed=stream.ObservedQueue(4096)
    def durable_counts(store):
        with store.db(readonly=True) as db:
            return [int(x or 0) for x in db.execute(
                'SELECT COALESCE(SUM(committed),0),COALESCE(SUM(duplicate),0),'
                'COALESCE(SUM(invalid),0),COALESCE(SUM(unprocessed_recorded),0) FROM bn_counts').fetchone()]
    stop=Event()
    producer_done=Event()
    case=fx.DataCases(methodName='test_utc_midnight_vietnam_seven_and_native_boundaries_require_new_ws')
    case.setUp()
    market=None
    reader_proc=None
    child_log=output.with_name(output.stem+'.reader.json')
    try:
        market=case.market('phase0-'+mode,names=names,stamp=clock())
        with closing(sqlite3.connect(market.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
        market.apply('directory',names,fx.directory(names),clock())
        for name in names:
            stamp=clock()
            market.apply('seed',[name],fixture_rows(split_topic(name)[1],stamp),stamp)
        market.frames(names,clock())
        # Seeding 1228 independent topics can consume most of proof freshness.
        # Re-verify the WHOLE directory with real test fixture evidence; do not
        # skip or alter any proof, expiry, publication, or consumer fence.
        market.apply('directory',names,fx.directory(names),clock())
        check_at=clock()
        validated=consumer.read(names,dt.datetime.fromtimestamp(check_at/1000,dt.timezone.utc),
                                reader=Store.reader(market.path),clock=lambda:check_at)
        validated.check_delivery()
        if len(validated.generation['windows'])!=len(names):
            raise AssertionError('PHASE0_INITIAL_FULL_SCOPE_NOT_READY')
        receipt_before = durable_counts(market.store)
        # Prep work is excluded from the 300 second measurement; no relaxed data gates.
        # Capture logical time BEFORE changing the closure's anchor.
        # A backwards rebasing invalidates WS event keys/proof validity and
        # produces misleading queue/reader results in ALL five groups.
        before_rebase=clock()
        anchor=time.monotonic()
        base=before_rebase
        config['anchor_mono']=anchor
        config['base_ms']=base
        clock=lambda:base+int((time.monotonic()-anchor)*1000)
        if clock()<before_rebase or clock()-before_rebase>5000:
            raise AssertionError('PHASE0_CLOCK_REBASE_INVALID')
        config['database']=str(market.path)
        wal=Path(str(market.path)+'-wal')
        def monitor_wal():
            while not stop.is_set():
                try:size=wal.stat().st_size
                except FileNotFoundError:size=0
                wal_metrics['last_bytes']=size
                wal_metrics['max_bytes']=max(size,wal_metrics['max_bytes'])
                wal_metrics['samples']+=1
                if stop.wait(.1):break
        def maintain_directory():
            next_at=anchor+30
            while next_at<anchor+seconds and not stop.is_set():
                if stop.wait(max(0,next_at-time.monotonic())):break
                started=time.perf_counter()
                try:
                    market.apply('directory',names,fx.directory(names),clock())
                except BaseException as error:
                    errors.append('DIRECTORY:'+repr(error));stop.set();break
                directory_stats.append((time.perf_counter()-started)*1000)
                next_at+=30
        def producer():
            nonlocal received_count,queue_full
            next_at=time.monotonic()
            previous={}
            while time.monotonic()<anchor+seconds and not stop.is_set():
                stamp=clock()
                start_cpu=time.thread_time()
                for name in names:
                    symbol,interval=split_topic(name)
                    shard=market.protocol.topic_shards[name]
                    bar_start=stamp//STEPS[interval]*STEPS[interval]
                    frames=[]
                    if name in previous and previous[name]!=bar_start:
                        frames.append((message(symbol,interval,stamp-1,
                                               start=previous[name],closed=True),stamp-1))
                    frames.append((message(symbol,interval,stamp),stamp))
                    for frame,received_at in frames:
                        try:
                            # Same receipt-first and bound as original strict soak.
                            market.protocol.arrived(shard,json.dumps(frame),received_at)
                            received_count+=1
                            observed.put((shard,frame,received_at),timeout=.5)
                        except queue.Full:
                            queue_full+=1
                            try:
                                market.store.unprocessed(market.epoch,[(frame,received_at)],'SOAK_QUEUE_FULL')
                            except BaseException as error:
                                errors.append('UNPROCESSED_RECORD_FAILED:'+repr(error))
                                stop.set()
                                break
                        except BaseException as error:
                            errors.append('PRODUCER:'+repr(error));stop.set();break
                    previous[name]=bar_start
                producer_cpu[0]+=time.thread_time()-start_cpu
                next_at+=.8
                pause=next_at-time.monotonic()
                if pause>0:stop.wait(pause)
                else:next_at=time.monotonic()
            producer_done.set()
        def writer():
            nonlocal committed_count,batch_count
            while not producer_done.is_set() or not observed.empty():
                try:batch=[observed.get(timeout=.05)]
                except queue.Empty:continue
                while len(batch)<100:
                    try:batch.append(observed.get_nowait())
                    except queue.Empty:break
                start=time.perf_counter()
                thread_start=time.thread_time()
                try:
                    market.protocol.receive_batch(batch)
                except BaseException as error:
                    errors.append('WRITER:'+repr(error));stop.set()
                    return
                writer_stats.append((time.perf_counter()-start)*1000)
                writer_cpu[0]+=time.thread_time()-thread_start
                committed_count+=len(batch);batch_count+=1
        writer_thread=Thread(target=writer,name='soak-writer')
        producer_thread=Thread(target=producer,name='soak-producer')
        directory_thread=Thread(target=maintain_directory,name='soak-directory')
        wal_thread=Thread(target=monitor_wal,name='soak-wal')
        wal_thread.start()
        if mode != 'C':
            writer_thread.start()
            producer_thread.start()
            directory_thread.start()
        if mode=='D':
            config_path=output.with_name(output.stem+'.config.json')
            config_path.write_text(json.dumps(config),encoding='utf8')
            reader_proc=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),
                '--child-reader',str(config_path),'--output',str(child_log)],
                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
            reader_stats=None
        elif mode=='B':
            reader_stats=dict(read=summary([]),read_cpu_seconds=0,read_errors={},last_row_count=None)
        else:
            reader_stats=reader_loop(config)
        # If no reader or a dead child, keep the group at least 300s.
        remaining=anchor+seconds-time.monotonic()
        if remaining>0:time.sleep(remaining)
        stop.set()
        wal_thread.join(3)
        if mode != 'C':
            directory_thread.join(30)
            producer_thread.join(30);writer_thread.join(30)
        if reader_proc is not None:
            try:_,stderr=reader_proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                reader_proc.kill();_,stderr=reader_proc.communicate()
            if reader_proc.returncode!=0:errors.append('READER_CHILD:'+str(stderr)[-500:])
            if child_log.exists():
                reader_stats=json.loads(child_log.read_text(encoding='utf8'))
        receipt_after=durable_counts(market.store)
        receipt_delta=[new-old for new,old in zip(receipt_after,receipt_before)]
        return dict(mode=mode,source_head=config['head'],manifest_sha256=config['manifest_sha256'],
            fixture_manifest_sha256=fx.MANIFEST, strict_topics=len(names),round_interval_seconds=.8,
            capacity=4096,actual_elapsed_seconds=round(time.monotonic()-anchor,3),
            queue_high_water=observed.high_water,queue_full_rejections=queue_full,
            received=received_count,committed_in_batches=committed_count,
            durable_outcome_initial=receipt_before,durable_outcome_final=receipt_after,
            durable_outcome_during_load=receipt_delta,
            receipt_conserved=(received_count==sum(receipt_delta)),
            write_batches=batch_count,
            writer=summary(writer_stats),writer_cpu_seconds=round(writer_cpu[0],3),
            producer_cpu_seconds=round(producer_cpu[0],3),
            reader=reader_stats, wal_file_bytes=wal.stat().st_size if wal.exists() else 0,
            wal_observations=wal_metrics,directory_maintenance=summary(directory_stats),
            market_transactions=market.store.transaction_diagnostics(),
            errors=errors,writer_thread_alive=writer_thread.is_alive() if mode!='C' else False,
            producer_thread_alive=producer_thread.is_alive() if mode!='C' else False,
            directory_thread_alive=directory_thread.is_alive() if mode!='C' else False)
    finally:
        case.doCleanups()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--mode',choices=('A','B','C','D','E'))
    p.add_argument('--seconds',type=int,default=300)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--child-reader',type=Path)
    opts=p.parse_args()
    if opts.child_reader:
        config=json.loads(opts.child_reader.read_text(encoding='utf8'))
        opts.output.write_text(json.dumps(reader_loop(config),sort_keys=True),encoding='utf8')
        return
    if opts.seconds<300: raise SystemExit('PHASE0_MINIMUM_300_SECONDS_REQUIRED')
    import subprocess as sub
    head=sub.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    names=fx.topics(['BTCUSDT']+['S%03dUSDT'%i for i in range(408)])
    if len(names)!=1228:raise SystemExit('TOPIC_COUNT_MISMATCH')
    hashes={}
    for name in ('research/hunter_binance_stream_store.py','research/hunter_binance_stream.py',
                 'research/hunter_binance_stream_consumer.py','research/hunter_binance_health_store.py'):
        hashes[name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    manifest=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
    cfg=dict(mode=opts.mode,seconds=opts.seconds,names=names,head=head,
             manifest_sha256=manifest,base_ms=fx.MIDNIGHT+120000,anchor_mono=time.monotonic())
    opts.output.parent.mkdir(parents=True,exist_ok=True)
    try:
        result=run(cfg,opts.output)
    except BaseException as error:
        result=dict(mode=opts.mode,source_head=head,manifest_sha256=manifest,error=repr(error))
        opts.output.write_text(json.dumps(result,sort_keys=True),encoding='utf8')
        raise
    opts.output.write_text(json.dumps(result,sort_keys=True),encoding='utf8')
    print('BINANCE_SOAK_PHASE0 '+json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__':main()
