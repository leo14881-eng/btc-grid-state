"""Prove real stale-signal exits on the immutable prior V2 Monitor and engine.

Only these two modules are pinned; scratch cache fixtures and other dependencies
come from the candidate. This is not an end-to-end replay of the old deployment.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from scripts.check_binance_monitor_health_compat import git_read, protect_scratch

PINNED_HEAD = '0b15bd7dde4d90519f316f1a146fd13da6f6b91a'
MODULES = ('research.hunter_shadow_trader_v2', 'research.hunter_position_monitor')
CASES = ('test_identity_io_revocation_blocks_real_target_momentum_exit',
         'test_identity_io_expiry_blocks_real_thesis_exit_and_recovery')


def main():
    if git_read('rev-parse', PINNED_HEAD+'^{commit}').decode().strip() != PINNED_HEAD:
        raise RuntimeError('PINNED_SIGNAL_USE_COMMIT_MISMATCH')
    sources = {name: git_read('show', PINNED_HEAD+':'+name.replace('.', '/')+'.py')
               for name in MODULES}
    candidate = git_read('rev-parse', 'HEAD').decode().strip()
    blocked = []
    with tempfile.TemporaryDirectory(prefix='binance-signal-use-regression-') as directory:
        scratch = Path(directory).resolve()
        previous, previous_temp = Path.cwd(), tempfile.tempdir
        tempfile.tempdir = str(scratch)
        protect_scratch(scratch, blocked)
        os.chdir(scratch)
        try:
            from tests import test_hunter_binance_signal_use as test
            modules = {}
            for name, source in sources.items():
                module = types.ModuleType(name)
                module.__file__ = '<prior-'+PINNED_HEAD+':'+name+'>'
                exec(compile(source, module.__file__, 'exec'), module.__dict__)
                modules[name] = module
            engine, monitor = (modules[name] for name in MODULES)
            monitor.eng = engine
            if (monitor.run_lane.__globals__['eng'] is not engine or
                    monitor.configure_lane.__globals__['eng'] is not engine or
                    engine.manage_existing_positions.__globals__ is not engine.__dict__):
                raise RuntimeError('PINNED_SIGNAL_USE_IDENTITY_MISMATCH')
            suite = unittest.TestSuite(test.SignalUseTests(name) for name in CASES)
            with patch.object(test, 'monitor', monitor):
                result = unittest.TextTestRunner(verbosity=2).run(suite)
            failed = sorted(case._testMethodName for case, _ in result.failures)
            good = (result.testsRun == 2 and failed == sorted(CASES) and
                    not result.errors and not result.skipped and not blocked)
            print('BINANCE_PRIOR_SIGNAL_USE_REPRODUCTION '+json.dumps(dict(
                prior_head=PINNED_HEAD, candidate_head=candidate,
                source_sha256={name: hashlib.sha256(data).hexdigest() for name, data in sources.items()},
                tests=result.testsRun, assertion_failures=failed, errors=len(result.errors),
                skipped=len(result.skipped), blocked_effects=len(blocked), reproduced=good), sort_keys=True))
            return 0 if good else 1
        finally:
            tempfile.tempdir = previous_temp
            os.chdir(previous)


if __name__ == '__main__':
    raise SystemExit(main())
