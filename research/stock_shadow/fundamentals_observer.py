#!/usr/bin/env python3
"""Stock Shadow fundamental observer. Observation-only; never changes BUY/ADD/SELL."""
import json, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; OUT=ROOT/"fundamentals-observer-v1.json"
UA="stock-shadow-research/1.0 leo14881-eng@users.noreply.github.com"
MAX_REFRESH=12

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
    # Rotate stale/unseen holdings; bounded SEC load per run.
    targets=sorted(symbols,key=lambda s:(s in companies,companies.get(s,{}).get("updated_at","")))[:MAX_REFRESH]
    refreshed=0; errors=[]
    for s in targets:
        meta=ticker_map_data.get(s)
        try:
            cik=int(meta.get("cik_str") or meta.get("cik")) if meta else resolve_cik_efts(s)
        except urllib.error.HTTPError as e:
            errors.append({"symbol":s,"stage":"CIK_RESOLUTION","type":"HTTPError","status":e.code}); continue
        except Exception as e:
            errors.append({"symbol":s,"stage":"CIK_RESOLUTION","type":type(e).__name__}); continue
        if not cik:
            companies[s]={"symbol":s,"status":"NO_SEC_MAPPING","updated_at":now()}; continue
        try:
            sub,transport=sec_submission(cik)
            facts,facts_transport=sec_companyfacts(cik)
            recent=(sub.get("filings") or {}).get("recent") or {}
            forms=recent.get("form") or []; dates=recent.get("filingDate") or []; acc=recent.get("accessionNumber") or []
            latest=[]
            for form,date,an in zip(forms,dates,acc):
                if form in {"10-K","10-Q","8-K","10-K/A","10-Q/A","8-K/A"}:
                    latest.append({"form":form,"filing_date":date,"accession":an})
                if len(latest)>=12: break
            risk_forms=[x for x in latest if x["form"].startswith("8-K")]
            evidence=financial_evidence(facts)
            risk_flags=filing_risk_evidence(latest)
            state_name=classify_evidence(evidence,risk_flags)
            companies[s]={"symbol":s,"cik":cik,"company":sub.get("name") or (meta or {}).get("title"),
                "status":"OBSERVED","transport":transport,"companyfacts_transport":facts_transport,
                "latest_material_filings":latest,"financial_evidence":evidence,"risk_evidence":risk_flags,
                "recent_8k_count":len(risk_forms),"updated_at":now(),
                "fundamental_state":state_name,
                "strategy_effect":False,
                "note":"Observation-only SEC evidence; no automatic BUY/ADD/SELL effect."}
            refreshed+=1
        except urllib.error.HTTPError as e:
            errors.append({"symbol":s,"type":"HTTPError","status":e.code})
        except Exception as e:
            errors.append({"symbol":s,"type":type(e).__name__})
    out={"updated_at":now(),"mode":"OBSERVATION_ONLY","strategy_effect":False,"positions":len(symbols),
         "tracked":sum(1 for s in symbols if s in companies),"refreshed_this_run":refreshed,
         "refresh_limit":MAX_REFRESH,"errors":errors,"mapping_errors":map_errors,"status":"OK" if not errors else "PARTIAL","companies":companies}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
    print(json.dumps({k:v for k,v in out.items() if k!="companies"}))

if __name__=="__main__": main()
