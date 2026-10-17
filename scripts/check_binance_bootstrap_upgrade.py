"""Offline package verifier: Git reads first, then forbid all network/process effects."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import hunter_binance_bootstrap_upgrade_once as entry


def main():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder).resolve();os.environ['BINANCE_BOOTSTRAP_FIXTURES']=str(root)
        for sha,path,name in [(entry.UPGRADER,'scripts/hunter_binance_upgrade.py','upgrade.py'),
                              (entry.WRAPPER,'scripts/hunter_binance_recover_three_once.py','evidence.py')]:
            (root/name).write_bytes(subprocess.check_output(['git','-C',str(ROOT),'show',sha+':'+path]))
        upgrade=entry.verified_module((root/'upgrade.py').read_bytes(),entry.UPGRADER_HASH,'verified_package_upgrade')
        upgrade.OLD,upgrade.NEW,upgrade.CHANGED=entry.OLD,entry.NEW,entry.CHANGED
        # Windows user's Git enables checkout/archive CRLF conversion. Verify
        # canonical Linux deployment bytes without changing repository config.
        original_run=upgrade.run
        upgrade.run=lambda args,**kw:original_run([args[0],'-c','core.autocrlf=false',*args[1:]] if args[0]=='git' else args,**kw)
        old,new=upgrade.releases(ROOT)
        print('EXACT_RUNTIME_DELTA',entry.OLD,entry.NEW,len(old),sorted(entry.CHANGED),flush=True)
        data=subprocess.check_output(['git','-C',str(ROOT),'archive',entry.NEW,'research','scripts'])
        runtime=root/'runtime';runtime.mkdir()
        with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
            for item in bundle:
                if item.isfile() and item.name.endswith('.py'):
                    target=runtime/item.name;target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(bundle.extractfile(item).read())
        shutil.copyfile(ROOT/'scripts/hunter_binance_bootstrap_upgrade_once.py',runtime/'scripts/hunter_binance_bootstrap_upgrade_once.py')
        tests=runtime/'tests';tests.mkdir()
        for path in (ROOT/'tests').glob('test_binance_bootstrap_*.py'):shutil.copyfile(path,tests/path.name)
        for key in list(sys.modules):
            if key=='scripts' or key.startswith(('scripts.','research.')):sys.modules.pop(key,None)
        sys.path.insert(0,str(runtime));tempfile.tempdir=str(root);blocked=[]
        def guard(event,args):
            if event.startswith('socket.') or event in ('subprocess.Popen','os.system','os.posix_spawn','os.fork','os.forkpty') or event.startswith(('os.exec','os.spawn')):
                blocked.append(event);raise RuntimeError('NETWORK_OR_PROCESS_FORBIDDEN')
        sys.addaudithook(guard)
        result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(tests),pattern='test_binance_bootstrap_*.py'))
        passed=result.wasSuccessful() and result.testsRun==30 and not result.skipped and not blocked
        print('BOOTSTRAP_UPGRADE_OFFLINE',result.testsRun,len(result.skipped),len(blocked),passed)
        return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
