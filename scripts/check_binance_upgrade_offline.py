"""Only the small Linux upgrade suite; block network, processes and external writes."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def main():
    if sys.platform != 'linux': raise RuntimeError('LINUX_FILESYSTEM_ACCEPTANCE_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='binance-upgrade-offline-') as directory:
        scratch = Path(directory).resolve(); tempfile.tempdir = str(scratch); blocked = []
        def reject(reason):
            blocked.append(reason); raise RuntimeError(reason)
        def inside(path, dir_fd=None):
            if isinstance(path,int): return
            target=Path(os.fsdecode(path))
            if not target.is_absolute() and isinstance(dir_fd,int) and dir_fd>=0:
                target=Path(os.readlink('/proc/self/fd/'+str(dir_fd)))/target
            target=target.resolve()
            if target!=scratch and scratch not in target.parents: reject('WRITE_OUTSIDE_SCRATCH')
        def guard(event,args):
            if event.startswith('socket.') or event in ('subprocess.Popen','os.system','os.posix_spawn','os.fork','os.forkpty') or event.startswith(('os.exec','os.spawn')):
                reject('NETWORK_OR_PROCESS_FORBIDDEN')
            if event=='open':
                path,mode,flags=args
                if isinstance(mode,str) and any(x in mode for x in 'wax+') or isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND): inside(path)
            if event in ('os.mkdir','os.remove','os.rmdir','os.chmod','os.utime'):
                index={'os.mkdir':2,'os.remove':1,'os.rmdir':1,'os.chmod':2,'os.utime':3}[event]
                inside(args[0],args[index] if len(args)>index else None)
            if event=='os.rename': inside(args[0]); inside(args[1])
            if event in ('os.symlink','os.link'): reject('LINK_FORBIDDEN')
        sys.addaudithook(guard)
        suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern='test_hunter_binance_upgrade.py')
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        good=result.wasSuccessful() and result.testsRun==16 and not result.skipped and not blocked
        print('BINANCE_UPGRADE_OFFLINE_ACCEPTANCE',dict(tests=result.testsRun,skipped=len(result.skipped),blocked_effects=len(blocked),passed=good))
        return 0 if good else 1


if __name__=='__main__': raise SystemExit(main())
