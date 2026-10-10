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
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from research import hunter_bybit_availability as availability
    from research.hunter_bybit_signal_capture import capture
    from research.hunter_http_evidence import ObservedHTTPError, emit_public_log, observation_context, request_json, validate_evidence
except ModuleNotFoundError:
    import hunter_bybit_availability as availability
    from hunter_bybit_signal_capture import capture
    from hunter_http_evidence import ObservedHTTPError, emit_public_log, observation_context, request_json, validate_evidence

DEFAULT = pathlib.Path("research/results/hunter-bybit-worker-snapshot.json")
SOURCE = "OFFICIAL_BYBIT_V5_VIA_WORKER"
INTERVALS = {"60", "15"}
SAFE_WORKER_ERROR = re.compile(r"BYBIT_HTTP_[1-5][0-9]{2}|BYBIT_RET_-?[0-9]{1,10}|BYBIT_STALE_TIME|INCOMPLETE_KLINE")
LOCAL_ERRORS = {
    "BYBIT_WORKER_BATCH_SCOPE_MISMATCH", "BYBIT_WORKER_BATCH_INTERVAL_MISMATCH",
    "BYBIT_WORKER_BATCH_SUCCESS_FAILURE_CONFLICT", "BYBIT_WORKER_FAILURE_SHAPE_INVALID",
    "BYBIT_WORKER_UPSTREAM_ERROR", "BYBIT_WORKER_WRONG_CATEGORY",
    "BYBIT_WORKER_KLINE_WRONG_SYMBOL", "BYBIT_WORKER_KLINE_INSUFFICIENT",
    "BYBIT_WORKER_KLINE_STALE_OR_GAPPED", "BYBIT_WORKER_KLINE_INVALID",
    "BYBIT_WORKER_KLINE_INVALID_OHLC", "WORKER_MISSING_KLINE_REASON",
    "WORKER_TIMEOUT", "WORKER_ABORTED", "WORKER_KLINE_FAILURE",
}


def exception_detail(exc):
    if isinstance(exc, ObservedHTTPError):
        # Persist only legacy codes/flags; richer classification belongs in logs.
        detail = (dict(code=exc.diagnostic_code, detail_redacted=True, detail_truncated=False)
                  if exc.diagnostic_code in LOCAL_ERRORS else failure_detail(exc.diagnostic_code))
        return dict(detail, http_evidence=exc.http_evidence)
    if isinstance(exc, urllib.error.HTTPError):
        return failure_detail("BYBIT_HTTP_" + str(exc.code))
    if isinstance(exc, TimeoutError) or (isinstance(exc, urllib.error.URLError)
                                       and isinstance(exc.reason, TimeoutError)):
        return dict(code="WORKER_TIMEOUT", detail_redacted=True, detail_truncated=False)
    raw = str(exc)
    if raw in LOCAL_ERRORS:
        return dict(code=raw, detail_redacted=False, detail_truncated=False)
    return failure_detail(raw or "UNKNOWN")


def contextual(detail, batch_id, symbol, interval, end, stage):
    return dict(detail, batch_id=batch_id, symbol=symbol, interval=interval,
                generation_end_ms=end, stage=stage)


def failure_detail(raw):
    """Keep bounded public error codes, never arbitrary upstream text or URLs."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("BYBIT_WORKER_FAILURE_SHAPE_INVALID")
    text = raw.strip()
    code = text.removeprefix("Error: ")
    known = len(text) <= 160 and SAFE_WORKER_ERROR.fullmatch(code) is not None
    return {"code": code if known else ("WORKER_ABORTED" if text.startswith("AbortError:") else "WORKER_KLINE_FAILURE"),
            "detail_redacted": not known, "detail_truncated": len(text) > 160}


def batch_parts(data, batch, end):
    klines, failures = data.get("klines", {}), data.get("failures", {})
    if (data.get("end") != end or not isinstance(klines, dict) or not isinstance(failures, dict)
            or (set(klines) | set(failures)) - set(batch)):
        raise ValueError("BYBIT_WORKER_BATCH_SCOPE_MISMATCH")
    for entries in (klines, failures):
        if any(not isinstance(values, dict) or not values or set(values) - INTERVALS
               for values in entries.values()):
            raise ValueError("BYBIT_WORKER_BATCH_INTERVAL_MISMATCH")
    details = {}
    for symbol, errors in failures.items():
        if set(errors) & set(klines.get(symbol, {})):
            raise ValueError("BYBIT_WORKER_BATCH_SUCCESS_FAILURE_CONFLICT")
        details[symbol] = {interval: failure_detail(raw) for interval, raw in errors.items()}
    diagnostics = data.get("failure_diagnostics", {})
    if not isinstance(diagnostics, dict) or set(diagnostics) - set(failures):
        raise ValueError("BYBIT_WORKER_BATCH_SCOPE_MISMATCH")
    for symbol, values in diagnostics.items():
        if not isinstance(values, dict) or not values or set(values) - set(failures[symbol]):
            raise ValueError("BYBIT_WORKER_BATCH_INTERVAL_MISMATCH")
        for interval, evidence in values.items():
            evidence = validate_evidence(evidence)
            if evidence["layer"] != "bybit_upstream":
                raise ValueError("BYBIT_WORKER_HTTP_EVIDENCE_INVALID")
            code = details[symbol][interval]["code"]
            if code.startswith("BYBIT_HTTP_") and evidence["http_status"] != int(code.removeprefix("BYBIT_HTTP_")):
                raise ValueError("BYBIT_WORKER_HTTP_EVIDENCE_INVALID")
            details[symbol][interval]["http_evidence"] = evidence
    return klines, details

def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(availability.SPOT_PROXY + path, data=data, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json", "Content-Type": "application/json"})
    return request_json(req, timeout=60)

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

def collect(binance_bases=(), now=None, fetcher=request, listing_fetcher=None,
            listing_refresher=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    end = int(now.timestamp() * 1000)
    listing_fetcher = listing_fetcher or availability.spot_proxy_universe
    listing_refresher = listing_refresher or (lambda: availability.spot_proxy_universe(now, force_refresh=True))
    bases, cache = listing_fetcher()
    try:
        from research.hunter_cex_scan import valid
    except ModuleNotFoundError:
        from hunter_cex_scan import valid
    bases = [base for base in bases if valid(base)]
    refresh_evidence=None
    for ticker_attempt in range(2):
        with observation_context(end):
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
        if not missing:
            break
        if ticker_attempt or cache.get('cache_hit') is not True:
            break
        # A 24h cached Trading list can outlive a delisting. Never subtract the
        # missing symbols from that list: refresh the entire official universe,
        # then re-fetch and validate ALL tickers, including newly listed assets.
        original=set(bases)
        refreshed, refreshed_cache=listing_refresher()
        age=(now-dt.datetime.fromisoformat(refreshed_cache['captured_at_utc'])).total_seconds()
        if (refreshed_cache.get('cache_hit') is not False
                or refreshed_cache.get('network_requests')!=1
                or refreshed_cache.get('complete') is not True or not 0<=age<=120):
            raise ValueError('BYBIT_WORKER_LISTING_REFRESH_NOT_FRESH_FULL_LIST')
        bases=[base for base in refreshed if valid(base)]
        refresh_evidence=dict(reason='CACHED_LIST_TICKER_CONTRADICTION',
            original_missing_or_invalid=missing,
            removed_bases=sorted(original-set(bases)),added_bases=sorted(set(bases)-original),
            captured_at_utc=refreshed_cache['captured_at_utc'])
        cache=refreshed_cache
    if missing:
        raise ValueError("BYBIT_WORKER_TICKERS_INCOMPLETE:"+",".join(missing[:10]))
    required=[r for r in rows if r["base"] not in set(binance_bases) and r["base"]!="BTC"]
    symbols=sorted({r["pair"] for r in required}|{"BTCUSDT"})
    batches=[symbols[i:i+20] for i in range(0,len(symbols),20)]
    bundles={}; failure_details={}
    def fetch_batch(index, batch):
        with observation_context(end, index):
            return fetcher("/bybit/early-klines", {"symbols":batch,"end":end})
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs={executor.submit(fetch_batch,index,batch):(index,batch)
              for index,batch in enumerate(batches)}
        for job in as_completed(jobs):
            batch_id,batch=jobs[job]
            stage="batch_request"
            try:
                response=job.result()
                stage="batch_validation"
                if isinstance(response,dict) and type(response.get("retCode")) is int and response["retCode"] != 0:
                    raise ValueError("BYBIT_RET_"+str(response["retCode"]))
                data=result(response)
                klines, details = batch_parts(data, batch, end)
            except Exception as exc:
                detail=exception_detail(exc)
                for symbol in batch:
                    failure_details[symbol]={i:contextual(detail,batch_id,symbol,i,end,stage) for i in INTERVALS}
                continue
            # Validate every interval before feature extraction (which short-circuits).
            # No missing or contradictory response can become a usable candle.
            for symbol in batch:
                for interval,limit in (("60",5),("15",25)):
                    detail=details.get(symbol,{}).get(interval)
                    stage="worker_response"
                    if detail is None:
                        stage="candle_validation"
                        try:
                            if interval not in klines.get(symbol,{}):
                                raise ValueError("WORKER_MISSING_KLINE_REASON")
                            body=klines[symbol][interval]
                            if isinstance(body,dict) and type(body.get("retCode")) is int and body["retCode"] != 0:
                                raise ValueError("BYBIT_RET_"+str(body["retCode"]))
                            bundles.setdefault(symbol,{})[interval]=candles(body,symbol,interval,limit,end)
                        except Exception as exc:
                            detail=exception_detail(exc)
                    if detail is not None:
                        failure_details.setdefault(symbol,{})[interval]=contextual(detail,batch_id,symbol,interval,end,stage)
    # Diagnostic evidence is deliberately excluded from the authoritative schema.
    # Existing stdout reaches the systemd journal / Actions log, not a new writer.
    for symbol, intervals in sorted(failure_details.items()):
        for interval, detail in sorted(intervals.items()):
            evidence = detail.pop("http_evidence", None)
            if evidence is not None:
                emit_public_log("HUNTER_BYBIT_HTTP_EVIDENCE " + json.dumps(dict(
                    schema="hunter_http_log_v1", source=SOURCE,
                    captured_at_utc=now.isoformat(), **detail,
                    http_evidence=validate_evidence(evidence)), sort_keys=True, separators=(",", ":")))
    def get(symbol,interval,limit):
        detail = failure_details.get(symbol, {}).get(interval)
        if detail:
            raise ValueError("BYBIT_WORKER_KLINE_FAILURE:"+symbol+":"+interval+":"+detail["code"])
        return bundles[symbol][interval]
    try:
        signals, failures=capture(required,get)
    except RuntimeError as exc:
        signals={}; failures={s:str(exc) for s in symbols}
    signals={b:{**s,"source_observed_at_utc":now.isoformat()} for b,s in signals.items()}
    status=dict(active_pairs=len(bases),valid_pairs=len(rows),missing_or_invalid=[],
                source=SOURCE,captured_at_utc=now.isoformat(),signal_pairs=len(signals),
                signal_failures=len(failures),signal_complete=not failures and len(signals)==len(required),
                signal_expected_bases=sorted(r["base"] for r in required),
                listing_cache=cache, ticker_requests=ticker_attempt+1,kline_batch_requests=len(batches))
    if refresh_evidence is not None:
        status['listing_refresh']=refresh_evidence
    payload=dict(schema="hunter_bybit_worker_v1",source=SOURCE,captured_at_utc=now.isoformat(),
                 rows=rows,venue_status=status,early_signals=signals,signal_failures=failures)
    if failure_details:
        payload["signal_failure_details"] = failure_details
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
    if "signal_failure_details" in snapshot:
        details = snapshot["signal_failure_details"]
        expected = status.get("signal_expected_bases")
        if (not isinstance(details, dict) or not isinstance(expected, list)
                or any(not isinstance(base, str) or base not in bases or base == "BTC" for base in expected)):
            raise ValueError("BYBIT_WORKER_SNAPSHOT_FAILURE_SCOPE_INVALID")
        requested = {base+"USDT" for base in expected} | {"BTCUSDT"}
        failures = snapshot.get("signal_failures", {})
        signals = snapshot.get("early_signals", {})
        if (set(details) - requested or not isinstance(failures, dict)
                or any(symbol not in failures for symbol in details)
                or (details and status.get("signal_complete") is not False)
                or ("BTCUSDT" in details and signals)
                or any(symbol[:-4] in signals for symbol in details)):
            raise ValueError("BYBIT_WORKER_SNAPSHOT_FAILURE_SIGNAL_CONFLICT")
        ordered=sorted(requested)
        generation_end=int(dt.datetime.fromisoformat(snapshot["captured_at_utc"]).timestamp()*1000)
        for symbol,values in details.items():
            if not isinstance(values, dict) or not values or set(values) - INTERVALS:
                raise ValueError("BYBIT_WORKER_SNAPSHOT_FAILURE_INTERVAL_INVALID")
            for interval,detail in values.items():
                if (not isinstance(detail, dict) or set(detail) != {"code", "detail_redacted", "detail_truncated",
                        "batch_id", "symbol", "interval", "generation_end_ms", "stage"}
                        or not isinstance(detail["code"], str)
                        or not (SAFE_WORKER_ERROR.fullmatch(detail["code"]) or detail["code"] in LOCAL_ERRORS)
                        or type(detail["batch_id"]) is not int or detail["batch_id"] != ordered.index(symbol)//20
                        or detail["symbol"] != symbol or detail["interval"] != interval
                        or type(detail["generation_end_ms"]) is not int or detail["generation_end_ms"] != generation_end
                        or detail["stage"] not in ("batch_request","batch_validation","worker_response","candle_validation")
                        or type(detail["detail_redacted"]) is not bool or type(detail["detail_truncated"]) is not bool):
                    raise ValueError("BYBIT_WORKER_SNAPSHOT_FAILURE_DETAIL_INVALID")
    return snapshot["rows"],status
