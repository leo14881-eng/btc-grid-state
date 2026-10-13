"""Monitor's original selected holdings use one complete immutable Kline cut."""
import copy
import datetime as dt
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from research import hunter_binance_stream_consumer as consumer
from research import hunter_position_monitor as monitor
from research.hunter_binance_stream_store import Store, topic, roster, split_topic
from tests.test_hunter_binance_cache import NOW, DATE, rows, message, scan


class MonitorCacheTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)/'state.sqlite3')
        self.names=roster(scan(),DATE)
        self.epoch=self.store.begin_session(self.names,'a'*64)
        self.store.ack(self.epoch,self.names)
        self.required=sorted(topic(s,i) for s in ('BTCUSDT','ETHUSDT') for i in ('15m','1h'))
        for name in self.required:
            symbol,interval=split_topic(name)
            self.store.ingest_rest(self.epoch,name,rows(interval),NOW,NOW)
            self.store.ingest_ws(self.epoch,message(symbol,interval),NOW+1)
        self.actual_read=consumer.read

    def read(self,names,now,**kwargs):
        return self.actual_read(names,now,reader=self.store,clock=lambda:int(now.timestamp()*1000),**kwargs)

    def refresh(self,source='SPOT_CACHE',states=None,review=None,now=DATE,read=None):
        states=states or [{'open_positions':[{'asset':'ETH'}]}, {'open_positions':[{'asset':'BTC'}]}]
        with ExitStack() as stack:
            stack.enter_context(patch.object(consumer,'selected',return_value=source))
            reader=stack.enter_context(patch.object(consumer,'read',side_effect=read or self.read))
            stack.enter_context(patch.object(monitor,'load',return_value={'evidence_refresh_cursor':0}))
            stack.enter_context(patch.object(monitor.books,'live_fetch',return_value={'fixture':'book'}))
            stack.enter_context(patch.object(monitor.books,'measure',return_value={'raw_book_evidence':{'fixture':'book'}}))
            estimate=stack.enter_context(patch.object(monitor.books,'estimate',return_value={'buy_slippage_bps':10}))
            stack.enter_context(redirect_stdout(StringIO()))
            result=monitor.refresh_management_evidence(states,{},review or {},{},now)
        return result,reader,estimate

    def test_rest_selector_does_not_read_cache_or_change_call_signature(self):
        with patch.object(consumer,'selected',return_value='REST'),patch.object(consumer,'read',side_effect=AssertionError):
            self.assertIsNone(consumer.management_inputs(['ETHUSDT'],DATE))
        with patch.object(monitor.signals,'rolling',return_value={}) as rolling,patch.object(monitor.signals,'micro',return_value={}) as micro:
            _,reader,_=self.refresh('REST')
        reader.assert_not_called()
        self.assertEqual(rolling.call_args_list[0].args,(['BTCUSDT','ETHUSDT'],'1h'))
        self.assertEqual(rolling.call_args_list[1].args,(['BTCUSDT','ETHUSDT'],'4h'))
        self.assertEqual(micro.call_args.args,(['BTCUSDT','ETHUSDT'],))

    def test_full_unique_holdings_and_btc_use_one_cut_without_scan_age_gate(self):
        with patch.object(consumer,'selected',return_value='SPOT_CACHE'),patch.object(consumer,'read',side_effect=self.read) as read,patch.object(consumer,'roster',side_effect=AssertionError('Discovery age gate is unrelated')):
            frozen=consumer.management_inputs(['ETHUSDT','BTCUSDT','ETHUSDT'],DATE)
        read.assert_called_once_with(self.required,DATE)
        self.assertEqual(set(frozen.generation['windows']),set(self.required))
        self.assertEqual(frozen.generation['generation_end_ms'],NOW+1)
        self.assertEqual({split_topic(n)[1] for n in self.required},{'15m','1h'})
        self.assertEqual(len(frozen.rows('ETHUSDT','15m')),25)
        self.assertEqual(len(frozen.rows('ETHUSDT','1h')),5)

    def test_same_frozen_reaches_all_three_signal_reads(self):
        with patch.object(monitor.signals,'get',side_effect=AssertionError('No REST fallback')),patch.object(monitor.signals,'rolling',wraps=monitor.signals.rolling) as rolling,patch.object(monitor.signals,'micro',wraps=monitor.signals.micro) as micro:
            (review,_,report),reader,_=self.refresh()
        reader.assert_called_once_with(self.required,DATE)
        frozen=rolling.call_args_list[0].args[2]
        self.assertIs(rolling.call_args_list[1].args[2],frozen)
        self.assertIs(micro.call_args.args[1],frozen)
        self.assertEqual(report['failures'],{})
        self.assertEqual(report['attempted'],['BTC','ETH'])
        packets=[p for row in review['candidates'] for p in row['signal']['source_provenance'].values()]
        self.assertTrue(packets)
        self.assertEqual({p['cache']['generation_sha256'] for p in packets},{frozen.generation['generation_sha256']})
        self.assertEqual({p['cache']['epoch'] for p in packets},{self.epoch})

    def test_cache_and_rest_keep_formulas_fees_books_and_signal_timestamp(self):
        def rest(url,*args):
            from urllib.parse import urlsplit,parse_qs
            q=parse_qs(urlsplit(url).query)
            return rows(q['interval'][0])[-int(q['limit'][0]):]
        with patch.object(monitor.signals,'get',side_effect=rest):
            (rest_review,rest_liq,rest_report),_,rest_estimate=self.refresh('REST')
        with patch.object(monitor.signals,'get',side_effect=AssertionError('No REST fallback')):
            (cached_review,cached_liq,cached_report),_,cached_estimate=self.refresh()
        for report in (rest_review,cached_review):
            for candidate in report['candidates']:candidate['signal'].pop('source_provenance')
            report['candidates'].sort(key=lambda row:row['asset'])
        self.assertEqual(cached_review,rest_review)
        self.assertEqual(cached_liq,rest_liq)
        self.assertEqual(cached_report,rest_report)
        self.assertEqual(sorted(call.args[1] for call in cached_estimate.call_args_list),[1000,1000,2000,2000,3000,3000,4000,4000])
        self.assertEqual(cached_estimate.call_count,rest_estimate.call_count)

    def test_missing_required_topic_fails_whole_original_batch_without_fallback(self):
        stale={'candidates':[{'asset':'ETH','signal_evidence':{'old':'must be invalidated'},'signal':{'score':100}}]}
        states=[{'open_positions':[{'asset':'ETH'},{'asset':'MISSING'}]},{'open_positions':[]}]
        with patch.object(monitor.signals,'get',side_effect=AssertionError),patch.object(monitor.signals,'rolling',side_effect=AssertionError) as rolling:
            (review,_,report),reader,estimate=self.refresh(states=states,review=stale)
        rolling.assert_not_called();estimate.assert_not_called()
        self.assertEqual(set(report['failures']),{'ETH','MISSING'})
        self.assertEqual(report['attempted'],['ETH','MISSING'])
        self.assertTrue(all(row['signal_evidence']=={} for row in review['candidates']))
        self.assertIn(topic('MISSINGUSDT','15m'),reader.call_args.args[0])
        self.assertIn(topic('BTCUSDT','1h'),reader.call_args.args[0])

    def test_stale_or_disconnected_cache_invalidates_all_selected_evidence(self):
        for now,disconnect in ((DATE+dt.timedelta(seconds=91),False),(DATE,True)):
            if disconnect:self.store.disconnect(self.epoch)
            with patch.object(monitor.signals,'get',side_effect=AssertionError),patch.object(monitor.signals,'rolling',side_effect=AssertionError) as rolling:
                (review,_,report),_,estimate=self.refresh(now=now)
            rolling.assert_not_called();estimate.assert_not_called()
            self.assertEqual(set(report['failures']),{'BTC','ETH'})
            self.assertTrue(all(row['signal_evidence']=={} for row in review['candidates']))

    def test_read_finish_expiry_or_clock_rollback_never_falls_back(self):
        for times in ([NOW+1,NOW+90002],[NOW+1,NOW]):
            def delayed(names,now,**kwargs):
                clock=iter(times)
                return self.actual_read(names,now,reader=self.store,clock=lambda:next(clock),**kwargs)
            with patch.object(monitor.signals,'get',side_effect=AssertionError),patch.object(monitor.signals,'rolling',side_effect=AssertionError) as rolling:
                (_,_,report),_,_=self.refresh(read=delayed)
            rolling.assert_not_called()
            self.assertEqual(set(report['failures']),{'BTC','ETH'})

    def test_priority_rotation_and_bybit_exclusion_keep_original_selected_set(self):
        states=[{'open_positions':[{'asset':f'A{i:03d}'} for i in range(100)]},
                {'open_positions':[{'asset':'ETH'},{'asset':'BTC'},{'asset':'ONLYBYBIT','execution_venue':'BYBIT_SPOT'}]}]
        original=copy.deepcopy(states);expected,next_cursor=monitor.management_selection(states,0,monitor.C['MONITOR_EVIDENCE_BATCH'])
        with patch.object(monitor.signals,'rolling',side_effect=AssertionError) as rolling:
            (_,_,report),reader,_=self.refresh(states=states)
        rolling.assert_not_called()  # unknown selected cache members do not shrink the batch
        self.assertEqual(states,original)
        self.assertEqual(report['attempted'],expected)
        self.assertEqual(report['evidence_refresh_cursor'],next_cursor)
        self.assertEqual(set(report['failures']),set(expected))
        names=reader.call_args.args[0]
        self.assertEqual(names,sorted(topic(a+'USDT',i) for a in set(expected)|{'BTC'} for i in ('15m','1h')))
        self.assertFalse(any('onlybybit' in name for name in names))


if __name__=='__main__':unittest.main()
