#!/usr/bin/env python3
"""Independent Stock Shadow V3 forward-sample run. Broad paper sampling only; never places orders."""
import json, math, os, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta, time as dtime
from zoneinfo import ZoneInfo
try:
    from .market_session import session_allows_trade
except ImportError:
    try:
        from market_session import session_allows_trade
    except ImportError:
        from research.stock_shadow.market_session import session_allows_trade
from pathlib import Path

try:
    from .state_safety import number, optional_number, validate_inputs, portfolio_statistics, run_with_health
except ImportError:
    try:
        from state_safety import number, optional_number, validate_inputs, portfolio_statistics, run_with_health
    except ImportError:
        from research.stock_shadow.state_safety import number, optional_number, validate_inputs, portfolio_statistics, run_with_health

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; EVENTS=ROOT/"trades-v1.json"; SUMMARY=ROOT/"summary-v1.json"
MARKET_CACHE=ROOT/"market-daily-cache-v1.json"
CALENDAR_CACHE=ROOT/"calendar-session-cache-v1.json"
NOTIONAL=1000.0; MAX_TRANCHES=5
LOW_PRICE_REFERENCE=2.0; MIN_DOLLAR_VOLUME=10_000_000.0; LOW_PRICE_MIN_DOLLAR_VOLUME=25_000_000.0; MIN_SCORE=68.0
MAX_5D_RETURN=18.0; MAX_20D_RETURN=45.0; MAX_SMA20_EXTENSION=18.0; MIN_20D_RETURN=-8.0
PARABOLIC_5D_RETURN=35.0; PARABOLIC_20D_RETURN=80.0; PARABOLIC_SMA20_EXTENSION=30.0
EARLY_MIN_SCORE=62.0; EARLY_MAX_20D_RETURN=18.0; EARLY_MAX_SMA20_EXTENSION=8.0
FEE_RATE=0.002          # conservative xStock spot-side research assumption; stored explicitly
ARM_NET_PCT=1.0
PROFIT_FLOOR_NET_PCT=0.10
# Swing book: once a trade has meaningful net profit, protect a positive net exit.
# Allowed giveback shrinks as MFE grows; all values are AFTER buy/sell fees.
PROFIT_GIVEBACK_BANDS=((30.0,0.20),(15.0,0.25),(8.0,0.35),(1.0,0.50))
SOURCE_COMMIT=os.getenv("STOCK_SHADOW_SOURCE_COMMIT","LOCAL")
RUN_ID=os.getenv("STOCK_SHADOW_RUN_ID","LOCAL")

def now(): return datetime.now(timezone.utc).isoformat()
def _alpaca_exchange_session(ts=None):
    """Resolve the authoritative Alpaca session, reusing only a same-day persisted cache."""
    t=(ts or datetime.now(timezone.utc)).astimezone(NY); day=t.date().isoformat()
    current_day=datetime.now(timezone.utc).astimezone(NY).date().isoformat()
    cacheable=(day==current_day)
    cached=load(CALENDAR_CACHE,{}) if cacheable else {}
    if cached.get("date")==day:
        if cached.get("closed") is True:
            API_USAGE["alpaca_calendar"]["cache_hits"]+=1
            return None
        if cached.get("open") and cached.get("close"):
            try:
                oh,om=map(int,cached["open"].split(":")); ch,cm=map(int,cached["close"].split(":"))
                API_USAGE["alpaca_calendar"]["cache_hits"]+=1
                return {"date":day,"open":dtime(oh,om),"close":dtime(ch,cm),"source":"ALPACA_EXCHANGE_CALENDAR_CACHE"}
            except Exception: pass
    API_USAGE["alpaca_calendar"]["cache_misses"]+=1
    key=os.getenv("APCA_API_KEY_ID"); secret=os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        API_USAGE["alpaca_calendar"]["errors"]+=1; return None
    url="https://paper-api.alpaca.markets/v2/calendar?"+urllib.parse.urlencode({"start":day,"end":day})
    req=urllib.request.Request(url,headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"Accept":"application/json"})
    try:
        API_USAGE["alpaca_calendar"]["http_requests"]+=1
        with urllib.request.urlopen(req,timeout=15) as r: rows=json.load(r)
        if not rows:
            if cacheable: save(CALENDAR_CACHE,{"date":day,"closed":True,"verified_at":now(),"source":"ALPACA_EXCHANGE_CALENDAR"})
            return None
        row=rows[0]; oh,om=map(int,row["open"].split(":")); ch,cm=map(int,row["close"].split(":"))
        if cacheable: save(CALENDAR_CACHE,{"date":day,"open":row["open"],"close":row["close"],"verified_at":now(),"source":"ALPACA_EXCHANGE_CALENDAR"})
        return {"date":day,"open":dtime(oh,om),"close":dtime(ch,cm),"source":"ALPACA_EXCHANGE_CALENDAR"}
    except Exception:
        API_USAGE["alpaca_calendar"]["errors"]+=1; return None

def trade_action_window(ts=None, session=None):
    ts=ts or datetime.now(timezone.utc)
    s=session if session is not None else _alpaca_exchange_session(ts)
    return session_allows_trade(ts,s)
def get_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 stock-shadow-research","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=25) as r: return json.load(r)

def first_json(urls):
    last=None
    for url in urls:
        try: return get_json(url), url
        except Exception as e: last=e
    raise last
def load(p,d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except Exception:
        if p in (STATE, EVENTS): raise
        return d
def save(p,o):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+".tmp")
    tmp.write_text(json.dumps(o,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
    tmp.replace(p)

def continuity_fingerprint(state, events):
    """Immutable identity of the forward cohort; ignores quotes/P&L/state fields."""
    positions=state.get("positions",{})
    cohort=[]
    for symbol,p in sorted(positions.items()):
        tranches=tuple((str(t.get("at","")), round(float(t.get("price",0)),8), round(float(t.get("notional",0)),8), str(t.get("reason",""))) for t in p.get("tranches",[]))
        cohort.append((symbol,str(p.get("opened_at","")),tranches))
    trade_ids=tuple((str(e.get("type","")),str(e.get("symbol","")),str(e.get("at","")),round(float(e.get("price",0)),8),round(float(e.get("notional",0) or 0),8),str(e.get("reason",""))) for e in events)
    closed_ids=tuple((str(x.get("symbol","")),str(x.get("closed_at","")),str(x.get("exit_reason",""))) for x in state.get("closed",[]))
    return (tuple(cohort),trade_ids,closed_ids)

def validate_ledger(state, events):
    positions=state.get("positions",{})
    # Persisted trade events must never originate on a New York weekend. This is a
    # second-line ledger invariant behind the authoritative exchange-calendar gate:
    # even a stale workflow checkout cannot resurrect the archived pre-gate weekend cohort.
    for event in events:
        if event.get("type") in {"BUY","ADD","SELL"} and event.get("at"):
            try:
                event_ny=datetime.fromisoformat(str(event["at"]).replace("Z","+00:00")).astimezone(NY)
            except Exception as exc:
                raise RuntimeError("ledger_invariant:invalid_trade_event_timestamp") from exc
            if event_ny.weekday() >= 5:
                raise RuntimeError("ledger_invariant:weekend_trade_event")
    # State continuity is a hard invariant: a reset marker is never a valid runtime state.
    if state.get("reset_reason") or state.get("reset_at"):
        raise RuntimeError("ledger_invariant:manual_reset_marker_present")
    if any(len((p or {}).get("tranches",[]))>MAX_TRANCHES for p in positions.values()):
        raise RuntimeError("ledger_invariant:max_tranches_exceeded")
    if any(float(x.get("realized_net_pnl_usdt",0))<=0 for x in state.get("closed",[])):
        raise RuntimeError("ledger_invariant:losing_sell_present")
    if any(x.get("type")=="SELL" and float(x.get("net_pnl_usdt",0))<=0 for x in events):
        raise RuntimeError("ledger_invariant:nonpositive_sell_event")
    return True

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EXCLUDED_NAME_MARKERS = (" ETF", " ETN", " WARRANT", " WTS", " UNIT", " RIGHT", " PREFERRED", " PFD", " DEPOSITARY", " DEPOSITORY")
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
MAX_REQUEST_TARGET_CHARS = 7000
DAILY_CACHE_KEEP_BARS = 24
API_USAGE={
    "alpaca_daily_bars":{"http_requests":0,"pages":0,"logical_batches":0},
    "alpaca_calendar":{"http_requests":0,"cache_hits":0,"cache_misses":0,"errors":0},
    "history_gap_recovery":{"http_requests":0,"logical_batches":0,"recovered":0,"deep_window_days":0},
}
NY=ZoneInfo("America/New_York")

def get_text(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 stock-shadow-research","Accept":"text/plain,*/*"})
    with urllib.request.urlopen(req,timeout=25) as r: return r.read().decode("utf-8","replace")

def _plain_common_stock(name):
    u=(" "+name.upper()+" ")
    return not any(marker in u for marker in EXCLUDED_NAME_MARKERS)

def discover_us_common_stocks():
    """Discover US-listed common stocks from Nasdaq Trader symbol directories.

    Includes Nasdaq plus NYSE/NYSE American and other US exchange listings.
    Excludes ETFs/test issues and obvious non-common-stock security types.
    """
    symbols=set(); source_errors=[]
    try:
        lines=get_text(NASDAQ_LISTED).splitlines()
        header=lines[0].split("|")
        for line in lines[1:]:
            if not line or line.startswith("File Creation Time"): continue
            row=dict(zip(header,line.split("|")))
            symbol=row.get("Symbol","").strip()
            name=row.get("Security Name","").strip()
            if symbol and "$" not in symbol and row.get("Test Issue")=="N" and row.get("ETF")=="N" and _plain_common_stock(name):
                symbols.add(symbol)
    except Exception as e:
        source_errors.append({"source":"NASDAQ_LISTED","error":type(e).__name__})
    try:
        lines=get_text(OTHER_LISTED).splitlines()
        header=lines[0].split("|")
        for line in lines[1:]:
            if not line or line.startswith("File Creation Time"): continue
            row=dict(zip(header,line.split("|")))
            symbol=(row.get("ACT Symbol") or row.get("NASDAQ Symbol") or "").strip()
            name=row.get("Security Name","").strip()
            if symbol and "$" not in symbol and row.get("Test Issue")=="N" and row.get("ETF")=="N" and _plain_common_stock(name):
                symbols.add(symbol)
    except Exception as e:
        source_errors.append({"source":"OTHER_LISTED","error":type(e).__name__})
    # Yahoo uses '-' for class shares; Nasdaq directories commonly use '.'.
    return sorted(s.replace(".","-") for s in symbols), source_errors

def _snapshot_from_bars(symbol, bars, source):
    rows=[b for b in bars if b.get("c") is not None and float(b["c"])>0]
    if len(rows)<22: raise ValueError("insufficient_history")
    prices=[float(b["c"]) for b in rows]; vols=[float(b.get("v") or 0) for b in rows]
    highs=[float(b["h"]) for b in rows if b.get("h") is not None]
    lows=[float(b["l"]) for b in rows if b.get("l") is not None]
    price=prices[-1]; ret5=(price/prices[-6]-1)*100; ret20=(price/prices[-21]-1)*100
    sma20=sum(prices[-20:])/20
    avg_dollar_volume=sum(p*v for p,v in zip(prices[-20:],vols[-20:]))/20
    vol20=(sum(((prices[i]/prices[i-1]-1)*100)**2 for i in range(len(prices)-19,len(prices)))/19)**0.5
    avg_volume20=sum(vols[-20:])/20
    volume_ratio=(vols[-1]/avg_volume20) if avg_volume20 else 0.0
    high20=max(highs[-20:]) if highs else max(prices[-20:])
    low20=min(lows[-20:]) if lows else min(prices[-20:])
    range_pos20=((price-low20)/(high20-low20)) if high20>low20 else 0.5
    return {"base":symbol,"price":price,"ret5":ret5,"ret20":ret20,"sma20":sma20,
            "avg_dollar_volume20":avg_dollar_volume,"daily_volatility20":vol20,
            "volume_ratio20":volume_ratio,"high20":high20,"low20":low20,"range_position20":range_pos20,
            "status":"OBSERVED","source":source,"observed_at":now()}

def _stock_snapshot(symbol):
    # Compatibility/fallback helper for benchmarks only. Full universe uses Alpaca batch bars.
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d"
    d=get_json(url); r=d["chart"]["result"][0]; q=r["indicators"]["quote"][0]
    bars=[{"c":p,"v":v,"h":h,"l":l} for p,v,h,l in zip(q["close"],q["volume"],q.get("high",[]),q.get("low",[])) if p is not None]
    return _snapshot_from_bars(symbol,bars,"FREE_PUBLIC_CHART_1D")

def _pack_alpaca_symbol_batches(symbols, timeframe, start, end):
    """Pack the largest practical GET batches by encoded request-target size, not an arbitrary symbol count."""
    batches=[]; batch=[]
    for sym in symbols:
        candidate=batch+[sym]
        params={"symbols":",".join(s.replace("-",".") for s in candidate),"timeframe":timeframe,
                "start":start.isoformat().replace("+00:00","Z"),"end":end.isoformat().replace("+00:00","Z"),
                "limit":10000,"feed":"sip","adjustment":"all"}
        target="/v2/stocks/bars?"+urllib.parse.urlencode(params)
        if batch and len(target)>MAX_REQUEST_TARGET_CHARS:
            batches.append(batch); batch=[sym]
        else:
            batch=candidate
    if batch: batches.append(batch)
    return batches

def _alpaca_batch_bars(symbols, start=None, end=None):
    key=os.getenv("APCA_API_KEY_ID"); secret=os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret: raise RuntimeError("missing_alpaca_secrets")
    end=end or (datetime.now(timezone.utc)-timedelta(minutes=20))
    start=start or (end-timedelta(days=45))
    api_symbols=[s.replace("-",".") for s in symbols]
    params={"symbols":",".join(api_symbols),"timeframe":"1Day","start":start.isoformat().replace("+00:00","Z"),
            "end":end.isoformat().replace("+00:00","Z"),"limit":10000,"feed":"sip","adjustment":"all"}
    headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"User-Agent":"stock-shadow/3.0"}
    merged={}; token=None; API_USAGE["alpaca_daily_bars"]["logical_batches"]+=1
    while True:
        if token: params["page_token"]=token
        else: params.pop("page_token",None)
        url=ALPACA_BARS_URL+"?"+urllib.parse.urlencode(params)
        req=urllib.request.Request(url,headers=headers)
        API_USAGE["alpaca_daily_bars"]["http_requests"]+=1
        with urllib.request.urlopen(req,timeout=45) as resp: body=json.load(resp)
        API_USAGE["alpaca_daily_bars"]["pages"]+=1
        for sym, rows in (body.get("bars") or {}).items(): merged.setdefault(sym.replace(".","-"),[]).extend(rows)
        token=body.get("next_page_token")
        if not token: break
    return merged

def score_candidate(m, spy=None, qqq=None):
    reasons=[]; rejects=[]
    price=m["price"]; dv=m["avg_dollar_volume20"]; r5=m["ret5"]; r20=m["ret20"]
    # V2 tradeability: price alone is not a rejection. Low-priced names must prove stronger liquidity.
    if price < LOW_PRICE_REFERENCE and dv < LOW_PRICE_MIN_DOLLAR_VOLUME: rejects.append("LOW_PRICE_INSUFFICIENT_LIQUIDITY")
    if dv < MIN_DOLLAR_VOLUME: rejects.append("LOW_DOLLAR_VOLUME")
    if r5 > PARABOLIC_5D_RETURN: rejects.append("PARABOLIC_5D")
    if r20 > PARABOLIC_20D_RETURN: rejects.append("PARABOLIC_20D")
    if r20 < MIN_20D_RETURN: rejects.append("WEAK_20D_TREND")
    if price < m["sma20"]*0.94: rejects.append("BELOW_TREND")
    score=0.0
    # Liquidity 0..15
    score += min(15.0, max(0.0, 5.0 + math.log10(max(dv,1)/MIN_DOLLAR_VOLUME)*5.0))
    # Trend 0..20 and momentum 0..20; reward strength without rewarding extreme chase.
    trend=max(0.0,min(20.0,10.0+r20*0.45)); momentum=max(0.0,min(20.0,10.0+r5*0.8))
    score += trend + momentum
    # Relative strength to SPY/QQQ, 0..25.
    spy20=spy["ret20"] if spy else 0.0; qqq20=qqq["ret20"] if qqq else 0.0
    rel=((r20-spy20)+(r20-qqq20))/2
    score += max(0.0,min(25.0,12.5+rel*0.8))
    # Price vs SMA20 structure, 0..10.
    dist=(price/m["sma20"]-1)*100
    if dist > PARABOLIC_SMA20_EXTENSION: rejects.append("PARABOLIC_SMA20_EXTENSION")
    score += max(0.0,min(10.0,7.0+dist*0.35))
    # Volatility quality, 0..10: enough movement, but penalize extreme noise.
    v=m["daily_volatility20"]
    score += 10.0 if 1.0<=v<=4.5 else (6.0 if v<=7.0 else 1.0)
    if r5>=0 and r20>0: reasons.append("POSITIVE_MOMENTUM")
    if rel>0: reasons.append("OUTPERFORMS_SPY_QQQ")
    if price>=m["sma20"]: reasons.append("ABOVE_SMA20")
    if dv>=MIN_DOLLAR_VOLUME: reasons.append("LIQUID")
    return round(score,2), reasons, rejects, {"relative20":round(rel,4),"distance_sma20_pct":round(dist,4)}

def entry_decision(m, spy=None, qqq=None):
    """One engine, multiple entry structures: early anomaly + right-side trend + healthy pullback."""
    score,reasons,rejects,metrics=score_candidate(m,spy,qqq)
    r5=m["ret5"]; r20=m["ret20"]; price=m["price"]; sma=m["sma20"]
    dist=(price/sma-1)*100
    vr=m.get("volume_ratio20",1.0); rp=m.get("range_position20",0.5)
    spy20=spy["ret20"] if spy else 0.0; qqq20=qqq["ret20"] if qqq else 0.0
    rel=((r20-spy20)+(r20-qqq20))/2
    metrics.update({"volume_ratio20":round(vr,4),"range_position20":round(rp,4)})
    structure="NONE"
    # Left-side / early anomaly: not extended, improving relative strength, abnormal participation,
    # and already stabilised around/above trend. It does NOT require a breakout.
    early=(not rejects and score>=EARLY_MIN_SCORE and -4.0<=r5<=8.0 and
           -3.0<=r20<=EARLY_MAX_20D_RETURN and -3.0<=dist<=EARLY_MAX_SMA20_EXTENSION and
           vr>=1.35 and rel>=0 and rp>=0.45)
    # Right-side trend: confirmed strength, but still within a non-parabolic chase envelope.
    momentum=(not rejects and score>=MIN_SCORE and 0<=r5<=MAX_5D_RETURN and
              r20<=MAX_20D_RETURN and dist<=MAX_SMA20_EXTENSION)
    # Healthy pullback inside an established trend.
    pullback=(not rejects and score>=MIN_SCORE and -3.0<r5<0 and r20>0 and
              -3.0<=dist<=12.0 and rel>0)
    if early: structure="EARLY_ACCUMULATION"
    elif pullback: structure="PULLBACK_IN_TREND"
    elif momentum: structure="MOMENTUM_TREND"
    ready=structure!="NONE"
    if early: reasons.append("EARLY_VOLUME_ANOMALY")
    if ready and structure!="EARLY_ACCUMULATION" and r5>MAX_5D_RETURN*0.75: reasons.append("LATE_STAGE_CAUTION")
    return {"ready":ready,"score":score,"reasons":reasons,"rejects":rejects,"metrics":metrics,"entry_structure":structure}


def profit_floor_net_pct(mfe):
    """Dynamic positive net-profit floor. Returns None until protection is armed."""
    if mfe < ARM_NET_PCT: return None
    for threshold, giveback_fraction in PROFIT_GIVEBACK_BANDS:
        if mfe >= threshold:
            return max(PROFIT_FLOOR_NET_PCT, mfe*(1.0-giveback_fraction))
    return None

def recovery_add_signal(p, m, ps):
    """ADD only after a real pullback has happened and the position is improving again."""
    price=number(m.get("price"),"recovery.price",positive=True)
    high=max(number(p.get("swing_high_price"),"swing_high_price",default=price,positive=True),price)
    p["swing_high_price"]=high
    dd=(price/high-1.0)*100 if high else 0.0
    if dd < -1.0:
        low=min(number(p.get("pullback_low_price"),"pullback_low_price",default=price,positive=True),price)
        p["pullback_low_price"]=low
        p["pullback_seen"]=True
    low=optional_number(p.get("pullback_low_price"),"pullback_low_price",positive=True)
    recovery=((price/low-1.0)*100) if low else 0.0
    prev_rel=(p.get("position_state_v2") or {}).get("market_relative20")
    prev_rel=optional_number(prev_rel,"previous.market_relative20")
    current_rel=optional_number(ps.get("market_relative20"),"current.market_relative20")
    rel_improving=prev_rel is not None and current_rel is not None and current_rel > prev_rel
    price_improving=bool(low and price > low)
    eligible=bool(p.get("pullback_seen") and ps["state"]!="BROKEN" and price_improving and rel_improving)
    return {"eligible":eligible,"drawdown_from_swing_high_pct":round(dd,4),
            "recovery_from_pullback_low_pct":round(recovery,4),"relative_improving":rel_improving,
            "price_improving":price_improving}

def position_state_v2(m, spy=None, qqq=None):
    """Small, explainable state engine. No single indicator can mark a position BROKEN."""
    price=m["price"]; sma=m["sma20"]; r5=m["ret5"]; r20=m["ret20"]
    spy20=spy["ret20"] if spy else 0.0; qqq20=qqq["ret20"] if qqq else 0.0
    relative20=((r20-spy20)+(r20-qqq20))/2
    dist=(price/sma-1)*100
    vr=m.get("volume_ratio20",1.0)
    trend_broken=(dist < -6.0 and r20 < 0)
    relative_weak=(relative20 < -5.0)
    selling_pressure=(r5 < -5.0 and vr >= 1.5)
    trend_strong=(dist >= 0 and r20 > 0)
    relative_strong=(relative20 >= 0)
    # BROKEN requires independent confirmation: structure + relative weakness.
    if trend_broken and relative_weak:
        state="BROKEN"
    elif r5 < 0 and not trend_broken and not relative_weak:
        state="HEALTHY_PULLBACK"
    elif trend_strong and relative_strong:
        state="STRONG"
    else:
        state="UNCERTAIN"
    return {"state":state,"market_relative20":round(relative20,4),"distance_sma20_pct":round(dist,4),
            "selling_pressure":selling_pressure,"trend_broken":trend_broken,"relative_weak":relative_weak,
            "sector_relative_status":"UNAVAILABLE_V2_BASELINE"}

def position_state_v3(p, market_state, net_return_pct):
    """Position-relative state; market strength and our trade outcome are separate dimensions."""
    mae=number(p.get("mae_net_pct"),"mae_net_pct",default=net_return_pct)
    if market_state.get("state")=="BROKEN":
        state="BROKEN"
    elif net_return_pct <= -8.0 or mae <= -10.0:
        state="DETERIORATING"
    elif net_return_pct <= -3.0:
        state="UNDERWATER"
    elif net_return_pct > 0:
        state="PROFITABLE"
    else:
        state="HEALTHY_PULLBACK"
    return {"state":state,"net_return_pct":round(net_return_pct,6),"mae_net_pct":round(mae,6),
            "market_state":market_state.get("state")}

def stock_universe():
    """Full US common-stock universe backed by a persisted daily-bar cache plus incremental refresh."""
    symbols, discovery_errors=discover_us_common_stocks()
    requested=list(dict.fromkeys(symbols+["SPY","QQQ"]))
    cache=load(MARKET_CACHE,{"bars":{}})
    cached=cache.get("bars") or {}
    end=datetime.now(timezone.utc)-timedelta(minutes=20)
    covered=sum(1 for s in symbols if len(cached.get(s) or [])>=22)
    bootstrap=(covered < max(1,int(len(symbols)*0.90)))
    start=end-timedelta(days=45 if bootstrap else 7)
    fetched={}; failed_batches=[]
    for batch in _pack_alpaca_symbol_batches(requested,"1Day",start,end):
        try:
            got=_alpaca_batch_bars(batch,start=start,end=end)
            for s,rows in got.items(): fetched.setdefault(s,[]).extend(rows)
        except urllib.error.HTTPError as e:
            failed_batches.append({"symbols":batch,"error":{"type":"HTTPError","status":e.code,"reason":str(e.reason),"source":"ALPACA_BATCH"}})
        except Exception as e:
            failed_batches.append({"symbols":batch,"error":{"type":type(e).__name__,"message":str(e)[:160],"source":"ALPACA_BATCH"}})
    # Merge by bar timestamp so an intraday forming 1Day bar is replaced on each hourly refresh.
    for s in requested:
        by_t={str(x.get("t")):x for x in (cached.get(s) or []) if x.get("t")}
        for x in fetched.get(s,[]): by_t[str(x.get("t"))]=x
        rows=sorted(by_t.values(),key=lambda x:str(x.get("t") or ""))[-DAILY_CACHE_KEEP_BARS:]
        if rows:
            cached[s]=[{k:x.get(k) for k in ("t","o","h","l","c","v") if x.get(k) is not None} for x in rows]

    # Deep-read only residual gaps; never refetch the full universe to repair a few symbols.
    gap_symbols=[s for s in symbols if len(cached.get(s) or [])<22]
    gap_recovered=set()
    if gap_symbols and not bootstrap:
        # 45 calendar days can contain fewer than 22 sessions around holidays/suspensions.
        # A 120-day residual-only read is cheap (only the gap queue) and distinguishes
        # genuinely short histories from an undersized recovery window.
        gap_start=end-timedelta(days=120)
        API_USAGE["history_gap_recovery"]["deep_window_days"]=120
        for batch in _pack_alpaca_symbol_batches(gap_symbols,"1Day",gap_start,end):
            before=API_USAGE["alpaca_daily_bars"]["http_requests"]
            API_USAGE["history_gap_recovery"]["logical_batches"]+=1
            try:
                got=_alpaca_batch_bars(batch,start=gap_start,end=end)
                for s in batch:
                    by_t={str(x.get("t")):x for x in (cached.get(s) or []) if x.get("t")}
                    for x in got.get(s,[]): by_t[str(x.get("t"))]=x
                    rows=sorted(by_t.values(),key=lambda x:str(x.get("t") or ""))[-DAILY_CACHE_KEEP_BARS:]
                    if rows: cached[s]=[{k:x.get(k) for k in ("t","o","h","l","c","v") if x.get(k) is not None} for x in rows]
                    if len(cached.get(s) or [])>=22: gap_recovered.add(s)
            except Exception as e:
                failed_batches.append({"symbols":batch,"error":{"type":type(e).__name__,"message":str(e)[:160],"source":"ALPACA_HISTORY_GAP_RECOVERY"}})
            finally:
                API_USAGE["history_gap_recovery"]["http_requests"]+=API_USAGE["alpaca_daily_bars"]["http_requests"]-before
        API_USAGE["history_gap_recovery"]["recovered"]=len(gap_recovered)

    MARKET_CACHE.parent.mkdir(parents=True,exist_ok=True)
    tmp=MARKET_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated_at":now(),"pit_cutoff":end.isoformat(),"feed":"sip","adjustment":"all",
                               "bootstrap":bootstrap,"bars":cached},separators=(",",":"))+"\n")
    tmp.replace(MARKET_CACHE)
    out={}; failed=[]
    failed_symbols_from_batches={s for item in failed_batches for s in item["symbols"]}
    for s in symbols:
        rows=cached.get(s,[])
        try: out[s]=_snapshot_from_bars(s,rows,"ALPACA_SIP_DAILY_CACHE")
        except Exception as e:
            rows=cached.get(s,[]); classification=None
            universe_exclusion=None
            if str(e)=="insufficient_history":
                # Zero bars after a 120-day residual recovery is not a 20-day indicator-history
                # problem: there is no recent tradable price series to evaluate. Keep it out of
                # history-gap counts and expose it as a universe/tradability identity exclusion.
                # This rule is point-in-time reproducible and avoids hard-coded symbol exceptions.
                if len(rows)==0:
                    classification=None
                    universe_exclusion="NO_RECENT_DAILY_BARS_AFTER_120D_RECOVERY"
                else:
                    # Purely factual classification: never infer IPO/listing/security age from
                    # the first bar in our cache. Corporate actions, uplists, resumptions and
                    # provider identity changes can all create a short current-source history.
                    classification="SOURCE_HISTORY_1_21_BARS_AFTER_120D_RECOVERY"
            first_bar_at=str(rows[0].get("t")) if rows else None
            last_bar_at=str(rows[-1].get("t")) if rows else None
            failed.append({"symbol":s,"error":{"type":type(e).__name__,"message":str(e)[:160],
                "source":"ALPACA_SIP_DAILY_CACHE","history_gap_classification":classification,
                "universe_exclusion":universe_exclusion,
                "bars_available":len(rows),"first_bar_at":first_bar_at,"last_bar_at":last_bar_at,
                "recovery_window_days":120 if (classification or universe_exclusion) else None}})
    for s in failed_symbols_from_batches:
        if not any(x["symbol"]==s for x in failed):
            failed.append({"symbol":s,"error":{"type":"BatchRefreshError","message":"incremental_refresh_failed","source":"ALPACA_BATCH"}})
    bench={}
    for idx in ("SPY","QQQ"):
        try: bench[idx]=_snapshot_from_bars(idx,cached.get(idx,[]),"ALPACA_SIP_DAILY_CACHE")
        except Exception: pass
    return out, failed, {"discovered":len(symbols),"source_errors":discovery_errors,
                         "cache_mode":"BOOTSTRAP_45D" if bootstrap else "INCREMENTAL_7D",
                         "cache_covered_before":covered,"benchmarks":bench,"api_usage":json.loads(json.dumps(API_USAGE))}



def avg(p):
    q=sum(t["notional"]/t["price"] for t in p["tranches"]); c=sum(t["notional"] for t in p["tranches"])
    return c/q if q else 0
def qty(p): return sum(t["notional"]/t["price"] for t in p["tranches"])
def net_pnl(p,price):
    c=sum(t["notional"] for t in p["tranches"]); proceeds=qty(p)*price
    fees=c*FEE_RATE+proceeds*FEE_RATE
    return proceeds-c-fees
def net_pct(p,price):
    c=sum(t["notional"] for t in p["tranches"])
    return net_pnl(p,price)/c*100 if c else 0

def main():
    state=load(STATE,{"version":2,"simulation_only":True,"positions":{},"closed":[]})
    events=load(EVENTS,[])
    validate_inputs(state,events)
    starting_positions=len(state.get("positions",{}))
    starting_events=len(events)
    starting_closed=len(state.get("closed",[]))
    starting_fingerprint=continuity_fingerprint(state,events)
    if state.get("reset_reason") or state.get("reset_at"):
        raise RuntimeError("state_continuity:manual_reset_marker_present")
    market, failed_symbols, discovery=stock_universe()
    bench=discovery.get("benchmarks") or {}
    session=_alpaca_exchange_session()
    actions_enabled=trade_action_window(session=session)
    candidates=[]; rejection_counts={}
    for s,m in market.items():
        d=entry_decision(m,bench.get("SPY"),bench.get("QQQ"))
        m["selection"]=d
        if d["ready"]: candidates.append((s,m,d))
        else:
            for reason in d["rejects"] or ["SCORE_OR_ENTRY_NOT_READY"]:
                rejection_counts[reason]=rejection_counts.get(reason,0)+1
    candidates.sort(key=lambda x:x[2]["score"],reverse=True)
    http_error_count=sum(1 for x in failed_symbols if (x.get("error") or {}).get("type")=="HTTPError")
    insufficient_history_count=sum(1 for x in failed_symbols if (x.get("error") or {}).get("history_gap_classification")=="SOURCE_HISTORY_1_21_BARS_AFTER_120D_RECOVERY")
    universe_exclusion_count=sum(1 for x in failed_symbols if (x.get("error") or {}).get("universe_exclusion"))
    # Transport health and history eligibility are separate dimensions.
    data_status=("OK" if market and http_error_count==0 else ("DEGRADED" if market else "UNKNOWN:ALL_STOCK_SOURCES_FAILED"))
    history_coverage_status=("COMPLETE" if insufficient_history_count==0 else "PARTIAL_HISTORY")
    # Selective V1: scan the whole market, but BUY only candidates that pass every gate.
    newly_opened=set()
    for s,m,d in candidates:
        event_at=now()
        if actions_enabled and trade_action_window(datetime.fromisoformat(event_at),session=session) and s not in state["positions"]:
            tr={"at":event_at,"price":m["price"],"notional":NOTIONAL,"reason":"SELECTIVE_ENTRY_V1","score":d["score"],"entry_structure":d["entry_structure"],"selection_reasons":d["reasons"],"selection_metrics":d["metrics"],"snapshot":m}
            state["positions"][s]={"symbol":s,"opened_at":tr["at"],"tranches":[tr],"entry_score":d["score"],"entry_structure":d["entry_structure"],"mfe_net_pct":net_pct({"tranches":[tr]},m["price"]),"mae_net_pct":net_pct({"tranches":[tr]},m["price"])}
            events.append({"type":"BUY","symbol":s,**tr}); newly_opened.add(s)
    for s,p in list(state["positions"].items()):
        m=market.get(s)
        if not m: continue
        price=m["price"]; r=net_pct(p,price)
        if p.get("mfe_net_pct") is None or p.get("mae_net_pct") is None:
            p["extrema_history_status"]="INITIALIZED_FROM_CURRENT_OBSERVATION"
        p["mfe_net_pct"]=max(number(p.get("mfe_net_pct"),f"{s}.mfe_net_pct",default=r),r); p["mae_net_pct"]=min(number(p.get("mae_net_pct"),f"{s}.mae_net_pct",default=r),r)
        p.update({"last_price":price,"last_at":now(),"avg_price":avg(p),"net_pnl_usdt":round(net_pnl(p,price),6),"net_return_pct":round(r,6)})
        # V3 lifecycle: worsening never ADDs; ADD waits for pullback + observable recovery.
        n=len(p["tranches"]); decision=entry_decision(m,bench.get("SPY"),bench.get("QQQ"))
        ps=position_state_v2(m,bench.get("SPY"),bench.get("QQQ"))
        recovery=recovery_add_signal(p,m,ps)
        p["recovery_add_signal"]=recovery
        p["market_state_v3"]=ps
        p["position_state_v3"]=position_state_v3(p,ps,r)
        p["position_state_v2"]=ps  # compatibility for existing forward-sample records
        mfe=p.get("mfe_net_pct",r); giveback=mfe-r
        floor=profit_floor_net_pct(mfe)
        p["profit_protection_floor_net_pct"]=round(floor,6) if floor is not None else None
        p["profit_protection_signal"]=bool(floor is not None and r<=floor)
        event_at=now()
        if actions_enabled and trade_action_window(datetime.fromisoformat(event_at),session=session) and s not in newly_opened and n<MAX_TRANCHES and recovery["eligible"] and decision["ready"]:
            tr={"at":event_at,"price":price,"notional":NOTIONAL,"reason":"PULLBACK_RECOVERY_ADD_V3","score":decision["score"],"position_state":ps,"recovery_signal":recovery,"snapshot":m}
            p["tranches"].append(tr); events.append({"type":"ADD","symbol":s,**tr}); p["avg_price"]=avg(p)
            p["pullback_seen"]=False; p["pullback_low_price"]=None
        # Intraday profit-protection SELL belongs exclusively to the 5m monitor.
        # This full scan uses daily bars that may be stale intraday/weekends.
        exit_reason=None
        if ps["state"]=="BROKEN":
            if r > 0:
                exit_reason="STRUCTURE_BROKEN_PROFIT_EXIT_V3"
                p.pop("rebound_exit_pending_v3",None)
            else:
                # Hard rule: never realize a loss into a breakdown. Wait for a rebound.
                p["rebound_exit_pending_v3"]={"armed_at":(p.get("rebound_exit_pending_v3") or {}).get("armed_at") or now(),
                    "reason":"FUNDAMENTAL_OR_STRUCTURE_DETERIORATION","lowest_net_return_pct":round(min(r,number((p.get("rebound_exit_pending_v3") or {}).get("lowest_net_return_pct"),f"{s}.lowest_net_return_pct",default=r)),6)}
        elif p.get("rebound_exit_pending_v3"):
            # Rebound exits are profit-only too. A losing rebound remains pending.
            if r > 0:
                exit_reason="REBOUND_PROFIT_EXIT_AFTER_DETERIORATION_V3"
        event_at=now()
        if exit_reason and actions_enabled and trade_action_window(datetime.fromisoformat(event_at),session=session):
            final_pnl=net_pnl(p,price); final_r=net_pct(p,price)
            closed=dict(p); closed.update({"closed_at":event_at,"exit_price":price,"exit_reason":exit_reason,
                "realized_net_pnl_usdt":round(final_pnl,6),"realized_net_return_pct":round(final_r,6),
                "gross_price_return_pct":round((price/avg(p)-1)*100,6),"estimated_total_fees_usdt":round((sum(t["notional"] for t in p["tranches"])*FEE_RATE)+(qty(p)*price*FEE_RATE),6),
                "profit_giveback_pct_points":round(giveback,6),"post_exit_tracking_due_days":[1,3,5,10]})
            state["closed"].append(closed); del state["positions"][s]
            events.append({"type":"SELL","at":closed["closed_at"],"symbol":s,"price":price,"reason":exit_reason,
                "net_pnl_usdt":closed["realized_net_pnl_usdt"],"net_return_pct":closed["realized_net_return_pct"],
                "gross_price_return_pct":closed["gross_price_return_pct"],"estimated_total_fees_usdt":closed["estimated_total_fees_usdt"],
                "position_state":ps,"USER_ALERT_REQUIRED":True})
        if s in state["positions"] and len(p["tranches"]) > n:
            # Report the post-ADD book only after this position's V3 decisions.
            # Keep r, extrema and lifecycle signals on their frozen decision basis.
            p.update({"net_pnl_usdt":round(net_pnl(p,price),6),
                      "net_return_pct":round(net_pct(p,price),6)})
    wins=[x for x in state["closed"] if x.get("realized_net_pnl_usdt",0)>0]; losses=[x for x in state["closed"] if x.get("realized_net_pnl_usdt",0)<=0]
    realized=sum(x.get("realized_net_pnl_usdt",0) for x in state["closed"])
    state["updated_at"]=now(); state["simulation_only"]=True
    state["source_commit"]=SOURCE_COMMIT; state["run_id"]=RUN_ID
    # Off-session scans are read/update-only: they must never delete the forward cohort or ledger.
    if not actions_enabled:
        if len(state.get("positions",{})) != starting_positions:
            raise RuntimeError(f"state_continuity:off_session_position_count_changed:{starting_positions}->{len(state.get('positions',{}))}")
        if len(events) != starting_events:
            raise RuntimeError(f"state_continuity:off_session_event_count_changed:{starting_events}->{len(events)}")
        if len(state.get("closed",[])) != starting_closed:
            raise RuntimeError(f"state_continuity:off_session_closed_count_changed:{starting_closed}->{len(state.get('closed',[]))}")
        if continuity_fingerprint(state,events) != starting_fingerprint:
            raise RuntimeError("state_continuity:off_session_forward_cohort_identity_changed")
    validate_ledger(state,events)
    save(STATE,state); save(EVENTS,events)
    save(SUMMARY,{"updated_at":now(),"source_commit":SOURCE_COMMIT,"run_id":RUN_ID,"simulation_only":True,**portfolio_statistics(state,events),"scan_state_stale":False,"universe_discovered":discovery["discovered"],"universe_seen":len(market),"market_data_status":data_status,"history_coverage_status":history_coverage_status,"transport_status":("OK" if market and http_error_count==0 else "DEGRADED"),"coverage_pct":round(len(market)/discovery["discovered"]*100,4) if discovery["discovered"] else 0.0,"insufficient_history_count":insufficient_history_count,"http_error_count":http_error_count,"trade_actions_enabled":actions_enabled,"universe_source_errors":discovery["source_errors"],"market_cache_mode":discovery.get("cache_mode"),
    "market_cache_covered_before":discovery.get("cache_covered_before"),"api_usage":json.loads(json.dumps(API_USAGE)),
    "history_gap_classification_counts":{k:sum(1 for x in failed_symbols if (x.get("error") or {}).get("history_gap_classification")==k) for k in sorted({(x.get("error") or {}).get("history_gap_classification") for x in failed_symbols if (x.get("error") or {}).get("history_gap_classification")})},
    "universe_exclusion_count":universe_exclusion_count,
    "universe_exclusion_counts":{k:sum(1 for x in failed_symbols if (x.get("error") or {}).get("universe_exclusion")==k) for k in sorted({(x.get("error") or {}).get("universe_exclusion") for x in failed_symbols if (x.get("error") or {}).get("universe_exclusion")})},
    "failed_symbols":failed_symbols,"candidates_ready":len(candidates),"rejection_counts":rejection_counts,"selection_version":"HYBRID_ENTRY_V1_POSITION_STATE_V3","open_positions":len(state["positions"]),"closed_positions":len(state["closed"]),"wins":len(wins),"losses":len(losses),"realized_net_pnl_usdt":round(realized,6),"events":len(events),"fee_rate_per_side":FEE_RATE,"policy":{"max_open":None,"standard_tranche_usdt":NOTIONAL,"max_tranches":MAX_TRANCHES,"profit_arm_net_pct":ARM_NET_PCT,"profit_floor_min_net_pct":PROFIT_FLOOR_NET_PCT,"profit_giveback_bands":PROFIT_GIVEBACK_BANDS,"paid_api_required":False,"real_orders":False}})
    print(json.dumps(load(SUMMARY,{}),ensure_ascii=False))

if __name__=="__main__":
    run_with_health(main,ROOT/"main-run-health-v1.json",SOURCE_COMMIT,RUN_ID,save)

