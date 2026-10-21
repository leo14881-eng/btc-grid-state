"""Bybit health acceptance: loopback only; durable state stays in scratch."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
from urllib.request import url2pathname

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
sys.dont_write_bytecode = True
# Exact contract/runtime/process suite: every case must run on Linux.
EXPECTED_TESTS = 52


def main():
    blocked = []
    with tempfile.TemporaryDirectory(prefix='bybit-health-offline-') as directory:
        scratch = Path(directory).resolve()
        previous, previous_temp = Path.cwd(), tempfile.tempdir
        tempfile.tempdir = str(scratch)
        helper = (ROOT/'tests/fixtures/public_http_child.py').resolve()

        def reject(reason):
            blocked.append(reason)
            raise RuntimeError(reason)

        def inside(path, dir_fd=None):
            if isinstance(path, int): return
            text = os.fsdecode(path)
            if text.startswith('file:'):
                uri = urlsplit(text)
                if uri.netloc: reject('NONLOCAL_SQLITE_URI')
                text = url2pathname(uri.path)
            target = Path(text)
            if not target.is_absolute() and isinstance(dir_fd, int) and dir_fd >= 0:
                target = Path(os.readlink('/proc/self/fd/'+str(dir_fd)))/target
            target = target.resolve()
            if target == Path(os.devnull) and stat.S_ISCHR(target.stat().st_mode): return
            if target != scratch and scratch not in target.parents:
                reject('WRITE_OR_SQLITE_OUTSIDE_SCRATCH')

        def guard(event, args):
            if event in ('socket.bind', 'socket.connect', 'socket.connect_ex'):
                address = args[1]
                if not isinstance(address, tuple) or address[0] != '127.0.0.1':
                    reject('NON_LOOPBACK_NETWORK')
            if event == 'socket.getaddrinfo' and args[0] != '127.0.0.1': reject('DNS_FORBIDDEN')
            if event in ('socket.gethostbyname', 'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg',
                         'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty') or event.startswith(('os.exec', 'os.spawn')):
                reject('NETWORK_OR_EXEC_FORBIDDEN')
            if event == 'subprocess.Popen':
                command = args[1]
                if (not isinstance(command, (list, tuple)) or len(command) < 3
                        or command[0] != sys.executable or command[1] != '-B'
                        or Path(command[2]).resolve() != helper):
                    reject('UNAPPROVED_TRANSPORT_CHILD')
            if event == 'sqlite3.connect': inside(args[0])
            if event == 'open':
                path, mode, flags = args
                if ((isinstance(mode, str) and any(x in mode for x in 'wax+')) or
                        isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                    inside(path)
            if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'):
                index = {'os.mkdir': 2, 'os.remove': 1, 'os.rmdir': 1, 'os.chmod': 2, 'os.utime': 3}[event]
                inside(args[0], args[index] if len(args) > index else None)
            if event == 'os.rename':
                inside(args[0], args[2] if len(args) > 2 else None)
                inside(args[1], args[3] if len(args) > 3 else None)
            if event in ('os.symlink', 'os.link'): reject('LINK_FORBIDDEN')

        sys.addaudithook(guard)
        os.chdir(scratch)
        try:
            # Keep dependency-requiring transport checks out of production
            # test_hunter*.py preludes; this dedicated gate still requires all 52.
            suite = unittest.TestSuite([
                unittest.defaultTestLoader.discover(str(ROOT/'tests'), pattern=pattern)
                for pattern in ('test_hunter_health_bybit.py', 'test_bybit_health_process.py')
            ])
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            good = (result.wasSuccessful() and result.testsRun == EXPECTED_TESTS
                    and EXPECTED_TESTS > 0 and not result.skipped and not blocked)
            print('BYBIT_HEALTH_OFFLINE_ACCEPTANCE '+json.dumps(dict(tests=result.testsRun,
                failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                blocked_effects=len(blocked), passed=good, deployment_verified=False), sort_keys=True))
            return 0 if good else 1
        finally:
            os.chdir(previous)
            tempfile.tempdir = previous_temp


if __name__ == '__main__': raise SystemExit(main())
