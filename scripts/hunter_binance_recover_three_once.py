"""Authorized incident only: exact upgrade, one public GET, audited orphan disposition.

Default is plan. Production requires --run through the existing root terminal.
No alternate endpoint, automatic HTTP retry, Bybit change or consumer selection.
"""
import argparse
import datetime as dt
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import types
import uuid

REPO = Path('/opt/shadow-runner/btc-grid-state')
PREFIX = Path('/opt/hunter-binance-stream')
STATE = Path('/var/lib/hunter-binance-stream')
SERVICE = 'hunter-binance-stream.service'
EPOCH = '51c723c224194317a49329ba8751d77f'
OLD = '99836e5362340725f806d5df6df1ca9835065bdd'
NEW = '8cb92bd7e39251e1997e1b363a5e9500c8344268'
INCIDENT_REPORT = Path('/tmp/hunter-binance-diagnostic-20261012-1719.report.json')
REPORT_HASH = '7c05690bbe062fec2638ab5f6b61fe35cd40617306acf35a9f25d383900c1f15'
ACQUIRED = [589226044,589226249,589226469]
UPGRADER = 'db0d59e3f7a6f15d1f99a678c07c2336e4474867'
UPGRADER_HASH = 'e0615746c73809d258b858f4058cbd346b2df9e011a8d7df819f8e5f77808ddc'
SCAN_CHECK = """import datetime as d,json,sys
s=json.load(sys.stdin); now=d.datetime.now(d.timezone.utc)
print(json.dumps(dict(now_utc=now.isoformat(),as_of_utc=s.get('as_of_utc'),
    binance_complete=s.get('binance_complete'),generation_id=s.get('generation_id'))),flush=True)
from research.hunter_binance_stream_store import roster
roster(s,now)
stamp=d.datetime.fromisoformat(s['as_of_utc'].replace('Z','+00:00'))
assert 0 <= (now-stamp).total_seconds() <= 1200, 'NATURAL_SCAN_TOO_OLD_WAIT_FOR_NEXT_CYCLE'
print('FRESH_SCOPE_OK')
"""


def require(ok, reason):
    if not ok: raise RuntimeError(reason)


def command(args, *, cwd=None, data=None, check=True, timeout=60):
    result = subprocess.run([str(x) for x in args], cwd=cwd, input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
    if check: require(result.returncode == 0, 'COMMAND_FAILED_'+Path(str(args[0])).name)
    return result


def helper():
    source = command(['git','-C',REPO,'show',UPGRADER+':scripts/hunter_binance_upgrade.py']).stdout
    require(hashlib.sha256(source).hexdigest() == UPGRADER_HASH, 'UPGRADE_ENTRY_HASH_MISMATCH')
    module = types.ModuleType('verified_binance_upgrade')
    exec(compile(source, '<verified-upgrader>', 'exec'), module.__dict__)
    module.OLD,module.NEW=OLD,NEW
    return module


def verify_report_source(upgrade):
    upgrade.regular(INCIDENT_REPORT)
    raw=INCIDENT_REPORT.read_bytes()
    require(hashlib.sha256(raw).hexdigest()==REPORT_HASH,'INCIDENT_REPORT_HASH_MISMATCH')
    report=json.loads(raw)
    require(report['status']=='COMPLETE' and not report['errors'] and report['cache']['epoch']==EPOCH,
            'INCIDENT_REPORT_NOT_COMPLETE')
    names=('state.sqlite3','state.sqlite3-wal','state.sqlite3-shm','state.sqlite3-journal','manifest.json')
    actual={}
    for name in names:
        path=STATE/name
        if path.exists():
            upgrade.regular(path);actual[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    require(actual==report['cache']['source_sha256'],'INCIDENT_SOURCE_BYTES_CHANGED')
    return report


def read_state(path=None):
    import contextlib,json,sqlite3
    from pathlib import Path
    source=Path('/var/lib/hunter-binance-stream/state.sqlite3') if path is None else path
    with contextlib.closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row; db.execute('BEGIN')
        value=dict(meta=dict(db.execute('SELECT key,value FROM meta')),
            budget=dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone()),
            clock=[dict(x) for x in db.execute('SELECT * FROM budget_clock')],
            requests=[dict(x) for x in db.execute('SELECT * FROM requests ORDER BY token')],
            leases=[dict(x) for x in db.execute('SELECT * FROM leases ORDER BY token')])
    if path is not None:return value
    print(json.dumps(value,sort_keys=True)); return 0


def consistent_backup(path=None):
    import contextlib,json,os,sqlite3,tempfile
    from pathlib import Path
    source=Path('/var/lib/hunter-binance-stream/state.sqlite3') if path is None else path
    fd,name=tempfile.mkstemp(prefix='recovery-evidence-',suffix='.sqlite3',dir=source.parent)
    os.close(fd)
    with contextlib.closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as original:
        with contextlib.closing(sqlite3.connect(name)) as target:
            original.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('BACKUP_INTEGRITY_FAILED')
    report=dict(path=name,integrity='ok')
    if path is not None:return report
    print(json.dumps(report)); return 0


def validate_incident(value, boot, monotonic_ms):
    require(value['meta'].get('connected') == '0' and value['meta'].get('epoch') == EPOCH, 'INCIDENT_EPOCH_OR_CONNECTION_CHANGED')
    require(value['clock'] == [{'id':1,'boot':boot}], 'BUDGET_CLOCK_REVIEW_REQUIRED')
    require(value['budget']['halt'] is None, 'PERSISTENT_HALT_PRESERVED')
    require(value['budget']['cooldown_ms'] <= monotonic_ms, 'COOLDOWN_STILL_ACTIVE')
    require(len(value['requests']) == len(value['leases']) == 3, 'EXPECTED_THREE_ORPHANS_CHANGED')
    import re
    require(len({x['token'] for x in value['requests']})==3,'DUPLICATE_INCIDENT_OWNER')
    require(sorted(x['acquired_ms'] for x in value['leases'])==ACQUIRED,'INCIDENT_ACQUISITIONS_CHANGED')
    require(all(re.fullmatch('[a-f0-9]{32}',request['token']) and request['denied']==0
                and request['token']==lease['token']
                for request,lease in zip(value['requests'],value['leases'])),'ORPHAN_OR_DENIAL_CHANGED')


def probe_once():
    # Runs as the existing collector UID. A complete transport return includes
    # kill/wait/drain, as verified by the fixed public-transport tests.
    import hashlib,json,time
    from research import hunter_binance_public as public
    from research.hunter_binance_stream_store import valid_row
    receipt=dict(http_attempts=0,success=False,process_reaped=False)
    cancelled=[False]
    requested=time.time_ns()//1000000
    def one(url,deadline,cancel,on_denial,before_go):
        if receipt['http_attempts']: raise public.DurableStopError('SECOND_HTTP_ATTEMPT_FORBIDDEN')
        receipt['http_attempts']+=1
        status,payload,retry=public.exchange(url,deadline,cancel,on_denial,before_go)
        receipt.update(http_status=status,process_reaped=True,retry_after_reported_seconds=retry,
                       retry_after_may_be_capped=(retry==600),payload_sha256=hashlib.sha256(payload).hexdigest())
        if status != 200: cancelled[0]=True  # Stop before Client can admit attempt 2.
        return status,payload,retry
    try:
        rows=public.Client(transport=one).request('/api/v3/klines',dict(symbol='BTCUSDT',interval='5m',
            limit=16,endTime=requested),timeout=30,cancelled=lambda:cancelled[0])
        checked=[valid_row(row,'5m') for row in rows]
        expected=[(requested//300000-i)*300000 for i in reversed(range(16))]
        if [row[0] for row in checked] != expected: raise ValueError('PROBE_WINDOW_INVALID')
        if not 0 <= time.time_ns()//1000000-requested <= 30000: raise TimeoutError('PROBE_FINISHED_LATE')
        receipt.update(success=True,rows=16)
    except BaseException as error:
        receipt.update(error_type=type(error).__name__)
    print(json.dumps(receipt,sort_keys=True),flush=True)
    return 0 if receipt['success'] else 1


def archive_three():
    # Runs as the existing collector UID to preserve SQLite/WAL ownership.
    import datetime as dt,json,sys,time
    from pathlib import Path
    from research.hunter_binance_stream_store import Store
    bundle=json.load(sys.stdin);expected=bundle['before']
    import re
    if (expected['meta'].get('epoch')!='51c723c224194317a49329ba8751d77f'
            or not re.fullmatch('[a-f0-9]{64}',bundle['evidence_sha256'])):
        raise RuntimeError('DISPOSITION_EVIDENCE_INVALID')
    with Store.reader().db() as db:
        actual=dict(meta=dict(db.execute('SELECT key,value FROM meta')),
            budget=dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone()),
            clock=[dict(x) for x in db.execute('SELECT * FROM budget_clock')],
            requests=[dict(x) for x in db.execute('SELECT * FROM requests ORDER BY token')],
            leases=[dict(x) for x in db.execute('SELECT * FROM leases ORDER BY token')])
        if any(actual[k] != expected[k] for k in ('meta','clock','requests','leases')):
            raise RuntimeError('ORPHAN_COMPARE_AND_SWAP_FAILED')
        if (actual['budget']['halt'] is not None or actual['budget']['cooldown_ms'] > time.monotonic_ns()//1000000
                or actual['budget']['cooldown_ms'] != expected['budget']['cooldown_ms']
                or actual['clock'][0]['boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                or any(actual['budget'][k] < expected['budget'][k] for k in ('next_ms','last_ms'))):
            raise RuntimeError('BUDGET_CHANGED_OR_STOPPED')
        if len(actual['requests'])!=3 or len(actual['leases'])!=3:
            raise RuntimeError('EXPECTED_THREE_ORPHANS_CHANGED')
        db.execute('''CREATE TABLE IF NOT EXISTS manual_http_dispositions (
            incident_epoch TEXT NOT NULL, token TEXT NOT NULL, acquired_ms INTEGER NOT NULL,
            denied INTEGER NOT NULL, historical_outcome TEXT NOT NULL CHECK(historical_outcome='UNKNOWN'),
            disposition TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, disposed_at_utc TEXT NOT NULL,
            PRIMARY KEY(incident_epoch,token))''')
        stamp=dt.datetime.now(dt.timezone.utc).isoformat()
        for request,lease in zip(expected['requests'],expected['leases']):
            token=request['token'];acquired=lease['acquired_ms']
            db.execute('INSERT INTO manual_http_dispositions VALUES(?,?,?,?,?,?,?,?)',
                (expected['meta']['epoch'],token,acquired,request['denied'],'UNKNOWN',
                 'USER_AUTHORIZED_BLOCKER_RETIRED',bundle['evidence_sha256'],stamp))
            if db.execute('DELETE FROM requests WHERE token=? AND denied=0',(token,)).rowcount != 1:
                raise RuntimeError('REQUEST_DELETE_COUNT_MISMATCH')
            if db.execute('DELETE FROM leases WHERE token=? AND acquired_ms=?',(token,acquired)).rowcount != 1:
                raise RuntimeError('LEASE_DELETE_COUNT_MISMATCH')
    print(json.dumps(dict(status='ARCHIVED_UNKNOWN_AND_RETIRED_ACTIVE_BLOCKERS',records=3,
                         historical_outcome='UNKNOWN')))


def prepare_manifest():
    import datetime as dt,json,sys
    from scripts.hunter_binance_stream_ops import prepare,atomic_json,STATE_PATH
    value=json.load(sys.stdin); snapshot=value['snapshot']
    receipt=prepare(snapshot,value['source_head'],dt.datetime.now(dt.timezone.utc))
    archive=STATE_PATH.parent/'manifests'; archive.mkdir(exist_ok=True)
    target=archive/(receipt['receipt_sha256']+'.json')
    if not target.exists(): atomic_json(target,dict(receipt=receipt,snapshot=snapshot))
    atomic_json(STATE_PATH.parent/'manifest.json',snapshot)
    print(json.dumps(dict(status='PREPARED_NOT_STARTED',receipt=receipt),sort_keys=True))


def child(function, data=None, check=True, timeout=50):
    source=inspect.getsource(function)+'\nraise SystemExit('+function.__name__+'())\n'
    return command(['runuser','-u','hunter-binance-stream','--',PREFIX/'venv/bin/python',
                    '-E','-s','-B','-c',source],cwd=PREFIX/'code',data=data,check=check,timeout=timeout)


def save(upgrade, work, name, value):
    upgrade.write_new(work/name,json.dumps(value,sort_keys=True).encode())
    upgrade.sync_dir(work)


def fresh_scan(upgrade):
    reference='refs/hunter-recovery/'+uuid.uuid4().hex+'/main'
    command(['git','-C',REPO,'fetch','--no-tags','--no-write-fetch-head','--refmap=',
             'origin','main:'+reference])
    head=command(['git','-C',REPO,'rev-parse',reference]).stdout.decode().strip()
    data=command(['git','-C',REPO,'show',head+':research/results/hunter-cex-universe-latest.json']).stdout
    # Validate without state writes. A 20-minute admission limit leaves startup headroom.
    result=command([PREFIX/'venv/bin/python','-E','-s','-B','-c',SCAN_CHECK],
                   cwd=PREFIX/'code',data=data,check=False)
    # This child only validates public scan data. Print its bounded diagnostics
    # before raising; do not expose arbitrary command arguments or Git stderr.
    print(json.dumps(dict(stage='FRESH_SCAN_VALIDATION',source_head=head,returncode=result.returncode,
        stdout=result.stdout.decode('utf-8','replace')[-8192:],
        stderr=result.stderr.decode('utf-8','replace')[-8192:]),sort_keys=True),flush=True)
    require(result.returncode==0,'FRESH_SCAN_VALIDATION_FAILED_BEFORE_MUTATION_OR_PROBE')
    return dict(source_head=head,snapshot=json.loads(data))


def apply():
    upgrade=helper()
    require(sys.platform=='linux' and os.geteuid()==0,'EXISTING_ROOT_LINUX_ENTRY_REQUIRED')
    old,new=upgrade.releases(REPO)
    upgrade.check_host(old)
    source=fresh_scan(upgrade)  # Fail stale scope before changing code or using market HTTP.
    attempt=PREFIX/('recovery-once-'+EPOCH+'.json')
    require(not os.path.lexists(attempt),'INCIDENT_ALREADY_ATTEMPTED_MANUAL_REVIEW_REQUIRED')
    with upgrade.maintenance():
        verify_report_source(upgrade)  # Byte-bound to the uploaded report before opening live SQLite.
        # Even read-only SQLite handles can create WAL/SHM side files. Keep all
        # live SQLite access under the existing collector UID, never root.
        before=json.loads(child(read_state).stdout)
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        validate_incident(before,boot,time.monotonic_ns()//1000000)
        work=PREFIX/('upgrade-'+uuid.uuid4().hex); work.mkdir(mode=0o700)
        print(json.dumps(dict(stage='EVIDENCE',directory=str(work))),flush=True)
        hashes=upgrade.state_evidence(work)
        upgrade.write_new(work/'incident-report.json',INCIDENT_REPORT.read_bytes())
        # A consistent SQLite backup supplements the untouched raw DB/WAL/SHM copies.
        backup=json.loads(child(consistent_backup).stdout)
        source_backup=Path(backup['path'])
        require(source_backup.parent==STATE and source_backup.name.startswith('recovery-evidence-')
                and source_backup.suffix=='.sqlite3' and backup['integrity']=='ok','BACKUP_PATH_INVALID')
        upgrade.regular(source_backup); upgrade.move(source_backup,work/'state-consistent.sqlite3')
        save(upgrade,work,'incident-before.json',before)
        save(upgrade,work,'source.json',source)
        save(upgrade,work,'receipt.json',dict(old=upgrade.OLD,new=upgrade.NEW,state_hashes=hashes,
            old_files=upgrade.tree_manifest(old),new_files=upgrade.tree_manifest(new)))
        _,staged,_=upgrade.code_paths(PREFIX,work)
        upgrade.stage(staged,new); upgrade.write_new(work/'new-release-head',(upgrade.NEW+'\n').encode(),0o444)
        upgrade.quiet(); upgrade.switch(PREFIX,work,old,new)
        # Durable, exclusive attempt receipt: rerunning cannot issue a second probe.
        save(upgrade,PREFIX,attempt.name,dict(epoch=EPOCH,evidence=str(work),
            authorization='USER_APPROVED_THREE_UNKNOWN_ARCHIVE_ONE_PROBE_ONE_START',historical_outcome='UNKNOWN'))
        upgrade.quiet()
        print(json.dumps(dict(stage='ONE_PUBLIC_GET',code_release=upgrade.NEW)),flush=True)
        result=child(probe_once,check=False)
        upgrade.write_new(work/'probe.stdout',result.stdout); upgrade.write_new(work/'probe.stderr',result.stderr)
        receipt=json.loads(result.stdout)
        print(json.dumps(dict(stage='PROBE_RESULT',receipt=receipt)),flush=True)
        require(result.returncode==0 and receipt.get('success') is True and receipt.get('http_status')==200
                and receipt.get('http_attempts')==1 and receipt.get('process_reaped') is True,'PROBE_FAILED_OR_UNKNOWN_STOPPED')
        upgrade.quiet()
        after=json.loads(child(read_state).stdout)
        validate_incident(after,boot,time.monotonic_ns()//1000000)
        require(all(after[k]==before[k] for k in ('meta','clock','requests','leases')),'INCIDENT_CHANGED_AFTER_PROBE')
        prepared=child(prepare_manifest,json.dumps(source).encode())
        upgrade.write_new(work/'manifest-receipt.json',prepared.stdout)
        save(upgrade,work,'disposition-intent.json',dict(original=before,probe=receipt,
             disposition='USER_AUTHORIZED_BLOCKER_RETIRED',historical_outcome='UNKNOWN',records=3))
        evidence_hash=hashlib.sha256((work/'disposition-intent.json').read_bytes()).hexdigest()
        done=child(archive_three,json.dumps(dict(before=before,evidence_sha256=evidence_hash)).encode())
        upgrade.write_new(work/'disposition-complete.json',done.stdout)
        upgrade.sync_dir(work)
        upgrade.quiet()
    # Only now release maintenance locks. The wrapper admits exactly one start.
    prepared=json.loads(prepared.stdout)['receipt']
    return supervise(upgrade,work,prepared)


def acceptance_sample():
    import json,sys,time
    from research.hunter_binance_stream_store import Store,Budget,roster_hash
    expected=json.load(sys.stdin);reader=Store.reader();now=time.time_ns()//1000000
    generation=reader.freeze(expected['topics'],now,now,expected_roster=expected['roster_hash'])
    finished=time.time_ns()//1000000
    budget=Budget(reader).status()
    with reader.db(readonly=True) as db:
        owners={name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('requests','leases')}
        archived=db.execute("SELECT COUNT(*) FROM manual_http_dispositions WHERE incident_epoch=? AND historical_outcome='UNKNOWN'",
                            ('51c723c224194317a49329ba8751d77f',)).fetchone()[0]
    ready=(generation['status']=='READY' and 0<=finished-now<=90000
           and generation.get('manifest_sha256')==expected['snapshot_sha256']
           and len(generation.get('windows',{}))==len(expected['topics']) and archived==3
           and not any(owners.values()) and not budget['halt'] and not budget['cooldown_ms'])
    print(json.dumps(dict(status='READY' if ready else 'UNKNOWN',epoch=generation.get('epoch'),
        reason=generation.get('reason'),failed_topic=generation.get('failed_topic'),owners=owners,
        archived_unknown=archived,halt=budget['halt'],cooldown_active=bool(budget['cooldown_ms']),
        generation_sha256=generation.get('generation_sha256'),roster_hash=expected['roster_hash'],
        expected_topics=len(expected['topics']),windows=len(generation.get('windows',{})),
        read_duration_ms=finished-now,publication_verified=False,shadow_only=True,
        real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY'),sort_keys=True))


def service_state(timeout=60):
    data=command(['systemctl','--no-pager','show',SERVICE,'-p','ActiveState','-p','SubState','-p','MainPID'],timeout=timeout).stdout
    return dict(line.split('=',1) for line in data.decode().splitlines())


def journal_evidence(since,timeout=30):
    import re
    raw=command(['journalctl','--utc','--no-pager','-u',SERVICE,'--since',since,
                 '-n','10000','-o','json'],timeout=timeout).stdout
    allowed={'event','epoch','topic','shard','subscribe_batch','received','processed','changed','last_rx',
        'last_processed_rx','offset','total','topics','final','zero_received','zero_processed','acked_batches',
        'committed_batches','batch_commit_max_us','receipt_to_commit_max_ms','receipt_clock_anomalies',
        'queued','queue_high_water','active_repairs','audit_output_failures','request_number','attempt','go',
        'response_complete','process_reaped','owner_retired','http_status','error_type','error_code','status',
        'session_age_ms','ws_rx','ws_ts','ingress_rx','ack','gap','source_head','manifest_sha256','roster_hash',
        'generation_sha256','reason','failed_topic','recovery_budget_restored','shadow_only','real_trading_enabled',
        'capital_authority','failures','observed_at_ms','ready_window_seconds'}
    def clean(value):
        if isinstance(value,dict):return {k:clean(v) for k,v in value.items() if k in allowed}
        if isinstance(value,list):return [clean(v) for v in value[:50]]
        if isinstance(value,str):return value[:180]
        return value if value is None or type(value) in (int,bool) else None
    rows=[];lines=raw.splitlines()
    for line in lines:
        entry=json.loads(line);message=entry.get('MESSAGE','')
        row=dict(journal_us=entry.get('__REALTIME_TIMESTAMP'))
        try:
            value=json.loads(message)
            require(isinstance(value,dict) and str(value.get('event','')).startswith('BINANCE_'),'UNSTRUCTURED')
            row['evidence']=clean(value)
        except (ValueError,RuntimeError,TypeError):
            row['message_sha256']=hashlib.sha256(str(message).encode()).hexdigest()
            code=re.search(r'\b(?:BINANCE_|PUBLIC_|ORPHANED_|BUDGET_)[A-Z0-9_]{1,110}\b',str(message))
            row['evidence']=dict(error_code=code.group(0)) if code else dict(raw_omitted=True)
        rows.append(row)
    return dict(records=rows,limit=10000,at_limit=len(lines)>=10000)


def continuous_ready(journal,sample,prepared,started_ms):
    """A sampled READY never substitutes for the collector's uninterrupted timer."""
    records=journal['records']
    metrics=[x['evidence'] for x in records
             if x.get('evidence',{}).get('event')=='BINANCE_SESSION_METRICS']
    require(not journal['at_limit'] and metrics and
            all(x.get('audit_output_failures')==0 for x in metrics),'OBSERVATION_INCOMPLETE_STOPPED')
    audits=[(i,x) for i,x in enumerate(records)
            if x.get('evidence',{}).get('event')=='BINANCE_CACHE_AUDIT']
    if not audits:return False
    index,row=audits[-1];audit=row['evidence'];observed=audit.get('observed_at_ms')
    require(type(observed) is int and started_ms<=observed<=time.time_ns()//1000000
            and 0<=time.time_ns()//1000000-observed<=60000
            and str(row.get('journal_us','')).isdigit()
            and 0<=int(row['journal_us'])//1000-observed<=60000,
            'READY_AUDIT_NOT_FRESH_STOPPED')
    require(audit.get('source_head')==NEW and audit.get('manifest_sha256')==prepared['snapshot_sha256']
            and audit.get('roster_hash')==prepared['roster_hash']
            and audit.get('shadow_only') is True and audit.get('real_trading_enabled') is False
            and audit.get('capital_authority')=='NONE_SHADOW_ONLY','READY_AUDIT_BINDING_MISMATCH_STOPPED')
    seconds=audit.get('ready_window_seconds')
    require(type(seconds) is int and 0<=seconds<=(observed-started_ms)//1000,
            'READY_AUDIT_TIMER_INVALID_STOPPED')
    interrupted=any(x.get('evidence',{}).get('event') in
                    ('BINANCE_RECOVERY_WINDOW_RESET','BINANCE_STREAM_RECOVERY') for x in records[index+1:])
    return (not interrupted and audit.get('status')=='READY' and
            audit.get('epoch')==sample['epoch'] and seconds>=300)


def stop_and_verify(upgrade,report):
    # A timed-out stop is ambiguous. Always try the independent UID/cgroup check.
    try:report['stop_returncode']=command(['systemctl','stop',SERVICE],check=False,timeout=60).returncode
    except BaseException as error:report['stop_error_type']=type(error).__name__
    try:upgrade.quiet();report['stopped_verified']=True
    except BaseException as error:
        report.update(stopped_verified=False,status='STOP_UNCONFIRMED_MANUAL_REVIEW',
                      stop_verification_error_type=type(error).__name__)


def supervise(upgrade,work,prepared):
    began=time.monotonic();since=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    started_ms=time.time_ns()//1000000
    def remaining(cap):
        left=1200-(time.monotonic()-began)
        require(left>0,'ACCEPTANCE_WINDOW_EXPIRED')
        return min(cap,left)
    report=dict(schema='binance_three_recovery_v1',old=OLD,new=NEW,incident_epoch=EPOCH,
                historical_outcome='UNKNOWN',archived_records=3,publication_verified=False,samples=[])
    healthy=None;epoch=None;accepted=False;started=False;persisted=False
    try:
        started=True  # Even an ambiguous start result must be followed by stop, never another start.
        command(['systemctl','start',SERVICE],timeout=remaining(60));remaining(60)
        while time.monotonic()-began<=1200:
            state=service_state(timeout=remaining(60));remaining(60)
            require(state.get('ActiveState')=='active' and state.get('SubState')=='running'
                    and state.get('MainPID','0')!='0','SERVICE_EXITED_NO_RETRY')
            sample=json.loads(child(acceptance_sample,json.dumps(prepared).encode(),timeout=remaining(50)).stdout)
            remaining(50)
            sample['elapsed_seconds']=round(time.monotonic()-began,3);report['samples'].append(sample)
            require(not sample['halt'] and not sample['cooldown_active'],'GLOBAL_STOP_OR_COOLDOWN_PRESERVED')
            require(sample['archived_unknown']==3,'DISPOSITION_AUDIT_CHANGED')
            if sample['status']=='READY':
                if healthy is None or epoch!=sample['epoch']:
                    require(time.monotonic()-began<=900,'INITIALIZATION_DEADLINE_STOPPED')
                    healthy=time.monotonic();epoch=sample['epoch']
                if time.monotonic()-healthy>=300:
                    journal=journal_evidence(since,timeout=remaining(30));remaining(30)
                    if continuous_ready(journal,sample,prepared,started_ms):
                        remaining(1);report['journal']=journal;accepted=True;break
            else:healthy=None
            require(time.monotonic()-began<900 or healthy is not None,'INITIALIZATION_DEADLINE_STOPPED')
            print(json.dumps(dict(stage='ACCEPTANCE',**sample)),flush=True)
            time.sleep(remaining(15))
        require(accepted,'ACCEPTANCE_WINDOW_EXPIRED')
        report['status']='READY_300S_CONSUMER_PUBLICATION_PENDING'
    except BaseException as error:
        import re
        code=str(error)
        report.update(status='FAILED_STOPPED',error_type=type(error).__name__,
                      error_code=code if re.fullmatch('[A-Z][A-Z0-9_]{0,127}',code) else 'UNCLASSIFIED')
    finally:
        if started and not accepted:
            stop_and_verify(upgrade,report)
        try:report['journal']=journal_evidence(since)
        except Exception as error:report['journal_error_type']=type(error).__name__
        destination=Path('/tmp/hunter-binance-recovery-'+EPOCH+'.report.json')
        try:
            save(upgrade,work,'recovery-report.json',report)
            upgrade.write_new(destination,json.dumps(report,sort_keys=True).encode());upgrade.sync_dir(destination.parent)
            digest=hashlib.sha256(destination.read_bytes()).hexdigest();persisted=True
        except BaseException as error:
            report.update(status='REPORT_PERSISTENCE_FAILED_STOPPED',report_error_type=type(error).__name__)
            if started and accepted:
                stop_and_verify(upgrade,report)
            accepted=False
            if report.get('stopped_verified') is False:report['status']='STOP_UNCONFIRMED_MANUAL_REVIEW'
            try:save(upgrade,work,'recovery-report-persistence-failure.json',report)
            except BaseException:pass  # The complete sanitized fallback still goes to the root console.
            print(json.dumps(dict(stage='REPORT_PERSISTENCE_FAILURE',report=report)),flush=True)
        print(json.dumps(dict(status=report['status'],report=str(destination),report_persisted=persisted,
            report_sha256=digest if persisted else None,evidence=str(work),
            historical_outcome='UNKNOWN',publication_verified=False)),flush=True)
    return 0 if accepted and persisted else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--run',action='store_true'); actions.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if args.check:
        result=command(['runuser','-u','hunter-binance-stream','--',PREFIX/'venv/bin/python','-E','-s','-B','-m',
            'scripts.hunter_binance_stream_ops','check','--snapshot',STATE/'manifest.json'],cwd=PREFIX/'code',check=False)
        sys.stdout.buffer.write(result.stdout); return result.returncode
    if args.run:
        try:return apply()
        except BaseException as error:
            import re
            code=str(error)
            report=dict(status='RECOVERY_ABORTED_MANUAL_REVIEW',incident_epoch=EPOCH,
                error_type=type(error).__name__,error_code=code if re.fullmatch('[A-Z][A-Z0-9_]{0,127}',code) else 'UNCLASSIFIED',
                historical_outcome='UNKNOWN',automatic_retry=False)
            destination=Path('/tmp/hunter-binance-recovery-'+EPOCH+'.report.json')
            if not os.path.lexists(destination):
                with destination.open('xb') as handle:
                    handle.write(json.dumps(report,sort_keys=True).encode());handle.flush();os.fsync(handle.fileno())
                destination.chmod(0o600)
            print(json.dumps(dict(**report,report=str(destination))),flush=True)
            return 1
    print(json.dumps(dict(status='PLAN_ONLY',incident_epoch=EPOCH,upgrade_entry=UPGRADER,
        records=3,old=OLD,new=NEW,http_attempts=1,service_starts=1,automatic_retry=False,
        historical_outcome='UNKNOWN',production_executed=False),sort_keys=True))
    return 0


if __name__=='__main__': raise SystemExit(main())
