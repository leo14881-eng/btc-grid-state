"""Linux scratch-only DELETE/WAL lifecycle mechanism diagnostic, not a load gate.

Four cases (two journal modes x two databases), two independent child processes
per case. Production settings, connections, owner checks and 250ms busy budgets
are not changed. Explicit checkpoint operations belong only to this experiment.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import parse_qs, unquote, urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
CHILD = ROOT/'tests/fixtures/binance_wal_lifecycle_child.py'
KEY = 'diagnostic_wal_lifecycle'
MODES = ('DELETE', 'WAL')
TARGETS = ('market', 'control')
FILENAMES = {'market': 'state.sqlite3', 'control': 'control.sqlite3'}
BOUND = 4000
HELD_TARGET_SECONDS = 2.0
WRITE_INTERVAL_NS = 500_000_000
SOURCES = (SELF, CHILD, *(ROOT/'research'/name for name in (
    'hunter_binance_health_store.py', 'hunter_binance_stream_store.py',
    'hunter_binance_market_control.py', 'hunter_bybit_stream_store.py',
    'hunter_binance_control.py')))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def check(condition, reason):
    if not condition:
        raise AssertionError(reason)


def source_hashes():
    # Capture before any research imports, including package __init__ files and
    # any subsequently imported repository dependency, not just the named pair.
    paths = set(ROOT.rglob('*.py')) | set(SOURCES)
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def loaded_source_audit(before):
    paths = set()
    for module in tuple(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if filename:
            path = Path(filename).resolve()
            if ROOT in path.parents and path.suffix == '.py':
                paths.add(path)
    actual = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(paths)}
    mismatches = [name for name, value in actual.items() if before.get(name) != value]
    return dict(loaded_repo_sha256=actual, before_after_equal=not mismatches, mismatches=mismatches)


def machine():
    return dict(pid=os.getpid(), ppid=os.getppid(), nodename=os.uname().nodename,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                uid=os.getuid(), gid=os.getgid(), python=sys.version, sqlite=sqlite3.sqlite_version)


def file_state(path):
    value = {}
    for label, target in (('directory', path.parent), ('database', path),
                          ('wal', Path(str(path)+'-wal')), ('shm', Path(str(path)+'-shm')),
                          ('journal', Path(str(path)+'-journal'))):
        try:
            info = target.stat()
        except FileNotFoundError:
            value[label] = dict(exists=False)
        else:
            value[label] = dict(exists=True, dev=info.st_dev, inode=info.st_ino,
                mode=oct(stat.S_IMODE(info.st_mode)), uid=info.st_uid, gid=info.st_gid,
                bytes=info.st_size, mtime_ns=info.st_mtime_ns)
    return value


def error_detail(error):
    return dict(type=type(error).__name__, reason=str(error),
        sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
        sqlite_errorname=getattr(error, 'sqlite_errorname', None),
        market_phase=getattr(error, 'market_phase', None),
        control_phase=getattr(error, 'control_phase', None),
        sqlite_context=getattr(error, 'sqlite_context', None),
        traceback=traceback.format_exc())


def payload(index):
    # A bounded synthetic meta value, never a candle, request or trading write.
    return canonical(dict(index=index, padding=chr(65+index)*16384))


def value_summary(value):
    return dict(bytes=len(value.encode()), sha256=hashlib.sha256(value.encode()).hexdigest())


def native_argument_types(records):
    return {kind: sum(item['argument_type'] == kind for item in records)
            for kind in sorted({item['argument_type'] for item in records})}


def install_guard(scratch, role, owned_pids, connection_audit):
    """Lifetime guard. SQLite native sidecar I/O is NOT a Python audit event."""
    blocked = []
    paths = {case/FILENAMES[target] for case in (
        [scratch] if role != 'parent' else
        [scratch/(mode.lower()+'-'+target) for mode in MODES for target in TARGETS])
        for target in TARGETS}

    def reject(reason):
        if len(blocked) < 32:
            blocked.append(reason)
        raise RuntimeError(reason)

    def resolve(value, dir_fd=None):
        if isinstance(value, int):
            raw = os.readlink('/proc/self/fd/'+str(value))
            if raw.startswith(('pipe:', 'socket:')):
                return None
            value = raw
        raw = os.fsdecode(value)
        if raw.startswith('file:'):
            parsed = urlsplit(raw)
            if parsed.netloc:
                reject('WAL_DIAGNOSTIC_NONLOCAL_URI')
            raw = unquote(parsed.path)
        target = Path(raw)
        if not target.is_absolute() and isinstance(dir_fd, int) and dir_fd >= 0:
            target = Path(os.readlink('/proc/self/fd/'+str(dir_fd)))/target
        return target.resolve()

    def writable(value, dir_fd=None):
        target = resolve(value, dir_fd)
        if target is None:
            return  # Existing subprocess pipes, not a filesystem write grant.
        if role == 'reader' or not (target == scratch or scratch in target.parents):
            reject('WAL_DIAGNOSTIC_FILE_WRITE_FORBIDDEN')

    def guard(event, args):
        if (event.startswith(('socket.', 'os.exec', 'os.spawn')) or
                event in ('os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.killpg')):
            reject('WAL_DIAGNOSTIC_NETWORK_OR_EXEC_FORBIDDEN')
        if event == 'os.kill' and (role != 'parent' or args[0] not in owned_pids or
                                  args[1] not in (signal.SIGTERM, signal.SIGKILL)):
            reject('WAL_DIAGNOSTIC_UNOWNED_PROCESS_SIGNAL')
        if event == 'subprocess.Popen':
            command = args[1]
            allowed = [[sys.executable, '-B', str(CHILD), str(scratch/(mode.lower()+'-'+target)),
                        child_role, target] for mode in MODES for target in TARGETS
                       for child_role in ('reader', 'writer')]
            if (role != 'parent' or args[0] != sys.executable or args[2] not in (None, str(ROOT), ROOT)
                    or args[3] is not None or not isinstance(command, (list, tuple)) or list(command) not in allowed):
                reject('WAL_DIAGNOSTIC_CHILD_NOT_EXACT_ARGV')
        if event == 'sqlite3.connect':
            if resolve(args[0]) not in paths:
                reject('WAL_DIAGNOSTIC_SQLITE_OUTSIDE_EXACT_PAIR')
            # CPython v3.10.22 audits the FSConverter bytes object; 3.12
            # exposes str. str(bytes) is a repr, not the supplied SQLite URI.
            # Source: python/cpython v3.10.22 Modules/_sqlite/connection.c.
            address = os.fsdecode(args[0])
            details = dict(argument_type=type(args[0]).__name__, database_uri=address,
                           query=parse_qs(urlsplit(address).query, keep_blank_values=True),
                           uri_flag='NOT_EXPOSED_BY_NATIVE_AUDIT_EVENT_SEE_WRAPPER_INTENT')
            if len(connection_audit) >= BOUND:
                reject('WAL_NATIVE_CONNECTION_AUDIT_BOUND')
            connection_audit.append(details)
            if role == 'reader' and (not address.startswith('file:') or details['query'] != {'mode': ['ro']}):
                reject('WAL_DIAGNOSTIC_READER_NOT_MODE_RO:'+canonical(details))
        if event == 'open':
            name, mode, flags = args
            if (isinstance(mode, str) and any(c in mode for c in 'wax+') or
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT |
                                                         os.O_TRUNC | os.O_APPEND)):
                writable(name)
        if event in ('os.chmod', 'os.chown', 'os.link', 'os.symlink', 'os.rename', 'os.replace'):
            reject('WAL_DIAGNOSTIC_PERMISSION_OR_LINK_MUTATION_FORBIDDEN')
        if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.utime', 'os.truncate'):
            position = {'os.mkdir': 2, 'os.remove': 1, 'os.rmdir': 1, 'os.utime': 3}.get(event)
            writable(args[0], args[position] if position is not None and len(args) > position else None)
    sys.addaudithook(guard)
    return blocked


def install_sql_audit(role, scratch):
    """Log SQL templates and hashes of bindings, never expanded payload/rows."""
    records, denied = [], []
    original = sqlite3.connect

    def record(template, parameters=()):
        if len(records) >= BOUND:
            raise AssertionError('WAL_SQL_AUDIT_BOUND')
        records.append(dict(template=template, bound_sha256=digest(parameters)))

    class Audited(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if role == 'reader':
                self.set_authorizer(self.authorize)

        def authorize(self, action, arg1, arg2, database, trigger):
            allowed = action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                                 sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION)
            if action == sqlite3.SQLITE_PRAGMA:
                allowed = arg2 is None and arg1.lower() in (
                    'busy_timeout', 'journal_mode', 'locking_mode', 'page_size', 'synchronous', 'wal_autocheckpoint')
            if not allowed:
                denied.append(dict(action=action, arg1=arg1, arg2=arg2))
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

        def execute(self, sql, parameters=()):
            record(sql, parameters)
            return super().execute(sql, parameters)

        def executemany(self, sql, parameters):
            parameters = list(parameters)
            record(sql, parameters)
            return super().executemany(sql, parameters)

        def executescript(self, sql):
            record(sql)
            return super().executescript(sql)

        def commit(self):
            record('COMMIT')
            return super().commit()

        def rollback(self):
            record('ROLLBACK')
            return super().rollback()

    def connect(*args, **kwargs):
        check('factory' not in kwargs and len(args) < 6, 'UNEXPECTED_SQLITE_FACTORY')
        address = os.fsdecode(args[0] if args else kwargs['database'])
        parsed = urlsplit(address)
        exact = (not parsed.netloc and Path(unquote(parsed.path)).resolve() in
                 {scratch/filename for filename in FILENAMES.values()})
        intent = dict(argument_type=type(args[0] if args else kwargs['database']).__name__,
                      database_uri=address if exact else 'NOT_EXACT_CHILD_PAIR',
                      query=parse_qs(parsed.query, keep_blank_values=True), uri=kwargs.get('uri', False))
        if role == 'reader':
            if not (exact and address.startswith('file:') and intent['query'] == {'mode': ['ro']}
                    and intent['uri'] is True):
                denied.append(dict(connect_intent=intent))
                raise RuntimeError('WAL_READER_CONNECT_INTENT_INVALID:'+canonical(intent))
            record('sqlite3.connect mode=ro', intent)
            records[-1]['connect_intent'] = intent
        return original(*args, factory=Audited, **kwargs)
    sqlite3.connect = connect
    return records, denied


def open_pair(scratch, readonly):
    from research.hunter_binance_health_store import Store
    store = Store.reader(scratch/FILENAMES['market'])
    control = store.enable_control(scratch/FILENAMES['control'], readonly=readonly)
    return store, control


def scope_state(control):
    # The real owner snapshot/fence implementation is neither patched nor bypassed.
    with control._db(readonly=True) as db:
        meta = dict(db.execute('SELECT key,value FROM meta'))
        counts = {table: db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                  for table in ('owners', 'requests', 'publications', 'quarantines')}
    names = ['btcusdt@kline_15m']
    fence = control.fence(names, epoch=meta['epoch'])
    status = control.status(timeout=.25)
    check(status['active_owners'] == 0 and status['halt'] is None, 'CONTROL_NOT_EMPTY_HEALTHY')
    return dict(epoch=meta['epoch'], phase=meta['phase'], counts=counts,
                fence_sha256=digest(fence), gate_sha256=hashlib.sha256(control.gate_path.read_bytes()).hexdigest())


def read_value(db):
    row = db.execute('SELECT value FROM meta WHERE key=?', (KEY,)).fetchone()
    check(row is not None, 'DIAGNOSTIC_KEY_MISSING')
    return value_summary(row[0])


def connection_state(db):
    return {key: db.execute('PRAGMA '+key).fetchone()[0] for key in
            ('journal_mode', 'locking_mode', 'busy_timeout', 'page_size', 'synchronous', 'wal_autocheckpoint')}


class Peer:
    def __init__(self, case, role, target, owned_pids):
        self.role, self.owned_pids = role, owned_pids
        self.stderr_path = case/(role+'.stderr.log')
        self.stderr = self.stderr_path.open('w', encoding='utf-8')
        self.process = subprocess.Popen([sys.executable, '-B', str(CHILD), str(case), role, target],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1)
        owned_pids.add(self.process.pid)

    def receive(self, expected):
        ready, _, _ = select.select([self.process.stdout], [], [], 30)
        if not ready:
            raise TimeoutError('WAL_CHILD_HANDSHAKE_TIMEOUT:'+self.role+':'+expected)
        line = self.process.stdout.readline(2_000_000)
        if not line or not line.endswith('\n'):
            tail = self.stderr_path.read_text(encoding='utf-8', errors='replace')[-16384:]
            raise AssertionError('WAL_CHILD_OUTPUT_MISSING_OR_OVERSIZE:'+self.role+':'+tail)
        value = json.loads(line)
        check(value['event'] == expected, 'WAL_CHILD_UNEXPECTED_EVENT:'+canonical(value))
        check(not value['blocked'] and not value['sql_denied'], 'CHILD_GUARD_VIOLATION')
        return value

    def command(self, command, expected, **parameters):
        self.process.stdin.write(canonical(dict(command=command, **parameters))+'\n')
        self.process.stdin.flush()
        return self.receive(expected)

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        finally:
            if self.process.poll() is not None:
                self.owned_pids.discard(self.process.pid)
            self.process.stdin.close()
            self.process.stdout.close()
            self.stderr.close()


def checkpoint(db, operation, path):
    check(operation in ('PASSIVE', 'TRUNCATE'), 'CHECKPOINT_OPERATION_INVALID')
    start = time.perf_counter_ns()
    result = tuple(db.execute('PRAGMA wal_checkpoint('+operation+')').fetchone())
    return dict(experiment_only=True, sql='PRAGMA wal_checkpoint('+operation+')',
                busy=result[0], log_frames=result[1], checkpointed_frames=result[2],
                wall_us=(time.perf_counter_ns()-start)//1000, files=file_state(path))


def locked_commit(value, target):
    error = value.get('error') or {}
    code = error.get('sqlite_errorcode')
    phase = error.get('market_phase') if target == 'market' else error.get('control_phase')
    context = error.get('sqlite_context') or {}
    return (not value.get('committed') and error.get('type') == 'OperationalError' and
            error.get('reason') == 'database is locked' and str(phase).lower() == 'commit' and
            context.get('database') == target and context.get('journal_mode') == 'delete' and
            context.get('busy_timeout_ms') == 250 and
            (code is None or (code & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)))


def run_case(root, mode, target, before, source_head, owned_pids, result):
    import fcntl
    from research.hunter_binance_health_store import Store
    from research.hunter_binance_control import Control
    case = root/(mode.lower()+'-'+target)
    case.mkdir()
    result.update(mode=mode, target=target, case_path=str(case), events=[], errors=[],
                  held_target_seconds=HELD_TARGET_SECONDS, injected_delay=True,
                  write_schedule_seconds=[.5, 1.0, 1.5, 2.0],
                  delay_scope='PARENT_COMMAND_SCHEDULE_WHILE_REAL_READ_TRANSACTION_REMAINS_OPEN')
    anchors, peers = {}, []
    path = case/FILENAMES[target]
    try:
        with (case/'collector.lock').open('x') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            store = Store(case/FILENAMES['market'])
            Control.provision(case/FILENAMES['control'], store.path)
            control = store.enable_control(case/FILENAMES['control'])
            store.set_control_source(source_head)
            store.begin_session(['btcusdt@kline_15m'], 'e'*64)
            # Offline scratch provision only. There are no child connections yet.
            for name, filename in FILENAMES.items():
                db = sqlite3.connect((case/filename).as_uri()+'?mode=rw', uri=True, timeout=.25)
                anchors[name] = db
                actual = db.execute('PRAGMA journal_mode='+mode).fetchone()[0]
                check(actual.upper() == mode, 'PROVISIONED_MODE_MISMATCH')
                db.execute('INSERT INTO meta(key,value) VALUES(?,?)', (KEY, payload(0)))
                db.commit()
            result['provisioned'] = {name: dict(connection=connection_state(db),
                files=file_state(case/FILENAMES[name])) for name, db in anchors.items()}
            for value in result['provisioned'].values():
                check(value['connection']['busy_timeout'] == 250, 'BUSY_TIMEOUT_CHANGED')
                if mode == 'WAL':
                    check(value['files']['wal']['exists'] and value['files']['shm']['exists'],
                          'PROVISIONED_WAL_SIDECARS_REQUIRED')
            result['control_before'] = scope_state(control)
            (case/'config.json').write_text(canonical(dict(source_head=source_head,
                source_hashes=before, mode=mode, target=target)), encoding='utf-8')
            reader = Peer(case, 'reader', target, owned_pids); peers.append(reader)
            writer = Peer(case, 'writer', target, owned_pids); peers.append(writer)
            result['reader_ready'] = reader.receive('READY')
            result['writer_ready'] = writer.receive('READY')
            parent = machine()
            for ready in (result['reader_ready'], result['writer_ready']):
                check(ready['source_hashes'] == before, 'CHILD_SOURCE_CHANGED')
                check(all(ready['machine'][key] == parent[key] for key in ('nodename', 'boot_id', 'uid', 'gid')),
                      'NOT_SAME_HOST_AND_IDENTITY')
                for name, info in result['provisioned'].items():
                    check(all(ready['files'][name]['database'][key] == info['files']['database'][key]
                              for key in ('dev', 'inode')), 'CHILD_DATABASE_IDENTITY_MISMATCH')
                    observed = ready['pair_connections'][name]
                    check(observed['journal_mode'].upper() == mode and observed['busy_timeout'] == 250,
                          'CHILD_PAIR_MODE_OR_BUSY_CHANGED')
                    check(observed['locking_mode'] == info['connection']['locking_mode'],
                          'CHILD_PAIR_LOCKING_MODE_CHANGED')
            check(len({reader.process.pid, writer.process.pid, os.getpid()}) == 3, 'INDEPENDENT_PIDS_REQUIRED')
            held = reader.command('HOLD', 'HELD')
            result['events'].append(held)
            check(held['value'] == value_summary(payload(0)), 'INITIAL_SNAPSHOT_VALUE')
            check(held['connection']['busy_timeout'] == 250 and held['connection']['journal_mode'].upper() == mode,
                  'READER_ACTUAL_MODE_OR_TIMEOUT_CHANGED')
            for index in range(1, 5):
                # Explicit two-second pin, identical in DELETE and WAL. This
                # changes only the synthetic metadata-write schedule; no
                # production reader cadence, busy timeout or queue is tuned.
                deadline = held['monotonic_ns']+index*WRITE_INTERVAL_NS
                remaining = (deadline-time.monotonic_ns())/1e9
                if remaining > 0:
                    time.sleep(remaining)
                command_start = time.monotonic_ns()
                check(command_start >= deadline, 'INJECTED_WRITE_STARTED_BEFORE_SCHEDULE')
                written = writer.command('WRITE', 'WRITTEN', index=index)
                observed = reader.command('READ_HELD', 'HELD_VALUE')
                sample = dict(index=index, write=written, held=observed,
                              scheduled_write_offset_ns=index*WRITE_INTERVAL_NS,
                              actual_command_start_offset_ns=command_start-held['monotonic_ns'],
                              checkpoint=checkpoint(anchors[target], 'PASSIVE', path))
                result['events'].append(sample)
                check(observed['value'] == held['value'], 'HELD_SNAPSHOT_CHANGED')
                check(written['connection']['busy_timeout'] == 250, 'WRITER_TIMEOUT_CHANGED')
                if mode == 'WAL':
                    check(written['committed'] and written.get('error') is None, 'WAL_WRITER_DID_NOT_COMMIT')
                    check(sample['checkpoint']['log_frames'] > sample['checkpoint']['checkpointed_frames'],
                          'HELD_READER_DID_NOT_LIMIT_PASSIVE_CHECKPOINT')
                else:
                    check(locked_commit(written, target), 'DELETE_EXPECTED_NATIVE_COMMIT_LOCK_REFUSAL')
            if mode == 'WAL':
                sizes = [event['checkpoint']['files']['wal']['bytes'] for event in result['events'] if 'checkpoint' in event]
                check(all(a < b for a, b in zip(sizes, sizes[1:])), 'WAL_DID_NOT_GROW_ACROSS_COMMITS')
            result['release'] = reader.command('RELEASE', 'RELEASED')
            result['held_read_wall_us'] = (result['release']['monotonic_ns']-held['monotonic_ns'])//1000
            result['held_read_wall_interval'] = 'HELD_EMIT_TO_RELEASED_EMIT_NOT_EXACT_SQLITE_LOCK_DURATION'
            check(result['release']['monotonic_ns']-held['monotonic_ns'] >= int(HELD_TARGET_SECONDS*1e9),
                  'INJECTED_HELD_WINDOW_SHORTER_THAN_TWO_SECONDS')
            result['visible_after_release'] = reader.command('REOPEN', 'REOPENED')
            check(result['visible_after_release'].get('error') is None, 'REOPEN_AFTER_RELEASE_FAILED')
            expected = value_summary(payload(4 if mode == 'WAL' else 0))
            check(result['visible_after_release'].get('value') == expected, 'COMMITTED_VISIBILITY_AFTER_RELEASE')
            result['post_release_write'] = writer.command('WRITE', 'WRITTEN', index=5)
            check(result['post_release_write']['committed'], 'WRITER_DID_NOT_PROGRESS_AFTER_RELEASE')
            result['visible_new_commit'] = reader.command('REOPEN', 'REOPENED')
            check(result['visible_new_commit'].get('error') is None, 'REOPEN_AFTER_NEW_COMMIT_FAILED')
            check(result['visible_new_commit'].get('value') == value_summary(payload(5)), 'NEW_COMMIT_NOT_VISIBLE')
            result['passive_after_release'] = checkpoint(anchors[target], 'PASSIVE', path)
            result['truncate_after_release'] = checkpoint(anchors[target], 'TRUNCATE', path)
            if mode == 'WAL':
                check(result['truncate_after_release']['busy'] == 0 and
                      result['truncate_after_release']['log_frames'] == 0, 'SCRATCH_TRUNCATE_INCOMPLETE')
            result['control_after'] = scope_state(control)
            check(result['control_before'] == result['control_after'], 'OWNER_OR_FENCE_STATE_CHANGED')
            # Child objects remain alive but all of their connections are closed.
            result['before_last_anchor_close'] = {name: file_state(case/filename) for name, filename in FILENAMES.items()}
            for db in anchors.values():
                db.close()
            anchors.clear()
            result['after_last_anchor_close'] = {name: file_state(case/filename) for name, filename in FILENAMES.items()}
            result['reader_reopen_after_last_close'] = reader.command('REOPEN', 'REOPENED')
            reopen = result['reader_reopen_after_last_close']
            result['after_readonly_reopen'] = {name: file_state(case/filename) for name, filename in FILENAMES.items()}
            # A failed mode=ro reopen is preserved as failed evidence, never
            # repaired via RW or accepted as an arbitrary OperationalError.
            check(reopen.get('error') is None, 'FINAL_MODE_RO_REOPEN_FAILED_SEE_NATIVE_EVIDENCE')
            check(reopen['value'] == value_summary(payload(5)), 'FINAL_REOPEN_VALUE_MISMATCH')
            check(reopen['connection']['busy_timeout'] == 250 and
                  reopen['connection']['journal_mode'].upper() == mode, 'FINAL_REOPEN_SETTINGS_CHANGED')
            for peer in peers:
                result[peer.role+'_final'] = peer.command('STOP', 'STOPPED')
                check(result[peer.role+'_final']['source_hashes'] == before, 'CHILD_SOURCE_CHANGED_AT_EXIT')
                check(result[peer.role+'_final']['source_audit']['before_after_equal'], 'CHILD_LOADED_SOURCE_CHANGED')
                check(peer.process.wait(timeout=5) == 0, 'CHILD_EXIT_FAILED')
            result['mechanism_assertions_passed'] = True
    except BaseException as error:
        result['errors'].append(error_detail(error))
        result['mechanism_assertions_passed'] = False
    finally:
        for peer in peers:
            try:
                peer.close()
            except BaseException as error:
                result['errors'].append(dict(cleanup='child', **error_detail(error)))
                result['mechanism_assertions_passed'] = False
        for db in anchors.values():
            try:
                db.close()
            except BaseException as error:
                result['errors'].append(dict(cleanup='anchor', **error_detail(error)))
                result['mechanism_assertions_passed'] = False
        result['final_files'] = {name: file_state(case/filename) for name, filename in FILENAMES.items()}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=ROOT, type=Path)
    parser.add_argument('--source-head', default=os.environ.get('CHECKOUT_SHA'))
    args = parser.parse_args()
    check(sys.platform == 'linux', 'LINUX_ONLY_NO_PORTABLE_RUNTIME')
    check(args.repo.resolve() == ROOT == Path.cwd().resolve(), 'CURRENT_CHECKOUT_REQUIRED')
    check(isinstance(args.source_head, str) and re.fullmatch('[0-9a-f]{40}', args.source_head) and
          args.source_head == os.environ.get('CHECKOUT_SHA'), 'CHECKOUT_SHA_BINDING_REQUIRED')
    # One exact local read before the lifetime guard. No git subprocess is
    # permitted after scratch initialization, in either parent or children.
    git_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    check(git_head == args.source_head, 'CHECKOUT_SHA_DOES_NOT_MATCH_ACTUAL_GIT_HEAD')
    sys.path.insert(0, str(ROOT))
    scratch = Path(tempfile.mkdtemp(prefix='binance-wal-lifecycle-')).resolve()
    owned = set()
    native_connections = []
    blocked = install_guard(scratch, 'parent', owned, native_connections)
    sql, denied = install_sql_audit('parent', scratch)
    before = source_hashes()
    result = dict(schema='binance_wal_lifecycle_diagnostic_v1', source_head=args.source_head, git_head=git_head,
        scratch=str(scratch), machine=machine(), diagnostic_cases=4, cases=[],
        mechanism_only=True, capacity_proof=False, production_mode_changed=False,
        deployment_blocker='WAL_RESET_FIX_NOT_VERIFIED',
        required_sqlite_fix='3.51.3+ or verified 3.44.6/3.50.7 backport; version alone is not vendor-patch evidence',
        limitations=[
            'Small scratch metadata transactions, not a collector/consumer throughput or strict acceptance gate.',
            'The reader pin includes an injected two-second schedule with four writes; not a long-duration or hour-load gate.',
            'No active HTTP owners; unchanged real control owner snapshot and scope fence are checked.',
            'mode=ro and SQL authorizer prohibit SQL writes, DDL and write PRAGMAs, not native WAL/SHM readmark I/O.',
            'Python audit hooks do not observe internal SQLite filesystem operations; file state is sampled explicitly.',
            'Same UID/GID with pre-existing permissions only; no permission enlargement or RW reopen fallback.',
            'PASSIVE busy=0 does not prove completion; compare log_frames and checkpointed_frames.',
            'DELETE lock errors are expected only in the precisely reported writer COMMIT phase.',
            'SQLite 3.45.1 mechanism results cannot verify the WAL-reset fix or authorize production WAL.'],
        source_before=before)
    print(canonical(dict(event='WAL_DIAGNOSTIC_START', source_head=args.source_head,
                         scratch=str(scratch), machine=result['machine'])), flush=True)
    for mode in MODES:
        for target in TARGETS:
            case = {}
            result['cases'].append(case)
            run_case(scratch, mode, target, before, args.source_head, owned, case)
            print(canonical(dict(event='WAL_CASE_COMPLETE', mode=mode, target=target,
                mechanism_assertions_passed=case['mechanism_assertions_passed'],
                errors=case['errors'])), flush=True)
    result.update(source_after=source_hashes(), source_audit=loaded_source_audit(before),
                  blocked_effects=blocked, sql_denied=denied,
                  parent_sql_audit=sql, native_connection_audit=native_connections,
                  native_connection_argument_types=native_argument_types(native_connections),
                  unreaped_pids=sorted(owned))
    result['diagnostic_completed'] = (all(case['mechanism_assertions_passed'] for case in result['cases'])
        and not blocked and not denied and not owned and before == result['source_after']
        and result['source_audit']['before_after_equal'])
    report = scratch/'wal-lifecycle-report.json'
    report.write_text(canonical(result), encoding='utf-8')
    print(canonical(dict(event='WAL_DIAGNOSTIC_FINISHED', report=str(report),
                         diagnostic_completed=result['diagnostic_completed'],
                         deployment_blocker=result['deployment_blocker'])), flush=True)
    return 0 if result['diagnostic_completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
