#!/usr/bin/env python3
"""Hunter shadow-account forward validator. Simulation only; never places exchange orders."""
import datetime as dt,json,math,pathlib,uuid
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"
REVIEW=ROOT/"hunter-tactical-capital-review.json"
STATE=ROOT/"hunter-shadow-portfolio.json"
SUMMARY=ROOT/"hunter-shadow-summary.json"
RULES=ROOT/"hunter-shadow-rules.json"
LEDGER=ROOT/"hunter-shadow-candidate-ledger.json"
FEE_BPS=10.0
MAX_OPEN=50
MAX_HOLD_HOURS=24.0
TARGET_PCT=8.0
INVALIDATION_PCT=-6.0
NOTIONAL=1000.0
ADD_DRAWDOWNS=(-4.0,-8.0)
MAX_TRANCHES=3
PROFIT_ARM_NET_PCT=2.0
PROFIT_GIVEBACK_PCT=2.0
PROFIT_FLOOR_NET_PCT=0.35

def load(p,d=None):
    try:return json.loads(p.read_text())
    except (OSError,ValueError):return {} if d is None else d
def parse(s):return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
def finite(x):
    try:
        v=float(x);return v if math.isfinite(v) else None
    except (TypeError,ValueError):return None
def price(scan,sym):
    c=(scan.get("coins") or {}).get(sym) or {}
    return finite(c.get("reference_price"))
def net_return(entry,exitp,buy_slip_bps=0,exit_slip_bps=0):
    buy=entry*(1+(buy_slip_bps+FEE_BPS)/10000)
    sell=exitp*(1-(exit_slip_bps+FEE_BPS)/10000)
    return sell/buy-1
def active_rules():
    d=load(RULES,{})
    return d.get("active_version","shadow-v1"),d.get("rules") or {}
def choose(review,scan,open_assets,rules=None):
    """V1 learning lane: buy every observable candidate that is priceable.

    Live-capital blockers, score thresholds and anti-chase rules intentionally do not
    block this simulation lane. The point is to generate forward evidence.
    """
    out=[]
    for c in review.get("candidates") or []:
        sym=c.get("asset"); sig=c.get("signal") or {}; p=price(scan,sym)
        if not sym or sym in open_assets or not p:continue
        ex=c.get("execution_scenario") or {}
        slip=finite(ex.get("buy_slippage_bps")) or 0.0
        score=finite(sig.get("score")) or 0.0
        out.append((score,sym,c,p,slip))
    out.sort(reverse=True,key=lambda x:x[0])
    return out

def position_notional(pos):
    return sum(float(t.get("notional_usdt",0)) for t in pos.get("tranches",[])) or float(pos.get("notional_usdt",NOTIONAL))
def weighted_entry(pos):
    ts=pos.get("tranches") or [{"price":pos["entry_reference_price"],"notional_usdt":pos.get("notional_usdt",NOTIONAL)}]
    n=sum(float(t["notional_usdt"]) for t in ts)
    return sum(float(t["price"])*float(t["notional_usdt"]) for t in ts)/n
def position_net_return(pos,exitp):
    ts=pos.get("tranches") or [{"price":pos["entry_reference_price"],"notional_usdt":pos.get("notional_usdt",NOTIONAL),"buy_slippage_bps":pos.get("buy_slippage_bps",0)}]
    n=sum(float(t["notional_usdt"]) for t in ts)
    qty=sum(float(t["notional_usdt"])/(float(t["price"])*(1+(float(t.get("buy_slippage_bps",0))+FEE_BPS)/10000)) for t in ts)
    return qty*exitp*(1-FEE_BPS/10000)/n-1
def add_tranche(pos,p,now,reason):
    i=len(pos.setdefault("tranches",[]))
    pos["tranches"].append({"tranche":i+1,"at":now.isoformat(),"price":p,"notional_usdt":NOTIONAL,
      "buy_slippage_bps":0.0,"reason":reason})
def profit_exit(pos,p):
    nr=position_net_return(pos,p)*100
    mfe=float(pos.get("mfe_net_pct",0))
    armed=mfe>=PROFIT_ARM_NET_PCT
    giveback=max(0.0,mfe-nr)
    exit_now=armed and nr>0 and (giveback>=PROFIT_GIVEBACK_PCT or nr<=PROFIT_FLOOR_NET_PCT)
    return exit_now,nr,mfe,giveback

def archetype(sig,coin):
    stage=str(sig.get("stage") or "").upper()
    r1=finite(sig.get("btc_relative_1h_pct")); r4=finite(sig.get("btc_relative_4h_pct"))
    ch=finite((coin or {}).get("change_24h_pct"))
    if "PRE" in stage:return "PRE_MOVE"
    if ch is not None and ch>0 and r1 is not None and r1<0:return "PULLBACK"
    if r1 is not None and r4 is not None and r1>0 and r4>0:return "RIGHT_CONTINUATION"
    return stage or "WATCH"

def update_candidate_ledger(review,scan,now,btc):
    """Persistent forward-only discovery ledger. No real-capital authority."""
    d=load(LEDGER,{"schema":"hunter_shadow_candidate_ledger_v1","mode":"SIMULATION_ONLY_NO_REAL_ORDERS","candidates":[]})
    rows=d.setdefault("candidates",[])
    # Repair legacy duplicate-per-generation rows: keep the true earliest discovery per asset.
    earliest={}
    for x in rows:
        a=x.get("asset")
        if not a:continue
        prev=earliest.get(a)
        if prev is None or str(x.get("first_discovered_at_utc",""))<str(prev.get("first_discovered_at_utc","")):
            if prev is not None:
                x["marks"]=(prev.get("marks") or [])+(x.get("marks") or [])
                x["mfe_pct"]=max(float(prev.get("mfe_pct",0)),float(x.get("mfe_pct",0)))
                x["mae_pct"]=min(float(prev.get("mae_pct",0)),float(x.get("mae_pct",0)))
            earliest[a]=x
        elif prev is not None:
            prev["marks"]=(prev.get("marks") or [])+(x.get("marks") or [])
            prev["mfe_pct"]=max(float(prev.get("mfe_pct",0)),float(x.get("mfe_pct",0)))
            prev["mae_pct"]=min(float(prev.get("mae_pct",0)),float(x.get("mae_pct",0)))
    rows=list(earliest.values()); d["candidates"]=rows
    by_key={x.get("asset"):x for x in rows if x.get("asset")}
    current_assets=set()
    for cand in review.get("candidates") or []:
        asset=cand.get("asset"); sig=cand.get("signal") or {}; p=price(scan,asset)
        if not asset or not p:continue
        current_assets.add(asset)
        key=asset
        # One immutable discovery record per asset/generation. Repeated cycles mark it forward.
        if key not in by_key:
            ex=cand.get("execution_scenario") or {}
            slip=finite(ex.get("buy_slippage_bps")) or 0.0
            cid="CAND-"+str(scan.get("generation_id"))+"-"+asset
            rec={"candidate_id":cid,"asset":asset,"first_discovered_at_utc":now.isoformat(),
              "first_scan_generation_id":scan.get("generation_id"),"first_price":p,"btc_first_price":btc,
              "stage":sig.get("stage"),"archetype":archetype(sig,(scan.get("coins") or {}).get(asset)),
              "signal_snapshot":sig,"evidence":{"live_capital_blockers":cand.get("blockers") or []},
              "hypothesis":{"shadow_entry_price":p,"buy_slippage_bps":round(slip,3),
                "fee_bps_each_side":FEE_BPS,"target_pct":TARGET_PCT,"invalidation_pct":INVALIDATION_PCT,
                "max_hold_hours":MAX_HOLD_HOURS},"marks":[],"mfe_pct":0.0,"mae_pct":0.0,
              "capital_authority":"NONE_SHADOW_ONLY"}
            rows.append(rec);by_key[key]=rec
        else:
            by_key[key]["last_seen_generation_id"]=scan.get("generation_id")
            by_key[key]["last_seen_at_utc"]=now.isoformat()
    # Mark every historical candidate when its asset is still priceable in the current scan.
    for rec in rows:
        p=price(scan,rec.get("asset"))
        if not p:continue
        opened=parse(rec["first_discovered_at_utc"]); hours=(now-opened).total_seconds()/3600
        raw=(p/rec["first_price"]-1)*100; btc_ret=(btc/rec["btc_first_price"]-1)*100
        rec["mfe_pct"]=round(max(float(rec.get("mfe_pct",0)),raw),4)
        rec["mae_pct"]=round(min(float(rec.get("mae_pct",0)),raw),4)
        rec["last_price"]=p;rec["last_marked_at_utc"]=now.isoformat()
        done={m.get("window") for m in rec.get("marks",[])}
        for label,threshold in (("1h",1),("6h",6),("24h",24)):
            if label in done or hours<threshold:continue
            nr=net_return(rec["first_price"],p,rec["hypothesis"].get("buy_slippage_bps",0),0)*100
            rec["marks"].append({"window":label,"marked_at_utc":now.isoformat(),"elapsed_hours":round(hours,2),
              "price":p,"net_return_pct":round(nr,4),"btc_return_pct":round(btc_ret,4),
              "btc_relative_return_pct":round(nr-btc_ret,4),"mfe_pct":rec["mfe_pct"],"mae_pct":rec["mae_pct"],
              "invalidated":raw<=INVALIDATION_PCT,"target_hit":raw>=TARGET_PCT})
    d["updated_at_utc"]=now.isoformat();d["last_cycle_generation_id"]=scan.get("generation_id")
    LEDGER.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n")
    return d

def build_summary(state,now):
    closed=state["closed_positions"]; wins=[x for x in closed if x["net_return_pct"]>0]
    gross_profit=sum(max(0,x["net_pnl_usdt"]) for x in closed); gross_loss=-sum(min(0,x["net_pnl_usdt"]) for x in closed)
    return {"schema":"hunter_shadow_summary_v1","as_of_utc":now.isoformat(),"mode":"SIMULATION_ONLY_NO_REAL_ORDERS",
      "open_positions":len(state["open_positions"]),"closed_positions":len(closed),
      "wins":len(wins),"losses":len(closed)-len(wins),
      "win_rate_pct":round(100*len(wins)/len(closed),2) if closed else None,
      "net_pnl_usdt":round(sum(x["net_pnl_usdt"] for x in closed),2),
      "profit_factor":round(gross_profit/gross_loss,3) if gross_loss>0 else (None if gross_profit==0 else "INF"),
      "avg_net_return_pct":round(sum(x["net_return_pct"] for x in closed)/len(closed),4) if closed else None,
      "avg_btc_relative_return_pct":round(sum(x["btc_relative_return_pct"] for x in closed)/len(closed),4) if closed else None,
      "max_adverse_excursion_pct":round(min([x["mae_pct"] for x in closed]+[0]),4),
      "policy":{"notional_usdt":NOTIONAL,"fee_bps_each_side":FEE_BPS,"target_pct":TARGET_PCT,
                "invalidation_pct":INVALIDATION_PCT,"max_hold_hours":MAX_HOLD_HOURS,"max_open":MAX_OPEN},
      "capital_authority":"NONE_SHADOW_ONLY"}
def main():
    now=dt.datetime.now(dt.timezone.utc);scan=load(SCAN);review=load(REVIEW);rule_version,rules=active_rules()
    if not scan.get("binance_complete") or review.get("scan_generation_id")!=scan.get("generation_id"):
        raise SystemExit("SHADOW_INPUT_GENERATION_MISMATCH")
    state=load(STATE,{"schema":"hunter_shadow_portfolio_v1","created_at_utc":now.isoformat(),"open_positions":[],"closed_positions":[],"events":[]})
    state.setdefault("open_positions",[]);state.setdefault("closed_positions",[]);state.setdefault("events",[])
    btc=price(scan,"BTC")
    if not btc:raise SystemExit("BTC_PRICE_MISSING")
    update_candidate_ledger(review,scan,now,btc)
    still=[]
    for pos in state["open_positions"]:
        p=price(scan,pos["asset"])
        if not p:still.append(pos);continue
        # Migrate legacy one-shot positions to tranche accounting without rewriting history.
        if not pos.get("tranches"):
            pos["tranches"]=[{"tranche":1,"at":pos["opened_at_utc"],"price":pos["entry_reference_price"],
              "notional_usdt":pos.get("notional_usdt",NOTIONAL),"buy_slippage_bps":pos.get("buy_slippage_bps",0),"reason":"LEGACY_INITIAL"}]
        avg=weighted_entry(pos);raw=(p/avg-1)*100;nr=position_net_return(pos,p)*100
        pos["weighted_entry_price"]=round(avg,12);pos["mfe_pct"]=round(max(pos.get("mfe_pct",0),raw),4)
        pos["mae_pct"]=round(min(pos.get("mae_pct",0),raw),4);pos["mfe_net_pct"]=round(max(pos.get("mfe_net_pct",0),nr),4)
        pos["last_price"]=p;pos["last_marked_at_utc"]=now.isoformat()
        # V1 explicitly studies averaging down: add at -4% and -8% from first tranche.
        if len(pos["tranches"])<MAX_TRANCHES:
            first=float(pos["tranches"][0]["price"]);dd=(p/first-1)*100;threshold=ADD_DRAWDOWNS[len(pos["tranches"])-1]
            if dd<=threshold:
                add_tranche(pos,p,now,"V1_AVERAGE_DOWN_"+str(threshold))
                state["events"].append({"type":"SHADOW_ADD","at":now.isoformat(),"asset":pos["asset"],"price":p,"drawdown_from_first_pct":round(dd,4)})
                avg=weighted_entry(pos);nr=position_net_return(pos,p)*100;pos["weighted_entry_price"]=round(avg,12)
        exit_now,nr,mfe_net,giveback=profit_exit(pos,p)
        if exit_now:
            br=btc/pos["btc_entry_price"]-1;n=position_notional(pos);pnl=n*nr/100
            pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":"PROFIT_GIVEBACK",
              "holding_hours":round((now-parse(pos["opened_at_utc"])).total_seconds()/3600,2),
              "net_return_pct":round(nr,4),"net_pnl_usdt":round(pnl,2),"total_notional_usdt":n,
              "btc_return_pct":round(br*100,4),"btc_relative_return_pct":round(nr-br*100,4),
              "profit_exit":{"mfe_net_pct":round(mfe_net,4),"giveback_pct":round(giveback,4)}})
            state["closed_positions"].append(pos)
            state["events"].append({"type":"SHADOW_SELL","at":now.isoformat(),"asset":pos["asset"],"reason":"PROFIT_GIVEBACK",
              "net_pnl_usdt":round(pnl,2),"mfe_net_pct":round(mfe_net,4),"giveback_pct":round(giveback,4),
              "USER_ALERT_REQUIRED":True})
        else:still.append(pos)
    state["open_positions"]=still
    slots=max(0,MAX_OPEN-len(still));open_assets={x["asset"] for x in still}
    for score,sym,c,p,slip in choose(review,scan,open_assets,rules)[:slots]:
        pid="SH-"+now.strftime("%Y%m%dT%H%M%S")+"-"+sym+"-"+uuid.uuid4().hex[:6]
        pos={"shadow_id":pid,"asset":sym,"opened_at_utc":now.isoformat(),"scan_generation_id":scan["generation_id"],
          "entry_reference_price":p,"btc_entry_price":btc,"notional_usdt":NOTIONAL,"buy_slippage_bps":round(slip,3),
          "tranches":[{"tranche":1,"at":now.isoformat(),"price":p,"notional_usdt":NOTIONAL,"buy_slippage_bps":round(slip,3),"reason":"OBSERVATION_ENTRY"}],
          "fee_bps_each_side":FEE_BPS,"shadow_rule_version":"V1_BROAD_LEARNING","signal_score":score,
          "signal_snapshot":c.get("signal"),"stage":(c.get("signal") or {}).get("stage"),
          "entry_conditions":{"change_24h_pct":((scan.get("coins") or {}).get(sym) or {}).get("change_24h_pct"),
             "live_capital_blockers":c.get("blockers") or [],"execution_scenario":c.get("execution_scenario")},
          "mfe_pct":0.0,"mae_pct":0.0,"mfe_net_pct":0.0,"last_price":p,"last_marked_at_utc":now.isoformat(),
          "exit_plan":{"profit_arm_net_pct":PROFIT_ARM_NET_PCT,"profit_giveback_pct":PROFIT_GIVEBACK_PCT,
             "profit_floor_net_pct":PROFIT_FLOOR_NET_PCT,"loss_exit":False,"average_down_drawdowns_pct":list(ADD_DRAWDOWNS)},
          "capital_authority":"NONE_SHADOW_ONLY"}
        state["open_positions"].append(pos);open_assets.add(sym)
        state["events"].append({"type":"SHADOW_BUY","at":now.isoformat(),"asset":sym,"shadow_id":pid,"price":p,
          "entry_conditions":pos["entry_conditions"]})
    state["updated_at_utc"]=now.isoformat();state["last_cycle_generation_id"]=scan["generation_id"];state["mode"]="SIMULATION_ONLY_NO_REAL_ORDERS"
    summary=build_summary(state,now)
    summary["policy"].update({"broad_observation_entry":True,"average_down_drawdowns_pct":list(ADD_DRAWDOWNS),
      "max_tranches":MAX_TRANCHES,"profit_arm_net_pct":PROFIT_ARM_NET_PCT,"profit_giveback_pct":PROFIT_GIVEBACK_PCT,
      "profit_floor_net_pct":PROFIT_FLOOR_NET_PCT,"loss_exit":False,"sell_alert_required":True})
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n");SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"shadow_open":[x["asset"] for x in state["open_positions"]],"closed_n":len(state["closed_positions"]),
      "profit_giveback_sells":[e for e in state["events"][-100:] if e.get("type")=="SHADOW_SELL" and e.get("reason")=="PROFIT_GIVEBACK"],"summary":summary},ensure_ascii=False))

if __name__=="__main__":main()

# shadow-v2 parallel validation trigger
