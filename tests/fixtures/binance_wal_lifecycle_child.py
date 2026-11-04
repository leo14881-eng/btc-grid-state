"""Exact-argv Linux scratch WAL reader/writer; no network or nested children."""
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    from scripts.profile_binance_wal_lifecycle import (FILENAMES, KEY, TARGETS, canonical,
        check, connection_state, error_detail, file_state, install_guard, install_sql_audit,
        loaded_source_audit, machine, native_argument_types, open_pair, payload, read_value,
        source_hashes, value_summary)
    check(sys.platform == 'linux' and len(sys.argv) == 4, 'WAL_CHILD_EXACT_LINUX_ARGUMENTS')
    scratch = Path(sys.argv[1])
    role, target = sys.argv[2:]
    check(scratch.is_absolute() and scratch.resolve() == scratch and scratch.is_dir(), 'WAL_CHILD_SCRATCH')
    check(role in ('reader', 'writer') and target in TARGETS, 'WAL_CHILD_ROLE_OR_TARGET')
    native_connections = []
    blocked = install_guard(scratch, role, set(), native_connections)
    sql, denied = install_sql_audit(role, scratch)
    config = json.loads((scratch/'config.json').read_text(encoding='utf-8'))
    check(config['target'] == target and config['source_head'] == os.environ.get('CHECKOUT_SHA'), 'WAL_CHILD_BINDING')
    check(config['source_hashes'] == source_hashes(), 'WAL_CHILD_SOURCE_MISMATCH')
    store, control = open_pair(scratch, readonly=role == 'reader')
    path = scratch/FILENAMES[target]
    context = store.db if target == 'market' else control._db
    held = None
    connection = None

    def emit(event, **values):
        check(not blocked and not denied, 'WAL_CHILD_SWALLOWED_GUARD_REJECTION')
        print(canonical(dict(event=event, monotonic_ns=time.monotonic_ns(), blocked=blocked,
                             sql_denied=denied, **values)), flush=True)

    pair_connections = {}
    for name, connect in (('market', store.db), ('control', control._db)):
        with connect(readonly=True, timeout=.25) as db:
            pair_connections[name] = connection_state(db)
    emit('READY', machine=machine(), source_hashes=source_hashes(), pair_connections=pair_connections,
         native_connection_audit=native_connections,
         native_connection_argument_types=native_argument_types(native_connections),
         files={name: file_state(scratch/filename) for name, filename in FILENAMES.items()})
    try:
        for raw in sys.stdin:
            command = json.loads(raw)
            action = command['command']
            if action == 'STOP':
                check(connection is None, 'STOP_WITH_LIVE_READ_TRANSACTION')
                emit('STOPPED', source_hashes=source_hashes(), sql_audit=sql,
                     native_connection_audit=native_connections,
                     native_connection_argument_types=native_argument_types(native_connections),
                     source_audit=loaded_source_audit(config['source_hashes']),
                     market_transactions=store.transaction_diagnostics(),
                     control_transactions=control.transaction_diagnostics())
                return 0
            if role == 'reader':
                if action == 'HOLD':
                    check(held is None, 'READER_ALREADY_HELD')
                    held = context(readonly=True, timeout=.25)
                    connection = held.__enter__()
                    value = read_value(connection)  # Establishes the real read snapshot.
                    emit('HELD', value=value, connection=connection_state(connection), files=file_state(path))
                elif action == 'READ_HELD':
                    check(connection is not None, 'READER_NOT_HELD')
                    emit('HELD_VALUE', value=read_value(connection), files=file_state(path))
                elif action == 'RELEASE':
                    check(connection is not None, 'READER_NOT_HELD')
                    held.__exit__(None, None, None)
                    held = connection = None
                    emit('RELEASED', files=file_state(path))
                elif action == 'REOPEN':
                    check(connection is None, 'REOPEN_REQUIRES_CLOSED_READ')
                    result = dict(before_files=file_state(path))
                    try:
                        with context(readonly=True, timeout=.25) as db:
                            result.update(value=read_value(db), connection=connection_state(db))
                    except Exception as error:
                        result['error'] = error_detail(error)
                    result['after_files'] = file_state(path)
                    emit('REOPENED', **result)
                else:
                    raise ValueError('WAL_READER_COMMAND_INVALID')
            elif action == 'WRITE':
                index = command['index']
                check(type(index) is int and 1 <= index <= 5, 'WAL_WRITE_INDEX_INVALID')
                value = payload(index)
                result = dict(index=index, committed=False, payload=value_summary(value))
                start = time.perf_counter_ns()
                try:
                    with context(timeout=.25) as db:
                        result['connection'] = connection_state(db)
                        db.execute('UPDATE meta SET value=? WHERE key=?', (value, KEY))
                    result['committed'] = True
                except Exception as error:
                    result['error'] = error_detail(error)
                result.update(wall_us=(time.perf_counter_ns()-start)//1000, files=file_state(path))
                emit('WRITTEN', **result)
            else:
                raise ValueError('WAL_WRITER_COMMAND_INVALID')
    finally:
        if held is not None:
            held.__exit__(None, None, None)
    raise ValueError('WAL_CHILD_PARENT_EOF_WITHOUT_STOP')


if __name__ == '__main__':
    raise SystemExit(main())
