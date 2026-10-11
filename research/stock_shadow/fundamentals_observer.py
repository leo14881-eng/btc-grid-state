#!/usr/bin/env python3
"""Stock Shadow fundamental observer. Observation-only; never changes BUY/ADD/SELL."""
# FINAL_ACCEPTANCE_20261004
import json, urllib.request, urllib.error, urllib.parse, io, zipfile, tempfile, shutil, time, math, os, csv, threading, re, zlib, sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; OUT=ROOT/"fundamentals-observer-v1.json"
UA="stock-shadow-research/1.0"
SEC_HOSTS=frozenset(("www.sec.gov", "data.sec.gov", "efts.sec.gov"))
SEC_MAX_WIRE_BYTES=8*1024*1024
SEC_MAX_DECODED_BYTES=32*1024*1024
SEC_REQUEST_INTERVAL=0.2
SEC_DENIAL_BACKOFF_SECONDS=3600

class SECTransportError(ValueError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)

def sec_user_agent():
    value=os.getenv("SEC_USER_AGENT", "").strip()
    if not value:
        raise SECTransportError("SEC_CONTACT_NOT_CONFIGURED")
    if (len(value)>256 or any(ord(c)<32 or ord(c)>126 for c in value)
            or not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)):
        raise SECTransportError("SEC_CONTACT_INVALID")
    return value

def _sec_host(url):
    parsed=urllib.parse.urlsplit(url)
    if (parsed.scheme!="https" or parsed.hostname not in SEC_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None,443)):
        raise SECTransportError("SEC_URL_NOT_ALLOWED")
    return parsed.hostname

@contextmanager
def _sec_budget():
    """One local-filesystem budget across stock processes, checkouts and restarts."""
    value=os.getenv("SEC_TRANSPORT_STATE","")
    if not value:
        raise SECTransportError("SEC_SHARED_BUDGET_NOT_CONFIGURED")
    path=Path(value)
    if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
        raise SECTransportError("SEC_SHARED_BUDGET_PATH_INVALID")
    db=None
    try:
        db=sqlite3.connect(str(path),timeout=5)
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS budget (key TEXT PRIMARY KEY, value REAL NOT NULL)")
        yield db
        db.commit()
    except urllib.error.HTTPError:
        raise  # HTTPError is also an OSError; retain the cached-denial classification.
    except (sqlite3.Error,OSError):
        raise SECTransportError("SEC_SHARED_BUDGET_UNAVAILABLE") from None
    finally:
        if db is not None: db.close()

def _sec_start(url):
    host=_sec_host(url)
    with _sec_budget() as db:
        current=time.time()
        denied=db.execute("SELECT value FROM budget WHERE key=?",("denied:"+host,)).fetchone()
        if denied and (not math.isfinite(denied[0]) or denied[0]<0):
            raise SECTransportError("SEC_SHARED_BUDGET_INVALID")
        if denied and denied[0]>current:
            error=urllib.error.HTTPError(url,403,"SEC_ACCESS_DENIED_CACHED",{},None)
            error.sec_cached_denial=True
            raise error
        row=db.execute("SELECT value FROM budget WHERE key='next_start'").fetchone()
        next_start=row[0] if row else current
        if not math.isfinite(next_start) or next_start>current+60:
            raise SECTransportError("SEC_SHARED_BUDGET_CLOCK_INVALID")
        wait=max(0.0,next_start-current)
        if wait: time.sleep(wait)
        db.execute("INSERT OR REPLACE INTO budget VALUES ('next_start',?)",
                   (max(time.time(),next_start)+SEC_REQUEST_INTERVAL,))

def _sec_deny(url):
    with _sec_budget() as db:
        db.execute("INSERT OR REPLACE INTO budget VALUES (?,?)",
                   ("denied:"+_sec_host(url),time.time()+SEC_DENIAL_BACKOFF_SECONDS))

class _SECRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _sec_host(newurl)  # Never forward the operator's contact to another host.
        _sec_start(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def _sec_urlopen(req, timeout):
    return urllib.request.build_opener(_SECRedirectHandler()).open(req,timeout=timeout)

def _sec_open(url, accept, timeout=25, encoding="gzip, deflate"):
    _sec_host(url)
    req=urllib.request.Request(url,headers={
        "User-Agent":sec_user_agent(),"Accept":accept,"Accept-Encoding":encoding})
    _sec_start(url)
    try:
        return _sec_urlopen(req,timeout)
    except urllib.error.HTTPError as error:
        if error.code==403:
            _sec_deny(error.url or url)
        raise

def _sec_decode(payload, encoding):
    encoding=encoding.strip().lower()
    if encoding in ("","identity"):
        decoded=payload
    elif encoding in ("gzip","deflate"):
        def inflate(window):
            stream=zlib.decompressobj(window)
            data=stream.decompress(payload,SEC_MAX_DECODED_BYTES+1)
            if len(data)>SEC_MAX_DECODED_BYTES or stream.unconsumed_tail:
                raise SECTransportError("SEC_DECODED_SIZE_LIMIT")
            if not stream.eof or stream.unused_data:
                raise SECTransportError("SEC_COMPRESSED_STREAM_INVALID")
            return data
        try:
            if encoding=="gzip":
                decoded=inflate(16+zlib.MAX_WBITS)
            else:
                try: decoded=inflate(zlib.MAX_WBITS)
                except zlib.error: decoded=inflate(-zlib.MAX_WBITS)
        except zlib.error:
            raise SECTransportError("SEC_COMPRESSED_STREAM_INVALID") from None
    else:
        raise SECTransportError("SEC_CONTENT_ENCODING_UNSUPPORTED")
    if len(decoded)>SEC_MAX_DECODED_BYTES:
        raise SECTransportError("SEC_DECODED_SIZE_LIMIT")
    return decoded

def _bounded_response(response):
    if response.status!=200:
        raise SECTransportError("SEC_HTTP_STATUS_UNEXPECTED")
    size=response.headers.get("Content-Length")
    if size is not None and (not size.isdigit() or int(size)>SEC_MAX_WIRE_BYTES):
        raise SECTransportError("SEC_WIRE_SIZE_LIMIT")
    chunks=[]; total=0
    while True:
        chunk=response.read(min(65536,SEC_MAX_WIRE_BYTES-total+1))
        if not chunk: break
        total+=len(chunk)
        if total>SEC_MAX_WIRE_BYTES: raise SECTransportError("SEC_WIRE_SIZE_LIMIT")
        chunks.append(chunk)
    if size is not None and total!=int(size):
        raise SECTransportError("SEC_WIRE_LENGTH_MISMATCH")
    media=response.headers.get("Content-Type","").split(";",1)[0].strip().lower()
    return _sec_decode(b"".join(chunks),response.headers.get("Content-Encoding","")),media

def _sec_response(url, accept, timeout=25):
    with _sec_open(url,accept,timeout) as response:
        return _bounded_response(response)

BULK_COMPANYFACTS="https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
BULK_SUBMISSIONS="https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
FALLBACK_MAX_REQUESTS=40
FRAME_REQUEST_BUDGET=12
MARKET_BATCH_SCHEMA_VERSION=7
PROXY_START_INTERVAL_SECONDS=2.0

def now(): return datetime.now(timezone.utc).isoformat()
def load(p,d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except Exception: return d
def get(url):
    raw,media=_sec_response(url,"application/json")
    if media not in ("application/json","text/json","text/plain","application/octet-stream"):
        raise SECTransportError("SEC_JSON_CONTENT_TYPE_INVALID")
    try:
        text=raw.decode("utf-8-sig")
        if text.lstrip().startswith("<"):
            raise SECTransportError("SEC_JSON_HTML_RESPONSE")
        data=json.loads(text)
    except (UnicodeDecodeError,json.JSONDecodeError):
        raise SECTransportError("SEC_JSON_INVALID") from None
    if not isinstance(data,dict):
        raise SECTransportError("SEC_JSON_SCHEMA_INVALID")
    return _validate_sec_json(data,url)

def _validate_sec_json(data,url):
    if not isinstance(data,dict) or any(key in data for key in ("error","errors","message")):
        raise SECTransportError("SEC_JSON_SCHEMA_INVALID")
    path=urllib.parse.urlsplit(url).path
    valid=True
    if "/frames/" in path:
        _validated_frame_rows(data)
    elif "/companyfacts/" in path: valid=isinstance(data.get("facts"),dict)
    elif "/submissions/" in path: valid=isinstance(data.get("filings"),dict)
    elif "search-index" in path: valid=isinstance(data.get("hits"),dict) and isinstance(data["hits"].get("hits"),list)
    elif path.endswith("company_tickers_exchange.json"):
        valid=isinstance(data.get("fields"),list) and isinstance(data.get("data"),list)
    elif path.endswith("company_tickers.json"):
        valid=bool(data) and all(isinstance(v,dict) and v.get("ticker") and (v.get("cik_str") or v.get("cik")) for v in data.values())
    if not valid: raise SECTransportError("SEC_JSON_SCHEMA_INVALID")
    return data


def transport_error_details(error):
    """Persist only allowlisted HTTP metadata; never response bodies, URLs or auth values."""
    details = {"type": type(error).__name__}
    if isinstance(error, SECTransportError):
        details["code"]=error.code
    if not isinstance(error, urllib.error.HTTPError):
        return details
    details["http_status"] = error.code
    if getattr(error,"sec_cached_denial",False): details["request_sent"]=False
    host = urllib.parse.urlsplit(error.url or "").hostname
    details["endpoint"] = {
        "www.sec.gov": "SEC_WWW",
        "data.sec.gov": "SEC_DATA",
        "efts.sec.gov": "SEC_EFTS",
        "r.jina.ai": "JINA_READER",
    }.get(host, "OTHER")
    headers = error.headers or {}
    challenge = str(headers.get("WWW-Authenticate", "")).split(None, 1)
    if challenge and challenge[0].lower() in {"basic", "bearer", "digest"}:
        details["auth_challenge_scheme"] = challenge[0].lower()
    request_id = str(headers.get("X-Request-ID", ""))
    if re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", request_id):
        details["request_id"] = request_id
    cf_ray = str(headers.get("CF-Ray", ""))
    if re.fullmatch(r"[0-9a-fA-F]{16}-[A-Z]{3}", cf_ray):
        details["cf_ray"] = cf_ray
    retry_after = str(headers.get("Retry-After", ""))
    if re.fullmatch(r"[0-9]{1,6}", retry_after):
        details["retry_after_seconds"] = int(retry_after)
    return details

_proxy_rate_lock=threading.Lock()
_proxy_next_start=0.0

def proxy_json(url):
    # Process-local fallback pacing controls request START rate while allowing network I/O to overlap.
    global _proxy_next_start
    with _proxy_rate_lock:
        wait=max(0.0,_proxy_next_start-time.monotonic())
        if wait: time.sleep(wait)
        _proxy_next_start=time.monotonic()+PROXY_START_INTERVAL_SECONDS
    proxy="https://r.jina.ai/"+url
    req=urllib.request.Request(proxy,headers={"User-Agent":"stock-shadow-fundamental-observer/1.0","Accept":"text/plain"})
    with urllib.request.urlopen(req,timeout=35) as r: body,media=_bounded_response(r)
    if media not in ("text/plain","text/markdown","application/json"):
        raise SECTransportError("SEC_JSON_CONTENT_TYPE_INVALID")
    try: raw=body.decode("utf-8-sig").strip()
    except UnicodeDecodeError: raise SECTransportError("SEC_JSON_INVALID") from None
    if raw.startswith("<"): raise SECTransportError("SEC_JSON_HTML_RESPONSE")
    # Read-only transport fallback; payload remains SEC JSON.
    if raw.startswith("Markdown Content:"): raw=raw.split("Markdown Content:",1)[1].strip()
    # Accept only the known wrapper and a complete JSON fence, never arbitrary error prose.
    if raw.startswith("```json\n") and raw.endswith("\n```"): raw=raw[8:-4].strip()
    elif raw.startswith("```\n") and raw.endswith("\n```"): raw=raw[4:-4].strip()
    try: data=json.loads(raw)
    except json.JSONDecodeError: raise SECTransportError("SEC_JSON_INVALID") from None
    return _validate_sec_json(data,url)


def download_bulk_zip(url):
    # Stream large SEC archives to disk; do not hold the whole market archive in RAM.
    sec_user_agent()  # Configuration must exist before allocating a temporary archive.
    tmp=tempfile.NamedTemporaryFile(prefix="stock-shadow-sec-",suffix=".zip",delete=False)
    try:
        with _sec_open(url,"application/zip",timeout=120,encoding="identity") as r:
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


FMP_BASE="https://financialmodelingprep.com/stable"
STOCKFIT_BASE="https://api.stockfit.io/v1"
STOCKFIT_DAILY_BUDGET=225
STOCKFIT_SYMBOL_BUDGET=75

def fmp_bulk_csv(endpoint, year, period, api_key):
    url=FMP_BASE+"/"+endpoint+"?"+urllib.parse.urlencode({"year":year,"period":period,"apikey":api_key})
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"text/csv"})
    with urllib.request.urlopen(req,timeout=20) as r:
        raw=r.read().decode("utf-8-sig","replace")
    if raw.lstrip().startswith(("{","[")):
        raise ValueError("fmp_bulk_not_csv_or_plan_denied")
    return list(csv.DictReader(io.StringIO(raw)))

def _num(v):
    try: return float(v) if v not in (None,"","None","null") else None
    except (TypeError,ValueError): return None

def stockfit_get(path, api_key):
    url=STOCKFIT_BASE+path
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"application/json","Authorization":"Bearer "+api_key})
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.load(r),{k.lower():v for k,v in r.headers.items()}

def has_financial_values(ev):
    return bool(ev) and any(((ev.get(k) or {}).get("values")) for k in
                          ("revenue","net_income","operating_cash_flow","free_cash_flow","cash","total_debt","shares"))

def missing_financial_fields(ev):
    ev=ev or {}
    return {k for k in ("revenue","net_income","operating_cash_flow","free_cash_flow","cash","total_debt","shares")
            if not ((ev.get(k) or {}).get("values"))}

def stockfit_evidence(symbol, api_key, missing_fields):
    """Read only statement families required by residual cached/SEC gaps."""
    need=set(missing_fields or ())
    bucket_fields={"income":{"revenue","net_income","shares"},"balance":{"cash","total_debt"},"cashflow":{"operating_cash_flow","free_cash_flow"}}
    paths={"income":"/api/financials/income-statement?"+urllib.parse.urlencode({"symbol":symbol,"period":"annual","limit":2}),
      "balance":"/api/financials/balance-sheet?"+urllib.parse.urlencode({"symbol":symbol,"period":"annual","limit":2}),
      "cashflow":"/api/financials/cash-flow-statement?"+urllib.parse.urlencode({"symbol":symbol,"period":"annual","limit":2})}
    rows={}; headers={}; requests=0; called=[]
    for bucket,fields in bucket_fields.items():
        if not (need & fields): continue
        data,h=stockfit_get(paths[bucket],api_key); requests+=1; headers=h; called.append(bucket)
        rows[bucket]=data if isinstance(data,list) else (data.get("data") or [])
    def values(bucket, fields):
        out=[]
        for row in rows.get(bucket,[]):
            facts=row.get("facts") or {}
            val=next((facts.get(k) for k in fields if facts.get(k) is not None),None)
            if val is not None:
                out.append({"end":row.get("period"),"val":val,"form":"STOCKFIT_NORMALIZED","filed":row.get("dateFiled"),
                            "accession":next(iter((row.get("sources") or {}).keys()),None)})
        out.sort(key=lambda x:str(x.get("end") or "")); return out[-5:]
    mapping={"revenue":("income",("revenue","revenues","salesRevenueNet")),"net_income":("income",("netIncome","netIncomeLoss","profitLoss")),
      "operating_cash_flow":("cashflow",("operatingCashFlow","netCashProvidedByOperatingActivities","netCashProvidedByUsedInOperatingActivities")),
      "free_cash_flow":("cashflow",("freeCashFlow",)),"cash":("balance",("cashAndCashEquivalents","cashAndShortTermInvestments","cash")),
      "total_debt":("balance",("totalDebt","longTermDebt","debt")),"shares":("income",("weightedAverageShares","weightedAverageShsOut","weightedAverageSharesOutstanding"))}
    ev={}
    for key,(bucket,fields) in mapping.items():
        vals=values(bucket,fields); ev[key]={"concept":"STOCKFIT_NORMALIZED","values":vals,"trend":_trend(vals)}
    sh=ev["shares"]["values"]; dilution=None
    if len(sh)>=2 and float(sh[-2]["val"]): dilution=(float(sh[-1]["val"])/float(sh[-2]["val"])-1)*100
    ev["share_dilution_pct_latest"]=round(dilution,4) if dilution is not None else None
    return ev,{"requests":requests,"called_buckets":called,"remaining_day":headers.get("x-ratelimit-remaining-day"),"remaining_minute":headers.get("x-ratelimit-remaining-minute")}

def stockfit_gap_evidence(gaps_by_symbol, api_key):
    """Small residual fallback only; systemic gaps must be solved by bulk concept coverage."""
    out={}; errors=[]; requests=0; remaining_day=None; endpoint_requests={"income":0,"balance":0,"cashflow":0}
    for sym,missing in gaps_by_symbol.items():
        try:
            ev,meta=stockfit_evidence(sym,api_key,missing); requests+=meta["requests"]; remaining_day=meta.get("remaining_day")
            for bucket in meta.get("called_buckets") or []: endpoint_requests[bucket]+=1
            if any(ev[k]["values"] for k in ("revenue","net_income","operating_cash_flow","free_cash_flow","cash","total_debt","shares")): out[sym]=ev
        except urllib.error.HTTPError as e:
            errors.append({"symbol":sym,"type":"HTTPError","status":e.code,"message":str(e.reason)[:100]})
        except Exception as e:
            errors.append({"symbol":sym,"type":type(e).__name__,"message":str(e)[:120]})
    return out,{"provider":"STOCKFIT_FREE_RESIDUAL_GAP","attempted":bool(gaps_by_symbol),"requests":requests,"endpoint_requests":endpoint_requests,
                "matched_symbols":len(out),"remaining_day":remaining_day,"errors":errors}

def fmp_bulk_evidence(symbols, api_key):
    """FMP bulk financial statements. Observation-only; no trading decisions consume this output."""
    wanted=set(symbols); raw={s:{"income":[],"balance":[],"cashflow":[]} for s in wanted}
    requests=0; errors=[]
    periods=[(2025,"Q4"),(2026,"Q1"),(2026,"Q2"),(2026,"Q3")]
    endpoints=[("income-statement-bulk","income"),("balance-sheet-statement-bulk","balance"),("cash-flow-statement-bulk","cashflow")]
    tasks=[(endpoint,bucket,year,period) for endpoint,bucket in endpoints for year,period in periods]
    requests=len(tasks)
    def fetch_bulk(task):
        endpoint,bucket,year,period=task
        return task,fmp_bulk_csv(endpoint,year,period,api_key)
    # Fixed market-wide request count; bounded parallelism avoids a slow endpoint serialising all 12 calls.
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures=[ex.submit(fetch_bulk,t) for t in tasks]
        for fut in as_completed(futures):
            try:
                (endpoint,bucket,year,period),rows=fut.result()
                for row in rows:
                    sym=str(row.get("symbol") or "").upper()
                    if sym in wanted: raw[sym][bucket].append(row)
            except Exception as e:
                errors.append({"type":type(e).__name__,"message":str(e)[:120]})
    out={}
    def series(rows,field):
        vals=[]
        for r in rows:
            v=_num(r.get(field))
            if v is not None: vals.append({"end":r.get("date"),"val":v,"form":"FMP_NORMALIZED","filed":r.get("filingDate"),"period":r.get("period")})
        vals.sort(key=lambda x:str(x.get("end") or ""))
        return vals[-5:]
    for sym,b in raw.items():
        inc=series(b["income"],"revenue"); ni=series(b["income"],"netIncome")
        ocf=series(b["cashflow"],"operatingCashFlow"); fcf=series(b["cashflow"],"freeCashFlow")
        cash=series(b["balance"],"cashAndCashEquivalents"); debt=series(b["balance"],"totalDebt")
        shares=series(b["income"],"weightedAverageShsOut")
        ev={}
        for key,vals in [("revenue",inc),("net_income",ni),("operating_cash_flow",ocf),("free_cash_flow",fcf),("cash",cash),("total_debt",debt),("shares",shares)]:
            ev[key]={"concept":"FMP_NORMALIZED", "values":vals, "trend":_trend(vals)}
        dilution=None
        if len(shares)>=2 and shares[-2]["val"]:
            dilution=(shares[-1]["val"]/shares[-2]["val"]-1)*100
        ev["share_dilution_pct_latest"]=round(dilution,4) if dilution is not None else None
        if any((ev[k]["values"] for k in ("revenue","net_income","operating_cash_flow","cash","total_debt"))):
            out[sym]=ev
    return out,{"provider":"FMP_BULK","attempted":True,"requests":requests,"errors":errors,"matched_symbols":len(out)}

_sec_direct_blocked=False
_sec_direct_lock=threading.Lock()

def frame_json(taxonomy, concept, unit, period):
    global _sec_direct_blocked
    url=f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{period}.json"
    if not _sec_direct_blocked:
        try:
            return get(url),"SEC_FRAMES_DIRECT"
        except urllib.error.HTTPError as e:
            if e.code not in (403,429): raise
            with _sec_direct_lock: _sec_direct_blocked=True
    return proxy_json(url),"SEC_FRAMES_VIA_READONLY_PROXY"

def _validated_frame_rows(data):
    if not isinstance(data,dict) or not isinstance(data.get("data"),list):
        raise SECTransportError("SEC_JSON_SCHEMA_INVALID")
    rows=data["data"]
    for row in rows:
        if (not isinstance(row,dict) or type(row.get("cik")) is not int or row["cik"]<=0
                or type(row.get("val")) not in (int,float) or not math.isfinite(row["val"])
                or not isinstance(row.get("end"),str)):
            raise SECTransportError("SEC_FRAME_ROW_INVALID")
        try:
            if datetime.strptime(row["end"],"%Y-%m-%d").strftime("%Y-%m-%d")!=row["end"]:
                raise ValueError()
        except ValueError:
            raise SECTransportError("SEC_FRAME_ROW_INVALID") from None
    return rows

def frame_evidence_by_cik(requested_batch_index=0):
    # Market-wide SEC XBRL Frames: one request returns one concept for all reporting entities.
    # Two completed quarters are enough for direction/trend evidence without per-company HTTP loops.
    periods=["CY2025Q4","CY2026Q1","CY2026Q2"]
    instant=["CY2025Q4I","CY2026Q1I","CY2026Q2I"]
    specs=[
      ("revenue","us-gaap","RevenueFromContractWithCustomerExcludingAssessedTax","USD",periods),
      ("revenue_alt","us-gaap","Revenues","USD",periods),
      ("revenue_alt2","us-gaap","SalesRevenueNet","USD",periods),
      ("net_income","us-gaap","NetIncomeLoss","USD",periods),
      ("net_income_alt","us-gaap","ProfitLoss","USD",periods),
      ("operating_cash_flow","us-gaap","NetCashProvidedByUsedInOperatingActivities","USD",periods),
      ("capex","us-gaap","PaymentsToAcquirePropertyPlantAndEquipment","USD",periods),
      ("cash","us-gaap","CashAndCashEquivalentsAtCarryingValue","USD",instant),
      ("cash_alt","us-gaap","CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents","USD",instant),
      ("total_debt","us-gaap","LongTermDebt","USD",instant),
      ("total_debt_alt","us-gaap","LongTermDebtCurrent","USD",instant),
      ("total_debt_alt2","us-gaap","LongTermDebtAndFinanceLeaseObligationsCurrent","USD",instant),
      ("total_debt_alt3","us-gaap","LongTermDebtAndFinanceLeaseObligationsNoncurrent","USD",instant),
      ("total_debt_alt4","us-gaap","ShortTermBorrowings","USD",instant),
      ("shares","dei","EntityCommonStockSharesOutstanding","shares",instant),
      ("shares_alt","us-gaap","CommonStockSharesOutstanding","shares",instant),
      # Foreign private issuers such as XP can report IFRS rather than US-GAAP.
      ("revenue","ifrs-full","Revenue","USD",["CY2024","CY2025"]),
      ("net_income","ifrs-full","ProfitLoss","USD",["CY2024","CY2025"]),
      ("operating_cash_flow","ifrs-full","CashFlowsFromUsedInOperatingActivities","USD",["CY2024","CY2025"]),
      ("cash","ifrs-full","CashAndCashEquivalents","USD",["CY2024I","CY2025I"]),
      ("total_debt","ifrs-full","Borrowings","USD",["CY2024I","CY2025I"]),
    ]
    raw={}; transport=set(); request_errors=[]; successful_requests=0; empty_requests=0
    all_tasks=[(key,tax,concept,unit,p) for key,tax,concept,unit,ps in specs for p in ps]
    # Each request is market-wide, but SEC still rate-limits concept/period endpoints.
    # Rotate a fixed request budget and merge persisted evidence across runs.
    batches=max(1,math.ceil(len(all_tasks)/FRAME_REQUEST_BUDGET))
    batch_index=int(requested_batch_index)%batches
    start=batch_index*FRAME_REQUEST_BUDGET
    tasks=all_tasks[start:start+FRAME_REQUEST_BUDGET]
    def fetch_one(task):
        key,tax,concept,unit,p=task
        last=None
        for attempt in range(3):
            try:
                d,t=frame_json(tax,concept,unit,p)
                return task,d,t
            except urllib.error.HTTPError as e:
                last=e
                if e.code!=429: raise
                time.sleep(2*(attempt+1))
            except json.JSONDecodeError as e:
                last=e; time.sleep(1*(attempt+1))
        raise last
    # Frames are independent market-wide reads. Small bounded parallelism keeps the observer
    # below workflow timeout without creating per-symbol request storms.
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures={ex.submit(fetch_one,t): t for t in tasks}
        for fut in as_completed(futures):
            try:
                (key,tax,concept,unit,p),d,t=fut.result()
                rows=_validated_frame_rows(d)  # Validate the entire response before merging any row.
                transport.add(t)
                if not rows:
                    empty_requests+=1
                    continue
                for row in rows:
                    cik=row["cik"]
                    raw.setdefault(cik,{}).setdefault(key,[]).append(
                        {"end":row.get("end"),"val":row.get("val"),"form":row.get("form"),"filed":row.get("filed"),"period":p})
                successful_requests+=1
            except Exception as e:
                request_errors.append({**transport_error_details(e),
                    "task": dict(zip(("field", "taxonomy", "concept", "unit", "period"), futures[fut]))})
    out={}
    for cik,x in raw.items():
        def vals(primary,*alts):
            rows=x.get(primary) or []
            if not rows:
                for alt in alts:
                    rows=x.get(alt) or []
                    if rows: break
            rows=[r for r in rows if r.get("val") is not None]
            rows.sort(key=lambda r:(str(r.get("end") or ""),str(r.get("filed") or "")))
            return rows[-5:]
        ev={}
        for key,primary,alt in [
            ("revenue","revenue",("revenue_alt","revenue_alt2")),("net_income","net_income",("net_income_alt",)),
            ("operating_cash_flow","operating_cash_flow",()),("cash","cash",("cash_alt",)),
            ("total_debt","total_debt",("total_debt_alt","total_debt_alt2","total_debt_alt3","total_debt_alt4")),("shares","shares",("shares_alt",))]:
            v=vals(primary,*alt)
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
    return out,{"status":"PARTIAL" if request_errors or empty_requests else "OK",
                "empty_requests":empty_requests,"successful_requests":successful_requests,"requests":len(tasks),"request_budget":FRAME_REQUEST_BUDGET,"total_tasks":len(all_tasks),"batch_index":batch_index,"transports":sorted(transport),"errors":request_errors,"matched_ciks":len(out)}

def merge_financial_evidence(prior,current):
    """Accumulate bounded frame batches without erasing evidence learned in prior runs."""
    prior=prior or {}; current=current or {}; out={}
    for key in ("revenue","net_income","operating_cash_flow","free_cash_flow","cash","total_debt","shares"):
        cur=current.get(key) or {}; old=prior.get(key) or {}
        out[key]=cur if cur.get("values") else old
    out["share_dilution_pct_latest"]=current.get("share_dilution_pct_latest")
    if out["share_dilution_pct_latest"] is None:
        out["share_dilution_pct_latest"]=prior.get("share_dilution_pct_latest")
    return out

def evidence_sufficient(ev):
    return sum(bool((ev.get(k) or {}).get("values")) for k in ("revenue","net_income","operating_cash_flow","cash","total_debt"))>=3

def ticker_map():
    urls=["https://www.sec.gov/files/company_tickers.json","https://www.sec.gov/files/company_tickers_exchange.json"]
    errs=[]
    for url in urls:
        try:
            raw=get(url)
            if isinstance(raw,dict) and "data" in raw and "fields" in raw:
                fields=raw["fields"]; return {str(dict(zip(fields,row)).get("ticker","")).upper().replace(".","-"):dict(zip(fields,row)) for row in raw["data"]},errs
            return {str(v.get("ticker","")).upper().replace(".","-"):v for v in raw.values()},errs
        except SECTransportError as e:
            errs.append({"url":url,**transport_error_details(e)})
            return {},errs  # No proxy substitution for invalid config or invalid payload.
        except urllib.error.HTTPError as e: errs.append({"url":url,"status":e.code,**transport_error_details(e)})
        except Exception as e: errs.append({"url":url,"type":type(e).__name__,"message":str(e)[:120]})
    # GitHub-hosted runners can be blocked by SEC edge policy. Try a read-only
    # transport proxy for the same SEC JSON; never use proxy-derived trading data.
    try:
        raw=proxy_json("https://www.sec.gov/files/company_tickers.json")
        return {str(v.get("ticker","")).upper().replace(".","-"):v for v in raw.values()},errs
    except Exception as e:
        errs.append({"url":"SEC_TICKER_MAP_VIA_READONLY_PROXY",**transport_error_details(e)})
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

def _sec_json_with_readonly_fallback(url):
    global _sec_direct_blocked
    if not _sec_direct_blocked:
        try:
            return get(url),"SEC_DIRECT"
        except urllib.error.HTTPError as e:
            if e.code not in (403,429): raise
            with _sec_direct_lock: _sec_direct_blocked=True
    return proxy_json(url),"SEC_VIA_READONLY_PROXY"

def sec_submission(cik):
    return _sec_json_with_readonly_fallback(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")

def sec_companyfacts(cik):
    return _sec_json_with_readonly_fallback(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")

def _validated_filing_text(body,media,proxy=False):
    allowed=("text/plain","text/markdown") if proxy else ("text/html","text/plain","application/xhtml+xml")
    if media not in allowed:
        raise SECTransportError("SEC_TEXT_CONTENT_TYPE_INVALID")
    text=body.decode("utf-8","replace")
    if (not text.strip() or text.lstrip().startswith(("{","["))
            or (proxy and text.lstrip().startswith("<"))
            or any(marker in text[:500].lower() for marker in ("access denied","error fetching","forbidden","error:"))):
        raise SECTransportError("SEC_TEXT_PAYLOAD_INVALID")
    return text

def sec_filing_text(cik, accession, primary_document):
    if not accession or not primary_document: raise ValueError("filing_document_identity_missing")
    acc=str(accession).replace("-","")
    url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{primary_document}"
    try:
        raw,media=_sec_response(url,"text/html,text/plain")
        return _validated_filing_text(raw,media),"SEC_DIRECT"
    except urllib.error.HTTPError as e:
        if e.code not in (403,429): raise
    # Read-only fallback for SEC edge blocks on GitHub-hosted runners.
    proxy="https://r.jina.ai/"+url
    with _proxy_rate_lock:
        global _proxy_next_start
        wait=max(0.0,_proxy_next_start-time.monotonic())
        if wait: time.sleep(wait)
        _proxy_next_start=time.monotonic()+1.0
    req=urllib.request.Request(proxy,headers={"User-Agent":"stock-shadow-filing-observer/1.0","Accept":"text/plain"})
    with urllib.request.urlopen(req,timeout=35) as r: body,media=_bounded_response(r)
    return _validated_filing_text(body,media,proxy=True),"SEC_VIA_READONLY_PROXY"

def semantic_risk_evidence(reviewed_docs):
    """Tri-state text evidence. VERIFIED_ABSENT means absent from the explicitly reviewed document scope only."""
    patterns={
      "going_concern":("going concern","substantial doubt about our ability","substantial doubt about the company"),
      "bankruptcy_restructuring":("bankruptcy","chapter 11","restructuring support agreement","debtor-in-possession"),
      "delisting_risk":("delisting","delist","noncompliance with the listing","listing deficiency"),
      "material_8k_risk":("material definitive agreement","event of default","termination of a material definitive agreement",
                          "bankruptcy or receivership","impairment","notice of delisting"),
    }
    if not reviewed_docs:
        return {"material_8k_present":None,"going_concern":None,"bankruptcy_restructuring":None,"delisting_risk":None,
                "material_8k_risk":None,"semantic_review_status":"NOT_YET_TEXT_VERIFIED",
                "semantic_risk_state":"UNKNOWN_PENDING_TEXT_REVIEW","reviewed_documents":[]}
    joined="\n".join(str(x.get("text") or "").lower() for x in reviewed_docs)
    forms=[str(x.get("form") or "") for x in reviewed_docs]
    out={"material_8k_present":"VERIFIED_PRESENT" if any(x.startswith("8-K") for x in forms) else "VERIFIED_ABSENT"}
    for key,needles in patterns.items():
        scope=joined if key!="material_8k_risk" else "\n".join(str(x.get("text") or "").lower() for x in reviewed_docs if str(x.get("form") or "").startswith("8-K"))
        out[key]="VERIFIED_PRESENT" if scope and any(n in scope for n in needles) else "VERIFIED_ABSENT"
    out["semantic_review_status"]="TEXT_VERIFIED"
    out["semantic_risk_state"]="VERIFIED_PRESENT" if any(out[k]=="VERIFIED_PRESENT" for k in patterns) else "VERIFIED_ABSENT"
    out["reviewed_documents"]=[{k:x.get(k) for k in ("form","filing_date","accession","primary_document","transport")} for x in reviewed_docs]
    return out

def _fact_series(facts, concepts, units=("USD","shares")):
    namespaces=[(facts.get("facts") or {}).get("us-gaap") or {},(facts.get("facts") or {}).get("ifrs-full") or {}]
    for concept in concepts:
        node={}
        for ns in namespaces:
            if concept in ns: node=ns.get(concept) or {}; break
        unit_map=node.get("units") or {}
        rows=[]
        for unit in units:
            rows.extend(unit_map.get(unit) or [])
        # Foreign private issuers commonly file IFRS facts in their functional
        # currency (TWD/EUR/ILS/etc.) rather than USD. CompanyFacts is already
        # scoped to one issuer, so a same-concept non-share currency series is
        # valid evidence and must not be discarded merely because it is not USD.
        if not rows and "USD" in units:
            for unit,unit_rows in unit_map.items():
                if unit not in {"shares","pure"}:
                    rows.extend(unit_rows or [])
        if rows:
            # Prefer filed annual/quarterly facts; de-duplicate amended/repeated facts by end date.
            good=[r for r in rows if r.get("end") and r.get("val") is not None and r.get("form") in {"10-K","10-Q","10-K/A","10-Q/A","20-F","20-F/A","6-K","6-K/A"}]
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
    # Preserve direction across negative values: -43 -> -55 is deterioration, not improvement.
    pct=((b-a)/abs(a))*100
    if pct>5: return "IMPROVING"
    if pct<-5: return "DETERIORATING"
    return "STABLE"

def financial_evidence(facts):
    specs={
      "revenue":(["RevenueFromContractWithCustomerExcludingAssessedTax","RevenueFromContractWithCustomers","Revenues","SalesRevenueNet","Revenue"],("USD",)),
      "net_income":(["NetIncomeLoss","ProfitLoss"],("USD",)),
      "operating_cash_flow":(["NetCashProvidedByUsedInOperatingActivities","CashFlowsFromUsedInOperatingActivities"],("USD",)),
      "cash":(["CashAndCashEquivalentsAtCarryingValue","CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents","CashAndCashEquivalents"],("USD",)),
      "total_debt":(["LongTermDebtAndFinanceLeaseObligationsCurrent","LongTermDebtCurrent","LongTermDebt","Borrowings","LongtermBorrowings"],("USD",)),
      "shares":(["CommonStocksIncludingAdditionalPaidInCapitalMember","CommonStockSharesOutstanding","EntityCommonStockSharesOutstanding"],("shares",)),
    }
    out={}
    for name,(concepts,units) in specs.items():
        concept,values=_latest_values(facts,concepts,units=units)
        out[name]={"concept":concept,"values":values,"trend":_trend(values)}
    # FCF is evidence-derived only when both OCF and capex facts are available.
    _,capex=_latest_values(facts,["PaymentsToAcquirePropertyPlantAndEquipment","PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],units=("USD",))
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
    severe=any(risk_flags.get(k) in (True,"VERIFIED_PRESENT") for k in ("bankruptcy_restructuring","going_concern","delisting_risk"))
    if severe: return "CRITICAL"
    if bad>=2 or (dilution is not None and dilution>=10): return "DETERIORATING"
    if bad==1 or (dilution is not None and dilution>=5) or risk_flags.get("material_8k_present") in (True,"VERIFIED_PRESENT"): return "WATCH"
    available=sum(bool(ev.get(k,{}).get("values")) for k in ("revenue","net_income","operating_cash_flow","cash","total_debt"))
    return "HEALTHY" if available>=3 else "UNKNOWN"

def filing_risk_evidence(latest):
    # None means UNKNOWN, never VERIFIED_ABSENT. Filing metadata can establish 8-K presence only.
    has_8k=any(x["form"].startswith("8-K") for x in latest)
    return {"material_8k_present":True if has_8k else None,
            "going_concern":None,"bankruptcy_restructuring":None,"delisting_risk":None,
            "material_8k_risk":None,"semantic_review_status":"NOT_YET_TEXT_VERIFIED",
            "semantic_risk_state":"UNKNOWN_PENDING_TEXT_REVIEW"}

def main():
    state=load(STATE,{"positions":{}}); old=load(OUT,{"companies":{}})
    companies=old.get("companies",{})
    # Migrate legacy observer output: old runs encoded unverified severe risks as False.
    # False must be reserved for VERIFIED_ABSENT; until text review exists these are unknown.
    for _company in companies.values():
        _risk=_company.get("risk_evidence")
        if isinstance(_risk,dict) and _risk.get("semantic_review_status")=="NOT_YET_TEXT_VERIFIED":
            for _key in ("going_concern","bankruptcy_restructuring","delisting_risk","material_8k_risk"):
                if _risk.get(_key) is False:
                    _risk[_key]=None
            _risk["semantic_risk_state"]="UNKNOWN_PENDING_TEXT_REVIEW"
    symbols=sorted(state.get("positions",{}))
    if not symbols:
        out={"updated_at":now(),"frames_refreshed_at":None,"mode":"OBSERVATION_ONLY","strategy_effect":False,"positions":0,"tracked":0,
             "evidence_complete":0,"evidence_pending":0,"pending_symbols":[],"refreshed_this_run":0,
             "bulk_transport":{"attempted":False,"reason":"NO_POSITIONS"},"frames_transport":{"attempted":False},
             "fallback_requests":0,"fallback_request_cap":FALLBACK_MAX_REQUESTS,"errors":[],"mapping_errors":[],
             "status":"OK_EMPTY","companies":{}}
        OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
        print(json.dumps({k:v for k,v in out.items() if k!="companies"})); return
    # Fundamentals/filings are low-frequency observation data. Reuse persisted SSOT for six hours.
    _last=old.get("market_batch_attempted_at") or old.get("market_batch_refreshed_at") or old.get("frames_refreshed_at") or old.get("updated_at")
    if _last and old.get("market_batch_schema_version")==MARKET_BATCH_SCHEMA_VERSION:
        try:
            _age=(datetime.now(timezone.utc)-datetime.fromisoformat(str(_last).replace("Z","+00:00"))).total_seconds()
        except Exception:
            _age=10**9
        if _age < 6*60*60:
            cached=dict(old); cached["cache_status"]="FUNDAMENTALS_CACHE_HIT_6H"; cached["strategy_effect"]=False
            print(json.dumps({k:v for k,v in cached.items() if k!="companies"})); return

    # Market-wide batch first: FMP bulk CSV covers the requested symbols in a small fixed
    # number of requests. SEC XBRL Frames is the independent market-wide supplement.
    # Never download multi-GB SEC bulk archives in hourly CI.
    ticker_map_data,map_errors=ticker_map()
    symbol_cik={}; errors=[]
    for sym in symbols:
        meta=ticker_map_data.get(sym)
        try:
            cik=int(meta.get("cik_str") or meta.get("cik")) if meta else resolve_cik_efts(sym)
            if cik: symbol_cik[sym]=cik
            else: companies[sym]={"symbol":sym,"status":"NO_SEC_MAPPING","updated_at":now(),"strategy_effect":False}
        except Exception as e:
            errors.append({"symbol":sym,"stage":"CIK_RESOLUTION",**transport_error_details(e)})
    bulk={"attempted":False,"reason":"DISABLED_IN_HOURLY_CI_MULTI_GB_ARCHIVE","ciks_requested":len(set(symbol_cik.values()))}

    last_batch_at=old.get("market_batch_attempted_at") or old.get("market_batch_refreshed_at") or old.get("frames_refreshed_at") or old.get("updated_at")
    fmp_refreshed_at=old.get("fmp_refreshed_at")
    batch_due=True
    if last_batch_at:
        try:
            last_dt=datetime.fromisoformat(str(last_batch_at).replace("Z","+00:00"))
            batch_due=(datetime.now(timezone.utc)-last_dt).total_seconds() >= 6*60*60
        except Exception:
            batch_due=True
    if old.get("market_batch_schema_version") != MARKET_BATCH_SCHEMA_VERSION:
        batch_due=True
    market_batch_refreshed_at=old.get("market_batch_refreshed_at") or old.get("frames_refreshed_at") or old.get("updated_at")
    frames_refreshed_at=old.get("frames_refreshed_at") or old.get("updated_at")
    market_batch_attempted_at=old.get("market_batch_attempted_at")
    frames_by_cik={}; fmp_by_symbol={}; stockfit_by_symbol={}
    stockfit_status={"provider":"STOCKFIT_FREE","attempted":False,"status":"CACHE_FRESH"}
    frames_status={"attempted":False,"status":"CACHE_FRESH","reason":"SIX_HOUR_MARKET_BATCH_CACHE"}
    fmp_status={"provider":"FMP_BULK","attempted":False,"status":"CACHE_FRESH","reason":"SIX_HOUR_MARKET_BATCH_CACHE"}
    if batch_due:
        # Known account capability: FMP bulk returns HTTP 402. Do not repeat doomed production calls.
        fmp_status={"provider":"FMP_BULK","attempted":False,"status":"FMP_FREE_BULK_UNAVAILABLE","requests":0}
        fmp_refreshed_at=old.get("fmp_refreshed_at")
        try:
            # Batch zero is a valid completed batch, not a missing cursor.
            saved_batch_index=(old.get("frames_transport") or {}).get("batch_index")
            previous_batch_index=int(saved_batch_index if saved_batch_index is not None else -1)
            frames_by_cik,frames_status=frame_evidence_by_cik(previous_batch_index+1)
            frames_status["attempted"]=True
            frames_status["status"]="PARTIAL" if (frames_status.get("errors") or frames_status.get("empty_requests")
                or frames_status.get("status")=="PARTIAL") else "OK"
            if frames_status.get("successful_requests",0): frames_refreshed_at=now()
        except Exception as e:
            frames_status={"attempted":True,"status":"FAILED","error":f"{type(e).__name__}:{str(e)[:160]}"}
        preliminary={sym:merge_financial_evidence((companies.get(sym) or {}).get("financial_evidence"),frames_by_cik.get(cik))
                     for sym,cik in symbol_cik.items()}
        field_missing={field:[s for s in symbols if field in missing_financial_fields(preliminary.get(s))]
                       for field in ("revenue","net_income","operating_cash_flow","free_cash_flow","cash","total_debt","shares")}
        systemic_fields={field for field,syms in field_missing.items() if len(syms)>max(12,int(len(symbols)*0.10))}
        residual_gaps={}
        for sym in symbols:
            miss=missing_financial_fields(preliminary.get(sym))-systemic_fields
            if miss: residual_gaps[sym]=miss
        residual_gaps=dict(list(sorted(residual_gaps.items()))[:12])
        stockfit_key=os.getenv("STOCKFIT_API_KEY")
        if stockfit_key and residual_gaps:
            stockfit_by_symbol,stockfit_status=stockfit_gap_evidence(residual_gaps,stockfit_key)
            stockfit_status["status"]="OK" if not stockfit_status.get("errors") else "PARTIAL"
            stockfit_status["systemic_fields"]=sorted(systemic_fields); stockfit_status["residual_symbols"]=sorted(residual_gaps)
            stockfit_refreshed_at=now() if stockfit_status.get("requests") else old.get("stockfit_refreshed_at")
        else:
            stockfit_status={"provider":"STOCKFIT_FREE_RESIDUAL_GAP","attempted":False,
                             "status":"NO_RESIDUAL_GAPS" if not residual_gaps else "NO_API_KEY","requests":0,
                             "systemic_fields":sorted(systemic_fields),"residual_symbols":sorted(residual_gaps)}
            stockfit_refreshed_at=old.get("stockfit_refreshed_at")
        stockfit_quota_day=old.get("stockfit_quota_day")
        stockfit_requests_today=int(old.get("stockfit_requests_today") or 0)+int(stockfit_status.get("requests") or 0)
        market_batch_attempted_at=now()
        if frames_status.get("successful_requests",0) or stockfit_by_symbol:
            market_batch_refreshed_at=now()

    # Rank only residual gaps after cached + SEC Frames + precise StockFit fallback have been merged.
    ranked=[]
    for sym,cik in symbol_cik.items():
        prior=companies.get(sym) or {}
        merged=merge_financial_evidence(prior.get("financial_evidence"),frames_by_cik.get(cik))
        merged=merge_financial_evidence(merged,stockfit_by_symbol.get(sym))
        if not evidence_sufficient(merged):
            ranked.append(sym)
    ranked.sort(key=lambda sym:str((companies.get(sym) or {}).get("gap_attempted_at")
        or (companies.get(sym) or {}).get("gap_refresh_at") or (companies.get(sym) or {}).get("updated_at") or ""))
    refresh_budget=4
    refresh_set=set(ranked[:refresh_budget])
    # Semantic filing review rotates independently from financial gaps so complete financial evidence
    # never prevents 10-K/10-Q/8-K/20-F/6-K text verification from eventually covering the cohort.
    semantic_ranked=sorted(symbol_cik,key=lambda sym:str((companies.get(sym) or {}).get("semantic_refresh_at") or ""))
    semantic_refresh_budget=4
    semantic_refresh_set=set(semantic_ranked[:semantic_refresh_budget])
    facts_by_cik={}; subs_by_cik={}
    def fetch_sec_pair(item):
        sym,cik=item
        facts=facts_t=sub=sub_t=None; errs=[]
        if sym in refresh_set:
            try: facts,facts_t=sec_companyfacts(cik)
            except Exception as e: errs.append({"symbol":sym,"stage":"COMPANYFACTS","type":type(e).__name__,"message":str(e)[:120]})
        if sym in semantic_refresh_set:
            try: sub,sub_t=sec_submission(cik)
            except Exception as e: errs.append({"symbol":sym,"stage":"SUBMISSIONS","type":type(e).__name__,"message":str(e)[:120]})
        return sym,cik,facts,facts_t,sub,sub_t,errs
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures=[ex.submit(fetch_sec_pair,(sym,symbol_cik[sym])) for sym in (refresh_set|semantic_refresh_set)]
        for fut in as_completed(futures):
            sym,cik,facts,facts_t,sub,sub_t,errs=fut.result()
            if facts is not None: facts_by_cik[cik]=(facts,facts_t)
            if sub is not None: subs_by_cik[cik]=(sub,sub_t)
            errors.extend(errs)
    sec_transport={"provider":"SEC_FRAMES_PLUS_RESIDUAL_GAP_BACKFILL","attempted":True,
                   "refresh_budget":refresh_budget,"requested_symbols":len(refresh_set),
                   "companyfacts_ok":len(facts_by_cik),"submissions_ok":len(subs_by_cik),
                   "semantic_refresh_budget":semantic_refresh_budget,"semantic_requested_symbols":len(semantic_refresh_set),
                   "frames_matched_ciks":len(frames_by_cik),"fmp_matched_symbols":len(fmp_by_symbol)}
    refreshed=0; fallback_requests=0
    for s,cik in symbol_cik.items():
        meta=ticker_map_data.get(s) or {}
        try:
            sub_pair=subs_by_cik.get(cik); facts_pair=facts_by_cik.get(cik)
            sub=sub_pair[0] if sub_pair else None; transport=sub_pair[1] if sub_pair else None
            facts=facts_pair[0] if facts_pair else None; facts_transport=facts_pair[1] if facts_pair else None
            frame_ev=frames_by_cik.get(cik); fmp_ev=fmp_by_symbol.get(s); stockfit_ev=stockfit_by_symbol.get(s)
            previous=companies.get(s) or {}
            prior_ev=previous.get("financial_evidence")
            # Filing metadata is optional here. Never turn frame-wide financial evidence
            # back into hundreds of per-company submissions requests.
            if facts is None and not prior_ev and not frame_ev and not fmp_ev:
                prev=companies.get(s) or {}
                if prev.get("financial_evidence"):
                    prev["status"]="OBSERVED_STALE_FALLBACK"; prev["processed_at"]=now(); prev["strategy_effect"]=False
                    companies[s]=prev
                else:
                    companies[s]={"symbol":s,"cik":cik,"company":meta.get("title"),"status":"SEC_REFRESH_PENDING",
                        "transport":transport,"companyfacts_transport":facts_transport,"updated_at":None,"processed_at":now(),
                        "gap_attempted_at":now() if s in refresh_set else prev.get("gap_attempted_at"),
                        "strategy_effect":False,"note":"SEC per-company refresh pending; prior evidence unavailable."}
                continue
            recent=((sub or {}).get("filings") or {}).get("recent") or {}
            forms=recent.get("form") or []; dates=recent.get("filingDate") or []; acc=recent.get("accessionNumber") or []
            docs=recent.get("primaryDocument") or []
            latest=[]
            for form,date,an,doc in zip(forms,dates,acc,docs):
                if form in {"10-K","10-Q","8-K","10-K/A","10-Q/A","8-K/A","20-F","20-F/A","6-K","6-K/A"}:
                    latest.append({"form":form,"filing_date":date,"accession":an,"primary_document":doc})
                if len(latest)>=12: break
            risk_forms=[x for x in latest if x["form"].startswith("8-K")]
            sec_ev=financial_evidence(facts) if facts is not None else None
            evidence=merge_financial_evidence(merge_financial_evidence(merge_financial_evidence(merge_financial_evidence(prior_ev,stockfit_ev),fmp_ev),frame_ev),sec_ev)
            prior_risk=(companies.get(s) or {}).get("risk_evidence") or filing_risk_evidence([])
            risk_flags=prior_risk
            semantic_refresh_at=(companies.get(s) or {}).get("semantic_refresh_at")
            if s in semantic_refresh_set and sub is not None:
                reviewed=[]
                for item in latest[:3]:
                    try:
                        text_body,text_transport=sec_filing_text(cik,item.get("accession"),item.get("primary_document"))
                        reviewed.append({**item,"text":text_body,"transport":text_transport})
                    except Exception as e:
                        errors.append({"symbol":s,"stage":"FILING_TEXT","form":item.get("form"),"type":type(e).__name__,"message":str(e)[:120]})
                if reviewed:
                    risk_flags=semantic_risk_evidence(reviewed)
                    semantic_refresh_at=now()
                else:
                    risk_flags=prior_risk
            new_financial=any(has_financial_values(x) for x in (sec_ev,frame_ev,fmp_ev,stockfit_ev))
            new_semantic=semantic_refresh_at!=previous.get("semantic_refresh_at")
            acquired=new_financial or new_semantic
            companies[s]={"symbol":s,"cik":cik,"company":(sub or {}).get("name") or meta.get("title"),
                "status":"OBSERVED" if acquired else "OBSERVED_STALE_FALLBACK","transport":transport,"companyfacts_transport":facts_transport,"fundamentals_provider":("SEC_GAP_BACKFILL" if facts is not None else ("FMP_BULK+SEC_XBRL_FRAMES" if fmp_ev and frame_ev else ("FMP_BULK" if fmp_ev else ("SEC_XBRL_FRAMES_MARKET_BATCH" if frame_ev else "CACHED_EVIDENCE")))),
                "latest_material_filings":latest,"financial_evidence":evidence,"risk_evidence":risk_flags,
                "recent_8k_count":len(risk_forms),"processed_at":now(),
                "updated_at":now() if acquired else previous.get("updated_at"),
                "source_evidence_at":now() if new_financial else previous.get("source_evidence_at",previous.get("updated_at")),
                "gap_attempted_at":now() if s in refresh_set else previous.get("gap_attempted_at"),
                "gap_refresh_at":(now() if has_financial_values(sec_ev) else previous.get("gap_refresh_at")),"semantic_refresh_at":semantic_refresh_at,
                "stockfit_refresh_at":(now() if stockfit_ev else (companies.get(s) or {}).get("stockfit_refresh_at")),
                "fundamental_state":classify_evidence(evidence,risk_flags),"strategy_effect":False,
                "note":"Observation-only fundamentals evidence; no automatic BUY/ADD/SELL effect."}
            refreshed+=int(acquired)
        except Exception as e:
            errors.append({"symbol":s,"stage":"EVIDENCE","type":type(e).__name__,"message":str(e)[:120]})
    complete=sum(1 for s in symbols if evidence_sufficient((companies.get(s) or {}).get("financial_evidence") or {}))
    pending_symbols=[s for s in symbols if not evidence_sufficient((companies.get(s) or {}).get("financial_evidence") or {})]
    pending_details={s:{"missing_fields":sorted(missing_financial_fields((companies.get(s) or {}).get("financial_evidence") or {})),
                        "available_fields":sorted(k for k in ("revenue","net_income","operating_cash_flow","free_cash_flow","cash","total_debt","shares")
                                                  if (((companies.get(s) or {}).get("financial_evidence") or {}).get(k) or {}).get("values")),
                        "concepts":{k:(((companies.get(s) or {}).get("financial_evidence") or {}).get(k) or {}).get("concept")
                                    for k in ("revenue","net_income","operating_cash_flow","cash","total_debt","shares")}}
                     for s in pending_symbols}
    if "stockfit_refreshed_at" not in locals(): stockfit_refreshed_at=old.get("stockfit_refreshed_at")
    if "stockfit_quota_day" not in locals(): stockfit_quota_day=old.get("stockfit_quota_day")
    if "stockfit_requests_today" not in locals(): stockfit_requests_today=old.get("stockfit_requests_today",0)
    out={"updated_at":now(),"market_batch_attempted_at":market_batch_attempted_at,"market_batch_schema_version":MARKET_BATCH_SCHEMA_VERSION,"stockfit_refreshed_at":stockfit_refreshed_at,"stockfit_quota_day":stockfit_quota_day,"stockfit_requests_today":stockfit_requests_today,"market_batch_refreshed_at":market_batch_refreshed_at,"fmp_refreshed_at":fmp_refreshed_at,"frames_refreshed_at":frames_refreshed_at,"mode":"OBSERVATION_ONLY","strategy_effect":False,"positions":len(symbols),
         "tracked":sum(1 for s in symbols if s in companies),"evidence_complete":complete,
         "evidence_pending":max(0,len(symbols)-complete),"pending_symbols":pending_symbols,"pending_details":pending_details,"refreshed_this_run":refreshed,
         "primary_transport":sec_transport,"stockfit_transport":stockfit_status,"fmp_transport":fmp_status,"bulk_transport":bulk,"frames_transport":frames_status,"fallback_requests":fallback_requests,"fallback_request_cap":FALLBACK_MAX_REQUESTS,
         "semantic_verified":sum(1 for s in symbols if ((companies.get(s) or {}).get("risk_evidence") or {}).get("semantic_review_status")=="TEXT_VERIFIED"),
         "semantic_pending":sum(1 for s in symbols if ((companies.get(s) or {}).get("risk_evidence") or {}).get("semantic_review_status")!="TEXT_VERIFIED"),
         "errors":errors,"mapping_errors":map_errors,"status":"OK" if (not errors and not map_errors and not frames_status.get("errors")
             and frames_status.get("status") not in ("PARTIAL","FAILED") and complete==len(symbols) and all(((companies.get(s) or {}).get("risk_evidence") or {}).get("semantic_review_status")=="TEXT_VERIFIED" for s in symbols)) else "PARTIAL","companies":companies}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
    print(json.dumps({k:v for k,v in out.items() if k!="companies"}))

if __name__=="__main__": main()
