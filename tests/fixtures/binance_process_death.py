"""Linux scratch-only SIGKILL boundary; no sockets, HTTP or nested children.

The parent kills this process after STAGED. The memory-only overflow branch is
an explicitly unsafe counterfactual, never an alternative collector path.
"""
import json
import os
from pathlib import Path
import queue
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if sys.platform != 'linux':
    raise SystemExit('LINUX_REQUIRED')
scratch = Path(sys.argv[1]).resolve()
scenario = sys.argv[2]
if scenario not in ('queued', 'durable-overflow', 'memory-only-overflow', 'active-owner', 'inflight-transaction'):
    raise ValueError('PROCESS_DEATH_SCENARIO_INVALID')
if not scratch.is_dir():
    raise ValueError('EXISTING_SCRATCH_REQUIRED')
blocked = []


def reject(reason):
    blocked.append(reason)
    raise RuntimeError(reason)


def inside(path, dir_fd=None):
    if isinstance(path, int):
        return
    target = Path(os.fsdecode(path).removeprefix('file:').split('?')[0])
    if not target.is_absolute() and isinstance(dir_fd, int) and dir_fd >= 0:
        target = Path(os.readlink('/proc/self/fd/'+str(dir_fd)))/target
    target = target.resolve()
    if target != scratch and scratch not in target.parents:
        reject('PROCESS_DEATH_WRITE_OUTSIDE_SCRATCH')


def guard(event, args):
    if (event.startswith('socket.') or event in ('subprocess.Popen', 'os.system',
            'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.kill', 'os.killpg')
            or event.startswith(('os.exec', 'os.spawn'))):
        reject('PROCESS_DEATH_NETWORK_OR_PROCESS_FORBIDDEN')
    if event == 'sqlite3.connect':
        inside(args[0])
    if event == 'open':
        path, mode, flags = args
        if ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR |
                    os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            inside(path)
    if event in ('os.remove', 'os.mkdir', 'os.rmdir', 'os.chmod', 'os.utime'):
        index = {'os.mkdir': 2, 'os.remove': 1, 'os.rmdir': 1, 'os.chmod': 2, 'os.utime': 3}[event]
        inside(args[0], args[index] if len(args) > index else None)
    if event in ('os.rename', 'os.replace', 'os.link', 'os.symlink'):
        reject('PROCESS_DEATH_RENAME_OR_LINK_FORBIDDEN')


sys.addaudithook(guard)
import fcntl
from research.hunter_binance_stream import ObservedQueue
from research.hunter_binance_stream_store import split_topic
from tests.test_hunter_binance_cache import message
from tests.test_hunter_binance_coordinated_runtime import Market, MIDNIGHT, HEAD, MANIFEST


def emit(event, **value):
    # Imported code must not turn a swallowed audit rejection into a valid
    # handshake. No prohibited attempt is permitted, even if it had no effect.
    if blocked:
        raise AssertionError('PROCESS_DEATH_BLOCKED_AUDIT_ATTEMPTS:'+repr(blocked))
    print(json.dumps(dict(event=event, blocked_effects=len(blocked), **value), sort_keys=True), flush=True)


# The same flock primitive/path basename as stream.main, without invoking main,
# its real paths, selector, services or exchange transports.
with (scratch/'collector.lock').open('a') as collector_lock:
    fcntl.flock(collector_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Keep every native candle boundary outside the 90s injected-clock window.
    market = Market(scratch/'state.sqlite3', stamp=MIDNIGHT+60000)
    stamp = market.seed()
    emit('SEEDED', pid=os.getpid(), scenario=scenario, stamp=stamp,
         names=market.names, epoch=market.epoch,
         fixture_source_head=HEAD, fixture_manifest_sha256=MANIFEST)
    if sys.stdin.readline().strip() != 'STAGE':
        raise ValueError('PROCESS_DEATH_STAGE_HANDSHAKE_REQUIRED')

    name = market.names[0]
    symbol, interval = split_topic(name)
    shard = market.protocol.topic_shards[name]
    received = stamp+1
    raw = json.dumps(message(symbol, interval, received))
    messages = ObservedQueue(4096)
    before = {n: row['received'] for n, row in market.protocol.evidence.topics.items()}
    accepted = 0
    full = False
    memory_only_revoked = False
    total = 4097 if scenario.endswith('overflow') else 1
    for _ in range(total):
        value = market.protocol.arrived(shard, raw, received)
        try:
            messages.put_nowait((shard, value, received))
            accepted += 1
        except queue.Full:
            full = True
            if scenario == 'durable-overflow':
                # Exact production queue-full receipt/revocation operation.
                market.store.unprocessed(market.epoch, [(value, received)], 'RECEIVE_QUEUE_FULL')
            elif scenario == 'memory-only-overflow':
                # UNSAFE HYPOTHESIS ONLY: this flag dies with the child. It is
                # intentionally not written to production state or consumed.
                memory_only_revoked = True
            else:
                raise
    owner = None
    if scenario == 'active-owner':
        control = market.store.control
        context = dict(epoch=market.epoch, job_key=('death-probe', name), scope=[name], kind='repair')
        call = control.begin_call(context)
        owner = control.admit(context, call, 1, '/api/v3/klines', 2, time.monotonic()+30)
        if owner is None:
            raise AssertionError('PROCESS_DEATH_OWNER_NOT_ADMITTED')
        # No GO, socket or HTTP child. Real durable owner and NativeLock remain
        # active until SIGKILL; no graceful retirement is performed.
    def staged(**transaction):
        received_delta = {n: row['received']-before[n]
                          for n, row in market.protocol.evidence.topics.items()}
        emit('STAGED', pid=os.getpid(), received_at=received, target_topic=name,
             received_delta=received_delta, accepted=accepted, queued=messages.qsize(),
             capacity=messages.maxsize, full=full, memory_only_revoked=memory_only_revoked,
             owner_token=owner.token if owner else None,
             owner_identity=list(owner.lock.identity) if owner else None,
             dangerous_counterfactual=scenario == 'memory-only-overflow', **transaction)

    if scenario == 'inflight-transaction':
        original_put = market.store._put

        def pause_after_real_put(db, *args, **kwargs):
            changes = db.total_changes
            changed = original_put(db, *args, **kwargs)
            if not changed or not db.in_transaction or db.total_changes <= changes:
                raise AssertionError('REAL_UNCOMMITTED_REVISION_WRITE_REQUIRED')
            staged(transaction_in_progress=True, transaction_phase='AFTER_REAL_PUT_BEFORE_COMMIT',
                   transaction_changes=db.total_changes-changes,
                   journal_mode=db.execute('PRAGMA journal_mode').fetchone()[0],
                   inflight=len(market.protocol.inflight))
            # SIGKILL bypasses the real ingest transaction's commit/rollback
            # and savepoint cleanup. No forced spill, journal rewrite or fake
            # successful commit is introduced by this diagnostic.
            sys.stdin.readline()
            raise AssertionError('SIGKILL_REQUIRED_INSIDE_TRANSACTION')

        market.store._put = pause_after_real_put
        market.protocol.receive_batch([messages.get_nowait()])
        raise AssertionError('TRANSACTION_PAUSE_NOT_REACHED')
    staged(transaction_in_progress=False, inflight=0)
    # A live pipe keeps all queue/owner objects resident. Parent must SIGKILL;
    # EOF or a second command is a harness error, not a successful crash.
    sys.stdin.readline()
    raise AssertionError('SIGKILL_REQUIRED_NO_GRACEFUL_EXIT')
