"""Frozen original collector from 0541d6b (unchanged at 63de1967). Offline oracle only."""
import json, os, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone
MAX_REQUEST_TARGET_CHARS=7000

def alpaca_snapshot_quotes(symbols):
    """Batch delayed SIP 5m bars for all held symbols using maximum practical GET batches."""
    if not symbols: return {},[],0,0
    key=os.getenv("APCA_API_KEY_ID"); secret=os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret: return {},[{"reason":"MISSING_ALPACA_SECRETS"}],0,0
    headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"User-Agent":"stock-shadow-position-monitor/3.0"}
    prices={}; errors=[]; requests=0
    end=datetime.now(timezone.utc)-__import__("datetime").timedelta(minutes=20)
    # Keep enough calendar lookback to cover weekends/holidays while still issuing one multi-symbol logical batch.
    start=end-__import__("datetime").timedelta(days=3)
    batches=[]; batch=[]
    for sym in symbols:
        candidate=batch+[sym]
        probe={"symbols":",".join(s.replace("-",".") for s in candidate),"timeframe":"5Min",
               "start":start.isoformat().replace("+00:00","Z"),"end":end.isoformat().replace("+00:00","Z"),
               "limit":10000,"feed":"sip","adjustment":"all"}
        target="/v2/stocks/bars?"+urllib.parse.urlencode(probe)
        if batch and len(target)>MAX_REQUEST_TARGET_CHARS:
            batches.append(batch); batch=[sym]
        else:
            batch=candidate
    if batch: batches.append(batch)
    for batch_index,batch in enumerate(batches):
        params={"symbols":",".join(s.replace("-",".") for s in batch),"timeframe":"5Min",
                "start":start.isoformat().replace("+00:00","Z"),"end":end.isoformat().replace("+00:00","Z"),
                "limit":10000,"feed":"sip","adjustment":"all"}
        token=None
        try:
            latest={}
            while True:
                if token: params["page_token"]=token
                else: params.pop("page_token",None)
                req=urllib.request.Request("https://data.alpaca.markets/v2/stocks/bars?"+urllib.parse.urlencode(params),headers=headers)
                with urllib.request.urlopen(req,timeout=35) as r: body=json.load(r)
                requests+=1
                for raw,rows in (body.get("bars") or {}).items():
                    if rows:
                        row=max(rows,key=lambda x:str(x.get("t") or ""))
                        latest[raw.replace(".","-")]={"price":row.get("c"),"price_asof":row.get("t"),"source":"ALPACA_SIP_5M_DELAYED"}
                token=body.get("next_page_token")
                if not token: break
            prices.update(latest)
        except urllib.error.HTTPError as e:
            requests+=1; errors.append({"batch":batch_index,"reason":f"HTTP_{e.code}","detail":str(e.reason)})
        except Exception as e:
            requests+=1; errors.append({"batch":batch_index,"reason":type(e).__name__,"detail":str(e)[:120]})
    return prices,errors,requests,len(batches)

