"""Typed control publication denial remains fail-closed, never corrupt data.

Scratch-only test; no trading / network / production database access.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research.hunter_binance_control import ScopeQuarantined
from tests.test_hunter_binance_coordinated_runtime import Market


class ReaderPublicationReasonTests(unittest.TestCase):
    def test_pending_publication_is_unknown_with_explicit_control_reason(self):
        with tempfile.TemporaryDirectory(prefix='binance-pending-fence-') as directory:
            market=Market(Path(directory)/'state.sqlite3')
            stamp=market.seed()
            baseline=market.store.freeze(market.names,stamp,stamp)
            self.assertEqual(baseline['status'],'READY')
            with patch.object(market.store.control,'fence',
                              side_effect=ScopeQuarantined('CONTROL_PUBLICATION_PENDING')):
                got=market.store.freeze(market.names,stamp,stamp)
            self.assertEqual(got['status'],'UNKNOWN')
            self.assertEqual(got['reason'],'CONTROL_PUBLICATION_PENDING')
            self.assertEqual(got['windows'],{})
            self.assertNotIn('generation_sha256',got)
            self.assertEqual(market.store.freeze(market.names,stamp,stamp)['status'],'READY')

    def test_other_quarantine_remains_unknown_generic(self):
        with tempfile.TemporaryDirectory(prefix='binance-other-fence-') as directory:
            market=Market(Path(directory)/'state.sqlite3')
            stamp=market.seed()
            with patch.object(market.store.control,'fence',
                              side_effect=ScopeQuarantined('CONTROL_SCOPE_QUARANTINED')):
                got=market.store.freeze(market.names,stamp,stamp)
            self.assertEqual(got['status'],'UNKNOWN')
            self.assertEqual(got['reason'],'BINANCE_CACHE_UNAVAILABLE_OR_CORRUPT')
            self.assertEqual(got['windows'],{})

    def test_real_sqlite_error_stays_unknown_generic(self):
        with tempfile.TemporaryDirectory(prefix='binance-read-error-') as directory:
            market=Market(Path(directory)/'state.sqlite3')
            stamp=market.seed()
            with patch.object(market.store,'_freeze_snapshot',
                              side_effect=sqlite3.OperationalError('database is locked')):
                got=market.store.freeze(market.names,stamp,stamp)
            self.assertEqual(got['status'],'UNKNOWN')
            self.assertEqual(got['reason'],'BINANCE_CACHE_UNAVAILABLE_OR_CORRUPT')
            self.assertEqual(got['windows'],{})


if __name__=='__main__':
    unittest.main()
