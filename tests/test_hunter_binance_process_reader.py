"""Linux native process reader acceptance with real scratch SQLite and fences."""
import sys
import unittest

from research.hunter_binance_process_reader import read_in_process, ReadOnlyStore
from tests import test_hunter_binance_coordinated_runtime as fx


class ProcessReaderContract(unittest.TestCase):
    def test_readonly_store_never_writes(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            # Constructor makes no database and read-only mode cannot create one.
            store=ReadOnlyStore.reader(Path(tmp)/'absent.sqlite3')
            with self.assertRaisesRegex(RuntimeError,'WRITE_FORBIDDEN'):
                with store.db():
                    pass

    @unittest.skipUnless(sys.platform=='linux','NATIVE_LINUX_CONTROL_LOCK_REQUIRED')
    def test_independent_reader_and_drain_revocation(self):
        case=fx.DataCases('test_utc_midnight_vietnam_seven_and_native_boundaries_require_new_ws')
        case.setUp()
        try:
            market=case.market('process-reader')
            now=market.seed()
            when=fx.dt.datetime.fromtimestamp(now/1000,fx.dt.timezone.utc)
            baseline=market.read(market.names,now)
            delivered=read_in_process(market.names,when,database=market.path,
                clock=lambda:now,timeout=20)
            self.assertEqual(delivered.generation['generation_sha256'],
                             baseline.generation['generation_sha256'])
            self.assertEqual(set(delivered.generation['windows']),set(market.names))
            delivered.check_delivery()
            with self.assertRaises((ValueError,RuntimeError)):
                read_in_process(market.names,when,database=market.path,
                    clock=lambda:now,timeout=20,
                    before_delivery=lambda:market.store.start_draining(market.epoch))
        finally:
            case.doCleanups()


    def test_research_monitor_and_single_window_route_to_child(self):
        from unittest.mock import patch
        from research import hunter_binance_stream_consumer as consumer
        import datetime as dt
        now=dt.datetime(2026,10,14,tzinfo=dt.timezone.utc)
        sentinel=object()
        with patch.object(consumer,'selected',return_value='SPOT_CACHE'), \
             patch.object(consumer,'isolated_read',return_value=sentinel) as isolated, \
             patch.object(consumer,'roster',return_value=['btcusdt@kline_15m']), \
             patch.object(consumer,'roster_hash',return_value='f'*64):
            self.assertIs(consumer.management_inputs(['ETHUSDT'],now),sentinel)
            self.assertEqual(len(isolated.call_args.args[0]),4)
            self.assertIs(consumer.window('BTCUSDT','1h',now),sentinel)
            self.assertEqual(isolated.call_args.args[0],['btcusdt@kline_1h'])
            self.assertIs(consumer.signal_inputs({'as_of_utc':'irrelevant'},
                ['ETHUSDT']),sentinel)
            self.assertEqual(isolated.call_args.kwargs['expected_roster'],'f'*64)

if __name__=='__main__':
    unittest.main()
