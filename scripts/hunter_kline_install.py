"""Reviewed fresh installation only. Default: print plan; no server mutations.

Run --apply only after action-time approval through the existing authorized
maintenance entry. Never starts/enables/restarts a unit or switches consumers.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

RELEASE = '99f75edcf2c2f95d59e5cf797051442639dc64ce'
RUNTIME_CONFIG = {'research/results/hunter-shadow-rules.json':
                  '05c28042d99511771a856c6099fdd7afcdd56faf74c9ee9970e701dbea3e16d3'}
VENUES = ('bybit', 'binance')
PREFIX_ROOT = Path('/opt')
STATE_ROOT = Path('/var/lib')
UNIT_ROOT = Path('/etc/systemd/system')
WHEEL_NAME = 'websockets-15.0.1-py3-none-any.whl'
WHEEL_URL = ('https://files.pythonhosted.org/packages/fa/a8/'
             '5b41e0da817d64113292ab1f8247140aac61cbf6cfd085d6a0fa77f4984f/' + WHEEL_NAME)
WHEEL_SHA256 = 'f7a866fbc1e97b5c617ee4116daaa09b722101d4a3c170c787450ba409f9736f'
ENV = dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LANG='C.UTF-8',
           PYTHONDONTWRITEBYTECODE='1', SYSTEMD_PAGER='cat', SYSTEMD_COLORS='0',
           PIP_CONFIG_FILE=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def target(venue):
    user = 'hunter-' + venue + '-stream'
    return user, 'hunter-' + venue + '-data', PREFIX_ROOT / user, STATE_ROOT / user, UNIT_ROOT / (user + '.service')


def command(args, *, cwd=None, binary=False, input_data=None):
    process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=ENV,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.PIPE, start_new_session=True)
    try:
        output, errors = process.communicate(input=input_data, timeout=180)
    except BaseException:
        # venv/ensurepip can create descendants. Reap the whole process group on
        # deadline or interruption before leaving a preserved partial install.
        if sys.platform == 'linux':
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        else:
            process.kill()
        process.communicate(timeout=10)
        raise RuntimeError('COMMAND_INTERRUPTED_GROUP_REAPED ' + str(args[0])) from None
    if process.returncode:
        # Retain diagnostics privately instead of losing the actual exception.
        # Never echo arbitrary subprocess output, argv, or environment to logs.
        fd, diagnostic = tempfile.mkstemp(prefix='hunter-kline-command-', suffix='.stderr')
        with os.fdopen(fd, 'wb') as handle:
            handle.write(errors)
            handle.flush()
            os.fsync(handle.fileno())
        raise RuntimeError('COMMAND_FAILED ' + str(args[0]) + ' exit=' + str(process.returncode)
                           + ' stderr_file=' + diagnostic)
    return output if binary else output.decode('utf-8').strip()


def absent(path):
    require(not os.path.lexists(path), 'EXISTING_PATH_PRESERVED ' + str(path))


def archive_entries(data):
    entries = []
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and '..' not in name.parts and '\\' not in member.name,
                    'UNSAFE_ARCHIVE_PATH')
            require(member.isdir() or member.isfile(), 'ARCHIVE_LINK_OR_SPECIAL_FILE')
            require(member.name not in seen, 'DUPLICATE_ARCHIVE_PATH')
            seen.add(member.name)
            if member.isfile():
                entries.append((str(name), archive.extractfile(member).read(), bool(member.mode & 0o111)))
    require(entries, 'EMPTY_ARCHIVE')
    return entries


def code_entries(repo):
    require(command(['git', '-C', repo, 'rev-parse', RELEASE + '^{commit}']) == RELEASE,
            'APPROVED_RELEASE_OBJECT_REQUIRED')
    names = command(['git', '-C', repo, 'ls-tree', '-r', '--name-only', RELEASE]).splitlines()
    # Explicit runtime config is immutable release content, not a generated result.
    # No other result, portfolio or historical ledger is copied.
    names = [n for n in names if (n.endswith('.py') and n.startswith(('research/', 'scripts/')))
             or n in RUNTIME_CONFIG or n in {f'deploy/systemd/hunter-{v}-stream.service' for v in VENUES}]
    entries = archive_entries(command(['git', '-C', repo, 'archive', RELEASE, '--', *names], binary=True))
    require({name for name, _, _ in entries} == set(names), 'ARCHIVE_FILE_SET_MISMATCH')
    for name, digest in RUNTIME_CONFIG.items():
        data = next((data for n, data, _ in entries if n == name), None)
        require(data is not None and hashlib.sha256(data).hexdigest() == digest,
                'RUNTIME_CONFIG_SHA256_MISMATCH ' + name)
    for venue in VENUES:
        require(any(n == f'deploy/systemd/hunter-{venue}-stream.service' for n, _, _ in entries),
                'RELEASE_UNIT_REQUIRED')
    return entries


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('DEPENDENCY_REDIRECT_REFUSED')


def wheel_bytes():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(WHEEL_URL, timeout=30) as response:
        require(response.status == 200, 'DEPENDENCY_HTTP_STATUS')
        data = response.read(2_000_001)
    require(len(data) <= 2_000_000, 'DEPENDENCY_TOO_LARGE')
    return data


def check_wheel(data):
    require(hashlib.sha256(data).hexdigest() == WHEEL_SHA256, 'DEPENDENCY_SHA256_MISMATCH')


def preflight(repo):
    require(sys.platform == 'linux' and os.geteuid() == 0, 'AUTHORIZED_LINUX_ROOT_ENTRY_REQUIRED')
    require(sys.version_info >= (3, 10), 'PYTHON_310_REQUIRED')
    import ensurepip  # Read-only check; never install apt packages or change system Python.
    import sqlite3
    import venv
    import pwd
    import grp
    for executable in ('git', 'groupadd', 'useradd', 'runuser', 'systemctl', 'systemd-analyze'):
        require(shutil.which(executable, path=ENV['PATH']), 'HOST_DEPENDENCY_REQUIRED ' + executable)
    require(Path('/usr/sbin/nologin').is_file(), 'NOLOGIN_REQUIRED')
    for parent in (PREFIX_ROOT, STATE_ROOT, UNIT_ROOT):
        st = parent.lstat()
        require(parent.is_dir() and not parent.is_symlink() and st.st_uid == 0 and not st.st_mode & 0o022,
                'UNTRUSTED_PARENT ' + str(parent))
    for venue in VENUES:
        user, group, prefix, state, unit = target(venue)
        try: pwd.getpwnam(user)
        except KeyError: pass
        else: raise RuntimeError('EXISTING_USER_PRESERVED ' + user)
        try: grp.getgrnam(group)
        except KeyError: pass
        else: raise RuntimeError('EXISTING_GROUP_PRESERVED ' + group)
        for path in (prefix, state, unit, Path(str(unit) + '.d')):
            absent(path)
        require(command(['systemctl', '--no-pager', 'show', user + '.service', '-p', 'LoadState', '--value']) == 'not-found',
                'EXISTING_UNIT_PRESERVED ' + user)
    return code_entries(repo)


def write_new(path, data, mode):
    with path.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(mode)


def extract_code(prefix, entries):
    code = prefix / 'code'
    code.mkdir(mode=0o755)
    for name, data, executable in entries:
        path = code / name
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        write_new(path, data, 0o555 if executable else 0o444)
    for path in sorted((p for p in code.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        path.chmod(0o555)
    code.chmod(0o555)
    write_new(prefix / 'release-head', (RELEASE + '\n').encode(), 0o444)


def check_immutable_tree(prefix):
    for path in [prefix, *prefix.rglob('*')]:
        st = path.stat()  # Venv interpreter links must also resolve to trusted root-owned files.
        require(st.st_uid == 0 and not st.st_mode & 0o022, 'WRITABLE_INSTALL_TREE ' + str(path))


def repair_runtime_config(repo):
    """Add only missing, hash-pinned config to an otherwise exact inactive install."""
    require(sys.platform == 'linux' and os.geteuid() == 0, 'AUTHORIZED_LINUX_ROOT_ENTRY_REQUIRED')
    entries = code_entries(repo)
    pending = []
    for venue in VENUES:
        user, _, prefix, _, unit = target(venue)
        require((prefix / 'release-head').read_text().strip() == RELEASE, 'INSTALLED_RELEASE_MISMATCH')
        check_immutable_tree(prefix)
        require(command(['systemctl', '--no-pager', 'show', user + '.service', '-p', 'ActiveState', '--value']) == 'inactive',
                'COLLECTOR_MUST_BE_INACTIVE')
        for name, data, _ in entries:
            path = prefix / 'code' / name
            if name in RUNTIME_CONFIG:
                # Validate all ancestors and existing targets before either venue is written.
                for parent in (prefix, prefix/'code', prefix/'code/research', path.parent):
                    if os.path.lexists(parent):
                        st = parent.lstat()
                        require(parent.is_dir() and not parent.is_symlink() and st.st_uid == 0
                                and not st.st_mode & 0o022, 'UNTRUSTED_CONFIG_PARENT ' + str(parent))
                if not os.path.lexists(path):
                    pending.append((path, data))
                    continue
                st = path.lstat()
                require(path.is_file() and not path.is_symlink() and st.st_uid == 0
                        and st.st_mode & 0o777 == 0o444, 'EXISTING_CONFIG_PRESERVED ' + str(path))
            require(path.read_bytes() == data, 'INSTALLED_CODE_MISMATCH ' + name)
        require(unit.read_bytes() == (prefix/'code/deploy/systemd'/unit.name).read_bytes(), 'INSTALLED_UNIT_MISMATCH')
    for path, data in pending:
        if not path.parent.exists():
            path.parent.mkdir(mode=0o555)
            path.parent.chmod(0o555)
        write_new(path, data, 0o444)  # Exclusive creation; no overwrite or permission repair.
        require(hashlib.sha256(path.read_bytes()).hexdigest() == RUNTIME_CONFIG['research/results/hunter-shadow-rules.json'],
                'WRITTEN_CONFIG_SHA256_MISMATCH')
        print('RUNTIME_CONFIG_ADDED ' + str(path), flush=True)
    for venue in VENUES:
        prefix = target(venue)[2]
        probe = ('from scripts.hunter_' + venue + '_stream_ops import prepare; '
                 + ('from research.hunter_bybit_worker import validate; ' if venue == 'bybit' else '')
                 + 'print("STAGED_IMPORT_OK")')
        require(command([prefix/'venv/bin/python', '-E', '-s', '-B', '-c', probe], cwd=prefix/'code')
                == 'STAGED_IMPORT_OK', 'STAGED_IMPORT_PROBE_FAILED ' + venue)
        print('STAGED_IMPORT_OK ' + venue, flush=True)
    print('RUNTIME_CONFIG_VERIFIED_NOT_STARTED release=' + RELEASE + ' added=' + str(len(pending)))


def apply(repo):
    entries = preflight(repo)  # All targets checked before any account/path mutation.
    wheel = wheel_bytes()
    check_wheel(wheel)        # Download/hash failure also precedes provisioning.
    previous_umask = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory(prefix='hunter-kline-install-') as temporary:
            wheel_path = Path(temporary) / WHEEL_NAME
            write_new(wheel_path, wheel, 0o400)
            for venue in VENUES:
                user, group, prefix, state, unit = target(venue)
                print('INSTALL_STEP ' + venue + ' CREATE_IDENTITIES', flush=True)
                # First mutation is exclusive group creation; concurrent/repeated installs stop.
                command(['groupadd', '--system', group])
                command(['useradd', '--system', '--gid', group, '--no-create-home',
                         '--no-user-group', '--home-dir', '/nonexistent', '--shell',
                         '/usr/sbin/nologin', '--password', '!', user])
                prefix.mkdir(mode=0o755)
                extract_code(prefix, entries)
                print('INSTALL_STEP ' + venue + ' CREATE_VENV_AND_INSTALL_VERIFIED_WHEEL', flush=True)
                command([sys.executable, '-I', '-B', '-m', 'venv', prefix / 'venv'])
                py = prefix / 'venv/bin/python'
                command([py, '-I', '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check',
                         'install', '--no-index', '--no-deps', '--no-cache-dir', str(wheel_path)])
                command([py, '-I', '-B', '-c', 'import sqlite3; from importlib.metadata import version; '
                         'from websockets.sync.client import connect; assert version("websockets")=="15.0.1"'])
                check_immutable_tree(prefix)
                import pwd
                import grp
                state.mkdir(mode=0o770)
                os.chown(state, pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid)
                state.chmod(0o2770)
                write_new(unit, (prefix / 'code/deploy/systemd' / unit.name).read_bytes(), 0o644)
            print('INSTALL_STEP both VERIFY_NEW_UNITS', flush=True)
            command(['systemd-analyze', 'verify', *[target(v)[4] for v in VENUES]])
            for venue in VENUES:
                user, _, prefix, _, _ = target(venue)
                print('INSTALL_STEP ' + venue + ' INITIALIZE_EMPTY_CACHE', flush=True)
                os.umask(0o007)
                try:
                    command(['runuser', '-u', user, '--', prefix / 'venv/bin/python', '-B', '-m',
                             f'scripts.hunter_{venue}_stream_ops', 'initialize'], cwd=prefix / 'code')
                finally:
                    os.umask(0o022)
            print('INSTALL_STEP both DAEMON_RELOAD_WITHOUT_START', flush=True)
            command(['systemctl', '--no-pager', 'daemon-reload'])
    finally:
        os.umask(previous_umask)
    print('INSTALLED_NOT_STARTED ' + RELEASE)


def prepare_readback(repo, readback):
    require(sys.platform == 'linux' and os.geteuid() == 0, 'AUTHORIZED_LINUX_ROOT_ENTRY_REQUIRED')
    require(re.fullmatch('[0-9a-f]{40}', readback) and readback != RELEASE, 'FRESH_READBACK_SHA_REQUIRED_NOT_CODE_SHA')
    require(command(['git', '-C', repo, 'rev-parse', 'origin/main']) == readback, 'PINNED_ORIGIN_MAIN_READBACK_REQUIRED')
    entries = code_entries(repo)
    snapshots = {}
    source_paths = dict(bybit='research/results/hunter-bybit-worker-snapshot.json',
                        binance='research/results/hunter-cex-universe-latest.json')
    for venue in VENUES:
        user, _, prefix, state, unit = target(venue)
        require((prefix / 'release-head').read_text().strip() == RELEASE, 'INSTALLED_RELEASE_MISMATCH')
        check_immutable_tree(prefix)
        for name, data, _ in entries:
            require((prefix / 'code' / name).read_bytes() == data, 'INSTALLED_CODE_MISMATCH ' + name)
        require(unit.read_bytes() == (prefix / 'code/deploy/systemd' / unit.name).read_bytes(), 'INSTALLED_UNIT_MISMATCH')
        require((state / 'state.sqlite3').is_file(), 'INITIALIZED_STATE_REQUIRED')
        require(command(['systemctl', '--no-pager', 'show', user + '.service', '-p', 'ActiveState', '--value']) == 'inactive',
                'COLLECTOR_MUST_BE_INACTIVE')
        snapshots[venue] = command(['git', '-C', repo, 'show', readback + ':' + source_paths[venue]], binary=True)
        # Validate both complete rosters/ages before writing either manifest.
        validate = ('import json,sys,datetime as d; from scripts.hunter_' + venue +
                    '_stream_ops import prepare; prepare(json.load(sys.stdin),sys.argv[1],d.datetime.now(d.timezone.utc))')
        command([prefix / 'venv/bin/python', '-E', '-s', '-B', '-c', validate, readback],
                cwd=prefix / 'code', input_data=snapshots[venue])
    previous_umask = os.umask(0o007)
    try:
        with tempfile.TemporaryDirectory(prefix='hunter-kline-readback-') as temporary:
            directory = Path(temporary)
            directory.chmod(0o755)  # Public exchange snapshots only; no secrets or environment dumps.
            for venue in VENUES:
                user, _, prefix, _, _ = target(venue)
                snapshot = directory / (venue + '.json')
                write_new(snapshot, snapshots[venue], 0o444)
                command(['runuser', '-u', user, '--', prefix / 'venv/bin/python', '-E', '-s', '-B', '-m',
                         f'scripts.hunter_{venue}_stream_ops', 'prepare', '--snapshot', snapshot,
                         '--source-head', readback], cwd=prefix / 'code')
    finally:
        os.umask(previous_umask)
    print('PREPARED_NOT_STARTED readback=' + readback + ' release=' + RELEASE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, help='Local repository containing the approved release object; never fetched/reset here')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--apply', action='store_true', help='Only after explicit action-time approval; creates accounts/paths/units/state')
    actions.add_argument('--prepare-readback', help='After installation: validate/capture both fresh natural-cycle snapshots; never starts services')
    actions.add_argument('--repair-runtime-config', action='store_true', help='Add only missing hash-pinned runtime config; preserve accounts, venvs, units and caches')
    args = parser.parse_args()
    if not args.apply and not args.prepare_readback and not args.repair_runtime_config:
        print(json.dumps(dict(status='PLAN_ONLY_NO_MUTATIONS', release=RELEASE,
            venues=VENUES, wheel_url=WHEEL_URL, wheel_sha256=WHEEL_SHA256,
            new_accounts=[dict(user=target(v)[0], group=target(v)[1], shell='/usr/sbin/nologin',
                               password='locked', home_created=False) for v in VENUES],
            install_root='root:root 0755', code='root:root dirs 0555, files 0444/0555',
            venv='root-owned; group/other write forbidden', cache='venue user/group 2770; umask 0007',
            units='root:root 0644; original reviewed sandbox; Restart=no',
            daemon_reload=True, initializes_empty_caches=True, apt_or_global_pip=False,
            repeat_policy='stop and preserve every existing account/path/state',
            runtime_config_sha256=RUNTIME_CONFIG,
            starts_services=False, changes_consumers=False, writes_ledgers=False), indent=2))
        return 0
    require(args.repo is not None, 'SOURCE_REPO_REQUIRED')
    if args.repair_runtime_config:
        repair_runtime_config(args.repo.resolve())
    elif args.prepare_readback:
        prepare_readback(args.repo.resolve(), args.prepare_readback)
    else:
        apply(args.repo.resolve())
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('INSTALL_STOPPED_PRESERVE_PARTIAL_STATE ' + str(error), file=sys.stderr)
        raise SystemExit(1)
