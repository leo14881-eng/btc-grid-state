#!/usr/bin/env python3
"""Full Binance+Bybit active USDT spot universe; research only, never trades."""
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
BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
BB=os.getenv("HUNTER_BYBIT_API","https://api.bytick.com")
EXCLUDE={"USDC","USDT","BUSD","FDUSD","TUSD","USDP","DAI","USDE","PYUSD","EUR","TRY","BRL","GBP","AUD","UST","USTC"}
LEVERAGED=("UP","DOWN","BULL","BEAR")

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

def number(x):
    try:
        v=float(x)
        return v if abs(v)<float("inf") else None
    except (TypeError,ValueError,OverflowError):return None

def binance():
    meta=fetch(BN+"/api/v3/exchangeInfo")
    active={p["symbol"]:p["baseAsset"] for p in meta["symbols"]
            if p.get("quoteAsset")=="USDT" and p.get("status")=="TRADING"
            and p.get("isSpotTradingAllowed",True) and valid(p.get("baseAsset",""))}
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
    return rows,dict(active_pairs=len(active),valid_pairs=len(rows),missing_or_invalid=missing)

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


def bybit_from_authorized_region():
    """Use only fresh complete official snapshots from a trusted repo runner.

    On a missing or stale snapshot, attempt direct official endpoints and keep
    failure explicit; never claim a Binance or third-party proxy as Bybit.
    """
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
        reference=max(rows,key=lambda r:r["volume_24h_usdt"])
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
    return dict(schema="hunter_cex_universe_v1",as_of_utc=at,
                scope="ALL active Binance and Bybit USDT spot pairs, excluding stablecoin bases; leveraged-like tickers retained for separate risk classification",
                limitations="Ticker dedup is provisional until contract IDs verified; venue 24h volumes overlap and must not be summed as unique demand.",
                unique_base_tickers=len(coins),venue_counts={k:len(v) for k,v in results.items()},
                coins=coins,research_leads=leads,capital_authority="NONE_RESEARCH_ONLY",
                research_mandate="MAXIMIZE_CREDIBLE_FUTURE_UPSIDE_FROM_CURRENT_ENTRY_PRICE_REGARDLESS_OF_PRIOR_RALLY",
                research_coverage_count=len(leads))

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    baseline=OUT/"hunter-cex-universe-latest.json"
    try:previous=json.loads(baseline.read_text()) if baseline.exists() else {}
    except (ValueError,OSError):previous={}
    at=dt.datetime.now(dt.timezone.utc).isoformat()
    results={};statuses={};errors={}
    for name,fn in (("binance",binance),("bybit",bybit_from_authorized_region)):
        try:
            rows,status=fn();results[name]=rows;statuses[name]=status
        except Exception as exc:
            errors[name]=str(exc);statuses[name]=dict(error=str(exc))
    report=build(results,previous,at)
    binance_complete="binance" in results and not statuses["binance"]["missing_or_invalid"]
    union_complete=len(results)==2 and all(not x["missing_or_invalid"] for x in statuses.values())
    report.update(venue_status=statuses,errors=errors,complete=union_complete,
                  binance_complete=binance_complete,bybit_complete="bybit" in results and not statuses["bybit"]["missing_or_invalid"] if "bybit" in results else False,
                  coverage_status="BOTH_EXCHANGES_COMPLETE" if union_complete else "BINANCE_COMPLETE_BYBIT_UNAVAILABLE" if binance_complete else "INCOMPLETE")
    (OUT/"hunter-cex-universe-run.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    # Preserve a usable Binance baseline even when Bybit's geo-restricted API is unavailable.
    if binance_complete:baseline.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    summary={k:v for k,v in report.items() if k!="coins"}
    (OUT/"hunter-cex-universe-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(dict(complete=report["complete"],unique_base_tickers=report["unique_base_tickers"],
                          venue_status=statuses,errors=errors,leads=report["research_leads"][:10]),ensure_ascii=False))
    return 0 if report["binance_complete"] else 2

if __name__=="__main__":raise SystemExit(main())
