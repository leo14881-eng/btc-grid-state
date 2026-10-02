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
MAX_OPEN=3
MAX_HOLD_HOURS=24.0
TARGET_PCT=8.0
INVALIDATION_PCT=-6.0
NOTIONAL=1000.0

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
    rules=rules or {}
    min_score=float(rules.get("min_score",8)); min_ind=int(rules.get("min_independent_signals",2))
    min_r1=float(rules.get("min_btc_relative_1h_pct",.8)); min_r4=float(rules.get("min_btc_relative_4h_pct",1.5))
    max_ch=float(rules.get("max_change_24h_pct",20)); max_slip=float(rules.get("max_buy_slippage_bps",75))
    out=[]
    for c in review.get("candidates") or []:
        sym=c.get("asset"); sig=c.get("signal") or {}; p=price(scan,sym)
        if not sym or sym in open_assets or not p:continue
        score=finite(sig.get("score")) or 0; r1=finite(sig.get("btc_relative_1h_pct")); r4=finite(sig.get("btc_relative_4h_pct"))
        ch=finite(((scan.get("coins") or {}).get(sym) or {}).get("change_24h_pct"))
        independent=int(sig.get("independent_signal_count") or 0)
        # Shadow lane intentionally evaluates signals even when fundamental/capital gates block live money.
        if score<min_score or independent<min_ind or r1 is None or r4 is None or (r1<min_r1 and r4<min_r4):continue
        if ch is not None and ch>max_ch:continue # anti-chase
        ex=c.get("execution_scenario") or {}
        slip=finite(ex.get("buy_slippage_bps")) or 0
        if slip>max_slip:continue
        out.append((score,sym,c,p,slip))
    out.sort(reverse=True,key=lambda x:x[0])
    return out
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
    by_key={(x.get("asset"),x.get("first_scan_generation_id")):x for x in rows}
    current_assets=set()
    for cand in review.get("candidates") or []:
        asset=cand.get("asset"); sig=cand.get("signal") or {}; p=price(scan,asset)
        if not asset or not p:continue
        current_assets.add(asset)
        key=(asset,scan.get("generation_id"))
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
    ledger=update_candidate_ledger(review,scan,now,btc)
    still=[]
    for pos in state["open_positions"]:
        p=price(scan,pos["asset"])
        if not p: still.append(pos);continue
        raw=(p/pos["entry_reference_price"]-1)*100
        pos["mfe_pct"]=round(max(pos.get("mfe_pct",0),raw),4);pos["mae_pct"]=round(min(pos.get("mae_pct",0),raw),4)
        pos["last_price"]=p;pos["last_marked_at_utc"]=now.isoformat()
        hours=(now-parse(pos["opened_at_utc"])).total_seconds()/3600
        reason=None
        if raw>=TARGET_PCT:reason="TARGET"
        elif raw<=INVALIDATION_PCT:reason="INVALIDATION"
        elif hours>=MAX_HOLD_HOURS:reason="TIME_EXIT"
        if reason:
            nr=net_return(pos["entry_reference_price"],p,pos.get("buy_slippage_bps",0),0)
            br=btc/pos["btc_entry_price"]-1
            pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":reason,
              "holding_hours":round(hours,2),"net_return_pct":round(nr*100,4),
              "net_pnl_usdt":round(NOTIONAL*nr,2),"btc_return_pct":round(br*100,4),
              "btc_relative_return_pct":round((nr-br)*100,4)})
            state["closed_positions"].append(pos);state["events"].append({"type":"SHADOW_SELL","at":now.isoformat(),"asset":pos["asset"],"reason":reason,"net_pnl_usdt":pos["net_pnl_usdt"]})
        else:still.append(pos)
    state["open_positions"]=still
    slots=max(0,MAX_OPEN-len(still));open_assets={x["asset"] for x in still}
    for score,sym,c,p,slip in choose(review,scan,open_assets,rules)[:slots]:
        pid="SH-"+now.strftime("%Y%m%dT%H%M%S")+"-"+sym+"-"+uuid.uuid4().hex[:6]
        pos={"shadow_id":pid,"asset":sym,"opened_at_utc":now.isoformat(),"scan_generation_id":scan["generation_id"],
          "entry_reference_price":p,"btc_entry_price":btc,"notional_usdt":NOTIONAL,"buy_slippage_bps":round(slip,3),
          "fee_bps_each_side":FEE_BPS,"shadow_rule_version":rule_version,"shadow_rule_snapshot":rules,"signal_score":score,"signal_snapshot":c.get("signal"),"stage":(c.get("signal") or {}).get("stage"),
          "live_capital_blockers":c.get("blockers") or [],"mfe_pct":0.0,"mae_pct":0.0,"last_price":p,"last_marked_at_utc":now.isoformat(),
          "exit_plan":{"target_pct":TARGET_PCT,"invalidation_pct":INVALIDATION_PCT,"max_hold_hours":MAX_HOLD_HOURS},
          "capital_authority":"NONE_SHADOW_ONLY"}
        state["open_positions"].append(pos);state["events"].append({"type":"SHADOW_BUY","at":now.isoformat(),"asset":sym,"shadow_id":pid,"price":p})
    state["updated_at_utc"]=now.isoformat();state["last_cycle_generation_id"]=scan["generation_id"];state["mode"]="SIMULATION_ONLY_NO_REAL_ORDERS"
    summary=build_summary(state,now)
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n");SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"shadow_open":[x["asset"] for x in state["open_positions"]],"closed_n":len(state["closed_positions"]),"summary":summary},ensure_ascii=False))
if __name__=="__main__":main()
