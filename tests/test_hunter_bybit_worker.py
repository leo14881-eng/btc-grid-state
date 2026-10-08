import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from research import hunter_bybit_worker as w

NOW=dt.datetime(2026,10,5,18,7,tzinfo=dt.timezone.utc)
END=int(NOW.timestamp()*1000)

def candle(symbol,interval,limit):
    step=int(interval)*60000
    return dict(retCode=0,result=dict(category="spot",symbol=symbol,list=[
        [str(END//step*step-(limit-1-i)*step),"10","11","9","10","1","10"] for i in range(limit)]))

class WorkerTests(unittest.TestCase):
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
