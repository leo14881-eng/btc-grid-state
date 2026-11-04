"""Scratch-only crash boundary for real intent/market transactions; no network."""
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
scratch = Path(sys.argv[1]).resolve()
phase = sys.argv[2]
if phase not in ('before-market', 'after-market'):
    raise ValueError('FIXTURE_PHASE_INVALID')


def inside(path):
    if isinstance(path, int):
        return
    target = Path(os.fsdecode(path).removeprefix('file:').split('?')[0]).resolve()
    if target != scratch and scratch not in target.parents:
        raise RuntimeError('FIXTURE_WRITE_OUTSIDE_SCRATCH')


def guard(event, args):
    if (event.startswith('socket.') or event in ('subprocess.Popen', 'os.system',
            'os.posix_spawn', 'os.fork', 'os.forkpty') or event.startswith(('os.exec', 'os.spawn'))):
        raise RuntimeError('FIXTURE_NETWORK_OR_PROCESS_FORBIDDEN')
    if event == 'sqlite3.connect':
        inside(args[0])
    if event == 'open':
        path, mode, flags = args
        if ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            inside(path)
    if event in ('os.remove', 'os.mkdir', 'os.rmdir', 'os.chmod', 'os.utime'):
        inside(args[0])
    if event in ('os.rename', 'os.replace', 'os.link', 'os.symlink'):
        raise RuntimeError('FIXTURE_RENAME_OR_LINK_FORBIDDEN')


sys.addaudithook(guard)
from research.hunter_binance_health_store import Store

work = json.loads((scratch/'crash-work.json').read_text(encoding='utf-8'))
store = Store.reader(scratch/'state.sqlite3')
store.enable_control()
store.set_control_source(work.pop('source_head'))
original = store._apply_intent_market


def crash_boundary(intent):
    if phase == 'before-market':
        os._exit(81)
    original(intent)  # Real SQLite transaction commits before the process dies.
    os._exit(82)


store._apply_intent_market = crash_boundary
store.apply_work(**work)
raise AssertionError('CRASH_BOUNDARY_NOT_REACHED')
