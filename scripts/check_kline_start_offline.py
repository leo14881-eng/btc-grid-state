"""Confine startup tests to scratch, permitting only local-fixture git children."""
from contextvars import ContextVar
import os
import shlex
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]


def main():
    with tempfile.TemporaryDirectory(prefix='kline-start-offline-') as directory:
        scratch = Path(directory).resolve()
        tempfile.tempdir = str(scratch)
        blocked = []
        descriptor = ContextVar('open_dir_fd', default=None)
        original_open = os.open
        def audited_open(value, flags, mode=0o777, *, dir_fd=None):
            token = descriptor.set(dir_fd)
            try: return original_open(value, flags, mode, dir_fd=dir_fd)
            finally: descriptor.reset(token)
        os.open = audited_open
        def reject(reason):
            blocked.append(reason)
            raise RuntimeError(reason)
        def resolved(value, fd=None):
            if isinstance(value, int): return None
            path = Path(os.fsdecode(value))
            if not path.is_absolute() and isinstance(fd, int) and fd >= 0:
                path = Path(os.readlink('/proc/self/fd/'+str(fd)))/path
            return path.resolve()
        def inside(value, fd=None):
            path = resolved(value, fd)
            if path is not None and path != scratch and scratch not in path.parents:
                reject('OUTSIDE_STARTUP_FIXTURE')
        def guard(event, args):
            if event.startswith(('socket.', 'os.exec', 'os.spawn')) or event in (
                    'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.chown', 'sqlite3.connect'):
                reject('NETWORK_ACCOUNT_OR_DATABASE_FORBIDDEN')
            if event == 'subprocess.Popen':
                executable, argv, _, env = args
                if isinstance(argv, str):
                    argv = [value.strip('"') for value in shlex.split(argv, posix=False)]
                executable = executable or argv[0]
                if Path(executable).name.lower() not in ('git', 'git.exe') or '-C' not in argv:
                    reject('ONLY_LOCAL_GIT_FIXTURE_CHILD_ALLOWED')
                inside(argv[argv.index('-C')+1])
                if not env or env.get('GIT_CONFIG_GLOBAL') != os.devnull or env.get('GIT_CONFIG_NOSYSTEM') != '1':
                    reject('GIT_GLOBAL_CONFIG_FORBIDDEN')
            if event == 'open':
                value, mode, flags = args
                path = resolved(value, descriptor.get())
                if path is not None:
                    parts = {p.lower() for p in path.parts}
                    if parts & {'.ssh', '.aws', '.gnupg', 'credentials', 'shadow', 'gshadow'}:
                        reject('CREDENTIAL_READ_FORBIDDEN')
                    if ROOT in path.parents and ('results' in parts or 'ledger' in path.name.lower()):
                        reject('REAL_RESULT_OR_LEDGER_FORBIDDEN')
                if ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or
                    isinstance(flags, int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
                    inside(value, descriptor.get())
            if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'):
                index = {'os.mkdir':2, 'os.remove':1, 'os.rmdir':1, 'os.chmod':2, 'os.utime':3}[event]
                inside(args[0], args[index] if len(args)>index else None)
            if event == 'os.rename':
                inside(args[0]); inside(args[1])
            if event in ('os.symlink', 'os.link'):
                reject('LINK_FORBIDDEN')
        sys.addaudithook(guard)
        suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'), pattern='test_hunter_kline_start.py')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        good = result.wasSuccessful() and result.testsRun == 16 and not result.skipped and not blocked
        print('KLINE_START_OFFLINE_ACCEPTANCE', dict(tests=result.testsRun, skips=len(result.skipped),
              blocked_effects=len(blocked), passed=good))
        return 0 if good else 1


if __name__ == '__main__': raise SystemExit(main())
