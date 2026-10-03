#!/usr/bin/env python3
"""Official Bybit execution-channel enrichment for Hunter shadow research.
Spot uses the public V5 endpoint. Alpha uses the official authenticated Alpha token-list endpoint when credentials exist.
Failure is UNKNOWN, never NOT_LISTED.
"""
import datetime as dt, hashlib, hmac, json, os, pathlib, time, urllib.parse, urllib.request
ROOT=pathlib.Path("research/results")
OUT=ROOT/"hunter-bybit-availability.json"
HOST=os.getenv("HUNTER_BYBIT_API","https://api.bybit.com")
KEY=os.getenv("BYBIT_ALPHA_API_KEY","")
SECRET=os.getenv("BYBIT_ALPHA_API_SECRET","")
RECV="5000"

def request(url, data=None, headers=None):
    req=urllib.request.Request(url,data=data,headers={"User-Agent":"hunter-bybit-enrichment/1.0","Accept":"application/json",**(headers or {})})
    with urllib.request.urlopen(req,timeout=15) as r:return json.load(r)

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
    except Exception as e:out["spot"]["error"]=type(e).__name__+":"+str(e)[:160]
    try:
        xs,why=alpha()
        if xs is None:out["alpha"]["error"]=why
        else:out["alpha"].update({"status":"OK","symbols":xs,"count":len(xs)})
    except Exception as e:out["alpha"]["error"]=type(e).__name__+":"+str(e)[:160]
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"spot_status":out["spot"]["status"],"spot_count":out["spot"].get("count"),"alpha_status":out["alpha"]["status"],"alpha_count":out["alpha"].get("count"),"alpha_error":out["alpha"].get("error")}))
if __name__=="__main__":main()
