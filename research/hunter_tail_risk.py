#!/usr/bin/env python3
"""Portfolio tail-risk controls for Hunter shadow lanes.

Shadow simulation only. This module never places, amends, cancels, or assumes
exchange orders. Historical prices cannot prove executable crash fills.
"""
import datetime as dt,hashlib,json,math,urllib.parse,urllib.request

LEVELS={"NORMAL":0,"ELEVATED":1,"HIGH":2,"CRITICAL":3}

def finite(x):
    try:
        v=float(x);return v if math.isfinite(v) else None
    except (TypeError,ValueError):return None

def parse(x):
    return dt.datetime.fromisoformat(str(x).replace("Z","+00:00"))

def is_fresh(ts,now,max_age):
    try:
        age=(now-parse(ts)).total_seconds()
        # Same-process evidence collectors may stamp a snapshot milliseconds
        # after the cycle timestamp. Tolerate only a tiny clock skew; this does
        # not make genuinely future/stale evidence valid.
        return -30<=age<=max_age
    except (TypeError,ValueError):return False

def _get_json(url,timeout=8):
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-systemic-risk-shadow/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r:return json.load(r)

def _btc_short_returns(now,base_url,fetch_json):
    q=urllib.parse.urlencode({"symbol":"BTCUSDT","interval":"5m","limit":16})
    rows=fetch_json(base_url+"/api/v3/klines?"+q)
    completed=[]
    now_ms=int(now.timestamp()*1000)
    for row in rows if isinstance(rows,list) else []:
        try:
            close_time=int(row[6]);close=float(row[4])
        except (TypeError,ValueError,IndexError):continue
        if close_time<=now_ms and close>0 and math.isfinite(close):completed.append((close_time,close))
    if len(completed)<13:return {"return_5m_pct":None,"return_15m_pct":None,"return_1h_pct":None,"last_completed_close_utc":None}
    z=completed[-13:];last=z[-1][1]
    pct=lambda old:round((last/old-1)*100,4)
    return {"return_5m_pct":pct(z[-2][1]),"return_15m_pct":pct(z[-4][1]),"return_1h_pct":pct(z[0][1]),
            "last_completed_close_utc":dt.datetime.fromtimestamp(z[-1][0]/1000,dt.timezone.utc).isoformat()}

def _stablecoin_prices(base_url,fetch_json):
    out={}
    for symbol in ("USDCUSDT","FDUSDUSDT","TUSDUSDT"):
        try:
            d=fetch_json(base_url+"/api/v3/ticker/price?"+urllib.parse.urlencode({"symbol":symbol}))
            p=finite((d or {}).get("price"))
            if p and p>0:out[symbol]=p
        except Exception:
            pass
    return out

def collect_systemic_evidence(scan,liq,now,cfg,base_url="https://data-api.binance.vision",fetch_json=None):
    """Collect independent shadow-only evidence. Missing/stale dimensions are explicit."""
    fetch_json=fetch_json or _get_json
    max_age=int(cfg.get("SYSTEMIC_MAX_EVIDENCE_AGE_SECONDS",cfg["MAX_EVIDENCE_AGE_SECONDS"]))
    scan_fresh=is_fresh((scan or {}).get("as_of_utc"),now,max_age)
    coins=(scan or {}).get("coins") or {}
    stable_bases={"USDT","USDC","FDUSD","TUSD","USDP","DAI","BUSD"}
    moves=[finite(v.get("change_24h_pct")) for k,v in coins.items() if k!="BTC" and k not in stable_bases and isinstance(v,dict)
           and (not v.get("venues") or "binance" in v["venues"])]
    moves=[x for x in moves if x is not None]
    negative=(sum(x<0 for x in moves)/len(moves)) if moves else None
    loss5=(sum(x<=-5 for x in moves)/len(moves)) if moves else None
    loss10=(sum(x<=-10 for x in moves)/len(moves)) if moves else None

    books=[]
    for a,row in ((liq or {}).get("snapshots") or {}).items():
        if not isinstance(row,dict) or not is_fresh(row.get("as_of_utc"),now,max_age):continue
        sp=finite(row.get("spread_bps"));bd=finite(row.get("bid_depth_2pct_usdt"));ad=finite(row.get("ask_depth_2pct_usdt"))
        if sp is None or bd is None or ad is None:continue
        books.append({"asset":a,"spread_bps":sp,"min_depth_2pct_usdt":min(bd,ad),
                      "catastrophic":sp>float(cfg["HARD_SPREAD_BPS"]) or min(bd,ad)<float(cfg["HARD_MIN_DEPTH_USDT"])})
    cat_frac=(sum(x["catastrophic"] for x in books)/len(books)) if books else None

    btc_error=None
    try:btc_short=_btc_short_returns(now,base_url,fetch_json)
    except Exception as exc:
        btc_short={"return_5m_pct":None,"return_15m_pct":None,"return_1h_pct":None,"last_completed_close_utc":None}
        btc_error=type(exc).__name__

    stable_error=None
    try:stable=_stablecoin_prices(base_url,fetch_json)
    except Exception as exc:
        stable={};stable_error=type(exc).__name__
    deviations={k:round(abs(v-1.0)*100,4) for k,v in stable.items()}
    max_dev=max(deviations.values()) if deviations else None

    missing=[]
    if not scan_fresh:missing.append("MARKET_SCAN_MISSING_OR_STALE")
    if btc_short["return_5m_pct"] is None or btc_short["return_15m_pct"] is None or btc_short["return_1h_pct"] is None:missing.append("BTC_SHORT_TERM_MISSING")
    if negative is None or loss5 is None:missing.append("MARKET_BREADTH_MISSING")
    if len(books)<int(cfg["SYSTEMIC_MIN_BOOK_SAMPLE"]):missing.append("CROSS_ASSET_LIQUIDITY_SAMPLE_INSUFFICIENT")
    if len(stable)<int(cfg["SYSTEMIC_MIN_STABLECOIN_SAMPLE"]):missing.append("STABLECOIN_EVIDENCE_INSUFFICIENT")
    material={"scan_generation_id":(scan or {}).get("generation_id"),"scan_as_of_utc":(scan or {}).get("as_of_utc"),
              "btc_close":btc_short.get("last_completed_close_utc"),"btc":btc_short,"negative":negative,"loss5":loss5,"loss10":loss10,
              "book_times":sorted(str(x.get("as_of_utc")) for x in ((liq or {}).get("snapshots") or {}).values() if isinstance(x,dict))[-10:],
              "stable":stable}
    oid=hashlib.sha256(json.dumps(material,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {"schema":"hunter_systemic_risk_evidence_v1","observed_at_utc":now.isoformat(),"observation_id":oid,
            "scan_generation_id":(scan or {}).get("generation_id"),"scan_as_of_utc":(scan or {}).get("as_of_utc"),"scan_fresh":scan_fresh,
            "btc_short":btc_short,"btc_fetch_error":btc_error,
            "breadth":{"sample_count":len(moves),"negative_fraction":round(negative,4) if negative is not None else None,
                       "loss_5pct_fraction":round(loss5,4) if loss5 is not None else None,"loss_10pct_fraction":round(loss10,4) if loss10 is not None else None},
            "liquidity":{"fresh_sample_count":len(books),"catastrophic_fraction":round(cat_frac,4) if cat_frac is not None else None,
                         "hard_spread_bps":float(cfg["HARD_SPREAD_BPS"]),"hard_min_depth_usdt":float(cfg["HARD_MIN_DEPTH_USDT"])},
            "stablecoins":{"prices":stable,"deviation_pct":deviations,"max_deviation_pct":max_dev,"fetch_error":stable_error},
            "missing_or_stale":missing,"capital_authority":"NONE_SHADOW_ONLY"}

def classify_systemic_risk(e,cfg):
    """Return raw risk level. Missing required evidence fails closed to HIGH."""
    reasons=[];severities=[]
    missing=list((e or {}).get("missing_or_stale") or [])
    if missing:return "HIGH",["FAIL_CLOSED_EVIDENCE:"+x for x in missing]
    b=(e or {}).get("btc_short") or {};r5=finite(b.get("return_5m_pct"));r15=finite(b.get("return_15m_pct"));r60=finite(b.get("return_1h_pct"))
    if r60<=float(cfg["SYSTEMIC_BTC_CRITICAL_1H_PCT"]) or r15<=float(cfg["SYSTEMIC_BTC_CRITICAL_15M_PCT"]):severities.append(3);reasons.append("BTC_RAPID_CRITICAL")
    elif r60<=float(cfg["SYSTEMIC_BTC_HIGH_1H_PCT"]) or r15<=float(cfg["SYSTEMIC_BTC_HIGH_15M_PCT"]) or r5<=float(cfg["SYSTEMIC_BTC_HIGH_5M_PCT"]):severities.append(2);reasons.append("BTC_RAPID_HIGH")
    elif r60<=float(cfg["SYSTEMIC_BTC_ELEVATED_1H_PCT"]):severities.append(1);reasons.append("BTC_RAPID_ELEVATED")

    br=(e or {}).get("breadth") or {};neg=finite(br.get("negative_fraction"));l5=finite(br.get("loss_5pct_fraction"));l10=finite(br.get("loss_10pct_fraction"))
    if l10 is not None and l10>=float(cfg["SYSTEMIC_BREADTH_CRITICAL_LOSS10_FRACTION"]):severities.append(3);reasons.append("MARKET_BREADTH_CRITICAL")
    elif (l5 is not None and l5>=float(cfg["SYSTEMIC_BREADTH_HIGH_LOSS5_FRACTION"])) or (neg is not None and neg>=float(cfg["SYSTEMIC_BREADTH_HIGH_NEGATIVE_FRACTION"])):severities.append(2);reasons.append("MARKET_BREADTH_HIGH")
    elif (l5 is not None and l5>=float(cfg["SYSTEMIC_BREADTH_ELEVATED_LOSS5_FRACTION"])) or (neg is not None and neg>=float(cfg["SYSTEMIC_BREADTH_ELEVATED_NEGATIVE_FRACTION"])):severities.append(1);reasons.append("MARKET_BREADTH_ELEVATED")

    cat=finite(((e or {}).get("liquidity") or {}).get("catastrophic_fraction"))
    if cat is not None and cat>=float(cfg["SYSTEMIC_LIQ_CRITICAL_FRACTION"]):severities.append(3);reasons.append("CROSS_ASSET_LIQUIDITY_CRITICAL")
    elif cat is not None and cat>=float(cfg["SYSTEMIC_LIQ_HIGH_FRACTION"]):severities.append(2);reasons.append("CROSS_ASSET_LIQUIDITY_HIGH")
    elif cat is not None and cat>=float(cfg["SYSTEMIC_LIQ_ELEVATED_FRACTION"]):severities.append(1);reasons.append("CROSS_ASSET_LIQUIDITY_ELEVATED")

    dev=finite(((e or {}).get("stablecoins") or {}).get("max_deviation_pct"))
    if dev is not None and dev>=float(cfg["SYSTEMIC_STABLE_CRITICAL_DEVIATION_PCT"]):severities.append(3);reasons.append("STABLECOIN_DEPEG_CRITICAL")
    elif dev is not None and dev>=float(cfg["SYSTEMIC_STABLE_HIGH_DEVIATION_PCT"]):severities.append(2);reasons.append("STABLECOIN_DEPEG_HIGH")
    elif dev is not None and dev>=float(cfg["SYSTEMIC_STABLE_ELEVATED_DEVIATION_PCT"]):severities.append(1);reasons.append("STABLECOIN_DEPEG_ELEVATED")

    critical=sum(x>=3 for x in severities);high=sum(x>=2 for x in severities);elev=sum(x>=1 for x in severities)
    if critical>=1 and high>=2:return "CRITICAL",reasons
    if critical>=1 or high>=2:return "HIGH",reasons
    if high>=1 or elev>=2:return "ELEVATED",reasons
    return "NORMAL",reasons or ["SYSTEMIC_EVIDENCE_NORMAL"]

def admit_recovery_observation(row,evidence,now,cfg,remember=True):
    """Persist exact recovery identities; never evict IDs and make them reusable.

    Only recovery episodes accumulate IDs, not ordinary NORMAL monitoring.
    Both source time and processing time must advance. Existing freshness and
    sampling limits apply; a new ID alone is not independent evidence.
    """
    oid=(evidence or {}).get("observation_id")
    observed=(evidence or {}).get("observed_at_utc")
    seen=list(row.get("recovery_seen_observation_ids") or [])
    previous=row.get("last_observation_id") or row.get("last_recovery_seen_observation_id")
    if not oid or oid in seen or oid==previous:return False
    max_age=cfg.get("SYSTEMIC_MAX_EVIDENCE_AGE_SECONDS",cfg["MAX_EVIDENCE_AGE_SECONDS"])
    if not is_fresh(observed,now,max_age):return False
    loss_times=[x.get("at_utc") for x in row.get("loss_events",[]) if x.get("at_utc")]
    if row.get("last_loss_exit_at_utc"):loss_times.append(row["last_loss_exit_at_utc"])
    try:
        if loss_times:
            loss_at=max(parse(x) for x in loss_times)
            if now<=loss_at or parse(observed)<=loss_at:return False
    except (TypeError,ValueError):return False
    processing_times=[row.get(key) for key in ("last_recovery_seen_at_utc","updated_at_utc","last_recovery_counted_at_utc") if row.get(key)]
    last_source=row.get("last_recovery_seen_observed_at_utc") or row.get("last_observed_at_utc")
    try:
        if processing_times and now<=max(parse(x) for x in processing_times):return False
        if last_source and parse(observed)<=parse(last_source):return False
    except (TypeError,ValueError):return False
    if remember:
        if previous and previous not in seen:seen.append(previous)
        seen.append(oid)
    row["recovery_seen_observation_ids"]=seen
    row["last_recovery_seen_at_utc"]=now.isoformat()
    row["last_recovery_seen_observed_at_utc"]=observed
    return True

def update_systemic_risk(state,evidence,now,cfg):
    raw,reasons=classify_systemic_risk(evidence,cfg)
    old=dict(state.get("systemic_risk") or {})
    oid=(evidence or {}).get("observation_id")
    if oid and oid==old.get("last_observation_id"):return old,False
    recovering=old.get("level") in ("HIGH","CRITICAL") or old.get("recovery_mode")
    tracking=recovering or (state.get("circuit_breaker") or {}).get("status") in ("WATCH","TRIPPED","RECOVERING")
    has_history="recovery_seen_observation_ids" in old
    accepted=admit_recovery_observation(old,evidence,now,cfg,tracking)
    if not accepted and raw=="NORMAL":return state.get("systemic_risk") or {},False
    prev=old.get("level") or "NORMAL";required=int(cfg["SYSTEMIC_RECOVERY_OBSERVATIONS"])
    recovery=int(old.get("recovery_observations") or 0)
    recovery_mode=bool(old.get("recovery_mode"))
    last_counted=old.get("last_recovery_counted_at_utc")
    # Migration safety: recovery counts created before temporal independence was
    # enforced are not trusted.
    if recovery_mode and recovery>0 and (not last_counted or not has_history or not old.get("last_recovery_counted_observed_at_utc")):recovery=0;last_counted=None
    if raw in ("HIGH","CRITICAL"):
        level=raw;recovery=0;release=0.0;recovery_mode=True;last_counted=None
    elif prev in ("HIGH","CRITICAL") or recovery_mode:
        if raw=="NORMAL":
            min_gap=float(cfg["SYSTEMIC_RECOVERY_MIN_GAP_SECONDS"])
            gap_ok=True
            if last_counted:
                try:gap_ok=(now-parse(last_counted)).total_seconds()>=min_gap and (parse(evidence["observed_at_utc"])-parse(old["last_recovery_counted_observed_at_utc"])).total_seconds()>=min_gap
                except Exception:gap_ok=False
            if gap_ok:
                recovery+=1;last_counted=now.isoformat();old["last_recovery_counted_observed_at_utc"]=evidence["observed_at_utc"]
            if recovery>=required:
                level="NORMAL";release=1.0;recovery_mode=False
            else:
                level="ELEVATED";release=round(recovery/required,4);recovery_mode=True
                if not gap_ok:reasons=list(reasons)+["RECOVERY_OBSERVATION_TOO_SOON"]
        else:
            level="HIGH";release=0.0;recovery=0;recovery_mode=True;last_counted=None
            reasons=list(reasons)+["RECOVERY_REQUIRES_CONSECUTIVE_NORMAL_OBSERVATIONS"]
    else:
        level=raw;release=1.0;recovery=0;recovery_mode=False
    row={"level":level,"raw_level":raw,"reasons":reasons,"entered_at_utc":old.get("entered_at_utc") if level==prev else now.isoformat(),
         "updated_at_utc":now.isoformat(),"last_observation_id":oid,"last_observed_at_utc":(evidence or {}).get("observed_at_utc"),
         "recovery_observations":recovery,"recovery_required":required,"recovery_mode":recovery_mode,
         "last_recovery_counted_at_utc":last_counted,"recovery_min_gap_seconds":float(cfg["SYSTEMIC_RECOVERY_MIN_GAP_SECONDS"]),
         "risk_release_fraction":release,"evidence":evidence,"capital_authority":"NONE_SHADOW_ONLY"}
    for key in ("recovery_seen_observation_ids","last_recovery_seen_at_utc","last_recovery_seen_observed_at_utc","last_recovery_counted_observed_at_utc"):
        if key in old:row[key]=old[key]
    state["systemic_risk"]=row
    return row,True

def risk_blocks_new(state):
    """Unified fail-closed gate for every Hunter BUY/ADD path.

    Existing positions remain manageable; only NEW risk is frozen.  Circuit
    breaker is deliberately checked here so V1 Broad Net and V2 Capital Review
    cannot diverge or bypass a TRIPPED/RECOVERING breaker.
    """
    row=(state or {}).get("systemic_risk") or {}
    cb=(state or {}).get("circuit_breaker") or {}
    systemic_block=row.get("level") in ("HIGH","CRITICAL") or not row.get("last_observation_id")
    circuit_block=cb.get("status") in ("TRIPPED","RECOVERING")
    return bool(systemic_block or circuit_block)

def confirmed_systemic_liquidity_shock(state):
    """Only confirmed fresh systemic evidence may suppress liquidity-only hard exits.
    Fail-closed HIGH caused by missing/stale data freezes entries but never grants
    this suppression authority.
    """
    row=(state or {}).get("systemic_risk") or {};ev=row.get("evidence") or {}
    if row.get("raw_level") not in ("HIGH","CRITICAL") or (ev.get("missing_or_stale") or []):return False
    reasons=row.get("reasons") or []
    return any(str(x).startswith(("BTC_RAPID_","MARKET_BREADTH_","CROSS_ASSET_LIQUIDITY_","STABLECOIN_DEPEG_")) for x in reasons)

def configured_tail_budget(cfg):
    active=finite(cfg.get("TAIL_LOSS_BUDGET_ACTIVE_USDT"))
    candidates=[finite(x) for x in cfg.get("TAIL_LOSS_BUDGET_CANDIDATES_USDT",[])];candidates=[x for x in candidates if x and x>0]
    if active and active>0:return active,"ACTIVE_APPROVED"
    if not candidates:return None,"UNCONFIGURED_FAIL_CLOSED"
    return max(candidates),"CALIBRATION_CEILING_NOT_FINAL"

def tail_budget_snapshot(state,cfg,capital_pool_usdt):
    budget,status=configured_tail_budget(cfg)
    stress=max(0.0,float(cfg["TAIL_STRESS_LOSS_PCT"]))/100
    buffer=max(0.0,float(cfg["TAIL_EXECUTION_BUFFER_PCT"]))/100
    loss_fraction=min(1.0,stress+buffer)
    candidates={}
    for x in cfg.get("TAIL_LOSS_BUDGET_CANDIDATES_USDT",[]):
        v=finite(x)
        if v and loss_fraction>0:candidates[str(int(v) if v.is_integer() else v)]=round(min(float(capital_pool_usdt),v/loss_fraction),2) if capital_pool_usdt is not None else round(v/loss_fraction,2)
    cap=None if budget is None or loss_fraction<=0 else budget/loss_fraction
    if cap is not None and capital_pool_usdt is not None:cap=min(float(capital_pool_usdt),cap)
    cb=(state or {}).get("circuit_breaker") or {};quarantine=max(0.0,finite(cb.get("quarantined_cash_usdt")) or 0.0)
    risk=(state or {}).get("systemic_risk") or {};release=finite(risk.get("risk_release_fraction"))
    if release is None:release=0.0
    effective=None if cap is None else max(0.0,cap-quarantine)
    if effective is not None and risk.get("recovery_mode"):effective*=max(0.0,min(1.0,release))
    return {"budget_usdt":budget,"budget_status":status,"stress_loss_pct":float(cfg["TAIL_STRESS_LOSS_PCT"]),
            "execution_buffer_pct":float(cfg["TAIL_EXECUTION_BUFFER_PCT"]),"stress_loss_fraction_with_buffer":round(loss_fraction,4),
            "tail_cap_usdt":round(cap,2) if cap is not None else None,"quarantined_cash_usdt":round(quarantine,2),
            "systemic_recovery_release_fraction":round(release,4),"effective_tail_cap_usdt":round(effective,2) if effective is not None else None,
            "candidate_budget_caps_usdt":candidates,"realized_profit_expands_tail_budget":False}

def record_loss_exit(state,pnl,reason,released_notional,now,cfg):
    # "consecutive" means consecutive loss exits. A profitable exit breaks
    # the sequence, while quarantine/recovery state remains intact.
    if pnl>=0:
        cb=(state or {}).get("circuit_breaker")
        if cb:
            cb["consecutive_loss_exits"]=0
            cb["last_profitable_exit_at_utc"]=now.isoformat()
            cb["updated_at_utc"]=now.isoformat()
        return
    cb=state.setdefault("circuit_breaker",{"status":"NORMAL","consecutive_loss_exits":0,"quarantined_cash_usdt":0.0,"loss_events":[]})
    events=cb.setdefault("loss_events",[]);events.append({"at_utc":now.isoformat(),"net_pnl_usdt":round(float(pnl),2),"reason":reason,"released_notional_usdt":round(float(released_notional),2)})
    cutoff=now-dt.timedelta(hours=float(cfg["CIRCUIT_BREAKER_WINDOW_HOURS"]))
    events=[x for x in events if parse(x["at_utc"])>=cutoff];cb["loss_events"]=events[-50:]
    cb["consecutive_loss_exits"]=int(cb.get("consecutive_loss_exits") or 0)+1
    cb["quarantined_cash_usdt"]=round(float(cb.get("quarantined_cash_usdt") or 0)+float(released_notional),2)
    cb["quarantine_base_usdt"]=cb["quarantined_cash_usdt"]
    rolling_loss=sum(abs(float(x.get("net_pnl_usdt") or 0)) for x in events if float(x.get("net_pnl_usdt") or 0)<0)
    trip=cb["consecutive_loss_exits"]>=int(cfg["CIRCUIT_BREAKER_CONSECUTIVE_LOSSES"]) or rolling_loss>=float(cfg["CIRCUIT_BREAKER_REALIZED_LOSS_USDT"])
    # Another loss cannot downgrade an already blocking circuit to WATCH.
    cb["status"]="TRIPPED" if trip or cb.get("status") in ("TRIPPED","RECOVERING") else "WATCH"
    cb["rolling_realized_loss_usdt"]=round(rolling_loss,2);cb["recovery_observations"]=0;cb["updated_at_utc"]=now.isoformat()
    cb["last_recovery_counted_at_utc"]=None
    cb["last_recovery_counted_observed_at_utc"]=None
    cb["last_recovery_seen_observation_id"]=None
    cb["last_loss_exit_at_utc"]=now.isoformat()
    cb["last_loss_exit_observation_id"]=((state.get("systemic_risk") or {}).get("last_observation_id"))

def advance_circuit_breaker(state,systemic,evidence,now,cfg,new_observation):
    cb=state.get("circuit_breaker")
    if not cb or cb.get("status")=="NORMAL" or not new_observation:return cb
    oid=(evidence or {}).get("observation_id")
    if not oid or oid in (cb.get("last_recovery_seen_observation_id"),cb.get("last_loss_exit_observation_id")):
        return cb
    has_history="recovery_seen_observation_ids" in cb
    if not admit_recovery_observation(cb,evidence,now,cfg):return cb
    if not has_history:
        cb["recovery_observations"]=0
        cb["last_recovery_counted_at_utc"]=None
    cb["last_recovery_seen_observation_id"]=oid
    if (systemic or {}).get("raw_level")!="NORMAL":
        cb["recovery_observations"]=0;cb["last_recovery_counted_at_utc"]=None
        cb["updated_at_utc"]=now.isoformat();return cb
    last=cb.get("last_recovery_counted_at_utc")
    # Use the existing independent-observation interval; no risk parameter changes.
    min_gap=float(cfg["SYSTEMIC_RECOVERY_MIN_GAP_SECONDS"])
    if last:
        try:gap_ok=(now-parse(last)).total_seconds()>=min_gap and (parse(evidence["observed_at_utc"])-parse(cb["last_recovery_counted_observed_at_utc"])).total_seconds()>=min_gap
        except (KeyError,TypeError,ValueError):gap_ok=False
        if not gap_ok:
            cb["recovery_wait_reason"]="CIRCUIT_RECOVERY_OBSERVATION_TOO_SOON"
            cb["updated_at_utc"]=now.isoformat();return cb
    elif int(cb.get("recovery_observations") or 0)>0:
        # Old counts without a durable timestamp cannot establish independence.
        cb["recovery_observations"]=0
    cb.pop("recovery_wait_reason",None)
    cb["last_recovery_counted_at_utc"]=now.isoformat()
    cb["last_recovery_counted_observed_at_utc"]=evidence["observed_at_utc"]
    n=int(cb.get("recovery_observations") or 0)+1;cb["recovery_observations"]=n
    wait=int(cfg["CIRCUIT_RECOVERY_OBSERVATIONS_BEFORE_RELEASE"])
    if n>=wait:
        cb["status"]="RECOVERING"
        base=max(0.0,finite(cb.get("quarantine_base_usdt")) or finite(cb.get("quarantined_cash_usdt")) or 0.0)
        release=base*float(cfg["CIRCUIT_RELEASE_STEP_PCT"])/100
        cb["quarantined_cash_usdt"]=round(max(0.0,float(cb.get("quarantined_cash_usdt") or 0)-release),2)
        if cb["quarantined_cash_usdt"]<=0:
            cb["quarantined_cash_usdt"]=0.0;cb["status"]="NORMAL";cb["consecutive_loss_exits"]=0;cb["recovery_observations"]=0
    cb["updated_at_utc"]=now.isoformat();return cb

def update_risk_controls(state,evidence,now,cfg):
    systemic,new_obs=update_systemic_risk(state,evidence,now,cfg)
    advance_circuit_breaker(state,systemic,evidence,now,cfg,new_obs)
    return systemic
