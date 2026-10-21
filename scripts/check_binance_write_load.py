"""Guarded, exact-checkout load acceptance and an immutable cache comparison.

The comparison pins only the two ingest modules and the frozen consumer. Coordination, diagnostics,
fixtures and dependencies remain those of the candidate; it is not a replay
of the complete prior deployment. Neither mode accesses an exchange/server.
"""
import argparse
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from scripts.check_binance_monitor_health_compat import git_read
from scripts.check_binance_public_transport import main as guarded

PRIOR = 'ec3d49b9ac3b098a7a5e7f7f3fbaee5e9b7efc9d'
MODULES = ('research.hunter_binance_stream_store', 'research.hunter_binance_health_store',
           'research.hunter_binance_stream_consumer')


class PriorIngest(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources):
        self.sources = sources

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.sources:
            return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = '<prior-ingest:'+PRIOR+':'+module.__name__+'>'
        exec(compile(self.sources[module.__name__], module.__file__, 'exec'), module.__dict__)
        module.__comparison_source_head__ = PRIOR


def only_capacity_failure(report):
    """A queue-full string cannot hide a second thread, SQLite or cleanup error."""
    if not isinstance(report, dict):
        return False
    work = report.get('jobs_shutdown') or {}
    terminals = report.get('control_terminal_counts') or {}
    if any(type(terminals.get(key)) is not int or terminals[key] < 0 for key in
           ('owners', 'requests', 'calls', 'terminal_calls', 'attempts', 'retired_attempts')):
        return False
    if not (report.get('phase') == 'POST_DRAIN_ASSERTIONS'
            and report.get('error', {}).get('type') == 'AssertionError'
            and [tuple(value) for value in report.get('thread_errors', [])]
                == [('producer', 'AssertionError', 'SOAK_QUEUE_FULL')]
            and report.get('diagnostic_errors') == []
            and report.get('load_error') is None
            and report.get('queue_depth') == 0
            and len(report.get('threads', [])) == 3
            and all(value.get('started') and not value.get('alive') for value in report['threads'])
            and work.get('complete') is True and work.get('errors') == []
            and report.get('all_topic_receipts_conserved') is True
            and report.get('partition_after_all_workers_stopped') is True
            and terminals.get('owners') == terminals.get('requests') == 0
            and terminals.get('calls') == terminals.get('terminal_calls')
            and terminals.get('attempts') == terminals.get('retired_attempts')):
        return False
    for label in ('market_transactions', 'control_transactions'):
        values = report.get(label)
        if not isinstance(values, dict):
            return False
        if any(value.get('errors') for value in values.values() if isinstance(value, dict)):
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, choices=(32, 3600), default=32)
    parser.add_argument('--prior-cache', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('LINUX_REQUIRED: platform skips are not acceptance')
    if args.prior_cache and args.seconds != 32:
        raise SystemExit('PRIOR_COMPARISON_REQUIRES_ORIGINAL_32_SECOND_TARGET')
    head = git_read('rev-parse', 'HEAD').decode().strip()
    sources = {}
    loader = None
    if args.prior_cache:
        if git_read('rev-parse', PRIOR+'^{commit}').decode().strip() != PRIOR:
            raise RuntimeError('PRIOR_INGEST_COMMIT_MISMATCH')
        sources = {name: git_read('show', PRIOR+':'+name.replace('.', '/')+'.py') for name in MODULES}
        if any(name in sys.modules for name in MODULES):
            raise RuntimeError('PRIOR_INGEST_ALREADY_IMPORTED')
        loader = PriorIngest(sources)
        sys.meta_path.insert(0, loader)
    os.environ['BINANCE_COORDINATED_SOAK_SECONDS'] = str(args.seconds)
    print('BINANCE_WRITE_LOAD_SOURCE '+json.dumps(dict(
        candidate_head=head, target_wall_seconds=args.seconds,
        prior_cache_head=PRIOR if args.prior_cache else None,
        prior_module_sha256={name: hashlib.sha256(value).hexdigest() for name, value in sources.items()},
        fixture_clock='FIXED_UTC_PLUS_REAL_ELAPSED', natural_utc_verified=False), sort_keys=True), flush=True)
    try:
        if args.prior_cache:
            # Importing defines tests only. Every test runs under guarded().
            import test_hunter_binance_coordinated_soak as cases
            for name in MODULES:
                if getattr(sys.modules.get(name), '__comparison_source_head__', None) != PRIOR:
                    raise RuntimeError('PRIOR_INGEST_IDENTITY_MISMATCH')
            original = cases.WholeRosterSoak.test_complete_1228_topics_during_continuous_wall_load

            @unittest.expectedFailure
            def queue_failure_only(instance):
                try:
                    original(instance)
                except AssertionError as error:
                    try:
                        expected = ('SOAK_QUEUE_FULL' in str(error)
                                    and only_capacity_failure(getattr(instance, 'soak_report', None)))
                    except BaseException as classification_error:
                        raise KeyboardInterrupt('PRIOR_INGEST_CLASSIFICATION_ERROR') from classification_error
                    if not expected:
                        raise KeyboardInterrupt('PRIOR_INGEST_UNEXPECTED_ASSERTION') from error
                    raise
                except BaseException as error:
                    raise KeyboardInterrupt('PRIOR_INGEST_UNEXPECTED_ERROR') from error

            cases.WholeRosterSoak.test_complete_1228_topics_during_continuous_wall_load = queue_failure_only
        return guarded(pattern='test_hunter_binance_coordinated_soak.py', expected=2,
            expected_failures=int(args.prior_cache),
            extra_helpers=('tests/fixtures/binance_ws_reconnect.py',))
    finally:
        if loader is not None:
            sys.meta_path.remove(loader)


if __name__ == '__main__':
    raise SystemExit(main())
