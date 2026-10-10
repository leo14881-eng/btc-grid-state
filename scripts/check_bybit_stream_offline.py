"""Linux offline acceptance: inherited audit guard also applies to forked workers."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
sys.dont_write_bytecode=True


def main():
    blocked=[]
    with tempfile.TemporaryDirectory(prefix='bybit-stream-offline-') as directory:
        scratch=Path(directory).resolve()
        tempfile.tempdir=str(scratch)
        def write_path(path):
            if isinstance(path,int):return
            resolved=Path(os.fsdecode(path)).resolve()
            if resolved!=scratch and scratch not in resolved.parents:
                blocked.append('WRITE_OUTSIDE_SCRATCH')
                raise RuntimeError('OFFLINE_WRITE_BLOCKED')
        def guard(event,args):
            # fork is intentionally allowed for six governor contenders. They
            # inherit this guard. No exec/spawn or network operation is allowed.
            if (event in ('socket.connect','socket.connect_ex','socket.getaddrinfo','socket.gethostbyname',
                          'socket.gethostbyaddr','socket.sendto','socket.sendmsg','subprocess.Popen','os.system',
                          'os.posix_spawn','os.forkpty') or event.startswith(('os.exec','os.spawn'))):
                blocked.append(event)
                raise RuntimeError('OFFLINE_NETWORK_OR_EXEC_BLOCKED')
            if event=='sqlite3.connect':write_path(os.fsdecode(args[0]).removeprefix('file:').split('?')[0])
            if event=='open':
                path,mode,flags=args
                if ((isinstance(mode,str) and any(x in mode for x in 'wax+'))
                        or isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
                    write_path(path)
            if event in ('os.mkdir','os.remove','os.rmdir','os.chmod','os.utime'):write_path(args[0])
            if event=='os.rename':write_path(args[0]);write_path(args[1])
            if event in ('os.symlink','os.link'):
                blocked.append(event);raise RuntimeError('OFFLINE_LINK_BLOCKED')
        sys.addaudithook(guard)
        previous=Path.cwd()
        os.chdir(scratch)
        try:
            suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern='test_hunter_bybit_stream*.py')
            result=unittest.TextTestRunner(verbosity=2).run(suite)
            good=result.wasSuccessful() and result.testsRun==31 and not result.skipped and not blocked
            print('BYBIT_STREAM_OFFLINE_ACCEPTANCE '+json.dumps(dict(tests=result.testsRun,
                  failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),
                  blocked_effects=len(blocked),passed=good)))
            return 0 if good else 1
        finally:os.chdir(previous)


if __name__=='__main__':raise SystemExit(main())
