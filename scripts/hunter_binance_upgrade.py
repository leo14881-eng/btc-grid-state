"""Exact Binance code upgrade only. Default plan; never starts units or edits cache state."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import uuid

OLD = '99f75edcf2c2f95d59e5cf797051442639dc64ce'
NEW = '99836e5362340725f806d5df6df1ca9835065bdd'
PREFIX = Path('/opt/hunter-binance-stream')
STATE = Path('/var/lib/hunter-binance-stream')
UNIT = Path('/etc/systemd/system/hunter-binance-stream.service')
SERVICE = 'hunter-binance-stream.service'
CONFIG = 'research/results/hunter-shadow-rules.json'
CONFIG_HASH = '05c28042d99511771a856c6099fdd7afcdd56faf74c9ee9970e701dbea3e16d3'
CHANGED = {'research/hunter_binance_public.py', 'research/hunter_binance_stream.py',
           'research/hunter_bybit_public.py', 'scripts/check_binance_public_transport.py'}
UNITS = {f'deploy/systemd/hunter-{venue}-stream.service' for venue in ('binance', 'bybit')}


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(args, *, cwd=None):
    result = subprocess.run([str(x) for x in args], cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    require(result.returncode == 0, 'COMMAND_FAILED_' + Path(str(args[0])).name)
    return result.stdout


def selected(name):
    return (name.endswith('.py') and name.startswith(('research/', 'scripts/'))) or name == CONFIG or name in UNITS


def release(repo, sha):
    require(run(['git', '-C', repo, 'rev-parse', sha+'^{commit}']).decode().strip() == sha, 'SOURCE_COMMIT_MISMATCH')
    names = [n for n in run(['git', '-C', repo, 'ls-tree', '-r', '--name-only', sha]).decode().splitlines() if selected(n)]
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(run(['git', '-C', repo, 'archive', sha, '--', *names]))) as archive:
        for item in archive:
            path = PurePosixPath(item.name)
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in item.name, 'UNSAFE_ARCHIVE_PATH')
            require(item.isdir() or item.isfile(), 'ARCHIVE_LINK_OR_SPECIAL_FILE')
            if item.isfile():
                require(item.name not in entries, 'DUPLICATE_ARCHIVE_FILE')
                entries[item.name] = (archive.extractfile(item).read(), 0o555 if item.mode & 0o111 else 0o444)
    require(set(entries) == set(names) and CONFIG in entries and UNITS <= entries.keys(), 'RUNTIME_FILE_SET_INVALID')
    require(digest(entries[CONFIG][0]) == CONFIG_HASH, 'POLICY_CONFIG_HASH_MISMATCH')
    return entries


def releases(repo):
    old, new = release(repo, OLD), release(repo, NEW)
    require(old.keys() == new.keys(), 'RUNTIME_FILE_SET_CHANGED')
    require({p for p in old if old[p] != new[p]} == CHANGED, 'UNREVIEWED_RUNTIME_DELTA')
    return old, new


def regular(path):
    mode = path.lstat().st_mode
    require(stat.S_ISREG(mode) and path.stat().st_nlink == 1, 'NOT_SINGLE_REGULAR_FILE_'+path.name)


def tree_manifest(entries):
    return {name: {'sha256': digest(data), 'mode': mode} for name, (data, mode) in sorted(entries.items())}


def verify_tree(root, entries, owner=0):
    require(root.is_dir() and not root.is_symlink(), 'CODE_DIRECTORY_INVALID')
    files = set()
    for path in [root, *root.rglob('*')]:
        info = path.lstat()
        require(not path.is_symlink(), 'CODE_SYMLINK_FORBIDDEN')
        if owner is not None:
            require(info.st_uid == owner, 'CODE_OWNER_MISMATCH')
        if path.is_dir():
            if sys.platform == 'linux': require(stat.S_IMODE(info.st_mode) == 0o555, 'CODE_DIRECTORY_MODE_MISMATCH')
            continue
        regular(path); name = path.relative_to(root).as_posix(); files.add(name)
        require(name in entries, 'UNEXPECTED_CODE_FILE_'+name)
        data, mode = entries[name]
        require(path.read_bytes() == data, 'CODE_HASH_MISMATCH_'+name)
        if sys.platform == 'linux': require(stat.S_IMODE(info.st_mode) == mode, 'CODE_FILE_MODE_MISMATCH')
    require(files == entries.keys(), 'INSTALLED_CODE_FILE_SET_MISMATCH')


def write_new(path, data, mode=0o600):
    with path.open('xb') as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    path.chmod(mode)  # New files only; never change existing permissions.


def stage(root, entries):
    root.mkdir(mode=0o755)
    for name, (data, mode) in entries.items():
        path = root/name; path.parent.mkdir(parents=True, exist_ok=True)
        write_new(path, data, mode)
    for path in sorted([root, *(p for p in root.rglob('*') if p.is_dir())], key=lambda p: len(p.parts), reverse=True):
        path.chmod(0o555)


def sync_dir(path):
    if sys.platform == 'linux':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)


def move(source, target):
    require(not os.path.lexists(target), 'DESTINATION_EXISTS_'+target.name)
    source.rename(target); sync_dir(source.parent)
    if source.parent != target.parent: sync_dir(target.parent)


def record(work, event):
    with (work/'journal.jsonl').open('ab') as handle:
        handle.write((json.dumps({'event': event})+'\n').encode()); handle.flush(); os.fsync(handle.fileno())


def quiet():
    fields = dict(line.split('=', 1) for line in run(['systemctl', '--no-pager', 'show', SERVICE,
        '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID', '-p', 'ControlGroup']).decode().splitlines())
    require(fields.get('ActiveState') in ('inactive', 'failed') and fields.get('MainPID') == '0', 'BINANCE_NOT_STOPPED')
    import pwd
    uid = pwd.getpwnam('hunter-binance-stream').pw_uid
    for proc in Path('/proc').glob('[0-9]*/status'):
        try: lines = proc.read_text().splitlines()
        except FileNotFoundError: continue
        require(not any(line.startswith('Uid:') and uid in map(int, line.split()[1:]) for line in lines), 'BINANCE_UID_PROCESS_ALIVE')
    group = fields.get('ControlGroup', '')
    if group:
        require(group.startswith('/') and '..' not in PurePosixPath(group).parts and group.endswith('/'+SERVICE), 'UNEXPECTED_CONTROL_GROUP')
        root = Path('/sys/fs/cgroup')/group.lstrip('/')
        for file in root.rglob('cgroup.procs') if root.exists() else ():
            require(not file.read_text().strip(), 'BINANCE_CGROUP_NOT_EMPTY')
    return fields


@contextlib.contextmanager
def maintenance():
    require(sys.platform == 'linux' and os.geteuid() == 0, 'EXISTING_ROOT_LINUX_ENTRY_REQUIRED')
    import fcntl
    for path in (PREFIX, PREFIX/'venv', STATE):
        require(path.is_dir() and path.resolve() == path, 'EXISTING_DIRECTORY_REQUIRED')
    require(PREFIX.stat().st_uid == 0 and not PREFIX.stat().st_mode & 0o022, 'PREFIX_OWNER_OR_MODE_INVALID')
    require((PREFIX/'venv').stat().st_uid == 0 and not (PREFIX/'venv').stat().st_mode & 0o022, 'VENV_OWNER_OR_MODE_INVALID')
    quiet()
    with contextlib.ExitStack() as stack:
        for path in [STATE/'collector.lock', *sorted(STATE.glob('request-*.lock'))]:
            regular(path)
            if path.name != 'collector.lock': require(re.fullmatch(r'request-[a-f0-9]{32}\.lock', path.name), 'INVALID_REQUEST_LOCK')
            handle = stack.enter_context(path.open('rb'))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        quiet()
        yield


def state_files():
    paths = []
    for path in sorted(STATE.iterdir()):
        if path.name in ('state.sqlite3', 'state.sqlite3-wal', 'state.sqlite3-shm', 'manifest.json', 'collector.lock') or re.fullmatch(r'request-[a-f0-9]{32}\.lock', path.name):
            regular(path); paths.append(path)
    require({'state.sqlite3', 'manifest.json'} <= {p.name for p in paths}, 'CACHE_EVIDENCE_MISSING')
    return paths


def state_evidence(work):
    # Opaque byte copies only: no SQLite writes/checkpoints, marker deletion or
    # request-outcome inference. Production must remain stopped after upgrade.
    backup = work/'state-evidence'; backup.mkdir(mode=0o700)
    hashes = {}
    for path in state_files():
        data = path.read_bytes(); hashes[path.name] = digest(data)
        write_new(backup/path.name, data)
    return hashes


def state_unchanged(hashes):
    current = {p.name: digest(p.read_bytes()) for p in state_files()}
    require(current == hashes, 'STATE_CHANGED_DURING_CODE_UPGRADE')


def restore(prefix, work, old, new, owner=0):
    # Also handles an interrupted swap. Unknown content is never overwritten.
    code, head = prefix/'code', prefix/'release-head'
    old_code, old_head = work/'old-code', work/'old-release-head'
    # Validate both halves before moving either. A mismatched head or backup
    # leaves the entire current installation untouched for manual review.
    if old_code.exists():
        verify_tree(old_code, old, owner)
        if code.exists(): verify_tree(code, new, owner)
    else: verify_tree(code, old, owner)
    if old_head.exists():
        regular(old_head); require(old_head.read_bytes() == (OLD+'\n').encode(), 'BACKUP_HEAD_MISMATCH')
        if head.exists():
            regular(head); require(head.read_bytes() == (NEW+'\n').encode(), 'ACTIVE_HEAD_UNKNOWN')
    else:
        regular(head); require(head.read_bytes() == (OLD+'\n').encode(), 'ACTIVE_HEAD_UNKNOWN')
    if old_code.exists():
        if code.exists(): move(code, work/'rolled-back-new-code')
        move(old_code, code)
    if old_head.exists():
        if head.exists():
            move(head, work/'rolled-back-new-release-head')
        move(old_head, head)
    regular(head); require(head.read_bytes() == (OLD+'\n').encode(), 'RESTORED_HEAD_MISMATCH')
    verify_tree(code, old, owner); record(work, 'ROLLED_BACK_NOT_STARTED')


def switch(prefix, work, old, new, owner=0):
    verify_tree(prefix/'code', old, owner); verify_tree(work/'new-code', new, owner)
    require((prefix/'release-head').read_bytes() == (OLD+'\n').encode(), 'INSTALLED_RELEASE_MISMATCH')
    try:
        for name, source, target in (
            ('SAVE_OLD_CODE', prefix/'code', work/'old-code'),
            ('INSTALL_NEW_CODE', work/'new-code', prefix/'code'),
            ('SAVE_OLD_HEAD', prefix/'release-head', work/'old-release-head'),
            ('INSTALL_NEW_HEAD', work/'new-release-head', prefix/'release-head')):
            record(work, name); move(source, target)
        verify_tree(prefix/'code', new, owner)
        require((prefix/'release-head').read_bytes() == (NEW+'\n').encode(), 'NEW_RELEASE_MISMATCH')
        record(work, 'UPGRADED_NOT_STARTED')
    except BaseException:
        restore(prefix, work, old, new, owner)
        raise


def check_host(old):
    for path in (PREFIX, PREFIX/'venv', STATE):
        require(path.is_dir() and path.resolve() == path, 'EXISTING_DIRECTORY_REQUIRED')
    regular(PREFIX/'release-head'); regular(UNIT)
    for path, mode in ((PREFIX/'release-head', 0o444), (UNIT, 0o644)):
        require(path.stat().st_uid == 0 and stat.S_IMODE(path.stat().st_mode) == mode, 'EXISTING_FILE_OWNER_OR_MODE_MISMATCH')
    require((PREFIX/'release-head').read_bytes() == (OLD+'\n').encode(), 'INSTALLED_RELEASE_MISMATCH')
    verify_tree(PREFIX/'code', old)
    require(UNIT.read_bytes() == old['deploy/systemd/'+SERVICE][0], 'INSTALLED_UNIT_MISMATCH')
    require(run([PREFIX/'venv/bin/python', '-I', '-B', '-c',
        "import importlib.metadata; print(importlib.metadata.version('websockets'))"]).strip() == b'15.0.1', 'DEPENDENCY_MISMATCH')
    quiet()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--check', action='store_true')
    actions.add_argument('--apply', action='store_true')
    actions.add_argument('--rollback', type=Path, metavar='EVIDENCE_DIRECTORY')
    args = parser.parse_args()
    if not (args.check or args.apply or args.rollback):
        print(json.dumps(dict(status='PLAN_ONLY', old=OLD, new=NEW, prefix=str(PREFIX),
            changed=sorted(CHANGED), state_action='PRESERVE_ORPHAN_AND_ALL_CACHE_STATE',
            start=False, bybit_changed=False, permissions_changed=False), sort_keys=True)); return
    require(args.repo is not None, 'LOCAL_REPOSITORY_REQUIRED_NO_AUTOMATIC_FETCH')
    old, new = releases(args.repo.resolve())
    if args.check:
        require(sys.platform == 'linux' and os.geteuid() == 0, 'EXISTING_ROOT_LINUX_ENTRY_REQUIRED')
        check_host(old); print('CHECKED_NO_MUTATIONS'); return
    with maintenance():
        if args.rollback:
            work = args.rollback.absolute()
            require(work.parent == PREFIX and re.fullmatch(r'upgrade-[a-f0-9]{32}', work.name)
                    and work.is_dir() and not work.is_symlink(), 'INVALID_EVIDENCE_DIRECTORY')
            require(work.stat().st_uid == 0 and stat.S_IMODE(work.stat().st_mode) == 0o700, 'EVIDENCE_OWNER_OR_MODE_MISMATCH')
            regular(work/'receipt.json'); receipt = json.loads((work/'receipt.json').read_text())
            require(receipt['old'] == OLD and receipt['new'] == NEW
                    and receipt['old_files'] == tree_manifest(old) and receipt['new_files'] == tree_manifest(new), 'RECEIPT_MISMATCH')
            hashes = {p.name: digest(p.read_bytes()) for p in state_files()}
            restore(PREFIX, work, old, new)
            state_unchanged(hashes); print('ROLLED_BACK_NOT_STARTED'); return
        check_host(old)
        work = PREFIX/('upgrade-'+uuid.uuid4().hex); work.mkdir(mode=0o700)
        print(json.dumps(dict(status='PREPARING_NOT_STARTED',evidence=str(work))),flush=True)
        hashes = state_evidence(work)
        write_new(work/'receipt.json', json.dumps(dict(old=OLD,new=NEW,state_hashes=hashes,
            old_files=tree_manifest(old),new_files=tree_manifest(new)),sort_keys=True).encode())
        stage(work/'new-code', new); write_new(work/'new-release-head', (NEW+'\n').encode(), 0o444)
        run([PREFIX/'venv/bin/python', '-E', '-s', '-B', '-c',
            'from research import hunter_binance_stream, hunter_binance_public; from research.hunter_policy import C'], cwd=work/'new-code')
        quiet(); state_unchanged(hashes)
        switch(PREFIX, work, old, new)
        quiet(); state_unchanged(hashes)
        print(json.dumps(dict(status='UPGRADED_NOT_STARTED',release=NEW,evidence=str(work),
                              orphan_cleared=False,bybit_changed=False),sort_keys=True))


if __name__ == '__main__':
    main()
