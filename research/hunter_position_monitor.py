#!/usr/bin/env python3
"""5-minute V1/V2 position monitor. No discovery, no new entries, no real orders."""
import datetime as dt,json,math,os,pathlib,urllib.parse,urllib.request
from research import hunter_shadow_trader_v2 as eng
ROOT=pathlib.Path("research/results"); BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
V1=ROOT/"hunter-shadow-portfolio.json"; V2=ROOT/"hunter-shadow-v2-portfolio.json"; OUT=ROOT/"hunter-position-monitor.json"
def load(p):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {}
def get(url):
 req=urllib.request.Request(url,headers={"User-Agent":"hunter-position-monitor/2.0","Accept":"application/json"})
 with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)
def assets(states):return sorted({p.get("asset") for s in states for p in (s.get("open_positions") or []) if p.get("asset")})
def batch_market(wanted):
 rows=get(BN+"/api/v3/ticker/24hr");by={x.get("symbol"):x for x in rows if isinstance(x,dict)};out={}
 for a in sorted(set(wanted)|{"BTC"}):
  x=by.get(a+"USDT") or {}
  try:p=float(x.get("lastPrice"));ch=float(x.get("priceChangePercent"))
  except (TypeError,ValueError):continue
  if math.isfinite(p) and p>0:out[a]={"reference_price":p,"change_24h_pct":ch}
 return out
def short_returns(wanted):
 out={}
 for a in sorted(set(wanted)|{"BTC"}):
  q=urllib.parse.urlencode({"symbol":a+"USDT","interval":"5m","limit":13});rows=get(BN+"/api/v3/klines?"+q)
  if not isinstance(rows,list) or len(rows)<13:continue
  closes=[float(x[4]) for x in rows]
  out[a]={"r5":(closes[-1]/closes[-2]-1)*100,"r15":(closes[-1]/closes[-4]-1)*100,"r60":(closes[-1]/closes[-13]-1)*100}
 return out
def relative(a,short):
 x=short.get(a) or {};b=short.get("BTC") or {}
 if not x or not b:return {}
 return {"btc_rel_5m":x["r5"]-b["r5"],"btc_rel_15m":x["r15"]-b["r15"],"btc_rel_60m":x["r60"]-b["r60"]}
def monitor(path,label,market,short,now):
 state=load(path);btc=(market.get("BTC") or {}).get("reference_price");opens=[];closed=state.setdefault("closed_positions",[]);events=state.setdefault("events",[])
 for pos in state.get("open_positions") or []:
  a=pos.get("asset");p=(market.get(a) or {}).get("reference_price")
  if not p:opens.append(pos);continue
  raw=eng.raw_return(pos,p);pos["mfe_pct"]=round(max(float(pos.get("mfe_pct") or 0),raw),4);pos["mae_pct"]=round(min(float(pos.get("mae_pct") or 0),raw),4)
  pos["last_price"]=p;pos["last_marked_at_utc"]=now.isoformat();pos["holding_hours"]=round((now-eng.parse(pos["opened_at_utc"])).total_seconds()/3600,2)
  protection=eng.profit_protection(pos,p);rel=relative(a,short)
  action="EXIT" if protection["exit"] else "HOLD";reason="PROFIT_PROTECTION" if protection["exit"] else "FAST_MARK"
  pos["fast_monitor"]={"at_utc":now.isoformat(),"action":action,"reason":reason,"price":p,**rel}
  if action=="EXIT":
   pnl=eng.net_pnl(pos,p);notion=eng.total_notional(pos);br=((btc/pos["btc_entry_price"]-1)*100) if btc and pos.get("btc_entry_price") else 0
   pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":reason+"_5M","weighted_entry_price":eng.weighted_entry(pos),"total_notional_usdt":notion,"net_pnl_usdt":round(pnl,2),"net_return_pct":round(pnl/notion*100,4),"btc_return_pct":round(br,4),"btc_relative_return_pct":round(pnl/notion*100-br,4),"profit_protection":protection})
   pos.update(eng.exit_analysis(pos,p,pos["exit_reason"]));closed.append(pos);events.append({"type":label+"_SELL","at":now.isoformat(),"asset":a,"shadow_id":pos.get("shadow_id"),"price":p,"reason":pos["exit_reason"],"net_pnl_usdt":round(pnl,2)})
  else:opens.append(pos)
 state["open_positions"]=opens;state["updated_at_utc"]=now.isoformat();eng.atomic_json_write(path,state)
 return {"lane":label,"open":len(opens),"closed":len(closed)}
def main():
 states=[load(V1),load(V2)];wanted=assets(states);now=dt.datetime.now(dt.timezone.utc)
 if not wanted:eng.atomic_json_write(OUT,{"as_of_utc":now.isoformat(),"assets":[],"status":"NO_OPEN_POSITIONS"});return
 market=batch_market(wanted);missing=sorted(set(wanted)-set(market))
 if missing:raise SystemExit("FAST_MONITOR_MARKET_DATA_MISSING "+",".join(missing))
 short=short_returns(wanted)
 if "BTC" not in short:raise SystemExit("FAST_MONITOR_BTC_KLINE_MISSING")
 results=[monitor(V1,"SHADOW_V1",market,short,now),monitor(V2,"SHADOW_V2",market,short,now)]
 eng.atomic_json_write(OUT,{"as_of_utc":now.isoformat(),"assets":wanted,"batch_endpoint":"/api/v3/ticker/24hr","short_horizon":"5m/15m/60m","results":results})
 print(json.dumps({"assets":wanted,"results":results},ensure_ascii=False))
if __name__=="__main__":main()
