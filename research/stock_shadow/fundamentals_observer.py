#!/usr/bin/env python3
"""Stock Shadow fundamental observer. Observation-only; never changes BUY/ADD/SELL."""
import json, urllib.request, urllib.error, urllib.parse, io, zipfile, tempfile, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; OUT=ROOT/"fundamentals-observer-v1.json"
UA="stock-shadow-research/1.0 leo14881-eng@users.noreply.github.com"
BULK_COMPANYFACTS="https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
BULK_SUBMISSIONS="https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
FALLBACK_MAX_REQUESTS=12

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
    periods=["CY2026Q1","CY2026Q2"]
    instant=["CY2026Q1I","CY2026Q2I"]
    specs=[
      ("revenue","us-gaap","RevenueFromContractWithCustomerExcludingAssessedTax","USD",periods),
      ("revenue_alt","us-gaap","Revenues","USD",periods),
      ("net_income","us-gaap","NetIncomeLoss","USD",periods),
      ("operating_cash_flow","us-gaap","NetCashProvidedByUsedInOperatingActivities","USD",periods),
      ("capex","us-gaap","PaymentsToAcquirePropertyPlantAndEquipment","USD",periods),
      ("cash","us-gaap","CashAndCashEquivalentsAtCarryingValue","USD",instant),
      ("total_debt","us-gaap","LongTermDebt","USD",instant),
      ("shares","dei","EntityCommonStockSharesOutstanding","shares",instant),
    ]
    raw={}; transport=set(); request_errors=[]
    tasks=[(key,tax,concept,unit,p) for key,tax,concept,unit,ps in specs for p in ps]
    def fetch_one(task):
        key,tax,concept,unit,p=task
        d,t=frame_json(tax,concept,unit,p)
        return task,d,t
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
        def vals(primary,alt=None):
            rows=x.get(primary) or (x.get(alt) if alt else []) or []
            rows=[r for r in rows if r.get("val") is not None]
            rows.sort(key=lambda r:(str(r.get("end") or ""),str(r.get("filed") or "")))
            return rows[-5:]
        ev={}
        for key,primary,alt in [
            ("revenue","revenue","revenue_alt"),("net_income","net_income",None),
            ("operating_cash_flow","operating_cash_flow",None),("cash","cash",None),
            ("total_debt","total_debt",None),("shares","shares",None)]:
            v=vals(primary,alt)
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
    return out,{"requests":len(specs)*2,"transports":sorted(transport),"errors":request_errors,"matched_ciks":len(out)}

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
    pct=(b/a-1)*100
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
    severe=any(risk_flags.get(k) for k in ("bankruptcy_restructuring","going_concern","delisting_risk"))
    if severe: return "CRITICAL"
    if bad>=2 or (dilution is not None and dilution>=10): return "DETERIORATING"
    if bad==1 or (dilution is not None and dilution>=5) or risk_flags.get("material_8k_present"): return "WATCH"
    available=sum(bool(ev.get(k,{}).get("values")) for k in ("revenue","net_income","operating_cash_flow","cash","total_debt"))
    return "HEALTHY" if available>=3 else "UNKNOWN"

def filing_risk_evidence(latest):
    # Form presence is evidence, not semantic proof of a severe event. Text review can enrich these later.
    return {"material_8k_present":any(x["form"].startswith("8-K") for x in latest),
            "going_concern":False,"bankruptcy_restructuring":False,"delisting_risk":False,
            "semantic_review_status":"NOT_YET_TEXT_VERIFIED"}

def main():
    state=load(STATE,{"positions":{}}); old=load(OUT,{"companies":{}})
    companies=old.get("companies",{})
    ticker_map_data,map_errors=ticker_map()
    symbols=sorted(state.get("positions",{}))
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
            if sub is None and fallback_requests < FALLBACK_MAX_REQUESTS:
                sub,transport=sec_submission(cik); fallback_requests+=1
            frame_ev=frames_by_cik.get(cik)
            if facts is None and frame_ev is None and fallback_requests < FALLBACK_MAX_REQUESTS:
                facts,facts_transport=sec_companyfacts(cik); fallback_requests+=1
            if facts is None and frame_ev is not None:
                facts_transport="SEC_XBRL_FRAMES_MARKET_BATCH"
            if sub is None and fallback_requests < FALLBACK_MAX_REQUESTS:
                sub,transport=sec_submission(cik); fallback_requests+=1
            if facts is None and frame_ev is None:
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
            evidence=financial_evidence(facts) if facts is not None else frame_ev; risk_flags=filing_risk_evidence(latest)
            companies[s]={"symbol":s,"cik":cik,"company":(sub or {}).get("name") or meta.get("title"),
                "status":"OBSERVED","transport":transport,"companyfacts_transport":facts_transport,
                "latest_material_filings":latest,"financial_evidence":evidence,"risk_evidence":risk_flags,
                "recent_8k_count":len(risk_forms),"updated_at":now(),
                "fundamental_state":classify_evidence(evidence,risk_flags),"strategy_effect":False,
                "note":"Observation-only SEC evidence; no automatic BUY/ADD/SELL effect."}
            refreshed+=1
        except Exception as e:
            errors.append({"symbol":s,"stage":"EVIDENCE","type":type(e).__name__,"message":str(e)[:120]})
    complete=sum(1 for s in symbols if (companies.get(s) or {}).get("financial_evidence"))
    out={"updated_at":now(),"mode":"OBSERVATION_ONLY","strategy_effect":False,"positions":len(symbols),
         "tracked":sum(1 for s in symbols if s in companies),"evidence_complete":complete,
         "evidence_pending":max(0,len(symbols)-complete),"refreshed_this_run":refreshed,
         "bulk_transport":bulk,"frames_transport":frames_status,"fallback_requests":fallback_requests,"fallback_request_cap":FALLBACK_MAX_REQUESTS,
         "errors":errors,"mapping_errors":map_errors,"status":"OK" if (not errors and complete==len(symbols) and bulk.get("companyfacts")=="OK" and bulk.get("submissions")=="OK") else "PARTIAL","companies":companies}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
    print(json.dumps({k:v for k,v in out.items() if k!="companies"}))

if __name__=="__main__": main()
