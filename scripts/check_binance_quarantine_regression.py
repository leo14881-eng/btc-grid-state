"""Reproduce the two lost-reverification cases on the immutable prior scheduler.

Loads only that scheduler from a local Git object. Candidate regression cases
must fail with assertions, not import/runtime errors. No network or deployment.
"""
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from scripts.check_binance_monitor_health_compat import git_read, protect_scratch

PINNED_HEAD = '0b15bd7dde4d90519f316f1a146fd13da6f6b91a'
MODULE = 'research.hunter_binance_health'
CASES = (
    'test_quiet_reverification_survives_fresh_websocket_removing_market_need',
    'test_repair_reverification_survives_websocket_resolving_gap_and_current_bar',
)


class PinnedScheduler(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, source):
        self.source = source

    def find_spec(self, fullname, path=None, target=None):
        if fullname == MODULE:
            return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.source, '<prior-scheduler:'+PINNED_HEAD+'>', 'exec'), module.__dict__)
        module.__regression_source_head__ = PINNED_HEAD


def main():
    # Read the already-local immutable object before installing the no-exec guard.
    if git_read('rev-parse', PINNED_HEAD+'^{commit}').decode().strip() != PINNED_HEAD:
        raise RuntimeError('PINNED_SCHEDULER_COMMIT_MISMATCH')
    source = git_read('show', PINNED_HEAD+':'+MODULE.replace('.', '/')+'.py')
    candidate = git_read('rev-parse', 'HEAD').decode().strip()
    if MODULE in sys.modules:
        raise RuntimeError('UNPINNED_SCHEDULER_ALREADY_IMPORTED')
    loader = PinnedScheduler(source)
    blocked = []
    with tempfile.TemporaryDirectory(prefix='binance-quarantine-regression-') as directory:
        scratch = Path(directory).resolve()
        previous, previous_temp = Path.cwd(), tempfile.tempdir
        tempfile.tempdir = str(scratch)
        protect_scratch(scratch, blocked)
        sys.meta_path.insert(0, loader)
        os.chdir(scratch)
        try:
            from tests.test_hunter_binance_coordinated_scheduler import SchedulerTests, health
            if getattr(health, '__regression_source_head__', None) != PINNED_HEAD:
                raise RuntimeError('PINNED_SCHEDULER_IDENTITY_MISMATCH')
            suite = unittest.TestSuite(SchedulerTests(name) for name in CASES)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            failed = sorted(test._testMethodName for test, _ in result.failures)
            good = (result.testsRun == 2 and failed == sorted(CASES) and
                    not result.errors and not result.skipped and not blocked)
            print('BINANCE_PRIOR_QUARANTINE_REPRODUCTION '+json.dumps(dict(
                prior_head=PINNED_HEAD, scheduler_sha256=hashlib.sha256(source).hexdigest(),
                candidate_head=candidate, tests=result.testsRun, assertion_failures=failed,
                errors=len(result.errors), skipped=len(result.skipped),
                blocked_effects=len(blocked), reproduced=good), sort_keys=True))
            return 0 if good else 1
        finally:
            sys.meta_path.remove(loader)
            tempfile.tempdir = previous_temp
            os.chdir(previous)


if __name__ == '__main__':
    raise SystemExit(main())
