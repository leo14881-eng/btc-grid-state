"""Bounded public HTTP failure evidence; raw bodies and arbitrary headers never persist."""
import json
import re
import urllib.error
import urllib.request

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


def allowed_headers(headers):
    out = {}
    for key, pattern in HEADER_RULES.items():
        value = (headers or {}).get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            out[key] = value
    return out


def read_evidence(stream, headers, status, layer, failure_kind="http"):
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
                            ("RATE_LIMIT_TEXT", r"too many requests|rate limit")):
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            try:
                return json.load(response)
            except (ValueError, UnicodeError):
                raise ObservedHTTPError("BYBIT_WORKER_NON_JSON",
                    read_evidence(None, response.headers, response.status, "worker_http", "invalid_json")) from None
    except ObservedHTTPError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            evidence = read_evidence(exc, exc.headers, exc.code, "worker_http")
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
