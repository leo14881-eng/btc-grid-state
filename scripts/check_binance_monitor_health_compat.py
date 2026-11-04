"""Offline PR128 Monitor + exact PR126 health-reader composition; no activation.

Only the candidate's management_inputs function is added to the pinned consumer.
The consumer, Store and health Store all load from the immutable PR126 Git object,
in memory, without checkout or import of main's older reader. Other Monitor code
and the existing eight regression cases come from this candidate checkout.
Three additional cases exercise the real health-contract gates in scratch SQLite.
This compatibility test does not authorize a selector change or deployment.
"""
import ast
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
from urllib.request import url2pathname

ROOT = Path(__file__).resolve().parents[1]
PINNED_HEAD = '33abf19c7a91855ba2efc2e65e1d383055494d75'
PINNED_MODULES = ('research.hunter_binance_stream_consumer',
                  'research.hunter_binance_stream_store',
                  'research.hunter_binance_health_store')
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def git_read(*args):
    # The caller/CI must already have this object. Never lazily fetch a missing
    # promisor object, refresh an index or execute a deployment helper here.
    result = subprocess.run(['git', '--no-optional-locks', '-C', str(ROOT), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, env=dict(os.environ, GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0'))
    if result.returncode:
        raise RuntimeError('PINNED_GIT_OBJECT_REQUIRED_NO_AUTOMATIC_FETCH')
    return result.stdout


class PinnedReader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources):
        self.sources = sources

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.sources:
            return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        filename = '<PR126-' + PINNED_HEAD + ':' + module.__name__ + '>'
        exec(compile(self.sources[module.__name__], filename, 'exec'), module.__dict__)
        module.__compat_source_head__ = PINNED_HEAD


def composition():
    if git_read('rev-parse', PINNED_HEAD + '^{commit}').decode().strip() != PINNED_HEAD:
        raise RuntimeError('PINNED_COMMIT_MISMATCH')
    sources = {name: git_read('show', PINNED_HEAD + ':' + name.replace('.', '/') + '.py').decode()
               for name in PINNED_MODULES}
    hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()}
    candidate = (ROOT/'research/hunter_binance_stream_consumer.py').read_text(encoding='utf-8')
    functions = [node for node in ast.parse(candidate).body
                 if isinstance(node, ast.FunctionDef) and node.name == 'management_inputs']
    if len(functions) != 1 or any(node.name == 'management_inputs' for node in
            ast.parse(sources[PINNED_MODULES[0]]).body if isinstance(node, ast.FunctionDef)):
        raise RuntimeError('EXACT_SINGLE_MANAGEMENT_ADDITION_REQUIRED')
    addition = ast.unparse(functions[0])
    sources[PINNED_MODULES[0]] += '\n' + addition + '\n'
    if any(name in sys.modules for name in PINNED_MODULES):
        raise RuntimeError('UNPINNED_READER_ALREADY_IMPORTED')
    return PinnedReader(sources), dict(runtime_head=PINNED_HEAD, runtime_source_sha256=hashes,
        candidate_head=git_read('rev-parse', 'HEAD').decode().strip(),
        management_function_sha256=hashlib.sha256(addition.encode()).hexdigest(),
        monitor_source_sha256=hashlib.sha256((ROOT/'research/hunter_position_monitor.py').read_bytes()).hexdigest())


def protect_scratch(scratch, blocked):
    def reject(reason):
        blocked.append(reason)
        raise RuntimeError(reason)

    def inside(path, dir_fd=None):
        if isinstance(path, int):
            return
        text = os.fsdecode(path)
        if text.startswith('file:'):
            uri = urlsplit(text)
            if uri.netloc:
                reject('COMPAT_NONLOCAL_SQLITE_URI')
            text = url2pathname(uri.path)
        target = Path(text)
        if not target.is_absolute() and isinstance(dir_fd, int) and dir_fd >= 0:
            target = Path(os.readlink('/proc/self/fd/' + str(dir_fd))) / target
        target = target.resolve()
        if target != scratch and scratch not in target.parents:
            reject('COMPAT_WRITE_OR_SQLITE_OUTSIDE_SCRATCH')

    def guard(event, args):
        if (event.startswith('socket.') or event in ('subprocess.Popen', 'os.system',
                'os.posix_spawn', 'os.fork', 'os.forkpty') or event.startswith(('os.exec', 'os.spawn'))):
            reject('COMPAT_NETWORK_OR_EXEC_BLOCKED')
        if event == 'sqlite3.connect':
            inside(args[0])
        if event == 'open':
            path, mode, flags = args
            if ((isinstance(mode, str) and any(x in mode for x in 'wax+')) or
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                inside(path)
        if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'):
            index = {'os.mkdir': 2, 'os.remove': 1, 'os.rmdir': 1, 'os.chmod': 2, 'os.utime': 3}[event]
            inside(args[0], args[index] if len(args) > index else None)
        if event == 'os.rename':
            inside(args[0], args[2] if len(args) > 2 else None)
            inside(args[1], args[3] if len(args) > 3 else None)
        if event in ('os.symlink', 'os.link'):
            reject('COMPAT_LINK_BLOCKED')
    sys.addaudithook(guard)


def main():
    loader, receipt = composition()  # Only immutable local Git reads before test guard.
    blocked = []
    with tempfile.TemporaryDirectory(prefix='binance-monitor-health-compat-') as directory:
        scratch = Path(directory).resolve()
        previous, previous_temp = Path.cwd(), tempfile.tempdir
        tempfile.tempdir = str(scratch)
        protect_scratch(scratch, blocked)
        sys.meta_path.insert(0, loader)
        os.chdir(scratch)
        try:
            from tests.test_hunter_binance_monitor_cache import MonitorCacheTests, NOW
            from research.hunter_binance_health_store import Store as HealthStore
            from research import hunter_binance_stream_consumer as consumer
            from research import hunter_binance_stream_store as stream_store
            if (consumer.Store is not stream_store.Store or HealthStore.__bases__ != (stream_store.Store,) or
                    any(getattr(sys.modules[name], '__compat_source_head__', None) != PINNED_HEAD for name in PINNED_MODULES)):
                raise RuntimeError('COMPOSED_READER_IDENTITY_MISMATCH')

            class ComposedHealth(MonitorCacheTests):
                def setUp(self):
                    super().setUp()
                    self.store = HealthStore(self.store.path)
                    self.store.enable_health()
                    self.store.begin_health(self.epoch, self.names, NOW-1000)
                    self.store.instrument_batch(self.epoch, self.required, {'symbols': [dict(
                        symbol=s+'USDT', baseAsset=s, quoteAsset='USDT', status='TRADING',
                        isSpotTradingAllowed=True) for s in ('BTC', 'ETH')]}, NOW, NOW)

                def assert_batch_unknown(self):
                    (review, _, report), _, _ = self.refresh()
                    self.assertEqual(set(report['failures']), {'BTC', 'ETH'})
                    self.assertTrue(all(row['signal_evidence'] == {} for row in review['candidates']))

                def test_composed_draining_rejects_whole_selected_batch(self):
                    self.store.start_draining(self.epoch)
                    self.assert_batch_unknown()

                def test_composed_missing_instrument_rejects_whole_selected_batch(self):
                    with self.store.db() as db:
                        db.execute("DELETE FROM bn_validity WHERE kind='instrument' AND topic=?", (self.required[0],))
                    self.assert_batch_unknown()

                def test_composed_quiet_latch_rejects_whole_selected_batch(self):
                    with self.store.db() as db:
                        db.execute('INSERT INTO metadata(kind,payload) VALUES(?,?)',
                                   ('quiet_reverify:'+self.required[0], '{}'))
                    self.assert_batch_unknown()

            suite = unittest.defaultTestLoader.loadTestsFromTestCase(ComposedHealth)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            good = result.wasSuccessful() and result.testsRun == 11 and not result.skipped and not blocked
            print('BINANCE_MONITOR_PR126_COMPAT_ACCEPTANCE ' + json.dumps(dict(receipt,
                tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                skipped=len(result.skipped), blocked_effects=len(blocked), passed=good,
                selector_changed=False, deployment_verified=False), sort_keys=True))
            return 0 if good else 1
        finally:
            sys.meta_path.remove(loader)
            tempfile.tempdir = previous_temp
            os.chdir(previous)


if __name__ == '__main__':
    raise SystemExit(main())
