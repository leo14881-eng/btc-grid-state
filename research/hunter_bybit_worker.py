"""Fresh Bybit spot research through the fixed public Worker market routes.

Listings are cached for 24 hours; tickers and candles are never reused across
discovery generations. Only Bybit-only assets need additional EARLY candles.
This adapter provides research evidence, never execution permission.
"""
import datetime as dt
import hashlib
import json
import math
import pathlib
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from research import hunter_bybit_availability as availability
    from research.hunter_bybit_signal_capture import capture
except ModuleNotFoundError:
    import hunter_bybit_availability as availability
    from hunter_bybit_signal_capture import capture

DEFAULT = pathlib.Path("research/results/hunter-bybit-worker-snapshot.json")
SOURCE = "OFFICIAL_BYBIT_V5_VIA_WORKER"

def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(availability.SPOT_PROXY + path, data=data, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)

def result(body):
    if not isinstance(body, dict) or body.get("retCode") != 0:
        raise ValueError("BYBIT_WORKER_UPSTREAM_ERROR")
    out = body.get("result")
    if not isinstance(out, dict) or out.get("category") != "spot":
        raise ValueError("BYBIT_WORKER_WRONG_CATEGORY")
    return out

def candles(body, symbol, interval, limit, end):
    data = result(body)
    if data.get("symbol") != symbol:
        raise ValueError("BYBIT_WORKER_KLINE_WRONG_SYMBOL")
    rows = data.get("list")
    step = int(interval) * 60 * 1000
    if not isinstance(rows, list) or len(rows) != limit:
        raise ValueError("BYBIT_WORKER_KLINE_INSUFFICIENT")
    rows = sorted(rows, key=lambda x: int(x[0]))
    stamps = [int(r[0]) for r in rows]
    if stamps[-1] != (end // step) * step or any(b-a != step for a,b in zip(stamps, stamps[1:])):
        raise ValueError("BYBIT_WORKER_KLINE_STALE_OR_GAPPED")
    for row in rows:
        if len(row) < 7 or any(not math.isfinite(float(v)) for v in row[:7]):
            raise ValueError("BYBIT_WORKER_KLINE_INVALID")
        o,h,l,c,v,q = map(float, row[1:7])
        if min(o,h,l,c) <= 0 or min(v,q) < 0 or l > min(o,c) or h < max(o,c) or l > h:
            raise ValueError("BYBIT_WORKER_KLINE_INVALID_OHLC")
    return rows

def collect(binance_bases=(), now=None, fetcher=request, listing_fetcher=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    end = int(now.timestamp() * 1000)
    listing_fetcher = listing_fetcher or availability.spot_proxy_universe
    bases, cache = listing_fetcher()
    try:
        from research.hunter_cex_scan import valid
    except ModuleNotFoundError:
        from hunter_cex_scan import valid
    bases = [base for base in bases if valid(base)]
    ticker_body = fetcher("/bybit/tickers")
    ticks = result(ticker_body)
    if abs(int(ticker_body.get("time",0))-end)>120000:
        raise ValueError("BYBIT_WORKER_TICKERS_STALE_OR_FUTURE")
    quotes = {}
    for q in ticks.get("list") or []:
        symbol = q.get("symbol") if isinstance(q,dict) else None
        if not symbol or symbol in quotes:
            raise ValueError("BYBIT_WORKER_TICKERS_INVALID_OR_DUPLICATE")
        quotes[symbol] = q
    rows=[]; missing=[]
    for base in sorted(bases):
        pair=base+"USDT"; q=quotes.get(pair,{})
        try:
            price,vol,change = [float(q[k]) for k in ("lastPrice","turnover24h","price24hPcnt")]
            if not all(math.isfinite(v) for v in (price,vol,change)) or price<=0 or vol<0:
                raise ValueError("invalid quote")
        except (KeyError,TypeError,ValueError,OverflowError):
            missing.append(pair); continue
        rows.append(dict(venue="bybit",pair=pair,base=base,price=price,volume_24h_usdt=vol,change_24h_pct=change*100))
    if missing:
        raise ValueError("BYBIT_WORKER_TICKERS_INCOMPLETE:"+",".join(missing[:10]))
    required=[r for r in rows if r["base"] not in set(binance_bases) and r["base"]!="BTC"]
    symbols=sorted({r["pair"] for r in required}|{"BTCUSDT"})
    batches=[symbols[i:i+20] for i in range(0,len(symbols),20)]
    bundles={}; batch_failures={}
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs={executor.submit(fetcher,"/bybit/early-klines",{"symbols":batch,"end":end}):batch for batch in batches}
        for job in as_completed(jobs):
            batch=jobs[job]
            try:
                data=result(job.result())
                if data.get("end")!=end or set(data.get("klines") or {})-set(batch):
                    raise ValueError("BYBIT_WORKER_BATCH_SCOPE_MISMATCH")
                bundles.update(data.get("klines") or {})
            except Exception as exc:
                batch_failures.update({s:type(exc).__name__+":"+str(exc)[:120] for s in batch})
    def get(symbol,interval,limit):
        if symbol in batch_failures:raise ValueError(batch_failures[symbol])
        return candles(bundles.get(symbol,{}).get(interval),symbol,interval,limit,end)
    try:
        signals, failures=capture(required,get)
    except RuntimeError as exc:
        signals={}; failures={s:str(exc) for s in symbols}
    signals={b:{**s,"source_observed_at_utc":now.isoformat()} for b,s in signals.items()}
    status=dict(active_pairs=len(bases),valid_pairs=len(rows),missing_or_invalid=[],
                source=SOURCE,captured_at_utc=now.isoformat(),signal_pairs=len(signals),
                signal_failures=len(failures),signal_complete=not failures and len(signals)==len(required),
                signal_expected_bases=sorted(r["base"] for r in required),
                listing_cache=cache, ticker_requests=1,kline_batch_requests=len(batches))
    payload=dict(schema="hunter_bybit_worker_v1",source=SOURCE,captured_at_utc=now.isoformat(),
                 rows=rows,venue_status=status,early_signals=signals,signal_failures=failures)
    payload["snapshot_sha256"]=digest(payload)
    validate(payload,now)
    DEFAULT.parent.mkdir(parents=True,exist_ok=True)
    temp=DEFAULT.with_suffix(".tmp")
    temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n")
    temp.replace(DEFAULT)
    return rows,{**status,"snapshot_sha256":payload["snapshot_sha256"]}

def validate(snapshot,now):
    if snapshot.get("schema")!="hunter_bybit_worker_v1" or snapshot.get("source")!=SOURCE:
        raise ValueError("BYBIT_WORKER_SNAPSHOT_SOURCE_INVALID")
    age=(now-dt.datetime.fromisoformat(snapshot["captured_at_utc"])).total_seconds()
    if not 0<=age<=900:raise ValueError("BYBIT_WORKER_SNAPSHOT_STALE_OR_FUTURE")
    if snapshot.get("snapshot_sha256")!=digest({k:v for k,v in snapshot.items() if k!="snapshot_sha256"}):
        raise ValueError("BYBIT_WORKER_SNAPSHOT_CHECKSUM_MISMATCH")
    bases=set()
    for row in snapshot["rows"]:
        b=row["base"]
        if b in bases or row["pair"]!=b+"USDT" or row["venue"]!="bybit":
            raise ValueError("BYBIT_WORKER_SNAPSHOT_ROWS_INVALID")
        bases.add(b)
        if not all(math.isfinite(float(row[k])) for k in ("price","volume_24h_usdt","change_24h_pct")) or row["price"]<=0 or row["volume_24h_usdt"]<0:
            raise ValueError("BYBIT_WORKER_SNAPSHOT_QUOTES_INVALID")
    status=snapshot["venue_status"]
    if not bases or status.get("valid_pairs")!=len(bases) or status.get("active_pairs")!=len(bases) or status.get("missing_or_invalid")!=[]:
        raise ValueError("BYBIT_WORKER_SNAPSHOT_COVERAGE_INVALID")
    for base,signal in snapshot.get("early_signals",{}).items():
        if base not in bases or signal.get("base")!=base or signal.get("pair")!=base+"USDT" or signal.get("source_venue")!="bybit" or signal.get("execution_supported") is not False or signal.get("stage") not in ("EARLY","WATCH"):
            raise ValueError("BYBIT_WORKER_SNAPSHOT_SIGNAL_INVALID")
    return snapshot["rows"],status
