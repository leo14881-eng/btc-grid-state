#!/usr/bin/env python3
"""Stock Shadow fundamental observer. Observation-only; never changes BUY/ADD/SELL."""
# FINAL_ACCEPTANCE_20261004
import json, urllib.request, urllib.error, urllib.parse, io, zipfile, tempfile, shutil, time, math, os, csv, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; OUT=ROOT/"fundamentals-observer-v1.json"
UA="stock-shadow-research/1.0 leo14881-eng@users.noreply.github.com"
BULK_COMPANYFACTS="https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
BULK_SUBMISSIONS="https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
FALLBACK_MAX_REQUESTS=40
FRAME_REQUEST_BUDGET=64

def now(): return datetime.now(timezone.utc).isoformat()
def load(p,d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except Exception: return d
def get(url):
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"application/json","Accept-Encoding":"identity"})
    with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)

_proxy_rate_lock=threading.Lock()
_proxy_next_start=0.0

def proxy_json(url):
    # Global pacing controls request START rate while allowing network I/O to overlap.
    global _proxy_next_start
    with _proxy_rate_lock:
        wait=max(0.0,_proxy_next_start-time.monotonic())
        if wait: time.sleep(wait)
        _proxy_next_start=time.monotonic()+1.0
    proxy="https://r.jina.ai/"+url
    req=urllib.request.Request(proxy,headers={"User-Agent":"stock-shadow-fundamental-observer/1.0","Accept":"text/plain"})
    with urllib.request.urlopen(req,timeout=35) as r: raw=r.read().decode("utf-8","replace").strip()
    # Read-only transport fallback; payload remains SEC JSON.
    if raw.startswith("Markdown Content:"): raw=raw.split("Markdown Content:",1)[1].strip()
    # Proxy may wrap JSON in prose/fences.
    a=raw.find("{"); b=raw.rfind("}")
    if a>=0 and b>a: raw=raw[a:b+1]
    return json.loads(raw)


def download_bulk_zip(url):
    # Stream large SEC archives to disk; do not hold the whole market archive in RAM.
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"application/zip","Accept-Encoding":"identity"})
    tmp=tempfile.NamedTemporaryFile(prefix="stock-shadow-sec-",suffix=".zip",delete=False)
    try:
        with urllib.request.urlopen(req,timeout=120) as r:
            shutil.copyfileobj(r,tmp,length=1024*1024)
        tmp.close()
        with open(tmp.name,"rb") as fp:
            if fp.read(2)!=b"PK": raise ValueError("not_zip_payload")
        return zipfile.ZipFile(tmp.name)
    except Exception:
        tmp.close()
        raise

def bulk_json_by_cik(zf, ciks):
    wanted={f"CIK{int(c):010d}.json":int(c) for c in ciks}
    out={}
    names=set(zf.namelist())
    for name,cik in wanted.items():
        candidates=[name, name.replace("CIK","",1)]
        hit=next((x for x in candidates if x in names),None)
        if hit:
            with zf.open(hit) as fp: out[cik]=json.load(fp)
    return out


FMP_BASE="https://financialmodelingprep.com/stable"

def fmp_bulk_csv(endpoint, year, period, api_key):
    url=FMP_BASE+"/"+endpoint+"?"+urllib.parse.urlencode({"year":year,"period":period,"apikey":api_key})
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"text/csv"})
    with urllib.request.urlopen(req,timeout=60) as r:
        raw=r.read().decode("utf-8-sig","replace")
    if raw.lstrip().startswith(("{","[")):
        raise ValueError("fmp_bulk_not_csv_or_plan_denied")
    return list(csv.DictReader(io.StringIO(raw)))

def _num(v):
    try: return float(v) if v not in (None,"","None","null") else None
    except (TypeError,ValueError): return None

def fmp_bulk_evidence(symbols, api_key):
    """FMP bulk financial statements. Observation-only; no trading decisions consume this output."""
    wanted=set(symbols); raw={s:{"income":[],"balance":[],"cashflow":[]} for s in wanted}
    requests=0; errors=[]
    periods=[(2025,"Q4"),(2026,"Q1"),(2026,"Q2"),(2026,"Q3")]
    endpoints=[("income-statement-bulk","income"),("balance-sheet-statement-bulk","balance"),("cash-flow-statement-bulk","cashflow")]
    for endpoint,bucket in endpoints:
        for year,period in periods:
            requests+=1
            try:
                for row in fmp_bulk_csv(endpoint,year,period,api_key):
                    sym=str(row.get("symbol") or "").upper()
                    if sym in wanted: raw[sym][bucket].append(row)
            except Exception as e:
                errors.append({"endpoint":endpoint,"year":year,"period":period,"type":type(e).__name__,"message":str(e)[:120]})
    out={}
    def series(rows,field):
        vals=[]
        for r in rows:
            v=_num(r.get(field))
            if v is not None: vals.append({"end":r.get("date"),"val":v,"form":"FMP_NORMALIZED","filed":r.get("filingDate"),"period":r.get("period")})
        vals.sort(key=lambda x:str(x.get("end") or ""))
        return vals[-5:]
    for sym,b in raw.items():
        inc=series(b["income"],"revenue"); ni=series(b["income"],"netIncome")
        ocf=series(b["cashflow"],"operatingCashFlow"); fcf=series(b["cashflow"],"freeCashFlow")
        cash=series(b["balance"],"cashAndCashEquivalents"); debt=series(b["balance"],"totalDebt")
        shares=series(b["income"],"weightedAverageShsOut")
        ev={}
        for key,vals in [("revenue",inc),("net_income",ni),("operating_cash_flow",ocf),("free_cash_flow",fcf),("cash",cash),("total_debt",debt),("shares",shares)]:
            ev[key]={"concept":"FMP_NORMALIZED", "values":vals, "trend":_trend(vals)}
        dilution=None
        if len(shares)>=2 and shares[-2]["val"]:
            dilution=(shares[-1]["val"]/shares[-2]["val"]-1)*100
        ev["share_dilution_pct_latest"]=round(dilution,4) if dilution is not None else None
        if any((ev[k]["values"] for k in ("revenue","net_income","operating_cash_flow","cash","total_debt"))):
            out[sym]=ev
    return out,{"provider":"FMP_BULK","attempted":True,"requests":requests,"errors":errors,"matched_symbols":len(out)}

_sec_direct_blocked=False
_sec_direct_lock=threading.Lock()

def frame_json(taxonomy, concept, unit, period):
    global _sec_direct_blocked
    url=f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{period}.json"
    if not _sec_direct_blocked:
        try:
            return get(url),"SEC_FRAMES_DIRECT"
        except urllib.error.HTTPError as e:
            if e.code not in (403,429): raise
            with _sec_direct_lock: _sec_direct_blocked=True
    return proxy_json(url),"SEC_FRAMES_VIA_READONLY_PROXY"

def frame_evidence_by_cik():
    # Market-wide SEC XBRL Frames: one request returns one concept for all reporting entities.
    # Two completed quarters are enough for direction/trend evidence without per-company HTTP loops.
    periods=["CY2025Q4","CY2026Q1","CY2026Q2"]
    instant=["CY2025Q4I","CY2026Q1I","CY2026Q2I"]
    specs=[
      ("revenue","us-gaap","RevenueFromContractWithCustomerExcludingAssessedTax","USD",periods),
      ("revenue_alt","us-gaap","Revenues","USD",periods),
      ("revenue_alt2","us-gaap","SalesRevenueNet","USD",periods),
      ("net_income","us-gaap","NetIncomeLoss","USD",periods),
      ("net_income_alt","us-gaap","ProfitLoss","USD",periods),
      ("operating_cash_flow","us-gaap","NetCashProvidedByUsedInOperatingActivities","USD",periods),
      ("capex","us-gaap","PaymentsToAcquirePropertyPlantAndEquipment","USD",periods),
      ("cash","us-gaap","CashAndCashEquivalentsAtCarryingValue","USD",instant),
      ("cash_alt","us-gaap","CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents","USD",instant),
      ("total_debt","us-gaap","LongTermDebt","USD",instant),
      ("total_debt_alt","us-gaap","LongTermDebtCurrent","USD",instant),
      ("shares","dei","EntityCommonStockSharesOutstanding","shares",instant),
      ("shares_alt","us-gaap","CommonStockSharesOutstanding","shares",instant),
      # Foreign private issuers such as XP can report IFRS rather than US-GAAP.
      ("revenue","ifrs-full","Revenue","USD",["CY2024","CY2025"]),
      ("net_income","ifrs-full","ProfitLoss","USD",["CY2024","CY2025"]),
    ]
    raw={}; transport=set(); request_errors=[]
    all_tasks=[(key,tax,concept,unit,p) for key,tax,concept,unit,ps in specs for p in ps]
    # Every Frames request is market-wide for one concept/period, so execute the full bounded
    # concept set each run instead of rotating by company/hour. Request count is independent
    # of the number of held symbols.
    batches=1
    batch_index=0
    tasks=all_tasks
    def fetch_one(task):
        key,tax,concept,unit,p=task
        last=None
        for attempt in range(3):
            try:
                d,t=frame_json(tax,concept,unit,p)
                return task,d,t
            except urllib.error.HTTPError as e:
                last=e
                if e.code!=429: raise
                time.sleep(2*(attempt+1))
            except json.JSONDecodeError as e:
                last=e; time.sleep(1*(attempt+1))
        raise last
    # Frames are independent market-wide reads. Small bounded parallelism keeps the observer
    # below workflow timeout without creating per-symbol request storms.
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures=[ex.submit(fetch_one,t) for t in tasks]
        for fut in as_completed(futures):
            try:
                (key,tax,concept,unit,p),d,t=fut.result(); transport.add(t)
                for row in d.get("data") or []:
                    cik=int(row.get("cik") or 0)
                    if not cik: continue
                    raw.setdefault(cik,{}).setdefault(key,[]).append(
                        {"end":row.get("end"),"val":row.get("val"),"form":row.get("form"),"filed":row.get("filed"),"period":p})
            except Exception as e:
                request_errors.append({"type":type(e).__name__,"message":str(e)[:160]})
    out={}
    for cik,x in raw.items():
        def vals(primary,*alts):
            rows=x.get(primary) or []
            if not rows:
                for alt in alts:
                    rows=x.get(alt) or []
                    if rows: break
            rows=[r for r in rows if r.get("val") is not None]
            rows.sort(key=lambda r:(str(r.get("end") or ""),str(r.get("filed") or "")))
            return rows[-5:]
        ev={}
        for key,primary,alt in [
            ("revenue","revenue",("revenue_alt","revenue_alt2")),("net_income","net_income",("net_income_alt",)),
            ("operating_cash_flow","operating_cash_flow",()),("cash","cash",("cash_alt",)),
            ("total_debt","total_debt",("total_debt_alt",)),("shares","shares",("shares_alt",))]:
            v=vals(primary,*alt)
            ev[key]={"concept":"SEC_XBRL_FRAME","values":v,"trend":_trend(v)}
        cap={r.get("end"):r for r in vals("capex")}
        fcf=[]
        for r in ev["operating_cash_flow"]["values"]:
            if r.get("end") in cap:
                fcf.append({"end":r["end"],"val":float(r["val"])-float(cap[r["end"]]["val"])})
        ev["free_cash_flow"]={"values":fcf,"trend":_trend(fcf)}
        sh=ev["shares"]["values"]; dilution=None
        if len(sh)>=2 and float(sh[-2]["val"]):
            dilution=(float(sh[-1]["val"])/float(sh[-2]["val"])-1)*100
        ev["share_dilution_pct_latest"]=round(dilution,4) if dilution is not None else None
        out[cik]=ev
    return out,{"requests":len(tasks),"request_budget":FRAME_REQUEST_BUDGET,"total_tasks":len(all_tasks),"batch_index":batch_index,"transports":sorted(transport),"errors":request_errors,"matched_ciks":len(out)}

def merge_financial_evidence(prior,current):
    """Accumulate bounded frame batches without erasing evidence learned in prior runs."""
    prior=prior or {}; current=current or {}; out={}
    for key in ("revenue","net_income","operating_cash_flow","free_cash_flow","cash","total_debt","shares"):
        cur=current.get(key) or {}; old=prior.get(key) or {}
        out[key]=cur if cur.get("values") else old
    out["share_dilution_pct_latest"]=current.get("share_dilution_pct_latest")
    if out["share_dilution_pct_latest"] is None:
        out["share_dilution_pct_latest"]=prior.get("share_dilution_pct_latest")
    return out

def evidence_sufficient(ev):
    return sum(bool((ev.get(k) or {}).get("values")) for k in ("revenue","net_income","operating_cash_flow","cash","total_debt"))>=3

def ticker_map():
    urls=["https://www.sec.gov/files/company_tickers.json","https://www.sec.gov/files/company_tickers_exchange.json"]
    errs=[]
    for url in urls:
        try:
            raw=get(url)
            if isinstance(raw,dict) and "data" in raw and "fields" in raw:
                fields=raw["fields"]; return {str(dict(zip(fields,row)).get("ticker","")).upper().replace(".","-"):dict(zip(fields,row)) for row in raw["data"]},errs
            return {str(v.get("ticker","")).upper().replace(".","-"):v for v in raw.values()},errs
        except urllib.error.HTTPError as e: errs.append({"url":url,"type":"HTTPError","status":e.code,"reason":str(e.reason)})
        except Exception as e: errs.append({"url":url,"type":type(e).__name__,"message":str(e)[:120]})
    # GitHub-hosted runners can be blocked by SEC edge policy. Try a read-only
    # transport proxy for the same SEC JSON; never use proxy-derived trading data.
    try:
        raw=proxy_json("https://www.sec.gov/files/company_tickers.json")
        return {str(v.get("ticker","")).upper().replace(".","-"):v for v in raw.values()},errs
    except Exception as e:
        errs.append({"url":"SEC_TICKER_MAP_VIA_READONLY_PROXY","type":type(e).__name__,"message":str(e)[:120]})
    return {},errs

def resolve_cik_efts(symbol):
    url="https://efts.sec.gov/LATEST/search-index?"+urllib.parse.urlencode({"q":symbol,"dateRange":"all","from":0,"size":20})
    d=get(url)
    hits=((d.get("hits") or {}).get("hits") or [])
    needle="("+symbol.upper().replace("-",".")+")"
    for h in hits:
        src=h.get("_source") or {}
        names=src.get("display_names") or []
        if isinstance(names,str): names=[names]
        if any(needle in str(n).upper() for n in names):
            ciks=src.get("ciks") or []
            if ciks: return int(str(ciks[0]).lstrip("0") or "0")
    return None

def _sec_json_with_readonly_fallback(url):
    global _sec_direct_blocked
    if not _sec_direct_blocked:
        try:
            return get(url),"SEC_DIRECT"
        except urllib.error.HTTPError as e:
            if e.code not in (403,429): raise
            with _sec_direct_lock: _sec_direct_blocked=True
    return proxy_json(url),"SEC_VIA_READONLY_PROXY"

def sec_submission(cik):
    return _sec_json_with_readonly_fallback(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")

def sec_companyfacts(cik):
    return _sec_json_with_readonly_fallback(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")

def _fact_series(facts, concepts, units=("USD","shares")):
    usgaap=(facts.get("facts") or {}).get("us-gaap") or {}
    for concept in concepts:
        node=usgaap.get(concept) or {}
        unit_map=node.get("units") or {}
        rows=[]
        for unit in units:
            rows.extend(unit_map.get(unit) or [])
        if rows:
            # Prefer filed annual/quarterly facts; de-duplicate amended/repeated facts by end date.
            good=[r for r in rows if r.get("end") and r.get("val") is not None and r.get("form") in {"10-K","10-Q","10-K/A","10-Q/A"}]
            by_end={}
            for r in good:
                prev=by_end.get(r["end"])
                if prev is None or str(r.get("filed","")) >= str(prev.get("filed","")): by_end[r["end"]]=r
            return concept, sorted(by_end.values(),key=lambda r:r["end"])
    return None,[]

def _latest_values(facts, concepts, limit=5, units=("USD","shares")):
    concept,rows=_fact_series(facts,concepts,units)
    return concept,[{"end":r["end"],"val":r["val"],"form":r.get("form"),"filed":r.get("filed")} for r in rows[-limit:]]

def _trend(values):
    vals=[float(x["val"]) for x in values if x.get("val") is not None]
    if len(vals)<2: return "INSUFFICIENT"
    a,b=vals[-2],vals[-1]
    if abs(a)<1e-12: return "IMPROVING" if b>0 else ("DETERIORATING" if b<0 else "FLAT")
    # Preserve direction across negative values: -43 -> -55 is deterioration, not improvement.
    pct=((b-a)/abs(a))*100
    if pct>5: return "IMPROVING"
    if pct<-5: return "DETERIORATING"
    return "STABLE"

def financial_evidence(facts):
    specs={
      "revenue":(["RevenueFromContractWithCustomerExcludingAssessedTax","Revenues","SalesRevenueNet"],("USD",)),
      "net_income":(["NetIncomeLoss","ProfitLoss"],("USD",)),
      "operating_cash_flow":(["NetCashProvidedByUsedInOperatingActivities"],("USD",)),
      "cash":(["CashAndCashEquivalentsAtCarryingValue","CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],("USD",)),
      "total_debt":(["LongTermDebtAndFinanceLeaseObligationsCurrent","LongTermDebtCurrent","LongTermDebt"],("USD",)),
      "shares":(["CommonStocksIncludingAdditionalPaidInCapitalMember","CommonStockSharesOutstanding","EntityCommonStockSharesOutstanding"],("shares",)),
    }
    out={}
    for name,(concepts,units) in specs.items():
        concept,values=_latest_values(facts,concepts,units=units)
        out[name]={"concept":concept,"values":values,"trend":_trend(values)}
    # FCF is evidence-derived only when both OCF and capex facts are available.
    _,capex=_latest_values(facts,["PaymentsToAcquirePropertyPlantAndEquipment"],units=("USD",))
    ocf=out["operating_cash_flow"]["values"]
    fcf=[]
    cap_by_end={x["end"]:x for x in capex}
    for x in ocf:
        if x["end"] in cap_by_end:
            fcf.append({"end":x["end"],"val":float(x["val"])-float(cap_by_end[x["end"]]["val"])})
    out["free_cash_flow"]={"values":fcf[-5:],"trend":_trend(fcf)}
    sh=out["shares"]["values"]
    dilution_pct=None
    if len(sh)>=2 and float(sh[-2]["val"]):
        dilution_pct=(float(sh[-1]["val"])/float(sh[-2]["val"])-1)*100
    out["share_dilution_pct_latest"]=round(dilution_pct,4) if dilution_pct is not None else None
    return out

def classify_evidence(ev, risk_flags):
    # Observation-only heuristic classification; never consumed by trading code.
    bad=sum(ev.get(k,{}).get("trend")=="DETERIORATING" for k in ("revenue","net_income","operating_cash_flow","free_cash_flow"))
    dilution=ev.get("share_dilution_pct_latest")
    severe=any(risk_flags.get(k) in (True,"VERIFIED_PRESENT") for k in ("bankruptcy_restructuring","going_concern","delisting_risk"))
    if severe: return "CRITICAL"
    if bad>=2 or (dilution is not None and dilution>=10): return "DETERIORATING"
    if bad==1 or (dilution is not None and dilution>=5) or risk_flags.get("material_8k_present") in (True,"VERIFIED_PRESENT"): return "WATCH"
    available=sum(bool(ev.get(k,{}).get("values")) for k in ("revenue","net_income","operating_cash_flow","cash","total_debt"))
    return "HEALTHY" if available>=3 else "UNKNOWN"

def filing_risk_evidence(latest):
    # None means UNKNOWN, never VERIFIED_ABSENT. Filing metadata can establish 8-K presence only.
    has_8k=any(x["form"].startswith("8-K") for x in latest)
    return {"material_8k_present":True if has_8k else None,
            "going_concern":None,"bankruptcy_restructuring":None,"delisting_risk":None,
            "material_8k_risk":None,"semantic_review_status":"NOT_YET_TEXT_VERIFIED",
            "semantic_risk_state":"UNKNOWN_PENDING_TEXT_REVIEW"}

def main():
    state=load(STATE,{"positions":{}}); old=load(OUT,{"companies":{}})
    companies=old.get("companies",{})
    # Migrate legacy observer output: old runs encoded unverified severe risks as False.
    # False must be reserved for VERIFIED_ABSENT; until text review exists these are unknown.
    for _company in companies.values():
        _risk=_company.get("risk_evidence")
        if isinstance(_risk,dict) and _risk.get("semantic_review_status")=="NOT_YET_TEXT_VERIFIED":
            for _key in ("going_concern","bankruptcy_restructuring","delisting_risk","material_8k_risk"):
                if _risk.get(_key) is False:
                    _risk[_key]=None
            _risk["semantic_risk_state"]="UNKNOWN_PENDING_TEXT_REVIEW"
    symbols=sorted(state.get("positions",{}))
    if not symbols:
        out={"updated_at":now(),"mode":"OBSERVATION_ONLY","strategy_effect":False,"positions":0,"tracked":0,
             "evidence_complete":0,"evidence_pending":0,"pending_symbols":[],"refreshed_this_run":0,
             "bulk_transport":{"attempted":False,"reason":"NO_POSITIONS"},"frames_transport":{"attempted":False},
             "fallback_requests":0,"fallback_request_cap":FALLBACK_MAX_REQUESTS,"errors":[],"mapping_errors":[],
             "status":"OK_EMPTY","companies":{}}
        OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
        print(json.dumps({k:v for k,v in out.items() if k!="companies"})); return
    # SEC XBRL Frames are the market-wide batch primary path. Avoid multi-GB bulk archives in hourly CI.
    # Existing evidence is cached in OUT. Per-company SEC JSON is only a bounded gap-filler.
    ticker_map_data,map_errors=ticker_map()
    symbol_cik={}; errors=[]
    for sym in symbols:
        meta=ticker_map_data.get(sym)
        try:
            cik=int(meta.get("cik_str") or meta.get("cik")) if meta else resolve_cik_efts(sym)
            if cik: symbol_cik[sym]=cik
            else: companies[sym]={"symbol":sym,"status":"NO_SEC_MAPPING","updated_at":now(),"strategy_effect":False}
        except Exception as e:
            errors.append({"symbol":sym,"stage":"CIK_RESOLUTION","type":type(e).__name__})
    bulk={"attempted":False,"reason":"DISABLED_IN_HOURLY_CI_MULTI_GB_ARCHIVE","ciks_requested":len(set(symbol_cik.values()))}
    try:
        frames_by_cik,frames_status=frame_evidence_by_cik()
        frames_status["attempted"]=True
    except Exception as e:
        frames_by_cik={}; frames_status={"attempted":True,"status":"FAILED","error":f"{type(e).__name__}:{str(e)[:160]}"}
    fmp_status={"provider":"FMP_BULK","attempted":False,"reason":"NOT_REQUIRED_SEC_FRAMES_PRIMARY"}
    # Financial evidence comes from market-wide Frames + persisted local evidence.
    # Only genuine coverage gaps enter a small per-company backfill queue.
    ranked=[]
    for sym,cik in symbol_cik.items():
        prior=companies.get(sym) or {}
        merged=merge_financial_evidence(prior.get("financial_evidence"),frames_by_cik.get(cik))
        if not evidence_sufficient(merged):
            ranked.append(sym)
    ranked.sort(key=lambda sym:str((companies.get(sym) or {}).get("gap_refresh_at") or (companies.get(sym) or {}).get("updated_at") or ""))
    refresh_budget=4
    refresh_set=set(ranked[:refresh_budget])
    facts_by_cik={}; subs_by_cik={}
    def fetch_sec_pair(item):
        sym,cik=item
        facts=facts_t=None; sub=sub_t=None; errs=[]
        try: facts,facts_t=sec_companyfacts(cik)
        except Exception as e: errs.append({"symbol":sym,"stage":"COMPANYFACTS","type":type(e).__name__,"message":str(e)[:120]})
        return sym,cik,facts,facts_t,None,None,errs
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures=[ex.submit(fetch_sec_pair,(sym,symbol_cik[sym])) for sym in refresh_set]
        for fut in as_completed(futures):
            sym,cik,facts,facts_t,sub,sub_t,errs=fut.result()
            if facts is not None: facts_by_cik[cik]=(facts,facts_t)
            if sub is not None: subs_by_cik[cik]=(sub,sub_t)
            errors.extend(errs)
    sec_transport={"provider":"SEC_FRAMES_MARKET_BATCH_PLUS_BOUNDED_FINANCIAL_GAP_BACKFILL","attempted":True,"refresh_budget":refresh_budget,
                   "requested_symbols":len(refresh_set),"companyfacts_ok":len(facts_by_cik),"submissions_ok":len(subs_by_cik),
                   "frames_matched_ciks":len(frames_by_cik)}
    refreshed=0; fallback_requests=0
    for s,cik in symbol_cik.items():
        meta=ticker_map_data.get(s) or {}
        try:
            sub_pair=subs_by_cik.get(cik); facts_pair=facts_by_cik.get(cik)
            sub=sub_pair[0] if sub_pair else None; transport=sub_pair[1] if sub_pair else None
            facts=facts_pair[0] if facts_pair else None; facts_transport=facts_pair[1] if facts_pair else None
            frame_ev=frames_by_cik.get(cik); fmp_ev=None
            prior_ev=(companies.get(s) or {}).get("financial_evidence")
            # Filing metadata is optional here. Never turn frame-wide financial evidence
            # back into hundreds of per-company submissions requests.
            if facts is None and not prior_ev and not frame_ev:
                prev=companies.get(s) or {}
                if prev.get("financial_evidence"):
                    prev["status"]="OBSERVED_STALE_FALLBACK"; prev["updated_at"]=now(); prev["strategy_effect"]=False
                    companies[s]=prev
                else:
                    companies[s]={"symbol":s,"cik":cik,"company":meta.get("title"),"status":"SEC_REFRESH_PENDING",
                        "transport":transport,"companyfacts_transport":facts_transport,"updated_at":now(),
                        "strategy_effect":False,"note":"SEC per-company refresh pending; prior evidence unavailable."}
                continue
            recent=((sub or {}).get("filings") or {}).get("recent") or {}
            forms=recent.get("form") or []; dates=recent.get("filingDate") or []; acc=recent.get("accessionNumber") or []
            latest=[]
            for form,date,an in zip(forms,dates,acc):
                if form in {"10-K","10-Q","8-K","10-K/A","10-Q/A","8-K/A"}:
                    latest.append({"form":form,"filing_date":date,"accession":an})
                if len(latest)>=12: break
            risk_forms=[x for x in latest if x["form"].startswith("8-K")]
            sec_ev=financial_evidence(facts) if facts is not None else None
            evidence=merge_financial_evidence(merge_financial_evidence(prior_ev,frame_ev),sec_ev)
            risk_flags=filing_risk_evidence(latest)
            companies[s]={"symbol":s,"cik":cik,"company":(sub or {}).get("name") or meta.get("title"),
                "status":"OBSERVED","transport":transport,"companyfacts_transport":facts_transport,"fundamentals_provider":("SEC_GAP_BACKFILL" if facts is not None else ("SEC_XBRL_FRAMES_MARKET_BATCH" if frame_ev else "CACHED_EVIDENCE")),
                "latest_material_filings":latest,"financial_evidence":evidence,"risk_evidence":risk_flags,
                "recent_8k_count":len(risk_forms),"updated_at":now(),
                "fundamental_state":classify_evidence(evidence,risk_flags),"strategy_effect":False,
                "note":"Observation-only fundamentals evidence; no automatic BUY/ADD/SELL effect."}
            refreshed+=1
        except Exception as e:
            errors.append({"symbol":s,"stage":"EVIDENCE","type":type(e).__name__,"message":str(e)[:120]})
    complete=sum(1 for s in symbols if evidence_sufficient((companies.get(s) or {}).get("financial_evidence") or {}))
    pending_symbols=[s for s in symbols if not evidence_sufficient((companies.get(s) or {}).get("financial_evidence") or {})]
    out={"updated_at":now(),"mode":"OBSERVATION_ONLY","strategy_effect":False,"positions":len(symbols),
         "tracked":sum(1 for s in symbols if s in companies),"evidence_complete":complete,
         "evidence_pending":max(0,len(symbols)-complete),"pending_symbols":pending_symbols,"refreshed_this_run":refreshed,
         "primary_transport":sec_transport,"fmp_transport":fmp_status,"bulk_transport":bulk,"frames_transport":frames_status,"fallback_requests":fallback_requests,"fallback_request_cap":FALLBACK_MAX_REQUESTS,
         "errors":errors,"mapping_errors":map_errors,"status":"OK" if (not errors and complete==len(symbols)) else "PARTIAL","companies":companies}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
    print(json.dumps({k:v for k,v in out.items() if k!="companies"}))

if __name__=="__main__": main()
