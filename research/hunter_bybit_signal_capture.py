"""Capture Bybit spot EARLY inputs on an authorized regional collector.

Only market evidence is returned. A missing candle is never inferred from a
24-hour ticker or from another venue's similarly named asset.
"""
import json
import datetime as dt
import math
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed

try:
 from research.hunter_early_signals import score_row
 from research.hunter_signal_provenance import capture as capture_source
except ModuleNotFoundError:
 from hunter_early_signals import score_row
 from hunter_signal_provenance import capture as capture_source

HOST="https://api.bytick.com"

def get(symbol,interval,limit):
    url=HOST+"/v5/market/kline?"+urllib.parse.urlencode({
        "category":"spot","symbol":symbol,"interval":interval,"limit":limit})
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-regional-early/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=12) as response:body=json.load(response)
    if body.get("retCode")!=0:raise ValueError("BYBIT_KLINE_ERROR:"+str(body.get("retMsg"))[:80])
    result=body.get("result") or {}
    if result.get('category')!='spot' or result.get('symbol')!=symbol:
        raise ValueError('BYBIT_KLINE_SOURCE_IDENTITY_MISMATCH')
    rows=result.get("list") or []
    if len(rows)<limit:raise ValueError("BYBIT_KLINE_INSUFFICIENT")
    rows=sorted(rows,key=lambda x:int(x[0]))[-limit:]
    if len({int(x[0]) for x in rows})!=limit:raise ValueError("BYBIT_KLINE_DUPLICATE")
    for row in rows:
        if len(row)<7 or any(not math.isfinite(float(x)) for x in row[:7]):
            raise ValueError("BYBIT_KLINE_INVALID")
    return rows

def features(hourly,quarter,symbol=None,observed_at=None):
    start=float(hourly[0][1]);close=float(hourly[-1][4]);one=float(hourly[-2][1])
    if min(start,close,one)<=0:raise ValueError("BYBIT_KLINE_NONPOSITIVE")
    r1={"return_pct":(close/one-1)*100,"quote_volume":sum(float(x[6]) for x in hourly[-2:])}
    r4={"return_pct":(close/start-1)*100,"quote_volume":sum(float(x[6]) for x in hourly)}
    highs=[float(x[2]) for x in quarter];lows=[float(x[3]) for x in quarter]
    closes=[float(x[4]) for x in quarter];vols=[float(x[6]) for x in quarter]
    if min(highs+lows+closes)<=0 or min(vols)<0:raise ValueError("BYBIT_KLINE_INVALID_PRICE_OR_VOLUME")
    def width(hi,lo):
        top=max(hi);bottom=min(lo);return (top-bottom)/((top+bottom)/2)*100
    recent=width(highs[-8:],lows[-8:]);prior=width(highs[-24:-8],lows[-24:-8])
    baseline=sum(vols[-14:-2])/12
    lo=min(lows[-24:]);hi=max(highs[-24:])
    micro={"volume_acceleration":(sum(vols[-2:])/2)/baseline if baseline>0 else 0,
           "compression_ratio":prior/max(recent,.05),
           "return_15m_pct":(closes[-1]/closes[-2]-1)*100,
           "range_position":(closes[-1]-lo)/(hi-lo) if hi>lo else .5}
    if symbol and observed_at:
        r1['source_provenance']=capture_source(hourly[-2:],symbol,'1h',observed_at,'bybit')
        r4['source_provenance']=capture_source(hourly,symbol,'1h',observed_at,'bybit')
        micro['source_provenance']=capture_source(quarter,symbol,'15m',observed_at,'bybit')
    return r1,r4,micro

def capture(rows,fetcher=get,observed_at=None):
    symbols={row["pair"]:row["base"] for row in rows}
    symbols["BTCUSDT"]="BTC"
    observed={};failures={}
    def one(symbol):
        hourly,quarter=fetcher(symbol,"60",5),fetcher(symbol,"15",25)
        return features(hourly,quarter,symbol,observed_at or dt.datetime.now(dt.timezone.utc))
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures={executor.submit(one,sym):sym for sym in sorted(symbols)}
        for future in as_completed(futures):
            sym=futures[future]
            try:observed[sym]=future.result()
            except Exception as exc:failures[sym]=type(exc).__name__+":"+str(exc)[:100]
    if "BTCUSDT" not in observed:raise RuntimeError("BYBIT_BTC_BENCHMARK_UNAVAILABLE")
    btc1,btc4,btc_micro=observed["BTCUSDT"]
    signals={}
    for symbol,base in symbols.items():
        if symbol=="BTCUSDT" or symbol not in observed:continue
        r1,r4,micro=observed[symbol]
        row=score_row(symbol,base,{symbol:r1},{symbol:r4},btc1,btc4,{symbol:micro,"BTCUSDT":btc_micro})
        if row:signals[base]={**row,"source_venue":"bybit","execution_supported":False}
    return signals,failures
