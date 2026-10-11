#!/usr/bin/env python3
"""Hunter shadow v2 capital-decision engine. Forward simulation only; never places exchange orders."""
import datetime as dt,json,math,os,pathlib,uuid,urllib.parse,urllib.request,subprocess
try:
 from research.hunter_policy import C,LANES,VERSION,POLICY,fresh,stamp,chase_blockers
 from research import hunter_tail_risk as tail
 from research import hunter_loss_freeze as loss_freeze
 from research import hunter_lifecycle_state as lifecycle
 from research.hunter_execution_identity import identity_fields
 from research.hunter_portfolio_integrity import PORTFOLIO_NAMES,load_portfolio,require_nonempty_history_transition
except ModuleNotFoundError as exc:
 if exc.name != 'research':raise
 from hunter_policy import C,LANES,VERSION,POLICY,fresh,stamp,chase_blockers
 import hunter_tail_risk as tail
 import hunter_loss_freeze as loss_freeze
 import hunter_lifecycle_state as lifecycle
 from hunter_execution_identity import identity_fields
 from hunter_portfolio_integrity import PORTFOLIO_NAMES,load_portfolio,require_nonempty_history_transition
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"; REVIEW=ROOT/"hunter-tactical-capital-review.json"
LIQ=ROOT/"hunter-liquidity-probe.json"; SUPPLY=ROOT/"hunter-tactical-supply-risk.json"
STATE=ROOT/"hunter-shadow-v2-portfolio.json"; SUMMARY=ROOT/"hunter-shadow-v2-summary.json"; GUARD=ROOT/"hunter-shadow-v2-overfilter-guard.json"; BYBIT=ROOT/"hunter-bybit-availability.json"
NEW_VERSION_CUTOFF_UTC=dt.datetime(2026,10,4,17,0,0,tzinfo=dt.timezone.utc)
MIN_RR=C["MIN_RR"]
MAX_SPREAD_BPS=C["MAX_SPREAD_BPS"]
MIN_DEPTH_USDT=C["MIN_DEPTH_USDT"]
MAX_SLIP_BPS=C["MAX_SLIP_BPS"]
TARGET=C["TARGET"]
PROTECT_ARM_PCT=C["PROTECT_ARM_PCT"]
GIVEBACK_MAX_PCT=C["GIVEBACK_MAX_PCT"]
MIN_PROTECTED_NET_PCT=C["MIN_PROTECTED_NET_PCT"]
MAX_CHASE_24H_PCT=C["MAX_CHASE_24H_PCT"]
MAX_CHASE_FROM_DISCOVERY_PCT=C["MAX_CHASE_FROM_DISCOVERY_PCT"]
MIN_CHASE_RR=C["MIN_CHASE_RR"]
MIN_CHASE_REL_1H=C["MIN_CHASE_REL_1H"]
MIN_CHASE_REL_4H=C["MIN_CHASE_REL_4H"]
OVERFILTER_ZERO_BUY_CYCLES=C["OVERFILTER_ZERO_BUY_CYCLES"]
OVERFILTER_LOOKBACK=C["OVERFILTER_LOOKBACK"]
OVERFILTER_MISSED_MOVE_PCT=C["OVERFILTER_MISSED_MOVE_PCT"]
OVERFILTER_MIN_SAFE_MISSES=C["OVERFILTER_MIN_SAFE_MISSES"]
MAX_DECISION_HISTORY=C["MAX_DECISION_HISTORY"]
MAX_EVENT_HISTORY=C["MAX_EVENT_HISTORY"]
MAX_DEFERRED_HISTORY=C["MAX_DEFERRED_HISTORY"]
MAX_CLOSED_HOT=C["MAX_CLOSED_HOT"]
DEGRADE_CONFIRM_CYCLES=C["DEGRADE_CONFIRM_CYCLES"]
HARD_SPREAD_BPS=C["HARD_SPREAD_BPS"]
HARD_MIN_DEPTH_USDT=C["HARD_MIN_DEPTH_USDT"]
REENTRY_PULLBACK_PCT=C["REENTRY_PULLBACK_PCT"]
REENTRY_BREAKOUT_PCT=C["REENTRY_BREAKOUT_PCT"]
MIN_SCORE=C["MIN_SCORE"]
MIN_REL_1H=C["MIN_REL_1H"]
MIN_REL_4H=C["MIN_REL_4H"]
MIN_ACCEL=C["MIN_ACCEL"]
MAX_EVIDENCE_AGE_SECONDS=C["MAX_EVIDENCE_AGE_SECONDS"]
EARLY_VOLUME_ACCEL=C["EARLY_VOLUME_ACCEL"]
EARLY_COMPRESSION=C["EARLY_COMPRESSION"]
EARLY_TURN_REL=C["EARLY_TURN_REL"]
MONITOR_EVIDENCE_BATCH=C["MONITOR_EVIDENCE_BATCH"]
FEE_BPS=C["FEE_BPS"]
TRANCHES=tuple(C["TRANCHES"])
REVIEW_HOURS=tuple(C["REVIEW_HOURS"])
DISCOVERY_MIN_INDEPENDENT=C["DISCOVERY_MIN_INDEPENDENT"]
CAPITAL_POOL_USDT=LANES["V2"]["capital_pool_usdt"]
DYNAMIC_RESERVE_USDT=3000.0
HARD_CASH_FLOOR_PCT=0.05
MARKET_UTILIZATION={"RISK_OFF":0.60,"NEUTRAL":0.75,"CONSTRUCTIVE":0.85,"STRONG":0.95}
ADD_MIN_MATERIAL_IMPROVEMENT_PCT=0.75
ADD_VOLATILITY_FRACTION=0.25
ADD_MAX_DYNAMIC_IMPROVEMENT_PCT=3.0
ADD_MIN_EVIDENCE_GAP_MINUTES=15
ADD_THIRD_TRANCHE_MULTIPLIER=1.5
ADD_THIRD_MAX_BTC_REL_4H_WEAKNESS=-1.0
DISCOVERY_MIN_SCORE=LANES["V2"]["discovery_min_score"]
ENTRY_MODE=LANES["V2"]["entry_mode"]
STRATEGY_ID=LANES["V2"]["strategy"]
ID_PREFIX="SHV2"
EVENT_PREFIX="SHADOW_V2"
SHADOW_FREEZE=os.getenv("HUNTER_SHADOW_FREEZE","1")!="0"
BINANCE_DATA_API=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
OPPORTUNITY_BACKFILL_BUDGET=int(os.getenv("HUNTER_OPPORTUNITY_BACKFILL_BUDGET","8"))
_opportunity_backfill_requests=0


def risk_blocks_new(state,now):
 return loss_freeze.risk_blocks_new(state,now)

def update_risk_controls(state,evidence,now):
 return loss_freeze.update_risk_controls(state,evidence,now,C)

def loss_quarantine(state):
 return loss_freeze.quarantine(state)

def entry_allowed(mode,broad,current_price,executable_action):
 return broad=="BUY" and current_price is not None and (mode=="DISCOVERY" or executable_action=="BUY")

def authoritative_entry_action(c, fallback_action):
 # V2 consumes Capital Review's final entry action. V1 discovery deliberately
 # bypasses it. Legacy fixtures without trade_action retain fallback behavior.
 if ENTRY_MODE!="EXECUTABLE": return fallback_action
 a=(c or {}).get("trade_action")
 return a if a in ("BUY","WAIT","REJECT","SYSTEM_BLOCKED") else "SYSTEM_BLOCKED"

def used_capital(state):
 return sum(total_notional(x) for x in state.get("open_positions",[]) if x.get("tranches"))
def realized_net_pnl(state):
 rows=(state.get("closed_trade_archive") or [])+(state.get("closed_positions") or [])
 return sum(float(x.get("net_pnl_usdt") or 0) for x in rows)
def capital_equity(state):
 # Initial V2 principal compounds only with realized net PnL. Unrealized PnL never expands capacity.
 return None if CAPITAL_POOL_USDT is None else max(0.0,float(CAPITAL_POOL_USDT)+realized_net_pnl(state))
def market_regime(scan):
 btc=((scan.get("coins") or {}).get("BTC") or {});b=finite(btc.get("change_24h_pct"))
 # Existing allocator thresholds were calibrated on Binance. Research-only
 # Bybit additions must not silently alter ordinary capital utilization.
 vals=[finite(x.get("change_24h_pct")) for k,x in (scan.get("coins") or {}).items()
       if k!="BTC" and (not x.get("venues") or "binance" in x["venues"])]
 vals=[x for x in vals if x is not None];negative=(sum(x<0 for x in vals)/len(vals)) if vals else None
 if b is None or negative is None:regime="RISK_OFF"
 elif b<=-2 or negative>=.60:regime="RISK_OFF"
 elif b<1 or negative>=.45:regime="NEUTRAL"
 elif b<3 or negative>=.30:regime="CONSTRUCTIVE"
 else:regime="STRONG"
 return regime,{"btc_change_24h_pct":b,"negative_breadth":round(negative,4) if negative is not None else None,"max_utilization":MARKET_UTILIZATION[regime]}

def capital_limit(state,scan=None):
 equity=capital_equity(state)
 if equity is None:return None
 regime,meta=market_regime(scan or {});util=min(meta["max_utilization"],1-HARD_CASH_FLOOR_PCT)
 market_cap=max(0.0,equity*util)
 # Tail stress calibration is a diagnostic, not a deployment/reserve budget.
 # Retain existing market/cash-floor controls and the fixed principal ceiling.
 quarantine=loss_quarantine(state)
 ceiling=max(0,min(market_cap,float(CAPITAL_POOL_USDT),equity-quarantine))
 risk=state.get('systemic_risk') or {}
 if risk.get('recovery_mode'):ceiling*=max(0,min(1,finite(risk.get('risk_release_fraction')) or 0))
 return ceiling

def reserve_snapshot(state):
 used=used_capital(state)
 strategic=sum(float(t.get('strategic_notional_usdt',0)) for p in state.get('open_positions',[]) for t in p.get('tranches',[]))
 return {'capital_pool':CAPITAL_POOL_USDT,'ordinary_opportunity_cap':17000 if CAPITAL_POOL_USDT is not None else None,'strategic_reserve':3000 if CAPITAL_POOL_USDT is not None else None,'strategic_reserve_used':round(strategic,2),'strategic_reserve_available':round(max(0,3000-strategic),2) if CAPITAL_POOL_USDT is not None else None,'ordinary_used':round(used-strategic,2),'ordinary_available':round(max(0,17000-(used-strategic)),2) if CAPITAL_POOL_USDT is not None else None,'tail_budget_role':'STRESS_DIAGNOSTIC_NOT_CAPITAL_ALLOCATION'}

def capital_available(state,amount,purpose="BUY",scan=None):
 limit=capital_limit(state,scan)
 return True if limit is None else used_capital(state)+amount<=limit+1e-9 and reserve_snapshot(state)['ordinary_used']+amount<=17000+1e-9

def opportunity_priority(e,kind):
 e=e or {};score=finite(e.get("score")) or 0.0;rr=finite(e.get("estimated_rr")) or 0.0
 r1=finite(e.get("btc_rel_1h")) or 0.0;r4=finite(e.get("btc_rel_4h")) or 0.0;acc=finite(e.get("rel_accel")) or 0.0
 friction=((finite(e.get("spread_bps")) or 0.0)+(finite(e.get("buy_slippage_bps")) or 0.0))/100.0
 return round(score+2*rr+0.25*r1+0.5*r4+0.5*acc-friction-(0.25 if kind=="ADD" else 0.0),6)

def marginal_capital_gate(state,amount,e,kind,scan):
 equity=capital_equity(state)
 if equity is None:return True,["CAPITAL_UNBOUNDED_TEST_MODE"]
 regime,meta=market_regime(scan);after_used=used_capital(state)+amount;after=after_used/equity if equity else 1.0;reasons=[]
 if after>min(meta["max_utilization"],1-HARD_CASH_FLOOR_PCT)+1e-12:reasons.append("MARKET_REGIME_CAPACITY_LIMIT")
 limit=capital_limit(state,scan)
 if limit is not None and after_used>limit+1e-9:
  reasons.append("FIXED_POOL_OR_MARKET_CAPACITY_LIMIT")
 rr=finite((e or {}).get("estimated_rr"));r1=finite((e or {}).get("btc_rel_1h"));r4=finite((e or {}).get("btc_rel_4h"));acc=finite((e or {}).get("rel_accel"))
 if after>0.60 and (rr is None or rr<1.8 or r4 is None or r4<0):reasons.append("MARGINAL_EDGE_INSUFFICIENT_ABOVE_60PCT")
 if after>0.75 and (rr is None or rr<2.2 or r4 is None or r4<=0 or acc is None or acc<=0):reasons.append("MARGINAL_EDGE_INSUFFICIENT_ABOVE_75PCT")
 if after>0.85 and (rr is None or rr<2.6 or r1 is None or r1<=0 or r4 is None or r4<0.5 or acc is None or acc<=0):reasons.append("MARGINAL_EDGE_INSUFFICIENT_ABOVE_85PCT")
 reserve=reserve_snapshot(state);strategic_needed=max(0,amount-max(0,17000-reserve['ordinary_used']))
 if reserve['ordinary_used']>17000+1e-9:reasons.append('LEGACY_ORDINARY_OVER_CAP_NO_NEW_ALLOCATION')
 if strategic_needed>reserve['strategic_reserve_available']+1e-9:reasons.append('STRATEGIC_RESERVE_CAPACITY_LIMIT')
 # Reuse the existing high-utilization edge qualification, never add an
 # ordinary BUY filter. Reserve access needs this existing strongest standard.
 if strategic_needed and (rr is None or rr<2.6 or r1 is None or r1<=0 or r4 is None or r4<.5 or acc is None or acc<=0):reasons.append('STRATEGIC_RESERVE_EXISTING_HIGH_QUALITY_EDGE_REQUIRED')
 return (not reasons),(reasons or ["PORTFOLIO_CAPITAL_ALLOCATOR_PASS",f"MARKET_REGIME_{regime}",f"POST_TRADE_UTILIZATION_{after:.4f}"])

def capital_snapshot(state,scan=None):
 equity=capital_equity(state);used=used_capital(state);regime,meta=market_regime(scan or {});limit=capital_limit(state,scan)
 risk=tail.tail_budget_snapshot({**state,'circuit_breaker':{'quarantined_cash_usdt':loss_quarantine(state)}},C,CAPITAL_POOL_USDT) if equity is not None else None
 controls=loss_freeze.control_snapshot(state)
 if equity is None:return {**controls,"initial_capital_usdt":None,"realized_net_pnl_usdt":round(realized_net_pnl(state),2),"equity_usdt":None,"used_capital_usdt":round(used,2),"market_regime":regime,"market_regime_evidence":meta,"max_deployable_usdt":None,"allocator_available_usdt":None,"systemic_risk":state.get("systemic_risk")}
 return {**reserve_snapshot(state),**controls,"initial_capital_usdt":float(CAPITAL_POOL_USDT),"realized_net_pnl_usdt":round(realized_net_pnl(state),2),"equity_usdt":round(equity,2),"used_capital_usdt":round(used,2),"market_regime":regime,"market_regime_evidence":meta,"hard_cash_floor_pct":HARD_CASH_FLOOR_PCT,"max_deployable_usdt":round(limit,2),"allocator_available_usdt":round(max(0.0,min(limit-used,17000-reserve_snapshot(state)['ordinary_used'])),2),"strategic_allocator_available_usdt":round(max(0,min(limit-used,reserve_snapshot(state)['strategic_reserve_available'])),2),"total_cash_usdt":round(max(0.0,equity-used),2),"tail_risk_budget":risk,"systemic_risk":state.get("systemic_risk")}

def load(p,d=None):
 if p.name in PORTFOLIO_NAMES:return load_portfolio(p)
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {} if d is None else d
def atomic_json_write(p,obj):
 # Never expose a partially-written portfolio/summary to a concurrent reader.
 if p.name in PORTFOLIO_NAMES:require_nonempty_history_transition(p,obj)
 tmp=p.with_suffix(p.suffix+".tmp")
 tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n")
 json.loads(tmp.read_text())
 tmp.replace(p)
def finite(x):
 try:
  v=float(x);return v if math.isfinite(v) else None
 except (TypeError,ValueError):return None
def parse(s):return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
def price(scan,a):return finite(((scan.get("coins") or {}).get(a) or {}).get("reference_price"))
def sig(c):return (c or {}).get("signal") or {}
def liq_for(liq,a):return (liq.get("snapshots") or {}).get(a) or {}
def supply_for(supply,a):return (supply.get("assets") or {}).get(a)
def confirmed_supply_risk(sr):
 if not sr:return False
 status=str(sr.get("status") or "").upper()
 # Missing/incomplete supply evidence is review metadata, not a trading blocker.
 # Only an explicit, confirmed material near-term unlock/new-circulation risk may veto.
 return bool(sr.get("confirmed_major_near_term_unlock") is True or status in ("CONFIRMED_MAJOR_NEAR_TERM_UNLOCK","CONFIRMED_MAJOR_NEAR_TERM_SUPPLY_RISK","MAJOR_NEAR_TERM_UNLOCK_CONFIRMED"))
def hard_blockers(c):
 # Portfolio usage is intentionally ignored in shadow-only simulation; evidence/execution blockers are not.
 return [x for x in ((c or {}).get("blockers") or []) if x!="PORTFOLIO_USAGE_REQUIRES_CURRENT_INPUT"]
def evidence(c,liq,supply):
 a=(c or {}).get("asset"); s=sig(c); l=liq_for(liq,a); sr=supply_for(supply,a)
 ex=(c or {}).get("execution_scenario") or {}
 return {"asset":a,"score":finite(s.get("score")),"independent":int(s.get("independent_signal_count") or 0),
  "btc_rel_1h":finite(s.get("btc_relative_1h_pct")),"btc_rel_4h":finite(s.get("btc_relative_4h_pct")),
  "rel_accel":finite(s.get("relative_acceleration_pct")),"spread_bps":finite(l.get("spread_bps")),
  "bid_depth_2pct_usdt":finite(l.get("bid_depth_2pct_usdt")),"ask_depth_2pct_usdt":finite(l.get("ask_depth_2pct_usdt")),
  "buy_slippage_bps":finite(ex.get("buy_slippage_bps")),"estimated_rr":finite(ex.get("estimated_rr")),
  "supply_verified":bool(sr and sr.get("tactical_supply_risk_verified")),"supply_status":(sr or {}).get("status"),"supply_confirmed_major_risk":confirmed_supply_risk(sr),
  "return_1h":finite(s.get("return_1h_pct")),"return_4h":finite(s.get("return_4h_pct")),
  "blockers":hard_blockers(c),"signal_evidence":(c or {}).get("signal_evidence"),"book_observed_at_utc":l.get("as_of_utc")}
def bybit_channel(bybit,a):
 spot=bybit.get("spot") or {}; alpha=bybit.get("alpha") or {}; a=str(a or "").upper()
 spot_v=(a in set(spot.get("symbols") or [])) if str(spot.get("status") or "").startswith("OK") else None
 alpha_v=(a in set(alpha.get("symbols") or [])) if str(alpha.get("status") or "").startswith("OK") else None
 if spot_v is True:return {"channel":"BYBIT_SPOT","spot":True,"alpha":alpha_v}
 if alpha_v is True:return {"channel":"BYBIT_ALPHA","spot":spot_v,"alpha":True}
 if spot_v is False and alpha_v is False:return {"channel":"NOT_ON_BYBIT","spot":False,"alpha":False}
 return {"channel":"UNKNOWN","spot":spot_v,"alpha":alpha_v}

def discovery_decision(c):
 """Broad forward-sample gate. Execution/liquidity/supply evidence is recorded, not used to erase research samples."""
 if not c:return "REJECT",["CANDIDATE_MISSING"]
 e=sig(c); reasons=[]
 if (finite(e.get("score")) or 0)<DISCOVERY_MIN_SCORE:reasons.append("DISCOVERY_SCORE_WEAK")
 if int(e.get("independent_signal_count") or 0)<DISCOVERY_MIN_INDEPENDENT:reasons.append("DISCOVERY_INDEPENDENT_SIGNALS_WEAK")
 # V1 discovery is an experiment lane: missing identity evidence is a label, not
 # proof of a bad asset. Only explicit mismatch/invalid identity blocks discovery.
 # V2 remains fail-closed because decision() still consumes every hard blocker.
 identity_blockers=[x for x in hard_blockers(c) if "IDENTITY" in x or "CONTRACT" in x]
 fatal_tokens=("MISMATCH","INVALID","WRONG_ASSET","CONFLICT")
 blockers=[x for x in identity_blockers if any(t in x for t in fatal_tokens)]
 if blockers:reasons += ["BLOCKER:"+x for x in blockers]
 return ("BUY" if not reasons else "REJECT"),(reasons or ["BROAD_DISCOVERY_GATE_PASS"])

def market_shock(scan):
 btc=((scan.get("coins") or {}).get("BTC") or {}); b=finite(btc.get("change_24h_pct"))
 vals=[finite(x.get("change_24h_pct")) for x in (scan.get("coins") or {}).values()
       if not x.get("venues") or "binance" in x["venues"]]
 vals=[x for x in vals if x is not None]; negative=(sum(x<0 for x in vals)/len(vals)) if vals else 0
 return bool(b is not None and b<=-2 and negative>=.60),{"btc_change_24h_pct":b,"negative_breadth":round(negative,4)}

def discovery_anchor(c):
 for k in ("first_discovery_price","first_price","discovery_price"):
  v=finite((c or {}).get(k))
  if v:return v
 return None
def add_material_improvement_required_pct(e,tranche_count):
 # Dynamic threshold: recent 1h movement approximates current noise; execution friction sets a second floor.
 r1=abs(finite((e or {}).get("return_1h")) or 0.0)
 friction_bps=max(finite((e or {}).get("spread_bps")) or 0.0,0.0)+max(finite((e or {}).get("buy_slippage_bps")) or 0.0,0.0)
 dynamic=max(ADD_MIN_MATERIAL_IMPROVEMENT_PCT,min(ADD_MAX_DYNAMIC_IMPROVEMENT_PCT,r1*ADD_VOLATILITY_FRACTION),friction_bps/100*2)
 if tranche_count>=2:dynamic*=ADD_THIRD_TRANCHE_MULTIPLIER
 return round(dynamic,4)

def add_fresh_evidence(pos,e):
 if not pos or not pos.get("tranches"):return False,["ADD_CONTEXT_MISSING"]
 last=pos["tranches"][-1]; meta=(e or {}).get("signal_evidence") or {}
 eid=meta.get("evidence_id"); gen=meta.get("generation_id"); observed=meta.get("observed_at_utc"); book=(e or {}).get("book_observed_at_utc")
 if not eid or not gen or not observed or not book:return False,["ADD_FRESH_EVIDENCE_INCOMPLETE"]
 if eid==last.get("signal_evidence_id") or gen==last.get("signal_generation_id"):return False,["ADD_SIGNAL_EVIDENCE_NOT_NEW"]
 try:
  last_at=parse(last.get("at")); obs_at=parse(observed); book_at=parse(book)
 except Exception:return False,["ADD_EVIDENCE_TIME_INVALID"]
 if obs_at<=last_at or book_at<=last_at:return False,["ADD_EVIDENCE_NOT_AFTER_LAST_TRANCHE"]
 if (obs_at-last_at).total_seconds()<ADD_MIN_EVIDENCE_GAP_MINUTES*60:return False,["ADD_WAIT_NEW_15M_EVIDENCE_WINDOW"]
 return True,["ADD_FRESH_EVIDENCE_CONFIRMED"]

def add_recovery_confirmed(e,tranche_count):
 r1=(e or {}).get("btc_rel_1h");r4=(e or {}).get("btc_rel_4h");acc=(e or {}).get("rel_accel")
 confirmations=sum((acc is not None and acc>0,r1 is not None and r1>=0,r4 is not None and r4>=0))
 if confirmations<2:return False,["ADD_RECOVERY_NOT_CONFIRMED"]
 if tranche_count>=2 and (acc is None or acc<=0 or r1 is None or r1<0 or r4 is None or r4<ADD_THIRD_MAX_BTC_REL_4H_WEAKNESS):
  return False,["ADD_THIRD_TRANCHE_RECOVERY_NOT_STRONG_ENOUGH"]
 return True,["ADD_RECOVERY_CONFIRMED"]

def decision(c,scan,liq,supply,kind="ENTRY",pos=None,p=None):
 if not c:
  # Dropping out of the current shortlist is not itself a thesis failure.
  # Existing shadow positions wait for fresh evidence instead of being force-sold.
  return ("HOLD" if kind!="ENTRY" else "REJECT"),["CANDIDATE_EVIDENCE_MISSING_REVIEW_ONLY"],{}
 e=evidence(c,liq,supply); reasons=[]
 if e["score"] is None or e["score"]<C["MIN_SCORE"]:reasons.append("SCORE_WEAK")
 if e["independent"]<2:reasons.append("INSUFFICIENT_INDEPENDENT_SIGNALS")
 if e["btc_rel_1h"] is None or e["btc_rel_4h"] is None:reasons.append("BTC_RELATIVE_MISSING")
 elif e["btc_rel_1h"]<C["MIN_REL_1H"] and e["btc_rel_4h"]<C["MIN_REL_4H"]:reasons.append("BTC_RELATIVE_WEAK")
 if e["rel_accel"] is not None and e["rel_accel"]<-1:reasons.append("RELATIVE_MOMENTUM_DECELERATING")
 if e["spread_bps"] is None or e["spread_bps"]>MAX_SPREAD_BPS:reasons.append("SPREAD_UNACCEPTABLE")
 depths=[e["bid_depth_2pct_usdt"],e["ask_depth_2pct_usdt"]]
 if any(x is None or x<MIN_DEPTH_USDT for x in depths):reasons.append("DEPTH_INSUFFICIENT")
 if e["buy_slippage_bps"] is None or e["buy_slippage_bps"]>MAX_SLIP_BPS:reasons.append("SLIPPAGE_UNACCEPTABLE")
 if e["estimated_rr"] is None or e["estimated_rr"]<MIN_RR:reasons.append("RR_BELOW_MINIMUM")
 if e["supply_confirmed_major_risk"]:reasons.append("CONFIRMED_MAJOR_NEAR_TERM_SUPPLY_RISK")
 if e["blockers"]:reasons+=["BLOCKER:"+x for x in e["blockers"]]
 ch=finite(((scan.get("coins") or {}).get(e["asset"]) or {}).get("change_24h_pct"))
 if kind=="ENTRY":
  if ch is not None and ch>MAX_CHASE_24H_PCT:
   # A strong right-side continuation is allowed only when the higher entry price
   # is compensated by materially stronger evidence and remaining reward/risk.
   chase_ok=(e["estimated_rr"] is not None and e["estimated_rr"]>=MIN_CHASE_RR and
             e["btc_rel_1h"] is not None and e["btc_rel_1h"]>=MIN_CHASE_REL_1H and
             e["btc_rel_4h"] is not None and e["btc_rel_4h"]>=MIN_CHASE_REL_4H and
             e["rel_accel"] is not None and e["rel_accel"]>0)
   if not chase_ok:reasons.append("CHASE_NOT_COMPENSATED_BY_EDGE")
  anchor=discovery_anchor(c)
  cur=p if p is not None else finite(((scan.get("coins") or {}).get(e["asset"]) or {}).get("reference_price"))
  if anchor and cur:
   chase_from_discovery=(cur/anchor-1)*100
   if chase_from_discovery>MAX_CHASE_FROM_DISCOVERY_PCT and (e["estimated_rr"] is None or e["estimated_rr"]<MIN_CHASE_RR):
    reasons.append("TOO_FAR_ABOVE_DISCOVERY_FOR_REMAINING_RR")
 if kind!="ENTRY" and e["btc_rel_1h"] is not None and e["btc_rel_4h"] is not None and e["btc_rel_1h"]<-2 and e["btc_rel_4h"]<-3:
  # Relative weakness is evidence for review, not sufficient proof that the thesis failed.
  reasons.append("SEVERE_BTC_RELATIVE_WEAKNESS_REVIEW")
 if reasons:
  if kind!="ENTRY":return "HOLD",reasons,e
  return "REJECT",reasons,e
 if kind=="ADD":
  if pos is None or p is None:return "REJECT",["ADD_CONTEXT_MISSING"],e
  last_price=finite((pos.get("tranches") or [{}])[-1].get("price"))
  if last_price is None:return "HOLD",["ADD_LAST_TRANCHE_PRICE_MISSING"],e
  # ADD is a new opportunity, not mechanical averaging down: it must beat the last fill by a material,
  # volatility-aware amount and be supported by fresh post-fill evidence plus an actual recovery signal.
  if p>=last_price:return "HOLD",["ADD_NOT_BELOW_LAST_TRANCHE"],e
  improvement=(last_price-p)/last_price*100
  required=add_material_improvement_required_pct(e,len(pos["tranches"]))
  if improvement<required:return "HOLD",[f"ADD_PRICE_IMPROVEMENT_TOO_SMALL_{improvement:.2f}PCT",f"ADD_REQUIRED_IMPROVEMENT_{required:.2f}PCT"],e
  if e["score"] is None or e["score"]<DISCOVERY_MIN_SCORE:return "HOLD",["ADD_THESIS_SCORE_NOT_REVALIDATED"],e
  if e["independent"]<DISCOVERY_MIN_INDEPENDENT:return "HOLD",["ADD_THESIS_SIGNALS_NOT_REVALIDATED"],e
  fresh_ok,fresh_reasons=add_fresh_evidence(pos,e)
  if not fresh_ok:return "HOLD",fresh_reasons,e
  recovery_ok,recovery_reasons=add_recovery_confirmed(e,len(pos["tranches"]))
  if not recovery_ok:return "HOLD",recovery_reasons,e
  return "ADD",["MATERIAL_BETTER_THAN_LAST_TRANCHE","THESIS_REVALIDATED"]+fresh_reasons+recovery_reasons+[f"LAST_TRANCHE_IMPROVEMENT_{improvement:.2f}PCT",f"REQUIRED_IMPROVEMENT_{required:.2f}PCT"],e
 return ("BUY" if kind=="ENTRY" else "HOLD"),["FULL_EVIDENCE_VALIDATED"],e
def weighted_entry(pos):
 n=sum(t["notional_usdt"] for t in pos["tranches"]);return sum(t["price"]*t["notional_usdt"] for t in pos["tranches"])/n
def total_notional(pos):return sum(t["notional_usdt"] for t in pos["tranches"])
def raw_return(pos,p):return (p/weighted_entry(pos)-1)*100
def net_pnl(pos,p):
 fee=pos.get('execution_fee_bps',FEE_BPS)
 qty=sum(t["notional_usdt"]/(t["price"]*(1+(t.get("buy_slippage_bps",0)+fee)/10000)) for t in pos["tranches"])
 return qty*p*(1-fee/10000)-total_notional(pos)
def scenario_returns(pos,p):
 out={}
 for n in range(1,len(pos.get("tranches",[]))+1):
  q={**pos,"tranches":pos["tranches"][:n]}
  notion=total_notional(q); pnl=net_pnl(q,p)
  out[f"{n}_tranche"]={"notional_usdt":notion,"weighted_entry_price":weighted_entry(q),"net_pnl_usdt":round(pnl,2),"net_return_pct":round(pnl/notion*100,4)}
 return out

def binance_kline_bars(asset,start,end,interval="5m"):
 """Observation-only historical bars. Failure never changes trading decisions."""
 global _opportunity_backfill_requests
 if not asset or not start or not end or end<=start:return None
 if _opportunity_backfill_requests>=OPPORTUNITY_BACKFILL_BUDGET:return None
 _opportunity_backfill_requests+=1
 symbol=str(asset).upper()+"USDT";cursor=int(start.timestamp()*1000);stop=int(end.timestamp()*1000);out=[]
 try:
  while cursor<=stop:
   q=urllib.parse.urlencode({"symbol":symbol,"interval":interval,"startTime":cursor,"endTime":stop,"limit":1000})
   req=urllib.request.Request(BINANCE_DATA_API+"/api/v3/klines?"+q,headers={"User-Agent":"hunter-opportunity-observer/1.0"})
   with urllib.request.urlopen(req,timeout=4) as r:rows=json.loads(r.read().decode())
   if not rows:break
   out.extend((int(row[0]),int(row[6]),finite(row[2])) for row in rows if finite(row[2]) is not None)
   nxt=int(rows[-1][0])+1
   if nxt<=cursor or len(rows)<1000:break
   cursor=nxt
  return out or None
 except Exception:return None

def peak_from_bars(bars,end=None):
 if not bars:return None
 stop=int(end.timestamp()*1000) if end else None
 # Historical evaluation must never use a candle whose HIGH was not fully known at the cutoff.
 # New tuples are (open_ms, close_ms, high); legacy 2-tuples remain accepted by tests/old callers.
 eligible=[]
 for x in bars:
  if len(x)>=3:
   open_ms,close_ms,high=x[0],x[1],x[2]
   if stop is None or close_ms<=stop:eligible.append((open_ms,high))
  elif stop is None or x[0]<=stop:eligible.append((x[0],x[1]))
 if not eligible:return None
 ts,p=max(eligible,key=lambda x:x[1])
 return {"peak_price":p,"peak_at_utc":dt.datetime.fromtimestamp(ts/1000,dt.timezone.utc).isoformat(),"source":"BINANCE_SPOT_KLINES_5M_COMPLETED_ONLY"}

def backfill_opportunity_history(pos,now):
 """Bounded historical reconstruction of initial-BUY opportunity peaks; observation only."""
 if pos.get('execution_venue')=='BYBIT_SPOT':
  pos['historical_opportunity_execution_status']='UNKNOWN_VENUE_MATCHED_HISTORY_NOT_INTEGRATED'
  return pos
 try:opened=parse(pos.get("opened_at_utc"))
 except Exception:return pos
 closed=parse(pos["closed_at_utc"]) if pos.get("closed_at_utc") else None
 end=min(now,closed+dt.timedelta(hours=max(REVIEW_HOURS))) if closed else now
 last=pos.get("opportunity_backfill_at_utc")
 if last:
  try:
   due_missing=bool(closed and any(now>=closed+dt.timedelta(hours=h) and f"{int(h)}h" not in (pos.get("post_exit_observation") or {}) for h in REVIEW_HOURS))
   if (now-parse(last)).total_seconds()<3300 and not due_missing:return pos
  except Exception:pass
 if _opportunity_backfill_requests>=OPPORTUNITY_BACKFILL_BUDGET:
  pos["data_provenance"]="BACKFILL_PENDING";return pos
 bars=binance_kline_bars(pos.get("asset"),opened,end);buy=initial_buy_price(pos);sell=finite(pos.get("exit_reference_price"))
 full=peak_from_bars(bars,end);hold=peak_from_bars(bars,closed or end)
 if not bars or not full or not hold or not buy:
  if pos.get("data_provenance")!="HISTORICAL_BACKFILL":
   pos["data_provenance"]="INSUFFICIENT_HISTORY"
   # Legacy mfe_pct used the then-current weighted entry and cannot be relabeled as
   # initial-BUY opportunity MFE without reliable historical bars.
   if sample_cohort(pos)=="MIGRATION_SAMPLE":
    pos["holding_mfe_pct"]=None;pos["holding_peak_price"]=None;pos["holding_peak_at_utc"]=None
    pos["full_opportunity_mfe_pct"]=None;pos["full_opportunity_peak_price"]=None;pos["full_opportunity_peak_at_utc"]=None
  pos["opportunity_backfill_at_utc"]=now.isoformat();return pos
 pos["holding_peak_price"]=hold["peak_price"];pos["holding_peak_at_utc"]=hold["peak_at_utc"];pos["holding_mfe_pct"]=round((hold["peak_price"]/buy-1)*100,4)
 pos["full_opportunity_peak_price"]=full["peak_price"];pos["full_opportunity_peak_at_utc"]=full["peak_at_utc"];pos["full_opportunity_mfe_pct"]=round((full["peak_price"]/buy-1)*100,4)
 pos["data_provenance"]="HISTORICAL_BACKFILL";pos["opportunity_backfill_source"]=full["source"];pos["opportunity_backfill_at_utc"]=now.isoformat()
 if not closed:
  pos["observation_complete"]=False
  return pos
 obs=pos.setdefault("post_exit_observation",{})
 for target in REVIEW_HOURS:
  horizon_end=closed+dt.timedelta(hours=target)
  if now<horizon_end:continue
  h=peak_from_bars([x for x in bars if x[0]>=int(closed.timestamp()*1000)],horizon_end)
  if h:
   obs[f"{int(target)}h"]={"observed_at_utc":now.isoformat(),"max_price":h["peak_price"],
    "max_return_from_initial_buy_pct":round((h["peak_price"]/buy-1)*100,4),
    "max_return_from_sell_pct":round((h["peak_price"]/sell-1)*100,4) if sell else None,"peak_at_utc":h["peak_at_utc"],
    "data_provenance":"HISTORICAL_BACKFILL","price_granularity":"5m_completed_candles"}
 pos["observation_complete"]=bool(now>=closed+dt.timedelta(hours=max(REVIEW_HOURS)) and obs.get("72h"))
 refresh_post_exit_status(pos,now)
 pos["holding_profit_capture_ratio"]=capture_ratio(pos.get("net_return_pct"),pos.get("holding_mfe_pct"))
 pos["full_opportunity_capture_ratio"]=capture_ratio(pos.get("net_return_pct"),pos.get("full_opportunity_mfe_pct"))
 pos["exit_evaluation"]=exit_evaluation(pos)
 return pos

def refresh_post_exit_status(pos,now):
 """Expose only reconstructed post-sell peaks as evaluated results."""
 if not pos.get("closed_at_utc"):return
 closed=parse(pos["closed_at_utc"]);obs=pos.get("post_exit_observation") or {}
 due=[f"{int(h)}h" for h in REVIEW_HOURS if now>=closed+dt.timedelta(hours=h)]
 missing=[h for h in due if (obs.get(h) or {}).get("max_return_from_sell_pct") is None]
 completed=[h for h in due if h not in missing]
 pos["post_exit_evaluation"]={"status":"COMPLETE" if pos.get("observation_complete") else "BACKFILL_PENDING" if missing else "OBSERVING",
  "due_horizons":due,"completed_horizons":completed,"missing_horizons":missing,
  "max_return_from_sell_pct":max((obs[h]["max_return_from_sell_pct"] for h in completed),default=None),
  "source":"HISTORICAL_BACKFILL" if completed else None}

def refresh_closed_observations(state,now,market_price=None):
 """Fair, bounded backfill across closed trades; never creates trade events."""
 closed=state.get("closed_positions") or []
 if not closed:return
 for pos in closed:
  if pos.get("observation_complete"):
   refresh_post_exit_status(pos,now);continue
  p=market_price(pos.get("asset")) if market_price and pos.get('execution_venue')!='BYBIT_SPOT' else None
  if p:update_post_exit(pos,p,now)
  else:refresh_post_exit_status(pos,now)
 cursor=int(state.get("observation_backfill_cursor") or 0)%len(closed)
 visited=0
 while visited<len(closed) and _opportunity_backfill_requests<OPPORTUNITY_BACKFILL_BUDGET:
  pos=closed[(cursor+visited)%len(closed)];visited+=1
  if not pos.get("closed_at_utc") or pos.get("observation_complete"):continue
  due=(pos.get("post_exit_evaluation") or {}).get("missing_horizons") or []
  last=pos.get("opportunity_backfill_at_utc")
  periodic=not pos.get("observation_complete") and (not last or (now-parse(last)).total_seconds()>=3300)
  if due or periodic:backfill_opportunity_history(pos,now)
 state["observation_backfill_cursor"]=(cursor+visited)%len(closed)

def sample_cohort(pos):
 try:return "NEW_VERSION_SAMPLE" if parse(pos.get("opened_at_utc"))>=NEW_VERSION_CUTOFF_UTC else "MIGRATION_SAMPLE"
 except Exception:return "UNKNOWN"

def initial_buy_price(pos):
 ts=pos.get("tranches") or []
 return finite(ts[0].get("price")) if ts else finite(pos.get("weighted_entry_price"))

def capture_ratio(realized,mfe):
 r=finite(realized);m=finite(mfe)
 if r is None or m is None or m<=0:return None
 # Profit capture is a [0,+inf) research ratio: a losing/breakeven exit captured
 # none of a positive opportunity. Preserve the loss separately in net_return_pct.
 if r<=0:return 0.0
 return round(r/m,6)

def ensure_opportunity_observation(pos,p=None,now=None,provenance="LIVE_OBSERVATION"):
 now=now or dt.datetime.now(dt.timezone.utc);buy=initial_buy_price(pos)
 pos.setdefault("sample_cohort",sample_cohort(pos))
 # Legacy mfe_pct is based on the then-current weighted entry after ADDs.  It is
 # not valid evidence for the new initial-BUY opportunity metric.  Only new-version
 # positions may seed from live MFE; migration samples require historical bars.
 if pos.get("holding_mfe_pct") is None and pos.get("mfe_pct") is not None and sample_cohort(pos)=="NEW_VERSION_SAMPLE":
  pos["holding_mfe_pct"]=finite(pos.get("mfe_pct"))
 if pos.get("holding_peak_price") is None and buy and pos.get("holding_mfe_pct") is not None:
  pos["holding_peak_price"]=round(buy*(1+float(pos["holding_mfe_pct"])/100),12)
 if pos.get("holding_peak_at_utc") is None and pos.get("holding_peak_price") is not None:pos["holding_peak_at_utc"]=pos.get("last_marked_at_utc") or pos.get("opened_at_utc")
 if pos.get("full_opportunity_peak_price") is None:
  seed=max([x for x in (buy,finite(pos.get("holding_peak_price")),finite(pos.get("exit_reference_price"))) if x is not None],default=None)
  pos["full_opportunity_peak_price"]=seed
  pos["full_opportunity_peak_at_utc"]=pos.get("holding_peak_at_utc") or pos.get("opened_at_utc")
 if p and buy:
  closed_at=parse(pos["closed_at_utc"]) if pos.get("closed_at_utc") else None
  inside_full_window=(closed_at is None or now<=closed_at+dt.timedelta(hours=max(REVIEW_HOURS)))
  if closed_at is None:
   if p>(finite(pos.get("holding_peak_price")) or 0):
    pos["holding_peak_price"]=p;pos["holding_peak_at_utc"]=now.isoformat()
   pos["holding_mfe_pct"]=round((pos["holding_peak_price"]/buy-1)*100,4)
  # Full Opportunity is strictly initial BUY -> SELL+72h. A monitor tick after
  # that deadline is never allowed to extend the evaluation window.
  if inside_full_window and p>(finite(pos.get("full_opportunity_peak_price")) or 0):
   pos["full_opportunity_peak_price"]=p;pos["full_opportunity_peak_at_utc"]=now.isoformat()
  if pos.get("full_opportunity_peak_price") is not None:
   pos["full_opportunity_mfe_pct"]=round((pos["full_opportunity_peak_price"]/buy-1)*100,4)
 pos.setdefault("data_provenance",provenance)
 if pos.get("closed_at_utc"):
  # Time passing alone is not evidence that the full 72h opportunity was observed.
  # Completion requires a truthful 72h horizon record (normally historical backfill).
  pos["observation_complete"]=bool((pos.get("post_exit_observation") or {}).get("72h"))
 else:pos["observation_complete"]=False
 return pos

def exit_evaluation(pos):
 if not pos.get("closed_at_utc"):return "OBSERVING"
 if not pos.get("observation_complete"):return "OBSERVING"
 full=finite(pos.get("full_opportunity_mfe_pct"));hold=finite(pos.get("holding_mfe_pct"));net=finite(pos.get("net_return_pct"));t=pos.get("post_exit_observation") or {}
 cont=max([finite((t.get(f"{int(h)}h") or {}).get("max_return_from_sell_pct")) for h in REVIEW_HOURS if finite((t.get(f"{int(h)}h") or {}).get("max_return_from_sell_pct")) is not None] or [0])
 if full is None or hold is None or net is None:return "INSUFFICIENT_DATA"
 if net>0 and full>=10 and cont>=8 and (capture_ratio(net,full) or 1)<.4:return "POTENTIAL_PREMATURE_EXIT"
 if hold>=2 and net<=0:return "PROFIT_GIVEBACK"
 if net>0 and (capture_ratio(net,hold) or 0)>=.6:return "GOOD_PROFIT_CAPTURE"
 return "NORMAL_EXIT"

def exit_analysis(pos,p,reason):
 scenarios=scenario_returns(pos,p); final=scenarios.get(f"{len(pos.get('tranches',[]))}_tranche",{})
 net=final.get("net_return_pct",0)
 if net>=0:failure=None
 elif reason=="THESIS_INVALIDATION":failure="THESIS_OR_SELECTION_FAILURE"
 elif reason=="PROFIT_PROTECTION":failure="PROFIT_GIVEBACK_FAILURE"
 else:failure="EXIT_OR_SELECTION_REVIEW"
 ensure_opportunity_observation(pos,p)
 return {"tranche_scenarios_at_exit":scenarios,"failure_attribution":failure,
  "post_exit_tracking":{"hours":list(REVIEW_HOURS),"max_rebound_from_exit_pct":0.,"potential_premature_exit":False,"marks":[]},
  "post_exit_observation":{}}

def update_post_exit(pos,p,now):
 ensure_opportunity_observation(pos,p,now)
 if not pos.get("closed_at_utc") or not p:return
 t=pos.setdefault("post_exit_tracking",{"hours":list(REVIEW_HOURS),"max_rebound_from_exit_pct":0.,"potential_premature_exit":False,"marks":[]})
 t["status"]="PROVISIONAL_SAMPLED_PRICE_NOT_PEAK"
 obs=pos.setdefault("post_exit_observation",{})
 hours=(now-parse(pos["closed_at_utc"])).total_seconds()/3600
 if hours<0:return
 sell=finite(pos.get("exit_reference_price"));buy=initial_buy_price(pos)
 rebound=(p/sell-1)*100 if sell else None
 if rebound is not None:t["max_rebound_from_exit_pct"]=round(max(finite(t.get("max_rebound_from_exit_pct")) or 0,rebound),4)
 for target in REVIEW_HOURS:
  key=f"{int(target)}h"
  if hours>=target and key not in obs:
   # A late monitor tick cannot truthfully reconstruct an earlier horizon from the
   # current all-time peak. Leave the horizon pending for bounded historical backfill.
   t["marks"].append({"horizon":key,"observed_hours":round(hours,2),"price":p,"rebound_from_exit_pct":round(rebound,4) if rebound is not None else None,"status":"HISTORICAL_BACKFILL_REQUIRED"})
 # Never mark the evaluation complete from elapsed time alone. A late monitor tick
 # cannot reconstruct the maximum price inside the 72h window.
 pos["observation_complete"]=bool(obs.get("72h"))
 pos["holding_profit_capture_ratio"]=capture_ratio(pos.get("net_return_pct"),pos.get("holding_mfe_pct"))
 pos["full_opportunity_capture_ratio"]=capture_ratio(pos.get("net_return_pct"),pos.get("full_opportunity_mfe_pct"))
 pos["exit_evaluation"]=exit_evaluation(pos)
 t["potential_premature_exit"]=pos["exit_evaluation"]=="POTENTIAL_PREMATURE_EXIT"
 refresh_post_exit_status(pos,now)

def profit_protection(pos,p):
 raw=raw_return(pos,p); mfe=finite(pos.get("mfe_pct")) or 0.
 armed=mfe>=PROTECT_ARM_PCT
 giveback=max(0.,mfe-raw)
 # Once a real profit window existed, do not deliberately let a shadow winner become a loser.
 # Exit review is triggered either near breakeven after costs or after excessive giveback.
 protect_floor=MIN_PROTECTED_NET_PCT+(2*pos.get('execution_fee_bps',FEE_BPS))/100.
 return {"armed":armed,"raw_pct":raw,"mfe_pct":mfe,"giveback_pct":giveback,"protect_floor_pct":protect_floor,
  "exit":bool(armed and (raw<=protect_floor or giveback>=GIVEBACK_MAX_PCT))}

def position_health(pos,e,now=None,systemic_level="NORMAL",confirmed_systemic_shock=False):
 # Ordinary signal decay is not a stop-loss. It only becomes thesis invalidation
 # after several consecutive multi-factor weak observations.
 old_state=pos.get('health_state')
 meta=e.get('signal_evidence') or {}
 if meta and not fresh(meta.get('observed_at_utc'),now or dt.datetime.now(dt.timezone.utc)):
  return 'EVIDENCE_PENDING',['SIGNAL_EVIDENCE_MISSING_OR_STALE']
 previous=pos.get('last_health_observed_at_utc')
 if meta and previous and parse(meta['observed_at_utc'])<=parse(previous):
  return 'EVIDENCE_PENDING',['SIGNAL_EVIDENCE_NOT_NEWER']
 hard=[]
 if e.get("supply_confirmed_major_risk"):hard.append("CONFIRMED_MAJOR_NEAR_TERM_SUPPLY_RISK")
 for b in e.get("blockers") or []:
  u=str(b).upper()
  if any(t in u for t in ("MISMATCH","INVALID","WRONG_ASSET","CONFLICT")):hard.append("FATAL_IDENTITY_OR_CONTRACT:"+str(b))
 sp=e.get("spread_bps");depths=[e.get("bid_depth_2pct_usdt"),e.get("ask_depth_2pct_usdt")]
 book_fresh=fresh(e.get("book_observed_at_utc"),now or dt.datetime.now(dt.timezone.utc))
 liquidity_hard=[]
 if book_fresh and sp is not None and sp>HARD_SPREAD_BPS:liquidity_hard.append("CATASTROPHIC_SPREAD")
 if book_fresh and all(x is not None for x in depths) and min(depths)<HARD_MIN_DEPTH_USDT:liquidity_hard.append("CATASTROPHIC_DEPTH")
 # During a confirmed/fail-closed systemic shock, cross-market liquidity collapse
 # is not proof that every individual asset died. Fatal identity/contract/supply
 # evidence above remains actionable; liquidity-only loss exits are suppressed.
 if confirmed_systemic_shock and systemic_level in ("HIGH","CRITICAL") and liquidity_hard:
  pos["systemic_liquidity_suppressed_at_utc"]=(now or dt.datetime.now(dt.timezone.utc)).isoformat()
  pos["systemic_liquidity_suppressed_reasons"]=liquidity_hard
 else:hard+=liquidity_hard
 if hard:
  if meta.get('generation_id'):
   if pos.get('last_health_generation_id')==meta['generation_id']:return 'EVIDENCE_PENDING',['SIGNAL_EVIDENCE_ALREADY_CONSUMED']
   pos.update(last_health_generation_id=meta['generation_id'],last_health_evidence_id=meta.get('evidence_id'),last_health_observed_at_utc=meta.get('observed_at_utc'))
  pos["health_state"]="HARD_INVALIDATION";pos["health_reasons"]=hard
  return "HARD_INVALIDATION",hard
 meta=e.get("signal_evidence") or {}
 if not meta.get("evidence_id") or not fresh(meta.get("observed_at_utc"),now or dt.datetime.now(dt.timezone.utc)):
  return "EVIDENCE_PENDING",["SIGNAL_EVIDENCE_MISSING_OR_STALE"]
 if pos.get("last_health_evidence_id")==meta["evidence_id"] or (meta.get("generation_id") and pos.get("last_health_generation_id")==meta["generation_id"]):
  return "EVIDENCE_PENDING",["SIGNAL_EVIDENCE_ALREADY_CONSUMED"]
 # Start a new counter for pre-fix positions; old ticks cannot count as new observations.
 if not pos.get("last_health_evidence_id"):pos["degraded_cycles"]=0
 previous=pos.get("last_health_observed_at_utc")
 if previous and parse(meta["observed_at_utc"])<=parse(previous):return "EVIDENCE_PENDING",["SIGNAL_EVIDENCE_NOT_NEWER"]
 if previous and (parse(meta['observed_at_utc'])-parse(previous)).total_seconds()>600:pos['degraded_cycles']=0
 pos["last_health_evidence_id"]=meta["evidence_id"];pos["last_health_observed_at_utc"]=meta["observed_at_utc"]
 pos["last_health_generation_id"]=meta.get("generation_id")
 weak=[]
 if e.get("score") is not None and e["score"]<DISCOVERY_MIN_SCORE:weak.append("SCORE_WEAK")
 if e.get("independent",0)<DISCOVERY_MIN_INDEPENDENT:weak.append("SIGNALS_WEAK")
 r1=e.get("btc_rel_1h");r4=e.get("btc_rel_4h");acc=e.get("rel_accel")
 if r1 is not None and r4 is not None and r1<0 and r4<0:weak.append("BTC_RELATIVE_NEGATIVE")
 if acc is not None and acc<-1:weak.append("RELATIVE_MOMENTUM_DECELERATING")
 if len(weak)>=2:
  n=int(pos.get("degraded_cycles") or 0)+1;pos["degraded_cycles"]=n
  state="THESIS_INVALIDATED" if n>=DEGRADE_CONFIRM_CYCLES else ("DEGRADED" if n>=2 else "WEAKENING")
 else:
  pos["degraded_cycles"]=0;state="WEAKENING" if weak else "STRONG"
 pos["health_state"]=state;pos["health_reasons"]=weak
 if state!=old_state:pos['health_transitions']=(pos.get('health_transitions',[])+[{'from':old_state,'to':state,'generation_id':meta.get('generation_id'),'observed_at_utc':meta.get('observed_at_utc'),'reasons':list(weak)}])[-32:]
 return state,weak

def update_loss_exit_guard(state,pnl,reason,now,released_notional=0.0):
 g=state.setdefault("loss_exit_guard",{"loss_exit_count":0,"realized_loss_usdt":0.,"hard_invalidation_loss_exits":0})
 if pnl<0:
  g["loss_exit_count"]=int(g.get("loss_exit_count") or 0)+1
  g["realized_loss_usdt"]=round(float(g.get("realized_loss_usdt") or 0)+abs(pnl),2)
  if reason=="HARD_INVALIDATION":g["hard_invalidation_loss_exits"]=int(g.get("hard_invalidation_loss_exits") or 0)+1
 g["updated_at_utc"]=now.isoformat()
 loss_freeze.record_loss_exit(state,pnl,reason,released_notional,now,C)

def register_exit_for_reentry(state,pos,p,reason,now):
 reg=state.setdefault("reentry_registry",{})
 pnl=round(float(pos.get("net_pnl_usdt") or 0),2);risk_lock=bool(pnl<0 or reason=="HARD_INVALIDATION")
 reg[pos["asset"]]={"last_exit_at_utc":now.isoformat(),"last_exit_price":p,"last_exit_reason":reason,
  "last_exit_pnl_usdt":pnl,"post_exit_low":p,"reset_seen":False,"state":"POST_EXIT_OBSERVATION",
  "risk_lock":risk_lock,"risk_lock_observation_id":((state.get("systemic_risk") or {}).get("last_observation_id"))}

def reentry_allowed(state,c,p):
 row=(state.get("reentry_registry") or {}).get((c or {}).get("asset"))
 if not row:return True,["FIRST_ENTRY"]
 if row.get("risk_lock"):
  risk=state.get("systemic_risk") or {};cb=loss_freeze.circuit_for_admission(state) or {}
  meta=(c or {}).get("signal_evidence") or {}
  fresh_after_exit=False
  try:fresh_after_exit=bool(meta.get("evidence_id") and meta.get("observed_at_utc") and parse(meta["observed_at_utc"])>parse(row.get("last_exit_at_utc")))
  except Exception:fresh_after_exit=False
  if risk.get("level")!="NORMAL" or risk.get("recovery_mode") or cb.get("status") not in (None,"NORMAL") or not fresh_after_exit:
   row["state"]="REENTRY_LOCK";return False,["REENTRY_LOCK_TAIL_RISK","MARKET_AND_CIRCUIT_RECOVERY_REQUIRED","FRESH_POST_EXIT_SIGNAL_REQUIRED"]
  row["risk_lock"]=False;row["risk_unlock_observation_id"]=risk.get("last_observation_id")
 ep=finite(row.get("last_exit_price"))
 if not ep:return False,["REENTRY_EXIT_PRICE_MISSING"]
 row["post_exit_low"]=min(finite(row.get("post_exit_low")) or p,p)
 s=sig(c);ind=int(s.get("independent_signal_count") or 0);r1=finite(s.get("btc_relative_1h_pct"));r4=finite(s.get("btc_relative_4h_pct"));acc=finite(s.get("relative_acceleration_pct"))
 if ind<DISCOVERY_MIN_INDEPENDENT or ((r1 is not None and r1<0) and (r4 is not None and r4<0)):row["reset_seen"]=True
 pullback=(ep-row["post_exit_low"])/ep*100
 fresh_strength=ind>=DISCOVERY_MIN_INDEPENDENT and acc is not None and acc>0 and ((r1 is not None and r1>0) or (r4 is not None and r4>0))
 breakout=p>=ep*(1+REENTRY_BREAKOUT_PCT/100)
 reset=bool(row.get("reset_seen")) or pullback>=REENTRY_PULLBACK_PCT
 if reset and fresh_strength and breakout:
  row["state"]="REENTRY_ELIGIBLE";return True,["NEW_MOVE_CONFIRMED","RESET_OR_PULLBACK_SEEN","FRESH_RELATIVE_STRENGTH","BREAKOUT_ABOVE_EXIT"]
 row["state"]="REENTRY_BLOCKED_SAME_MOVE";return False,["REENTRY_BLOCKED_SAME_MOVE"]

def compact_closed_history(state):
 closed=state.get("closed_positions") or []
 if len(closed)<=MAX_CLOSED_HOT:return
 archive=state.setdefault("closed_trade_archive",[])
 # Never archive an unfinished observation: the compact archive is not backfilled.
 eligible=[x for x in closed if x.get("observation_complete")][:len(closed)-MAX_CLOSED_HOT]
 if not eligible:return
 ids={id(x) for x in eligible}
 for x in eligible:
  archive.append({"shadow_id":x.get("shadow_id"),"asset":x.get("asset"),"opened_at_utc":x.get("opened_at_utc"),"closed_at_utc":x.get("closed_at_utc"),
   "exit_reason":x.get("exit_reason"),"net_pnl_usdt":x.get("net_pnl_usdt"),"net_return_pct":x.get("net_return_pct"),"mfe_pct":x.get("mfe_pct"),"mae_pct":x.get("mae_pct"),"holding_mfe_pct":x.get("holding_mfe_pct"),"full_opportunity_mfe_pct":x.get("full_opportunity_mfe_pct"),"holding_profit_capture_ratio":x.get("holding_profit_capture_ratio"),"full_opportunity_capture_ratio":x.get("full_opportunity_capture_ratio"),"observation_complete":x.get("observation_complete"),"sample_cohort":x.get("sample_cohort"),"exit_evaluation":x.get("exit_evaluation"),"data_provenance":x.get("data_provenance")})
  for key in ("protection_lifecycle","loss_recovery_lifecycle","health_lifecycle_snapshot","health_transitions","exit_execution_estimate"):
   if key in x:archive[-1][key]=x[key]
  archive[-1]["post_exit_evaluation"]=x.get("post_exit_evaluation")
  archive[-1]["post_exit_observation"]=x.get("post_exit_observation")
 state["closed_positions"]=[x for x in closed if id(x) not in ids]
 # Keep a compact permanent ledger in the authoritative state; no 5-minute scan needs full old position blobs.
 if len(archive)>5000:state["closed_trade_archive"]=archive[-5000:]

def record_deferred_buy(state,c,now,p,e,scan):
 state.setdefault("deferred_buy_opportunities",[])
 s=sig(c); row={"at_utc":now.isoformat(),"asset":c.get("asset"),"price":p,"reason":"BUY_BUT_NO_CAPITAL","trade_action":authoritative_entry_action(c,"SYSTEM_BLOCKED"),
  "score":finite(s.get("score")),"independent_signal_count":int(s.get("independent_signal_count") or 0),"btc_relative_1h_pct":finite(s.get("btc_relative_1h_pct")),
  "btc_relative_4h_pct":finite(s.get("btc_relative_4h_pct")),"estimated_rr":e.get("estimated_rr"),"used_capital_usdt":used_capital(state),"capital_pool_usdt":CAPITAL_POOL_USDT,"capital_snapshot":capital_snapshot(state,scan),
  "loss_making_positions_are_not_rotated":True}
 state["deferred_buy_opportunities"].append(row)
 state["deferred_buy_opportunities"]=state["deferred_buy_opportunities"][-MAX_DEFERRED_HISTORY:]
 return row

def record(state,pos,action,now,reasons,e,p):
 state["decisions"].append({"at":now.isoformat(),"shadow_id":pos.get("shadow_id"),"asset":pos["asset"],"action":action,
  "price":p,"reasons":reasons,"evidence":e,"tranches":len(pos.get("tranches",[])),"notional_usdt":total_notional(pos) if pos.get("tranches") else 0})
def trade_event(state,pos,action,now,p,reason=None,pnl=None):
 event={"type":EVENT_PREFIX+"_"+action,"at":now.isoformat(),"asset":pos["asset"],"shadow_id":pos.get("shadow_id"),"price":p,
  "generation_id":state.get('active_observation_generation_id'),
  "tranches":len(pos.get("tranches",[])),"notional_usdt":total_notional(pos) if pos.get("tranches") else 0}
 if reason is not None:event["reason"]=reason
 if pnl is not None:event["net_pnl_usdt"]=round(pnl,2)
 if EVENT_PREFIX=='SHADOW_V2' and pos.get('execution_venue'):
  for key in ('execution_venue','market_symbol','market_type','execution_identity_scope'):
   event[key]=pos.get(key)
 state["events"].append(event)
 if action=="BUY":
  entered=set(state.get("ever_entered_assets") or [])
  entered.add(pos["asset"]);state["ever_entered_assets"]=sorted(entered)
def add(pos,p,e,now):
 i=len(pos["tranches"]);meta=(e or {}).get("signal_evidence") or {}
 pos["tranches"].append({"tranche":i+1,"at":now.isoformat(),"price":p,"notional_usdt":TRANCHES[i],
  "buy_slippage_bps":e.get("buy_slippage_bps") or 0,"reason":"INITIAL" if i==0 else "MATERIAL_BETTER_PRICE_FRESH_RECOVERY_REVALIDATION",
  "signal_evidence_id":meta.get("evidence_id"),"signal_generation_id":meta.get("generation_id"),
  "signal_observed_at_utc":meta.get("observed_at_utc"),"book_observed_at_utc":e.get("book_observed_at_utc")})
def update_overfilter_guard(state,scan,review,liq,supply,now,buy_count):
 guard=load(GUARD,{"schema":"hunter_shadow_v2_overfilter_guard_v1","cycles":[],"status":"NORMAL"})
 safe_misses=[]
 for c in review.get("candidates") or []:
  a=c.get("asset"); p=price(scan,a)
  if not a or not p:continue
  act,reasons,e=decision(c,scan,liq,supply,"ENTRY",p=p)
  # Candidate passed hard market-safety evidence but was rejected by strategy selectivity.
  hard=[r for r in reasons if r.startswith("BLOCKER:") or r in ("SPREAD_UNACCEPTABLE","DEPTH_INSUFFICIENT","SLIPPAGE_UNACCEPTABLE","SUPPLY_RISK_UNVERIFIED")]
  ch=finite(((scan.get("coins") or {}).get(a) or {}).get("change_24h_pct"))
  if act!="BUY" and not hard and ch is not None and ch>=OVERFILTER_MISSED_MOVE_PCT:
   safe_misses.append({"asset":a,"change_24h_pct":ch,"reasons":reasons,"estimated_rr":e.get("estimated_rr")})
 risk_blocked=risk_blocks_new(state,now)
 cycles=guard.get("cycles",[]);cycles.append({"at_utc":now.isoformat(),"generation_id":scan.get("generation_id"),"buys":buy_count,"safe_misses":[] if risk_blocked else safe_misses,"risk_blocked":risk_blocked})
 cycles=cycles[-OVERFILTER_LOOKBACK:];guard["cycles"]=cycles
 zero=0
 for x in reversed(cycles):
  if x.get("risk_blocked"):continue
  if x.get("buys",0)==0:zero+=1
  else:break
 recent_misses={m["asset"] for x in cycles[-OVERFILTER_ZERO_BUY_CYCLES:] for m in x.get("safe_misses",[])}
 over=zero>=OVERFILTER_ZERO_BUY_CYCLES and len(recent_misses)>=OVERFILTER_MIN_SAFE_MISSES
 guard.update({"as_of_utc":now.isoformat(),"consecutive_zero_buy_cycles":zero,"recent_safe_missed_assets":sorted(recent_misses),
  "status":"OVER_FILTERING" if over else "NORMAL",
  "optimizer_action":"RELAX_ONE_SHADOW_DIMENSION_AND_AB_TEST" if over else "NONE",
  "live_capital_rules_changed":False,"capital_authority":"NONE_SHADOW_ONLY"})
 atomic_json_write(GUARD,guard);return guard
def quarantine_non_crypto_history(state,excluded):
 excluded=set(excluded or [])
 if not excluded:return {"assets":[],"open":0,"closed":0,"events":0,"decisions":0,"ever":0}
 report={"assets":set(),"open":0,"closed":0,"events":0,"decisions":0,"ever":0}
 for src,dst,key in (
  ("open_positions","excluded_non_crypto_positions","open"),
  ("closed_positions","excluded_non_crypto_closed_positions","closed"),
  ("events","excluded_non_crypto_events","events"),
  ("decisions","excluded_non_crypto_decisions","decisions")):
  rows=state.get(src) or []; bad=[x for x in rows if x.get("asset") in excluded]
  if bad:
   state[src]=[x for x in rows if x.get("asset") not in excluded]
   state.setdefault(dst,[]).extend(bad);report[key]=len(bad);report["assets"].update(x.get("asset") for x in bad if x.get("asset"))
 ever=list(state.get("ever_entered_assets") or []);bad_ever=sorted(set(ever)&excluded)
 if bad_ever:
  state["ever_entered_assets"]=sorted(set(ever)-excluded)
  state["excluded_non_crypto_ever_entered_assets"]=sorted(set(state.get("excluded_non_crypto_ever_entered_assets") or [])|set(bad_ever))
  report["ever"]=len(bad_ever);report["assets"].update(bad_ever)
 report["assets"]=sorted(report["assets"])
 return report

def manage_existing_positions(state,scan,review,liq,supply,now,btc=None,capital_proposals=None):
 """Manage open positions only. Ordinary deterioration never forces a loss exit."""
 btc=btc or price(scan,"BTC");cm={c.get("asset"):c for c in review.get("candidates") or [] if c.get("asset")}
 state.setdefault("open_positions",[]);state.setdefault("closed_positions",[]);state.setdefault("events",[]);state.setdefault("decisions",[])
 state['active_observation_generation_id']=scan.get('generation_id')
 still=[];base_scan=scan;base_liq=liq
 for pos in state["open_positions"]:
  scan=base_scan;liq=base_liq;venue_packet=None
  identity=(pos.get('execution_venue'),pos.get('market_symbol'),pos.get('market_type'))
  if pos.get('execution_venue')=='BYBIT_SPOT' and ENTRY_MODE=='EXECUTABLE':
   try:
    from research.hunter_bybit_management import management_context
    venue_packet=management_context(pos,scan,now)
    scan=dict(scan,coins=dict(scan.get('coins') or {},**{pos['asset']:venue_packet['market']}))
    liq=dict(liq,snapshots=dict(liq.get('snapshots') or {},**{pos['asset']:venue_packet['liquidity']}))
   except (KeyError,TypeError,ValueError) as exc:
    record(state,pos,'HOLD',now,['PRIMARY_VENUE_MANAGEMENT_EVIDENCE_UNAVAILABLE',str(exc)],{},pos.get('last_price'))
    still.append(pos);continue
  elif ((any(identity) and identity!=('BINANCE_SPOT',pos['asset']+'USDT','spot'))
      or (pos.get('execution_fee_bps') is not None and pos['execution_fee_bps']!=FEE_BPS)):
   # This authoritative manager consumes Binance marks/signals/books only.
   # A reference-market profit is not evidence of a primary-venue exit or ADD.
   # Retain prior fresh health/marks until matched management evidence exists.
   record(state,pos,'HOLD',now,['PRIMARY_VENUE_MANAGEMENT_EVIDENCE_UNAVAILABLE'],{},pos.get('last_price'))
   still.append(pos);continue
  p=price(scan,pos["asset"])
  if not p:record(state,pos,"HOLD",now,["CURRENT_PRICE_MISSING"],{},pos.get("last_price"));still.append(pos);continue
  if venue_packet:
   pos['last_primary_venue_market_source_timestamp']=venue_packet['market_source_timestamp']
   pos['last_primary_venue_management_evidence']={'execution_venue':'BYBIT_SPOT','market_symbol':pos['market_symbol'],'generation_id':scan.get('generation_id'),'observed_at_utc':venue_packet['observed_at_utc'],'market_source_timestamp':venue_packet['market_source_timestamp'],'fee_bps':venue_packet['fee_bps'],'signal':venue_packet['candidate']['signal'],'signal_evidence':venue_packet['candidate']['signal_evidence'],'raw_book_evidence':venue_packet['liquidity']['raw_book_evidence'],'historical_execution_verified':False}
  c=venue_packet['candidate'] if venue_packet else cm.get(pos["asset"]);raw=raw_return(pos,p);pos["mfe_pct"]=round(max(pos.get("mfe_pct",0),raw),4);pos["mae_pct"]=round(min(pos.get("mae_pct",0),raw),4);ensure_opportunity_observation(pos,p,now)
  pos["last_price"]=p;pos["last_marked_at_utc"]=now.isoformat();pos["holding_hours"]=round((now-parse(pos["opened_at_utc"])).total_seconds()/3600,2)
  act,reasons,e=decision(c,scan,liq,supply,"ADD" if len(pos["tranches"])<3 else "HOLD",pos,p)
  systemic_level=((state.get("systemic_risk") or {}).get("level") or "HIGH")
  confirmed_systemic=tail.confirmed_systemic_liquidity_shock(state)
  health,health_reasons=position_health(pos,e,now,systemic_level,confirmed_systemic)
  signal_fresh=fresh((e.get("signal_evidence") or {}).get("observed_at_utc"),now)
  book_fresh=fresh(e.get("book_observed_at_utc"),now)
  if not (signal_fresh and book_fresh):act="HOLD";reasons.append("MANAGEMENT_EVIDENCE_STALE_OR_MISSING")
  if health!="STRONG":act="HOLD"
  if venue_packet and act=='ADD':
   act='HOLD';reasons.append('BYBIT_ADD_REQUIRES_HOURLY_CAPITAL_REVALIDATION')
  if risk_blocks_new(state,now):
   if act=="ADD":reasons.append("SYSTEMIC_RISK_ADD_FREEZE")
   act="HOLD"
  if act=="ADD":
   next_amount=TRANCHES[len(pos["tranches"])]
   if capital_proposals is not None:
    capital_proposals.append({"kind":"ADD","asset":pos["asset"],"pos":pos,"price":p,"evidence":e,"reasons":list(reasons),"amount":next_amount})
    record(state,pos,"HOLD",now,["CAPITAL_ALLOCATION_PENDING"]+reasons,e,p)
   elif capital_available(state,next_amount,"ADD",scan):
    add(pos,p,e,now);record(state,pos,"ADD",now,reasons,e,p);trade_event(state,pos,"ADD",now,p,"MATERIAL_BETTER_PRICE_FRESH_RECOVERY_REVALIDATION");raw=raw_return(pos,p)
   else:record(state,pos,"HOLD",now,["CAPITAL_ALLOCATOR_CAPACITY_WAIT"],e,p)
  else:record(state,pos,"HOLD",now,reasons+["POSITION_HEALTH_"+health]+health_reasons,e,p)
  pnl=net_pnl(pos,p);legacy_protection=profit_protection(pos,p)
  execution=lifecycle.liquidation(pos,liq_for(liq,pos['asset']).get('raw_book_evidence',{}),now,pos.get('execution_fee_bps',FEE_BPS))
  pos['last_exit_estimate']=execution
  admit_execution_identity(pos,liq_for(liq,pos['asset']).get('raw_book_evidence',{}),execution,scan,now)
  protection=lifecycle.protect(pos,p,pnl,execution,now,scan.get('generation_id'),scan.get('as_of_utc'),PROTECT_ARM_PCT,GIVEBACK_MAX_PCT,MIN_PROTECTED_NET_PCT)
  lifecycle.recovery(pos,e,now,pnl,pos.get('last_health_generation_id'),health)
  exit_reason=None;exit_reasons=None
  if legacy_protection["exit"] and pnl<=0 and health!="HARD_INVALIDATION":
   # Audit the blocked profit window without changing the net-positive-only exit policy.
   protection_review={**legacy_protection,"arm_provenance":"HISTORICAL_MFE_RECALCULATION_ONLY","historical_execution_window":"UNVERIFIABLE_HISTORICAL_EXECUTION_WINDOW","observed_at_utc":now.isoformat(),"generation_id":scan.get("generation_id"),
    "reference_price":p,"net_pnl_usdt":pnl,"fee_bps_per_side":pos.get('execution_fee_bps',FEE_BPS),
    "tranche_cost_inputs":[{"price":t["price"],"notional_usdt":t["notional_usdt"],"buy_slippage_bps":t.get("buy_slippage_bps",0)} for t in pos["tranches"]],
    "sell_cost_model":"REFERENCE_PRICE_WITH_SELL_FEE_ONLY","sell_spread_and_slippage_modelled":False}
   pos["profit_protection_review"]=protection_review
   record(state,pos,"HOLD",now,["PROFIT_PROTECTION_BLOCKED_NET_NONPOSITIVE"],{**e,"profit_protection_review":protection_review},p)
  if health=="HARD_INVALIDATION":exit_reason="HARD_INVALIDATION";exit_reasons=health_reasons
  elif protection["exit"] and execution.get('net_pnl_usdt',0)>0:
   pnl=execution['net_pnl_usdt'];exit_reason="PROFIT_PROTECTION";exit_reasons=["PERSISTED_PROFIT_PROTECTION","GIVEBACK_OR_PROTECTED_FLOOR","FULL_QUANTITY_SHADOW_RECEIPT_COST_ESTIMATE"]
  elif health=="THESIS_INVALIDATED":
   if pnl>0:exit_reason="PROFIT_STAGNATION" if pos.get("mfe_pct",0)<TARGET else "THESIS_INVALIDATED_PROFIT_EXIT";exit_reasons=["THESIS_INVALIDATED","NET_PROFIT_AVAILABLE"]
   else:record(state,pos,"HOLD",now,["LOSS_RECOVERY","NO_MECHANICAL_LOSS_EXIT"],e,p)
  elif raw>=TARGET and signal_fresh:
   r1=e.get("btc_rel_1h");r4=e.get("btc_rel_4h");accel=e.get("rel_accel");runner=r1 is not None and r4 is not None and accel is not None and r1>0 and r4>0 and accel>0
   if runner:record(state,pos,"HOLD",now,["PROFIT_TARGET_REACHED_BUT_RELATIVE_MOMENTUM_STILL_STRONG","RUNNER_MODE"],e,p)
   else:exit_reason="PROFIT_REVIEW_MOMENTUM_FADED";exit_reasons=["PROFIT_TARGET_REACHED","RELATIVE_MOMENTUM_NOT_STRONG_ENOUGH_TO_RUN"]
  if exit_reason and scan.get('as_of_utc') and not lifecycle.fresh(scan['as_of_utc'],now):
   record(state,pos,'HOLD',now,['EXIT_MARKET_EVIDENCE_STALE'],e,p);exit_reason=None
  if exit_reason and venue_packet:
   if execution.get('status')!='SHADOW_RECEIPT_ESTIMATE' or (exit_reason!='HARD_INVALIDATION' and execution.get('net_pnl_usdt',0)<=0):
    record(state,pos,'HOLD',now,['PRIMARY_VENUE_NET_EXECUTION_NOT_AVAILABLE'],e,p);exit_reason=None
   else:
    pnl=execution['net_pnl_usdt'];pos['exit_execution_estimate']=execution
  if exit_reason:
   notion=total_notional(pos);br=((btc/pos["btc_entry_price"]-1)*100) if btc and pos.get("btc_entry_price") else 0
   pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":exit_reason,"weighted_entry_price":weighted_entry(pos),"total_notional_usdt":notion,"net_pnl_usdt":round(pnl,2),"net_return_pct":round(pnl/notion*100,4),"btc_return_pct":round(br,4),"btc_relative_return_pct":round(pnl/notion*100-br,4)})
   if exit_reason=="PROFIT_PROTECTION":
    pos["profit_protection"]=protection
    pos['protection_lifecycle']['state']='EXITED'
    pos['protection_lifecycle'].update(exited_at_utc=now.isoformat(),exited_generation_id=scan.get('generation_id'))
    pos['exit_execution_estimate']=execution
   pos.update(exit_analysis(pos,p,exit_reason));refresh_post_exit_status(pos,now);update_loss_exit_guard(state,pnl,exit_reason,now,notion);register_exit_for_reentry(state,pos,p,exit_reason,now)
   record(state,pos,"EXIT",now,exit_reasons,e,p);trade_event(state,pos,"SELL",now,p,exit_reason,pnl);state["closed_positions"].append(pos);continue
  still.append(pos)
 state["open_positions"]=still;compact_closed_history(state);return state

def _execution_source_sha():
 try:return subprocess.check_output(['git','rev-parse','HEAD'],text=True,timeout=5).strip()
 except (OSError,subprocess.SubprocessError):return None

def admit_execution_identity(pos,book,execution,scan,now,entry=False):
 # Metadata only, inside the existing authoritative lifecycle/Single Writer.
 # Never rewrite historical events or infer execution from availability labels.
 if ENTRY_MODE!='EXECUTABLE' or any(pos.get(k) for k in ('execution_venue','market_symbol','market_type')):return
 source_sha=_execution_source_sha()
 if not source_sha or len(source_sha)!=40 or any(c not in '0123456789abcdef' for c in source_sha):return
 pos.update(identity_fields(pos,book,execution,now,scan.get('generation_id'),source_sha,FEE_BPS,
  source_kind='SHADOW_ENTRY' if entry else ('POSITION_MONITOR' if str(scan.get('generation_id','')).startswith('MONITOR_') else 'HOURLY_RESEARCH'),formal=True,entry=entry))

def execute_capital_proposals(state,proposals,scan,now):
 executed_buys=0
 ranked=sorted(proposals,key=lambda x:opportunity_priority(x.get("evidence"),x.get("kind")),reverse=True)
 for q in ranked:
  kind=q["kind"];pos=q["pos"];amount=q["amount"];e=q["evidence"];p=q["price"]
  if risk_blocks_new(state,now):
   record(state,pos if kind=="ADD" else {"asset":q["asset"],"tranches":[]},"HOLD",now,["SYSTEMIC_RISK_ENTRY_FREEZE","CAPITAL_ALLOCATOR_WAIT"],e,p);continue
  if kind=="ADD" and pos not in state.get("open_positions",[]):continue
  ok,gate_reasons=marginal_capital_gate(state,amount,e,kind,scan)
  if not ok:
   if kind=="BUY" and q.get("candidate") is not None:record_deferred_buy(state,q["candidate"],now,p,e,scan)
   record(state,pos if kind=="ADD" else {"asset":q["asset"],"tranches":[]},"HOLD",now,gate_reasons+["CAPITAL_ALLOCATOR_WAIT"],e,p);continue
  reserve_amount=max(0,amount-max(0,17000-reserve_snapshot(state)['ordinary_used'])) if CAPITAL_POOL_USDT is not None else 0
  if kind=="ADD":
   add(pos,p,e,now);record(state,pos,"ADD",now,q["reasons"]+gate_reasons,e,p);trade_event(state,pos,"ADD",now,p,"PORTFOLIO_ALLOCATOR_ADD")
  else:
   add(pos,p,e,now)
   book=q.get('entry_book') or {}
   admit_execution_identity(pos,book,lifecycle.liquidation(pos,book,now,FEE_BPS),scan,now,entry=True)
   record(state,pos,"BUY",now,q["reasons"]+gate_reasons,e,p);trade_event(state,pos,"BUY",now,p,("DISCOVERY_ENTRY" if ENTRY_MODE=="DISCOVERY" else "EXECUTABLE_ENTRY"));state["open_positions"].append(pos);executed_buys+=1
  pos['tranches'][-1]['strategic_notional_usdt']=reserve_amount
  pos['tranches'][-1]['ordinary_notional_usdt']=amount-reserve_amount
  if reserve_amount:pos['tranches'][-1]['strategic_qualification']='EXISTING_ABOVE_85PCT_EDGE_GATE'
 return executed_buys

def opportunity_summary(rows,now=None):
 def vals(k):return [float(x[k]) for x in rows if finite(x.get(k)) is not None]
 def avg(v):return round(sum(v)/len(v),4) if v else None
 def med(v):
  if not v:return None
  z=sorted(v);n=len(z);return round(z[n//2],4) if n%2 else round((z[n//2-1]+z[n//2])/2,4)
 full=vals("full_opportunity_mfe_pct");holding=vals("holding_mfe_pct")
 now=now or dt.datetime.now(dt.timezone.utc)
 coverage={}
 for h in REVIEW_HOURS:
  key=f"{int(h)}h"
  due=[x for x in rows if x.get("closed_at_utc") and now>=parse(x["closed_at_utc"])+dt.timedelta(hours=h)]
  coverage[key]={"due":len(due),"backfilled":sum((x.get("post_exit_observation") or {}).get(key,{}).get("max_return_from_sell_pct") is not None for x in due)}
 completed=[x for x in rows if x.get("observation_complete") and (x.get("post_exit_observation") or {}).get("72h",{}).get("max_return_from_sell_pct") is not None]
 dist={"lt_0":0,"0_3":0,"3_5":0,"5_10":0,"10_20":0,"gt_20":0}
 for v in full:
  if v<0:dist["lt_0"]+=1
  elif v<3:dist["0_3"]+=1
  elif v<5:dist["3_5"]+=1
  elif v<10:dist["5_10"]+=1
  elif v<=20:dist["10_20"]+=1
  else:dist["gt_20"]+=1
 return {"evaluated_positions":len(rows),"holding_mfe_available":len(holding),"full_opportunity_mfe_available":len(full),"completed_72h_observations":len(completed),
  "post_exit_coverage":coverage,"avg_completed_72h_max_return_from_sell_pct":avg([x["post_exit_observation"]["72h"]["max_return_from_sell_pct"] for x in completed]),
  "full_opportunity_metrics_provisional":len(completed)<len(rows),
  "avg_holding_mfe_pct":avg(holding),"median_holding_mfe_pct":med(holding),"avg_full_opportunity_mfe_pct":avg(full),"median_full_opportunity_mfe_pct":med(full),
  "avg_realized_net_return_pct":avg(vals("net_return_pct")),"avg_holding_profit_capture_ratio":avg(vals("holding_profit_capture_ratio")),
  "avg_full_opportunity_capture_ratio":avg(vals("full_opportunity_capture_ratio")),
  "potential_premature_exit_count":sum(x.get("exit_evaluation")=="POTENTIAL_PREMATURE_EXIT" for x in rows),
  "profit_giveback_count":sum(x.get("exit_evaluation")=="PROFIT_GIVEBACK" for x in rows),"full_opportunity_mfe_distribution":dist}

def build_summary(state,now,guard_status="NORMAL",scan=None):
 closed=state.get("closed_positions") or [];arch=state.get("closed_trade_archive") or [];all_closed=arch+closed
 gp=sum(max(0,float(x.get("net_pnl_usdt") or 0)) for x in all_closed);gl=-sum(min(0,float(x.get("net_pnl_usdt") or 0)) for x in all_closed)
 for x in state.get("open_positions") or []:
  ensure_opportunity_observation(x,x.get("last_price"),now)
  if x.get("sample_cohort")=="MIGRATION_SAMPLE" and x.get("data_provenance")!="HISTORICAL_BACKFILL":backfill_opportunity_history(x,now)
 for x in closed:ensure_opportunity_observation(x,None,now)
 cohorts={"all_samples":opportunity_summary(all_closed,now),"migration_samples":opportunity_summary([x for x in all_closed if x.get("sample_cohort")=="MIGRATION_SAMPLE"],now),
  "new_version_samples":opportunity_summary([x for x in all_closed if x.get("sample_cohort")=="NEW_VERSION_SAMPLE"],now)}
 return {**lifecycle.mtm(state,now,net_pnl,FEE_BPS),"generation_id":state.get('last_cycle_generation_id'),"schema":"hunter_shadow_v2_summary_v3","as_of_utc":now.isoformat(),"mode":"SIMULATION_ONLY_NO_REAL_ORDERS",
  "strategy":STRATEGY_ID,"policy_version":VERSION,"open_positions":len(state.get("open_positions") or []),"closed_positions":len(closed),"archived_closed_positions":len(arch),"total_closed_positions":len(all_closed),
  "net_pnl_usdt":round(sum(float(x.get("net_pnl_usdt") or 0) for x in all_closed),2),"profit_factor":round(gp/gl,3) if gl else ("INF" if gp else None),
  "policy":{"tranches_usdt":list(TRANCHES),"price_only_stop_loss":POLICY["price_only_stop_loss"],"time_exit_enabled":POLICY["time_exit_enabled"],"time_review_hours":list(REVIEW_HOURS),
   "entry_mode":ENTRY_MODE,"entry_requires_full_execution_validation":ENTRY_MODE=="EXECUTABLE","add_requires_revalidation":True,"fail_closed_on_missing_candidate_evidence":True,"min_estimated_rr":MIN_RR,
   "max_spread_bps":MAX_SPREAD_BPS,"min_depth_2pct_usdt":MIN_DEPTH_USDT,"max_buy_slippage_bps":MAX_SLIP_BPS,"profit_review_trigger_pct":TARGET,"profit_target_is_forced_exit":False,"runner_requires_positive_1h_4h_relative_and_acceleration":True,
   "profit_protection":{"arm_mfe_pct":PROTECT_ARM_PCT,"max_giveback_pct":GIVEBACK_MAX_PCT,"min_protected_net_pct":MIN_PROTECTED_NET_PCT},
   "three_tranche_adds_are_conditional_not_mechanical":True,"capital_pool_usdt":CAPITAL_POOL_USDT,"max_open":None,"discovery_sample_cap":None,"first_tranche":("AFTER_RESEARCH_DISCOVERY_ADMISSION" if ENTRY_MODE=="DISCOVERY" else "ONLY_AFTER_FULL_EXECUTABLE_DECISION_GATE"),"bybit_channel_is_label_not_discovery_gate":True,"post_exit_tracking_hours":list(REVIEW_HOURS),"tranche_counterfactuals_at_exit":True,
   "overfilter_guard":{"zero_buy_cycles":OVERFILTER_ZERO_BUY_CYCLES,"missed_move_pct":OVERFILTER_MISSED_MOVE_PCT,"min_safe_misses":OVERFILTER_MIN_SAFE_MISSES,"status":guard_status},
   "capital_management":capital_snapshot(state,scan),
   "tail_risk_phase1":{**loss_freeze.control_snapshot(state),"systemic_risk":state.get("systemic_risk"),"calibration_only":True,"capital_authority":"NONE_SHADOW_ONLY","exchange_protection_orders":"FUTURE_ADMISSION_GATE_NOT_IMPLEMENTED"},
   "capital_rotation":{"loss_making_position_rotation_allowed":False,"profitable_exit_may_release_capital_for_new_buy":True,"full_pool_buy_status":"CAPITAL_ALLOCATOR_WAIT","buy_add_compete_same_queue":True,"arrival_order_has_no_capital_priority":True,"market_regime_controls_utilization":True,"hard_cash_floor_pct":HARD_CASH_FLOOR_PCT,"realized_net_pnl_compounds_equity":True,"unrealized_pnl_expands_equity":False},
   "position_lifecycle":{"degrade_confirm_cycles":DEGRADE_CONFIRM_CYCLES,"ordinary_thesis_invalidation_loss_exit":False,"hard_invalidation_may_exit_at_loss":True,"profit_stagnation_exit":True,"reentry_requires_new_move":True,"max_hot_closed_positions":MAX_CLOSED_HOT}},
  "opportunity_evaluation":cohorts,"capital_authority":"NONE_SHADOW_ONLY"}

def record_early_sample_exclusions(state,review,scan,now):
 """Diagnostic decisions only; never admit a sample or create a trade event."""
 if ENTRY_MODE!="DISCOVERY" or "v1_early_exclusions" not in review:return
 audit=review["v1_early_exclusions"]
 if not scan.get("generation_id") or audit.get("scan_generation_id")!=scan.get("generation_id"):
  raise SystemExit("V1_EXCLUSION_AUDIT_GENERATION_MISMATCH")
 existing={(d.get("asset"),((d.get("evidence") or {}).get("signal_evidence") or {}).get("generation_id"))
           for d in state.get("decisions",[]) if d.get("action")=="SAMPLE_EXCLUDED"}
 state["early_sample_exclusions"]=audit
 for row in audit.get("rows") or []:
  if (row["asset"],audit["scan_generation_id"]) in existing:continue
  record(state,{"asset":row["asset"],"tranches":[]},"SAMPLE_EXCLUDED",now,[row["reason"]],row,price(scan,row["asset"]))
  existing.add((row["asset"],audit["scan_generation_id"]))

def main():
 if SHADOW_FREEZE:
  print(json.dumps({"status":"SHADOW_STRATEGY_FREEZE","strategy":STRATEGY_ID,"writes":0,"capital_pool_usdt":CAPITAL_POOL_USDT}))
  return
 now=dt.datetime.now(dt.timezone.utc);scan=load(SCAN);review=load(REVIEW);liq=load(LIQ);supply=load(SUPPLY);bybit=load(BYBIT,{})
 if review.get("policy_version")!=VERSION:raise SystemExit("SHADOW_INPUT_POLICY_MISMATCH")
 if not scan.get("binance_complete") or review.get("scan_generation_id")!=scan.get("generation_id"):raise SystemExit("V2_INPUT_GENERATION_MISMATCH")
 btc=price(scan,"BTC")
 if not btc:raise SystemExit("BTC_PRICE_MISSING")
 state=load(STATE,{"schema":"hunter_shadow_v2_portfolio_v2","mode":"SIMULATION_ONLY_NO_REAL_ORDERS","open_positions":[],"closed_positions":[],"events":[],"decisions":[]})
 state.setdefault("open_positions",[]);state.setdefault("closed_positions",[]);state.setdefault("events",[]);state.setdefault("decisions",[])
 record_early_sample_exclusions(state,review,scan,now)
 risk_evidence=tail.collect_systemic_evidence(scan,liq,now,C,BINANCE_DATA_API)
 update_risk_controls(state,risk_evidence,now)
 excluded=set((((scan.get("venue_status") or {}).get("binance") or {}).get("excluded_bstocks") or []))
 if not excluded:raise SystemExit("CRYPTO_SCOPE_BSTOCK_CLASSIFICATION_MISSING")
 quarantine_non_crypto_history(state,excluded)
 refresh_closed_observations(state,now,lambda asset:price(scan,asset))
 capital_proposals=[]
 manage_existing_positions(state,scan,review,liq,supply,now,btc,capital_proposals)
 open_assets={x["asset"] for x in state["open_positions"]};buy_count=0
 ranked=sorted(review.get("candidates") or [],key=lambda c:finite(sig(c).get("score")) or 0,reverse=True)
 for c in ranked:
  a=c.get("asset")
  if not a or a in open_assets:continue
  p=price(scan,a)
  if risk_blocks_new(state,now):
   e=evidence(c,liq,supply);risk_reasons=["SYSTEMIC_RISK_ENTRY_FREEZE",str((state.get("systemic_risk") or {}).get("level") or "UNKNOWN")]
   record(state,{"asset":a,"tranches":[]},"RISK_BLOCKED" if ENTRY_MODE=="DISCOVERY" else "WAIT",now,risk_reasons,e,p)
   if ENTRY_MODE=="DISCOVERY":
    rows=state.setdefault("risk_blocked_samples",[])
    rows.append({"at_utc":now.isoformat(),"asset":a,"price":p,"signal_evidence":c.get("signal_evidence"),"systemic_risk":state.get("systemic_risk"),"would_be_discovery":discovery_decision(c)[0]=="BUY","capital_authority":"NONE_SHADOW_ONLY"})
    state["risk_blocked_samples"]=rows[-MAX_DEFERRED_HISTORY:]
   continue
  re_ok,re_reasons=reentry_allowed(state,c,p) if p else (False,["CURRENT_PRICE_MISSING"])
  if not re_ok:
   record(state,{"asset":a,"tranches":[]},"WAIT",now,re_reasons,{},p);continue
  broad,broad_reasons=discovery_decision(c);fallback,reasons,e=decision(c,scan,liq,supply,"ENTRY")
  act=authoritative_entry_action(c,fallback)
  if not entry_allowed(ENTRY_MODE,broad,p,act):
   if broad!="BUY": reject_reasons=broad_reasons
   elif not p: reject_reasons=["CURRENT_PRICE_MISSING"]
   elif ENTRY_MODE=="EXECUTABLE" and act in ("WAIT","SYSTEM_BLOCKED"):
    reject_reasons=[act]+list(c.get("research_gaps") or [])+list(c.get("blockers") or [])
   else: reject_reasons=reasons
   dummy={"asset":a,"tranches":[]};record(state,dummy,act if act in ("WAIT","SYSTEM_BLOCKED") else "REJECT",now,reject_reasons,e,p);continue
  pos={"shadow_id":ID_PREFIX+"-"+now.strftime("%Y%m%dT%H%M%S")+"-"+a+"-"+uuid.uuid4().hex[:6],"asset":a,"opened_at_utc":now.isoformat(),
   "scan_generation_id":scan["generation_id"],"btc_entry_price":btc,"tranches":[],"mfe_pct":0.,"mae_pct":0.,"holding_mfe_pct":0.,"holding_peak_price":p,"holding_peak_at_utc":now.isoformat(),"full_opportunity_mfe_pct":0.,"full_opportunity_peak_price":p,"full_opportunity_peak_at_utc":now.isoformat(),"observation_complete":False,"sample_cohort":("NEW_VERSION_SAMPLE" if now>=NEW_VERSION_CUTOFF_UTC else "MIGRATION_SAMPLE"),"data_provenance":"LIVE_OBSERVATION","last_price":p,
   "last_marked_at_utc":now.isoformat(),"capital_authority":"NONE_SHADOW_ONLY",
   "discovery_gate":"BROAD_FORWARD_SAMPLE","execution_channel":bybit_channel(bybit,a),
   "executable_gate":{"pass":act=="BUY","reasons":reasons,"source":"CAPITAL_REVIEW_FINAL_ACTION","purpose":"SINGLE_AUTHORITATIVE_ENTRY_DECISION"}}
  entry_reasons=(broad_reasons if ENTRY_MODE=="DISCOVERY" else reasons)
  capital_proposals.append({"kind":"BUY","asset":a,"pos":pos,"price":p,"evidence":e,"reasons":list(entry_reasons),"amount":TRANCHES[0],"candidate":c,"entry_book":liq_for(liq,a).get('raw_book_evidence',{})})
  open_assets.add(a)
 buy_count=execute_capital_proposals(state,capital_proposals,scan,now)
 guard=update_overfilter_guard(state,scan,review,liq,supply,now,buy_count)
 # Trade events and positions are durable audit history. High-frequency HOLD/REJECT
 # decisions are diagnostic only and must not make the authoritative portfolio grow forever.
 if len(state["decisions"])>MAX_DECISION_HISTORY:
  state["decision_history_truncated"]=int(state.get("decision_history_truncated") or 0)+len(state["decisions"])-MAX_DECISION_HISTORY
  state["decisions"]=state["decisions"][-MAX_DECISION_HISTORY:]
 entered=set(state.get("ever_entered_assets") or [])
 entered.update(x.get("asset") for x in state.get("events",[]) if x.get("type") in ("SHADOW_V1_BUY","SHADOW_V2_BUY") and x.get("asset"))
 state["ever_entered_assets"]=sorted(entered)
 if len(state["events"])>MAX_EVENT_HISTORY:
  state["event_history_truncated"]=int(state.get("event_history_truncated") or 0)+len(state["events"])-MAX_EVENT_HISTORY
  state["events"]=state["events"][-MAX_EVENT_HISTORY:]
 state["policy_version"]=VERSION;state["updated_at_utc"]=now.isoformat();state["last_cycle_generation_id"]=scan["generation_id"];state["schema"]="hunter_shadow_v2_portfolio_v2";state["overfilter_guard_status"]=guard["status"]
 summary=build_summary(state,now,guard.get("status") or "NORMAL",scan)
 atomic_json_write(STATE,state);atomic_json_write(SUMMARY,summary)
 print(json.dumps({"open":[x["asset"] for x in state["open_positions"]],"closed":len(state.get("closed_positions") or []),"decisions":len(state["decisions"]),"summary":summary},ensure_ascii=False))
if __name__=="__main__":main()
