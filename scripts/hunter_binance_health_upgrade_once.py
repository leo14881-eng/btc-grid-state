"""One separately approved Binance collector upgrade; default is a side-effect-free plan.

Existing root operator terminal only. No permissions provisioning, guard reset,
consumer selector change, policy/ledger mutation, Worker fallback or automatic retry.
--run must be owned by the named transient supervisor with this script as its exit hook.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import tarfile
import time
import types

OLD = 'f4e8ad4de377a19763f4803e4fd56882c27d887a'
NEW = '33abf19c7a91855ba2efc2e65e1d383055494d75'
LOCK_HASH = '417ec411c5cdc17ab597873e585b4b8ebb7ac270f9f4fbe8dffac2213d89a6c1'
UPGRADER_HASH = 'e0615746c73809d258b858f4058cbd346b2df9e011a8d7df819f8e5f77808ddc'
WRAPPER_HASH = '05aab679ee1f4b3cc53ad4fcf366959340fe79a0856e55fa066b07a77c93fdfa'
JOURNAL_HASH = '51d7db101269b8253cf04f2592342baefbe8ccf76f982b21209bf18978a05355'
REPO = Path('/opt/shadow-runner/btc-grid-state')
PREFIX = Path('/opt/hunter-binance-stream')
STATE = Path('/var/lib/hunter-binance-stream')
UNIT = Path('/etc/systemd/system/hunter-binance-stream.service')
DROP_DIR = Path(str(UNIT) + '.d')
DROP = DROP_DIR / '90-health-upgrade.conf'
SERVICE = 'hunter-binance-stream.service'
SUPERVISOR = 'hunter-binance-health-upgrade-33abf19c.service'
OLD_SUPERVISOR = 'hunter-binance-bootstrap-f4e8ad4.service'
OLD_INVOCATION = 'fbdca04a45154c179f0aaf9f6a4f54d1'
OLD_COLLECTOR = 'de6766b8511c4d22a68e308cf24d26cd'
OLD_EPOCH = 'a437b978bd914068bdc1c39bb9cc8fda'
GUARDS = {
    'bootstrap-upgrade-once-' + OLD + '.json': '84533616f40e19928f49ffc376c3282102e3cf34193f4688fe40cf16d91edec9',
    'recovery-once-51c723c224194317a49329ba8751d77f.json': 'bc6acf8b3ab04f84f805d7778f59feac7c90f83052e2049ef0787df2bace7d1a'}
HISTORY_HASH = '7c5fd02902695542cfb869a04df459e6bdd6ce7640896ba440fd42f549b9cb10'
BOOT = '50a415e8-e1ec-4e92-a972-cdf4c0481bde'
SUPERVISOR_SECONDS = 1800
ACCEPTANCE_SECONDS = 1200
STOP_SECONDS = 110
FINALIZE_SECONDS = 240
MAX_SCAN_AGE = 1200


def require(value, reason):
    if not value: raise RuntimeError(reason)


def digest(raw): return hashlib.sha256(raw).hexdigest()


def canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def marker(): return PREFIX / ('health-upgrade-once-' + NEW + '.json')


def command(args, *, timeout=30, check=True):
    result = subprocess.run([str(x) for x in args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'})
    require(not check or result.returncode == 0, 'COMMAND_FAILED_' + Path(str(args[0])).name)
    return result


def regular(path, private=False):
    require(path.resolve() == path and not path.is_symlink(), 'SYMLINK_OR_LAYOUT_CHANGED')
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == 0,
            'ROOT_SINGLE_REGULAR_FILE_REQUIRED')
    require(not stat.S_IMODE(before.st_mode) & (0o077 if private else 0o022), 'FILE_MODE_INVALID')
    raw = path.read_bytes(); after = path.lstat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'FILE_CHANGED_DURING_READ')
    return raw


def private_json(path): return json.loads(regular(path, True))


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def save(path, value, mode=0o600):
    raw = canonical(value) + b'\n' if not isinstance(value, bytes) else value
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    sync(path.parent)


def module(raw, expected, name):
    require(digest(raw) == expected, 'PINNED_COMPONENT_HASH_MISMATCH')
    import linecache
    filename = '<' + name + '-' + expected + '>'
    linecache.cache[filename] = (len(raw), None, raw.decode().splitlines(True), filename)
    value = types.ModuleType(name); value.__file__ = filename
    exec(compile(raw, filename, 'exec'), value.__dict__)
    return value


def bundle():
    root = Path(__file__).resolve().parent
    raw = regular(root / 'runtime-lock.json')
    require(digest(raw) == LOCK_HASH, 'RUNTIME_LOCK_HASH_MISMATCH')
    lock = json.loads(raw)
    require(lock['old'] == OLD and lock['new'] == NEW, 'RELEASE_BINDING_MISMATCH')
    entries = []
    for label in ('old', 'new'):
        raw = regular(root / ('runtime-' + label + '.tar'))
        require(digest(raw) == lock[label + '_archive_sha256'], 'ARCHIVE_HASH_MISMATCH')
        files = {}
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for item in archive:
                p = PurePosixPath(item.name)
                require(not p.is_absolute() and '..' not in p.parts and '\\' not in item.name, 'UNSAFE_ARCHIVE_PATH')
                require(item.isfile() or item.isdir(), 'ARCHIVE_SPECIAL_FILE')
                if item.isdir(): continue
                require(item.name not in files, 'DUPLICATE_ARCHIVE_FILE')
                files[item.name] = (archive.extractfile(item).read(), 0o555 if item.mode & 0o111 else 0o444)
        actual = {n: dict(sha256=digest(d), mode=m) for n, (d, m) in files.items()}
        require(actual == lock[label + '_files'], 'RUNTIME_MANIFEST_MISMATCH')
        entries.append(files)
    upgrade = module(regular(root / 'upgrade.py'), UPGRADER_HASH, 'pinned_upgrade')
    evidence = module(regular(root / 'evidence.py'), WRAPPER_HASH, 'pinned_evidence')
    upgrade.OLD, upgrade.NEW = OLD, NEW
    upgrade.PREFIX, upgrade.STATE, upgrade.UNIT, upgrade.SERVICE = PREFIX, STATE, UNIT, SERVICE
    evidence.PREFIX, evidence.STATE, evidence.REPO, evidence.SERVICE, evidence.NEW = PREFIX, STATE, REPO, SERVICE, NEW
    module(regular(root / 'journal.py'), JOURNAL_HASH, 'pinned_health_journal')
    return upgrade, evidence, entries[0], entries[1]


def show(unit, extra=()):
    fields = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlGroup', 'InvocationID', 'Job',
              'ExecMainStartTimestampMonotonic') + tuple(extra)
    raw = command(['systemctl', '--no-pager', 'show', unit, *[x for p in fields for x in ('-p', p)]]).stdout
    return dict(line.split('=', 1) for line in raw.decode().splitlines() if '=' in line)


def stopped(value):
    return (value.get('ActiveState') in ('inactive', 'failed') and value.get('MainPID') == '0'
            and value.get('Job') in ('', '0'))


def guard_hashes():
    value = {name: digest(regular(PREFIX / name, True)) for name in GUARDS}
    require(value == GUARDS, 'PREVIOUS_GUARD_CHANGED_PRESERVED')
    return value


def preflight(upgrade, old):
    """No writes, network, SQLite open or stop. Any changed installation aborts here."""
    require(not os.path.lexists(marker()), 'ATTEMPT_ALREADY_CONSUMED')
    require(not os.path.lexists(DROP_DIR), 'EXISTING_DROPINS_REQUIRE_REVIEW')
    guard_hashes(); upgrade.check_host(old)
    prior = show(OLD_SUPERVISOR)
    require(stopped(prior) and prior['InvocationID'] == OLD_INVOCATION, 'OLD_SUPERVISOR_CHANGED')
    current = show(SERVICE, ('FragmentPath', 'DropInPaths', 'NeedDaemonReload', 'Restart', 'KillMode', 'TriggeredBy'))
    require(stopped(current) and current['InvocationID'] == OLD_COLLECTOR, 'COLLECTOR_CHANGED')
    require(all(current.get(k) == v for k, v in dict(FragmentPath=str(UNIT), DropInPaths='',
        NeedDaemonReload='no', Restart='no', KillMode='control-group', TriggeredBy='').items()), 'EFFECTIVE_UNIT_CHANGED')
    # Existing root lock created by PR124; missing lock is a blocker, never recreated.
    regular(PREFIX / 'bootstrap-upgrade-operation.lock', True)
    require(Path('/proc/sys/kernel/random/boot_id').read_text().strip() == BOOT, 'BOOT_CHANGED_REVIEW_REQUIRED')
    return dict(guards=dict(GUARDS), collector=current, previous_supervisor=prior)


@contextlib.contextmanager
def operation_lock():
    import fcntl
    with (PREFIX / 'bootstrap-upgrade-operation.lock').open('rb') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def live_state():
    import contextlib, json, sqlite3
    from pathlib import Path
    with contextlib.closing(sqlite3.connect(Path('/var/lib/hunter-binance-stream/state.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row; db.execute('BEGIN')
        value = dict(meta=dict(db.execute('SELECT key,value FROM meta')),
            budget=dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone()),
            clock=[dict(x) for x in db.execute('SELECT * FROM budget_clock')],
            requests=[dict(x) for x in db.execute('SELECT * FROM requests ORDER BY token')],
            leases=[dict(x) for x in db.execute('SELECT * FROM leases ORDER BY token')],
            dispositions=[dict(x) for x in db.execute('SELECT * FROM manual_http_dispositions ORDER BY incident_epoch,token')])
    print(json.dumps(value, sort_keys=True)); return 0


def validate_state(value):
    require(value['meta'].get('connected') == '0' and value['meta'].get('epoch') == OLD_EPOCH,
            'STOPPED_CACHE_EPOCH_CHANGED')
    require(value['requests'] == [] and value['leases'] == [], 'OWNERS_REMAIN_PRESERVE_STATE')
    require(value['clock'] == [{'id': 1, 'boot': BOOT}], 'BUDGET_BOOT_CHANGED')
    now = time.monotonic_ns() // 1000000
    require(value['budget']['halt'] is None and value['budget']['cooldown_ms'] <= now
            and value['budget']['last_ms'] <= now, 'GLOBAL_STOP_OR_CLOCK_ROLLBACK_PRESERVED')
    require(digest(canonical(value['dispositions'])) == HISTORY_HASH, 'HISTORICAL_UNKNOWN_ROWS_CHANGED')


def dropin(invocation):
    require(re.fullmatch('[a-f0-9]{32}', invocation), 'INVOCATION_INVALID')
    return ('[Service]\nExecStart=\nExecStart=/opt/hunter-binance-stream/venv/bin/python -B -m research.hunter_binance_stream'
        ' --manifest /var/lib/hunter-binance-stream/manifest.json --source-head-file /opt/hunter-binance-stream/release-head'
        ' --collect --health-supervision\nTimeoutStopSec=90\nEnvironment=HUNTER_BINANCE_UPGRADE_INVOCATION=' + invocation + '\n').encode()


def work_for(invocation): return PREFIX / ('health-upgrade-' + invocation)


def work_binding():
    invocation = os.environ.get('INVOCATION_ID', '')
    require(re.fullmatch('[a-f0-9]{32}', invocation), 'TRANSIENT_INVOCATION_REQUIRED')
    guard = private_json(marker())
    work = work_for(invocation)
    require(guard.get('invocation_id') == invocation and guard.get('old') == OLD and guard.get('new') == NEW
            and guard.get('work') == str(work), 'GUARD_BINDING_MISMATCH')
    require(work.resolve() == work and work.is_dir() and work.stat().st_uid == 0
            and stat.S_IMODE(work.stat().st_mode) == 0o700, 'WORK_DIRECTORY_CHANGED')
    return invocation, work


def supervisor_entry():
    invocation = os.environ.get('INVOCATION_ID', '')
    require(re.fullmatch('[a-f0-9]{32}', invocation), 'TRANSIENT_SUPERVISOR_REQUIRED')
    state = show(SUPERVISOR, ('Type', 'Restart', 'Transient', 'RuntimeMaxUSec', 'TimeoutStopUSec',
        'KillMode', 'ExecStopPost', 'StandardInput', 'StandardOutput', 'StandardError'))
    expected = dict(MainPID=str(os.getpid()), Type='exec', Restart='no', Transient='yes',
        InvocationID=invocation, RuntimeMaxUSec='30min', TimeoutStopUSec='4min', KillMode='control-group',
        StandardInput='null', StandardOutput='journal', StandardError='journal')
    require(all(state.get(k) == v for k, v in expected.items()), 'SUPERVISOR_PROPERTIES_INVALID')
    hook = state['ExecStopPost']
    require(hook.count('argv[]=') == 1 and 'path=/usr/bin/python3 ;' in hook and
        'argv[]=/usr/bin/python3 -I -B ' + str(Path(__file__).resolve()) + ' --cleanup ;' in hook
        and 'ignore_errors=no' in hook, 'INDEPENDENT_EXIT_HOOK_REQUIRED')
    return invocation


def fresh_scope(evidence):
    # This is after the durable guard: Git fetch adds only a unique audit ref.
    source = evidence.fresh_scan(None)
    captured = dt.datetime.fromisoformat(source['snapshot']['as_of_utc'].replace('Z', '+00:00'))
    require(captured.tzinfo is not None and 0 <= (dt.datetime.now(dt.timezone.utc)-captured).total_seconds() <= MAX_SCAN_AGE,
            'WAIT_FOR_NEXT_NATURAL_DISCOVERY_NO_RETRY')
    return source


def health_sample():
    import datetime as dt, hashlib, json, time
    from pathlib import Path
    from research.hunter_binance_health_store import Store
    from research.hunter_binance_stream_store import roster, content_hash
    from scripts.hunter_binance_health_check import read_sample
    manifest = json.loads(Path('/var/lib/hunter-binance-stream/manifest.json').read_bytes())
    head = Path('/opt/hunter-binance-stream/release-head').read_text().strip()
    names = roster(manifest, dt.datetime.fromisoformat(manifest['as_of_utc'].replace('Z', '+00:00')))
    store = Store.reader()
    with store.db(readonly=True) as db:
        history = [dict(x) for x in db.execute('SELECT * FROM manual_http_dispositions ORDER BY incident_epoch,token')]
    sample = read_sample(store, names, content_hash(manifest), head, time.time_ns() // 1000000,
                         clock=lambda: time.time_ns() // 1000000)
    sample['historical_unknown_sha256'] = hashlib.sha256(json.dumps(history,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    print(json.dumps(sample, sort_keys=True)); return 0


class Window:
    """Same 300s advancing-sample contract; wall time never turns replay into health."""
    def __init__(self): self.first = self.steady = self.last = self.last_steady = self.binding = None

    def observe(self, value, steady):
        stamp = value.get('observed_at')
        binding = tuple(value.get(k) for k in ('epoch', 'roster_hash', 'manifest_sha256', 'source_head'))
        good = value.get('health_verified') is True and value.get('service_status') in ('HEALTHY', 'DEGRADED')
        if (not good or type(stamp) is not int or binding != self.binding or
            (self.last_steady is not None and not 0 <= steady-self.last_steady <= 30) or
            (self.last is not None and not 0 <= stamp-self.last <= 30000)):
            self.first = self.steady = self.last = None
        self.binding = binding; self.last_steady = steady
        if not good or type(stamp) is not int: return False
        if self.first is None: self.first = stamp; self.steady = steady
        if stamp == self.last: return False
        self.last = stamp
        return min((stamp-self.first)/1000, steady-self.steady) >= 300


def alive(value, invocation):
    require(value.get('ActiveState') == 'active' and value.get('SubState') == 'running'
            and value.get('MainPID', '0') != '0' and value.get('InvocationID') == invocation,
            'COLLECTOR_EXITED_OR_INVOCATION_CHANGED')


def owned_service(work):
    """A failed preflight has no start intent and can never stop an existing service."""
    intent = private_json(work / 'start-intent.json')
    require(intent.get('supervisor_invocation') == os.environ.get('INVOCATION_ID') and intent.get('new') == NEW,
            'START_INTENT_BINDING_MISMATCH')
    require(regular(PREFIX / 'release-head') == (NEW + '\n').encode()
            and regular(DROP) == dropin(intent['supervisor_invocation']), 'START_CONFIGURATION_CHANGED')
    state = show(SERVICE)
    if stopped(state): return state
    binding = work / 'collector-binding.json'
    # A durable start intent + the unique immutable attempt drop-in owns a queued
    # or activating start too. There was no job at admission. Do not confuse it
    # with a stopped service just because PID1 has not executed its process yet.
    if state.get('MainPID') == '0' and not binding.exists():
        require(state.get('Job') not in (None, '', '0') or state.get('ActiveState') == 'activating',
                'UNOWNED_ZERO_PID_STATE')
        return state
    origin = int(state.get('ExecMainStartTimestampMonotonic', '0'))
    require(origin >= intent['started_monotonic_us'] and origin <= intent['started_monotonic_us'] + 90000000,
            'COLLECTOR_START_OUTSIDE_OWNED_WINDOW')
    if binding.exists(): require(private_json(binding)['invocation_id'] == state.get('InvocationID'), 'COLLECTOR_OWNERSHIP_CHANGED')
    else: require(re.fullmatch('[a-f0-9]{32}', state.get('InvocationID', '')), 'COLLECTOR_INVOCATION_UNKNOWN')
    return state


def stopped_processes(state):
    import pwd
    require(stopped(state), 'COLLECTOR_NOT_STOPPED')
    uid = pwd.getpwnam('hunter-binance-stream').pw_uid
    for path in Path('/proc').glob('[0-9]*/status'):
        try: lines = path.read_text().splitlines()
        except FileNotFoundError: continue
        require(not any(line.startswith('Uid:') and uid in map(int, line.split()[1:]) for line in lines), 'COLLECTOR_UID_PROCESS_REMAINS')
    group = state.get('ControlGroup', '')
    if group:
        require(group.startswith('/') and '..' not in PurePosixPath(group).parts and group.endswith('/' + SERVICE), 'CGROUP_BINDING_INVALID')
        root = Path('/sys/fs/cgroup') / group.lstrip('/')
        for path in root.rglob('cgroup.procs') if root.exists() else ():
            require(not path.read_text().strip(), 'COLLECTOR_CGROUP_NOT_EMPTY')


def stop_owned(work):
    result = dict(stopped_verified=False, stop_attempted=False)
    try:
        state = owned_service(work)
        if not stopped(state):
            result['stop_attempted'] = True
            try: result['stop_returncode'] = command(['systemctl', 'stop', SERVICE], timeout=STOP_SECONDS, check=False).returncode
            except subprocess.TimeoutExpired: result['stop_timed_out'] = True
        state = show(SERVICE); stopped_processes(state)
        result.update(stopped_verified=True, service=state)
    except BaseException as error:
        result.update(error_type=type(error).__name__, reason=str(error)[:180])
    return result


def sample(evidence, prepared):
    value = json.loads(evidence.child(health_sample, timeout=25).stdout)
    require(value.get('historical_unknown_sha256') == HISTORY_HASH, 'HISTORICAL_UNKNOWN_ROWS_CHANGED')
    require(value.get('publication_verified') is False and value.get('shadow_only') is True and
        value.get('real_trading_enabled') is False and value.get('capital_authority') == 'NONE_SHADOW_ONLY', 'SAFETY_BINDING_CHANGED')
    if value.get('health_verified'):
        now = time.time_ns() // 1000000
        stamp, verified = value.get('observed_at'), value.get('health_verified_at')
        require(type(stamp) is int and type(verified) is int and stamp <= verified <= now <= stamp + 30000
                and type(value.get('queue_progress_at')) is int and 0 <= now-value['queue_progress_at'] <= 30000
                and type(value.get('oldest_active_ms')) is int and 0 <= value['oldest_active_ms'] + now-stamp <= 45000
                and isinstance(value.get('shards'), list) and bool(value['shards'])
                and all(type(s.get('observed_at')) is int and 0 <= now-s['observed_at'] <= 90000 for s in value['shards']),
                'HEALTH_EXPIRED_AT_PARENT_RECEIVE')
        require(value.get('source_head') == NEW and value.get('manifest_sha256') == prepared['snapshot_sha256']
                and value.get('roster_hash') == prepared['roster_hash'] and value.get('topics') == len(prepared['topics']),
                'HEALTH_SCOPE_BINDING_CHANGED')
    return value



def journal(work, prepared, collector, *, require_final=False, epoch=None):
    parser = module(regular(Path(__file__).resolve().parent / 'journal.py'), JOURNAL_HASH, 'pinned_health_journal')
    require(re.fullmatch('[a-f0-9]{32}', collector), 'JOURNAL_COLLECTOR_BINDING_REQUIRED')
    since = private_json(work / 'start-intent.json')['since']
    raw = command(['journalctl', '--utc', '--no-pager', '--all', '-u', SERVICE,
        '_SYSTEMD_INVOCATION_ID=' + collector, '--since', since, '-n', '10000', '-o', 'json'], timeout=30).stdout
    expected = dict(source_head=NEW, manifest_sha256=prepared['snapshot_sha256'],
        roster_hash=prepared['roster_hash'], topics=prepared['topics'], collector_invocation=collector)
    if epoch is not None: expected['epoch'] = epoch
    return parser.parse_journal(raw, expected=expected, require_final=require_final)


def apply(upgrade, evidence, old, new, invocation):
    before_host = preflight(upgrade, old)
    work = work_for(invocation)
    require(not os.path.lexists(work), 'WORK_ALREADY_EXISTS')
    with upgrade.maintenance():
        preflight(upgrade, old)
        save(marker(), dict(schema='binance_health_upgrade_once_v1', old=OLD, new=NEW,
            invocation_id=invocation, work=str(work), previous_guards=GUARDS,
            phase='BEFORE_MUTATION', automatic_retry=False, service_starts=1))
        work.mkdir(mode=0o700); sync(PREFIX)
        save(work / 'preflight.json', before_host)
        hashes = upgrade.state_evidence(work)
        before = json.loads(evidence.child(live_state).stdout); validate_state(before)
        save(work / 'state-before.json', before)
        source = fresh_scope(evidence); save(work / 'source.json', source)
        save(work / 'receipt.json', dict(old=OLD, new=NEW, old_files=upgrade.tree_manifest(old),
            new_files=upgrade.tree_manifest(new), state_hashes=hashes, previous_guards=GUARDS))
        _, staged, _ = upgrade.code_paths(PREFIX, work)
        upgrade.stage(staged, new); upgrade.write_new(work / 'new-release-head', (NEW + '\n').encode(), 0o444)
        upgrade.run([PREFIX / 'venv/bin/python', '-E', '-s', '-B', '-c',
            'from research import hunter_binance_health, hunter_binance_health_store; from scripts import hunter_binance_health_check'], cwd=staged)
        upgrade.quiet(); upgrade.state_unchanged(hashes)
        upgrade.switch(PREFIX, work, old, new)
        require(json.loads(evidence.child(live_state).stdout) == before, 'STATE_CHANGED_DURING_SWAP')
        prepared = json.loads(evidence.child(evidence.prepare_manifest, json.dumps(source).encode()).stdout)['receipt']
        save(work / 'prepared.json', prepared)
        require(json.loads(evidence.child(live_state).stdout) == before, 'STATE_CHANGED_DURING_MANIFEST_PREPARE')
        guard_hashes(); upgrade.quiet()
        DROP_DIR.mkdir(mode=0o755); save(DROP, dropin(invocation), 0o644); sync(DROP_DIR.parent)
        command(['systemctl', 'daemon-reload'])
        config = show(SERVICE, ('DropInPaths', 'NeedDaemonReload', 'Restart', 'KillMode', 'TimeoutStopUSec'))
        require(config.get('DropInPaths') == str(DROP) and config.get('NeedDaemonReload') == 'no'
                and config.get('TimeoutStopUSec') == '1min 30s' and config.get('Restart') == 'no'
                and config.get('KillMode') == 'control-group', 'RELOADED_UNIT_MISMATCH')
        save(work / 'installed.json', dict(head=NEW, dropin_sha256=digest(regular(DROP)), unit=config))
    return supervise(upgrade, evidence, work, prepared, invocation)


def supervise(upgrade, evidence, work, prepared, invocation):
    began = time.monotonic(); deadline = began + ACCEPTANCE_SECONDS
    since = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    intent = dict(supervisor_invocation=invocation, new=NEW, started_monotonic_us=time.monotonic_ns()//1000, since=since)
    save(work / 'start-intent.json', intent)
    command(['systemctl', 'start', '--no-block', SERVICE], timeout=30)
    current = owned_service(work)
    while (current.get('MainPID') == '0' or current.get('Job') not in ('', '0')) and time.monotonic() - began < 60:
        require(not stopped(current), 'COLLECTOR_EXITED_BEFORE_BINDING')
        time.sleep(1); current = owned_service(work)
    require(current.get('MainPID') != '0' and current.get('Job') in ('', '0'), 'COLLECTOR_START_DEADLINE')
    collector = current['InvocationID']; save(work / 'collector-binding.json', dict(invocation_id=collector, service=current))
    window = Window(); samples = []
    while time.monotonic() < deadline:
        alive(show(SERVICE), collector)
        value = sample(evidence, prepared); samples.append(value)
        require(time.monotonic() < deadline, 'ACCEPTANCE_DEADLINE_EXPIRED')
        save(work / ('sample-%04d.json' % len(samples)), value)
        if window.observe(value, time.monotonic()):
            guard_hashes(); alive(show(SERVICE), collector)
            journal_report = journal(work, prepared, collector, epoch=value['epoch'])
            require(journal_report.get('complete') is True, 'JOURNAL_EVIDENCE_INCOMPLETE')
            report = dict(schema='binance_health_upgrade_acceptance_v1', old=OLD, new=NEW,
                supervisor_invocation=invocation, collector_invocation=collector, epoch=value['epoch'],
                collector_accepted=True, data_status=value.get('data_status', 'UNKNOWN'),
                publication_verified=False, historical_unknown_sha256=HISTORY_HASH,
                status='COLLECTOR_ACCEPTED_CONSUMERS_NOT_ENABLED', sample_count=len(samples),
                final_sample=value, journal=journal_report, awaiting_independent_exit_hook=True)
            save(work / 'acceptance.json', report)
            print(json.dumps(dict(event='CANDIDATE_HEALTH_ACCEPTED_AWAITING_EXIT_HOOK', report=str(work / 'acceptance.json'),
                report_sha256=digest(regular(work / 'acceptance.json', True)), publication_verified=False)), flush=True)
            return 0
        print(json.dumps(dict(event='HEALTH_UPGRADE_SAMPLE', **value)), flush=True)
        time.sleep(min(10, max(0, deadline-time.monotonic())))
    raise RuntimeError('ACCEPTANCE_DEADLINE_EXPIRED')


def cleanup():
    """Independent exit hook: missing/torn success never leaves an owned collector running."""
    invocation = os.environ.get('INVOCATION_ID', '')
    # No guard means preflight failed; never stop or alter anything.
    if not os.path.lexists(marker()):
        print(json.dumps(dict(event='EXIT_WITHOUT_GUARD_NO_ACTION', invocation=invocation)), flush=True); return 1
    invocation, work = work_binding()
    if not (work / 'start-intent.json').exists():
        save(work / 'exit-before-start.json', dict(invocation=invocation, status='ABORTED_BEFORE_START',
             old_guards_preserved=guard_hashes() == GUARDS, collector_accepted=False, publication_verified=False))
        return 1
    normal = (os.environ.get('SERVICE_RESULT') == 'success' and os.environ.get('EXIT_CODE') == 'exited'
              and os.environ.get('EXIT_STATUS') == '0' and not (work / 'failure.json').exists())
    if normal:
        try:
            report = private_json(work / 'acceptance.json')
            require(report.get('collector_accepted') is True and report.get('new') == NEW and
                report.get('supervisor_invocation') == invocation and report.get('publication_verified') is False,
                'ACCEPTANCE_BINDING_INVALID')
            alive(owned_service(work), report['collector_invocation']); guard_hashes()
            _, evidence, _, _ = bundle()
            final = sample(evidence, private_json(work / 'prepared.json'))
            require(final.get('health_verified') is True and final.get('epoch') == report['epoch'], 'EXIT_HEALTH_CHANGED')
            alive(owned_service(work), report['collector_invocation'])
            save(work / 'supervisor-final.json', dict(status='ACCEPTED_AFTER_NORMAL_SUPERVISOR_EXIT',
                supervisor_invocation=invocation, collector_invocation=report['collector_invocation'],
                collector_accepted=True, publication_verified=False, data_status=final.get('data_status', 'UNKNOWN'),
                acceptance_sha256=digest(regular(work / 'acceptance.json', True)), final_sample=final))
            print(json.dumps(dict(event='FINAL_SUPERVISOR_RECEIPT', path=str(work / 'supervisor-final.json'),
                                  collector_accepted=True, publication_verified=False)), flush=True)
            return 0
        except BaseException as error:
            print(json.dumps(dict(event='EXIT_ACCEPTANCE_REJECTED', error_type=type(error).__name__)), flush=True)
    result = stop_owned(work)
    try:
        _, evidence, _, _ = bundle()
        collector = private_json(work / 'collector-binding.json')['invocation_id']
        result['journal'] = journal(work, private_json(work / 'prepared.json'), collector, require_final=True)
    except BaseException as error: result['journal_error_type'] = type(error).__name__
    result.update(status='FAILED_STOPPED' if result['stopped_verified'] else 'STOP_UNCONFIRMED_REVIEW_REQUIRED',
                  supervisor_invocation=invocation, collector_accepted=False, publication_verified=False)
    save(work / 'supervisor-final-failure.json', result)
    print(json.dumps(dict(event='FINAL_SUPERVISOR_FAILURE', **result)), flush=True)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true'); group.add_argument('--run', action='store_true')
    group.add_argument('--cleanup', action='store_true')
    args = parser.parse_args(argv)
    if not (args.check or args.run or args.cleanup):
        print(json.dumps(dict(status='PLAN_ONLY_NOT_EXECUTED', old=OLD, new=NEW, guard=str(marker()),
            supervisor=SUPERVISOR, supervisor_seconds=SUPERVISOR_SECONDS, stop_seconds=STOP_SECONDS,
            collector_accepted=False, publication_verified=False, consumer_switch=False, automatic_retry=False)))
        return 0
    require(sys.platform == 'linux' and os.geteuid() == 0, 'EXISTING_AUTHORIZED_ROOT_LINUX_TERMINAL_REQUIRED')
    if args.cleanup: return cleanup()
    upgrade, evidence, old, new = bundle()
    preflight(upgrade, old)
    if args.check:
        print('READ_ONLY_PREFLIGHT_PASSED_NOT_AUTHORIZATION_NOT_STARTED'); return 0
    invocation = supervisor_entry()
    def interrupted(*_): raise RuntimeError('SUPERVISOR_INTERRUPTED')
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(signum, interrupted)
    try:
        with operation_lock(): return apply(upgrade, evidence, old, new, invocation)
    except BaseException as error:
        work = work_for(invocation)
        value = dict(event='HEALTH_UPGRADE_FAILED', error_type=type(error).__name__, reason=str(error)[:180],
                     collector_accepted=False, publication_verified=False, awaiting_exit_hook=True)
        try:
            if work.is_dir(): save(work / 'failure.json', value)
        except BaseException: value['failure_receipt_persisted'] = False
        print(json.dumps(value), flush=True); return 1


if __name__ == '__main__': raise SystemExit(main())
