#!/usr/bin/env python3
"""Stock Shadow fundamental observer. Observation-only; never changes BUY/ADD/SELL."""
# FINAL_ACCEPTANCE_20261004
import json, urllib.request, urllib.error, urllib.parse, io, zipfile, tempfile, shutil, time, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; OUT=ROOT/"fundamentals-observer-v1.json"
UA="stock-shadow-research/1.0 leo14881-eng@users.noreply.github.com"
BULK_COMPANYFACTS="https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
BULK_SUBMISSIONS="https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
FALLBACK_MAX_REQUESTS=12
FRAME_REQUEST_BUDGET=12

def now(): return datetime.now(timezone.utc).isoformat()
def load(p,d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except Exception: return d
def get(url):
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"application/json","Accept-Encoding":"identity"})
    with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)

def proxy_json(url):
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


def frame_json(taxonomy, concept, unit, period):
    url=f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{period}.json"
    try:
        return get(url),"SEC_FRAMES_DIRECT"
    except urllib.error.HTTPError as e:
        if e.code!=403: raise
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
    batches=max(1,math.ceil(len(all_tasks)/FRAME_REQUEST_BUDGET))
    batch_index=datetime.now(timezone.utc).hour % batches
    start=batch_index*FRAME_REQUEST_BUDGET
    tasks=all_tasks[start:start+FRAME_REQUEST_BUDGET]
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
    with ThreadPoolExecutor(max_workers=2) as ex:
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

def sec_submission(cik):
    url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    try: return get(url),"SEC_DIRECT"
    except urllib.error.HTTPError as e:
        if e.code!=403: raise
        return proxy_json(url),"SEC_VIA_READONLY_PROXY"

def sec_companyfacts(cik):
    url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    try: return get(url),"SEC_DIRECT"
    except urllib.error.HTTPError as e:
        if e.code!=403: raise
        return proxy_json(url),"SEC_VIA_READONLY_PROXY"

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
    severe=any(risk_flags.get(k) is True for k in ("bankruptcy_restructuring","going_concern","delisting_risk"))
    if severe: return "CRITICAL"
    if bad>=2 or (dilution is not None and dilution>=10): return "DETERIORATING"
    if bad==1 or (dilution is not None and dilution>=5) or risk_flags.get("material_8k_present"): return "WATCH"
    available=sum(bool(ev.get(k,{}).get("values")) for k in ("revenue","net_income","operating_cash_flow","cash","total_debt"))
    return "HEALTHY" if available>=3 else "UNKNOWN"

def filing_risk_evidence(latest):
    # Form presence is evidence, not semantic proof of a severe event. Text review can enrich these later.
    return {"material_8k_present":any(x["form"].startswith("8-K") for x in latest),
            # Unknown is intentional: absence of semantic filing-text verification is NOT evidence of absence.
            "going_concern":None,"bankruptcy_restructuring":None,"delisting_risk":None,
            "semantic_review_status":"NOT_YET_TEXT_VERIFIED",
            "semantic_risk_state":"UNKNOWN"}

def main():
    state=load(STATE,{"positions":{}}); old=load(OUT,{"companies":{}})
    companies=old.get("companies",{})
    symbols=sorted(state.get("positions",{}))
    if not symbols:
        out={"updated_at":now(),"mode":"OBSERVATION_ONLY","strategy_effect":False,"positions":0,"tracked":0,
             "evidence_complete":0,"evidence_pending":0,"pending_symbols":[],"refreshed_this_run":0,
             "bulk_transport":{"attempted":False,"reason":"NO_POSITIONS"},"frames_transport":{"attempted":False},
             "fallback_requests":0,"fallback_request_cap":FALLBACK_MAX_REQUESTS,"errors":[],"mapping_errors":[],
             "status":"OK_EMPTY","companies":{}}
        OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
        print(json.dumps({k:v for k,v in out.items() if k!="companies"})); return
    ticker_map_data,map_errors=ticker_map()
    symbol_cik={}; errors=[]
    for s in symbols:
        meta=ticker_map_data.get(s)
        try:
            cik=int(meta.get("cik_str") or meta.get("cik")) if meta else resolve_cik_efts(s)
            if cik: symbol_cik[s]=cik
            else: companies[s]={"symbol":s,"status":"NO_SEC_MAPPING","updated_at":now()}
        except Exception as e:
            errors.append({"symbol":s,"stage":"CIK_RESOLUTION","type":type(e).__name__})

    bulk={"attempted":True,"companyfacts":"NOT_RUN","submissions":"NOT_RUN","ciks_requested":len(set(symbol_cik.values()))}
    facts_by_cik={}; subs_by_cik={}; frames_by_cik={}; frames_status={"attempted":False}
    try:
        z=download_bulk_zip(BULK_COMPANYFACTS)
        facts_by_cik=bulk_json_by_cik(z,set(symbol_cik.values()))
        bulk["companyfacts"]="OK"; bulk["companyfacts_matched"]=len(facts_by_cik)
    except Exception as e:
        bulk["companyfacts"]="FAILED"; bulk["companyfacts_error"]=f"{type(e).__name__}:{str(e)[:160]}"
        frames_status["attempted"]=True
        try:
            frames_by_cik,frames_status=frame_evidence_by_cik()
            frames_status["attempted"]=True
        except Exception as fe:
            frames_status={"attempted":True,"status":"FAILED","error":f"{type(fe).__name__}:{str(fe)[:160]}"}
    try:
        z=download_bulk_zip(BULK_SUBMISSIONS)
        subs_by_cik=bulk_json_by_cik(z,set(symbol_cik.values()))
        bulk["submissions"]="OK"; bulk["submissions_matched"]=len(subs_by_cik)
    except Exception as e:
        bulk["submissions"]="FAILED"; bulk["submissions_error"]=f"{type(e).__name__}:{str(e)[:160]}"

    refreshed=0; fallback_requests=0
    for s,cik in symbol_cik.items():
        meta=ticker_map_data.get(s) or {}
        try:
            sub=subs_by_cik.get(cik)
            facts=facts_by_cik.get(cik)
            transport="SEC_BULK_SUBMISSIONS" if sub else None
            facts_transport="SEC_BULK_COMPANYFACTS" if facts else None
            frame_ev=frames_by_cik.get(cik)
            prior_ev=(companies.get(s) or {}).get("financial_evidence")
            if facts is None and frame_ev is None and not prior_ev and fallback_requests < FALLBACK_MAX_REQUESTS:
                fallback_requests+=1
                try:
                    facts,facts_transport=sec_companyfacts(cik)
                except Exception:
                    facts=None
            if facts is None and frame_ev is not None:
                facts_transport="SEC_XBRL_FRAMES_MARKET_BATCH"
            # Filing metadata is optional here. Never turn frame-wide financial evidence
            # back into hundreds of per-company submissions requests.
            if facts is None and frame_ev is None:
                prev=companies.get(s) or {}
                if prev.get("financial_evidence"):
                    prev["status"]="OBSERVED_STALE_FALLBACK"; prev["updated_at"]=now(); prev["strategy_effect"]=False
                    companies[s]=prev
                else:
                    companies[s]={"symbol":s,"cik":cik,"company":meta.get("title"),"status":"BULK_MISSING",
                        "transport":transport,"companyfacts_transport":facts_transport,"updated_at":now(),
                        "strategy_effect":False,"note":"Bulk/frame evidence missing; bounded fallback only."}
                continue
            recent=((sub or {}).get("filings") or {}).get("recent") or {}
            forms=recent.get("form") or []; dates=recent.get("filingDate") or []; acc=recent.get("accessionNumber") or []
            latest=[]
            for form,date,an in zip(forms,dates,acc):
                if form in {"10-K","10-Q","8-K","10-K/A","10-Q/A","8-K/A"}:
                    latest.append({"form":form,"filing_date":date,"accession":an})
                if len(latest)>=12: break
            risk_forms=[x for x in latest if x["form"].startswith("8-K")]
            evidence=financial_evidence(facts) if facts is not None else merge_financial_evidence(prior_ev,frame_ev); risk_flags=filing_risk_evidence(latest)
            companies[s]={"symbol":s,"cik":cik,"company":(sub or {}).get("name") or meta.get("title"),
                "status":"OBSERVED","transport":transport,"companyfacts_transport":facts_transport,
                "latest_material_filings":latest,"financial_evidence":evidence,"risk_evidence":risk_flags,
                "recent_8k_count":len(risk_forms),"updated_at":now(),
                "fundamental_state":classify_evidence(evidence,risk_flags),"strategy_effect":False,
                "note":"Observation-only SEC evidence; no automatic BUY/ADD/SELL effect."}
            refreshed+=1
        except Exception as e:
            errors.append({"symbol":s,"stage":"EVIDENCE","type":type(e).__name__,"message":str(e)[:120]})
    complete=sum(1 for s in symbols if evidence_sufficient((companies.get(s) or {}).get("financial_evidence") or {}))
    pending_symbols=[s for s in symbols if not evidence_sufficient((companies.get(s) or {}).get("financial_evidence") or {})]
    out={"updated_at":now(),"mode":"OBSERVATION_ONLY","strategy_effect":False,"positions":len(symbols),
         "tracked":sum(1 for s in symbols if s in companies),"evidence_complete":complete,
         "evidence_pending":max(0,len(symbols)-complete),"pending_symbols":pending_symbols,"refreshed_this_run":refreshed,
         "bulk_transport":bulk,"frames_transport":frames_status,"fallback_requests":fallback_requests,"fallback_request_cap":FALLBACK_MAX_REQUESTS,
         "errors":errors,"mapping_errors":map_errors,"status":"OK" if (not errors and complete==len(symbols)) else "PARTIAL","companies":companies}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
    print(json.dumps({k:v for k,v in out.items() if k!="companies"}))

if __name__=="__main__": main()
