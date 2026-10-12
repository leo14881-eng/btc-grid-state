"""Authorized incident only: exact upgrade, one public GET, audited orphan disposition.

Default is plan. Production requires --run through the existing root terminal.
No alternate endpoint, automatic HTTP retry, Bybit change or consumer selection.
"""
import argparse
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
EPOCH = '334a5a2f75324d1b924df41c8efaa800'
UPGRADER = 'db0d59e3f7a6f15d1f99a678c07c2336e4474867'
UPGRADER_HASH = 'e0615746c73809d258b858f4058cbd346b2df9e011a8d7df819f8e5f77808ddc'


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
    return module


def read_state(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row; db.execute('BEGIN')
        return dict(meta=dict(db.execute('SELECT key,value FROM meta')),
            budget=dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone()),
            clock=[dict(x) for x in db.execute('SELECT * FROM budget_clock')],
            requests=[dict(x) for x in db.execute('SELECT * FROM requests ORDER BY token')],
            leases=[dict(x) for x in db.execute('SELECT * FROM leases ORDER BY token')])


def validate_incident(value, boot, monotonic_ms):
    require(value['meta'].get('connected') == '0' and value['meta'].get('epoch') == EPOCH, 'INCIDENT_EPOCH_OR_CONNECTION_CHANGED')
    require(value['clock'] == [{'id':1,'boot':boot}], 'BUDGET_CLOCK_REVIEW_REQUIRED')
    require(value['budget']['halt'] is None, 'PERSISTENT_HALT_PRESERVED')
    require(value['budget']['cooldown_ms'] <= monotonic_ms, 'COOLDOWN_STILL_ACTIVE')
    require(len(value['requests']) == len(value['leases']) == 1, 'EXPECTED_SINGLE_ORPHAN_CHANGED')
    request,lease=value['requests'][0],value['leases'][0]
    import re
    require(re.fullmatch('[a-f0-9]{32}',request['token']) and request['denied'] == 0
            and request['token'] == lease['token'], 'ORPHAN_OR_DENIAL_CHANGED')


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


def dispose_one():
    # Runs as the existing collector UID to preserve SQLite/WAL ownership.
    import json,sys,time
    from pathlib import Path
    from research.hunter_binance_stream_store import Store
    expected=json.load(sys.stdin)
    with Store.reader().db() as db:
        actual=dict(meta=dict(db.execute('SELECT key,value FROM meta')),
            budget=dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone()),
            clock=[dict(x) for x in db.execute('SELECT * FROM budget_clock')],
            requests=[dict(x) for x in db.execute('SELECT * FROM requests ORDER BY token')],
            leases=[dict(x) for x in db.execute('SELECT * FROM leases ORDER BY token')])
        if any(actual[k] != expected[k] for k in ('meta','clock','requests','leases')):
            raise RuntimeError('ORPHAN_COMPARE_AND_SWAP_FAILED')
        if (actual['budget']['halt'] is not None or actual['budget']['cooldown_ms'] > time.monotonic_ns()//1000000
                or actual['clock'][0]['boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                or any(actual['budget'][k] < expected['budget'][k] for k in ('next_ms','last_ms'))):
            raise RuntimeError('BUDGET_CHANGED_OR_STOPPED')
        token=expected['requests'][0]['token']; acquired=expected['leases'][0]['acquired_ms']
        if db.execute('DELETE FROM requests WHERE token=? AND denied=0',(token,)).rowcount != 1:
            raise RuntimeError('REQUEST_DELETE_COUNT_MISMATCH')
        if db.execute('DELETE FROM leases WHERE token=? AND acquired_ms=?',(token,acquired)).rowcount != 1:
            raise RuntimeError('LEASE_DELETE_COUNT_MISMATCH')
    print(json.dumps(dict(status='DISPOSED_HISTORICAL_HTTP_OUTCOME_UNKNOWN',records=1)))


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


def child(function, data=None, check=True):
    source=inspect.getsource(function)+'\nraise SystemExit('+function.__name__+'())\n'
    return command(['runuser','-u','hunter-binance-stream','--',PREFIX/'venv/bin/python',
                    '-E','-s','-B','-c',source],cwd=PREFIX/'code',data=data,check=check,timeout=50)


def save(upgrade, work, name, value):
    upgrade.write_new(work/name,json.dumps(value,sort_keys=True).encode())


def fresh_scan(upgrade):
    reference='refs/hunter-recovery/'+uuid.uuid4().hex+'/main'
    command(['git','-C',REPO,'fetch','--no-tags','--no-write-fetch-head','--refmap=',
             'origin','main:'+reference])
    head=command(['git','-C',REPO,'rev-parse',reference]).stdout.decode().strip()
    data=command(['git','-C',REPO,'show',head+':research/results/hunter-cex-universe-latest.json']).stdout
    # Validate without state writes. A 20-minute admission limit leaves startup headroom.
    code="""import datetime as d,json,sys
from research.hunter_binance_stream_store import roster
s=json.load(sys.stdin); now=d.datetime.now(d.timezone.utc); roster(s,now)
stamp=d.datetime.fromisoformat(s['as_of_utc'].replace('Z','+00:00'))
assert 0 <= (now-stamp).total_seconds() <= 1200, 'NATURAL_SCAN_TOO_OLD_WAIT_FOR_NEXT_CYCLE'
print('FRESH_SCOPE_OK')
"""
    command([PREFIX/'venv/bin/python','-E','-s','-B','-c',code],cwd=PREFIX/'code',data=data)
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
        before=read_state(STATE/'state.sqlite3')
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        validate_incident(before,boot,time.monotonic_ns()//1000000)
        work=PREFIX/('upgrade-'+uuid.uuid4().hex); work.mkdir(mode=0o700)
        print(json.dumps(dict(stage='EVIDENCE',directory=str(work))),flush=True)
        hashes=upgrade.state_evidence(work)
        # A consistent SQLite backup supplements the untouched raw DB/WAL/SHM copies.
        backup=work/'state-consistent.sqlite3'; upgrade.write_new(backup,b'')
        with sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True) as original:
            with sqlite3.connect(backup) as target:
                original.backup(target)
                require(target.execute('PRAGMA integrity_check').fetchone()[0]=='ok','BACKUP_INTEGRITY_FAILED')
        save(upgrade,work,'incident-before.json',before)
        save(upgrade,work,'source.json',source)
        save(upgrade,work,'receipt.json',dict(old=upgrade.OLD,new=upgrade.NEW,state_hashes=hashes,
            old_files=upgrade.tree_manifest(old),new_files=upgrade.tree_manifest(new)))
        _,staged,_=upgrade.code_paths(PREFIX,work)
        upgrade.stage(staged,new); upgrade.write_new(work/'new-release-head',(upgrade.NEW+'\n').encode(),0o444)
        upgrade.quiet(); upgrade.switch(PREFIX,work,old,new)
        # Durable, exclusive attempt receipt: rerunning cannot issue a second probe.
        save(upgrade,PREFIX,attempt.name,dict(epoch=EPOCH,evidence=str(work),
            authorization='USER_APPROVED_SINGLE_PUBLIC_GET_AND_CONDITIONAL_DISPOSITION',historical_outcome='UNKNOWN'))
        upgrade.quiet()
        print(json.dumps(dict(stage='ONE_PUBLIC_GET',code_release=upgrade.NEW)),flush=True)
        result=child(probe_once,check=False)
        upgrade.write_new(work/'probe.stdout',result.stdout); upgrade.write_new(work/'probe.stderr',result.stderr)
        receipt=json.loads(result.stdout)
        require(result.returncode==0 and receipt.get('success') is True and receipt.get('http_status')==200
                and receipt.get('http_attempts')==1 and receipt.get('process_reaped') is True,'PROBE_FAILED_OR_UNKNOWN_STOPPED')
        upgrade.quiet()
        after=read_state(STATE/'state.sqlite3')
        validate_incident(after,boot,time.monotonic_ns()//1000000)
        require(all(after[k]==before[k] for k in ('meta','clock','requests','leases')),'INCIDENT_CHANGED_AFTER_PROBE')
        prepared=child(prepare_manifest,json.dumps(source).encode())
        upgrade.write_new(work/'manifest-receipt.json',prepared.stdout)
        save(upgrade,work,'disposition-intent.json',dict(original=before,probe=receipt,
             disposition='HISTORICAL_OUTCOME_UNKNOWN_MANUALLY_RESOLVED',records=1))
        done=child(dispose_one,json.dumps(before).encode())
        upgrade.write_new(work/'disposition-complete.json',done.stdout)
        upgrade.quiet()
    # Only now release the maintenance locks and start the single collector.
    command(['systemctl','start',SERVICE])
    state=command(['systemctl','--no-pager','show',SERVICE,'-p','ActiveState','-p','SubState','-p','MainPID']).stdout.decode()
    fields=dict(line.split('=',1) for line in state.splitlines())
    require(fields.get('ActiveState')=='active' and fields.get('SubState')=='running'
            and fields.get('MainPID','0')!='0','START_DID_NOT_REMAIN_RUNNING_NO_RETRY')
    print(json.dumps(dict(status='STARTED_ACCEPTANCE_PENDING',evidence=str(work),service=state,
        historical_http_outcome='UNKNOWN',bybit_changed=False,consumers_changed=False)),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--run',action='store_true'); actions.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if args.check:
        result=command(['runuser','-u','hunter-binance-stream','--',PREFIX/'venv/bin/python','-E','-s','-B','-m',
            'scripts.hunter_binance_stream_ops','check','--snapshot',STATE/'manifest.json'],cwd=PREFIX/'code',check=False)
        sys.stdout.buffer.write(result.stdout); return result.returncode
    if args.run: apply(); return 0
    print(json.dumps(dict(status='PLAN_ONLY',incident_epoch=EPOCH,upgrade_entry=UPGRADER,
        http_attempts=1,automatic_retry=False,historical_outcome='UNKNOWN',production_executed=False),sort_keys=True))
    return 0


if __name__=='__main__': raise SystemExit(main())
