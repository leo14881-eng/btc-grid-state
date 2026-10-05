#!/usr/bin/env python3
"""Official Bybit execution-channel enrichment for Hunter shadow research.
Spot uses the public V5 endpoint. Alpha uses the official authenticated Alpha token-list endpoint when credentials exist.
Failure is UNKNOWN, never NOT_LISTED.
"""
import datetime as dt, hashlib, hmac, json, os, pathlib, re, time, urllib.error, urllib.parse, urllib.request
ROOT=pathlib.Path("research/results")
OUT=ROOT/"hunter-bybit-availability.json"
REVIEW=ROOT/"hunter-tactical-capital-review.json"
SPOT_CACHE=ROOT/"hunter-bybit-spot-cache.json"
HOST=os.getenv("HUNTER_BYBIT_API","https://api.bybit.com")
SPOT_PROXY=os.getenv("HUNTER_BYBIT_SPOT_PROXY","https://bybit-api-test.qinx468.workers.dev").rstrip("/")
KEY=os.getenv("BYBIT_ALPHA_API_KEY","")
SECRET=os.getenv("BYBIT_ALPHA_API_SECRET","")
PROXY_TOKEN=os.getenv("HUNTER_PROXY_TOKEN","")
ALPHA_PROXY=os.getenv("HUNTER_BYBIT_ALPHA_PROXY",SPOT_PROXY+"/bybit/alpha/token-list")
RECV="5000"

def request(url, data=None, headers=None):
    # Use a normal browser-like UA for public edge endpoints. Some CDN/WAF layers
    # reject urllib/bot-like clients even though the endpoint itself is public.
    base={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/153.0 Safari/537.36","Accept":"application/json,text/plain,*/*"}
    req=urllib.request.Request(url,data=data,headers={**base,**(headers or {})})
    try:
        with urllib.request.urlopen(req,timeout=15) as r:return json.load(r)
    except urllib.error.HTTPError as e:
        body=e.read(500).decode("utf-8","ignore").replace("\n"," ")
        raise RuntimeError(f"HTTP_{e.code} url={url} body={body[:240]}") from e

def spot():
    rows=[]; cursor=""
    while True:
        q={"category":"spot","limit":"1000"}
        if cursor:q["cursor"]=cursor
        d=request(HOST+"/v5/market/instruments-info?"+urllib.parse.urlencode(q))
        if d.get("retCode")!=0:raise RuntimeError("BYBIT_SPOT_RET_"+str(d.get("retCode")))
        result=d.get("result") or {}; rows.extend(result.get("list") or [])
        cursor=result.get("nextPageCursor") or ""
        if not cursor:break
    return sorted({str(x.get("baseCoin","")).upper() for x in rows if x.get("quoteCoin")=="USDT" and x.get("status")=="Trading" and x.get("baseCoin")})

def classify_error(exc):
    s=str(exc)
    if "HTTP_403" in s: return "HTTP_403"
    if "HTTP_429" in s: return "RATE_LIMIT"
    if "CREDENTIALS_NOT_CONFIGURED" in s: return "CREDENTIALS_NOT_CONFIGURED"
    if "BYBIT_PROXY_RET_10001" in s or "symbol invalid" in s.lower(): return "INVALID_SYMBOL"
    return "UPSTREAM_ERROR"

def exchange_asset(value):
    """Normalize only exchange-safe base tickers; human labels never become API symbols."""
    a=str(value or "").strip().upper()
    return a if re.fullmatch(r"[A-Z0-9]{1,30}",a) else None

CACHE_SCHEMA="hunter_bybit_spot_full_cache_v2"
CACHE_TTL_SECONDS=24*60*60

def validate_spot_rows(rows):
    """Reject malformed or candidate-scoped responses before replacing full cache."""
    if not isinstance(rows,list) or not rows:
        raise RuntimeError("BYBIT_FULL_LIST_EMPTY_OR_INVALID")
    seen=set()
    for row in rows:
        if not isinstance(row,dict):raise RuntimeError("BYBIT_FULL_LIST_INVALID_ROW")
        symbol=row.get("symbol"); base=exchange_asset(row.get("baseCoin")); quote=exchange_asset(row.get("quoteCoin"))
        if not base or not quote or symbol!=base+quote or not row.get("status") or symbol in seen:
            raise RuntimeError("BYBIT_FULL_LIST_INVALID_ROW")
        seen.add(symbol)
    if not {"BTCUSDT","ETHUSDT"}.issubset(seen):
        raise RuntimeError("BYBIT_FULL_LIST_INCOMPLETE_OR_SYMBOL_SCOPED")
    return rows

def spot_proxy_universe(now=None):
    """One full-list request per successful 24h cache period; never per-symbol calls."""
    now=now or dt.datetime.now(dt.timezone.utc)
    endpoint=SPOT_PROXY+"/bybit/spot"
    try: cache=json.loads(SPOT_CACHE.read_text())
    except Exception: cache={}
    try:
        stamp=dt.datetime.fromisoformat(cache["captured_at_utc"])
        age=(now-stamp).total_seconds()
        fresh=(cache.get("schema")==CACHE_SCHEMA and cache.get("endpoint")==endpoint
               and cache.get("complete") is True and 0<=age<CACHE_TTL_SECONDS)
        if fresh:validate_spot_rows(cache["instruments"])
    except Exception: fresh=False
    network_requests=0
    if not fresh:
        network_requests=1
        data=request(endpoint)
        if not isinstance(data,dict) or data.get("retCode")!=0:
            raise RuntimeError("BYBIT_PROXY_RET_"+str(data.get("retCode") if isinstance(data,dict) else "INVALID_RESPONSE"))
        result=data.get("result") or {}
        if result.get("category")!="spot" or result.get("nextPageCursor"):
            raise RuntimeError("BYBIT_FULL_LIST_WRONG_CATEGORY_OR_PARTIAL")
        rows=validate_spot_rows(result.get("list"))
        cache={"schema":CACHE_SCHEMA,"captured_at_utc":now.isoformat(),
               "expires_at_utc":(now+dt.timedelta(seconds=CACHE_TTL_SECONDS)).isoformat(),
               "complete":True,"endpoint":endpoint,"source":"BYBIT_OFFICIAL_V5_VIA_WORKER",
               "instruments":rows}
        SPOT_CACHE.parent.mkdir(parents=True,exist_ok=True)
        temp=SPOT_CACHE.with_suffix(".tmp")
        temp.write_text(json.dumps(cache,ensure_ascii=False,indent=2)+"\n")
        temp.replace(SPOT_CACHE)
        age=0
    symbols=sorted({row["baseCoin"] for row in cache["instruments"]
                    if row["quoteCoin"]=="USDT" and row["status"]=="Trading"
                    and row.get("symbolType")!="xstocks"})
    return symbols,{"network_requests":network_requests,"cache_hit":fresh,
                    "cache_count":len(cache["instruments"]),"cache_age_seconds":round(age,3),
                    "captured_at_utc":cache["captured_at_utc"],"expires_at_utc":cache["expires_at_utc"],
                    "complete":True,"ttl_seconds":CACHE_TTL_SECONDS}

def spot_proxy_candidates():
    """Explicitly candidate-scoped fallback while Worker requires symbol.

    Never label this result as full market coverage. Legacy per-symbol cache is
    still useful for V2's execution-channel labels.
    """
    try: review=json.loads(REVIEW.read_text())
    except Exception: review={}
    try: cache=json.loads(SPOT_CACHE.read_text())
    except Exception: cache={}
    if cache.get("schema")==CACHE_SCHEMA: cache={}
    now=dt.datetime.now(dt.timezone.utc);requested=set();failures={}
    for raw in review.get("capital_review_eligible") or []:
        asset=exchange_asset(raw)
        if asset:requested.add(asset)
        elif raw:failures[str(raw)]={"reason":"INVALID_SYMBOL","detail":"REJECTED_BEFORE_TRANSPORT"}
    requested.update({"BTC","ETH"});ok=[];requests=0
    for asset in sorted(requested):
        saved=cache.get(asset) or {}
        try: age=(now-dt.datetime.fromisoformat(saved["as_of_utc"])).total_seconds()
        except Exception: age=float("inf")
        if 0<=age<CACHE_TTL_SECONDS:
            if saved.get("trading"):ok.append(asset)
            else:failures[asset]={"reason":"INVALID_SYMBOL","detail":"CACHED_NOT_TRADING_OR_NOT_LISTED"}
            continue
        if requests>=10:
            failures[asset]={"reason":"DEFERRED","detail":"BYBIT_NEW_SYMBOL_REQUEST_BUDGET"}
            continue
        symbol=asset+"USDT";requests+=1
        try:
            data=request(SPOT_PROXY+"/bybit/spot?"+urllib.parse.urlencode({"symbol":symbol}))
            if data.get("retCode")!=0:raise RuntimeError("BYBIT_PROXY_RET_"+str(data.get("retCode")))
            rows=(data.get("result") or {}).get("list") or []
            trading=any(row.get("symbol")==symbol and row.get("baseCoin")==asset
                        and row.get("quoteCoin")=="USDT" and row.get("status")=="Trading" for row in rows)
            cache[asset]={"as_of_utc":now.isoformat(),"trading":trading}
            if trading:ok.append(asset)
            else:failures[asset]={"reason":"INVALID_SYMBOL","detail":"NOT_TRADING_OR_NOT_LISTED"}
        except Exception as exc:
            failures[asset]={"reason":classify_error(exc),"detail":type(exc).__name__+":"+str(exc)[:120]}
    if cache:
        SPOT_CACHE.parent.mkdir(parents=True,exist_ok=True)
        SPOT_CACHE.write_text(json.dumps(cache,ensure_ascii=False,indent=2)+"\n")
    return sorted(ok),failures,requests,len(cache)

def spot_official_pages():
    """Candidate-scoped fallback when V5 is region-blocked. Uses only official Bybit spot pages."""
    try: review=json.loads(REVIEW.read_text())
    except Exception: review={}
    assets={str(x.get("asset","")).upper() for x in review.get("candidates") or [] if x.get("asset")}
    assets.add("ALPHA")  # regression probe requested for channel validation
    ok=[]
    for a in sorted(assets):
        url="https://www.bybit.com/en/trade/spot/"+urllib.parse.quote(a)+"/USDT"
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 hunter-bybit-enrichment","Accept":"text/html"})
            with urllib.request.urlopen(req,timeout=12) as r:
                body=r.read(600000).decode("utf-8","ignore").upper()
            if (a+"/USDT") in body:ok.append(a)
        except Exception:continue
    if not ok:raise RuntimeError("OFFICIAL_SPOT_PAGE_FALLBACK_EMPTY")
    return ok

def alpha():
    if not KEY or not SECRET:return None,"CREDENTIALS_NOT_CONFIGURED"
    ts=str(int(time.time()*1000)); body=json.dumps({"tokenTag":0},separators=(",",":"))
    sign=hmac.new(SECRET.encode(),(ts+KEY+RECV+body).encode(),hashlib.sha256).hexdigest()
    if not PROXY_TOKEN:
        return None,"PROXY_TOKEN_NOT_CONFIGURED"
    d=request(ALPHA_PROXY,body.encode(),{"Content-Type":"application/json","X-Hunter-Proxy-Token":PROXY_TOKEN,"X-BAPI-API-KEY":KEY,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":RECV,"X-BAPI-SIGN":sign})
    if d.get("retCode")!=0:raise RuntimeError("BYBIT_ALPHA_RET_"+str(d.get("retCode")))
    rows=d.get("result") or []
    return sorted({str(x.get("symbol","")).upper() for x in rows if x.get("symbol") and int(x.get("riskFlag") or 0)==0}),None

def main():
    now=dt.datetime.now(dt.timezone.utc).isoformat()
    out={"schema":"hunter_bybit_availability_v1","as_of_utc":now,"source":"BYBIT_OFFICIAL_V5","spot":{"status":"UNKNOWN","symbols":[],"error":None},"alpha":{"status":"UNKNOWN","symbols":[],"error":None}}
    # GitHub Hosted Runner US egress is known to receive CloudFront HTTP 403 from
    # api.bybit.com. Avoid a guaranteed failing direct request: Worker is primary
    # transport, with official Bybit V5 as its upstream.
    try:
        xs,cache_meta=spot_proxy_universe()
        out["spot"].update({"status":"OK_FULL_UNIVERSE_BYBIT_VIA_CLOUDFLARE","business_status":"TRADING",
                            "symbols":xs,"count":len(xs),"symbol_failures":{},"scope":"FULL_USDT_SPOT_UNIVERSE",
                            "transport":"CLOUDFLARE_WORKER_PRIMARY_FOR_GITHUB","upstream":"BYBIT_OFFICIAL_V5_INSTRUMENTS_INFO",
                            "endpoint_template":"/bybit/spot","proxy":SPOT_PROXY,"error_reason":None,**cache_meta})
    except Exception as e:
        proxy_error=type(e).__name__+":"+str(e)[:160]
        out["spot"].update({"status":"UNKNOWN","business_status":"UNKNOWN","error":proxy_error,"error_reason":classify_error(e),"transport":"CLOUDFLARE_WORKER_PRIMARY_FOR_GITHUB","upstream":"BYBIT_OFFICIAL_V5_INSTRUMENTS_INFO"})
        out["spot"].update({"scope":"FULL_USDT_SPOT_UNIVERSE","complete":False,"network_requests":1})
        # Restore the previously working candidate check without promoting it to
        # an all-market source. A partial failure remains UNKNOWN.
        try:
            xs,failures,requests,cache_count=spot_proxy_candidates()
            out["spot"].update({"status":"OK_CANDIDATE_SCOPED_BYBIT_VIA_CLOUDFLARE",
                                "business_status":"TRADING","symbols":xs,"count":len(xs),
                                "symbol_failures":failures,"cache_count":cache_count,
                                "network_requests":requests+1,"scope":"CACHE_FIRST_CANDIDATE_MATCH",
                                "input_rejections":{k:v for k,v in failures.items() if v.get("detail")=="REJECTED_BEFORE_TRANSPORT"},
                                "bulk_error":proxy_error,"error":None,"error_reason":None})
        except Exception as fallback_error:
            out["spot"]["fallback_error"]=type(fallback_error).__name__+":"+str(fallback_error)[:160]
    try:
        xs,why=alpha()
        if xs is None:out["alpha"]["error"]=why
        else:out["alpha"].update({"status":"OK","symbols":xs,"count":len(xs)})
    except Exception as e:out["alpha"]["error"]=type(e).__name__+":"+str(e)[:160]
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"spot_status":out["spot"]["status"],"spot_count":out["spot"].get("count"),"alpha_status":out["alpha"]["status"],"alpha_count":out["alpha"].get("count"),"alpha_error":out["alpha"].get("error")}))
if __name__=="__main__":main()
