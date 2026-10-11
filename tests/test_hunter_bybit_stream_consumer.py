import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research import hunter_bybit_stream_store as storage
from research import hunter_bybit_stream_consumer as consumer
from research import hunter_bybit_worker as worker
from research import hunter_cex_scan as scan
from research import hunter_early_signals as early
from research.hunter_bybit_signal_capture import capture
from test_hunter_bybit_stream import NOW, rest, message


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = storage.Store(Path(self.temp.name)/'state.sqlite3')
        self.bases = ['GRASS', 'TA']
        self.names = consumer.topics_for(self.bases)
        self.epoch = self.store.begin_session(self.names, 'a'*64)
        self.store.ack(self.epoch, self.names)
        for name in self.names:
            symbol, interval = storage.split_topic(name)
            self.store.ingest_rest(self.epoch, name, rest(symbol, interval), NOW, NOW)
            self.store.ingest_ws(self.epoch, message(symbol, interval, NOW+1), NOW+1)
        self.end = NOW+1
        self.now = dt.datetime.fromtimestamp(self.end/1000, dt.timezone.utc)
        self.required = [dict(base=b,pair=b+'USDT') for b in self.bases]

    def freeze(self, end=None, now=None):
        return storage.Store.reader(self.store.path).freeze(self.names, end or self.end, now or self.end)

    def collect(self):
        return consumer.collect(self.required, self.now, storage.Store.reader(self.store.path), lambda:self.end)

    def snapshot(self):
        calls = []
        def fetch(path, body=None):
            calls.append(path)
            self.assertEqual(path, '/bybit/tickers')
            self.assertIsNone(body)
            return dict(retCode=0,time=self.end,result=dict(category='spot',list=[
                dict(symbol=b+'USDT',lastPrice='101',turnover24h='10000',price24hPcnt='.01')
                for b in ['BTC']+self.bases]))
        with patch.object(worker,'DEFAULT',Path(self.temp.name)/'snapshot.json'):
            rows, status = worker.collect({'BTC'}, self.now, fetch,
                lambda:(['BTC']+self.bases,dict(cache_hit=True,network_requests=0)),
                kline_source='SPOT_CACHE',cache_reader=storage.Store.reader(self.store.path),cache_clock=lambda:self.end)
            snapshot = json.loads(worker.DEFAULT.read_text())
        return rows, status, snapshot, calls

    def test_complete_generation_matches_legacy_feature_numbers(self):
        signals, failures, generation = self.collect()
        old, old_failures = capture(self.required,lambda s,i,n:sorted(rest(s,i)['result']['list']),observed_at=self.now)
        self.assertEqual(failures,old_failures)
        for base in self.bases:
            actual = {k:v for k,v in signals[base].items() if k not in ('source_provenance','source_observed_at_utc')}
            expected = {k:v for k,v in old[base].items() if k != 'source_provenance'}
            self.assertEqual(actual,expected)
            self.assertEqual(signals[base]['source_provenance']['micro']['source'],consumer.SOURCE)
        self.assertEqual(generation['status'],'READY')

    def test_cross_symbol_revisions_after_cut_do_not_change_frozen_generation(self):
        before = self.freeze()
        for name in self.names:
            symbol, interval = storage.split_topic(name)
            update = message(symbol,interval,NOW+2)
            update['data'][0]['close']='102'
            self.store.ingest_ws(self.epoch, update, NOW+2)
        self.assertEqual(self.freeze(now=NOW+3),before)
        after = self.freeze(end=NOW+2,now=NOW+3)
        self.assertNotEqual(after['generation_sha256'],before['generation_sha256'])
        self.assertTrue(all(w['rows'][-1][4]=='102' for w in after['windows'].values()))

    def test_all_topics_share_one_sqlite_read_transaction_during_reconnect(self):
        with sqlite3.connect(self.store.path) as db:
            db.execute('PRAGMA journal_mode=WAL')
        original = storage.valid_row
        changed = []
        def reconnect(row, interval):
            if not changed:
                changed.append(self.store.begin_session(self.names,'b'*64))
            return original(row,interval)
        with patch.object(storage,'valid_row',reconnect):
            frozen = self.freeze()
        self.assertEqual(frozen['status'],'READY')
        self.assertEqual(frozen['epoch'],self.epoch)
        self.assertEqual(frozen['manifest_sha256'],'a'*64)
        self.assertEqual(self.freeze()['status'],'UNKNOWN')

    def test_future_only_history_is_unknown_not_backfilled(self):
        name = storage.topic('TAUSDT','15')
        with self.store.db() as db:
            db.execute('DELETE FROM revisions WHERE topic=?',(name,))
        self.store.ingest_ws(self.epoch,message('TAUSDT','15',NOW+2),NOW+2)
        self.assertEqual(self.freeze()['reason'],'HISTORY_MISSING_FOR_GENERATION')

    def test_roster_changes_fail_whole_generation_without_expansion(self):
        other = consumer.topics_for(['GRASS'])
        self.assertEqual(self.store.freeze(other,self.end,self.end)['reason'],'ROSTER_OR_MANIFEST_MISMATCH')
        with self.store.db() as db:
            db.execute("UPDATE meta SET value=? WHERE key='roster_hash'",('b'*64,))
        self.assertEqual(self.freeze()['status'],'UNKNOWN')

    def test_manifest_receipt_is_distinct_from_stable_roster_hash(self):
        frozen = self.freeze()
        self.assertEqual(frozen['manifest_sha256'],'a'*64)
        self.assertEqual(frozen['roster_hash'],storage.roster_hash(self.names))
        self.assertNotEqual(frozen['manifest_sha256'],frozen['roster_hash'])

    def test_retention_is_bounded_and_eviction_returns_unknown(self):
        for n in range(2,300):
            self.store.ingest_ws(self.epoch,message('TAUSDT','15',NOW+n),NOW+n)
        with self.store.db(readonly=True) as db:
            count = db.execute('SELECT COUNT(*) FROM revisions WHERE topic=? AND start=?',
                (storage.topic('TAUSDT','15'),NOW//900000*900000)).fetchone()[0]
        self.assertLessEqual(count,storage.MAX_REVISIONS)
        self.assertEqual(self.freeze(now=NOW+300)['status'],'UNKNOWN')

    def test_global_deny_and_cooldown_block_even_otherwise_ready_cache(self):
        for text in ('access too frequent','country','unknown forbidden'):
            storage.Budget(self.store).failure(403,text,self.end)
            self.assertEqual(self.freeze()['reason'],'COLLECTOR_DISCONNECTED_OR_GLOBAL_STOP')
        signals, failures, _ = self.collect()
        self.assertEqual(signals,{})
        self.assertEqual(set(failures),{'BTCUSDT','GRASSUSDT','TAUSDT'})

    def test_missing_one_asset_or_btc_propagates_unknown_and_no_worker_kline(self):
        for symbol in ('TAUSDT','BTCUSDT'):
            with self.store.db() as db:
                db.execute('DELETE FROM revisions WHERE topic=?',(storage.topic(symbol,'15'),))
            rows,status,snapshot,calls = self.snapshot()
            self.assertEqual(calls,['/bybit/tickers'])
            self.assertEqual(snapshot['early_signals'],{})
            self.assertFalse(status['signal_complete'])
            self.assertEqual(len(rows),3)
            scan_fixture={'generation_id':'g','coins':{b:dict(venues=['bybit']) for b in self.bases}}
            benchmark={'BTCUSDT':dict(return_pct=0)}
            report=early.build(scan_fixture,benchmark,benchmark,{},self.now,snapshot['early_signals'])
            self.assertEqual(report['bybit_only_unscored'],self.bases)
            self.assertEqual(report['all_signals'],[])

    def test_live_consumer_contract_separates_ticker_listing_and_kline_sources(self):
        _,status,snapshot,calls = self.snapshot()
        self.assertEqual(calls,['/bybit/tickers'])
        self.assertEqual(status['source'],consumer.SOURCE)
        self.assertEqual(status['ticker_source'],worker.SOURCE)
        self.assertEqual(status['listing_source'],worker.SOURCE)
        self.assertEqual(status['kline_batch_requests'],0)
        self.assertEqual(snapshot['schema'],consumer.SCHEMA)
        worker.validate(snapshot,self.now)

    def test_rehashed_wrong_source_scope_history_and_signal_are_rejected(self):
        _,_,snapshot,_=self.snapshot()
        for mutation in ('source','roster','history','packet','generation','complete','score','scalar','masquerade'):
            bad=copy.deepcopy(snapshot)
            if mutation=='source':bad['venue_status']['kline_source']=worker.SOURCE
            if mutation=='roster':bad['venue_status']['signal_expected_bases']=['GRASS']
            if mutation=='history':bad['cache_generation']['windows'][self.names[0]]['rows'][0][0]='0'
            if mutation=='packet':bad['early_signals']['TA']['source_provenance']['btc_1h']['cache']['epoch']='b'*32
            if mutation=='generation':bad['cache_generation']['generation_end_ms']+=1
            if mutation=='complete':bad['venue_status']['signal_complete']=False
            if mutation=='score':bad['early_signals']['TA']['score']+=1
            if mutation=='scalar':bad['early_signals']['TA']['return_1h_pct']+=1
            if mutation=='masquerade':bad.update(schema='hunter_bybit_worker_v1',source=worker.SOURCE)
            gen=bad['cache_generation']
            gen['generation_sha256']=storage.content_hash({k:v for k,v in gen.items() if k!='generation_sha256'})
            bad['venue_status']['cache_generation_sha256']=gen['generation_sha256']
            bad['snapshot_sha256']=worker.digest({k:v for k,v in bad.items() if k!='snapshot_sha256'})
            with self.assertRaises(ValueError):worker.validate(bad,self.now)

    def test_default_worker_route_unchanged_and_optin_explicit(self):
        with patch.dict('os.environ',{'HUNTER_BYBIT_WORKER_MARKET_ENABLED':'1'},clear=True),patch.object(worker,'collect',return_value=([],{})) as call:
            scan.bybit_from_authorized_region({'BTC'})
            call.assert_called_once_with({'BTC'})
        with patch.dict('os.environ',{'HUNTER_BYBIT_WORKER_MARKET_ENABLED':'1','HUNTER_BYBIT_KLINE_SOURCE':'SPOT_CACHE'},clear=True),patch.object(worker,'collect',return_value=([],{})) as call:
            scan.bybit_from_authorized_region({'BTC'})
            call.assert_called_once_with({'BTC'},kline_source='SPOT_CACHE')

    def test_invalid_selector_never_falls_through_to_regional_or_network(self):
        for value in ('TYPO','SPOT_CACHE'):
            with patch.dict('os.environ',{'HUNTER_BYBIT_KLINE_SOURCE':value},clear=True),patch.object(scan,'bybit_with_fallback',side_effect=AssertionError('NO_FALLBACK')):
                with self.assertRaises(ValueError):scan.bybit_from_authorized_region()

    def test_missing_database_reader_does_not_create_files(self):
        path=Path(self.temp.name)/'absent.sqlite3'
        result=storage.Store.reader(path).freeze(self.names,self.end,self.end)
        self.assertEqual(result['status'],'UNKNOWN')
        self.assertFalse(path.exists())

    def test_old_generation_and_new_session_cannot_reuse_cache(self):
        self.assertEqual(self.freeze(now=self.end+90001)['reason'],'GENERATION_STALE_OR_FUTURE')
        self.store.disconnect(self.epoch)
        new=self.store.begin_session(self.names,'b'*64)
        self.store.ack(new,self.names)
        self.assertEqual(self.freeze()['status'],'UNKNOWN')

    def test_corrupt_transport_fields_never_become_ready(self):
        name=self.names[0]
        for field,value in [('origin',None),('confirmed',2),('epoch','broken'),('observed','oops')]:
            with self.store.db() as db:
                original=db.execute('SELECT * FROM revisions WHERE topic=? LIMIT 1',(name,)).fetchone()
                db.execute('UPDATE revisions SET '+field+'=? WHERE topic=? AND start=? AND source_time=?',
                    (value,name,original['start'],original['source_time']))
            result=self.freeze()
            self.assertEqual(result['status'],'UNKNOWN')
            self.assertEqual(result['windows'],{})
            with self.store.db() as db:
                db.execute('UPDATE revisions SET '+field+'=? WHERE topic=? AND start=? AND source_time=?',
                    (original[field],name,original['start'],original['source_time']))


if __name__ == '__main__':
    unittest.main()
