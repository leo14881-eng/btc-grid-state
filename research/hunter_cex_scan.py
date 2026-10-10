#!/usr/bin/env python3
"""Binance and independently verified Bybit USDT spot discovery; research only."""
import datetime as dt
import json
import importlib.util
import os
import pathlib
import random
import time
import urllib.error
import urllib.parse
import urllib.request

OUT=pathlib.Path("research/results")
HISTORY=OUT/"hunter-universe-history.json"
BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
BB=os.getenv("HUNTER_BYBIT_API","https://api.bytick.com")
EXCLUDE={"USDC","USDT","BUSD","FDUSD","TUSD","USDP","DAI","USDE","PYUSD","EUR","TRY","BRL","GBP","AUD","UST","USTC"}
LEVERAGED=("UP","DOWN","BULL","BEAR")
BSTOCK_API="https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai?type=3"

def fetch(url):
    error=None
    for i in range(4):
        try:
            request=urllib.request.Request(url,headers={"User-Agent":"hunter-cex/1.0","Accept":"application/json"})
            with urllib.request.urlopen(request,timeout=25) as response:return json.load(response)
        except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,ValueError) as exc:
            error=exc
            if isinstance(exc,urllib.error.HTTPError) and exc.code==403 and ("bybit.com" in url or "bytick.com" in url):
                body=exc.read(600).decode("utf-8","replace").lower()
                if "block access from your country" in body:
                    raise RuntimeError("BYBIT_GEO_BLOCKED_EGRESS_COUNTRY__REQUIRES_AUTHORIZED_REGIONAL_RUNNER") from exc
            if isinstance(exc,urllib.error.HTTPError) and exc.code in (400,401,403,404,451):break
            if i<3:time.sleep(min(2**i,8)+random.random()/5)
    raise RuntimeError(f"{url.split('?')[0]}: {type(error).__name__}: {error}")

def valid(base):
    return bool(base) and base not in EXCLUDE

def bstock_bases():
    """Authoritative Binance type=3 tokenized-security symbols. Crypto Hunter excludes them."""
    payload=fetch(BSTOCK_API); out=set()
    def walk(x):
        if isinstance(x,dict):
            for k,v in x.items():
                if str(k).lower()=="symbol" and isinstance(v,str) and v:
                    out.add(v.upper().replace("/USDT","").replace("USDT",""))
                walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
    walk(payload)
    if not out: raise RuntimeError("BINANCE_BSTOCK_CLASSIFICATION_EMPTY")
    return out

def number(x):
    try:
        v=float(x)
        return v if abs(v)<float("inf") else None
    except (TypeError,ValueError,OverflowError):return None

def binance():
    bstocks=bstock_bases()
    meta=fetch(BN+"/api/v3/exchangeInfo")
    active={p["symbol"]:p["baseAsset"] for p in meta["symbols"]
            if p.get("quoteAsset")=="USDT" and p.get("status")=="TRADING"
            and p.get("isSpotTradingAllowed",True) and valid(p.get("baseAsset",""))
            and p.get("baseAsset","").upper() not in bstocks}
    ticks=fetch(BN+"/api/v3/ticker/24hr")
    if not isinstance(ticks,list):raise RuntimeError("Binance ticker response not list")
    quotes={p["symbol"]:p for p in ticks if isinstance(p,dict) and "symbol" in p}
    rows=[];missing=[]
    for symbol,base in active.items():
        q=quotes.get(symbol,{})
        price=number(q.get("lastPrice"));vol=number(q.get("quoteVolume"));change=number(q.get("priceChangePercent"))
        if price is None or price<=0 or vol is None or vol<0 or change is None:
            missing.append(symbol);continue
        rows.append(dict(venue="binance",pair=symbol,base=base,price=price,volume_24h_usdt=vol,change_24h_pct=change))
    return rows,dict(active_pairs=len(active),valid_pairs=len(rows),missing_or_invalid=missing,excluded_bstocks=sorted(bstocks))

def bybit(base_url=None):
    base_url=base_url or BB
    active={};cursor="";seen=set()
    while True:
        params={"category":"spot"}
        if cursor:params["cursor"]=cursor
        page=fetch(base_url+"/v5/market/instruments-info?"+urllib.parse.urlencode(params))
        if page.get("retCode")!=0:raise RuntimeError("Bybit instruments: "+str(page.get("retMsg")))
        result=page.get("result") or {}
        for p in result.get("list") or []:
            base=p.get("baseCoin","")
            if p.get("quoteCoin")=="USDT" and p.get("status")=="Trading" and valid(base):
                active[p["symbol"]]=base
        next_cursor=result.get("nextPageCursor") or ""
        if not next_cursor:break
        if next_cursor in seen or len(seen)>=100:raise RuntimeError("Bybit cursor loop/overflow")
        seen.add(next_cursor);cursor=next_cursor
    page=fetch(base_url+"/v5/market/tickers?category=spot")
    if page.get("retCode")!=0:raise RuntimeError("Bybit tickers: "+str(page.get("retMsg")))
    quotes={p["symbol"]:p for p in (page.get("result") or {}).get("list",[]) if "symbol" in p}
    rows=[];missing=[]
    for symbol,base in active.items():
        q=quotes.get(symbol,{})
        price=number(q.get("lastPrice"));vol=number(q.get("turnover24h"));change=number(q.get("price24hPcnt"))
        if price is None or price<=0 or vol is None or vol<0 or change is None:
            missing.append(symbol);continue
        rows.append(dict(venue="bybit",pair=symbol,base=base,price=price,volume_24h_usdt=vol,change_24h_pct=round(change*100,4)))
    return rows,dict(active_pairs=len(active),valid_pairs=len(rows),missing_or_invalid=missing)

def bybit_with_fallback():
    """Try the alternate official Bybit API host only on access denial.

    Never substitute third-party exchange listings for genuine Bybit coverage.
    """
    try:
        return bybit(BB)
    except RuntimeError as first:
        if "BYBIT_GEO_BLOCKED_EGRESS_COUNTRY" in str(first):
            raise
        if "HTTP Error 403" not in str(first) and "HTTP Error 451" not in str(first):
            raise
        fallback="https://api.bybit.com"
        if BB.rstrip("/")==fallback:
            raise
        try:
            rows,status=bybit(fallback)
            status["api_host_used"]=fallback
            status["primary_host_access_error"]=str(first)
            return rows,status
        except Exception as second:
            raise RuntimeError(
                "BYBIT_OFFICIAL_HOSTS_UNAVAILABLE primary="+str(first)+
                " fallback="+str(second)) from second


def bybit_from_authorized_region(binance_bases=()):
    """Use only fresh complete official snapshots from a trusted repo runner.

    On a missing or stale snapshot, attempt direct official endpoints and keep
    failure explicit; never claim a Binance or third-party proxy as Bybit.
    """
    if os.getenv("HUNTER_BYBIT_WORKER_MARKET_ENABLED")=="1":
        try:
            from research import hunter_bybit_worker
        except ModuleNotFoundError:
            import hunter_bybit_worker
        return hunter_bybit_worker.collect(binance_bases)
    path=pathlib.Path(os.getenv(
        "HUNTER_BYBIT_REGIONAL_SNAPSHOT",
        "research/results/hunter-bybit-regional-snapshot.json"))
    if path.exists():
        spec=importlib.util.spec_from_file_location(
            "hunter_bybit_regional",
            pathlib.Path(__file__).resolve().parent/"hunter_bybit_regional.py")
        regional=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(regional)
        try:
            return regional.load(path,dt.datetime.now(dt.timezone.utc))
        except (ValueError,TypeError,KeyError,OverflowError,OSError) as exc:
            # Invalid/stale regional evidence never becomes venue coverage.
            print("BYBIT_REGIONAL_SNAPSHOT_REJECTED",type(exc).__name__,str(exc))
    if os.getenv("HUNTER_BYBIT_DIRECT_DISABLED")=="1":
        raise RuntimeError("BYBIT_REGIONAL_COLLECTOR_NOT_CONFIGURED_OR_STALE__US_RUNNER_GEO_BLOCKED")
    return bybit_with_fallback()


def build(results,previous,at):
    grouped={}
    for rows in results.values():
        for row in rows:grouped.setdefault(row["base"],[]).append(row)
    coins={};leads=[]
    for base,rows in sorted(grouped.items()):
        # Preserve the Binance mark for existing shadow positions. A second
        # venue is discovery evidence, not permission to switch price sources.
        reference=next((r for r in rows if r["venue"]=="binance"),None) or max(rows,key=lambda r:r["volume_24h_usdt"])
        prev=(previous.get("coins") or {}).get(base,{})
        prior=prev.get("venue_prices",{})
        changes=[(r["price"]/prior[r["venue"]]-1)*100 for r in rows if prior.get(r["venue"],0)>0]
        since=max(changes,key=abs) if changes else None
        pct=reference["change_24h_pct"]
        stage="POST_MOVE" if pct>=20 else "EARLY_MOVE" if pct>=8 else "PRE_MOVE_WATCH" if since is not None and since>=2 and pct<8 else "BASELINE"
        coins[base]=dict(venues=sorted(r["venue"] for r in rows),pairs=rows,
                         reference_venue=reference["venue"],reference_price=reference["price"],
                         change_24h_pct=pct,change_since_previous_scan_pct=round(since,3) if since is not None else None,
                         stage=stage,venue_prices={r["venue"]:r["price"] for r in rows},
                         contract_identity_unverified=len(rows)>1)
        # Every listed asset enters forward-upside research, irrespective of its
        # prior return or price-only stage. A rally is neither an eligibility
        # bonus nor an automatic rejection. Deep research must evaluate upside
        # FROM THE CURRENT ENTRY PRICE and the credible catalyst window.
        leads.append(dict(base=base,stage=stage,reference_price=reference["price"],
                          change_24h_pct=pct,change_since_previous_scan_pct=coins[base]["change_since_previous_scan_pct"],
                          venues=coins[base]["venues"],research_only=True,
                          research_priority="FORWARD_UPSIDE_UNASSESSED",
                          prior_rally_auto_reject=False))
    # Stable alphabetical ordering prevents stage-biased top-N truncation.
    # Momentum is retained for separate alerting, never as a buy ranking.
    leads.sort(key=lambda r:r["base"])
    generation_id=dt.datetime.fromisoformat(at).strftime("%Y%m%dT%H%M%S%fZ")
    return dict(schema="hunter_cex_universe_v2",generation_id=generation_id,source_head_sha=os.getenv("HUNTER_SOURCE_HEAD_SHA"),as_of_utc=at,
                scope="Crypto-only active Binance and verified Bybit USDT spot pairs; stablecoins and Binance type=3 bStocks/tokenized securities excluded",
                limitations="Ticker dedup is provisional until contract IDs verified; venue 24h volumes overlap and must not be summed as unique demand.",
                unique_base_tickers=len(coins),venue_counts={k:len(v) for k,v in results.items()},
                coins=coins,research_leads=leads,capital_authority="NONE_RESEARCH_ONLY",
                research_mandate="MAXIMIZE_CREDIBLE_FUTURE_UPSIDE_FROM_CURRENT_ENTRY_PRICE_REGARDLESS_OF_PRIOR_RALLY",
                research_coverage_count=len(leads))

def persist_universe_history(report):
    try:
        history=json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    except (ValueError,OSError):
        history={}
    if history.get("schema") not in (None,"hunter_universe_history_v1"):
        raise RuntimeError("UNIVERSE_HISTORY_SCHEMA_MISMATCH")
    assets=history.setdefault("assets",{})
    at=report["as_of_utc"]; generation=report["generation_id"]
    for base,coin in (report.get("coins") or {}).items():
        price=coin.get("reference_price"); item=assets.get(base)
        if item is None:
            assets[base]={"first_seen_at_utc":at,"first_seen_price":price,"first_generation_id":generation,
                          "observations":1,"last_seen_at_utc":at,"last_seen_price":price,"last_generation_id":generation}
        else:
            item["observations"]=int(item.get("observations",0))+1
            item["last_seen_at_utc"]=at; item["last_seen_price"]=price; item["last_generation_id"]=generation
    history["schema"]="hunter_universe_history_v1"; history["updated_at_utc"]=at
    tmp=HISTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(history,ensure_ascii=False,indent=2)+"\n")
    check=json.loads(tmp.read_text())
    if not isinstance(check.get("assets"),dict): raise RuntimeError("UNIVERSE_HISTORY_INVALID")
    tmp.replace(HISTORY)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    baseline=OUT/"hunter-cex-universe-latest.json"
    try:previous=json.loads(baseline.read_text()) if baseline.exists() else {}
    except (ValueError,OSError):previous={}
    at=dt.datetime.now(dt.timezone.utc).isoformat()
    results={};statuses={};errors={}
    try:
        rows,status=binance();results["binance"]=rows;statuses["binance"]=status
    except Exception as exc:
        errors["binance"]=str(exc);statuses["binance"]=dict(error=str(exc))
    # The hosted runner cannot use Bybit's official API from its egress region.
    # A fresh complete official regional snapshot is the only admissible input;
    # missing evidence stays visible rather than becoming fake coverage.
    try:
        rows,status=bybit_from_authorized_region({r["base"] for r in results.get("binance",[])});results["bybit"]=rows;statuses["bybit"]=status
    except Exception as exc:
        errors["bybit"]=str(exc);statuses["bybit"]=dict(error=str(exc))
    report=build(results,previous,at)
    # Log-only binding: generation time is not an upstream request timestamp.
    try:
        from research.hunter_http_evidence import log_generation_binding
    except ModuleNotFoundError:
        from hunter_http_evidence import log_generation_binding
    log_generation_binding(report["generation_id"], statuses.get("bybit"))
    binance_complete=("binance" in results and
                      not statuses["binance"]["missing_or_invalid"])
    bybit_complete=("bybit" in results and
                    not statuses["bybit"]["missing_or_invalid"])
    bybit_signal_complete=bybit_complete and statuses["bybit"].get("signal_complete") is True
    report.update(required_venues=["binance","bybit"],venue_status=statuses,
                  errors=errors,complete=binance_complete and bybit_signal_complete,
                  binance_complete=binance_complete,
                  bybit_complete=bybit_complete,bybit_signal_complete=bybit_signal_complete,bybit_required=True,
                  coverage_status=("BINANCE_BYBIT_COMPLETE" if binance_complete and bybit_signal_complete
                                   else "BINANCE_BYBIT_SIGNALS_INCOMPLETE" if binance_complete and bybit_complete
                                   else "BINANCE_ONLY_BYBIT_UNAVAILABLE" if binance_complete
                                   else "BINANCE_INCOMPLETE"))
    (OUT/"hunter-cex-universe-run.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    # Only a fully valid Binance scan may replace the last usable baseline.
    if binance_complete:
        baseline.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        persist_universe_history(report)
    summary={k:v for k,v in report.items() if k!="coins"}
    (OUT/"hunter-cex-universe-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(dict(complete=report["complete"],unique_base_tickers=report["unique_base_tickers"],
                          venue_status=statuses,errors=errors,leads=report["research_leads"][:10])))
    return 0 if report["binance_complete"] else 2

if __name__=="__main__":raise SystemExit(main())
