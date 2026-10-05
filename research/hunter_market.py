"""Venue registry for spot shadow market data. No order placement or private APIs.

Strategy code binds a market once, then consumes quotes/books/candles through
this interface. A failed venue never falls back to a similarly named asset.
"""
import datetime as dt
import json
import math
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

def request(url, body=None):
    raw=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(url,data=raw,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json","Content-Type":"application/json"})
    timeout=45 if "/early-klines" in url else 30 if "/orderbooks" in url else 5 if url.endswith("/health") else 10
    with urllib.request.urlopen(req,timeout=timeout) as response:return json.load(response)

def bybit_result(body):
    if not isinstance(body,dict) or body.get("retCode")!=0 or (body.get("result") or {}).get("category")!="spot":
        raise ValueError("VENUE_UPSTREAM_INVALID")
    return body["result"]

class Binance:
    name="binance"
    def __init__(self,fetch=None):
        self.fetch=fetch or request
        self.host=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
    def quotes(self):
        data=self.fetch(self.host+"/api/v3/ticker/24hr")
        if not isinstance(data,list):raise ValueError("BINANCE_TICKERS_INVALID")
        return normalize_quotes(data,self.name,"priceChangePercent",1)
    def books(self,pairs):
        books={};errors={}
        def one(pair):return self.fetch(self.host+"/api/v3/depth?"+urllib.parse.urlencode({"symbol":pair,"limit":100}))
        with ThreadPoolExecutor(max_workers=8) as executor:
            tasks={executor.submit(one,p):p for p in pairs}
            for job in as_completed(tasks):
                pair=tasks[job]
                try:books[pair]=validate_book(job.result(),pair,self.name)
                except Exception as exc:errors[pair]=type(exc).__name__+":"+str(exc)[:120]
        return books,errors
    def candles(self,pair,interval,limit,start=None,end=None):
        params={"symbol":pair,"interval":str(interval)+"m","limit":limit}
        if start is not None:params["startTime"]=start
        if end is not None:params["endTime"]=end
        return self.fetch(self.host+"/api/v3/klines?"+urllib.parse.urlencode(params))
    def signals(self,pairs,now):
        try:
            from research import hunter_early_signals as s
        except ModuleNotFoundError:import hunter_early_signals as s
        symbols=list(dict.fromkeys(list(pairs)+["BTCUSDT"]))
        r1=s.rolling(symbols,"1h");r4=s.rolling(symbols,"4h");micro=s.micro(symbols)
        return {p[:-4]:row for p in pairs if (row:=s.score_row(p,p[:-4],r1,r4,r1.get("BTCUSDT"),r4.get("BTCUSDT"),micro))}

class Bybit:
    name="bybit"
    def __init__(self,fetch=None):
        self.fetch=fetch or request
        self.host=os.getenv("HUNTER_BYBIT_SPOT_PROXY","https://bybit-api-test.qinx468.workers.dev").rstrip("/")
    def ready(self):
        try:
            doc=self.fetch(self.host+"/health")
            if not (doc.get("version",0)>=3 and set(("tickers","orderbooks","klines","early-klines")).issubset(doc.get("capabilities") or [])):return False
            books,failures=self.books(["BTCUSDT"])
            bars=self.candles("BTCUSDT",5,2)
            return not failures and "BTCUSDT" in books and len(bars)==2
        except Exception:return False
    def quotes(self):
        body=self.fetch(self.host+"/bybit/tickers")
        if abs(float(body.get("time",0))-dt.datetime.now(dt.timezone.utc).timestamp()*1000)>120000:
            raise ValueError("BYBIT_TICKERS_STALE")
        return normalize_quotes(bybit_result(body).get("list"),self.name,"price24hPcnt",100)
    def books(self,pairs):
        books={};errors={};pairs=list(dict.fromkeys(pairs))
        def one(batch):return bybit_result(self.fetch(self.host+"/bybit/orderbooks",{"symbols":batch}))
        batches=[pairs[i:i+20] for i in range(0,len(pairs),20)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks={executor.submit(one,b):b for b in batches}
            for job in as_completed(tasks):
                batch=tasks[job]
                try:
                    data=job.result()
                    if set(data.get("books") or {})-set(batch):raise ValueError("BYBIT_BOOK_SCOPE_INVALID")
                    for pair in batch:
                        try:
                            book=bybit_result((data.get("books") or {}).get(pair))
                            if book.get("s")!=pair:raise ValueError("BYBIT_BOOK_WRONG_SYMBOL")
                            if abs(float(book.get("ts",0))-dt.datetime.now(dt.timezone.utc).timestamp()*1000)>120000:raise ValueError("BYBIT_BOOK_STALE")
                            books[pair]=validate_book({"bids":book.get("b"),"asks":book.get("a")},pair,self.name)
                        except Exception as exc:errors[pair]=type(exc).__name__+":"+str(exc)[:120]
                except Exception as exc:errors.update({p:type(exc).__name__+":"+str(exc)[:120] for p in batch})
        return books,errors
    def candles(self,pair,interval,limit,start=None,end=None):
        params={"symbol":pair,"interval":interval,"limit":limit}
        if start is not None:params["start"]=start
        if end is not None:params["end"]=end
        data=bybit_result(self.fetch(self.host+"/bybit/klines?"+urllib.parse.urlencode(params)))
        if data.get("symbol")!=pair:raise ValueError("BYBIT_KLINE_WRONG_SYMBOL")
        rows=data.get("list") or [];step=int(interval)*60000
        converted=sorted([r[:6]+[int(r[0])+step-1,r[6]] for r in rows],key=lambda r:int(r[0]))
        stamps=[int(r[0]) for r in converted]
        if len(converted)>limit or any(b-a!=step for a,b in zip(stamps,stamps[1:])):raise ValueError("BYBIT_KLINE_GAPPED_OR_DUPLICATE")
        for row in converted:
            o,h,l,c,v,q=map(float,[*row[1:6],row[7]])
            if not all(math.isfinite(x) for x in (o,h,l,c,v,q)) or min(o,h,l,c)<=0 or min(v,q)<0 or l>min(o,c) or h<max(o,c):raise ValueError("BYBIT_KLINE_INVALID_OHLC")
        return converted
    def signals(self,pairs,now):
        try:
            from research.hunter_bybit_signal_capture import capture
            from research.hunter_bybit_worker import candles
        except ModuleNotFoundError:
            from hunter_bybit_signal_capture import capture
            from hunter_bybit_worker import candles
        symbols=sorted(set(pairs)|{"BTCUSDT"});end=int(now.timestamp()*1000);bundles={}
        batches=[symbols[i:i+20] for i in range(0,len(symbols),20)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(self.fetch,self.host+"/bybit/early-klines",{"symbols":b,"end":end}) for b in batches]
            for f in futures:
                data=bybit_result(f.result())
                if data.get("end")!=end:raise ValueError("SIGNAL_GENERATION_MISMATCH")
                bundles.update(data.get("klines") or {})
        def get(symbol,interval,limit):return candles(bundles.get(symbol,{}).get(interval),symbol,interval,limit,end)
        rows,failures=capture([{"base":p[:-4],"pair":p} for p in pairs],get)
        return rows

def normalize_quotes(rows,venue,change_key,factor):
    if not isinstance(rows,list):raise ValueError("VENUE_TICKERS_INVALID")
    out={}
    for row in rows:
        if not isinstance(row,dict):continue
        pair=str(row.get("symbol") or "")
        if not pair.endswith("USDT"):continue
        try:p=float(row["lastPrice"]);change=float(row[change_key])*factor
        except (KeyError,TypeError,ValueError):continue
        if math.isfinite(p) and p>0 and math.isfinite(change):
            out[pair]={"reference_price":p,"change_24h_pct":change,"venue":venue,"pair":pair}
    return out

def validate_book(book,pair,venue):
    for name,reverse in (("bids",True),("asks",False)):
        rows=book.get(name) or []
        if not rows:raise ValueError("EMPTY_BOOK")
        previous=None
        for row in rows:
            p,q=map(float,row[:2])
            if not math.isfinite(p+q) or p<=0 or q<0:raise ValueError("INVALID_BOOK_LEVEL")
            if previous is not None and ((reverse and p>=previous) or (not reverse and p<=previous)):raise ValueError("UNSORTED_BOOK")
            previous=p
    if float(book["bids"][0][0])>=float(book["asks"][0][0]):raise ValueError("CROSSED_BOOK")
    return {**book,"venue":venue,"pair":pair}

REGISTRY={"binance":Binance,"bybit":Bybit}
def adapter(venue,fetch=None):
    if venue not in REGISTRY:raise ValueError("UNSUPPORTED_MARKET_VENUE:"+str(venue))
    return REGISTRY[venue]() if fetch is None else REGISTRY[venue](fetch)

def binding(coin=None,position=None):
    if position is not None:
        # Unstamped history predates multi-venue execution and is Binance history.
        venue=position.get("execution_venue") or "binance"
        pair=position.get("execution_pair") or str(position.get("asset"))+"USDT"
    else:
        coin=coin or {};pairs=coin.get("pairs") or []
        venue="binance" if not coin.get("venues") or "binance" in coin["venues"] else coin["venues"][0]
        selected=[p for p in pairs if p.get("venue")==venue]
        pair=selected[0]["pair"] if selected else None
    if venue not in REGISTRY:raise ValueError("UNSUPPORTED_MARKET_VENUE:"+str(venue))
    return venue,pair

def mark(coin,position):
    venue,pair=binding(position=position)
    if pair!=position.get("asset")+"USDT":raise ValueError("POSITION_PAIR_MISMATCH")
    if coin.get("venue") and coin["venue"]!=venue:return None
    value=(coin.get("venue_prices") or {}).get(venue)
    if value is None and (not coin.get("venues") or venue in coin["venues"]):value=coin.get("reference_price")
    try:value=float(value)
    except (TypeError,ValueError):return None
    return value if math.isfinite(value) and value>0 else None

def shadow_supported(venue,scan):
    if venue=="binance":return True
    return venue in REGISTRY and (scan.get("venue_status") or {}).get(venue,{}).get("shadow_market_supported") is True
