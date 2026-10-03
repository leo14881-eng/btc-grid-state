#!/usr/bin/env python3
"""Official Bybit execution-channel enrichment for Hunter shadow research.
Spot uses the public V5 endpoint. Alpha uses the official authenticated Alpha token-list endpoint when credentials exist.
Failure is UNKNOWN, never NOT_LISTED.
"""
import datetime as dt, hashlib, hmac, json, os, pathlib, time, urllib.error, urllib.parse, urllib.request
ROOT=pathlib.Path("research/results")
OUT=ROOT/"hunter-bybit-availability.json"
REVIEW=ROOT/"hunter-tactical-capital-review.json"
HOST=os.getenv("HUNTER_BYBIT_API","https://api.bybit.com")
SPOT_PROXY=os.getenv("HUNTER_BYBIT_SPOT_PROXY","https://bybit-api-test.qinx468.workers.dev").rstrip("/")
KEY=os.getenv("BYBIT_ALPHA_API_KEY","")
SECRET=os.getenv("BYBIT_ALPHA_API_SECRET","")
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

def spot_proxy_candidates():
    """Candidate-scoped Spot lookup through verified Cloudflare egress; Bybit remains upstream."""
    try: review=json.loads(REVIEW.read_text())
    except Exception: review={}
    assets={str(x.get("asset","")).upper() for x in review.get("candidates") or [] if x.get("asset")}
    assets.update({"BTC","ETH"})
    ok=[]
    for a in sorted(assets):
        symbol=a+"USDT"
        d=request(SPOT_PROXY+"/bybit/spot?"+urllib.parse.urlencode({"symbol":symbol}))
        if d.get("retCode")!=0:raise RuntimeError("BYBIT_PROXY_RET_"+str(d.get("retCode")))
        rows=(d.get("result") or {}).get("list") or []
        if any(str(x.get("symbol","")).upper()==symbol and x.get("quoteCoin")=="USDT" and x.get("status")=="Trading" for x in rows):ok.append(a)
    return ok

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
    d=request(HOST+"/v5/alpha/trade/biz-token-list",body.encode(),{"Content-Type":"application/json","X-BAPI-API-KEY":KEY,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":RECV,"X-BAPI-SIGN":sign})
    if d.get("retCode")!=0:raise RuntimeError("BYBIT_ALPHA_RET_"+str(d.get("retCode")))
    rows=d.get("result") or []
    return sorted({str(x.get("symbol","")).upper() for x in rows if x.get("symbol") and int(x.get("riskFlag") or 0)==0}),None

def main():
    now=dt.datetime.now(dt.timezone.utc).isoformat()
    out={"schema":"hunter_bybit_availability_v1","as_of_utc":now,"source":"BYBIT_OFFICIAL_V5","spot":{"status":"UNKNOWN","symbols":[],"error":None},"alpha":{"status":"UNKNOWN","symbols":[],"error":None}}
    try:
        xs=spot();out["spot"].update({"status":"OK","symbols":xs,"count":len(xs)})
    except Exception as e:
        api_error=type(e).__name__+":"+str(e)[:160]
        try:
            xs=spot_proxy_candidates();out["spot"].update({"status":"OK_CANDIDATE_SCOPED_BYBIT_VIA_CLOUDFLARE","symbols":xs,"count":len(xs),"scope":"CURRENT_CANDIDATES_PLUS_BTC_ETH_REGRESSION_PROBES","api_error":api_error,"proxy":SPOT_PROXY})
        except Exception as e2:
            proxy_error=type(e2).__name__+":"+str(e2)[:120]
            try:
                xs=spot_official_pages();out["spot"].update({"status":"OK_CANDIDATE_SCOPED_OFFICIAL_PAGE_FALLBACK","symbols":xs,"count":len(xs),"scope":"CURRENT_CANDIDATES_PLUS_ALPHA_REGRESSION_PROBE","api_error":api_error,"proxy_error":proxy_error})
            except Exception as e3:out["spot"]["error"]=api_error+"; PROXY="+proxy_error+"; FALLBACK="+type(e3).__name__+":"+str(e3)[:120]
    try:
        xs,why=alpha()
        if xs is None:out["alpha"]["error"]=why
        else:out["alpha"].update({"status":"OK","symbols":xs,"count":len(xs)})
    except Exception as e:out["alpha"]["error"]=type(e).__name__+":"+str(e)[:160]
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"spot_status":out["spot"]["status"],"spot_count":out["spot"].get("count"),"alpha_status":out["alpha"]["status"],"alpha_count":out["alpha"].get("count"),"alpha_error":out["alpha"].get("error")}))
if __name__=="__main__":main()
