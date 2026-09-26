#!/usr/bin/env python3
"""Evidence-gated, rotating forward-upside research across the complete CEX scan.

This is a research prioritizer, NOT a return predictor, recommendation, or trader.
Every symbol is retained. Historical returns never disqualify an asset.
Optional public APIs may fail; failures are explicitly recorded and cannot become BUY.
"""
import datetime as dt
import json
import math
import os
import pathlib
import time
import urllib.parse
import urllib.request

ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"
OUT=ROOT/"hunter-forward-research.json"
CACHE=ROOT/"hunter-market-enrichment.json"
BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
CG=os.getenv("HUNTER_COINGECKO_API","https://api.coingecko.com/api/v3")
DL=os.getenv("HUNTER_DEFILLAMA_API","https://api.llama.fi")
ROTATION=int(os.getenv("HUNTER_RESEARCH_ROTATION","25"))
TRIGGER_LIMIT=int(os.getenv("HUNTER_RESEARCH_TRIGGERS","10"))

def get(url,timeout=18):
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-forward-research/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.load(r)

def read(path,default):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return default

def finite(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (ValueError,TypeError,OverflowError):return None

def unique_symbols(rows,key):
    """Never silently attach another project's financials via a shared ticker."""
    seen={};collisions=set()
    for row in rows:
        if not isinstance(row,dict):continue
        sym=str(row.get(key) or "").strip().upper()
        if not sym or not sym.isalnum():continue
        if sym in seen:collisions.add(sym)
        else:seen[sym]=row
    for sym in collisions:seen.pop(sym,None)
    return seen,sorted(collisions)

def cache_age_hours(cache,now):
    try:
        age=(now-dt.datetime.fromisoformat(cache["as_of_utc"])).total_seconds()/3600
        return age if age>=0 else float("inf")
    except (KeyError,ValueError,TypeError):return float("inf")

def enrichment(now,previous,errors):
    if cache_age_hours(previous,now)<24:
        return previous
    result={"as_of_utc":now.isoformat(),"coingecko":[],"defillama":[],"errors":{}}
    # Free CoinGecko only supplies a market-cap-ordered subset. No claim of
    # fundamentals coverage for coins absent from its pages.
    for page in (1,2):
        url=CG+"/coins/markets?"+urllib.parse.urlencode({
            "vs_currency":"usd","order":"market_cap_desc","per_page":250,
            "page":page,"sparkline":"false"})
        try:
            data=get(url)
            if not isinstance(data,list):raise ValueError("non-list response")
            result["coingecko"].extend(data)
            if len(data)<250:break
            time.sleep(1)
        except Exception as exc:
            result["errors"]["coingecko_page_"+str(page)]=str(exc)[:180]
            break
    try:
        data=get(DL+"/protocols")
        if not isinstance(data,list):raise ValueError("non-list response")
        result["defillama"]=data
    except Exception as exc:
        result["errors"]["defillama"]=str(exc)[:180]
    # If the refresh fails, retain old evidence WITH its original timestamp,
    # not falsely marked fresh. Empty new data never overwrites valid old data.
    for source in ("coingecko","defillama"):
        if not result[source] and previous.get(source):
            result[source]=previous[source]
            result[source+"_as_of_utc"]=previous.get(source+"_as_of_utc",previous.get("as_of_utc"))
            errors[source]="refresh failed; retained timestamped cached data"
        else:result[source+"_as_of_utc"]=now.isoformat() if result[source] else None
    errors.update(result["errors"])
    return result

def compact_market(market,universe):
    """Persist only matched symbols and fields; never commit raw 12MB API dumps."""
    allowed=set(universe)
    keep={"coingecko":("symbol","id","market_cap","fully_diluted_valuation",
                       "circulating_supply","total_supply"),
          "defillama":("symbol","name","slug","tvl","change_7d","change_1m")}
    for source,fields in keep.items():
        rows=market.get(source) or []
        market[source]=[{k:r.get(k) for k in fields}
                        for r in rows if isinstance(r,dict)
                        and str(r.get("symbol") or "").upper() in allowed]
    market["cache_scope"]="ONLY_CURRENT_CEX_SYMBOLS_AND_RESEARCH_FIELDS"
    market["cache_universe_count"]=len(allowed)
    return market

def select_rotation(coins,previous):
    symbols=sorted(coins)
    if not symbols:return [],0,[]
    cursor=int(previous.get("rotation_cursor",0))%len(symbols)
    n=min(max(1,ROTATION),len(symbols))
    rotation=[symbols[(cursor+i)%len(symbols)] for i in range(n)]
    triggered=[]
    for sym,coin in coins.items():
        change=finite(coin.get("change_since_previous_scan_pct"))
        day=finite(coin.get("change_24h_pct"))
        if (change is not None and abs(change)>=8) or (day is not None and abs(day)>=15):
            triggered.append(sym)
    # Triggers are researched regardless of direction or prior rally; queue
    # overflow remains visible, never silently dropped.
    triggered.sort(key=lambda s:(-abs(finite(coins[s].get("change_since_previous_scan_pct")) or 0),
                                 -abs(finite(coins[s].get("change_24h_pct")) or 0),s))
    chosen=list(dict.fromkeys(triggered[:TRIGGER_LIMIT]+rotation))
    return chosen,(cursor+n)%len(symbols),triggered

def candle_features(candles,turnover):
    if not isinstance(candles,list) or len(candles)<15:
        return None,"insufficient 1d candles (min 15)"
    rows=[]
    for c in candles:
        if not isinstance(c,list) or len(c)<8:return None,"malformed candle"
        close=finite(c[4]);high=finite(c[2]);quote=finite(c[7])
        if None in (close,high,quote) or close<=0 or high<=0 or quote<0:
            return None,"invalid candle values"
        rows.append((close,high,quote))
    # Last candle may still be open. Compare only completed days.
    completed=rows[:-1]
    if len(completed)<14:return None,"insufficient completed candles"
    prior7=completed[-14:-7];last7=completed[-7:]
    old_volume=sum(r[2] for r in prior7)
    new_volume=sum(r[2] for r in last7)
    price=rows[-1][0]
    high30=max(r[1] for r in completed[-30:])
    return {"last_7d_quote_volume":round(new_volume,2),
            "previous_7d_quote_volume":round(old_volume,2),
            "volume_7d_ratio":round(new_volume/old_volume,3) if old_volume>0 else None,
            "drawdown_from_completed_30d_high_pct":round((price/high30-1)*100,3),
            "return_vs_7_completed_days_pct":round((price/last7[0][0]-1)*100,3),
            "volume_24h_usdt":turnover},None

def research_one(sym,coin,cg,dl,now):
    pairs=[p for p in coin.get("pairs",[]) if p.get("venue")=="binance"]
    result={"symbol":sym,"researched_at_utc":now.isoformat(),
            "stage_price_only":coin.get("stage"),
            "past_return_not_an_eligibility_gate":True,
            "current_reference_price":coin.get("reference_price"),
            "status":"DATA_INCOMPLETE_RESEARCH_ONLY","capital_ready":False,
            "missing_facts":[],"observations":{},"source_urls":[]}
    cap=cg.get(sym)
    if cap:
        result["observations"]["coingecko_id"]=cap.get("id")
        for key in ("market_cap","fully_diluted_valuation","circulating_supply","total_supply"):
            result["observations"][key]=finite(cap.get(key))
        result["source_urls"].append("https://www.coingecko.com/en/coins/"+str(cap.get("id")))
        mc=finite(cap.get("market_cap"));fdv=finite(cap.get("fully_diluted_valuation"))
        if mc and fdv and fdv/mc>=3:
            result["missing_facts"].append("FDV >=3x circulating cap: verify future sellable supply and unlocks")
    else:result["missing_facts"].append("Unique CoinGecko identity / market cap / FDV unavailable")
    proto=dl.get(sym)
    if proto:
        result["observations"]["defillama_protocol_name"]=proto.get("name")
        result["observations"]["defillama_tvl_usd"]=finite(proto.get("tvl"))
        result["observations"]["defillama_tvl_7d_change_pct"]=finite(proto.get("change_7d"))
        result["observations"]["defillama_tvl_1m_change_pct"]=finite(proto.get("change_1m"))
        result["observations"]["protocol_match_status"]="TICKER_ONLY__OFFICIAL_CONTRACT_VERIFICATION_REQUIRED"
        result["source_urls"].append("https://defillama.com/protocol/"+str(proto.get("slug") or ""))
    else:result["missing_facts"].append("Independent protocol demand/value-capture evidence unavailable")
    if pairs:
        p=max(pairs,key=lambda r:finite(r.get("volume_24h_usdt")) or 0)
        pair=p["pair"]
        try:
            url=BN+"/api/v3/klines?"+urllib.parse.urlencode({"symbol":pair,"interval":"1d","limit":35})
            features,err=candle_features(get(url),finite(p.get("volume_24h_usdt")))
            if err:result["missing_facts"].append(err)
            else:result["observations"]["market_structure"]=features
            result["source_urls"].append("https://www.binance.com/en/trade/"+sym+"_USDT")
        except Exception as exc:
            result["missing_facts"].append("Binance candle API: "+str(exc)[:140])
    else:result["missing_facts"].append("No verified Binance spot candle source")
    # TVL is NOT tokenholder revenue, and ticker matches are NOT contract identity.
    result["missing_facts"].extend([
        "Primary-source token contract and exact asset identity not verified",
        "Forward 30/90/180-day unlocks, insider sellable float and emissions unverified",
        "Primary-source catalyst and causal tokenholder value capture unverified",
        "No evidence-grounded bear/base/bull terminal valuation or BTC-relative FRM",
        "Executable venue depth, portfolio and counterparty gates not completed"])
    # These are research-attention signals only, not return predictions.
    market=result["observations"].get("market_structure") or {}
    signals=[]
    if (market.get("volume_7d_ratio") or 0)>=1.5:
        signals.append("7D_VOLUME_EXPANSION")
    if proto and (finite(proto.get("change_1m")) or 0)>=20:
        signals.append("PROTOCOL_TVL_1M_GROWTH_PROXY_UNVERIFIED")
    if (finite(coin.get("change_since_previous_scan_pct")) or 0)>=8:
        signals.append("RECENT_REPRICING_REEVALUATE_FORWARD_UPSIDE")
    result["research_attention_signals"]=signals
    return result

def build_report(scan,previous,market,now,get_candles=True):
    coins=scan.get("coins") or {}
    if not isinstance(coins,dict) or not coins:raise ValueError("No CEX coins: refuse empty research success")
    chosen,cursor,triggered=select_rotation(coins,previous)
    cg,cg_collision=unique_symbols(market.get("coingecko") or [],"symbol")
    dl,dl_collision=unique_symbols(market.get("defillama") or [],"symbol")
    results={}
    for sym in chosen:
        # Tests may patch get(); live runner uses public endpoints.
        results[sym]=research_one(sym,coins[sym],cg,dl,now)
    previous_results=previous.get("research_results") or {}
    for sym,item in previous_results.items():
        if sym not in results and sym in coins:results[sym]=item
    current_count=sum(1 for x in results.values() if x.get("researched_at_utc")==now.isoformat())
    return {"schema":"hunter_forward_research_v1","as_of_utc":now.isoformat(),
            "universe_scan_as_of_utc":scan.get("as_of_utc"),
            "universe_coverage_status":scan.get("coverage_status"),
            "universe_size":len(coins),"lightweight_universe_review_count":len(coins),
            "deep_research_this_cycle":current_count,
            "deep_research_total_cached":len(results),
            "rotation_cursor":cursor,"rotation_batch":chosen,
            "triggered_total":len(triggered),"triggered_researched":triggered[:TRIGGER_LIMIT],
            "trigger_backlog":triggered[TRIGGER_LIMIT:],
            "coingecko_unique_symbols":len(cg),"coingecko_ambiguous_symbols":cg_collision,
            "defillama_unique_symbols":len(dl),"defillama_ambiguous_symbols":dl_collision,
            "capital_ready":[],"buy_proposals":[],
            "capital_policy":"RESEARCH_ONLY__NEVER_INFER_BUY_FROM_PRICE_OR_TVL",
            "mandate":"FORWARD_APPRECIATION_FROM_CURRENT_ENTRY_PRICE_REGARDLESS_OF_PRIOR_RALLY",
            "research_results":results}

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    scan=read(SCAN,{})
    if not scan.get("binance_complete"):
        raise SystemExit("Fatal: no verified complete Binance snapshot; research not run")
    now=dt.datetime.now(dt.timezone.utc)
    previous=read(OUT,{})
    errors={}
    cached=read(CACHE,{})
    market=compact_market(enrichment(now,cached,errors),scan.get("coins") or {})
    CACHE.write_text(json.dumps(market,ensure_ascii=False,indent=2)+"\n")
    report=build_report(scan,previous,market,now)
    report["enrichment_errors"]=errors
    report["market_enrichment_as_of_utc"]=market.get("as_of_utc")
    report["market_enrichment_source_times"]={x:market.get(x+"_as_of_utc") for x in ("coingecko","defillama")}
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:report[k] for k in ("as_of_utc","universe_size","universe_coverage_status",
        "lightweight_universe_review_count","deep_research_this_cycle","deep_research_total_cached",
        "triggered_total","capital_ready","enrichment_errors")},ensure_ascii=False))
    return 0

if __name__=="__main__":raise SystemExit(main())
