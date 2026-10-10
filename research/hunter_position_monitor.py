#!/usr/bin/env python3
"""5-minute V1/V2 existing-position manager. No discovery, no new entries, no real orders."""
import datetime as dt,json,math,os,pathlib,time,urllib.request,urllib.parse
from research import hunter_shadow_trader_v2 as eng
from research import hunter_shadow_trader as v1
from research import hunter_early_signals as signals
from research import hunter_liquidity_probe as books
from research import hunter_leading_risk as leading
from research.hunter_policy import C,LANES,VERSION,stamp
from concurrent.futures import ThreadPoolExecutor,as_completed
ROOT=pathlib.Path("research/results");BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
V1=ROOT/"hunter-shadow-portfolio.json";V2=ROOT/"hunter-shadow-v2-portfolio.json";OUT=ROOT/"hunter-position-monitor.json"
V1_SUMMARY=ROOT/"hunter-shadow-summary.json";V2_SUMMARY=ROOT/"hunter-shadow-v2-summary.json";V1_GUARD=ROOT/"hunter-shadow-v1-overfilter-guard.json";V2_GUARD=ROOT/"hunter-shadow-v2-overfilter-guard.json"
REVIEW=ROOT/"hunter-tactical-capital-review.json";LIQ=ROOT/"hunter-liquidity-probe.json";SUPPLY=ROOT/"hunter-tactical-supply-risk.json";UNIVERSE=ROOT/"hunter-cex-universe-run.json";LEADING=ROOT/"hunter-leading-risk.json"
TIMINGS={}
def timed(stage,fn,*args,**kwargs):
 start=time.perf_counter()
 try:return fn(*args,**kwargs)
 finally:
  seconds=round(time.perf_counter()-start,6);TIMINGS[stage]=seconds
  print("HUNTER_STAGE_TIMING "+json.dumps({"stage":stage,"seconds":seconds}),flush=True)
def load(p):
 if p.name in eng.PORTFOLIO_NAMES:return eng.load_portfolio(p)
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {}
def get(url):
 req=urllib.request.Request(url,headers={"User-Agent":"hunter-position-monitor/3.0","Accept":"application/json"})
 with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)
def assets(states):
 # Observe open positions plus closed positions whose 72h opportunity window is
 # incomplete. This is observation-only and cannot create entries or alter exits.
 return sorted({p.get("asset") for s in states for p in ((s.get("open_positions") or [])+(s.get("closed_positions") or []))
                if p.get("asset") and (not p.get("closed_at_utc") or not p.get("observation_complete"))})
def batch_market(wanted):
 rows=get(BN+"/api/v3/ticker/24hr");out={}
 # Keep the full USDT market snapshot from the same request. Existing-position
 # management still touches only wanted assets, while systemic breadth gets a
 # fresh cross-market observation every five minutes.
 for x in rows if isinstance(rows,list) else []:
  if not isinstance(x,dict):continue
  sym=str(x.get("symbol") or "")
  if not sym.endswith("USDT") or len(sym)<=4:continue
  a=sym[:-4]
  try:p=float(x.get("lastPrice"));ch=float(x.get("priceChangePercent"))
  except (TypeError,ValueError):continue
  if math.isfinite(p) and p>0 and math.isfinite(ch):out[a]={"reference_price":p,"change_24h_pct":ch}
 return out
def v1_review():
 try:
  _,review=v1.load_early_into_review();return review
 except (OSError,ValueError,KeyError):return load(REVIEW)
def crypto_exclusions():
 u=load(UNIVERSE);xs=(((u.get("venue_status") or {}).get("binance") or {}).get("excluded_bstocks") or [])
 if not xs: raise RuntimeError("CRYPTO_SCOPE_BSTOCK_CLASSIFICATION_MISSING")
 return set(xs)
def configure_lane(v1_mode):
 if v1_mode:
  v1.configure_v1();return
 eng.STATE=V2;eng.SUMMARY=V2_SUMMARY;eng.GUARD=V2_GUARD;eng.CAPITAL_POOL_USDT=LANES["V2"]["capital_pool_usdt"];eng.ENTRY_MODE=LANES["V2"]["entry_mode"];eng.DISCOVERY_MIN_SCORE=LANES["V2"]["discovery_min_score"];eng.DISCOVERY_MIN_INDEPENDENT=C["DISCOVERY_MIN_INDEPENDENT"];eng.STRATEGY_ID=LANES["V2"]["strategy"];eng.ID_PREFIX="SHV2";eng.EVENT_PREFIX="SHADOW_V2"
def run_lane(path,label,market,review,liq,supply,now,v1_mode=False,excluded=None,regime_scan=None,risk_evidence=None):
 state=load(path);excluded=set(excluded or [])
 # Both lanes get their own bounded observation budget in this shared process.
 eng._opportunity_backfill_requests=0
 quarantine=eng.quarantine_non_crypto_history(state,excluded)
 before={p.get("asset"):len(p.get("tranches") or []) for p in state.get("open_positions") or []}
 configure_lane(v1_mode)
 if risk_evidence is not None:eng.tail.update_risk_controls(state,risk_evidence,now,eng.C)
 scan={"coins":market,"as_of_utc":(regime_scan or {}).get('as_of_utc') or now.isoformat(),"generation_id":(regime_scan or {}).get("generation_id") or "MONITOR_"+now.strftime("%Y%m%dT%H%M%S%fZ")};btc=(market.get("BTC") or {}).get("reference_price")
 if not v1_mode:scan['venue_management']=(regime_scan or {}).get('venue_management') or {}
 state['last_cycle_generation_id']=scan['generation_id']
 capital_proposals=[] if not v1_mode else None
 eng.manage_existing_positions(state,scan,review,liq,supply,now,btc,capital_proposals)
 # V2 ADDs are intentionally deferred here. The 5-minute monitor owns health/exit
 # responsiveness; capital deployment is owned by the full allocator where BUY
 # and ADD can compete in one ranked queue. V1 keeps its independent Broad Net behavior.
 deferred_adds=sorted(q.get("asset") for q in (capital_proposals or []) if q.get("kind")=="ADD")
 # Closed positions remain observation subjects through SELL+72h. Live marks may
 # extend the all-time opportunity peak; exact horizon peaks are reconstructed by
 # the bounded historical backfill, never inferred from a late current price.
 eng.refresh_closed_observations(state,now,lambda asset:(market.get(asset) or {}).get("reference_price"))
 if len(state.get("decisions") or [])>eng.MAX_DECISION_HISTORY:
  n=len(state["decisions"])-eng.MAX_DECISION_HISTORY;state["decision_history_truncated"]=int(state.get("decision_history_truncated") or 0)+n;state["decisions"]=state["decisions"][-eng.MAX_DECISION_HISTORY:]
 if len(state.get("events") or [])>eng.MAX_EVENT_HISTORY:
  n=len(state["events"])-eng.MAX_EVENT_HISTORY;state["event_history_truncated"]=int(state.get("event_history_truncated") or 0)+n;state["events"]=state["events"][-eng.MAX_EVENT_HISTORY:]
 after={p.get("asset"):len(p.get("tranches") or []) for p in state.get("open_positions") or []}
 added=sorted(a for a,n in after.items() if n>before.get(a,0));closed=sorted(set(before)-set(after))
 state["policy_version"]=VERSION;state["updated_at_utc"]=now.isoformat();eng.atomic_json_write(path,state)
 summary_path=(V1_SUMMARY if path==V1 else V2_SUMMARY if path==V2 else path.with_name(path.stem+"-summary.json"))
 guard_path=V1_GUARD if v1_mode else V2_GUARD
 eng.atomic_json_write(summary_path,eng.build_summary(state,now,guard_status=(load(guard_path).get("status") or "NORMAL"),scan=(regime_scan or scan)))
 return {"lane":label,"open":len(after),"added":added,"deferred_adds":deferred_adds,"closed":closed,"quarantined_non_crypto":quarantine["assets"],"quarantine_counts":{k:v for k,v in quarantine.items() if k!="assets"}}
def management_selection(states,cursor,budget):
 # V2 health/recovery needs consecutive fresh five-minute observations. Only
 # the independent V1 management batch rotates; discovery admission is unchanged.
 priority=sorted({p['asset'] for st in states[1:] for p in st.get('open_positions',[]) if p.get('execution_venue')!='BYBIT_SPOT'})
 ordinary=sorted({p['asset'] for st in states[:1] for p in st.get('open_positions',[])}-set(priority))
 count=min(max(0,budget-len(priority)),len(ordinary))
 selected=priority+[ordinary[(cursor+i)%len(ordinary)] for i in range(count)] if ordinary else priority
 return selected,(cursor+count)%len(ordinary) if ordinary else 0

def refresh_management_evidence(states,market,review,liq,now):
 """Refresh bounded rotating holdings, including names no longer EARLY. No entry path."""
 import copy
 review=copy.deepcopy(review);liq=copy.deepcopy(liq)
 wanted=sorted({p.get("asset") for st in states for p in st.get("open_positions") or [] if p.get("asset")})
 previous=load(OUT);cursor=int(previous.get("evidence_refresh_cursor") or 0)
 selected,next_cursor=management_selection(states,cursor,C["MONITOR_EVIDENCE_BATCH"])
 pairs=[a+"USDT" for a in selected]+["BTCUSDT"]
 generation="MONITOR_"+now.strftime("%Y%m%dT%H%M%S%fZ")
 current={};failures={};raw_books={};micro={}
 try:
  r1=timed("evidence_1h",signals.rolling,pairs,"1h");r4=timed("evidence_4h",signals.rolling,pairs,"4h");micro=timed("evidence_micro",signals.micro,pairs)
  for a in selected:
   row=signals.score_row(a+"USDT",a,r1,r4,r1.get("BTCUSDT"),r4.get("BTCUSDT"),micro)
   if row:current[a]=row
   else:failures[a]="SIGNAL_REFRESH_FAILED"
 except Exception as exc:
  failures.update({a:"SIGNAL_REFRESH_FAILED:"+type(exc).__name__ for a in selected})
 by={x.get("asset"):x for x in review.get("candidates") or []}
 def book(a):
  query=urllib.parse.urlencode({"symbol":a+"USDT","limit":100})
  raw=books.live_fetch(books.BN+"/api/v3/depth?"+query)
  row=books.measure(raw,dt.datetime.now(dt.timezone.utc),pair=a+"USDT");row["execution_scenarios"]={}
  for amount in (1000,2000,3000,4000):
   try:row["execution_scenarios"][str(amount)]=books.estimate(raw,amount)
   except ValueError:pass
  return row
 with ThreadPoolExecutor(max_workers=16) as executor:
  futures={executor.submit(book,a):a for a in selected if a in current}
  for future in as_completed(futures):
   a=futures[future]
   try:
    snapshot=future.result();liq.setdefault("snapshots",{})[a]=snapshot
    raw_books[a]=snapshot["raw_book_evidence"]
    c=by.setdefault(a,{"asset":a,"blockers":[]})
    c["signal"]=current[a];c["signal_evidence"]=stamp(a,generation,now.isoformat())
    if any(p.get('asset')==a for st in states[1:] for p in st.get('open_positions',[])):
     c['v2_lifecycle_evidence']={'generation_id':generation,'micro_receipt':(micro.get(a+'USDT') or {}).get('v2_candle_receipt'),
       'relative_receipts':{role+'_'+window:(data.get(symbol) or {}).get('v2_source_receipt')
        for window,data in (('1h',r1),('4h',r4)) for role,symbol in (('asset',a+'USDT'),('btc','BTCUSDT'))}}
    c["execution_scenario"]=snapshot["execution_scenarios"].get("3000",{})
    # Old microstructure failures must be replaced by this fresh snapshot.
    market_flags={"LIVE_ORDERBOOK_MISSING","LIVE_ORDERBOOK_STALE","LIVE_ORDERBOOK_INVALID","SPREAD_EXCEEDS_50_BPS","DEPTH_BELOW_30K_USDT","BTC_RELATIVE_SIGNAL_MISSING","SIGNAL_EVIDENCE_STALE"}
    c["blockers"]=[b for b in c.get("blockers") or [] if b not in market_flags]
    c["management_only"]=True
   except Exception as exc:failures[a]="BOOK_REFRESH_FAILED:"+type(exc).__name__
 for a in failures:
  c=by.setdefault(a,{"asset":a,"blockers":[]});c["signal_evidence"]={};c["management_refresh_error"]=failures[a]
 review["candidates"]=list(by.values())
 return review,liq,{"generation_id":generation,"observed_at_utc":now.isoformat(),"attempted":selected,"failures":failures,
                   "observed_raw_books":raw_books,
                   "v2_lifecycle_observations":{a:{'signal':by[a].get('signal'),'signal_evidence':by[a].get('signal_evidence'),
                     'lifecycle_evidence':by[a].get('v2_lifecycle_evidence')} for a in selected if by.get(a,{}).get('v2_lifecycle_evidence')},
                   "evidence_refresh_cursor":next_cursor,"policy_version":VERSION}

def main():
 TIMINGS.clear();monitor_started=time.perf_counter()
 states=[load(V1),load(V2)];wanted=assets(states);now=dt.datetime.now(dt.timezone.utc)
 if not wanted:
  eng.atomic_json_write(OUT,{"as_of_utc":now.isoformat(),"assets":[],"status":"NO_OPEN_POSITIONS","scope":"EXISTING_POSITIONS_ONLY","new_entry_enabled":False})
  print(json.dumps({"assets":[],"status":"NO_OPEN_POSITIONS"}))
  return
 market=timed('market',batch_market,wanted)
 # A Bybit-only holding must not require a Binance same-name market to exist.
 binance_states=[states[0],dict(states[1],open_positions=[p for p in states[1].get('open_positions',[]) if p.get('execution_venue')!='BYBIT_SPOT'],closed_positions=[p for p in states[1].get('closed_positions',[]) if p.get('execution_venue')!='BYBIT_SPOT'])]
 missing=sorted(set(assets(binance_states))-set(market))
 if missing:raise SystemExit("FAST_MONITOR_MARKET_DATA_MISSING "+",".join(missing))
 liq=load(LIQ);supply=load(SUPPLY);review=load(REVIEW);excluded=crypto_exclusions()
 review,liq,refresh=timed('management_evidence',refresh_management_evidence,states,market,review,liq,now)
 persisted_universe=load(UNIVERSE)
 regime_scan={"schema":"hunter_monitor_market_snapshot_v1","generation_id":refresh["generation_id"],"as_of_utc":now.isoformat(),"coins":market,"binance_complete":True}
 from research.hunter_bybit_management import collect as collect_bybit_management
 packets,bybit_failures=timed('bybit_management_evidence',collect_bybit_management,states[1].get('open_positions',[]),review,refresh['generation_id'])
 regime_scan['venue_management']={'BYBIT_SPOT':packets}
 refresh['bybit_management']={'expected_symbols':sorted(p.get('market_symbol') or p['asset'] for p in states[1].get('open_positions',[]) if p.get('execution_venue')=='BYBIT_SPOT'),'observed_symbols':sorted(packets),'failures':bybit_failures,'capital_authority':'NONE_SHADOW_ONLY'}
 # One systemic observation is shared by both lanes so V1/V2 cannot disagree
 # merely because they were evaluated a few milliseconds apart.
 risk_evidence=timed('systemic_evidence',eng.tail.collect_systemic_evidence,regime_scan,liq,now,eng.C,eng.BINANCE_DATA_API)
 # Leading Warning is observation-only in phase 1. It cannot mutate positions or
 # ordinary BUY/SELL decisions. Persisted history lets acceleration and restart
 # behavior be evaluated across independent five-minute observations.
 leading_doc=load(LEADING);previous=leading_doc.get("current") or {}
 leading_evidence=timed('leading_evidence',leading.collect_evidence,regime_scan,liq,review,risk_evidence,previous,now,eng.C)
 leading_row,leading_changed=leading.update_state(previous,leading_evidence,now,eng.C)
 v2_before=load(V2);used=sum(sum(float(t.get("notional_usdt") or 0) for t in p.get("tranches") or []) for p in v2_before.get("open_positions") or [])
 tail_cap=(eng.tail.tail_budget_snapshot(v2_before,eng.C,LANES["V2"]["capital_pool_usdt"]) or {}).get("tail_cap_usdt")
 leading_doc={"schema":"hunter_leading_risk_state_v1","updated_at_utc":now.isoformat(),"current":leading_row,
              "history":leading.update_history(leading_doc,leading_row,used,tail_cap,eng.C),
              "shadow_only":True,"real_position_mutation":False,"ordinary_buy_sell_signals_unchanged":True,
              "capital_authority":"NONE_SHADOW_ONLY"}
 eng.atomic_json_write(LEADING,leading_doc)
 # Exit responsiveness is independent of the rotating fundamental review batch.
 # Fetch all V2 books and any V1 armed/armable position before exit evaluation.
 exit_assets={p['asset'] for p in states[1].get('open_positions',[]) if p.get('execution_venue')!='BYBIT_SPOT'}
 exit_assets.update(p['asset'] for p in states[0].get('open_positions',[]) if (p.get('protection_lifecycle',{}).get('state','UNARMED')!='UNARMED' or ((market.get(p['asset']) or {}).get('reference_price') and eng.raw_return(p,market[p['asset']]['reference_price'])>=eng.PROTECT_ARM_PCT)))
 exit_failures={}
 def exit_book(asset):
  query=urllib.parse.urlencode({'symbol':asset+'USDT','limit':100})
  return books.measure(books.live_fetch(books.BN+'/api/v3/depth?'+query),dt.datetime.now(dt.timezone.utc),pair=asset+'USDT')
 with ThreadPoolExecutor(max_workers=8) as executor:
  futures={executor.submit(exit_book,a):a for a in exit_assets}
  for future in as_completed(futures):
   a=futures[future]
   try:
    snapshot=future.result();liq.setdefault('snapshots',{})[a]=snapshot
    refresh.setdefault('observed_exit_raw_books',{})[a]=snapshot['raw_book_evidence']
   except Exception as ex:
    exit_failures[a]=type(ex).__name__
    liq.setdefault('snapshots',{})[a]={}  # never reuse an old book after a failed attempt
 refresh['exit_book_refresh_failures']=exit_failures
 # Evaluate after acquisitions finish: a just-fetched book must not appear to
 # come from the future relative to the scan's initial clock snapshot.
 evaluated_at=dt.datetime.now(dt.timezone.utc)
 results=[timed("v1_lifecycle",run_lane,V1,"SHADOW_V1",market,review,liq,supply,evaluated_at,True,excluded,regime_scan,risk_evidence),timed("v2_lifecycle",run_lane,V2,"SHADOW_V2",market,review,liq,supply,evaluated_at,False,excluded,regime_scan,risk_evidence)]
 TIMINGS["monitor_total"]=round(time.perf_counter()-monitor_started,6)
 # Observe subscription drift after authoritative lifecycle updates. The resident
 # stream owns its 30-second subscription self-healing; this never writes its state.
 from research.hunter_fast_reconciliation import observe
 import subprocess
 try:
  source_sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True,timeout=5).strip()
 except (OSError,subprocess.SubprocessError):source_sha='UNKNOWN'
 fast_reconciliation=observe(load(V2),{'evidence_refresh':refresh,'batch_endpoint':'/api/v3/ticker/24hr','capital_authority':'NONE_SHADOW_ONLY'},source_sha,dt.datetime.now(dt.timezone.utc))
 eng.atomic_json_write(OUT,{"as_of_utc":now.isoformat(),"assets":wanted,"batch_endpoint":"/api/v3/ticker/24hr","scope":"EXISTING_POSITIONS_ONLY","new_entry_enabled":False,"shared_manager":"hunter_shadow_trader_v2.manage_existing_positions","results":results,"timing_seconds":dict(TIMINGS),"evidence_refresh":refresh,"fast_watch_reconciliation":fast_reconciliation,"systemic_risk_evidence":risk_evidence,"leading_risk":leading_row,"leading_risk_changed":leading_changed,"evidence_refresh_cursor":refresh["evidence_refresh_cursor"],"policy_version":VERSION,"capital_authority":"NONE_SHADOW_ONLY"})
 print(json.dumps({"assets":wanted,"results":results}))
if __name__=="__main__":main()
