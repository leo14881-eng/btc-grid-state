"""Opt-in, cycle-local raw responses for the two historical risk replays only.

No live consumers, retries, credentials, decisions or report timestamps live here.
Unsupported platforms/configuration/files always use the original HTTP path.
"""
import base64
import hashlib
import json
import math
import os
import re
import stat
import time
import urllib.parse
import uuid
if os.name == "posix":
    import fcntl

SCHEMA = "hunter_replay_raw_v1"
TTL_SECONDS = 900
MAX_BODY = 256 * 1024
MAX_ENTRY = 360 * 1024
MAX_NAMESPACES = 64
MAX_FILES = 64
STEP_MS = 300000
ORIGINS = {"https://data-api.binance.vision", "https://api.binance.com"}
SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "SUIUSDT", "ENAUSDT"}
AGENTS = {"hunter-tail-risk-replay/1.0", "hunter-leading-replay/1.0"}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _query(url):
    u = urllib.parse.urlsplit(url)
    origin = u.scheme + "://" + u.netloc
    if origin not in ORIGINS or u.path != "/api/v3/klines" or u.fragment:
        raise ValueError("not an approved public history endpoint")
    pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True, strict_parsing=True)
    q = dict(pairs)
    if len(q) != len(pairs) or set(q) != {"symbol", "interval", "startTime", "endTime", "limit"}:
        raise ValueError("ambiguous or unsupported parameters")
    if q["symbol"] not in SYMBOLS or q["interval"] != "5m" or q["limit"] != "1000":
        raise ValueError("not a replay request")
    for field in ("startTime", "endTime"):
        if not re.fullmatch(r"[1-9][0-9]{0,12}", q[field]):
            raise ValueError("invalid time")
    start, end = int(q["startTime"]), int(q["endTime"])
    if start % STEP_MS or end % STEP_MS or not 0 <= (end - start) // STEP_MS < 1000:
        raise ValueError("unaligned or paginated history")
    return {"method": "GET", "origin": origin, "market": "BINANCE_SPOT",
            "path": u.path, "query": q, "accept": "application/json"}


def _identity(request, now):
    if request.get_method() != "GET" or request.data is not None:
        raise ValueError("not public GET")
    headers = {k.lower(): v for k, v in request.header_items()}
    # These two fixed User-Agent labels identify callers; they do not select data.
    # Every other header/parameter is matched or rejected, including auth/cookies.
    if set(headers) != {"user-agent", "accept"} or headers["user-agent"] not in AGENTS or headers["accept"] != "application/json":
        raise ValueError("unsupported headers")
    query = _query(request.full_url)
    if int(query["query"]["endTime"]) + STEP_MS > now * 1000:
        raise ValueError("window is not completely closed")
    generation = os.environ.get("HUNTER_REPLAY_CACHE_GENERATION", "")
    cycle = os.environ.get("HUNTER_REPLAY_CACHE_CYCLE", "")
    started = float(os.environ.get("HUNTER_REPLAY_CACHE_STARTED_AT", "nan"))
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", generation) or not re.fullmatch(r"[0-9a-f]{32}", cycle):
        raise ValueError("explicit generation and unique cycle required")
    if not math.isfinite(started) or not 0 <= now - started < TTL_SECONDS:
        raise ValueError("cycle expired or future")
    return {"schema": SCHEMA, "generation": generation, "cycle": cycle,
            "started_at": started, "request": query}


def _rows_valid(raw, identity, fetched):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BODY:
        return False
    try:
        rows = json.loads(raw.decode("utf-8"))
        q = identity["request"]["query"]
        start, end = int(q["startTime"]), int(q["endTime"])
        # Binance endTime is inclusive on OPEN time: preserve the boundary row.
        if not isinstance(rows, list) or len(rows) != (end - start) // STEP_MS + 1 or len(rows) > 1000:
            return False
        for i, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != 12:
                return False
            opened = start + i * STEP_MS
            if type(row[0]) is not int or type(row[6]) is not int or row[0] != opened or row[6] != opened + STEP_MS - 1 or row[6] >= fetched * 1000:
                return False
            if type(row[8]) is not int or row[8] < 0 or row[11] != "0":
                return False
            values = []
            for j in (1, 2, 3, 4, 5, 7, 9, 10):
                if not isinstance(row[j], str) or len(row[j]) > 64:
                    return False
                value = float(row[j])
                if not math.isfinite(value) or value < 0:
                    return False
                values.append(value)
            op, hi, lo, close = values[:4]
            if not 0 < lo <= min(op, close) <= max(op, close) <= hi:
                return False
        return True
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return False


def _private(fd, directory=False):
    s = os.fstat(fd)
    kind = stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)
    if not kind or s.st_uid != os.getuid() or s.st_mode & 0o077 or (not directory and s.st_nlink != 1):
        raise ValueError("unsafe cache permissions or file type")


def _directory(identity):
    # Linux/POSIX dirfds pin each component. Never follow a symlink (including
    # parents), use a Windows reparse point, or resolve through a writable path.
    path = os.environ.get("HUNTER_REPLAY_CACHE_DIR", "")
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not path.startswith("/"):
        raise ValueError("safe directory operations unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = path.split("/")[1:]
    if not parts or any(x in ("", ".", "..") for x in parts):
        raise ValueError("absolute dedicated directory required")
    fd = os.open("/", flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        _private(fd, directory=True)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        namespace = _hash(_encoded({k: identity[k] for k in ("schema", "generation", "cycle", "started_at")}))
        names = os.listdir(fd)
        if namespace not in names:
            if len(names) >= MAX_NAMESPACES:
                raise ValueError("cache namespace budget reached")
            try:
                os.mkdir(namespace, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
        child = os.open(namespace, flags, dir_fd=fd)
        os.close(fd)
        fd = child
        _private(fd, directory=True)
        return fd
    except Exception:
        os.close(fd)
        raise


def _read(fd, key, identity, now):
    handle = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, "rb") as f:
        _private(f.fileno())
        if not 0 < os.fstat(f.fileno()).st_size <= MAX_ENTRY:
            raise ValueError("entry too large")
        raw = f.read(MAX_ENTRY + 1)
    if len(raw) > MAX_ENTRY:
        raise ValueError("entry grew")
    doc = json.loads(raw)
    if set(doc) != {"identity", "fetched_at", "body_b64", "body_sha256", "record_sha256"}:
        raise ValueError("invalid envelope")
    checksum = doc.pop("record_sha256")
    if checksum != _hash(_encoded(doc)) or doc["identity"] != identity:
        raise ValueError("identity or envelope mismatch")
    fetched = doc["fetched_at"]
    if type(fetched) not in (int, float) or not math.isfinite(fetched) or not identity["started_at"] <= fetched <= now or now - fetched >= TTL_SECONDS:
        raise ValueError("invalid original fetch time")
    body = base64.b64decode(doc["body_b64"], validate=True)
    if _hash(body) != doc["body_sha256"] or not _rows_valid(body, identity, fetched):
        raise ValueError("corrupt or incomplete history")
    return json.loads(body)


def _write(fd, key, identity, fetched, body):
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if len(os.listdir(fd)) >= MAX_FILES:
        return
    doc = {"identity": identity, "fetched_at": fetched,
           "body_b64": base64.b64encode(body).decode("ascii"), "body_sha256": _hash(body)}
    doc["record_sha256"] = _hash(_encoded(doc))
    raw = _encoded(doc)
    if len(raw) > MAX_ENTRY:
        return
    temp = ".partial-" + uuid.uuid4().hex
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        # Atomic, no overwrite: concurrent successful writers keep the first
        # original response/time. A crash leaves only an ignored bounded temp.
        os.link(temp, key, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.unlink(temp, dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass


def load_json(request, opener, timeout=20):
    """Exactly one original HTTP attempt on every miss; never change retries."""
    fd = None
    identity = None
    if os.environ.get("HUNTER_REPLAY_CACHE") == "1":
        try:
            now = time.time()
            identity = _identity(request, now)
            fd = _directory(identity)
            key = _hash(_encoded(identity)) + ".json"
            try:
                result = _read(fd, key, identity, now)
            except Exception:
                pass
            else:
                os.close(fd)
                return result
        except Exception:
            identity = None
    try:
        with opener(request, timeout=timeout) as response:
            if fd is None or identity is None:
                return json.load(response)
            body = response.read()
            result = json.loads(body)  # Original parse/errors still reach caller.
            fetched = time.time()
            try:
                if (response.status == 200 and _query(response.geturl()) == identity["request"]
                        and response.headers.get_content_type() == "application/json"
                        and not response.headers.get("Set-Cookie")
                        and identity["started_at"] <= fetched < identity["started_at"] + TTL_SECONDS
                        and _rows_valid(body, identity, fetched)):
                    _write(fd, key, identity, fetched, body)
            except Exception:
                pass
            return result
    finally:
        if fd is not None:
            os.close(fd)
