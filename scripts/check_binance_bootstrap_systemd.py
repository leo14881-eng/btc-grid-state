"""CI-only root systemd fixture. Unique fake units; no exchange, production path or collector user.

Refuses hosts without GITHUB_ACTIONS=true. Every unit is prefixed codex-binance-fixture-.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


def run(args,check=True):
    return subprocess.run(args,check=check,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)


def load(path):
    spec=importlib.util.spec_from_file_location('candidate',path);module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module);return module


def write(path,value):
    data=json.dumps(value).encode() if not isinstance(value,bytes) else value
    with path.open('xb') as handle:handle.write(data);handle.flush();os.fsync(handle.fileno())
    path.chmod(0o600)


def state(unit):
    raw=run(['systemctl','show',unit,'-p','ActiveState','-p','SubState','-p','MainPID','-p','InvocationID']).stdout
    return dict(line.split('=',1) for line in raw.decode().splitlines())


def fixture(mode):
    root=Path(__file__).resolve().parent;config=json.loads((root/'fixture.json').read_bytes())
    entry=load(root/'entry.py');entry.PREFIX=root/'prefix';entry.STATE=root/'state'
    entry.SERVICE=config['collector'];entry.SUPERVISOR=config['supervisor']
    # Exact hook validation points to this isolated fixture driver, which calls
    # the candidate's real cleanup. Production uses the standalone entry.py.
    entry.__file__=str(Path(__file__).resolve())
    upgrade=entry.verified_module((root/'upgrade.py').read_bytes(),entry.UPGRADER_HASH,'fixture_upgrade')
    evidence=entry.verified_module((root/'evidence.py').read_bytes(),entry.WRAPPER_HASH,'fixture_evidence')
    evidence.SERVICE=entry.SERVICE
    def quiet():
        value=state(entry.SERVICE)
        if value.get('ActiveState') not in ('inactive','failed') or value.get('MainPID')!='0':raise RuntimeError('FIXTURE_ALIVE')
    upgrade.quiet=quiet
    entry.components=lambda:(upgrade,evidence)
    # Fake collector has no market events; mock only data evidence, while stop,
    # process lifetimes, systemd exit status and report persistence remain real.
    evidence.journal_evidence=lambda *args,**kwargs:dict(complete=True,records=[],at_limit=False)
    if mode=='--cleanup':
        if config['case']=='component-loss':entry.components=lambda:(_ for _ in ()).throw(OSError('fixture Git unavailable'))
        if config['case']=='final-receipt-failure':
            real_save=evidence.save
            def failed_save(up,where,name,value):
                real_save(up,where,name,value)
                if name=='supervisor-final.json':raise OSError('fixture final receipt fsync failure')
            evidence.save=failed_save
        return entry.main(['--cleanup'])
    invocation=entry.supervisor_entry(evidence)
    work=entry.PREFIX/('upgrade-'+'a'*32);work.mkdir(mode=0o700)
    write(entry.marker(),dict(invocation_id=invocation,new=entry.NEW,evidence=str(work)))
    write(work/'start-intent.json',dict(invocation_id=invocation,since='now'))
    run(['systemd-run','--quiet','--unit',entry.SERVICE,'--service-type=exec','--property=RuntimeMaxSec=60',
         '--property=Restart=no','--property=StandardInput=null','/usr/bin/sleep','60'])
    write(root/'ready',b'1')
    case=config['case']
    if case in ('success','final-receipt-failure'):
        report=entry.report_base(invocation);report.update(accepted=True,new=entry.NEW,journal={'complete':True},
            collector_invocation_id=state(entry.SERVICE)['InvocationID'],status='READY_300S_CONSUMER_PUBLICATION_PENDING')
        entry.publish(upgrade,evidence,work,report);return 0
    if case=='exit-failure':return 7
    if case=='component-loss':return 7
    if case=='corrupt-intent':(work/'start-intent.json').write_bytes(b'{');return 7
    if case=='timeout':run(['systemctl','set-property',entry.SUPERVISOR,'RuntimeMaxSec=2s'])
    time.sleep(30);return 9


def swap_checks(entry,source,helper):
    upgrade=entry.verified_module(helper,entry.UPGRADER_HASH,'real_swap_fixture')
    upgrade.OLD,upgrade.NEW,upgrade.CHANGED=entry.OLD,entry.NEW,entry.CHANGED
    original_run=upgrade.run
    upgrade.run=lambda args,**kw:original_run([args[0],'-c','safe.directory='+str(source),'-c','core.autocrlf=false',*args[1:]] if args[0]=='git' else args,**kw)
    old,new=upgrade.releases(source)
    for boundary in range(5):
        with tempfile.TemporaryDirectory(prefix='codex-binance-fixture-swap-') as folder:
            prefix=Path(folder);work=prefix/('upgrade-'+'b'*32);work.mkdir(mode=0o700)
            upgrade.stage(prefix/'code',old);upgrade.write_new(prefix/'release-head',(entry.OLD+'\n').encode(),0o444)
            _,staged,_=upgrade.code_paths(prefix,work);upgrade.stage(staged,new)
            upgrade.write_new(work/'new-release-head',(entry.NEW+'\n').encode(),0o444)
            preserved=prefix/'consumed-guard-and-unknown-state';preserved.write_bytes(b'preserve every prior byte')
            real_move=upgrade.move;calls=[0]
            def fault_move(a,b):
                calls[0]+=1
                if calls[0]==boundary:raise RuntimeError('INJECTED_SWAP_BOUNDARY')
                return real_move(a,b)
            upgrade.move=fault_move
            try:
                if boundary:
                    try:upgrade.switch(prefix,work,old,new)
                    except RuntimeError as error:assert str(error)=='INJECTED_SWAP_BOUNDARY'
                    else:raise AssertionError('FAULT_NOT_TRIGGERED')
                else:
                    upgrade.switch(prefix,work,old,new);upgrade.verify_tree(prefix/'code',new)
                    upgrade.restore(prefix,work,old,new)
                upgrade.verify_tree(prefix/'code',old)
                assert (prefix/'release-head').read_bytes()==(entry.OLD+'\n').encode()
                assert preserved.read_bytes()==b'preserve every prior byte'
            finally:upgrade.move=real_move
            print('REAL_IMMUTABLE_SWAP_CHECK',boundary,'PASS',flush=True)


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or sys.platform!='linux' or os.geteuid()!=0:
        raise RuntimeError('ISOLATED_GITHUB_LINUX_SYSTEMD_ONLY')
    if len(sys.argv)>1:return fixture(sys.argv[1])
    source=Path(__file__).resolve().parents[1];entry=load(source/'scripts/hunter_binance_bootstrap_upgrade_once.py')
    helper=run(['git','-c','safe.directory='+str(source),'-C',str(source),'show',entry.UPGRADER+':scripts/hunter_binance_upgrade.py']).stdout
    wrapper=run(['git','-c','safe.directory='+str(source),'-C',str(source),'show',entry.WRAPPER+':scripts/hunter_binance_recover_three_once.py']).stdout
    assert hashlib.sha256(helper).hexdigest()==entry.UPGRADER_HASH
    assert hashlib.sha256(wrapper).hexdigest()==entry.WRAPPER_HASH
    swap_checks(entry,source,helper)
    for case in ('success','exit-failure','term','kill','timeout','component-loss','corrupt-intent','final-receipt-failure'):
        with tempfile.TemporaryDirectory(prefix='codex-binance-fixture-') as folder:
            root=Path(folder);root.chmod(0o700);(root/'prefix').mkdir(mode=0o700);(root/'state').mkdir(mode=0o700)
            token=uuid.uuid4().hex;supervisor='codex-binance-fixture-supervisor-'+token+'.service'
            collector='codex-binance-fixture-collector-'+token+'.service'
            write(root/'fixture.json',dict(case=case,supervisor=supervisor,collector=collector))
            write(root/'upgrade.py',helper);write(root/'evidence.py',wrapper)
            write(root/'entry.py',(source/'scripts/hunter_binance_bootstrap_upgrade_once.py').read_bytes())
            script=root/'fixture.py';write(script,Path(__file__).read_bytes())
            try:
                # Detached from the command's terminal. The launcher exits while
                # the supervised fixture continues under the service manager.
                run(['systemd-run','--quiet','--unit',supervisor,'--service-type=exec',
                    '--setenv=GITHUB_ACTIONS=true','--property=Restart=no','--property=RuntimeMaxSec=1800',
                    '--property=TimeoutStopSec=240','--property=KillMode=control-group',
                    '--property=StandardInput=null','--property=StandardOutput=journal','--property=StandardError=journal',
                    '--property=ExecStopPost=/usr/bin/python3 -I -B '+str(script)+' --cleanup',
                    '/usr/bin/python3','-I','-B',str(script),'--fixture'])
                deadline=time.monotonic()+15
                while not (root/'ready').exists() and time.monotonic()<deadline:time.sleep(.1)
                assert (root/'ready').exists(),'FIXTURE_DID_NOT_REACH_START'
                if case in ('term','kill'):
                    run(['systemctl','kill','--kill-whom=main','--signal='+('TERM' if case=='term' else 'KILL'),supervisor])
                while state(supervisor).get('ActiveState') not in ('inactive','failed') and time.monotonic()<deadline:time.sleep(.1)
                assert state(supervisor).get('ActiveState') in ('inactive','failed'),'SUPERVISOR_NOT_FINISHED'
                running=state(collector).get('ActiveState')=='active'
                assert running==(case=='success'),(case,state(collector))
                reports=list((root/'prefix').glob('upgrade-*/supervisor-exit-report-*.json'))
                logs=run(['journalctl','--no-pager','--all','-u',supervisor,'-o','cat']).stdout.decode()
                if case in ('component-loss','corrupt-intent'):assert 'INDEPENDENT_EXIT_STOP_FALLBACK' in logs
                elif case!='success':assert len(reports)==1 and json.loads(reports[0].read_bytes())['stopped_verified'] is True
                print('REAL_SYSTEMD_EXIT_CHECK',case,'PASS',flush=True)
            except BaseException:
                print(run(['journalctl','--no-pager','--all','-u',supervisor,'-o','cat'],check=False).stdout.decode(),flush=True)
                raise
            finally:
                # Only the two generated fixture names; never wildcard units.
                for unit in (supervisor,collector):
                    run(['systemctl','stop',unit],check=False);run(['systemctl','reset-failed',unit],check=False)
    print('REAL_SYSTEMD_EXIT_ACCEPTANCE 8 0 True');return 0


if __name__=='__main__':raise SystemExit(main())
