#!/usr/bin/env python3
"""REVIEW CANDIDATE: bounded, read-only host inventory; never a deployment runner.

Run only after explicit approval through an already authorized operator terminal.
No network, installed Python execution, SQLite open, service mutation or file write.
The sole output is a redacted JSON report on stdout; stderr contains fatal errors.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import sys
import time

PREFIX = Path('/opt/hunter-binance-stream')
STATE = Path('/var/lib/hunter-binance-stream')
HEAD = '33abf19c7a91855ba2efc2e65e1d383055494d75'
OLD_HEAD = 'f4e8ad4de377a19763f4803e4fd56882c27d887a'
GUARDS = {
    'bootstrap-upgrade-once-' + OLD_HEAD + '.json':
        '84533616f40e19928f49ffc376c3282102e3cf34193f4688fe40cf16d91edec9',
    'recovery-once-51c723c224194317a49329ba8751d77f.json':
        'bc6acf8b3ab04f84f805d7778f59feac7c90f83052e2049ef0787df2bace7d1a',
}
UNITS = ('hunter-binance-stream.service', 'hunter-binance-bootstrap-f4e8ad4.service',
         'hunter-discovery.service', 'hunter-research.service',
         'hunter-position-monitor.service')
PROPERTIES = ('Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlGroup',
    'InvocationID', 'Result', 'ExecMainStatus', 'ExecMainStartTimestamp',
    'ExecMainExitTimestamp', 'FragmentPath', 'DropInPaths', 'User', 'Group',
    'SupplementaryGroups', 'DynamicUser', 'WorkingDirectory', 'ProtectSystem',
    'ProtectHome', 'PrivateTmp', 'NoNewPrivileges', 'ReadWritePaths', 'ReadOnlyPaths',
    'InaccessiblePaths', 'UMask', 'Restart', 'KillMode', 'TimeoutStopUSec',
    'MemoryMax', 'TasksMax', 'Environment', 'EnvironmentFiles', 'ExecStart')
ENV_KEYS = ('HUNTER_BINANCE_KLINE_SOURCE', 'HUNTER_SERVER_ISOLATED_CHECKOUT')
BYTE_LIMIT = 2 * 1024 * 1024
FILE_LIMIT = 384


def digest(data):
    return hashlib.sha256(data).hexdigest()


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def metadata(path):
    value = path.lstat()
    return dict(path=str(path), mode=oct(stat.S_IMODE(value.st_mode)), uid=value.st_uid,
                gid=value.st_gid, size=value.st_size, mtime_ns=value.st_mtime_ns,
                inode=value.st_ino, symlink=stat.S_ISLNK(value.st_mode))


def regular_bytes(path):
    # Every directory component is opened relative to an already held descriptor.
    # resolve()+open() cannot enforce this: a parent can be replaced in between.
    path = Path(path)
    if sys.platform != 'linux':
        raise ValueError('LINUX_DESCRIPTOR_READ_REQUIRED')
    if not path.is_absolute() or '..' in path.parts or len(path.parts) < 2:
        raise ValueError('ABSOLUTE_CANONICAL_FILE_PATH_REQUIRED')
    directories = []
    links = []
    fd = None
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_nlink,
                             s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    try:
        directories.append(os.open('/', directory_flags))
        for component in path.parts[1:-1]:
            parent = directories[-1]
            child = os.open(component, directory_flags, dir_fd=parent)
            directories.append(child)
            links.append((parent, component, child))
        parent = directories[-1]
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('REGULAR_SINGLE_LINK_FILE_REQUIRED')
        if before.st_size > BYTE_LIMIT:
            raise ValueError('FILE_SIZE_LIMIT')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            data = handle.read(BYTE_LIMIT + 1)
        after = os.fstat(fd)
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if len(data) > BYTE_LIMIT or fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(current):
            raise ValueError('FILE_CHANGED_DURING_READ')
        for parent, component, child in links:
            held = os.fstat(child)
            current = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if (not stat.S_ISDIR(current.st_mode)
                    or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError('PARENT_CHANGED_DURING_READ')
        return data
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


def command(args):
    allowed = (len(args) >= 4 and args[:3] == ['systemctl', '--no-pager', 'show']
               and args[3] in UNITS
               and args[4:] == [x for prop in PROPERTIES for x in ('-p', prop)])
    allowed = allowed or args in (
        ['getent', 'passwd', 'hunter-binance-stream'],
        ['getent', 'group', 'hunter-binance-data'],
        ['timedatectl', 'show', '-p', 'NTPSynchronized'])
    if not allowed:
        raise ValueError('COMMAND_NOT_IN_FIXED_READ_ONLY_SET')
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=10, check=False,
        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'SYSTEMD_PAGER': ''})
    if result.returncode or len(result.stdout) > BYTE_LIMIT:
        raise ValueError('READ_COMMAND_FAILED_OR_OVERSIZED')
    return result.stdout


def sanitize_show(raw):
    fields = dict(line.split('=', 1) for line in raw.decode('utf-8', 'strict').splitlines() if '=' in line)
    fields = {key: value for key, value in fields.items() if key in PROPERTIES}
    environment = fields.pop('Environment', '')
    assignments = dict(token.split('=', 1) for token in shlex.split(environment) if '=' in token)
    choices = {'HUNTER_BINANCE_KLINE_SOURCE': {'REST', 'SPOT_CACHE'},
               'HUNTER_SERVER_ISOLATED_CHECKOUT': {'0', '1'}}
    fields['configured_selector'] = {
        key: ('UNSET_IN_UNIT_ENVIRONMENT' if key not in assignments else assignments[key]
              if assignments[key] in choices[key] else 'UNRECOGNIZED_VALUE_REDACTED')
        for key in ENV_KEYS}
    exec_start = fields.pop('ExecStart', '')
    fields['exec_start_sha256'] = digest(exec_start.encode())
    fields['collector_health_flag_present'] = '--health-supervision' in exec_start
    fields['effective_runtime_selector'] = 'UNKNOWN_REQUIRES_PROCESS_OR_NATURAL_CYCLE_EVIDENCE'
    # EnvironmentFiles may override values. Never read arbitrary files named by a unit.
    fields['environment_files_present'] = bool(fields.pop('EnvironmentFiles', ''))
    return fields


def manifest_summary(raw, now):
    value = json.loads(raw)
    symbols = []
    for base, coin in value['coins'].items():
        for pair in coin.get('pairs', []):
            if pair.get('venue') == 'binance':
                symbol = pair['pair']
                if (pair.get('base') != base or symbol != base + 'USDT'
                        or not symbol.isalnum() or symbol.upper() != symbol
                        or symbol.lower().upper() != symbol or not 1 <= len(base) <= 30):
                    raise ValueError('MANIFEST_PAIR_INVALID')
                symbols.append(symbol)
    if not symbols or len(symbols) != len(set(symbols)) or 'BTCUSDT' not in symbols:
        raise ValueError('MANIFEST_ROSTER_INVALID')
    captured = dt.datetime.fromisoformat(value['as_of_utc'].replace('Z', '+00:00'))
    if captured.tzinfo is None or not value.get('generation_id'):
        raise ValueError('MANIFEST_GENERATION_INVALID')
    age = (now - captured).total_seconds()
    topics = sorted([s.lower() + '@kline_' + interval for s in symbols
                     for interval in ('15m', '1h', '1d')] + ['btcusdt@kline_5m'])
    canonical = lambda v: json.dumps(v, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return dict(as_of_utc=value['as_of_utc'], age_seconds=age,
        complete=value.get('binance_complete') is True,
        fresh_complete_at_read=value.get('binance_complete') is True and 0 <= age <= 1800,
        generation_id=value['generation_id'], source_head_sha=value.get('source_head_sha'),
        symbols=len(symbols), topics=len(topics), discovery_topics=2 * len(symbols),
        roster_hash=digest(canonical(topics)), manifest_sha256=digest(canonical(value)),
        topics_full=topics, consumer_ready='NOT_ASSESSED')


def collect():
    report = dict(schema='pr126_predeployment_readback_v1', started_at_utc=utc(),
        candidate_head=HEAD, read_only=True, deployed=False, publication_verified=False,
        approval='OPERATOR_ASSERTED_READBACK_ONLY', files={}, units_before={}, units_after={},
        errors=[], acceptance='NOT_MADE_REVIEW_REQUIRED')

    def attempt(label, fn):
        try:
            return fn()
        except Exception as error:
            report['errors'].append(dict(item=label, error=type(error).__name__))
            return None

    def read(path):
        data = regular_bytes(path)
        report['files'][str(path)] = dict(metadata(path), sha256=digest(data))
        return data

    def units():
        return {unit: attempt(unit, lambda unit=unit: sanitize_show(command(
            ['systemctl', '--no-pager', 'show', unit] + [x for p in PROPERTIES for x in ('-p', p)]))) for unit in UNITS}

    def guards():
        return {name: attempt(name, lambda name=name: digest(read(PREFIX / name))) for name in GUARDS}

    report['units_before'] = units()
    report['guards_before'] = guards()
    for path in (PREFIX, PREFIX / 'code', PREFIX / 'venv', STATE):
        report['files'][str(path)] = attempt(str(path), lambda path=path: metadata(path))
    release = attempt('release-head', lambda: read(PREFIX / 'release-head').decode('ascii').strip())
    report['release_head_before'] = release
    report['manifest'] = attempt('manifest', lambda: manifest_summary(read(STATE / 'manifest.json'), dt.datetime.now(dt.timezone.utc)))
    for name in ('state.sqlite3', 'state.sqlite3-wal', 'state.sqlite3-shm', 'collector.lock'):
        path = STATE / name
        report['files'][str(path)] = attempt(name, lambda path=path: metadata(path))
    report['sqlite_opened'] = False
    report['budget_halt_and_leases'] = 'UNKNOWN_READ_ONLY_CANDIDATE_CHECK_REQUIRED_AFTER_APPROVAL'

    def inventory():
        files = sorted(p for directory in ('research', 'scripts')
                       for p in (PREFIX / 'code' / directory).glob('*.py'))
        if not files or len(files) > FILE_LIMIT:
            raise ValueError('CODE_FILE_COUNT_LIMIT')
        for path in files:
            payload = read(path)
            if path.name in ('hunter_binance_stream_store.py', 'hunter_binance_stream_consumer.py'):
                report.setdefault('installed_code_markers', {})[path.name] = {
                    marker: marker.encode() in payload for marker in ('health_contract', 'topic_gate', 'DRAINING')}
        return dict(files=len(files), complete=True, note='Top-level runtime Python modules only; not a release authenticity proof')

    report['runtime_inventory'] = attempt('runtime_inventory', inventory)
    for unit in ('hunter-binance-stream.service',):
        attempt('unit_file', lambda unit=unit: read(Path('/etc/systemd/system') / unit))
    for label, args in (
        ('collector_user', ['getent', 'passwd', 'hunter-binance-stream']),
        ('shared_group', ['getent', 'group', 'hunter-binance-data']),
        ('clock', ['timedatectl', 'show', '-p', 'NTPSynchronized'])):
        report[label] = attempt(label, lambda args=args: command(args).decode('utf-8').strip())
    # Dependency metadata only: do not execute a preexisting interpreter or import installed code.
    def dependencies():
        paths = sorted((PREFIX / 'venv' / 'lib').glob('python*/site-packages/websockets-*.dist-info/METADATA'))
        if len(paths) != 1:
            raise ValueError('DEPENDENCY_LAYOUT_REVIEW_REQUIRED')
        payload = read(paths[0]).decode('utf-8')
        return [line for line in payload.splitlines() if line.startswith(('Name: ', 'Version: ', 'Requires-Python: '))]
    report['websockets_metadata'] = attempt('dependency', dependencies)
    report['guards_after'] = guards()
    report['guards_unchanged_during_read'] = all(report['guards_before'][n] is not None and
        report['guards_before'][n] == report['guards_after'][n] for n in GUARDS)
    report['guards_match_0237_archive'] = {n: report['guards_after'][n] == h for n, h in GUARDS.items()}
    report['release_head_after'] = attempt('release-head-after', lambda: read(PREFIX / 'release-head').decode('ascii').strip())
    report['units_after'] = units()
    report['finished_at_utc'] = utc()
    report['limitations'] = ['No SQLite or current process environment read',
        'Unit EnvironmentFiles not read; configured selectors are not runtime proof',
        'No service mutation, host file writes, new guard, permission changes or external network',
        'Before/after snapshots do not prove continuity between observations']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-readback-only', action='store_true')
    args = parser.parse_args()
    if not args.approved_readback_only:
        parser.error('Explicit operator readback approval required; this flag does not grant permission')
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Use only an existing, explicitly approved Linux operator terminal; do not escalate here')
    def deadline(*_):
        raise SystemExit('READBACK_150_SECOND_DEADLINE_NO_ACCEPTANCE')
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(150)
    started = time.monotonic()
    report = collect()
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2))
    signal.alarm(0)
    return 0 if not report['errors'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
