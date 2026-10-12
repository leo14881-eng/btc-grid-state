"""Run installer fixtures with network/process denial and scratch-only writes."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
sys.dont_write_bytecode = True


def main():
    with tempfile.TemporaryDirectory(prefix='kline-install-offline-') as directory:
        scratch = Path(directory).resolve()
        tempfile.tempdir = str(scratch)
        blocked = []

        def reject(reason):
            blocked.append(reason)
            raise RuntimeError(reason)

        def path(value, dir_fd=None):
            if isinstance(value, int):
                return None
            result = Path(os.fsdecode(value))
            if not result.is_absolute() and isinstance(dir_fd, int) and dir_fd >= 0:
                result = Path(os.readlink('/proc/self/fd/' + str(dir_fd))) / result
            return result.resolve()

        def inside(value, dir_fd=None):
            target = path(value, dir_fd)
            if target is not None and target != scratch and scratch not in target.parents:
                reject('WRITE_OUTSIDE_INSTALL_FIXTURE')

        def guard(event, args):
            if event.startswith(('socket.', 'os.exec', 'os.spawn')) or event in (
                    'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.chown'):
                reject('REAL_NETWORK_PROCESS_OR_ACCOUNT_MUTATION')
            if event == 'open':
                value, mode, flags = args
                target = path(value)
                if target is not None:
                    parts = {p.lower() for p in target.parts}
                    if parts & {'.aws', '.ssh', '.gnupg', 'credentials', 'shadow', 'gshadow'}:
                        reject('CREDENTIAL_READ_FORBIDDEN')
                    if ROOT in target.parents and ('results' in parts or 'ledger' in target.name.lower()
                                                   or target.suffix in ('.json', '.jsonl')):
                        reject('EXISTING_LEDGER_OR_RESULT_READ_FORBIDDEN')
                if ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or
                        isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                    inside(value)
            if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'):
                index = {'os.mkdir': 2, 'os.remove': 1, 'os.rmdir': 1, 'os.chmod': 2, 'os.utime': 3}[event]
                inside(args[0], args[index] if len(args) > index else None)
            if event == 'os.rename':
                inside(args[0]); inside(args[1])
            if event in ('os.symlink', 'os.link', 'sqlite3.connect'):
                reject('LINK_OR_REAL_DATABASE_FORBIDDEN')

        sys.addaudithook(guard)
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_hunter_kline_install.py')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        good = result.wasSuccessful() and result.testsRun == 26 and not result.skipped and not blocked
        print('KLINE_INSTALL_OFFLINE_ACCEPTANCE', dict(tests=result.testsRun, skips=len(result.skipped),
              blocked_effects=len(blocked), passed=good))
        return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
