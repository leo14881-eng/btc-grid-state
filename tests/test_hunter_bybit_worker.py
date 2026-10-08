import datetime as dt
import copy
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from research import hunter_bybit_worker as w

NOW=dt.datetime(2026,10,5,18,7,tzinfo=dt.timezone.utc)
END=int(NOW.timestamp()*1000)

def candle(symbol,interval,limit):
    step=int(interval)*60000
    return dict(retCode=0,result=dict(category="spot",symbol=symbol,list=[
        [str(END//step*step-(limit-1-i)*step),"10","11","9","10","1","10"] for i in range(limit)]))


def partial_fetch(failures):
    def fetch(path, body):
        if path == '/bybit/tickers':
            return dict(retCode=0, time=END, result=dict(category='spot', list=[
                dict(symbol=s, lastPrice='10', turnover24h='10', price24hPcnt='.1')
                for s in ('BTCUSDT', 'FLUIDUSDT')]))
        klines = {s: {i: candle(s, i, n) for i, n in (('60', 5), ('15', 25))}
                  for s in body['symbols']}
        for symbol, errors in failures.items():
            for interval in errors:
                klines.get(symbol, {}).pop(interval, None)
            if symbol in klines and not klines[symbol]:
                del klines[symbol]
        return dict(retCode=0, result=dict(category='spot', end=END,
                                         klines=klines, failures=failures))
    return fetch

class WorkerTests(unittest.TestCase):
    def setUp(self):
        # Fail closed if any test accidentally reaches real market data.
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'urllib.request.urlopen'):
            guard=patch(target,side_effect=AssertionError('NETWORK_FORBIDDEN_IN_TEST'))
            guard.start()
            self.addCleanup(guard.stop)

    def run_collect(self,bases=("BTC","FLUID","CARV"),fetcher=None,refresher=None):
        calls=[]
        def fetch(path,body=None):
            calls.append((path,body))
            if fetcher:return fetcher(path,body)
            if path=="/bybit/tickers":return dict(retCode=0,time=END,result=dict(category="spot",list=[
                dict(symbol=b+"USDT",lastPrice="10",turnover24h="100000",price24hPcnt=".1") for b in bases]))
            return dict(retCode=0,result=dict(category="spot",end=body["end"],klines={s:{i:candle(s,i,n) for i,n in (("60",5),("15",25))} for s in body["symbols"]}))
        with tempfile.TemporaryDirectory() as d,patch.object(w,"DEFAULT",pathlib.Path(d)/"snapshot.json"):
            refresher=refresher or (lambda:(list(bases),dict(cache_hit=False,network_requests=1,
                complete=True,captured_at_utc=NOW.isoformat())))
            rows,status=w.collect({"BTC","CARV"},NOW,fetch,
                lambda:(list(bases),{"cache_hit":True,"network_requests":0}),refresher)
            snapshot=json.loads(w.DEFAULT.read_text())
            w.validate(snapshot,NOW)
        return rows,status,snapshot,calls

    def test_bulk_tickers_and_only_bybit_only_candles_with_btc(self):
        rows,status,snapshot,calls=self.run_collect()
        self.assertEqual(len(rows),3)
        self.assertEqual([c[0] for c in calls],["/bybit/tickers","/bybit/early-klines"])
        self.assertEqual(set(calls[1][1]["symbols"]),{"BTCUSDT","FLUIDUSDT"})
        self.assertEqual(status["signal_expected_bases"],["FLUID"])
        self.assertTrue(status["signal_complete"])
        self.assertFalse(snapshot["early_signals"]["FLUID"]["execution_supported"])

    def test_stale_tickers_never_produce_snapshot(self):
        with self.assertRaisesRegex(ValueError,"STALE"):
            self.run_collect(fetcher=lambda p,b:dict(retCode=0,time=END-121000,result=dict(category="spot",list=[])))

    def test_missing_ticker_never_claims_full_coverage(self):
        with self.assertRaisesRegex(ValueError,"INCOMPLETE"):
            self.run_collect(fetcher=lambda p,b:dict(retCode=0,time=END,result=dict(category="spot",list=[])))

    def test_partial_kline_failure_keeps_listing_but_not_signal(self):
        def fetch(path,body):
            if path=="/bybit/tickers":return dict(retCode=0,time=END,result=dict(category="spot",list=[dict(symbol=s,lastPrice="10",turnover24h="10",price24hPcnt=".1") for s in ("BTCUSDT","FLUIDUSDT")]))
            return dict(retCode=0,result=dict(category="spot",end=END,klines={"BTCUSDT":{i:candle("BTCUSDT",i,n) for i,n in (("60",5),("15",25))}}))
        rows,status,snapshot,_=self.run_collect(("BTC","FLUID"),fetch)
        self.assertEqual(len(rows),2)
        self.assertFalse(status["signal_complete"])
        self.assertNotIn("FLUID",snapshot["early_signals"])
        self.assertIn("FLUIDUSDT",snapshot["signal_failures"])

    def test_bounded_batches(self):
        bases=["BTC"]+["ALT"+str(i) for i in range(45)]
        _,status,_,calls=self.run_collect(bases)
        self.assertEqual(status["kline_batch_requests"],3)
        self.assertTrue(all(len(body["symbols"])<=20 for path,body in calls if body))

    def test_failure_context_uses_request_order_across_multiple_batches(self):
        bases=['BTC']+['ALT'+str(i) for i in range(45)]
        def fetch(path,body):
            if body is None:
                return dict(retCode=0,time=END,result=dict(category='spot',list=[
                    dict(symbol=b+'USDT',lastPrice='10',turnover24h='100000',price24hPcnt='.1') for b in bases]))
            klines={s:{i:candle(s,i,n) for i,n in [('60',5),('15',25)]} for s in body['symbols']}
            failures={}
            if 'ALT30USDT' in klines:
                del klines['ALT30USDT']['15']
                failures={'ALT30USDT':{'15':'Error: BYBIT_HTTP_429'}}
            return dict(retCode=0,result=dict(category='spot',end=END,klines=klines,failures=failures))
        _,status,snapshot,_=self.run_collect(bases,fetch)
        detail=snapshot['signal_failure_details']['ALT30USDT']['15']
        self.assertEqual(detail['batch_id'],sorted(b+'USDT' for b in bases).index('ALT30USDT')//20)
        self.assertEqual(status['signal_failures'],1)
        self.assertNotIn('ALT30',snapshot['early_signals'])
        self.assertIn('ALT1',snapshot['early_signals'])

    def test_worker_failure_details_preserve_both_intervals_without_signals(self):
        errors = {'FLUIDUSDT': {'60': 'Error: BYBIT_HTTP_429', '15': 'Error: BYBIT_RET_10006'}}
        _, status, snapshot, _ = self.run_collect(('BTC', 'FLUID'), partial_fetch(errors))
        details = snapshot['signal_failure_details']['FLUIDUSDT']
        self.assertEqual(details['60']['code'], 'BYBIT_HTTP_429')
        self.assertEqual(details['15']['code'], 'BYBIT_RET_10006')
        self.assertFalse(details['60']['detail_redacted'])
        self.assertIn('FLUIDUSDT:60:BYBIT_HTTP_429', snapshot['signal_failures']['FLUIDUSDT'])
        self.assertEqual(snapshot['early_signals'], {})
        self.assertFalse(status['signal_complete'])
        self.assertEqual(status['signal_failures'], 1)

    def test_quarter_hour_failure_does_not_reuse_successful_hourly_signal(self):
        _, status, snapshot, _ = self.run_collect(('BTC', 'FLUID'), partial_fetch(
            {'FLUIDUSDT': {'15': 'Error: INCOMPLETE_KLINE'}}))
        self.assertIn('FLUIDUSDT:15:INCOMPLETE_KLINE', snapshot['signal_failures']['FLUIDUSDT'])
        self.assertNotIn('FLUID', snapshot['early_signals'])
        self.assertFalse(status['signal_complete'])

    def test_btc_failure_keeps_its_detail_and_blocks_all_signals(self):
        _, status, snapshot, _ = self.run_collect(('BTC', 'FLUID'), partial_fetch(
            {'BTCUSDT': {'15': 'AbortError: The operation was aborted.'}}))
        self.assertEqual(snapshot['signal_failure_details']['BTCUSDT']['15']['code'], 'WORKER_ABORTED')
        self.assertEqual(snapshot['early_signals'], {})
        self.assertFalse(status['signal_complete'])
        self.assertIn('BYBIT_BTC_BENCHMARK_UNAVAILABLE', snapshot['signal_failures']['FLUIDUSDT'])

    def test_unknown_worker_text_is_bounded_and_redacted(self):
        raw = 'TypeError: https://example.invalid/?token=DO_NOT_PERSIST\n' + 'x' * 2000
        _, _, snapshot, _ = self.run_collect(('BTC', 'FLUID'), partial_fetch(
            {'FLUIDUSDT': {'60': raw}}))
        detail = snapshot['signal_failure_details']['FLUIDUSDT']['60']
        self.assertEqual({k:detail[k] for k in ('code','detail_redacted','detail_truncated')},
                         dict(code='WORKER_KLINE_FAILURE', detail_redacted=True, detail_truncated=True))
        self.assertNotIn('DO_NOT_PERSIST', json.dumps(snapshot))
        self.assertNotIn('example.invalid', json.dumps(snapshot))
        self.assertLess(len(json.dumps(detail)), 350)

    def test_missing_reason_has_generation_batch_and_interval_context(self):
        fetch=partial_fetch({})
        def missing(path,body):
            response=fetch(path,body)
            if body:
                del response['result']['klines']['FLUIDUSDT']
            return response
        _,status,snapshot,_=self.run_collect(('BTC','FLUID'),missing)
        self.assertFalse(status['signal_complete'])
        for interval,detail in snapshot['signal_failure_details']['FLUIDUSDT'].items():
            self.assertEqual(detail['code'],'WORKER_MISSING_KLINE_REASON')
            self.assertEqual(detail['symbol'],'FLUIDUSDT')
            self.assertEqual(detail['interval'],interval)
            self.assertEqual(detail['generation_end_ms'],END)
            self.assertEqual(detail['batch_id'],0)
            self.assertEqual(detail['stage'],'candle_validation')
        self.assertEqual(set(snapshot['signal_failure_details']['FLUIDUSDT']),{'60','15'})

    def test_entire_batch_errors_are_classified_and_never_leak_text(self):
        cases=[(TimeoutError('SECRET_URL'),'WORKER_TIMEOUT'),
               (urllib.error.URLError(TimeoutError('SECRET_URL')),'WORKER_TIMEOUT'),
               (urllib.error.HTTPError('SECRET_URL',503,'SECRET_URL',{},None),'BYBIT_HTTP_503'),
               (RuntimeError('SECRET_URL'),'WORKER_KLINE_FAILURE')]
        for exc,code in cases:
            fetch=partial_fetch({})
            def broken(path,body):
                if body:raise exc
                return fetch(path,body)
            with self.subTest(code=code):
                _,status,snapshot,_=self.run_collect(('BTC','FLUID'),broken)
                self.assertEqual(snapshot['early_signals'],{})
                self.assertFalse(status['signal_complete'])
                self.assertNotIn('SECRET_URL',json.dumps(snapshot))
                for symbol in ('BTCUSDT','FLUIDUSDT'):
                    for detail in snapshot['signal_failure_details'][symbol].values():
                        self.assertEqual(detail['code'],code)
                        self.assertEqual(detail['stage'],'batch_request')

    def test_batch_retcode_preserved_without_message(self):
        fetch=partial_fetch({})
        def broken(path,body):
            return dict(retCode=10006,retMsg='SECRET_URL') if body else fetch(path,body)
        _,_,snapshot,_=self.run_collect(('BTC','FLUID'),broken)
        self.assertEqual(snapshot['signal_failure_details']['BTCUSDT']['60']['code'],'BYBIT_RET_10006')
        self.assertNotIn('SECRET_URL',json.dumps(snapshot))

    def test_invalid_second_interval_is_preserved_even_when_first_failed(self):
        fetch=partial_fetch({'FLUIDUSDT':{'60':'Error: BYBIT_HTTP_429'}})
        def broken(path,body):
            response=fetch(path,body)
            if body:response['result']['klines']['FLUIDUSDT']['15']['result']['symbol']='OTHERUSDT'
            return response
        _,_,snapshot,_=self.run_collect(('BTC','FLUID'),broken)
        details=snapshot['signal_failure_details']['FLUIDUSDT']
        self.assertEqual(details['60']['code'],'BYBIT_HTTP_429')
        self.assertEqual(details['15']['code'],'BYBIT_WORKER_KLINE_WRONG_SYMBOL')

    def test_rechecksummed_context_mismatch_is_rejected(self):
        _,_,original,_=self.run_collect(('BTC','FLUID'),partial_fetch(
            {'FLUIDUSDT':{'60':'Error: BYBIT_HTTP_429'}}))
        for key,value in [('batch_id',1),('batch_id',False),('symbol','BTCUSDT'),
                          ('interval','15'),('generation_end_ms',END-1),('stage','SECRET_URL')]:
            forged=copy.deepcopy(original)
            forged['signal_failure_details']['FLUIDUSDT']['60'][key]=value
            forged['snapshot_sha256']=w.digest({k:v for k,v in forged.items() if k!='snapshot_sha256'})
            with self.subTest(key=key,value=value),self.assertRaisesRegex(ValueError,'FAILURE_DETAIL_INVALID'):
                w.validate(forged,NOW)

    def test_failure_scope_interval_shape_and_conflict_are_rejected(self):
        valid = dict(category='spot', end=END, klines={'FLUIDUSDT': {'60': candle('FLUIDUSDT', '60', 5)}}, failures={})
        mutations = [
            dict(failures={'OTHERUSDT': {'15': 'Error: BYBIT_HTTP_429'}}),
            dict(failures={'FLUIDUSDT': {'5': 'Error: BYBIT_HTTP_429'}}),
            dict(failures={'FLUIDUSDT': {'60': 'Error: BYBIT_HTTP_429'}}),
            dict(failures={'FLUIDUSDT': {'15': None}}),
            dict(failures={'FLUIDUSDT': {'15': ''}}),
            dict(failures={'FLUIDUSDT': []}), dict(failures=None),
            dict(klines={'FLUIDUSDT': {'5': candle('FLUIDUSDT', '15', 25)}}),
            dict(end=END - 1),
        ]
        for update in mutations:
            with self.subTest(update=update), self.assertRaises(ValueError):
                w.batch_parts({**valid, **update}, ['FLUIDUSDT'], END)

    def test_success_failure_conflict_never_produces_signal(self):
        fetch = partial_fetch({})
        def conflict(path, body):
            response = fetch(path, body)
            if path == '/bybit/early-klines':
                response['result']['failures'] = {'FLUIDUSDT': {'60': 'Error: BYBIT_HTTP_429'}}
            return response
        _, status, snapshot, _ = self.run_collect(('BTC', 'FLUID'), conflict)
        self.assertEqual(snapshot['early_signals'], {})
        self.assertFalse(status['signal_complete'])

    def test_failure_details_are_checksum_bound_and_legacy_snapshots_still_validate(self):
        _, _, old, _ = self.run_collect()
        self.assertNotIn('signal_failure_details', old)
        w.validate(old, NOW)
        _, _, snapshot, _ = self.run_collect(('BTC', 'FLUID'), partial_fetch(
            {'FLUIDUSDT': {'60': 'Error: BYBIT_HTTP_429'}}))
        snapshot['signal_failure_details']['FLUIDUSDT']['60']['code'] = 'BYBIT_HTTP_503'
        with self.assertRaisesRegex(ValueError, 'CHECKSUM_MISMATCH'):
            w.validate(snapshot, NOW)
        del snapshot['signal_failure_details']
        snapshot['snapshot_sha256'] = w.digest({k: v for k, v in snapshot.items() if k != 'snapshot_sha256'})
        w.validate(snapshot, NOW)

    def test_rechecksummed_detail_cannot_forge_scope_or_success(self):
        _, _, good, _ = self.run_collect()
        _, _, failed, _ = self.run_collect(('BTC', 'FLUID'), partial_fetch(
            {'FLUIDUSDT': {'60': 'Error: BYBIT_HTTP_429'}}))
        variants = []
        forged = copy.deepcopy(failed)
        forged['early_signals'] = good['early_signals']
        variants.append(forged)
        forged = copy.deepcopy(failed)
        forged['venue_status']['signal_complete'] = True
        variants.append(forged)
        forged = copy.deepcopy(failed)
        forged['signal_failure_details']['OTHERUSDT'] = forged['signal_failure_details'].pop('FLUIDUSDT')
        variants.append(forged)
        forged = copy.deepcopy(failed)
        forged['signal_failure_details']['FLUIDUSDT']['5'] = forged['signal_failure_details']['FLUIDUSDT'].pop('60')
        variants.append(forged)
        forged = copy.deepcopy(failed)
        forged['signal_failure_details']['FLUIDUSDT']['60']['code'] = 'https://example.invalid/?token=secret'
        variants.append(forged)
        for snapshot in variants:
            snapshot['snapshot_sha256'] = w.digest({k: v for k, v in snapshot.items() if k != 'snapshot_sha256'})
            with self.subTest(snapshot=snapshot), self.assertRaisesRegex(ValueError, 'FAILURE_'):
                w.validate(snapshot, NOW)

    def test_candles_reject_wrong_symbol_stale_gap_and_bad_ohlc(self):
        for kind in ("symbol","stale","gap","price"):
            data=candle("FLUIDUSDT","15",25)
            if kind=="symbol":data["result"]["symbol"]="BTCUSDT"
            if kind=="stale":data["result"]["list"][-1][0]=str(END-6000000)
            if kind=="gap":data["result"]["list"][0][0]="0"
            if kind=="price":data["result"]["list"][0][2]="5"
            with self.subTest(kind=kind),self.assertRaises(ValueError):w.candles(data,"FLUIDUSDT","15",25,END)

    def test_snapshot_rejects_stale_future_and_tampering(self):
        _,_,snapshot,_=self.run_collect()
        for now in (NOW+dt.timedelta(seconds=901),NOW-dt.timedelta(seconds=1)):
            with self.assertRaises(ValueError):w.validate(snapshot,now)
        snapshot["rows"][0]["price"]=42
        with self.assertRaisesRegex(ValueError,"CHECKSUM"):w.validate(snapshot,NOW)

    def test_delisting_refreshes_full_universe_and_tickers_once(self):
        refresh=[]
        def listing():
            refresh.append(True)
            return ['BTC','CARV'],dict(cache_hit=False,network_requests=1,complete=True,
                                      captured_at_utc=NOW.isoformat())
        def fetch(path,body):
            if path=='/bybit/tickers':
                return dict(retCode=0,time=END,result=dict(category='spot',list=[
                    dict(symbol=b+'USDT',lastPrice='10',turnover24h='10',price24hPcnt='.1')
                    for b in ['BTC','CARV']]))
            return dict(retCode=0,result=dict(category='spot',end=END,klines={
                s:{i:candle(s,i,n) for i,n in [('60',5),('15',25)]} for s in body['symbols']}))
        rows,status,_,calls=self.run_collect(('BTC','CARV','AGI','SCOR'),fetch,listing)
        self.assertEqual(len(refresh),1)
        self.assertEqual(status['ticker_requests'],2)
        self.assertEqual({r['base'] for r in rows},{'BTC','CARV'})
        self.assertEqual(status['listing_refresh']['removed_bases'],['AGI','SCOR'])
        self.assertEqual(status['missing_or_invalid'],[])
        self.assertTrue(status['signal_complete'])

    def test_still_active_missing_ticker_remains_failure(self):
        calls=[]
        def listing():
            calls.append(True)
            return ['BTC','FLUID'],dict(cache_hit=False,network_requests=1,complete=True,
                                       captured_at_utc=NOW.isoformat())
        with self.assertRaisesRegex(ValueError,'TICKERS_INCOMPLETE'):
            self.run_collect(('BTC','FLUID'),lambda p,b:dict(retCode=0,time=END,
                result=dict(category='spot',list=[])),listing)
        self.assertEqual(len(calls),1)

    def test_new_listing_is_not_silently_dropped(self):
        def listing():
            return ['BTC','NEW'],dict(cache_hit=False,network_requests=1,complete=True,
                                     captured_at_utc=NOW.isoformat())
        with self.assertRaisesRegex(ValueError,'TICKERS_INCOMPLETE:NEWUSDT'):
            self.run_collect(('BTC','OLD'),lambda p,b:dict(retCode=0,time=END,
                result=dict(category='spot',list=[dict(symbol='BTCUSDT',lastPrice='10',
                turnover24h='10',price24hPcnt='.1')])),listing)

    def test_refresh_failure_never_becomes_coverage(self):
        def listing(): raise RuntimeError('UPSTREAM_UNAVAILABLE')
        with self.assertRaisesRegex(RuntimeError,'UPSTREAM_UNAVAILABLE'):
            self.run_collect(fetcher=lambda p,b:dict(retCode=0,time=END,
                result=dict(category='spot',list=[])),refresher=listing)

    def test_cached_partial_stale_refresh_is_rejected(self):
        for update in [dict(cache_hit=True),dict(complete=False),dict(network_requests=0),
                       dict(captured_at_utc=(NOW-dt.timedelta(seconds=121)).isoformat())]:
            meta=dict(cache_hit=False,network_requests=1,complete=True,captured_at_utc=NOW.isoformat())
            meta.update(update)
            with self.subTest(update=update),self.assertRaisesRegex(ValueError,'REFRESH_NOT_FRESH'):
                self.run_collect(fetcher=lambda p,b:dict(retCode=0,time=END,
                    result=dict(category='spot',list=[])),refresher=lambda:(['BTC'],meta))

    def test_complete_tickers_do_not_request_listing_refresh(self):
        def unexpected(): raise AssertionError('refresh not needed')
        self.run_collect(refresher=unexpected)

    def test_fresh_network_listing_missing_ticker_does_not_refresh(self):
        def unexpected():raise AssertionError('must not refresh a fresh list')
        with self.assertRaisesRegex(ValueError,'TICKERS_INCOMPLETE'):
            w.collect({'BTC'},NOW,lambda p,b=None:dict(retCode=0,time=END,
                result=dict(category='spot',list=[])),
                lambda:(['BTC'],dict(cache_hit=False,network_requests=1)),unexpected)

    def test_failed_second_ticker_preserves_previous_snapshot(self):
        for kind in ['missing','stale','duplicate','invalid']:
            count=[]
            def fetch(path,body=None):
                count.append(path)
                rows=[]
                if len(count)>1 and kind in ('duplicate','invalid'):
                    quote=dict(symbol='BTCUSDT',lastPrice='nan' if kind=='invalid' else '10',
                               turnover24h='10',price24hPcnt='.1')
                    rows=[quote,quote] if kind=='duplicate' else [quote]
                return dict(retCode=0,time=END-121000 if len(count)>1 and kind=='stale' else END,
                            result=dict(category='spot',list=rows))
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d,patch.object(w,'DEFAULT',pathlib.Path(d)/'snapshot.json'):
                w.DEFAULT.write_bytes(b'previous verified snapshot')
                with self.assertRaises(ValueError):
                    w.collect({'BTC'},NOW,fetch,lambda:(['BTC'],dict(cache_hit=True)),
                        lambda:(['BTC'],dict(cache_hit=False,network_requests=1,complete=True,
                                            captured_at_utc=NOW.isoformat())))
                self.assertEqual(w.DEFAULT.read_bytes(),b'previous verified snapshot')
                self.assertEqual(count,['/bybit/tickers','/bybit/tickers'])

    def test_new_listing_with_complete_quotes_keeps_early_signal_requirement(self):
        def fetch(path,body):
            if path=='/bybit/tickers':
                return dict(retCode=0,time=END,result=dict(category='spot',list=[
                    dict(symbol=b+'USDT',lastPrice='10',turnover24h='10',price24hPcnt='.1')
                    for b in ['BTC','FLUID']]))
            return dict(retCode=0,result=dict(category='spot',end=END,klines={
                s:{i:candle(s,i,n) for i,n in [('60',5),('15',25)]} for s in body['symbols']}))
        rows,status,_,calls=self.run_collect(('BTC','OLD'),fetch,
            lambda:(['BTC','FLUID'],dict(cache_hit=False,network_requests=1,complete=True,
                                       captured_at_utc=NOW.isoformat())))
        self.assertEqual(status['signal_expected_bases'],['FLUID'])
        self.assertTrue(status['signal_complete'])
        self.assertEqual(status['listing_refresh']['added_bases'],['FLUID'])
        self.assertEqual(set(calls[-1][1]['symbols']),{'BTCUSDT','FLUIDUSDT'})
