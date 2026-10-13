"""Pinned, independent post-failure upgrade. Plan by default; no owner disposition.

--run is only for the separately reviewed transient systemd supervisor. Its
ExecStopPost must invoke --cleanup. This module does not install or start itself.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import types
import uuid

REPO = Path('/opt/shadow-runner/btc-grid-state')
PREFIX = Path('/opt/hunter-binance-stream')
STATE = Path('/var/lib/hunter-binance-stream')
SERVICE = 'hunter-binance-stream.service'
SUPERVISOR = 'hunter-binance-bootstrap-f4e8ad4.service'
OLD = '8cb92bd7e39251e1997e1b363a5e9500c8344268'
NEW = 'f4e8ad4de377a19763f4803e4fd56882c27d887a'
WRAPPER = '7bd133584a21d5cc2af1147594f24031e29fd635'
WRAPPER_HASH = '05aab679ee1f4b3cc53ad4fcf366959340fe79a0856e55fa066b07a77c93fdfa'
UPGRADER = 'db0d59e3f7a6f15d1f99a678c07c2336e4474867'
UPGRADER_HASH = 'e0615746c73809d258b858f4058cbd346b2df9e011a8d7df819f8e5f77808ddc'
CHANGED = {'research/hunter_binance_stream.py', 'scripts/check_binance_public_transport.py'}
INCIDENT = '51c723c224194317a49329ba8751d77f'
PREVIOUS_INVOCATION = '676227d443f4421589636a4da8760a8f'
PREVIOUS_EPOCH = '4eedfa078d32421e91a15dd06aac53d6'
PREVIOUS_REPORT_HASH = 'd7cac76b56fe51a9daca95b92609cc801ee09cb71cde9afb19c9e56f356156fe'
ACQUIRED = [589226044, 589226249, 589226469]
INITIALIZATION_SECONDS = 900
READY_SECONDS = 300
OBSERVATION_SECONDS = 90
ACCEPTANCE_SECONDS = INITIALIZATION_SECONDS + READY_SECONDS + OBSERVATION_SECONDS
START_SECONDS = 60
SUPERVISOR_SECONDS = 1800
FINALIZE_SECONDS = 240
MAX_SCAN_AGE = 300


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def command(args, *, timeout=30):
    value = subprocess.run([str(x) for x in args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'LANG':'C.UTF-8', 'PYTHONDONTWRITEBYTECODE':'1'})
    require(value.returncode == 0, 'COMMAND_FAILED_'+Path(str(args[0])).name)
    return value.stdout


def verified_module(source, expected, name):
    require(digest(source) == expected, 'COMPONENT_HASH_MISMATCH')
    import linecache
    filename='<'+name+'-'+expected+'>'
    # Pinned child() uses inspect.getsource. Keep only the verified immutable
    # source available to inspect; never substitute local working-tree bytes.
    linecache.cache[filename]=(len(source),None,source.decode('utf-8').splitlines(True),filename)
    module = types.ModuleType(name)
    module.__file__=filename
    exec(compile(source,filename,'exec'),module.__dict__)
    return module


def components():
    upgrade = verified_module(command(['git','-C',REPO,'show',UPGRADER+':scripts/hunter_binance_upgrade.py']),
                              UPGRADER_HASH, 'pinned_code_upgrade')
    evidence = verified_module(command(['git','-C',REPO,'show',WRAPPER+':scripts/hunter_binance_recover_three_once.py']),
                               WRAPPER_HASH, 'pinned_evidence')
    upgrade.OLD, upgrade.NEW, upgrade.CHANGED = OLD, NEW, CHANGED
    evidence.OLD, evidence.NEW = OLD, NEW
    return upgrade, evidence


def marker():
    return PREFIX/('bootstrap-upgrade-once-'+NEW+'.json')


def previous_marker():
    return PREFIX/('recovery-once-'+INCIDENT+'.json')


def private_json(upgrade, path):
    upgrade.regular(path)
    require(path.stat().st_uid == 0 and not stat.S_IMODE(path.stat().st_mode) & 0o077,
            'PRIVATE_EVIDENCE_OWNER_OR_MODE_INVALID')
    return json.loads(path.read_bytes())


def evidence_directory(path):
    path = Path(path)
    require(path.parent == PREFIX and re.fullmatch(r'upgrade-[a-f0-9]{32}', path.name)
            and path.is_dir() and not path.is_symlink() and path.resolve() == path,
            'INVALID_EVIDENCE_DIRECTORY')
    require(path.stat().st_uid == 0 and stat.S_IMODE(path.stat().st_mode) == 0o700,
            'EVIDENCE_OWNER_OR_MODE_INVALID')
    return path


def previous_evidence(upgrade):
    guard = private_json(upgrade, previous_marker())
    require(guard.get('epoch') == INCIDENT and guard.get('invocation_id') == PREVIOUS_INVOCATION
            and guard.get('phase') == 'BEFORE_LIVE_STATE_ACCESS'
            and guard.get('historical_outcome') == 'UNKNOWN', 'PREVIOUS_GUARD_MISMATCH')
    path = Path('/tmp')/('hunter-binance-recovery-'+INCIDENT+'-'+PREVIOUS_INVOCATION+'.report.json')
    require(guard.get('report') == str(path), 'PREVIOUS_REPORT_PATH_MISMATCH')
    report = private_json(upgrade, path)
    require(digest(path.read_bytes()) == PREVIOUS_REPORT_HASH, 'PREVIOUS_REPORT_HASH_MISMATCH')
    require(report.get('status') == 'FAILED_STOPPED' and report.get('stopped_verified') is True
            and report.get('new') == OLD and report.get('invocation_id') == PREVIOUS_INVOCATION
            and report.get('incident_epoch') == INCIDENT and report.get('archived_records') == 3
            and report.get('historical_outcome') == 'UNKNOWN', 'PREVIOUS_REPORT_STATE_MISMATCH')
    last = report['samples'][-1]
    require(last.get('epoch') == PREVIOUS_EPOCH and last.get('owners') == {'requests':0,'leases':0}
            and last.get('halt') is None and last.get('cooldown_active') is False, 'PREVIOUS_FINAL_SAMPLE_MISMATCH')
    work = evidence_directory(guard['evidence'])
    intent_path = work/'disposition-intent.json'
    intent = private_json(upgrade, intent_path)
    before = intent['original']
    require(before['meta']['epoch'] == INCIDENT and len(before['requests']) == len(before['leases']) == 3
            and sorted(x['acquired_ms'] for x in before['leases']) == ACQUIRED,
            'PREVIOUS_DISPOSITION_BINDING_INVALID')
    return dict(guard_sha256=digest(previous_marker().read_bytes()), report_sha256=PREVIOUS_REPORT_HASH,
                intent_sha256=digest(intent_path.read_bytes()), before=before)


def effective_unit(evidence):
    names = ['FragmentPath','DropInPaths','NeedDaemonReload','Restart','KillMode','TriggeredBy']
    data = evidence.command(['systemctl','--no-pager','show',SERVICE,*sum((['-p',x] for x in names),[])],timeout=30).stdout
    fields = dict(line.split('=',1) for line in data.decode().splitlines())
    require(fields == dict(FragmentPath='/etc/systemd/system/'+SERVICE, DropInPaths='', NeedDaemonReload='no',
                           Restart='no', KillMode='control-group', TriggeredBy=''), 'EFFECTIVE_UNIT_MISMATCH')


def fresh_scope(evidence):
    source = evidence.fresh_scan(None)  # Git readback only; never dispatch Discovery.
    scope_fresh(source)
    return source


def scope_fresh(source):
    stamp = dt.datetime.fromisoformat(source['snapshot']['as_of_utc'].replace('Z','+00:00'))
    require(stamp.tzinfo is not None and 0 <= (dt.datetime.now(dt.timezone.utc)-stamp).total_seconds() <= MAX_SCAN_AGE,
            'WAIT_FOR_NATURAL_SCAN_WITHIN_300_SECONDS')


def live_state(path=None):
    """Executed only as the collector UID, after the new guard is durable."""
    import contextlib,json,sqlite3
    from pathlib import Path
    fixture=path is not None
    path=Path('/var/lib/hunter-binance-stream/state.sqlite3') if path is None else path
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        value=dict(meta=dict(db.execute('SELECT key,value FROM meta')),
            budget=dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone()),
            clock=[dict(x) for x in db.execute('SELECT * FROM budget_clock')],
            requests=[dict(x) for x in db.execute('SELECT * FROM requests ORDER BY token')],
            leases=[dict(x) for x in db.execute('SELECT * FROM leases ORDER BY token')],
            dispositions=[dict(x) for x in db.execute('SELECT * FROM manual_http_dispositions ORDER BY incident_epoch,token')])
    if fixture:return value
    print(json.dumps(value,sort_keys=True));return 0


def validate_state(value, previous, boot, now):
    require(value['meta'].get('connected') == '0' and value['meta'].get('epoch') == PREVIOUS_EPOCH,
            'FAILED_STATE_EPOCH_OR_CONNECTION_CHANGED')
    require(value['requests'] == [] and value['leases'] == [], 'OWNERS_NOT_ZERO_MANUAL_REVIEW')
    require(value['clock'] == previous['before']['clock'] == [{'id':1,'boot':boot}], 'BUDGET_CLOCK_REVIEW_REQUIRED')
    require(value['budget']['halt'] is None and value['budget']['cooldown_ms'] <= now,
            'HALT_OR_COOLDOWN_PRESERVED')
    original = previous['before']
    require(all(value['budget'][k] >= original['budget'][k] for k in ('next_ms','last_ms')), 'BUDGET_RESET_FORBIDDEN')
    expected = {x['token']:x for x in original['leases']}
    require(len(expected) == 3 and {x['token'] for x in original['requests']} == set(expected)
            and all(x['denied'] == 0 for x in original['requests']), 'PREVIOUS_OWNERS_INVALID')
    rows = value['dispositions']
    require(len(rows) == 3 and {x['token'] for x in rows} == set(expected), 'HISTORICAL_UNKNOWN_ROWS_CHANGED')
    for row in rows:
        require(row['incident_epoch'] == INCIDENT and row['acquired_ms'] == expected[row['token']]['acquired_ms']
                and row['denied'] == 0 and row['historical_outcome'] == 'UNKNOWN'
                and row['disposition'] == 'USER_AUTHORIZED_BLOCKER_RETIRED'
                and row['evidence_sha256'] == previous['intent_sha256'], 'HISTORICAL_UNKNOWN_ROWS_CHANGED')


def preflight(upgrade, evidence):
    require(not os.path.lexists(marker()), 'NEW_ATTEMPT_ALREADY_CONSUMED')
    old, new = upgrade.releases(REPO)
    upgrade.check_host(old)
    effective_unit(evidence)
    previous = previous_evidence(upgrade)
    source = fresh_scope(evidence)
    return old, new, previous, source


def root_entry():
    require(sys.platform == 'linux' and os.geteuid() == 0, 'EXISTING_ROOT_LINUX_ENTRY_REQUIRED')


@contextlib.contextmanager
def operation_lock(upgrade):
    import fcntl
    path=PREFIX/'bootstrap-upgrade-operation.lock'
    try:upgrade.write_new(path,b'',0o600);upgrade.sync_dir(PREFIX)
    except FileExistsError:pass
    private_mode=path.stat().st_mode
    upgrade.regular(path)
    require(path.stat().st_uid == 0 and stat.S_IMODE(private_mode) == 0o600, 'OPERATION_LOCK_INVALID')
    with path.open('rb') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def supervisor_entry(evidence):
    invocation = os.environ.get('INVOCATION_ID','')
    require(re.fullmatch('[a-f0-9]{32}',invocation), 'TRANSIENT_SUPERVISOR_REQUIRED')
    names=['MainPID','Type','Restart','Transient','InvocationID','RuntimeMaxUSec','TimeoutStopUSec',
           'KillMode','ExecStopPost','StandardInput','StandardOutput','StandardError']
    raw=evidence.command(['systemctl','--no-pager','show',SUPERVISOR,*sum((['-p',x] for x in names),[])],timeout=30).stdout
    value=dict(line.split('=',1) for line in raw.decode().splitlines())
    hook=value.pop('ExecStopPost','')
    require(value == dict(MainPID=str(os.getpid()),Type='exec',Restart='no',Transient='yes',InvocationID=invocation,
            RuntimeMaxUSec='30min',TimeoutStopUSec='4min',KillMode='control-group',
            StandardInput='null',StandardOutput='journal',StandardError='journal'), 'TRANSIENT_SUPERVISOR_MISMATCH')
    require(hook.count('argv[]=') == 1 and 'path=/usr/bin/python3 ;' in hook
            and 'argv[]=/usr/bin/python3 -I -B '+str(Path(__file__).resolve())+' --cleanup ;' in hook
            and 'ignore_errors=no' in hook, 'INDEPENDENT_EXIT_HOOK_REQUIRED')
    return invocation


def apply(upgrade, evidence, invocation):
    old,new,previous,source=preflight(upgrade,evidence)
    with upgrade.maintenance():
        require(not os.path.lexists(marker()), 'NEW_ATTEMPT_ALREADY_CONSUMED')
        upgrade.check_host(old);effective_unit(evidence)
        require(previous_evidence(upgrade) == previous, 'PREVIOUS_EVIDENCE_CHANGED')
        scope_fresh(source)
        work=PREFIX/('upgrade-'+uuid.uuid4().hex)
        evidence.save(upgrade,PREFIX,marker().name,dict(schema='binance_bootstrap_upgrade_guard_v1',
            invocation_id=invocation,old=OLD,new=NEW,wrapper=WRAPPER,evidence=str(work),
            previous_guard_sha256=previous['guard_sha256'],phase='BEFORE_LIVE_STATE_ACCESS',
            historical_outcome='UNKNOWN',service_starts=1,automatic_retry=False))
        work.mkdir(mode=0o700);upgrade.sync_dir(PREFIX)
        hashes=upgrade.state_evidence(work)
        evidence.save(upgrade,work,'previous-binding.json',previous)
        before=json.loads(evidence.child(live_state).stdout)
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        validate_state(before,previous,boot,time.monotonic_ns()//1000000)
        evidence.save(upgrade,work,'state-before.json',before)
        backup=json.loads(evidence.child(evidence.consistent_backup).stdout)
        backup_path=Path(backup['path'])
        require(backup_path.parent == STATE and backup_path.name.startswith('recovery-evidence-')
                and backup_path.suffix == '.sqlite3' and backup['integrity'] == 'ok', 'BACKUP_PATH_INVALID')
        upgrade.regular(backup_path);upgrade.move(backup_path,work/'state-consistent.sqlite3')
        evidence.save(upgrade,work,'source.json',source)
        evidence.save(upgrade,work,'receipt.json',dict(old=OLD,new=NEW,state_hashes=hashes,
            old_files=upgrade.tree_manifest(old),new_files=upgrade.tree_manifest(new)))
        _,staged,_=upgrade.code_paths(PREFIX,work)
        upgrade.stage(staged,new);upgrade.write_new(work/'new-release-head',(NEW+'\n').encode(),0o444)
        upgrade.run([PREFIX/'venv/bin/python','-E','-s','-B','-c',
                     'from research import hunter_binance_stream, hunter_binance_public; from research.hunter_policy import C'],cwd=staged)
        upgrade.quiet();upgrade.switch(PREFIX,work,old,new);upgrade.quiet()
        after=json.loads(evidence.child(live_state).stdout)
        require(after == before, 'LIVE_STATE_CHANGED_DURING_CODE_SWAP')
        scope_fresh(source)
        prepared=json.loads(evidence.child(evidence.prepare_manifest,json.dumps(source).encode()).stdout)['receipt']
        require(json.loads(evidence.child(live_state).stdout) == before, 'LIVE_STATE_CHANGED_DURING_MANIFEST_PREPARE')
        require(digest(previous_marker().read_bytes()) == previous['guard_sha256'], 'PREVIOUS_GUARD_CHANGED')
        evidence.save(upgrade,work,'prepared.json',prepared)
        upgrade.quiet();scope_fresh(source)
    return supervise(upgrade,evidence,work,prepared,invocation)


def service(evidence, timeout=30):
    raw=evidence.command(['systemctl','--no-pager','show',SERVICE,'-p','ActiveState','-p','SubState',
        '-p','MainPID','-p','InvocationID','-p','ExecMainStartTimestampMonotonic'],timeout=timeout).stdout
    return dict(line.split('=',1) for line in raw.decode().splitlines())


def alive(value, invocation=None):
    require(value.get('ActiveState') == 'active' and value.get('SubState') == 'running'
            and value.get('MainPID','0') != '0' and re.fullmatch('[a-f0-9]{32}',value.get('InvocationID','')),
            'SERVICE_EXITED_NO_RETRY')
    if invocation is not None:require(value['InvocationID'] == invocation, 'COLLECTOR_INVOCATION_CHANGED')


def sample_valid(sample):
    require(sample.get('archived_unknown') == 3 and sample.get('halt') is None
            and sample.get('cooldown_active') is False, 'HISTORY_OR_GLOBAL_STOP_CHANGED')
    require(sample.get('shadow_only') is True and sample.get('real_trading_enabled') is False
            and sample.get('capital_authority') == 'NONE_SHADOW_ONLY', 'SAMPLE_SAFETY_BINDING_INVALID')


def initialized(journal, epoch):
    return any(x.get('evidence',{}).get('event') == 'BINANCE_INITIALIZATION_READY'
               and x['evidence'].get('epoch') == epoch for x in journal['records'])


def report_base(invocation):
    return dict(schema='binance_bootstrap_upgrade_v1',invocation_id=invocation,old=OLD,new=NEW,wrapper=WRAPPER,
        previous_report_sha256=PREVIOUS_REPORT_HASH,historical_outcome='UNKNOWN',archived_records=3,
        publication_verified=False,shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY',
        initialization_seconds=INITIALIZATION_SECONDS,ready_seconds=READY_SECONDS,
        observation_seconds=OBSERVATION_SECONDS,acceptance_seconds=ACCEPTANCE_SECONDS,
        status='FAILED_STOPPED',accepted=False,samples=[])


def failure(report, error):
    code=str(error)
    report.update(accepted=False,status='FAILED_STOPPED',error_type=type(error).__name__,
                  error_code=code if re.fullmatch('[A-Z][A-Z0-9_]{0,127}',code) else 'UNCLASSIFIED')


def final_journal(evidence, report, since, stopped):
    report['journal']=evidence.journal_evidence(since,timeout=30,require_final=stopped)
    require(report['journal'].get('complete') is True, 'FINAL_EVIDENCE_INCOMPLETE')


def publish(upgrade,evidence,work,report,name='acceptance-report.json'):
    evidence.save(upgrade,work,name,report)
    path=work/name
    receipt=dict(invocation_id=report['invocation_id'],accepted=report['accepted'],report=str(path),
                 report_sha256=digest(path.read_bytes()))
    evidence.save(upgrade,work,'completion.json' if report['accepted'] else 'failure-completion.json',receipt)
    print(json.dumps(dict(stage='CANDIDATE_PERSISTED_AWAITING_EXIT_HOOK' if report['accepted'] else 'FAILED_DURABLE_RESULT',
                         **receipt,status=report['status']),sort_keys=True),flush=True)


def supervise(upgrade,evidence,work,prepared,invocation):
    report=report_base(invocation);since=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    started_ms=time.time_ns()//1000000;began=time.monotonic();accepted=False
    evidence.save(upgrade,work,'start-intent.json',dict(invocation_id=invocation,since=since,started_ms=started_ms,
                                                       prepared=prepared,service_starts=1))
    try:
        evidence.command(['systemctl','start',SERVICE],timeout=START_SECONDS)
        state=service(evidence);alive(state)
        origin=int(state['ExecMainStartTimestampMonotonic'])/1000000
        require(began <= origin <= time.monotonic() and time.monotonic()-began <= START_SECONDS+30,
                'COLLECTOR_START_TIME_INVALID')
        report['collector_invocation_id']=state['InvocationID'];deadline=origin+ACCEPTANCE_SECONDS
        def remaining(cap):
            left=deadline-time.monotonic();require(left>0,'ACCEPTANCE_WINDOW_EXPIRED')
            return min(cap,left)
        saw_initialization=False
        while True:
            alive(service(evidence,remaining(30)),report['collector_invocation_id']);remaining(1)
            sample=json.loads(evidence.child(evidence.acceptance_sample,json.dumps(prepared).encode(),timeout=remaining(50)).stdout)
            remaining(1);sample_valid(sample)
            sample['elapsed_seconds']=round(time.monotonic()-origin,3);report['samples'].append(sample)
            if sample['status'] == 'READY':
                require(sample.get('owners') == {'requests':0,'leases':0}, 'READY_OWNERS_NOT_ZERO')
                journal=evidence.journal_evidence(since,timeout=remaining(30));remaining(1)
                saw_initialization=saw_initialization or initialized(journal,sample['epoch'])
                require(journal.get('complete') is True, 'OBSERVATION_INCOMPLETE_STOPPED')
                if saw_initialization and initialized(journal,sample['epoch']) and evidence.continuous_ready(journal,sample,prepared,started_ms):
                    historical=json.loads(evidence.child(live_state,timeout=remaining(30)).stdout)
                    require(historical['dispositions'] == private_json(upgrade,work/'state-before.json')['dispositions'],
                            'HISTORICAL_UNKNOWN_ROWS_CHANGED')
                    remaining(1)
                    final_sample=json.loads(evidence.child(evidence.acceptance_sample,json.dumps(prepared).encode(),timeout=remaining(50)).stdout)
                    remaining(1);sample_valid(final_sample)
                    require(final_sample['status'] == 'READY' and final_sample['epoch'] == sample['epoch']
                            and final_sample.get('owners') == {'requests':0,'leases':0}, 'FINAL_SAMPLE_CHANGED')
                    final_journal(evidence,report,since,False);remaining(1)
                    require(evidence.continuous_ready(report['journal'],final_sample,prepared,started_ms), 'FINAL_READY_INTERRUPTED')
                    report['final_sample']=final_sample
                    alive(service(evidence,remaining(30)),report['collector_invocation_id']);remaining(1)
                    accepted=True;break
            require(saw_initialization or time.monotonic()-origin < INITIALIZATION_SECONDS+OBSERVATION_SECONDS,
                    'INITIALIZATION_EVIDENCE_DEADLINE_STOPPED')
            print(json.dumps(dict(stage='ACCEPTANCE',**sample),sort_keys=True),flush=True)
            time.sleep(remaining(15))
        report.update(status='READY_300S_CONSUMER_PUBLICATION_PENDING',accepted=True)
        publish(upgrade,evidence,work,report)
        remaining(1)
        return 0
    except BaseException as error:
        failure(report,error);accepted=False
    finally:
        if not accepted:
            evidence.stop_and_verify(upgrade,report)
            try:final_journal(evidence,report,since,True)
            except BaseException as error:report['final_evidence_error_type']=type(error).__name__
            try:publish(upgrade,evidence,work,report,'failed-report.json')
            except BaseException as error:
                report.update(report_persisted=False,report_error_type=type(error).__name__)
                print(json.dumps(dict(stage='REPORT_PERSISTENCE_FAILURE',report=report),sort_keys=True),flush=True)
    return 1


def cleanup(upgrade,evidence):
    """Independent ExecStopPost: abnormal death can never authorize leaving it on."""
    require(os.path.lexists(marker()), 'CLEANUP_GUARD_MISSING')
    guard=private_json(upgrade,marker())
    require(guard.get('invocation_id') == os.environ.get('INVOCATION_ID') and guard.get('new') == NEW,
            'CLEANUP_INVOCATION_MISMATCH')
    work=evidence_directory(guard['evidence'])
    # No accepted completion is sufficient reason to stop, including a torn
    # intent/guard. Only fully verified success may leave the collector alive.
    require((work/'start-intent.json').exists(), 'CLEANUP_START_INTENT_MISSING')
    intent=private_json(upgrade,work/'start-intent.json')
    require(intent['invocation_id'] == guard['invocation_id'], 'START_INTENT_MISMATCH')
    normal=(os.environ.get('SERVICE_RESULT') == 'success' and os.environ.get('EXIT_CODE') == 'exited'
            and os.environ.get('EXIT_STATUS') == '0')
    if normal:
        try:
            require(not (work/'failure-completion.json').exists(), 'FAILURE_COMPLETION_TAKES_PRECEDENCE')
            receipt=private_json(upgrade,work/'completion.json')
            require(receipt['invocation_id'] == guard['invocation_id'] and receipt['accepted'] is True
                    and receipt['report'] == str(work/'acceptance-report.json'), 'COMPLETION_NOT_ACCEPTED')
            report=private_json(upgrade,work/'acceptance-report.json')
            require(digest((work/'acceptance-report.json').read_bytes()) == receipt['report_sha256']
                    and report['invocation_id'] == guard['invocation_id'] and report['accepted'] is True
                    and report['new'] == NEW and report['wrapper'] == WRAPPER and report['journal']['complete'] is True,
                    'COMPLETION_REPORT_INVALID')
            alive(service(evidence),report['collector_invocation_id'])
            evidence.save(upgrade,work,'supervisor-final.json',dict(status='ACCEPTED_AFTER_NORMAL_SUPERVISOR_EXIT',
                **receipt,service_result='success',exit_code='exited',exit_status='0'))
            print(json.dumps(dict(stage='FINAL_SUPERVISOR_RECEIPT',path=str(work/'supervisor-final.json'),accepted=True)),flush=True)
            return 0
        except BaseException:pass
    report=report_base(guard['invocation_id']);report['error_code']='SUPERVISOR_EXIT_WITHOUT_DURABLE_ACCEPTANCE'
    evidence.stop_and_verify(upgrade,report)
    try:final_journal(evidence,report,intent['since'],True)
    except BaseException as error:report['final_evidence_error_type']=type(error).__name__
    try:
        path=work/('supervisor-exit-report-'+uuid.uuid4().hex+'.json')
        evidence.save(upgrade,work,path.name,report)
        evidence.save(upgrade,work,'supervisor-final-failure-'+uuid.uuid4().hex+'.json',dict(
            accepted=False,invocation_id=guard['invocation_id'],report=str(path),report_sha256=digest(path.read_bytes())))
    except BaseException as error:report['report_error_type']=type(error).__name__
    print(json.dumps(dict(stage='SUPERVISOR_EXIT_STOP',report=report),sort_keys=True),flush=True)
    return 1


def emergency_stop(error):
    """Fail-closed fallback even if Git, the guard or all report files fail."""
    report=dict(stage='INDEPENDENT_EXIT_STOP_FALLBACK',status='STOP_UNCONFIRMED_MANUAL_REVIEW',
                error_type=type(error).__name__,accepted=False,stopped_verified=False)
    try:
        command(['systemctl','stop',SERVICE],timeout=60)
    except BaseException as stop_error:report['stop_error_type']=type(stop_error).__name__
    try:
        raw=command(['systemctl','--no-pager','show',SERVICE,'-p','ActiveState','-p','MainPID','-p','ControlGroup'],timeout=30)
        value=dict(line.split('=',1) for line in raw.decode().splitlines())
        require(value.get('ActiveState') in ('inactive','failed') and value.get('MainPID') == '0', 'COLLECTOR_NOT_STOPPED')
        import pwd
        uid=pwd.getpwnam('hunter-binance-stream').pw_uid
        for path in Path('/proc').glob('[0-9]*/status'):
            try:lines=path.read_text().splitlines()
            except FileNotFoundError:continue
            require(not any(line.startswith('Uid:') and uid in map(int,line.split()[1:]) for line in lines), 'COLLECTOR_UID_ALIVE')
        group=value.get('ControlGroup','')
        if group:
            from pathlib import PurePosixPath
            require(group.startswith('/') and '..' not in PurePosixPath(group).parts and group.endswith('/'+SERVICE), 'CGROUP_INVALID')
            root=Path('/sys/fs/cgroup')/group.lstrip('/')
            for path in root.rglob('cgroup.procs') if root.exists() else ():
                require(not path.read_text().strip(), 'COLLECTOR_CGROUP_ALIVE')
        report.update(status='FAILED_STOPPED',stopped_verified=True)
    except BaseException as verify_error:report['verification_error_type']=type(verify_error).__name__
    # stdout is the independent supervisor journal. Do not depend on the failed
    # work directory or overwrite an earlier acceptance report.
    print(json.dumps(report,sort_keys=True),flush=True)
    return 1


def rollback(upgrade,evidence,work):
    work=evidence_directory(work);guard=private_json(upgrade,marker())
    require(guard['evidence'] == str(work) and guard['new'] == NEW, 'ROLLBACK_GUARD_MISMATCH')
    with upgrade.maintenance():
        raw=evidence.command(['systemctl','--no-pager','show',SUPERVISOR,'-p','ActiveState','-p','MainPID'],timeout=30).stdout
        fields=dict(line.split('=',1) for line in raw.decode().splitlines())
        require(fields.get('ActiveState') in ('inactive','failed') and fields.get('MainPID') == '0',
                'SUPERVISOR_STILL_ACTIVE_ROLLBACK_FORBIDDEN')
        old,new=upgrade.releases(REPO);receipt=private_json(upgrade,work/'receipt.json')
        require(receipt['old'] == OLD and receipt['new'] == NEW
                and receipt['old_files'] == upgrade.tree_manifest(old) and receipt['new_files'] == upgrade.tree_manifest(new),
                'ROLLBACK_RECEIPT_MISMATCH')
        require(not (work/'rollback-receipt.json').exists(), 'ROLLBACK_ALREADY_CONSUMED')
        hashes={p.name:digest(p.read_bytes()) for p in upgrade.state_files()}
        upgrade.restore(PREFIX,work,old,new);upgrade.quiet();upgrade.state_unchanged(hashes)
        evidence.save(upgrade,work,'rollback-receipt.json',dict(status='ROLLED_BACK_NOT_STARTED',state_hashes=hashes,
            old=OLD,new=NEW,historical_outcome='UNKNOWN',attempt_guard_preserved=True))
    print('ROLLED_BACK_NOT_STARTED');return 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--check',action='store_true');actions.add_argument('--run',action='store_true')
    actions.add_argument('--cleanup',action='store_true');actions.add_argument('--rollback',type=Path)
    args=parser.parse_args(argv)
    if not (args.check or args.run or args.cleanup or args.rollback):
        print(json.dumps(dict(status='PLAN_ONLY',old=OLD,new=NEW,wrapper=WRAPPER,
            guard=str(marker()),previous_guard_preserved=True,owner_dispositions=0,explicit_probes=0,
            service_starts=1,initialization_seconds=900,ready_seconds=300,acceptance_seconds=ACCEPTANCE_SECONDS,
            supervisor_seconds=SUPERVISOR_SECONDS,production_executed=False),sort_keys=True));return 0
    root_entry()
    if args.cleanup:
        try:
            upgrade,evidence=components()
            return cleanup(upgrade,evidence)
        except BaseException as error:return emergency_stop(error)
    upgrade,evidence=components()
    if args.rollback:
        with operation_lock(upgrade):return rollback(upgrade,evidence,args.rollback)
    if args.check:
        preflight(upgrade,evidence);print('PREFLIGHT_NO_LIVE_SQLITE_OR_SERVICE_START');return 0
    invocation=supervisor_entry(evidence)
    def interrupted(*_):raise RuntimeError('SUPERVISOR_INTERRUPTED')
    for signum in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(signum,interrupted)
    try:
        with operation_lock(upgrade):return apply(upgrade,evidence,invocation)
    except BaseException as error:
        report=report_base(invocation);failure(report,error)
        report.update(status='PREPARATION_ABORTED_NO_AUTOMATIC_RETRY',guard_present=os.path.lexists(marker()))
        path=Path('/tmp')/('hunter-binance-bootstrap-'+invocation+'.report.json')
        try:report_hash=evidence.write_report(path,report)
        except BaseException:report_hash=None
        print(json.dumps(dict(stage='PREPARATION_FAILURE',report=report,report_path=str(path),report_sha256=report_hash)),flush=True)
        return 1


if __name__ == '__main__':raise SystemExit(main())
