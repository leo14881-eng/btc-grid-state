"""Isolated F10 control WAL experiment; NEVER provisions a production database.

Runs the *original* 1228-topic/0.8-second/4096-queue continuous-reader
strict test with a scratch-only SQLite journal override. It is diagnostic and
can never replace the production acceptance gate.
"""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

if sys.platform!='linux' or os.environ.get('GITHUB_ACTIONS')!='true':
    raise SystemExit('SCRATCH_GITHUB_LINUX_ONLY')
from tests import test_hunter_binance_coordinated_runtime as runtime
from tests.test_hunter_binance_coordinated_soak import WholeRosterSoak

original = runtime.Market.__init__
seen = []

def scratch_init(instance,*args,**kwargs):
    original(instance,*args,**kwargs)
    market=Path(instance.path)
    if not market.is_absolute():
        raise RuntimeError('SCRATCH_ABSOLUTE_MARKET_REQUIRED')
    control=market.with_name('control.sqlite3')
    if not control.is_file() or 'binance-coordinated-runtime-' not in str(market.parent.parent):
        raise RuntimeError('SCRATCH_ISOLATION_MARKER_MISSING')
    with closing(sqlite3.connect(control,timeout=.25,isolation_level=None)) as db:
        before=db.execute('PRAGMA journal_mode').fetchone()[0]
        if before!='delete':
            raise RuntimeError('CONTROL_SCRATCH_EXPECTS_DELETE')
        owners=db.execute('SELECT COUNT(*) FROM owners').fetchone()[0]
        if owners:
            raise RuntimeError('CONTROL_SCRATCH_OWNERS_ACTIVE')
        after=db.execute('PRAGMA journal_mode=WAL').fetchone()[0]
        if after!='wal':
            raise RuntimeError('CONTROL_SCRATCH_WAL_SWITCH_FAILED')
        seen.append(dict(before=before,after=after,scratch=str(market.parent)))
    with closing(sqlite3.connect('file:'+str(control)+'?mode=ro',uri=True,timeout=.25)) as db:
        if db.execute('PRAGMA journal_mode').fetchone()[0]!='wal':
            raise RuntimeError('CONTROL_SCRATCH_WAL_READBACK_FAILED')


with patch.object(runtime.Market,'__init__',scratch_init):
    suite=unittest.defaultTestLoader.loadTestsFromName(
        'tests.test_hunter_binance_coordinated_soak.WholeRosterSoak.test_complete_1228_topics_during_continuous_wall_load')
    results=unittest.TextTestRunner(verbosity=2).run(suite)
print('CONTROL_SCRATCH_WAL_PROBE',dict(versions=sqlite3.sqlite_version,fixtures=len(seen),
    gates_passed=results.wasSuccessful(),tests=results.testsRun),flush=True)
if results.testsRun!=1 or len(seen)!=1 or not results.wasSuccessful():
    raise SystemExit(1)
