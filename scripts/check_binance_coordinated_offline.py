"""Linux-only coordinated recovery acceptance; guarded scratch and loopback."""
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.check_binance_public_transport import main

PATTERNS = ('test_hunter_binance_control.py',
            'test_hunter_binance_coordinated_transport.py',
            'test_hunter_binance_coordinated_scheduler.py',
            'test_hunter_binance_coordinated_runtime.py',
            'test_hunter_binance_consumer_fence.py')

if __name__=='__main__':
    if sys.platform!='linux':
        raise SystemExit('LINUX_REQUIRED: do not count platform skips as acceptance')
    print('COORDINATED_RUNTIME',dict(python=sys.version,sqlite=sqlite3.sqlite_version))
    # Exact count is deliberately maintained with the candidate test suite.
    raise SystemExit(main(patterns=PATTERNS,expected=96,
        extra_helpers=('tests/fixtures/binance_coordinated_crash.py',)))
