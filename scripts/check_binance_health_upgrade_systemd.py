"""Real PID 1 exit-boundary tests, restricted to an isolated GitHub Linux runner.

Only random codex-binance-health-fixture-* transient units and /tmp fixture
directories are used. There is no production user, path, exchange or network.
The health data/window and package availability are fixture inputs. Admission,
start-intent/guard/configuration binding, systemctl stop, process termination,
durable reports and the independent ExecStopPost are the candidate's real code.
This does not claim to test an installation or the 300-second market contract.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import types
import uuid


UNIT_PREFIX = 'codex-binance-health-fixture-'
CASES = ('success', 'exit-failure', 'term', 'kill', 'timeout', 'component-loss',
         'final-receipt-failure', 'changed-invocation', 'preflight', 'no-start-guard',
         'queued-start-kill', 'start-timeout')
UNCHANGED_RUNNING = ('changed-invocation', 'preflight', 'no-start-guard')
QUEUED_START = ('queued-start-kill', 'start-timeout')


def require(value, reason):
    if not value:
        raise AssertionError(reason)


def isolated_host():
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and sys.platform == 'linux'
            and os.geteuid() == 0, 'ISOLATED_GITHUB_LINUX_ROOT_ONLY')
    require(Path('/proc/1/comm').read_text().strip() == 'systemd'
            and Path('/run/systemd/system').is_dir(), 'REAL_SYSTEMD_PID1_REQUIRED')


def unit_name(value):
    require(re.fullmatch(UNIT_PREFIX + r'(?:supervisor|collector|blocker)-[a-f0-9]{32}\.service', value),
            'NON_FIXTURE_UNIT_REFUSED')
    return value


def run(args, *, check=True, timeout=30):
    return subprocess.run([str(x) for x in args], check=check, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})


def write(path, value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode() + b'\n'
    with path.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def load(path):
    spec = importlib.util.spec_from_file_location('health_upgrade_fixture_candidate', path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def state(unit):
    unit_name(unit)
    fields = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'InvocationID', 'ControlGroup', 'Job',
              'ExecMainStartTimestampMonotonic')
    result = run(['systemctl', '--no-pager', 'show', unit,
                  *[x for key in fields for x in ('-p', key)]], check=False)
    value = dict(line.split('=', 1) for line in result.stdout.decode().splitlines() if '=' in line)
    require(result.returncode == 0 or value.get('LoadState') == 'not-found', 'SYSTEMCTL_SHOW_FAILED')
    return value


def records(unit):
    unit_name(unit)
    return [json.loads(line) for line in run(
        ['journalctl', '--no-pager', '--all', '-u', unit, '-o', 'json']).stdout.decode().splitlines()]


def start_collector(unit, blocker=None):
    unit_name(unit)
    ordering = []
    if blocker is not None:
        unit_name(blocker)
        ordering = ['--no-block', '--property=Requires=' + blocker, '--property=After=' + blocker]
    run(['systemd-run', '--quiet', '--unit', unit, '--service-type=exec', *ordering,
         '--property=Restart=no', '--property=RuntimeMaxSec=90', '--property=TimeoutStopSec=5',
         '--property=KillMode=control-group', '--property=StandardInput=null',
         '--property=StandardOutput=journal', '--property=StandardError=journal',
         '/usr/bin/sleep', '90'])
    value = state(unit)
    if blocker is None:
        require(value.get('ActiveState') == 'active' and int(value.get('MainPID', '0')) > 0,
                'FAKE_COLLECTOR_NOT_RUNNING')
    else:
        require(value.get('MainPID') == '0' and value.get('Job') not in (None, '', '0'),
                'FAKE_COLLECTOR_START_NOT_QUEUED')
    return value


def dead_fixture_processes(value, collector, before):
    """Use the exact fake PID/cgroup; never query the production account's UID."""
    require(value.get('ActiveState') in ('inactive', 'failed') and value.get('MainPID') == '0'
            and value.get('Job') in ('', '0'),
            'FIXTURE_COLLECTOR_NOT_STOPPED')
    require(not Path('/proc', before['MainPID']).exists(), 'FIXTURE_COLLECTOR_PID_REMAINS')
    group = before.get('ControlGroup', '')
    if not group and before['MainPID'] == '0':
        require(before.get('Job') not in (None, '', '0'), 'NOT_A_PREVIOUSLY_QUEUED_START')
        return
    require(group.startswith('/') and '..' not in Path(group).parts and group.endswith('/' + collector),
            'FIXTURE_CGROUP_BINDING_INVALID')
    directory = Path('/sys/fs/cgroup') / group.lstrip('/')
    for path in directory.rglob('cgroup.procs') if directory.exists() else ():
        require(not path.read_text().strip(), 'FIXTURE_CGROUP_NOT_EMPTY')


def configured_entry(root, config):
    raw = (root / 'entry.py').read_bytes()
    require(hashlib.sha256(raw).hexdigest() == config['entry_sha256'], 'CANDIDATE_COPY_HASH_CHANGED')
    entry = load(root / 'entry.py')
    entry.PREFIX, entry.STATE, entry.REPO = root / 'prefix', root / 'state', root / 'unused-repo'
    entry.UNIT = root / 'units' / config['collector']
    entry.DROP_DIR = Path(str(entry.UNIT) + '.d')
    entry.DROP = entry.DROP_DIR / '90-health-upgrade.conf'
    entry.SERVICE, entry.SUPERVISOR = unit_name(config['collector']), unit_name(config['supervisor'])
    entry.OLD_SUPERVISOR = entry.SUPERVISOR
    entry.GUARDS = {'prior-consumed-guard.json': hashlib.sha256(b'preserved old guard\n').hexdigest()}
    # The actual exit hook is this isolated driver, which invokes real cleanup.
    entry.__file__ = str(Path(__file__).resolve())
    # A fake configuration has no reference to a production executable/directory.
    entry.dropin = lambda invocation: ('[Service]\nExecStart=\nExecStart=/usr/bin/sleep 90\n'
        'Environment=HUNTER_BINANCE_UPGRADE_INVOCATION=' + invocation + '\n').encode()
    original_command = entry.command

    def trace(args):
        with (root / 'commands.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(args) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    def command(args, **kwargs):
        args = [str(x) for x in args]
        trace(args)
        if args == ['systemctl', 'start', '--no-block', entry.SERVICE]:
            # The collector is a transient fixture unit; create it at the same
            # actual start boundary, after the candidate's durable start intent.
            before = start_collector(entry.SERVICE, config['blocker'] if config['case'] in QUEUED_START else None)
            write(root / 'collector-started.json', before)
            if config['case'] in QUEUED_START:
                write(root / 'ready.json', {'invocation': os.environ['INVOCATION_ID']})
            return subprocess.CompletedProcess(args, 0, b'', b'')
        if args == ['systemctl', 'stop', entry.SERVICE]:
            return original_command(args, **kwargs)
        require(args[:3] == ['systemctl', '--no-pager', 'show']
                and args[3] in (entry.SERVICE, entry.SUPERVISOR)
                and len(args[4:]) % 2 == 0
                and all(args[index] == '-p' for index in range(4, len(args), 2)),
                'NON_FIXTURE_CANDIDATE_COMMAND_REFUSED: ' + repr(args))
        return original_command(args, **kwargs)

    entry.command = command

    def stopped_processes(value):
        before = json.loads((root / 'collector-started.json').read_bytes())
        dead_fixture_processes(value, entry.SERVICE, before)

    entry.stopped_processes = stopped_processes
    return entry


def fixture(mode):
    root = Path(__file__).resolve().parent
    require(root.parent == Path('/tmp') and root.name.startswith(UNIT_PREFIX)
            and root.stat().st_uid == 0 and root.stat().st_mode & 0o777 == 0o700,
            'PRIVATE_FIXTURE_DIRECTORY_REQUIRED')
    config = json.loads((root / 'fixture.json').read_bytes())
    entry = configured_entry(root, config)
    case = config['case']
    require(case in CASES, 'UNKNOWN_FIXTURE_CASE')
    prepared = {'snapshot_sha256': 'f' * 64, 'roster_hash': 'e' * 64, 'topics': ['FIXTUREUSDT@kline_5m']}

    def health_value():
        return dict(health_verified=True, service_status='DEGRADED', data_status='UNKNOWN',
            observed_at=time.time_ns() // 1000000, health_verified_at=time.time_ns() // 1000000,
            queue_progress_at=time.time_ns() // 1000000, oldest_active_ms=0,
            shards=[{'observed_at':time.time_ns() // 1000000}], epoch='fixture-epoch',
            source_head=entry.NEW, manifest_sha256=prepared['snapshot_sha256'],
            roster_hash=prepared['roster_hash'], topics=len(prepared['topics']),
            historical_unknown_sha256=entry.HISTORY_HASH, publication_verified=False,
            shadow_only=True, real_trading_enabled=False, capital_authority='NONE_SHADOW_ONLY')

    def ready():
        if not (root / 'ready.json').exists():
            write(root / 'ready.json', {'invocation': os.environ['INVOCATION_ID']})

    def child(_fn, *args, **kwargs):
        if mode == '--fixture':
            ready()  # supervise has persisted the real collector invocation by now.
            if case == 'exit-failure':
                raise RuntimeError('FIXTURE_SAMPLE_FAILURE')
            if case == 'changed-invocation':
                before = state(entry.SERVICE)
                run(['systemctl', 'restart', entry.SERVICE])
                replacement = state(entry.SERVICE)
                require(replacement['InvocationID'] != before['InvocationID'], 'INVOCATION_DID_NOT_CHANGE')
                write(root / 'replacement.json', replacement)
                raise RuntimeError('FIXTURE_REPLACEMENT_MUST_SURVIVE')
            if case in ('term', 'kill', 'timeout'):
                # Real PID 1 kills this real process. No fake time/signal/stop.
                time.sleep(60)
                raise RuntimeError('FIXTURE_SIGNAL_WAS_NOT_DELIVERED')
        return subprocess.CompletedProcess(['fixture-health-data'], 0,
            json.dumps(health_value()).encode(), b'')

    def journal_evidence(*args, **kwargs):
        # Real fixture journal read; no invented collector or PID 1 records.
        rows = records(entry.SERVICE)
        return {'complete': True, 'records': rows, 'at_limit': False}

    evidence = types.SimpleNamespace(child=child, journal_evidence=journal_evidence)
    # Support the candidate's dedicated health-journal adapter as well. A fake
    # sleep collector has no health payload; raw PID 1 records remain real and
    # are independently checked below, without any fabricated terminal event.
    entry.journal = lambda *_args, **_kwargs: journal_evidence()
    entry.bundle = lambda: (None, evidence, None, None)
    if mode == '--cleanup':
        if case == 'component-loss':
            def unavailable():
                raise OSError('FIXTURE_PINNED_COMPONENT_UNAVAILABLE')
            entry.bundle = unavailable
        if case == 'final-receipt-failure':
            original_save = entry.save

            def failing_save(path, value, *args, **kwargs):
                original_save(path, value, *args, **kwargs)
                if path.name == 'supervisor-final.json':
                    raise OSError('FIXTURE_FINAL_RECEIPT_FSYNC_FAILURE_AFTER_WRITE')
            entry.save = failing_save
        return entry.main(['--cleanup'])

    require(mode == '--fixture', 'UNKNOWN_FIXTURE_MODE')
    if case == 'timeout':
        original_show = entry.show

        def compressed_admission(unit, extra=()):
            result = original_show(unit, extra)
            require(unit == entry.SUPERVISOR and result['RuntimeMaxUSec'] == '2s',
                    'REAL_TWO_SECOND_RUNTIME_LIMIT_REQUIRED')
            print('FIXTURE_RUNTIME_COMPRESSION_VERIFIED 2s TO 30min ADMISSION_ONLY', flush=True)
            return dict(result, RuntimeMaxUSec='30min')
        entry.show = compressed_admission
        try:
            invocation = entry.supervisor_entry()
        finally:
            entry.show = original_show
    else:
        invocation = entry.supervisor_entry()

    if case == 'preflight':
        entry.DROP_DIR.mkdir(mode=0o755)
        ready()
        # A real earliest preflight rejection: no consumed marker, no start,
        # no production check_host or user lookup can be reached.
        try:
            entry.preflight(None, {})
        except RuntimeError as error:
            require(str(error) == 'EXISTING_DROPINS_REQUIRE_REVIEW', str(error))
            return 7
        raise AssertionError('PREFLIGHT_DID_NOT_REJECT')

    work = entry.work_for(invocation)
    work.mkdir(mode=0o700)
    entry.save(entry.marker(), dict(invocation_id=invocation, old=entry.OLD, new=entry.NEW,
                                   work=str(work)))
    if case == 'no-start-guard':
        ready()
        return 7
    entry.save(entry.PREFIX / 'release-head', (entry.NEW + '\n').encode(), 0o444)
    entry.DROP_DIR.mkdir(mode=0o755)
    entry.save(entry.DROP, entry.dropin(invocation), 0o644)
    entry.save(work / 'prepared.json', prepared)

    class FixtureHealthWindow:
        # The production Window has separate deterministic tests. This fixture
        # supplies already-qualified health so all exit cases fit in CI.
        def observe(self, value, _steady):
            return value['health_verified'] is True

    entry.Window = FixtureHealthWindow
    print('FIXTURE_MOCK_HEALTH_WINDOW_ONLY DATA_UNKNOWN PUBLICATION_FALSE', flush=True)
    return entry.supervise(None, evidence, work, prepared, invocation)


def final_evidence(supervisor, invocation, case):
    deadline = time.monotonic() + 5
    while True:
        rows = records(supervisor)
        terminal = [row for row in rows if row.get('_PID') == '1' and row.get('UNIT') == supervisor
                    and row.get('INVOCATION_ID') == invocation
                    and (row.get('MESSAGE', '').endswith(': Deactivated successfully.')
                         or 'UNIT_RESULT' in row)]
        if terminal or time.monotonic() >= deadline:
            break
        time.sleep(.05)
    require(terminal, ('MISSING_PID1_FINAL_INVOCATION_RESULT', case, rows))
    successful = [row for row in terminal if row.get('MESSAGE', '').endswith(': Deactivated successfully.')]
    require(bool(successful) == (case == 'success'), ('UNEXPECTED_PID1_RESULT', case, terminal))
    if case == 'timeout':
        require(any(row.get('UNIT_RESULT') == 'timeout' for row in terminal), 'MISSING_REAL_TIMEOUT_RESULT')
    hooks = [row for row in rows if row.get('_SYSTEMD_INVOCATION_ID') == invocation
             and 'FINAL_SUPERVISOR_RECEIPT' in row.get('MESSAGE', '')]
    if case == 'success':
        require(len(hooks) == len(successful) == 1, 'DUPLICATED_OR_MISSING_FINAL_RECEIPT')
        require(hooks[0]['_BOOT_ID'] == successful[0]['_BOOT_ID']
                and int(hooks[0]['__MONOTONIC_TIMESTAMP']) < int(successful[0]['__MONOTONIC_TIMESTAMP']),
                'FINAL_RECEIPT_NOT_BEFORE_PID1_NORMAL_EXIT')
    else:
        require(not hooks, 'FAILED_CASE_REPORTED_FINAL_SUCCESS')
    fields = ('_PID', 'UNIT', 'INVOCATION_ID', '_SYSTEMD_INVOCATION_ID', '_BOOT_ID',
              'MESSAGE_ID', 'MESSAGE', 'UNIT_RESULT', '__CURSOR', '__MONOTONIC_TIMESTAMP')
    print('REAL_SYSTEMD_FINAL_EVIDENCE', case, json.dumps(
        [{key: row[key] for key in fields if key in row} for row in hooks + terminal], sort_keys=True), flush=True)
    return rows


def swap_checks(source):
    """Validate the actual pinned bundle, then exercise real immutable renames."""
    reviewed = Path('/tmp/binance-reviewed-bundle')
    require(reviewed.resolve() == reviewed and reviewed.is_dir(), 'REVIEWED_BUNDLE_REQUIRED')
    names = ('entry.py', 'runtime-lock.json', 'runtime-old.tar', 'runtime-new.tar', 'upgrade.py', 'evidence.py', 'journal.py')
    with tempfile.TemporaryDirectory(prefix=UNIT_PREFIX + 'bundle-', dir='/tmp') as folder:
        root = Path(folder)
        root.chmod(0o700)
        for name in names:
            path = reviewed / name
            require(path.resolve() == path and path.is_file() and path.stat().st_nlink == 1,
                    'BUNDLE_FILE_NOT_SINGLE_REGULAR')
            write(root / name, path.read_bytes())
        require((root / 'entry.py').read_bytes() == (source / 'scripts' / 'hunter_binance_health_upgrade_once.py').read_bytes(),
                'BUNDLE_ENTRY_IS_NOT_TESTED_CANDIDATE')
        entry = load(root / 'entry.py')
        entry.PREFIX, entry.STATE, entry.REPO = root / 'prefix', root / 'state', root / 'unused-repo'
        entry.UNIT = root / 'unused-fixture.service'
        entry.SERVICE = UNIT_PREFIX + 'collector-' + uuid.uuid4().hex + '.service'
        upgrade, _, old, new = entry.bundle()
        require(len(old) == 110 and len(new) == 114, 'PINNED_110_TO_114_RUNTIME_REQUIRED')
        print('REAL_IMMUTABLE_BUNDLE_CHECK', entry.OLD, entry.NEW, len(old), len(new), 'PASS', flush=True)
        for boundary in range(5):
            with tempfile.TemporaryDirectory(prefix=UNIT_PREFIX + 'swap-', dir='/tmp') as swap_folder:
                prefix = Path(swap_folder)
                prefix.chmod(0o700)
                work = prefix / ('health-upgrade-' + uuid.uuid4().hex)
                work.mkdir(mode=0o700)
                upgrade.stage(prefix / 'code', old)
                upgrade.write_new(prefix / 'release-head', (entry.OLD + '\n').encode(), 0o444)
                _, staged, _ = upgrade.code_paths(prefix, work)
                upgrade.stage(staged, new)
                upgrade.write_new(work / 'new-release-head', (entry.NEW + '\n').encode(), 0o444)
                preserved = {'prior-consumed-guard.json': b'{"consumed":true,"never_reset":true}\n',
                             'opaque-state.sqlite3': b'fixture\x00unknown-dispositions\xff\n',
                             'opaque-state.sqlite3-wal': b'preserve WAL bytes\x00\n'}
                for name, payload in preserved.items():
                    write(prefix / name, payload)
                real_move = upgrade.move
                calls = [0]

                def injected_move(old_path, new_path):
                    calls[0] += 1
                    if calls[0] == boundary:
                        raise RuntimeError('FIXTURE_RENAME_FAILURE_' + str(boundary))
                    return real_move(old_path, new_path)

                upgrade.move = injected_move
                try:
                    if boundary:
                        try:
                            upgrade.switch(prefix, work, old, new)
                        except RuntimeError as error:
                            require(str(error) == 'FIXTURE_RENAME_FAILURE_' + str(boundary), str(error))
                        else:
                            raise AssertionError('RENAME_FAULT_NOT_EXERCISED')
                    else:
                        upgrade.switch(prefix, work, old, new)
                        upgrade.verify_tree(prefix / 'code', new)
                        require((prefix / 'release-head').read_bytes() == (entry.NEW + '\n').encode(),
                                'NEW_RELEASE_NOT_INSTALLED')
                        upgrade.restore(prefix, work, old, new)
                    upgrade.verify_tree(prefix / 'code', old)
                    require((prefix / 'release-head').read_bytes() == (entry.OLD + '\n').encode(),
                            'OLD_RELEASE_NOT_RESTORED')
                    for name, payload in preserved.items():
                        require((prefix / name).read_bytes() == payload, 'PRESERVED_BYTES_CHANGED: ' + name)
                finally:
                    upgrade.move = real_move
                print('REAL_IMMUTABLE_SWAP_CHECK', boundary, 'PASS', flush=True)
        print('REAL_IMMUTABLE_SWAP_ACCEPTANCE', 5, 0, True, flush=True)


def test_case(source, case):
    with tempfile.TemporaryDirectory(prefix=UNIT_PREFIX, dir='/tmp') as folder:
        root = Path(folder)
        root.chmod(0o700)
        for name in ('prefix', 'state', 'units'):
            (root / name).mkdir(mode=0o700)
        write(root / 'prefix' / 'prior-consumed-guard.json', b'preserved old guard\n')
        token = uuid.uuid4().hex
        supervisor = unit_name(UNIT_PREFIX + 'supervisor-' + token + '.service')
        collector = unit_name(UNIT_PREFIX + 'collector-' + token + '.service')
        blocker = unit_name(UNIT_PREFIX + 'blocker-' + token + '.service')
        candidate = (source / 'scripts' / 'hunter_binance_health_upgrade_once.py').read_bytes()
        config = dict(case=case, supervisor=supervisor, collector=collector, blocker=blocker,
                      entry_sha256=hashlib.sha256(candidate).hexdigest())
        write(root / 'fixture.json', config)
        write(root / 'entry.py', candidate)
        script = root / 'fixture.py'
        write(script, Path(__file__).read_bytes())
        original = None
        try:
            if case in QUEUED_START:
                blocker_script = root / 'blocker.py'
                write(blocker_script, b'import time\nfrom pathlib import Path\n'
                    b'limit = time.monotonic() + 80\n'
                    b'while not Path(__file__).with_name("release-blocker").exists():\n'
                    b'    if time.monotonic() >= limit: raise SystemExit(8)\n'
                    b'    time.sleep(0.05)\n')
                run(['systemd-run', '--quiet', '--no-block', '--unit', blocker, '--service-type=oneshot',
                     '--property=RemainAfterExit=yes', '--property=Restart=no', '--property=TimeoutStartSec=85',
                     '--property=TimeoutStopSec=5', '--property=StandardInput=null',
                     '/usr/bin/python3', '-I', '-B', str(blocker_script)])
                limit = time.monotonic() + 5
                while state(blocker).get('MainPID') == '0' and time.monotonic() < limit:
                    time.sleep(.05)
                require(state(blocker).get('ActiveState') == 'activating' and state(blocker).get('MainPID') != '0',
                        'DEPENDENCY_DID_NOT_BLOCK')
            if case in ('preflight', 'no-start-guard'):
                original = start_collector(collector)
            run(['systemd-run', '--quiet', '--unit', supervisor, '--service-type=exec',
                 '--setenv=GITHUB_ACTIONS=true', '--property=Restart=no',
                 '--property=RuntimeMaxSec=' + ('2' if case == 'timeout' else '1800'),
                 '--property=TimeoutStopSec=240', '--property=KillMode=control-group',
                 '--property=StandardInput=null', '--property=StandardOutput=journal',
                 '--property=StandardError=journal',
                 '--property=ExecStopPost=/usr/bin/python3 -I -B ' + str(script) + ' --cleanup',
                 '/usr/bin/python3', '-I', '-B', str(script), '--fixture'])
            deadline = time.monotonic() + (75 if case == 'start-timeout' else 30)
            while not (root / 'ready.json').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            require((root / 'ready.json').exists(), 'FIXTURE_DID_NOT_REACH_BOUNDARY: ' + case)
            invocation = json.loads((root / 'ready.json').read_bytes())['invocation']
            if case in ('term', 'kill', 'queued-start-kill'):
                run(['systemctl', 'kill', '--kill-whom=main',
                     '--signal=' + ('TERM' if case == 'term' else 'KILL'), supervisor])
            while state(supervisor).get('ActiveState') not in ('inactive', 'failed') and time.monotonic() < deadline:
                time.sleep(.05)
            require(state(supervisor).get('ActiveState') in ('inactive', 'failed'), 'SUPERVISOR_NOT_FINISHED')
            after = state(collector)
            require((after.get('ActiveState') == 'active') == (case == 'success' or case in UNCHANGED_RUNNING),
                    ('WRONG_COLLECTOR_LIFETIME', case, after))
            rows = final_evidence(supervisor, invocation, case)
            work = root / 'prefix' / ('health-upgrade-' + invocation)
            commands = [json.loads(line) for line in (root / 'commands.jsonl').read_text().splitlines()]
            stops = [command for command in commands if command[:2] == ['systemctl', 'stop']]
            require((root / 'prefix' / 'prior-consumed-guard.json').read_bytes() == b'preserved old guard\n',
                    'PRIOR_GUARD_MODIFIED')
            if case == 'success':
                final = json.loads((work / 'supervisor-final.json').read_bytes())
                require(final['collector_accepted'] is True and final['publication_verified'] is False
                        and final['data_status'] == 'UNKNOWN' and final['supervisor_invocation'] == invocation
                        and final['collector_invocation'] == after['InvocationID'], 'FINAL_REPORT_BINDING_INVALID')
                require(not stops and not (work / 'supervisor-final-failure.json').exists(), 'SUCCESS_STOPPED_OR_FAILED')
            elif case == 'preflight':
                require(not work.exists() and not list((root / 'prefix').glob('health-upgrade-once-*.json')),
                        'FAILED_PREFLIGHT_CONSUMED_GUARD')
                require(any('EXIT_WITHOUT_GUARD_NO_ACTION' in row.get('MESSAGE', '') for row in rows),
                        'MISSING_NO_GUARD_EXIT_EVIDENCE')
            elif case == 'no-start-guard':
                final = json.loads((work / 'exit-before-start.json').read_bytes())
                require(final['status'] == 'ABORTED_BEFORE_START' and final['old_guards_preserved'] is True
                        and final['collector_accepted'] is False, 'NO_START_REPORT_INVALID')
                require(not (work / 'start-intent.json').exists(), 'UNEXPECTED_START_INTENT')
            else:
                final = json.loads((work / 'supervisor-final-failure.json').read_bytes())
                require(final['collector_accepted'] is False and final['publication_verified'] is False
                        and final['supervisor_invocation'] == invocation, 'FAILURE_REPORT_BINDING_INVALID')
                if case == 'changed-invocation':
                    replacement = json.loads((root / 'replacement.json').read_bytes())
                    require(final['stopped_verified'] is False and final['stop_attempted'] is False
                            and final['reason'] == 'COLLECTOR_OWNERSHIP_CHANGED'
                            and after['InvocationID'] == replacement['InvocationID']
                            and after['MainPID'] == replacement['MainPID'], 'REPLACEMENT_OWNERSHIP_NOT_PRESERVED')
                else:
                    require(final['stopped_verified'] is True and final['stop_attempted'] is True
                            and final['status'] == 'FAILED_STOPPED' and stops == [['systemctl', 'stop', collector]],
                            ('OWNED_COLLECTOR_NOT_STOPPED', case, final, stops))
                    started = json.loads((root / 'collector-started.json').read_bytes())
                    dead_fixture_processes(after, collector, started)
                if case == 'component-loss':
                    require(final.get('journal_error_type') == 'OSError', 'COMPONENT_LOSS_NOT_EXERCISED')
                if case == 'final-receipt-failure':
                    require((work / 'supervisor-final.json').exists(), 'FINAL_RECEIPT_FAILURE_NOT_AFTER_WRITE')
                if case in QUEUED_START:
                    require(not (work / 'collector-binding.json').exists(), 'QUEUED_START_UNEXPECTEDLY_EXECUTED')
                    # Let the dependency finish normally. A surviving start job
                    # would now execute; cancellation must survive its release.
                    write(root / 'release-blocker', b'release normally\n')
                    release_deadline = time.monotonic() + 5
                    while state(blocker).get('SubState') != 'exited' and time.monotonic() < release_deadline:
                        time.sleep(.05)
                    require(state(blocker).get('ActiveState') == 'active' and state(blocker).get('SubState') == 'exited',
                            'DEPENDENCY_DID_NOT_FINISH_NORMALLY')
                    time.sleep(.25)
                    dead_fixture_processes(state(collector), collector,
                        json.loads((root / 'collector-started.json').read_bytes()))
                    if case == 'start-timeout':
                        require(any('COLLECTOR_START_DEADLINE' in row.get('MESSAGE', '') for row in rows),
                                'REAL_START_TIMEOUT_NOT_EXERCISED')
                    print('REAL_SYSTEMD_CANCELLED_JOB_AFTER_DEPENDENCY_SUCCESS', case, 'PASS', flush=True)
            if case in UNCHANGED_RUNNING:
                require(not stops, 'UNOWNED_SERVICE_STOP_ATTEMPTED')
            if original:
                require(after['InvocationID'] == original['InvocationID'] and after['MainPID'] == original['MainPID']
                        and Path('/proc', after['MainPID']).exists(), 'PREFLIGHT_OR_NO_START_CHANGED_EXISTING_PROCESS')
            print('REAL_SYSTEMD_EXIT_CHECK', case, 'PASS', flush=True)
        except BaseException:
            print(run(['journalctl', '--no-pager', '--all', '-u', supervisor, '-o', 'cat'],
                      check=False).stdout.decode(), flush=True)
            raise
        finally:
            # Explicit generated names only; never enumerate or wildcard units.
            for unit in (supervisor, collector, blocker) if case in QUEUED_START else (supervisor, collector):
                unit_name(unit)
                run(['systemctl', 'stop', unit], check=False)
                run(['systemctl', 'reset-failed', unit], check=False)


def main():
    isolated_host()
    if len(sys.argv) > 1:
        require(len(sys.argv) == 2 and sys.argv[1] in ('--fixture', '--cleanup'), 'UNKNOWN_ARGUMENT')
        return fixture(sys.argv[1])
    source = Path(__file__).resolve().parents[1]
    candidate = source / 'scripts' / 'hunter_binance_health_upgrade_once.py'
    print('REAL_SYSTEMD_CANDIDATE_SHA256', hashlib.sha256(candidate.read_bytes()).hexdigest(), flush=True)
    swap_checks(source)
    for case in CASES:
        test_case(source, case)
    print('REAL_SYSTEMD_EXIT_ACCEPTANCE', len(CASES), 0, True, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
