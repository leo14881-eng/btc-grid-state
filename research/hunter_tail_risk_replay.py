#!/usr/bin/env python3
"""Historical tail-risk replay for Hunter phase-1 shadow controls.

This is NOT a strategy backtest and NOT evidence of executable crash fills.
It uses Binance completed 5m klines for market-path replay. Historical order-book
depth, stop queue priority, exchange latency and true fills are unavailable, so
execution stress is reported separately under explicit assumptions.
"""
import datetime as dt,json,math,os,pathlib,urllib.parse,urllib.request
try:
    from research.hunter_policy import C
except ModuleNotFoundError as exc:
    if exc.name!="research":raise
    from hunter_policy import C

ROOT=pathlib.Path("research/results")
OUT=ROOT/"hunter-tail-risk-replay.json"
BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
SYMBOLS=("BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","SUIUSDT","ENAUSDT")
START=dt.datetime(2025,10,10,12,0,tzinfo=dt.timezone.utc)
END=dt.datetime(2025,10,11,12,0,tzinfo=dt.timezone.utc)

def get(url):
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-tail-risk-replay/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)

def bars(symbol,start=START,end=END):
    q=urllib.parse.urlencode({"symbol":symbol,"interval":"5m","startTime":int(start.timestamp()*1000),"endTime":int(end.timestamp()*1000),"limit":1000})
    rows=get(BN+"/api/v3/klines?"+q);out=[]
    for x in rows:
        try:
            t=int(x[6]);p=float(x[4])
        except (TypeError,ValueError,IndexError):continue
        if p>0 and math.isfinite(p) and t<=int(end.timestamp()*1000):out.append((t,p))
    return out

def align(series):
    common=None
    maps={}
    for sym,rows in series.items():
        m={t:p for t,p in rows};maps[sym]=m;common=set(m) if common is None else common&set(m)
    ts=sorted(common or [])
    return ts,{s:[maps[s][t] for t in ts] for s in maps}

def pct(a,b):return (b/a-1)*100 if a else 0.0

def detector(ts,px):
    btc=px["BTCUSDT"];alts=[s for s in px if s!="BTCUSDT"];high=[];normal=[]
    for i in range(len(ts)):
        if i<12:high.append(False);normal.append(True);continue
        b1=pct(btc[i-12],btc[i])
        rs=[pct(px[s][i-12],px[s][i]) for s in alts]
        neg=sum(x<0 for x in rs)/len(rs);loss5=sum(x<=-5 for x in rs)/len(rs);loss10=sum(x<=-10 for x in rs)/len(rs)
        btc_high=b1<=float(C["SYSTEMIC_BTC_HIGH_1H_PCT"]);btc_critical=b1<=float(C["SYSTEMIC_BTC_CRITICAL_1H_PCT"])
        breadth_high=loss5>=float(C["SYSTEMIC_BREADTH_HIGH_LOSS5_FRACTION"]) or neg>=float(C["SYSTEMIC_BREADTH_HIGH_NEGATIVE_FRACTION"])
        breadth_critical=loss10>=float(C["SYSTEMIC_BREADTH_CRITICAL_LOSS10_FRACTION"])
        high.append(bool((btc_high and breadth_high) or btc_critical or breadth_critical))
        normal.append(bool(not btc_high and not breadth_high and not btc_critical and not breadth_critical))
    return high,normal

def freeze_windows(ts,high,normal):
    required=int(C["SYSTEMIC_RECOVERY_OBSERVATIONS"]);frozen=False;recovery=0;start=None;windows=[]
    for i,t in enumerate(ts):
        if high[i]:
            if not frozen:start=t
            frozen=True;recovery=0
        elif frozen:
            recovery=recovery+1 if normal[i] else 0
            if recovery>=required:
                windows.append((start,t));frozen=False;recovery=0;start=None
    if frozen:windows.append((start,ts[-1]))
    return windows

def basket_index(px):
    alts=[s for s in px if s!="BTCUSDT"];base={s:px[s][0] for s in alts}
    return [sum(px[s][i]/base[s] for s in alts)/len(alts) for i in range(len(next(iter(px.values()))))]

def max_drawdown_equity(index,notional,principal=20000.0):
    eq=[principal+notional*(x-1) for x in index];peak=eq[0];mdd=0.0
    for x in eq:
        peak=max(peak,x)
        if peak>0:mdd=min(mdd,(x/peak-1)*100)
    return round(mdd,4),round(min(eq)-principal,2)

def missed_upside(index,ts,windows,undeployed):
    # Opportunity-cost proxy only: positive basket move during freeze windows
    # multiplied by undeployed capital. Existing positions are not assumed sold.
    by={t:i for i,t in enumerate(ts)};total=0.0
    for a,b in windows:
        i=by[a];j=by[b];move=max(0.0,index[j]/index[i]-1);total+=undeployed*move
    return round(total,2)

def partial_false_freeze_proxy(index,ts,windows):
    # Explicit proxy: a freeze start is counted false only if the next 60 minutes
    # finish positive AND never fall more than 1% below the freeze-start basket.
    # This evaluates only the replay's BTC+breadth detector, not unavailable
    # historical order-book/stablecoin dimensions.
    by={t:i for i,t in enumerate(ts)};flags=[]
    for a,_ in windows:
        i=by[a];j=min(len(index)-1,i+12);segment=index[i:j+1]
        flags.append(bool(index[j]>index[i] and min(segment)/index[i]-1>-0.01))
    return {"count":sum(flags),"total_freezes":len(flags),"rate":round(sum(flags)/len(flags),4) if flags else 0.0,
            "definition":"next 60m basket ends positive and has no additional drawdown worse than 1%; partial BTC+breadth replay proxy only"}

def stressed_fill_move(trough_move_pct,extra_adverse_slippage_pct):
    return max(-99.9,float(trough_move_pct)-float(extra_adverse_slippage_pct))

def run():
    series={s:bars(s) for s in SYMBOLS};ts,px=align(series)
    if len(ts)<100:raise RuntimeError("HISTORICAL_REPLAY_INSUFFICIENT_ALIGNED_BARS")
    index=basket_index(px);high,normal=detector(ts,px);windows=freeze_windows(ts,high,normal)
    principal=20000.0;baseline_notional=principal*.95
    budgets=[float(x) for x in C["TAIL_LOSS_BUDGET_CANDIDATES_USDT"]]
    stress_fraction=min(1.0,(float(C["TAIL_STRESS_LOSS_PCT"])+float(C["TAIL_EXECUTION_BUFFER_PCT"]))/100)
    scenarios=[]
    for budget in budgets:
        notional=min(principal,budget/stress_fraction)
        mdd,tail=max_drawdown_equity(index,notional,principal)
        end=round(notional*(index[-1]-1),2)
        scenarios.append({"tail_loss_budget_usdt":budget,"max_exposure_usdt":round(notional,2),"max_drawdown_pct":mdd,
                          "tail_pnl_usdt":tail,"end_net_pnl_usdt":end,
                          "missed_upside_proxy_usdt":missed_upside(index,ts,windows,max(0.0,baseline_notional-notional))})
    base_mdd,base_tail=max_drawdown_equity(index,baseline_notional,principal)
    recovery=[round((b-a)/60000,2) for a,b in windows]
    trough=min(range(len(index)),key=lambda i:index[i]);trough_move=(index[trough]-1)*100
    assumed_extra_slip=10.0
    forced_fill_move=stressed_fill_move(trough_move,assumed_extra_slip)
    report={"schema":"hunter_tail_risk_replay_v1","as_of_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
            "historical_window":{"start_utc":START.isoformat(),"end_utc":END.isoformat(),"source":"BINANCE_SPOT_5M_COMPLETED_KLINES","symbols":list(SYMBOLS),"aligned_bars":len(ts)},
            "scope":"TAIL_RISK_PATH_REPLAY_NOT_STRATEGY_BACKTEST",
            "limitations":["Historical klines cannot prove extreme-event stop execution price.","Historical spread/depth and queue priority are not reconstructed.","Stablecoin/order-book dimensions are omitted from detector replay; live gate remains fail-closed when those dimensions are missing.","Missed-upside is an opportunity-cost proxy, not evidence that Hunter would have selected the basket."],
            "partial_detector_assumption":"Replay HIGH uses historical BTC 1h speed plus cross-alt 1h breadth only; missing historical microstructure is NOT treated as live evidence.",
            "baseline_current_allocator":{"assumed_exposure_usdt":baseline_notional,"max_drawdown_pct":base_mdd,"tail_pnl_usdt":base_tail,"end_net_pnl_usdt":round(baseline_notional*(index[-1]-1),2)},
            "tail_budget_scenarios":scenarios,
            "freeze_windows":[{"start_utc":dt.datetime.fromtimestamp(a/1000,dt.timezone.utc).isoformat(),"end_utc":dt.datetime.fromtimestamp(b/1000,dt.timezone.utc).isoformat(),"recovery_minutes":round((b-a)/60000,2)} for a,b in windows],
            "recovery_time_minutes":{"max":max(recovery) if recovery else 0,"average":round(sum(recovery)/len(recovery),2) if recovery else 0},
            "false_freeze_proxy":partial_false_freeze_proxy(index,ts,windows),
            "full_false_freeze_rate":None,
            "full_false_freeze_rate_caveat":"A full live-gate false-freeze rate requires historical order-book/stablecoin evidence and is intentionally not fabricated.",
            "execution_stress_assumption":{"hypothetical_liquidity_only_forced_exit_at_basket_trough":True,"extra_adverse_slippage_pct":assumed_extra_slip,
                                           "basket_trough_move_pct":round(trough_move,4),"hypothetical_fill_move_pct":round(forced_fill_move,4),
                                           "caveat":"Assumption only. Not a reconstructed fill."},
            "capital_authority":"NONE_SHADOW_ONLY","real_trading_enabled":False}
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(report,ensure_ascii=False))
    return report

if __name__=="__main__":run()
