#!/usr/bin/env python3
"""Hunter shadow v2: staged entries and thesis invalidation. Simulation only; no exchange orders."""
import datetime as dt, json, math, pathlib, uuid
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"; REVIEW=ROOT/"hunter-tactical-capital-review.json"
STATE=ROOT/"hunter-shadow-v2-portfolio.json"; SUMMARY=ROOT/"hunter-shadow-v2-summary.json"
FEE_BPS=10.0; TRANCHES=(1000.0,1000.0,1000.0); ADD_DD=(-4.0,-8.0)
TARGET=8.0; REVIEW_HOURS=(24.0,48.0,72.0); MAX_OPEN=3

def load(p,d=None):
    try:return json.loads(p.read_text())
    except (OSError,ValueError):return {} if d is None else d
def finite(x):
    try:
        v=float(x); return v if math.isfinite(v) else None
    except (TypeError,ValueError):return None
def parse(s):return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
def price(scan,a):return finite(((scan.get("coins") or {}).get(a) or {}).get("reference_price"))
def signal(c):return (c or {}).get("signal") or {}
def thesis_alive(c):
    if not c:return True
    s=signal(c); r1=finite(s.get("btc_relative_1h_pct")); r4=finite(s.get("btc_relative_4h_pct"))
    if int(s.get("independent_signal_count") or 0)==0:return False
    return not (r1 is not None and r4 is not None and r1 < -2.0 and r4 < -3.0)
def entry_ok(c,scan):
    s=signal(c); a=c.get("asset"); r1=finite(s.get("btc_relative_1h_pct")); r4=finite(s.get("btc_relative_4h_pct"))
    ch=finite(((scan.get("coins") or {}).get(a) or {}).get("change_24h_pct"))
    slip=finite((c.get("execution_scenario") or {}).get("buy_slippage_bps")) or 0
    return bool(price(scan,a) and (finite(s.get("score")) or 0)>=8 and int(s.get("independent_signal_count") or 0)>=2
      and r1 is not None and r4 is not None and (r1>=.8 or r4>=1.5) and (ch is None or ch<=20) and slip<=75)
def weighted_entry(pos):
    n=sum(t["notional_usdt"] for t in pos["tranches"])
    return sum(t["price"]*t["notional_usdt"] for t in pos["tranches"])/n
def total_notional(pos):return sum(t["notional_usdt"] for t in pos["tranches"])
def raw_return(pos,p):return (p/weighted_entry(pos)-1)*100
def net_pnl(pos,p):
    qty=0
    for t in pos["tranches"]:
        effective=t["price"]*(1+(t.get("buy_slippage_bps",0)+FEE_BPS)/10000)
        qty+=t["notional_usdt"]/effective
    return qty*p*(1-FEE_BPS/10000)-total_notional(pos)
def should_add(pos,p,c):
    if len(pos["tranches"])>=3 or not thesis_alive(c):return False
    dd=(p/pos["tranches"][0]["price"]-1)*100
    return dd<=ADD_DD[len(pos["tranches"])-1]
def add_tranche(pos,p,c,now):
    i=len(pos["tranches"]); slip=finite(((c or {}).get("execution_scenario") or {}).get("buy_slippage_bps")) or 0
    pos["tranches"].append({"tranche":i+1,"at":now.isoformat(),"price":p,"notional_usdt":TRANCHES[i],
      "buy_slippage_bps":round(slip,3),"reason":"INITIAL" if i==0 else "LOWER_PRICE_THESIS_ALIVE"})
def build_summary(state,now):
    closed=state["closed_positions"]; gp=sum(max(0,x["net_pnl_usdt"]) for x in closed); gl=-sum(min(0,x["net_pnl_usdt"]) for x in closed)
    return {"schema":"hunter_shadow_v2_summary_v1","as_of_utc":now.isoformat(),"mode":"SIMULATION_ONLY_NO_REAL_ORDERS",
      "strategy":"THREE_TRANCHE_THESIS_INVALIDATION_AB_TEST","open_positions":len(state["open_positions"]),
      "closed_positions":len(closed),"wins":sum(x["net_pnl_usdt"]>0 for x in closed),"losses":sum(x["net_pnl_usdt"]<=0 for x in closed),
      "net_pnl_usdt":round(sum(x["net_pnl_usdt"] for x in closed),2),"profit_factor":round(gp/gl,3) if gl else ("INF" if gp else None),
      "policy":{"tranches_usdt":list(TRANCHES),"add_drawdowns_from_first_entry_pct":list(ADD_DD),"price_only_stop_loss":False,
        "target_from_weighted_average_pct":TARGET,"time_exit_enabled":False,"time_review_hours":list(REVIEW_HOURS),"max_open":MAX_OPEN},
      "capital_authority":"NONE_SHADOW_ONLY"}
def main():
    now=dt.datetime.now(dt.timezone.utc); scan=load(SCAN); review=load(REVIEW)
    if not scan.get("binance_complete") or review.get("scan_generation_id")!=scan.get("generation_id"):
        raise SystemExit("SHADOW_V2_INPUT_GENERATION_MISMATCH")
    btc=price(scan,"BTC")
    if not btc:raise SystemExit("BTC_PRICE_MISSING")
    cm={c.get("asset"):c for c in review.get("candidates") or [] if c.get("asset")}
    state=load(STATE,{"schema":"hunter_shadow_v2_portfolio_v1","mode":"SIMULATION_ONLY_NO_REAL_ORDERS","open_positions":[],"closed_positions":[],"events":[]})
    state.setdefault("open_positions",[]); state.setdefault("closed_positions",[]); state.setdefault("events",[])
    still=[]
    for pos in state["open_positions"]:
        p=price(scan,pos["asset"])
        if not p:still.append(pos);continue
        c=cm.get(pos["asset"]); raw=raw_return(pos,p)
        pos["mfe_pct"]=round(max(pos.get("mfe_pct",0),raw),4); pos["mae_pct"]=round(min(pos.get("mae_pct",0),raw),4)
        pos["last_price"]=p; pos["last_marked_at_utc"]=now.isoformat()
        if should_add(pos,p,c):
            add_tranche(pos,p,c,now); state["events"].append({"type":"SHADOW_V2_ADD","at":now.isoformat(),"asset":pos["asset"],
              "tranche":len(pos["tranches"]),"price":p,"weighted_entry":round(weighted_entry(pos),10)})
            raw=raw_return(pos,p)
        hours=(now-parse(pos["opened_at_utc"])).total_seconds()/3600; pos["holding_hours"]=round(hours,2); pos["time_review_due"]=next((h for h in REVIEW_HOURS if hours>=h and h not in pos.get("completed_time_reviews",[])),None); reason=None
        if not thesis_alive(c):reason="THESIS_INVALIDATION"
        elif raw>=TARGET:reason="TARGET_FROM_WEIGHTED_AVG"
        if reason:
            pnl=net_pnl(pos,p); notion=total_notional(pos); br=(btc/pos["btc_entry_price"]-1)*100
            pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":reason,"holding_hours":round(hours,2),
              "weighted_entry_price":round(weighted_entry(pos),10),"total_notional_usdt":notion,"net_pnl_usdt":round(pnl,2),
              "net_return_pct":round(pnl/notion*100,4),"btc_return_pct":round(br,4),"btc_relative_return_pct":round(pnl/notion*100-br,4)})
            state["closed_positions"].append(pos); state["events"].append({"type":"SHADOW_V2_SELL","at":now.isoformat(),"asset":pos["asset"],"reason":reason,"net_pnl_usdt":round(pnl,2)})
        else:still.append(pos)
    state["open_positions"]=still; open_assets={x["asset"] for x in still}; slots=max(0,MAX_OPEN-len(still))
    ranked=sorted([c for c in review.get("candidates") or [] if c.get("asset") not in open_assets and entry_ok(c,scan)],
      key=lambda c:finite(signal(c).get("score")) or 0,reverse=True)
    for c in ranked[:slots]:
        a=c["asset"]; p=price(scan,a); pid="SHV2-"+now.strftime("%Y%m%dT%H%M%S")+"-"+a+"-"+uuid.uuid4().hex[:6]
        pos={"shadow_id":pid,"asset":a,"opened_at_utc":now.isoformat(),"scan_generation_id":scan["generation_id"],"btc_entry_price":btc,
          "tranches":[],"signal_snapshot":signal(c),"mfe_pct":0.0,"mae_pct":0.0,"last_price":p,"last_marked_at_utc":now.isoformat(),
          "capital_authority":"NONE_SHADOW_ONLY"}
        add_tranche(pos,p,c,now); state["open_positions"].append(pos); state["events"].append({"type":"SHADOW_V2_BUY","at":now.isoformat(),"asset":a,"shadow_id":pid,"tranche":1,"price":p})
    state["updated_at_utc"]=now.isoformat(); state["last_cycle_generation_id"]=scan["generation_id"]
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n"); sm=build_summary(state,now)
    SUMMARY.write_text(json.dumps(sm,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"v2_open":[x["asset"] for x in state["open_positions"]],"v2_closed":len(state["closed_positions"]),"summary":sm},ensure_ascii=False))
if __name__=="__main__":main()
