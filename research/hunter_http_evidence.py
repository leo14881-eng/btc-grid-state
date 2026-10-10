"""Bounded public HTTP failure evidence; raw bodies and arbitrary headers never persist."""
import json
import re
import datetime as dt
import time
import contextlib
import contextvars
import threading
import urllib.error
import urllib.request
import urllib.parse

LIMIT = 4096
SCHEMA = "hunter_http_evidence_v1"
BUILD = "hunter-bybit-httpdiag-v1"
HEADER_RULES = {
    "cf-ray": r"[0-9a-fA-F]{16,32}(?:-[A-Z]{3})?",
    "traceid": r"[0-9a-fA-F]{16,64}",
    "x-amz-cf-id": r"[A-Za-z0-9_=-]{16,128}",
    "retry-after": r"[0-9]{1,6}",
    "x-hunter-worker-build": re.escape(BUILD),
    "x-hunter-request-id": r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
}
MARKERS = {"ACCESS_DENIED_TEXT", "COUNTRY_BLOCK_TEXT", "RATE_LIMIT_TEXT"}
KEYS = {"schema", "layer", "http_status", "failure_kind", "headers", "body_kind",
        "body_bytes_sampled", "body_truncated", "body_read_failed", "body_markers", "ret_code"}
REQUEST_CONTEXT = contextvars.ContextVar("hunter_public_request_context", default={})
LOG_LOCK = threading.Lock()
MAX_OBSERVATION_MS = 120000
WALL_MONOTONIC_TOLERANCE_MS = 1000
OBSERVATION_HEADERS = {key: value for key, value in HEADER_RULES.items()
                       if not key.startswith("x-hunter-")}
OBSERVATION_HEADERS.update({"x-bapi-limit": r"[0-9]{1,10}",
    "x-bapi-limit-status": r"[0-9]{1,10}", "x-bapi-limit-reset-timestamp": r"[0-9]{1,16}"})
OBSERVATION_KEYS = {"schema", "layer", "job", "upstream_job_index", "symbol", "interval",
    "started_at_utc", "completed_at_utc", "duration_ms", "http_status", "ret_code",
    "worker_request_id", "worker_build", "headers", "root_cause"}


def emit_public_log(line):
    """Serialize a complete line, including newline/flush, across batch threads.

    A sink error is diagnostic loss only, never a market-data failure. The lock
    covers print's multiple stream writes, not just JSON serialization.
    """
    try:
        with LOG_LOCK:
            print(line, flush=True)
        return True
    except Exception:
        return False


@contextlib.contextmanager
def observation_context(generation_end_ms, batch_id=None):
    """Trusted collect context, scoped to one thread; not request/response content."""
    token = REQUEST_CONTEXT.set(dict(generation_end_ms=generation_end_ms, batch_id=batch_id))
    try:
        yield
    finally:
        REQUEST_CONTEXT.reset(token)


def log_generation_binding(generation_id, status):
    """Connect scan generation to the collect timestamp; never a request timer."""
    try:
        if (not isinstance(generation_id, str) or not re.fullmatch(r"\d{8}T\d{12}Z", generation_id)
                or not isinstance(status, dict) or status.get("source") != "OFFICIAL_BYBIT_V5_VIA_WORKER"
                or not re.fullmatch(r"[0-9a-f]{64}", status.get("snapshot_sha256", ""))):
            return
        captured = dt.datetime.fromisoformat(status["captured_at_utc"])
        emit_public_log("HUNTER_BYBIT_GENERATION_BINDING " + json.dumps(dict(
            scan_generation_id=generation_id, generation_end_ms=int(captured.timestamp()*1000),
            snapshot_sha256=status["snapshot_sha256"]), sort_keys=True, separators=(",", ":")))
    except Exception:
        pass


def consume_observations(body, headers, request):
    """Strip optional transport metadata before callers see authoritative data.

    A malformed/missing observation never changes market acceptance. Validate the
    complete bounded set and outer request-id binding before emitting any record.
    HTTP status is not a claim of valid candles or known root cause.
    """
    if not isinstance(body, dict) or "request_observations" not in body:
        return body
    body = dict(body)
    records = body.pop("request_observations")
    try:
        if not isinstance(records, list) or not 1 <= len(records) <= 40:
            raise ValueError("invalid observations")
        selector = request.selector
        if len(selector) > 2048:
            raise ValueError("invalid route")
        route = urllib.parse.urlsplit(selector)
        job = {"/bybit/spot": "spot", "/bybit/tickers": "tickers",
               "/bybit/early-klines": "early_klines"}.get(route.path)
        expected = [(None, None)]
        if job == "early_klines":
            if not isinstance(request.data, bytes) or len(request.data) > 2048:
                raise ValueError("invalid request scope")
            sent = json.loads(request.data)
            symbols = sent.get("symbols")
            if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 20
                    or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{2,30}", s) for s in symbols)
                    or len(set(symbols)) != len(symbols)):
                raise ValueError("invalid request scope")
            expected = [(symbol, interval) for symbol in symbols for interval in ("60", "15")]
        elif job in ("spot", "tickers"):
            symbol = urllib.parse.parse_qs(route.query).get("symbol", [None])[0]
            expected = [(symbol.upper().strip() if symbol is not None else None, None)]
        else:
            raise ValueError("invalid route")
        request_id = allowed_headers(headers).get("x-hunter-request-id")
        if request_id is None or len(records) != len(expected):
            raise ValueError("unbound observations")
        seen = set()
        for row in records:
            if not isinstance(row, dict) or set(row) != OBSERVATION_KEYS:
                raise ValueError("invalid shape")
            index = row["upstream_job_index"]
            if (type(index) is not int or not 0 <= index < len(expected) or index in seen
                    or (row["symbol"], row["interval"]) != expected[index]
                    or row["schema"] != "hunter_bybit_request_observation_v1"
                    or row["layer"] != "bybit_upstream" or row["job"] != job
                    or row["worker_request_id"] != request_id or row["worker_build"] != BUILD
                    or row["root_cause"] != "UNKNOWN"
                    or type(row["http_status"]) is not int
                    or not (row["http_status"] == 0 or 100 <= row["http_status"] <= 599)
                    or type(row["duration_ms"]) is not int or not 0 <= row["duration_ms"] <= MAX_OBSERVATION_MS
                    or (row["ret_code"] is not None and (type(row["ret_code"]) is not int or abs(row["ret_code"]) > 9999999999))):
                raise ValueError("invalid scope")
            seen.add(index)
            stamps = []
            for key in ("started_at_utc", "completed_at_utc"):
                stamp = row[key]
                if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", stamp):
                    raise ValueError("invalid timestamp")
                stamps.append(dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")))
            wall_ms = round((stamps[1] - stamps[0]).total_seconds()*1000)
            if (not 0 <= wall_ms <= MAX_OBSERVATION_MS
                    or abs(wall_ms-row["duration_ms"]) > WALL_MONOTONIC_TOLERANCE_MS):
                raise ValueError("inconsistent request timing")
            # Only early-klines has a Worker-generated envelope clock. The
            # public spot/ticker time belongs to Bybit, not the Worker clock.
            # Never infer cross-machine order from Vultr/generation wall time or
            # from the order in which concurrent observations reach stdout.
            envelope_ms = body.get("time")
            if (job == "early_klines" and type(envelope_ms) is int and
                    not envelope_ms-MAX_OBSERVATION_MS <= int(stamps[0].timestamp()*1000)
                    <= int(stamps[1].timestamp()*1000) <= envelope_ms+WALL_MONOTONIC_TOLERANCE_MS):
                raise ValueError("request timing outside worker envelope")
            safe = row["headers"]
            if (not isinstance(safe, dict) or set(safe) - set(OBSERVATION_HEADERS)
                    or any(not isinstance(value, str) or not re.fullmatch(OBSERVATION_HEADERS[key], value)
                           for key, value in safe.items())):
                raise ValueError("invalid headers")
        for row in records:
            emit_public_log("HUNTER_BYBIT_REQUEST_OBSERVATION " + json.dumps(
                dict(row, **REQUEST_CONTEXT.get()), sort_keys=True, separators=(",", ":")))
    except Exception:
        try:
            emit_public_log("HUNTER_BYBIT_OBSERVATION_DROPPED INVALID_OR_UNBOUND")
        except Exception:
            pass
    return body


def allowed_headers(headers):
    out = {}
    for key, pattern in HEADER_RULES.items():
        value = (headers or {}).get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            out[key] = value
    return out


def read_evidence(stream, headers, status, layer, failure_kind="http", request=None):
    sample = b""
    read_failed = False
    try:
        if stream is not None:
            sample = stream.read(LIMIT + 1)
    except Exception:
        read_failed = True
    truncated = len(sample) > LIMIT
    sample = sample[:LIMIT]
    text = sample.decode("utf-8", errors="replace")
    kind = "unavailable" if stream is None or read_failed else ("empty" if not sample else "text")
    ret_code = None
    upstream = None
    worker_error_code = None
    try:
        if not truncated and sample:
            body = json.loads(text)
            if request is not None:
                body = consume_observations(body, headers, request)
            kind = "json"
            if isinstance(body, dict) and type(body.get("retCode")) is int and abs(body["retCode"]) <= 9999999999:
                ret_code = body["retCode"]
            if layer == "worker_http" and isinstance(body, dict) and body.get("error") == "BYBIT_FETCH_FAILED":
                detail = body.get("detail")
                if isinstance(detail, str) and re.fullmatch(r"Error: BYBIT_HTTP_[1-5][0-9]{2}", detail):
                    worker_error_code = detail.removeprefix("Error: ")
            if layer == "worker_http" and isinstance(body, dict) and "diagnostics" in body:
                candidate = validate_evidence(body["diagnostics"])
                if candidate["layer"] == "bybit_upstream":
                    upstream = candidate
    except (ValueError, RecursionError):
        if re.search(r"<!doctype\s+html|<html(?:\s|>)", text, re.I):
            kind = "html"
    markers = []
    for marker, pattern in (("ACCESS_DENIED_TEXT", r"access denied"),
                            ("COUNTRY_BLOCK_TEXT", r"block access from your country"),
                            ("RATE_LIMIT_TEXT", r"too many requests|rate limit|access too frequent")):
        if re.search(pattern, text, re.I):
            markers.append(marker)
    evidence = dict(schema=SCHEMA, layer=layer, http_status=status, failure_kind=failure_kind,
                headers=allowed_headers(headers), body_kind=kind, body_bytes_sampled=len(sample),
                body_truncated=truncated, body_read_failed=read_failed,
                body_markers=markers, ret_code=ret_code)
    if upstream is not None:
        evidence["upstream"] = upstream
    if worker_error_code is not None:
        # A code reported by the Worker, not independently observed Bybit headers.
        evidence["worker_error_code"] = worker_error_code
    return evidence


def validate_evidence(value):
    if (not isinstance(value, dict) or set(value) - {"upstream", "worker_error_code"} != KEYS or value.get("schema") != SCHEMA
            or value.get("layer") not in ("worker_http", "bybit_upstream")
            or type(value.get("http_status")) is not int
            or not (value["http_status"] == 0 or 100 <= value["http_status"] <= 599)
            or value.get("failure_kind") not in ("http", "timeout", "transport", "invalid_json")
            or value.get("body_kind") not in ("json", "html", "text", "empty", "unavailable")
            or type(value.get("body_bytes_sampled")) is not int or not 0 <= value["body_bytes_sampled"] <= LIMIT
            or type(value.get("body_truncated")) is not bool or type(value.get("body_read_failed")) is not bool
            or not isinstance(value.get("headers"), dict) or allowed_headers(value["headers"]) != value["headers"]
            or not isinstance(value.get("body_markers"), list)
            or any(not isinstance(item, str) or item not in MARKERS for item in value["body_markers"])
            or len(value["body_markers"]) != len(set(value["body_markers"]))
            or (value.get("ret_code") is not None and
                (type(value["ret_code"]) is not int or abs(value["ret_code"]) > 9999999999))):
        raise ValueError("BYBIT_WORKER_HTTP_EVIDENCE_INVALID")
    if "worker_error_code" in value:
        if (value["layer"] != "worker_http" or not isinstance(value["worker_error_code"], str)
                or not re.fullmatch(r"BYBIT_HTTP_[1-5][0-9]{2}", value["worker_error_code"])):
            raise ValueError("BYBIT_WORKER_HTTP_EVIDENCE_INVALID")
    if "upstream" in value:
        nested = value["upstream"]
        if (value["layer"] != "worker_http" or not isinstance(nested, dict)
                or "upstream" in nested or nested.get("layer") != "bybit_upstream"):
            raise ValueError("BYBIT_WORKER_HTTP_EVIDENCE_INVALID")
        validate_evidence(nested)
        if "worker_error_code" in value and int(value["worker_error_code"].removeprefix("BYBIT_HTTP_")) != nested["http_status"]:
            raise ValueError("BYBIT_WORKER_HTTP_EVIDENCE_INVALID")
        for key in ("x-hunter-worker-build", "x-hunter-request-id"):
            if key in value["headers"] and nested["headers"].get(key) != value["headers"][key]:
                raise ValueError("BYBIT_WORKER_HTTP_EVIDENCE_INVALID")
    return value


class ObservedHTTPError(urllib.error.HTTPError):
    def __init__(self, code, evidence):
        self.diagnostic_code = code
        self.http_evidence = validate_evidence(evidence)
        super().__init__(None, evidence["http_status"], code, None, None)

    def __str__(self):
        # Existing outer discovery error reporting retains this safe, bounded JSON.
        return self.diagnostic_code + ":" + json.dumps(self.http_evidence, separators=(",", ":"), sort_keys=True)


def request_json(request, timeout):
    """One public request; preserve caller timeout, no retries or cache behavior."""
    started = dt.datetime.now(dt.timezone.utc)
    monotonic = time.monotonic()
    observation = {}
    try:
        return _request_json(request, timeout, observation)
    finally:
        # Outer Worker requests only: never mistake HTTP 200 for complete
        # upstream coverage. No raw body, URL, request headers or credentials.
        try:
            job = {"/bybit/spot": "spot", "/bybit/tickers": "tickers",
                   "/bybit/early-klines": "early_klines"}.get(request.selector.split("?", 1)[0])
            if job is not None:
                safe = allowed_headers(observation.get("headers"))
                for key, pattern in (("x-bapi-limit", r"[0-9]{1,10}"),
                                     ("x-bapi-limit-status", r"[0-9]{1,10}"),
                                     ("x-bapi-limit-reset-timestamp", r"[0-9]{1,16}")):
                    value = (observation.get("headers") or {}).get(key)
                    if isinstance(value, str) and re.fullmatch(pattern, value):
                        safe[key] = value
                emit_public_log("HUNTER_BYBIT_REQUEST_OBSERVATION " + json.dumps(dict(
                    schema="hunter_bybit_request_observation_v1", layer="worker_http", job=job,
                    started_at_utc=started.isoformat(),
                    completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                    duration_ms=max(0, round((time.monotonic()-monotonic)*1000)),
                    http_status=observation.get("status", 0), ret_code=observation.get("ret_code"),
                    worker_request_id=safe.get("x-hunter-request-id"), headers=safe,
                    root_cause="UNKNOWN", **REQUEST_CONTEXT.get()), sort_keys=True, separators=(",", ":")))
        except Exception:
            pass  # Logging must not change successful values or failure propagation.


def _request_json(request, timeout, observation):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            observation.update(status=getattr(response, "status", 0), headers=getattr(response, "headers", None))
            try:
                body = consume_observations(json.load(response), response.headers, request)
                code = body.get("retCode") if isinstance(body, dict) else None
                if type(code) is int and abs(code) <= 9999999999:
                    observation["ret_code"] = code
                return body
            except (ValueError, UnicodeError):
                raise ObservedHTTPError("BYBIT_WORKER_NON_JSON",
                    read_evidence(None, response.headers, response.status, "worker_http", "invalid_json")) from None
    except ObservedHTTPError:
        raise
    except urllib.error.HTTPError as exc:
        observation.update(status=exc.code, headers=exc.headers)
        try:
            evidence = read_evidence(exc, exc.headers, exc.code, "worker_http", request=request)
            observation["ret_code"] = evidence["ret_code"]
        finally:
            try:
                exc.close()
            except Exception:
                pass
        raise ObservedHTTPError("BYBIT_HTTP_" + str(exc.code), evidence) from None
    except (TimeoutError, urllib.error.URLError) as exc:
        timeout_error = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
        raise ObservedHTTPError("WORKER_TIMEOUT" if timeout_error else "WORKER_TRANSPORT_ERROR",
            read_evidence(None, None, 0, "worker_http", "timeout" if timeout_error else "transport")) from None
