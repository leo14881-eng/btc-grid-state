#!/usr/bin/env python3
"""Leading crash-risk sentinel for Hunter.

Shadow-only. It estimates pre-crash risk accumulation before the existing
SYSTEMIC_RISK layer. It never places/cancels orders and never mutates Hunter's
ordinary BUY/SELL decisions.
"""
import datetime as dt,hashlib,json,math,statistics,urllib.parse,urllib.request

LEVELS={"NORMAL":0,"WATCH":1,"PRE_CRASH_1":2,"PRE_CRASH_2":3,"PRE_CRASH_3":4,"DATA_UNCERTAIN":1}

def finite(x):
    try:
        v=float(x);return v if math.isfinite(v) else None
    except (TypeError,ValueError):return None

def parse(x):
    return dt.datetime.fromisoformat(str(x).replace("Z","+00:00"))

def _get_json(url,timeout=8):
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-leading-risk-shadow/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r:return json.load(r)

def _derivatives(fetch_json):
    """Best-effort public Binance futures evidence. Missing data is uncertainty, never a sell signal."""
    base="https://fapi.binance.com"
    premium=fetch_json(base+"/fapi/v1/premiumIndex?"+urllib.parse.urlencode({"symbol":"BTCUSDT"}))
    oi=fetch_json(base+"/fapi/v1/openInterest?"+urllib.parse.urlencode({"symbol":"BTCUSDT"}))
    mark=finite((premium or {}).get("markPrice"));index=finite((premium or {}).get("indexPrice"))
    return {"open_interest":finite((oi or {}).get("openInterest")),
            "funding_rate":finite((premium or {}).get("lastFundingRate")),
            "basis_pct":round((mark/index-1)*100,5) if mark and index else None}

def _median(xs):
    xs=[x for x in xs if x is not None]
    return statistics.median(xs) if xs else None

def collect_evidence(scan,liq,review,systemic,previous,now,cfg,fetch_json=None):
    fetch_json=fetch_json or _get_json
    coins=(scan or {}).get("coins") or {}
    stable={"USDT","USDC","FDUSD","TUSD","USDP","DAI","BUSD"}
    moves=[finite(v.get("change_24h_pct")) for a,v in coins.items() if a!="BTC" and a not in stable and isinstance(v,dict)]
    moves=[x for x in moves if x is not None]
    neg=sum(x<0 for x in moves)/len(moves) if moves else None
    loss5=sum(x<=-5 for x in moves)/len(moves) if moves else None

    rel1=[];rel4=[]
    for row in (review or {}).get("candidates") or []:
        sig=(row or {}).get("signal") or {}
        a=finite(sig.get("btc_relative_1h_pct"));b=finite(sig.get("btc_relative_4h_pct"))
        if a is not None:rel1.append(a)
        if b is not None:rel4.append(b)
    rel1neg=sum(x<0 for x in rel1)/len(rel1) if rel1 else None
    rel4neg=sum(x<0 for x in rel4)/len(rel4) if rel4 else None

    spreads=[];bid_depth=[];imbal=[]
    max_age=float(cfg["LEADING_MAX_EVIDENCE_AGE_SECONDS"])
    for row in ((liq or {}).get("snapshots") or {}).values():
        if not isinstance(row,dict):continue
        try:age=(now-parse(row.get("as_of_utc"))).total_seconds()
        except Exception:continue
        if age< -30 or age>max_age:continue
        sp=finite(row.get("spread_bps"));bd=finite(row.get("bid_depth_2pct_usdt"));ad=finite(row.get("ask_depth_2pct_usdt"))
        if sp is not None:spreads.append(sp)
        if bd is not None:bid_depth.append(bd)
        if bd is not None and ad is not None and bd+ad>0:imbal.append((bd-ad)/(bd+ad))
    liquidity={"sample_count":len(spreads),"median_spread_bps":_median(spreads),"median_bid_depth_usdt":_median(bid_depth),
               "median_book_imbalance":_median(imbal)}

    deriv_error=None
    try:deriv=_derivatives(fetch_json)
    except Exception as exc:
        deriv={"open_interest":None,"funding_rate":None,"basis_pct":None};deriv_error=type(exc).__name__

    prev=(previous or {}).get("evidence") or {}
    def delta(path,current):
        p=prev
        for k in path:p=(p or {}).get(k)
        p=finite(p)
        return None if p is None or current is None else current-p
    deriv["open_interest_change_pct"]=None
    old_oi=finite(((prev.get("leverage") or {}).get("open_interest")))
    if old_oi and deriv["open_interest"] is not None:
        deriv["open_interest_change_pct"]=round((deriv["open_interest"]/old_oi-1)*100,4)

    breadth={"sample_count":len(moves),"negative_fraction":round(neg,4) if neg is not None else None,
             "loss_5pct_fraction":round(loss5,4) if loss5 is not None else None,
             "negative_acceleration":delta(["breadth","negative_fraction"],neg)}
    relative={"sample_1h":len(rel1),"negative_1h_fraction":round(rel1neg,4) if rel1neg is not None else None,
              "sample_4h":len(rel4),"negative_4h_fraction":round(rel4neg,4) if rel4neg is not None else None,
              "negative_1h_acceleration":delta(["relative","negative_1h_fraction"],rel1neg)}
    liquidity["spread_acceleration_bps"]=delta(["liquidity","median_spread_bps"],liquidity["median_spread_bps"])
    old_depth=finite(((prev.get("liquidity") or {}).get("median_bid_depth_usdt")))
    liquidity["bid_depth_change_pct"]=None if not old_depth or liquidity["median_bid_depth_usdt"] is None else round((liquidity["median_bid_depth_usdt"]/old_depth-1)*100,4)

    btc=dict((systemic or {}).get("btc_short") or {})
    stablecoins=dict((systemic or {}).get("stablecoins") or {})
    missing=[]
    if len(moves)<int(cfg["LEADING_MIN_BREADTH_SAMPLE"]):missing.append("BREADTH_SAMPLE_INSUFFICIENT")
    if len(spreads)<int(cfg["LEADING_MIN_BOOK_SAMPLE"]):missing.append("BOOK_SAMPLE_INSUFFICIENT")
    if len(rel1)<int(cfg["LEADING_MIN_RELATIVE_SAMPLE"]):missing.append("BTC_RELATIVE_SAMPLE_INSUFFICIENT")
    if deriv["open_interest"] is None or deriv["funding_rate"] is None:missing.append("DERIVATIVES_UNAVAILABLE")
    material={"scan":(scan or {}).get("generation_id"),"at":(scan or {}).get("as_of_utc"),"breadth":breadth,
              "relative":relative,"liquidity":liquidity,"leverage":deriv,"btc":btc,"stable":stablecoins}
    oid=hashlib.sha256(json.dumps(material,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {"schema":"hunter_leading_risk_evidence_v1","observed_at_utc":now.isoformat(),"observation_id":oid,
            "breadth":breadth,"relative":relative,"liquidity":liquidity,"leverage":deriv,"leverage_fetch_error":deriv_error,
            "btc_structure":btc,"stablecoins":stablecoins,"missing_or_stale":missing,"capital_authority":"NONE_SHADOW_ONLY"}

def classify(e,cfg):
    groups={};reasons=[]
    b=e.get("breadth") or {};neg=finite(b.get("negative_fraction"));acc=finite(b.get("negative_acceleration"));l5=finite(b.get("loss_5pct_fraction"))
    groups["breadth"]=bool((neg is not None and neg>=cfg["LEADING_BREADTH_NEGATIVE_FRACTION"]) or
                           (acc is not None and acc>=cfg["LEADING_BREADTH_ACCELERATION"]) or
                           (l5 is not None and l5>=cfg["LEADING_BREADTH_LOSS5_FRACTION"]))
    r=e.get("relative") or {};rn=finite(r.get("negative_1h_fraction"));ra=finite(r.get("negative_1h_acceleration"))
    groups["relative"]=bool((rn is not None and rn>=cfg["LEADING_RELATIVE_NEGATIVE_FRACTION"]) or
                            (ra is not None and ra>=cfg["LEADING_RELATIVE_ACCELERATION"]))
    l=e.get("liquidity") or {};sp=finite(l.get("median_spread_bps"));sa=finite(l.get("spread_acceleration_bps"));dd=finite(l.get("bid_depth_change_pct"));imb=finite(l.get("median_book_imbalance"))
    groups["liquidity"]=bool((sp is not None and sp>=cfg["LEADING_MEDIAN_SPREAD_BPS"]) or
                             (sa is not None and sa>=cfg["LEADING_SPREAD_ACCELERATION_BPS"]) or
                             (dd is not None and dd<=cfg["LEADING_BID_DEPTH_DROP_PCT"]) or
                             (imb is not None and imb<=cfg["LEADING_BOOK_IMBALANCE"]))
    btc=e.get("btc_structure") or {};r5=finite(btc.get("return_5m_pct"));r15=finite(btc.get("return_15m_pct"));r60=finite(btc.get("return_1h_pct"))
    groups["btc_structure"]=bool((r15 is not None and r15<=cfg["LEADING_BTC_15M_PCT"]) or
                                 (r60 is not None and r60<=cfg["LEADING_BTC_1H_PCT"]) or
                                 (r5 is not None and r15 is not None and r5<=cfg["LEADING_BTC_5M_PCT"] and r15<0))
    lev=e.get("leverage") or {};oi=finite(lev.get("open_interest_change_pct"));fund=finite(lev.get("funding_rate"));basis=finite(lev.get("basis_pct"))
    groups["leverage"]=bool((oi is not None and abs(oi)>=cfg["LEADING_OI_CHANGE_PCT"]) or
                            (fund is not None and abs(fund)>=cfg["LEADING_FUNDING_ABS"]) or
                            (basis is not None and abs(basis)>=cfg["LEADING_BASIS_ABS_PCT"]))
    dev=finite((e.get("stablecoins") or {}).get("max_deviation_pct"))
    groups["stablecoin"]=bool(dev is not None and dev>=cfg["LEADING_STABLE_DEVIATION_PCT"])
    weights={"breadth":20,"relative":20,"liquidity":20,"btc_structure":15,"leverage":15,"stablecoin":10}
    score=sum(weights[k] for k,v in groups.items() if v)
    for k,v in groups.items():
        if v:reasons.append("LEADING_"+k.upper()+"_DETERIORATION")
    n=sum(groups.values())
    if score>=cfg["LEADING_PRE3_SCORE"] and n>=4:raw="PRE_CRASH_3"
    elif score>=cfg["LEADING_PRE2_SCORE"] and n>=3:raw="PRE_CRASH_2"
    elif score>=cfg["LEADING_PRE1_SCORE"] and n>=2:raw="PRE_CRASH_1"
    elif score>=cfg["LEADING_WATCH_SCORE"] and n>=1:raw="WATCH"
    else:raw="NORMAL"
    if not reasons:reasons=["LEADING_EVIDENCE_NORMAL"]
    return raw,int(score),groups,reasons

def update_state(state,evidence,now,cfg):
    old=state or {};oid=evidence.get("observation_id")
    if oid and oid==old.get("last_observation_id"):return old,False
    raw,score,groups,reasons=classify(evidence,cfg)
    prev=old.get("level") or "NORMAL";candidate=old.get("candidate_level");count=int(old.get("candidate_count") or 0)
    last=old.get("last_transition_observation_at_utc");gap_ok=True
    if last:
        try:gap_ok=(now-parse(last)).total_seconds()>=float(cfg["LEADING_MIN_CONFIRM_GAP_SECONDS"])
        except Exception:gap_ok=False
    if LEVELS.get(raw,0)>LEVELS.get(prev,0):
        if raw==candidate and gap_ok:count+=1
        else:candidate=raw;count=1
        if count>=int(cfg["LEADING_CONFIRM_OBSERVATIONS"]):
            level=raw;candidate=None;count=0;last=now.isoformat()
        else:level=prev
        recovery=0
    elif LEVELS.get(raw,0)<LEVELS.get(prev,0):
        recovery=int(old.get("recovery_observations") or 0)+(1 if gap_ok else 0)
        if recovery>=int(cfg["LEADING_RECOVERY_OBSERVATIONS"]):
            level=raw;recovery=0;candidate=None;count=0;last=now.isoformat()
        else:level=prev
    else:
        level=prev;recovery=0;candidate=None;count=0
    row={"level":level,"raw_level":raw,"score":score,"groups":groups,"reasons":reasons,
         "candidate_level":candidate,"candidate_count":count,"recovery_observations":recovery,
         "last_transition_observation_at_utc":last,"last_observation_id":oid,"updated_at_utc":now.isoformat(),
         "evidence":evidence,"data_uncertain":bool(evidence.get("missing_or_stale")),"missing_or_stale":evidence.get("missing_or_stale") or [],
         "capital_authority":"NONE_SHADOW_ONLY"}
    return row,True

def shadow_derisk(level,used_usdt,tail_cap_usdt=None):
    """Counterfactual only. Returns targets; never changes a position."""
    profiles={
      "A_GENTLE":{"NORMAL":1.0,"WATCH":1.0,"PRE_CRASH_1":1.0,"PRE_CRASH_2":.75,"PRE_CRASH_3":.50},
      "B_BALANCED":{"NORMAL":1.0,"WATCH":1.0,"PRE_CRASH_1":1.0,"PRE_CRASH_2":.50,"PRE_CRASH_3":.25},
      "C_DEFENSIVE":{"NORMAL":1.0,"WATCH":1.0,"PRE_CRASH_1":1.0,"PRE_CRASH_2":.25,"PRE_CRASH_3":.20}}
    out={}
    for name,m in profiles.items():
        frac=m.get(level,1.0);target=max(0.0,float(used_usdt)*frac)
        if level=="PRE_CRASH_3" and tail_cap_usdt is not None:target=max(float(tail_cap_usdt),target)
        out[name]={"target_fraction":frac,"target_exposure_usdt":round(target,2),
                   "counterfactual_reduction_usdt":round(max(0.0,float(used_usdt)-target),2)}
    return out

def update_history(doc,row,used_usdt,tail_cap_usdt,cfg):
    hist=list((doc or {}).get("history") or [])
    hist.append({"at_utc":row["updated_at_utc"],"level":row["level"],"raw_level":row["raw_level"],"score":row["score"],
                 "groups":row["groups"],"data_uncertain":row["data_uncertain"],"used_exposure_usdt":round(float(used_usdt),2),
                 "shadow_derisk":shadow_derisk(row["level"],used_usdt,tail_cap_usdt)})
    return hist[-int(cfg["LEADING_HISTORY_LIMIT"]):]
