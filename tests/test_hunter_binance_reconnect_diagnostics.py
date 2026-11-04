"""Failure evidence remains readable even when both real SQLite files are busy."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research.hunter_binance_control import Control
from research.hunter_binance_market_control import MarketControlMixin
from tests.fixtures.binance_reconnect_diagnostics import (
    MAX_ERRORS, MAX_TRACEBACK, append_error, error_detail, final_state)


class FixtureStore(MarketControlMixin):
    def __init__(self, path, control):
        self.path = path
        self._existing = True
        self.control = control
        self._local()

    def metadata(self):
        with self.db(readonly=True, timeout=.01) as db:
            return dict(db.execute('SELECT key,value FROM meta'))


class ReconnectDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name)
        control_path = self.folder/'control.sqlite3'
        market_path = self.folder/'market.sqlite3'
        with closing(sqlite3.connect(control_path)) as db:
            db.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);"
                "INSERT INTO meta VALUES('format','binance_control_v1'),('boot','fixture');"
                "CREATE TABLE owners(value); CREATE TABLE requests(value);"
                "CREATE TABLE calls(terminal); CREATE TABLE attempts(terminal);")
            db.commit()
        (self.folder/'control.sqlite3.gate').write_bytes(b'0')
        with closing(sqlite3.connect(market_path)) as db:
            db.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);"
                "INSERT INTO meta VALUES('connected','0');")
            db.commit()
        self.control = Control(control_path, boot='fixture')
        self.store = FixtureStore(market_path, self.control)

    def test_original_trace_and_connection_context_survive_failure(self):
        with closing(sqlite3.connect(self.store.path)) as external:
            external.execute('BEGIN EXCLUSIVE')
            try:
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    self.store.metadata()
                detail = error_detail('fixture.read', caught.exception)
            finally:
                external.rollback()
        self.assertEqual(detail['operation'], 'fixture.read')
        self.assertEqual(detail['sqlite_context']['begin_statement'], 'BEGIN')
        self.assertEqual(detail['sqlite_context']['busy_timeout_ms'], 10)
        self.assertEqual(detail['sqlite_context']['phase'], 'body')
        self.assertEqual(detail['sqlite_errorcode'], getattr(caught.exception, 'sqlite_errorcode', 'unknown'))
        # unittest clears the caught traceback; collect inside except in the
        # fixture, and separately exercise that exact capture order here.
        try:
            raise caught.exception
        except sqlite3.Error as error:
            detail = error_detail('fixture.read', error)
        self.assertIn('raise caught.exception', detail['traceback'])
        self.assertFalse(detail['traceback_truncated'])

    def test_final_reads_fail_independently_without_masking_primary_evidence(self):
        primary = error_detail('collect', RuntimeError('original collector failure'))
        with closing(sqlite3.connect(self.store.path)) as market_db, \
                closing(sqlite3.connect(self.control.path)) as control_db:
            market_db.execute('BEGIN EXCLUSIVE')
            control_db.execute('BEGIN EXCLUSIVE')
            try:
                result = final_state(self.store, self.folder)
            finally:
                market_db.rollback()
                control_db.rollback()
        self.assertEqual(primary['message'], 'original collector failure')
        self.assertIsNone(result['active_owners'])
        self.assertIsNone(result['connected'])
        self.assertEqual(result['orphan_files'], 0)
        self.assertEqual([e['operation'] for e in result['final_read_errors']],
            ['control.final_counts', 'market.final_metadata'])
        self.assertIsInstance(result['market_transactions'], dict)
        self.assertIsInstance(result['control_transactions'], dict)
        self.assertNotIn('wal_bytes', result['market_transactions'])

    def test_metrics_snapshot_never_reads_sqlite_or_filesystem(self):
        with patch.object(sqlite3, 'connect', side_effect=AssertionError('must not connect')), \
                patch.object(Path, 'stat', side_effect=AssertionError('must not stat')):
            self.assertIsInstance(self.store.transaction_diagnostics(include_file_state=False), dict)
            self.assertIsInstance(self.control.transaction_diagnostics(), dict)

    def test_bounded_errors_preserve_first_original_contexts(self):
        errors = []
        for number in range(MAX_ERRORS + 3):
            append_error(errors, error_detail('fixture', ValueError(str(number))))
        self.assertEqual(len(errors), MAX_ERRORS)
        self.assertEqual(errors[0]['message'], '0')
        with patch('traceback.format_exception', return_value=['x'*(MAX_TRACEBACK+1)]):
            result = error_detail('fixture', ValueError('large traceback'))
        self.assertEqual(len(result['traceback']), MAX_TRACEBACK)
        self.assertTrue(result['traceback_truncated'])

    def test_successful_final_counts_remain_actual_sqlite_values(self):
        result = final_state(self.store, self.folder)
        self.assertEqual(result['final_read_errors'], [])
        self.assertEqual([result[key] for key in ('active_owners', 'requests', 'calls', 'terminals', 'attempts')], [0]*5)
        self.assertEqual(result['connected'], '0')


if __name__ == '__main__':
    unittest.main()
