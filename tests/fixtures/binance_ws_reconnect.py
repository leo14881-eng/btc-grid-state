"""Linux scratch/loopback collector fixture and its fixed HTTP child adapter.

Both exec modes install their own guard. This is never a production entrypoint.
The HTTP mode retains production URL validation, GO, parent-death and IPC rules.
"""
import os
from pathlib import Path
import stat
import sys
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(__file__).resolve()
sys.path.insert(0, str(ROOT))
if sys.platform != 'linux':
    raise RuntimeError('LINUX_PROCESS_ACCEPTANCE_REQUIRED')
mode = sys.argv[1] if len(sys.argv) > 1 else ''
if mode == '--collector' and len(sys.argv) == 5:
    scratch = Path(sys.argv[2])
    ports = {int(sys.argv[3]), int(sys.argv[4])}
    ws_port, http_port = int(sys.argv[3]), int(sys.argv[4])
    if ws_port == http_port:
        raise ValueError('FIXTURE_PORTS_MUST_DIFFER')
elif mode == '--http-child' and len(sys.argv) == 6:
    url, parent, http_port = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    scratch = Path(sys.argv[5])
    ports = {http_port}
    if parent <= 0:
        raise ValueError('FIXTURE_PARENT_INVALID')
else:
    raise ValueError('FIXTURE_EXACT_MODE_AND_ARGUMENTS_REQUIRED')
if (not scratch.is_absolute() or scratch.resolve() != scratch or not scratch.is_dir()
        or any(not 1 <= port <= 65535 for port in ports)):
    raise ValueError('FIXTURE_SCRATCH_OR_PORT_INVALID')

blocked = []


def reject(reason):
    blocked.append(reason)
    raise RuntimeError(reason)


def inside(path, dir_fd=None):
    if isinstance(path, int):
        return
    raw = os.fsdecode(path)
    if raw.startswith('file:'):
        uri = urlsplit(raw)
        if uri.netloc:
            reject('FIXTURE_NONLOCAL_FILE_URI')
        raw = unquote(uri.path)
    target = Path(raw)
    if not target.is_absolute() and isinstance(dir_fd, int) and dir_fd >= 0:
        target = Path(os.readlink('/proc/self/fd/'+str(dir_fd)))/target
    target = target.resolve()
    if target == Path(os.devnull) and stat.S_ISCHR(target.stat().st_mode):
        return
    if target != scratch and scratch not in target.parents:
        reject('FIXTURE_WRITE_OUTSIDE_SCRATCH')


def guard(event, args):
    if event in ('socket.connect', 'socket.connect_ex'):
        address = args[1]
        if not isinstance(address, tuple) or address[0] != '127.0.0.1' or address[1] not in ports:
            reject('FIXTURE_NON_LOOPBACK_OR_WRONG_PORT')
    if event == 'socket.getaddrinfo' and (args[0] != '127.0.0.1' or args[1] not in ports):
        reject('FIXTURE_DNS_FORBIDDEN')
    if event in ('socket.bind', 'socket.gethostbyname', 'socket.gethostbyaddr',
                 'socket.sendto', 'socket.sendmsg', 'os.system', 'os.posix_spawn',
                 'os.fork', 'os.forkpty') or event.startswith(('os.exec', 'os.spawn')):
        reject('FIXTURE_NETWORK_OR_EXEC_FORBIDDEN')
    if event == 'subprocess.Popen':
        command = args[1]
        if (mode != '--collector' or len(command) != 8 or command[:3] != [sys.executable, '-B', str(HELPER)]
                or command[3] != '--http-child' or command[5:] != [str(os.getpid()), str(http_port), str(scratch)]):
            reject('FIXTURE_UNAPPROVED_CHILD')
        from research.hunter_binance_http_child import allowed
        allowed(command[4])
    if event == 'sqlite3.connect':
        inside(args[0])
    if event == 'open':
        path, file_mode, flags = args
        if ((isinstance(file_mode, str) and any(c in file_mode for c in 'wax+')) or
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            inside(path)
    if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'):
        index = {'os.mkdir': 2, 'os.remove': 1, 'os.rmdir': 1, 'os.chmod': 2, 'os.utime': 3}[event]
        inside(args[0], args[index] if len(args) > index else None)
    if event in ('os.rename', 'os.replace', 'os.link', 'os.symlink'):
        reject('FIXTURE_RENAME_OR_LINK_FORBIDDEN')


sys.addaudithook(guard)

if mode == '--http-child':
    import http.client
    from research import hunter_binance_http_child as child
    child.allowed(url)

    def local_open(opener, request, timeout):
        # Preserve the already-validated public endpoint/query for fixture
        # responses. The only actual socket has this literal loopback address.
        child.allowed(request.full_url)
        parsed = urlsplit(request.full_url)
        connection = http.client.HTTPConnection('127.0.0.1', http_port, timeout=timeout)
        connection.request('GET', parsed.path+'?'+parsed.query)
        response = connection.getresponse()
        response.code = response.status
        return response

    child.urllib.request.OpenerDirector.open = local_open
    sys.argv = [str(HELPER), url, str(parent)]
    result = child.main()
    raise SystemExit(1 if blocked else result)

import datetime as dt
import json
import sqlite3
from threading import Event, Thread
import time
import traceback
from websockets.sync.client import connect
from research import hunter_binance_health as health
from research import hunter_binance_public as public
from research import hunter_binance_stream as stream
from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_control import Control, ControlError
from research.hunter_binance_health_store import Store
from research.hunter_binance_stream_store import topic

config = json.loads((scratch/'fixture.json').read_text(encoding='utf-8'))
names = sorted([topic('BTCUSDT', '5m'), topic('BTCUSDT', '1h'), topic('ETHUSDT', '15m')])
if set(config) != {'base_ms', 'anchor_mono', 'source_head', 'manifest'}:
    raise ValueError('FIXTURE_CONFIG_KEYS')
if (type(config['base_ms']) is not int or not isinstance(config['anchor_mono'], (int, float))
        or not 0 <= time.monotonic()-config['anchor_mono'] <= 20
        or config['source_head'] != 'd'*40 or config['manifest'] != 'e'*64):
    raise ValueError('FIXTURE_CONFIG_BINDING')
clock = lambda: config['base_ms']+int((time.monotonic()-config['anchor_mono'])*1000)
store = Store(scratch/'state.sqlite3')
Control.provision(scratch/'control.sqlite3', store.path)
store.enable_control()
stop = Event()
ready_epochs, watch_errors, rejected_old, sqlite_errors = [], [], [], []
started = time.monotonic()
public.worker_command = lambda value: [sys.executable, '-B', str(HELPER), '--http-child',
    value, str(os.getpid()), str(http_port), str(scratch)]


def sqlite_diagnostic(operation, error):
    # Failure-only observations: retain the original error even if a phase
    # read also fails. These later reads aren't a snapshot of the failure.
    detail = dict(operation=operation, type=type(error).__name__, message=str(error),
        traceback=''.join(traceback.format_exception(type(error), error, error.__traceback__)),
        error_market_phase=getattr(error, 'market_phase', None),
        error_control_phase=getattr(error, 'control_phase', None),
        sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
        sqlite_errorname=getattr(error, 'sqlite_errorname', None),
        elapsed_wall_seconds=time.monotonic()-started,
        phase_observation='BEST_EFFORT_AFTER_ERROR', market_phase=None, control_phase=None,
        market_epoch=None, control_epoch=None, diagnostic_errors=[])
    try:
        meta = store.metadata()
        detail.update(market_phase=meta.get('health_phase'), market_epoch=meta.get('epoch'))
    except BaseException as diagnostic_error:
        detail['diagnostic_errors'].append(dict(operation='market.metadata',
            type=type(diagnostic_error).__name__, message=str(diagnostic_error)))
    try:
        with store.control._db(readonly=True, timeout=.05) as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
        detail.update(control_phase=meta.get('phase'), control_epoch=meta.get('epoch'))
    except BaseException as diagnostic_error:
        detail['diagnostic_errors'].append(dict(operation='control.meta',
            type=type(diagnostic_error).__name__, message=str(diagnostic_error)))
    return detail


def watch():
    first = None
    operation = 'watch.start'
    try:
        while not stop.wait(.05):
            if (scratch/'stop').exists():
                stop.set()
                break
            if time.monotonic()-started > 65:
                raise AssertionError('FIXTURE_COLLECTOR_WALL_TIMEOUT')
            if first is not None and not rejected_old:
                operation = 'market.metadata.before_old_frozen'
                meta = store.metadata()
                if meta.get('epoch') != first.generation['epoch'] or meta.get('health_phase') != 'ACTIVE':
                    try:
                        operation = 'old_frozen.check_delivery.after_epoch_change'
                        first.check_delivery()
                    except (ValueError, ControlError):
                        rejected_old.append(True)
                        stream.emit(dict(event='FIXTURE_OLD_FROZEN_REJECTED', epoch=first.generation['epoch'],
                            current_epoch=meta.get('epoch'), health_phase=meta.get('health_phase')))
            try:
                now = clock()
                operation = 'consumer.read.full_scope'
                frozen = consumer.read(names, dt.datetime.fromtimestamp(now/1000, dt.timezone.utc),
                                       reader=store, clock=clock)
                epoch = frozen.generation['epoch']
                operation = 'control.publications.read_applied'
                with store.control._db(readonly=True) as db:
                    publications = [json.loads(row[0]) for row in db.execute(
                        "SELECT payload FROM publications WHERE epoch=? AND state='APPLIED'", (epoch,))]
                seeded = {n for p in publications if p['kind'] == 'seed' for n in p['names']}
                if seeded != set(names) or epoch in ready_epochs:
                    continue
                operation = 'market.states.check_ack'
                states = store.states()
                if not all(s['epoch'] == epoch and s['ack'] for s in states):
                    continue
                operation = 'frozen.check_delivery.before_ready'
                frozen.check_delivery()
                if first is None:
                    first = frozen
                else:
                    try:
                        operation = 'old_frozen.check_delivery.before_second_ready'
                        first.check_delivery()
                    except (ValueError, ControlError):
                        if not rejected_old:
                            rejected_old.append(True)
                    else:
                        raise AssertionError('FIXTURE_PREVIOUS_EPOCH_FROZEN_STILL_DELIVERABLE')
                ready_epochs.append(epoch)
                stream.emit(dict(event='FIXTURE_EPOCH_READY', epoch=epoch, topics=len(names),
                    seeded=len(seeded), acked=len(states), windows=len(frozen.generation['windows']),
                    old_frozen_rejected=bool(rejected_old), source_head=config['source_head']))
            except sqlite3.Error as error:
                sqlite_errors.append(sqlite_diagnostic(operation, error))
                continue
            except (ValueError, ControlError):
                # Startup, a revoked epoch, or a publication in progress is
                # expected UNKNOWN. It never produces a fixture READY receipt.
                continue
    except BaseException as error:
        if isinstance(error, sqlite3.Error):
            sqlite_errors.append(sqlite_diagnostic(operation, error))
        watch_errors.append(str(error))
        stop.set()


watcher = Thread(target=watch)
watcher.start()
problem = None
try:
    health.collect(store, names, config['manifest'], config['source_head'], stop=stop.is_set,
        connect=lambda: connect(f'ws://127.0.0.1:{ws_port}', proxy=None, ping_interval=None,
            open_timeout=3, close_timeout=1, max_queue=1024), clock=clock, coordinated=True)
except BaseException as error:
    problem = dict(type=type(error).__name__, message=str(error))
finally:
    stop.set()
    watcher.join(3)
    with store.control._db(readonly=True) as db:
        owner_count = db.execute('SELECT COUNT(*) FROM owners').fetchone()[0]
        request_count = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
        call_count, terminal_count = db.execute('SELECT COUNT(*),COUNT(terminal) FROM calls').fetchone()
        attempt_count, retired_count = db.execute(
            "SELECT COUNT(*),SUM(CASE WHEN terminal='RETIRED' THEN 1 ELSE 0 END) FROM attempts").fetchone()
    stream.emit(dict(event='FIXTURE_PROCESS_FINAL', ready_epochs=ready_epochs,
        old_frozen_rejected=bool(rejected_old), watcher_alive=watcher.is_alive(),
        active_owners=owner_count, requests=request_count, calls=call_count, terminals=terminal_count,
        attempts=attempt_count, retired=retired_count, orphan_files=len(list(scratch.glob('*.owner-*.lock'))),
        watch_errors=watch_errors, sqlite_errors=sqlite_errors, blocked_effects=blocked,
        problem=problem, elapsed_wall_seconds=time.monotonic()-started,
        connected=store.metadata().get('connected'), market_clock='FIXED_UTC_PLUS_REAL_ELAPSED',
        natural_utc_rollover_verified=False))
if (problem or watch_errors or sqlite_errors or blocked or watcher.is_alive() or len(ready_epochs) != 2
        or not rejected_old or owner_count or request_count or call_count != terminal_count):
    raise SystemExit(1)
