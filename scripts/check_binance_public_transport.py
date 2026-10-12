"""Isolated loopback/process acceptance; never permits a public market socket."""
import os
from pathlib import Path
import sys
import stat
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
sys.dont_write_bytecode=True


def main(pattern='test_hunter_binance_transport*.py',expected=28,expected_failures=0):
    with tempfile.TemporaryDirectory(prefix='public-http-offline-') as directory:
        scratch=Path(directory).resolve();tempfile.tempdir=str(scratch)
        helper=(ROOT/'tests/fixtures/binance_http_child.py').resolve()
        blocked=[]
        def reject(message):blocked.append(message);raise RuntimeError(message)
        def inside(path,dir_fd=None):
            if isinstance(path,int):return
            target=Path(os.fsdecode(path).removeprefix('file:').split('?')[0])
            if not target.is_absolute() and isinstance(dir_fd,int) and dir_fd>=0:
                target=Path(os.readlink('/proc/self/fd/'+str(dir_fd)))/target
            target=target.resolve()
            if target==Path(os.devnull) and stat.S_ISCHR(target.stat().st_mode):return
            if target!=scratch and scratch not in target.parents:reject('WRITE_OUTSIDE_SCRATCH')
        def guard(event,args):
            if event in ('socket.bind','socket.connect','socket.connect_ex'):
                address=args[1]
                if not isinstance(address,tuple) or address[0]!='127.0.0.1':reject('NON_LOOPBACK_NETWORK')
            if event=='socket.getaddrinfo' and args[0]!='127.0.0.1':reject('DNS_FORBIDDEN')
            if event in ('socket.gethostbyname','socket.gethostbyaddr','socket.sendto','socket.sendmsg',
                         'os.system','os.posix_spawn','os.fork','os.forkpty') or event.startswith(('os.exec','os.spawn')):
                reject('NETWORK_OR_EXEC_FORBIDDEN')
            if event=='subprocess.Popen':
                command=args[1]
                if len(command)<3 or command[0]!=sys.executable or command[1]!='-B' or Path(command[2]).resolve()!=helper:
                    reject('UNAPPROVED_TRANSPORT_CHILD')
            if event=='sqlite3.connect':inside(args[0])
            if event=='open':
                path,mode,flags=args
                if ((isinstance(mode,str) and any(x in mode for x in 'wax+'))
                        or isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):inside(path)
            if event in ('os.mkdir','os.remove','os.rmdir','os.chmod','os.utime'):
                index={'os.mkdir':2,'os.remove':1,'os.rmdir':1,'os.chmod':2,'os.utime':3}[event]
                inside(args[0],args[index] if len(args)>index else None)
            if event=='os.rename':inside(args[0]);inside(args[1])
            if event in ('os.symlink','os.link'):reject('LINK_FORBIDDEN')
        sys.addaudithook(guard)
        suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=pattern)
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        good=(result.wasSuccessful() and result.testsRun==expected and not result.skipped and not blocked
              and len(result.expectedFailures)==expected_failures)
        print('BINANCE_TRANSPORT_OFFLINE_ACCEPTANCE',dict(tests=result.testsRun,expected_failures=len(result.expectedFailures),blocked_effects=len(blocked),passed=good))
        return 0 if good else 1


if __name__=='__main__':raise SystemExit(main())
