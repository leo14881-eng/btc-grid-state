#!/usr/bin/env python3
"""Official Bybit execution-channel enrichment for Hunter shadow research.
Spot uses the public V5 endpoint. Alpha uses the official authenticated Alpha token-list endpoint when credentials exist.
Failure is UNKNOWN, never NOT_LISTED.
"""
import datetime as dt, hashlib, hmac, json, math, os, pathlib, re, time, urllib.error, urllib.parse, urllib.request
try:
    from research.hunter_http_evidence import ObservedHTTPError, emit_public_log, request_json, validate_evidence
except ModuleNotFoundError:
    from hunter_http_evidence import ObservedHTTPError, emit_public_log, request_json, validate_evidence
ROOT=pathlib.Path("research/results")
OUT=ROOT/"hunter-bybit-availability.json"
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
    if data is None and url == SPOT_PROXY + "/bybit/spot":
        try:
            return request_json(req, timeout=15)
        except ObservedHTTPError as exc:
            evidence = validate_evidence(exc.http_evidence)
            emit_public_log("HUNTER_BYBIT_HTTP_EVIDENCE " + json.dumps(dict(
                schema="hunter_http_log_v1", route="/bybit/spot",
                observed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                http_evidence=evidence), sort_keys=True, separators=(",", ":")))
            if evidence["failure_kind"] == "http":
                # Preserve old public error classification without URL/raw body.
                reported = evidence.get("worker_error_code", "")
                raise RuntimeError(("HTTP_" + str(exc.code) + " " + reported).strip()) from None
            if evidence["failure_kind"] == "invalid_json":
                raise ValueError("BYBIT_WORKER_NON_JSON") from None
            if evidence["failure_kind"] == "timeout":
                raise TimeoutError("WORKER_TIMEOUT") from None
            raise urllib.error.URLError("WORKER_TRANSPORT_ERROR") from None
    try:
        with urllib.request.urlopen(req,timeout=15) as r:return json.load(r)
    except urllib.error.HTTPError as e:
        body=e.read(500).decode("utf-8","ignore").replace("\n"," ")
        raise RuntimeError(f"HTTP_{e.code} url={url} body={body[:240]}") from e

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

def spot_proxy_universe(now=None, force_refresh=False):
    """Cache the full list; permit one explicit refresh on a ticker contradiction."""
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
    if force_refresh:
        fresh=False
    if not fresh:
        network_requests=1
        data=request(endpoint)
        if not isinstance(data,dict) or data.get("retCode")!=0:
            raise RuntimeError("BYBIT_PROXY_RET_"+str(data.get("retCode") if isinstance(data,dict) else "INVALID_RESPONSE"))
        result=data.get("result") or {}
        if result.get("category")!="spot" or result.get("nextPageCursor"):
            raise RuntimeError("BYBIT_FULL_LIST_WRONG_CATEGORY_OR_PARTIAL")
        if force_refresh:
            try:
                source_time=float(data['time'])/1000
                if not math.isfinite(source_time) or abs(now.timestamp()-source_time)>120:
                    raise ValueError('stale')
            except (KeyError,TypeError,ValueError,OverflowError):
                raise RuntimeError("BYBIT_FULL_LIST_REFRESH_TIME_INVALID") from None
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
    try:
        xs,why=alpha()
        if xs is None:out["alpha"]["error"]=why
        else:out["alpha"].update({"status":"OK","symbols":xs,"count":len(xs)})
    except Exception as e:out["alpha"]["error"]=type(e).__name__+":"+str(e)[:160]
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"spot_status":out["spot"]["status"],"spot_count":out["spot"].get("count"),"alpha_status":out["alpha"]["status"],"alpha_count":out["alpha"].get("count"),"alpha_error":out["alpha"].get("error")}))
if __name__=="__main__":main()
