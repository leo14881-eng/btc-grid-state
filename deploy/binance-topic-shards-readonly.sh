#!/usr/bin/env bash
set -eu
umask 077
/opt/hunter-binance-stream/venv/bin/python -B - --script "$0" <<'PY'
"""One consistent, read-only snapshot of the stopped Binance collector.

Run with its existing service account and Python -B; no collector imports,
service control, network, token output, SQLite checkpoint, or recovery action.
"""
import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

UNIT='hunter-binance-stream.service'
PREFIX=Path('/opt/hunter-binance-stream')
OUTPUT=Path('/tmp/hunter-binance-diagnostic-20261012-1719')
WINDOW=('2026-10-12 17:19:00','2026-10-12 17:22:00')
PROPERTIES=('ActiveState','SubState','MainPID','ControlGroup','Result','ExecMainCode','ExecMainStatus',
    'ExecMainStartTimestampMonotonic','ExecMainExitTimestampMonotonic','TasksCurrent')
CRITICAL_FILES=('research/hunter_binance_stream.py','research/hunter_binance_public.py',
    'research/hunter_binance_stream_store.py','research/hunter_bybit_public.py','research/hunter_bybit_stream_store.py')
KNOWN_FILES={
    '99836e5362340725f806d5df6df1ca9835065bdd':(
        '0ab6e6aaec164e03103572f2d7eb5dc4ee08b06a2ea51d30e4dbd8691975de4d',
        'a8339c8a495c5b94de232d6a06e01e4488538d483dd2327cea09cd0302af5948',
        '17f2181fbd398d58936cd3fb2b2ab37e2a2b16a921524243f2c1b28142143300',
        '84f645f61a27b47ba0d91d0c264901db1041257dee974bd36ed25fb2ff9b5c97',
        'c4a74256efaedf0f2ee502e68b0e74208f5327e80f17600cc7674af28d274c92'),
    '01c5c932fd6e169c9a451b751a2178cfb0bc2d0c':(
        '3bdd9020b7ab0b03ccf246a8c52c4cc224eb62b5d9abbc612cfed76e250c0b26',
        'ac05dd82bb9a41cce7e506867358bc06f0dece63376df104945df63c18e43188',
        '17f2181fbd398d58936cd3fb2b2ab37e2a2b16a921524243f2c1b28142143300',
        'dec5407612a37f12ddcfaf782989688f803a45543220c38d2e4be09ebc0b3b6b',
        'c4a74256efaedf0f2ee502e68b0e74208f5327e80f17600cc7674af28d274c92')}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def safe_code(value):
    return value if isinstance(value,str) and re.fullmatch('[A-Z][A-Z0-9_]{0,127}',value) else 'UNRECORDED_OR_REDACTED'


def utc(ms):
    return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat() if ms else None


def anonymous_owners(db,directory):
    counts={name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('requests','leases')}
    if max(counts.values())>16:raise ValueError('OWNER_REPORT_LIMIT_EXCEEDED')
    requests=dict(db.execute('SELECT token,denied FROM requests'))
    leases=dict(db.execute('SELECT token,acquired_ms FROM leases'))
    result=[]
    for number,token in enumerate(sorted(requests.keys()|leases.keys()),1):
        if not isinstance(token,str) or not re.fullmatch('[a-f0-9]{32}',token):
            raise ValueError('OWNER_TOKEN_FORMAT_INVALID')
        denied=requests.get(token);acquired=leases.get(token)
        if denied not in (None,0,1) or (acquired is not None and (type(acquired) is not int or acquired<0)):
            raise ValueError('OWNER_METADATA_INVALID')
        lock=directory/('request-'+token+'.lock')
        try:
            info=lock.lstat()
            lock_info=dict(exists=True,regular=stat.S_ISREG(info.st_mode),bytes=info.st_size,
                mtime_ns=info.st_mtime_ns,mode=oct(stat.S_IMODE(info.st_mode)))
        except FileNotFoundError:lock_info=dict(exists=False)
        result.append(dict(owner='owner-'+str(number),request_present=token in requests,
            lease_present=token in leases,denied=denied,acquired_ms=acquired,
            acquired_clock='BUDGET_CLOCK_DOMAIN_NOT_UTC',lock=lock_info,
            lock_held='NOT_PROBED',http_endpoint='NOT_RECORDED',http_outcome='UNKNOWN'))
    return counts,result


def inspect(directory):
    raw=(directory/'manifest.json').read_bytes();scan=json.loads(raw)
    symbols=[]
    for base,coin in scan['coins'].items():
        for pair in coin.get('pairs',[]):
            if pair.get('venue')=='binance':
                if pair.get('base')!=base or pair.get('pair')!=base+'USDT':
                    raise ValueError('MANIFEST_PAIR_MISMATCH')
                symbols.append(pair['pair'])
    if (not symbols or len(symbols)!=len(set(symbols)) or 'BTCUSDT' not in symbols
            or any(not isinstance(s,str) or not s.endswith('USDT')
                or not 1<=len(s[:-4])<=30 or not s.isalnum()
                or s.upper()!=s or s.lower().upper()!=s for s in symbols)):
        raise ValueError('MANIFEST_SYMBOLS_INVALID')
    # Match deployed topic()'s Unicode symbol domain, including CJK assets.
    # Exact deployed roster(): lexicographic topics, 500 per socket and 100
    # per SUBSCRIBE. Historical manifest age is evidence, not readiness.
    names=sorted([s.lower()+'@kline_'+i for s in symbols for i in ('15m','1h','1d')]+['btcusdt@kline_5m'])
    if len(names)>4000:raise ValueError('TOPIC_REPORT_LIMIT_EXCEEDED')
    # Even mode=ro can update the live WAL shm reader marks. Never open the
    # source with SQLite: capture bytes, then query an isolated temporary copy.
    def capture():
        result={}
        for name in ('state.sqlite3','state.sqlite3-wal','state.sqlite3-shm','manifest.json'):
            path=directory/name
            if not path.exists():
                if name in ('state.sqlite3','manifest.json'):raise ValueError('SOURCE_FILE_MISSING')
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size>256*1024*1024:
                raise ValueError('SOURCE_FILE_UNSAFE_OR_TOO_LARGE')
            result[name]=path.read_bytes()
        return result
    source=capture()
    if source.get('manifest.json')!=raw or capture()!=source:
        raise ValueError('SOURCE_CHANGED_DURING_CAPTURE')
    with tempfile.TemporaryDirectory(prefix='binance-readonly-snapshot-') as scratch:
        snapshot=Path(scratch)
        for name in ('state.sqlite3','state.sqlite3-wal'):
            if name in source:(snapshot/name).write_bytes(source[name])
        db=sqlite3.connect((snapshot/'state.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1)
        db.row_factory=sqlite3.Row
        try:
            query_deadline=time.monotonic()+15
            db.set_progress_handler(lambda:int(time.monotonic()>query_deadline),10000)
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('epoch','connected','scope_hash','roster_hash')"))
            rows=[dict(r) for r in db.execute('SELECT topic,epoch,ws_rx,ws_ts,ack,gap FROM topics LIMIT 4001')]
            owners,owner_details=anonymous_owners(db,directory)
            cache_counts={};cache_times={}
            for table in ('bars','revisions','readiness'):
                cache_counts[table]=[dict(r) for r in db.execute('SELECT CASE WHEN epoch=? THEN \'current\' ELSE \'other\' END AS session,COUNT(*) AS rows,MIN(observed) AS min_observed,MAX(observed) AS max_observed FROM '+table+' GROUP BY session',(meta.get('epoch'),))]
            for table in ('bars','revisions'):
                cache_times[table]=[dict(r) for r in db.execute('SELECT origin,confirmed,COUNT(*) AS rows,MIN(observed) AS min_observed,MAX(observed) AS max_observed,MIN(source_time) AS min_source_time,MAX(source_time) AS max_source_time FROM '+table+' WHERE epoch=? GROUP BY origin,confirmed',(meta.get('epoch'),))]
                for row in cache_times[table]:row['origin']=safe_code(row['origin'])
            budget=dict(db.execute('SELECT next_ms,cooldown_ms,last_ms,halt FROM budget WHERE id=1').fetchone())
            budget['halt']=safe_code(budget['halt']) if budget['halt'] else None
            budget['clock']='BUDGET_CLOCK_DOMAIN_NOT_UTC'
        finally:db.close()
    if capture()!=source:raise ValueError('SOURCE_CHANGED_DURING_DIAGNOSTIC')
    if (meta.get('scope_hash')!=digest(scan) or meta.get('roster_hash')!=digest(names)
            or sorted(r['topic'] for r in rows)!=names or any(r['epoch']!=meta.get('epoch') for r in rows)):
        raise ValueError('SNAPSHOT_MANIFEST_OR_EPOCH_MISMATCH')
    if not re.fullmatch('[a-f0-9]{32}',meta.get('epoch','')) or meta.get('connected') not in ('0','1'):
        raise ValueError('SNAPSHOT_META_INVALID')
    for value in (budget['next_ms'],budget['cooldown_ms'],budget['last_ms']):
        if type(value) is not int or value<0:raise ValueError('SNAPSHOT_BUDGET_INVALID')
    for items in (*cache_counts.values(),*cache_times.values()):
        for item in items:
            if any(value is not None and type(value) is not int for key,value in item.items() if key not in ('session','origin')):
                raise ValueError('SNAPSHOT_AGGREGATE_INVALID')
    rows={r['topic']:r for r in rows};groups=defaultdict(list);shards=defaultdict(list)
    for ordinal,name in enumerate(names):
        row=rows[name]
        if any(type(row[k]) is not int or row[k]<0 for k in ('ws_rx','ws_ts')):
            raise ValueError('INVALID_PERSISTED_TIMESTAMP')
        if any(row[k] not in (0,1) for k in ('ack','gap')):raise ValueError('INVALID_TOPIC_STATE')
        symbol,interval=name.split('@kline_')
        row.update(ordinal=ordinal,shard=ordinal//500,subscribe_batch=(ordinal%500)//100,interval=interval)
        groups[symbol].append(row);shards[row['shard']].append(row)
    summary=[]
    def summarize(items,**identity):
        seen=[r for r in items if r['ws_rx']>0]
        return dict(**identity,total=len(items),no_frame=sum(r['ws_rx']==0 for r in items),
            missing_intervals=dict(Counter(r['interval'] for r in items if r['ws_rx']==0)),
            final_ack=sum(r['ack']==1 for r in items),final_gap=sum(r['gap']==1 for r in items),
            first_topic=items[0]['topic'],last_topic=items[-1]['topic'],
            min_rx=min((r['ws_rx'] for r in seen),default=None),max_rx=max((r['ws_rx'] for r in seen),default=None))
    batch_groups=defaultdict(list);interval_groups=defaultdict(list)
    for shard,items in sorted(shards.items()):summary.append(summarize(items,shard=shard))
    for row in rows.values():
        batch_groups[(row['shard'],row['subscribe_batch'])].append(row)
        interval_groups[row['interval']].append(row)
    fields=('topic','ordinal','shard','subscribe_batch','ws_rx','ws_ts','ack','gap')
    affected=[dict(symbol=symbol,intervals=[{k:r[k] for k in fields} for r in items])
        for symbol,items in sorted(groups.items()) if any(r['ws_rx']==0 for r in items)]
    buckets=Counter((r['ws_rx']//10000*10000,r['ws_ts']//10000*10000) for r in rows.values())
    return dict(source_read_only=True,query_target='PRIVATE_TEMP_COPY_REMOVED_ON_EXIT',
        source_sha256={name:hashlib.sha256(data).hexdigest() for name,data in source.items()},
        epoch=meta['epoch'],connected=meta.get('connected'),owners=owners,owner_details=owner_details,
        cache_counts_by_session=cache_counts,current_epoch_cache_by_origin=cache_times,budget=budget,
        lock_metadata_scope='POINT_IN_TIME_LSTAT_ONLY; no contents or lock acquisition',
        manifest_sha256=hashlib.sha256(raw).hexdigest(),scope_hash=meta['scope_hash'],roster_hash=meta['roster_hash'],
        manifest_as_of=dt.datetime.fromisoformat(scan['as_of_utc'].replace('Z','+00:00')).isoformat(),topics=len(names),no_frame=sum(r['ws_rx']==0 for r in rows.values()),
        index_base=0,shards=summary,affected_symbols=affected,
        subscription_batches=[summarize(items,shard=shard,subscribe_batch=batch) for (shard,batch),items in sorted(batch_groups.items())],
        intervals=[summarize(items,interval=interval) for interval,items in sorted(interval_groups.items())],
        topic_receipts=[{k:r[k] for k in fields} for r in rows.values()],
        ws_10s_buckets=[dict(rx_ms=rx,ts_ms=ts,n=n) for (rx,ts),n in sorted(buckets.items())],
        last_receipt_range_utc=dict(first=utc(min((r['ws_rx'] for r in rows.values() if r['ws_rx']),default=0)),
            last=utc(max((r['ws_rx'] for r in rows.values()),default=0))),
        interpretation='Persisted receipt only; no first-stale cause or request outcome inferred.')


def command(args,*,source=None,timeout=20):
    # All callers use fixed read-only commands. Never include stderr/raw command
    # failures in the report: those can contain identities or local details.
    result=subprocess.run(args,input=source,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        timeout=timeout,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8','TZ':'UTC'})
    if len(result.stdout)>4*1024*1024:raise ValueError('COMMAND_REPORT_LIMIT_EXCEEDED')
    if result.returncode:raise ValueError('READ_ONLY_COMMAND_FAILED')
    return result.stdout


def service_state(run=command):
    raw=run(['systemctl','show',UNIT,*['--property='+p for p in PROPERTIES]]).decode()
    result={}
    for line in raw.splitlines():
        key,sep,value=line.partition('=')
        if sep and key in PROPERTIES:
            if not re.fullmatch(r'[A-Za-z0-9_./\[\] -]{0,200}',value):raise ValueError('SERVICE_PROPERTY_INVALID')
            result[key]=value
    group=result.get('ControlGroup','')
    if group:
        if not re.fullmatch(r'/system.slice/hunter-binance-stream\.service',group):
            raise ValueError('UNEXPECTED_SERVICE_CGROUP')
        path=Path('/sys/fs/cgroup'+group)/'cgroup.procs'
        try:
            pids=path.read_text().split()
            if not all(p.isdecimal() for p in pids):raise ValueError('CGROUP_PID_INVALID')
            result['cgroup_processes']=len(pids)
        except FileNotFoundError:result['cgroup_processes']=0
    else:result['cgroup_processes']=0
    return result


def stopped(state):
    return state.get('ActiveState') in ('failed','inactive') and state.get('MainPID')=='0' and state.get('cgroup_processes')==0


def deployment(prefix=PREFIX):
    marker=(prefix/'release-head').read_text().strip()
    if not re.fullmatch('[0-9a-f]{40}',marker):raise ValueError('RELEASE_MARKER_INVALID')
    hashes={}
    for name in CRITICAL_FILES:
        path=prefix/'code'/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024:
            raise ValueError('RUNTIME_FILE_INVALID')
        hashes[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    expected=KNOWN_FILES.get(marker)
    return dict(release_head=marker,critical_file_sha256=hashes,
        critical_files_match_known_release=(tuple(hashes.values())==expected) if expected else None,
        scope='Five critical files only; not a full package attestation')


def safe_topic(value):
    if not isinstance(value,str) or len(value)>50:return False
    pieces=value.split('@kline_')
    if len(pieces)!=2 or pieces[1] not in ('5m','15m','1h','1d'):return False
    symbol=pieces[0].upper()
    return (symbol.endswith('USDT') and 1<=len(symbol[:-4])<=30 and symbol.isalnum()
        and symbol.lower()==pieces[0] and symbol.lower().upper()==symbol)


def sanitized_journal(raw):
    records=[]
    for line in raw.splitlines():
        if len(records)>=200:break
        item=json.loads(line);message=item.get('MESSAGE','')
        stamp=item.get('__REALTIME_TIMESTAMP','')
        row=dict(journal_us=int(stamp) if isinstance(stamp,str) and stamp.isdecimal() else None)
        if not isinstance(message,str):message=''
        row['message_sha256']=hashlib.sha256(message.encode()).hexdigest()
        try:value=json.loads(message)
        except (ValueError,TypeError):value=None
        evidence={}
        if isinstance(value,dict):
            for key in ('event','reason','error_code','status','capital_authority'):
                if key in value:evidence[key]=safe_code(value[key])
            for key in ('topics','failures','session_age_ms','ws_rx','ws_ts','ingress_rx','ack','gap','queued','active_repairs','shard'):
                if type(value.get(key)) is int:evidence[key]=value[key]
            for key in ('topic','failed_topic'):
                if safe_topic(value.get(key)):evidence[key]=value[key]
            for key,size in (('epoch',32),('source_head',40),('manifest_sha256',64),('roster_hash',64),('generation_sha256',64)):
                if isinstance(value.get(key),str) and re.fullmatch('[a-f0-9]{'+str(size)+'}',value[key]):evidence[key]=value[key]
            for key in ('shadow_only','real_trading_enabled','recovery_budget_restored'):
                if type(value.get(key)) is bool:evidence[key]=value[key]
        else:
            error=re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9_.]*Error|[A-Za-z_][A-Za-z0-9_.]*Exception): ([A-Z][A-Z0-9_]{0,127})',message.strip())
            if error:evidence['exception_code']=error.group(1)
        row['evidence']=evidence or dict(unparsed='RAW_MESSAGE_OMITTED')
        records.append(row)
    return dict(since_utc=WINDOW[0],until_utc=WINDOW[1],limit=200,records=records,
        at_limit=len(records)==200,privacy='Allowlisted structured fields/error codes only; all other raw text omitted')


def collect_report(source,run=command,state_reader=service_state,release_reader=deployment):
    report=dict(schema='binance_incident_readonly_v2',captured_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        actions=dict(service_control=False,recovery=False,owner_cleanup=False,market_http=False),errors=[])
    def attempt(name,action):
        try:report[name]=action()
        except Exception as error:report['errors'].append(dict(step=name,error_type=type(error).__name__,code=safe_code(str(error))))
    attempt('service_before',lambda:state_reader(run))
    attempt('deployment',release_reader)
    attempt('incident_journal',lambda:sanitized_journal(run(['journalctl','--utc','--no-pager','-u',UNIT,
        '--since',WINDOW[0],'--until',WINDOW[1],'-n','200','-o','json','--output-fields=MESSAGE,__REALTIME_TIMESTAMP'])))
    before=report.get('service_before',{})
    if stopped(before):
        attempt('cache',lambda:json.loads(run(['runuser','-u','hunter-binance-stream','--',str(PREFIX/'venv/bin/python'),
            '-B','-','--cache-only'],source=source,timeout=60)))
        if 'diagnostic_error' in report.get('cache',{}):
            report['errors'].append(dict(step='cache',**report['cache']['diagnostic_error']))
    else:report['errors'].append(dict(step='cache',code='STOPPED_SERVICE_REQUIRED'))
    attempt('service_after',lambda:state_reader(run))
    if report.get('service_after')!=before or not stopped(report.get('service_after',{})):
        report['errors'].append(dict(step='service_consistency',code='SERVICE_CHANGED_OR_NOT_STOPPED'))
    details=[r for r in report.get('incident_journal',{}).get('records',[]) if r['evidence'].get('event')=='BINANCE_TOPIC_STALE_DETAIL']
    report['historical_processing_evidence']=dict(stale_detail_events=details,
        first_stale_topic='AVAILABLE_IN_DETAIL_EVENTS' if details else 'NOT_RECORDED_IN_AVAILABLE_LOGS',
        queue_depth='AVAILABLE_AT_DETAIL_EVENT_ONLY' if details else 'NOT_RECORDED',
        per_topic_receiver_count='NOT_RECORDED',queue_high_water='NOT_RECORDED',
        batch_commit_duration='NOT_RECORDED',http_owner_topic_or_response='NOT_RECORDED',
        durable_times='Pump receipt/source timestamps, not SQLite commit timestamps',
        warning='Final ACK/gap were reset by disconnect; buckets count last persisted topic receipts, not throughput; recovery log occurs after teardown.')
    report['status']='COMPLETE' if not report['errors'] else 'PARTIAL'
    return report


def summary(report):
    state=report.get('service_after',{});release=report.get('deployment',{});cache=report.get('cache',{})
    lines=['BINANCE READ-ONLY INCIDENT REPORT: '+report['status'],
        'service='+state.get('ActiveState','UNKNOWN')+'/'+state.get('SubState','UNKNOWN')+' MainPID='+state.get('MainPID','UNKNOWN'),
        'release_head='+release.get('release_head','UNKNOWN')+' critical_files_match='+str(release.get('critical_files_match_known_release')),
        'topics='+str(cache.get('topics','UNKNOWN'))+' no_persisted_frame='+str(cache.get('no_frame','UNKNOWN'))+' owners='+json.dumps(cache.get('owners',{}),sort_keys=True)]
    for shard in cache.get('shards',[]):lines.append('shard='+str(shard['shard'])+' total='+str(shard['total'])+' no_persisted_frame='+str(shard['no_frame']))
    codes=sorted({e for row in report.get('incident_journal',{}).get('records',[]) for key,e in row['evidence'].items() if key in ('error_code','exception_code')})
    lines+=['incident_codes='+','.join(codes),'historical queue/commit timing: see explicit NOT_RECORDED fields in full report',
        'No HTTP, service start, recovery, or unresolved-owner cleanup. No first-stale cause inferred.']
    if report['errors']:lines.append('diagnostic_errors='+json.dumps(report['errors'],sort_keys=True))
    return '\n'.join(lines)+'\n'


def save_report(report,base=OUTPUT):
    full=json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
    if len(full.encode())>4*1024*1024:raise ValueError('FINAL_REPORT_LIMIT_EXCEEDED')
    short=summary(report)
    for suffix,content in (('.report.json',full),('.summary.txt',short)):
        target=Path(str(base)+suffix)
        # mkstemp is mode 0600. Replacing the named /tmp output never follows an
        # existing symlink; only diagnostic output paths can be replaced.
        fd,temp=tempfile.mkstemp(prefix='.binance-diagnostic-',dir=target.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as handle:
                handle.write(content);handle.flush();os.fsync(handle.fileno())
            os.replace(temp,target)
        finally:
            if os.path.exists(temp):os.unlink(temp)
    return short,hashlib.sha256(full.encode()).hexdigest()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=Path('/var/lib/hunter-binance-stream'))
    parser.add_argument('--cache-only',action='store_true')
    parser.add_argument('--script',type=Path)
    args=parser.parse_args()
    if args.cache_only:
        try:value=inspect(args.state_dir)
        except Exception as error:value=dict(diagnostic_error=dict(error_type=type(error).__name__,code=safe_code(str(error))))
        print(json.dumps(value,ensure_ascii=False,separators=(',',':')))
    else:
        # Reuse this verified embedded program under the existing service user.
        source=args.script.read_text(encoding='utf-8').split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0].encode()
        report=collect_report(source)
        report['diagnostic_program_sha256']=hashlib.sha256(source).hexdigest()
        short,checksum=save_report(report)
        print(short,end='')
        print('REPORT='+str(OUTPUT)+'.report.json')
        print('SUMMARY='+str(OUTPUT)+'.summary.txt')
        print('REPORT_SHA256='+checksum)

PY
