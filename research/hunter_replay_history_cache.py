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

SCHEMA = "hunter_replay_raw_v2"
TTL_SECONDS = 900
MAX_BODY = 256 * 1024
MAX_ENTRY = 360 * 1024
MAX_NAMESPACES = 64
MAX_FILES = 64
STEP_MS = 300000
ORIGINS = {"https://data-api.binance.vision", "https://api.binance.com"}
SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "SUIUSDT", "ENAUSDT"}
AGENTS = {"hunter-tail-risk-replay/1.0", "hunter-leading-replay/1.0"}
CONSUMERS = {"hunter-tail-risk-replay/1.0": "tail", "hunter-leading-replay/1.0": "leading"}


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


def _clock():
    # BOOTTIME is monotonic across processes and includes system suspend. The
    # boot ID prevents adopting the same counter value from another boot.
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
        boot = stream.read(80).strip()
    return time.time(), time.clock_gettime_ns(time.CLOCK_BOOTTIME), boot


def _live(identity, clock, fetched=None):
    now, mono, boot = clock
    if (boot != identity["boot_id"] or type(mono) is not int
            or not math.isfinite(now) or not 0 <= now - identity["started_at"] < TTL_SECONDS
            or not 0 <= mono - identity["started_monotonic_ns"] < TTL_SECONDS * 10**9):
        raise ValueError("cycle expired, future or different boot")
    if fetched is not None:
        wall, tick = fetched["fetched_at"], fetched["fetched_monotonic_ns"]
        if (type(wall) not in (int, float) or not math.isfinite(wall) or type(tick) is not int
                or not identity["started_at"] <= wall <= now or now - wall >= TTL_SECONDS
                or not identity["started_monotonic_ns"] <= tick <= mono
                or mono - tick >= TTL_SECONDS * 10**9):
            raise ValueError("invalid original fetch time")


def _identity(request, clock):
    now, _, boot = clock
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
    started_mono = int(os.environ.get("HUNTER_REPLAY_CACHE_STARTED_MONOTONIC_NS", "-1"))
    expected_boot = os.environ.get("HUNTER_REPLAY_CACHE_BOOT_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", generation) or not re.fullmatch(r"[0-9a-f]{32}", cycle):
        raise ValueError("explicit generation and unique cycle required")
    if (not math.isfinite(started) or started_mono < 0 or expected_boot != boot
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", expected_boot)):
        raise ValueError("explicit same-boot monotonic origin required")
    identity = {"schema": SCHEMA, "generation": generation, "cycle": cycle,
                "started_at": started, "started_monotonic_ns": started_mono,
                "boot_id": boot, "request": query}
    _live(identity, clock)
    return identity


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
        namespace = _hash(_encoded({k: v for k, v in identity.items() if k != "request"}))
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


def _read(fd, key, identity, clock):
    handle = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, "rb") as f:
        _private(f.fileno())
        if not 0 < os.fstat(f.fileno()).st_size <= MAX_ENTRY:
            raise ValueError("entry too large")
        raw = f.read(MAX_ENTRY + 1)
    if len(raw) > MAX_ENTRY:
        raise ValueError("entry grew")
    doc = json.loads(raw)
    if set(doc) != {"identity", "fetched_at", "fetched_monotonic_ns", "body_b64", "body_sha256", "record_sha256"}:
        raise ValueError("invalid envelope")
    checksum = doc.pop("record_sha256")
    if checksum != _hash(_encoded(doc)) or doc["identity"] != identity:
        raise ValueError("identity or envelope mismatch")
    _live(identity, clock, doc)
    body = base64.b64decode(doc["body_b64"], validate=True)
    if _hash(body) != doc["body_sha256"] or not _rows_valid(body, identity, doc["fetched_at"]):
        raise ValueError("corrupt or incomplete history")
    return json.loads(body), doc


def _write(fd, key, identity, fetched, body):
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Reserve BOTH temporary and destination names before creating either. The
    # two-name link/unlink publication interval must also stay within the cap.
    if len(os.listdir(fd)) + 2 > MAX_FILES:
        return False
    _live(identity, _clock(), fetched)
    doc = {"identity": identity, **fetched,
           "body_b64": base64.b64encode(body).decode("ascii"), "body_sha256": _hash(body)}
    doc["record_sha256"] = _hash(_encoded(doc))
    raw = _encoded(doc)
    if len(raw) > MAX_ENTRY:
        return False
    temp = ".partial-" + uuid.uuid4().hex
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        _live(identity, _clock(), fetched)
        # Atomic, no overwrite: concurrent successful writers keep the first
        # original response/time. A crash leaves only an ignored bounded temp.
        os.link(temp, key, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.unlink(temp, dir_fd=fd)
        os.fsync(fd)
        return True
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass


def _receipt(request, identity, source, mode, snapshot_published=False, receipt_id=None):
    """Separate consumer provenance event retained by the runner's stdout journal.

    This is a source receipt, not a trade/validation receipt or a fresh-fetch
    claim. Only allowlisted identities and hashes are logged, never raw bodies.
    An in-flight miss may consume a later revision if another writer won.
    """
    headers = {k.lower(): v for k, v in request.header_items()}
    event = {"schema": "hunter_replay_consumption_v1", "receipt_id": receipt_id or uuid.uuid4().hex,
             "invocation": identity["cycle"],
             "generation": identity["generation"], "request_id": _hash(_encoded(identity)),
             "request": identity["request"], "consumer": CONSUMERS[headers["user-agent"]],
             "selection": mode, "snapshot_published": snapshot_published,
             "snapshot_semantics": "FIRST_SUCCESSFULLY_PUBLISHED_VALIDATED_RESPONSE",
             "original_fetched_at": source["fetched_at"],
             "original_fetched_monotonic_ns": source["fetched_monotonic_ns"],
             "source_boot_id": identity["boot_id"], "raw_sha256": source["body_sha256"]}
    print("HUNTER_REPLAY_CONSUMPTION " + _encoded(event).decode(), flush=True)


def _return_valid(request, identity, source):
    current = _clock()
    if _identity(request, current) != identity:
        raise ValueError("invocation changed during lookup")
    _live(identity, current, source)


def load_json(request, opener, timeout=20):
    """First published response snapshot; exactly one original attempt per miss.

    A closed candle can be corrected upstream. Snapshot hits intentionally reuse
    the first published bytes, not a guarantee of equality to a second HTTP fetch.
    """
    fd = None
    identity = None
    if os.environ.get("HUNTER_REPLAY_CACHE") == "1":
        try:
            clock = _clock()
            identity = _identity(request, clock)
            fd = _directory(identity)
            key = _hash(_encoded(identity)) + ".json"
            receipt_id = None
            try:
                result, source = _read(fd, key, identity, clock)
                os.close(fd)
                fd = None
                # Disk read, hashing, decoding and row validation can take time.
                _return_valid(request, identity, source)
                receipt_id = uuid.uuid4().hex
                _receipt(request, identity, source, "snapshot_hit", receipt_id=receipt_id)
                # Even a slow journal sink may cross expiry. An invalidated
                # selection is explicitly cancelled, never claimed as consumed.
                _return_valid(request, identity, source)
            except Exception:
                if receipt_id is not None:
                    try:
                        print("HUNTER_REPLAY_SELECTION_INVALIDATED " + receipt_id, flush=True)
                    except Exception:
                        pass
            else:
                return result
        except Exception:
            pass
    try:
        with opener(request, timeout=timeout) as response:
            if identity is None:
                return json.load(response)
            body = response.read()
            result = json.loads(body)  # Original parse/errors still reach caller.
            published = False
            try:
                clock = _clock()
                source = {"fetched_at": clock[0], "fetched_monotonic_ns": clock[1],
                          "body_sha256": _hash(body)}
                if (fd is not None and response.status == 200 and _query(response.geturl()) == identity["request"]
                        and response.headers.get_content_type() == "application/json"
                        and not response.headers.get("Set-Cookie")
                        and _identity(request, clock) == identity
                        and _rows_valid(body, identity, clock[0])):
                    fetched = {k: source[k] for k in ("fetched_at", "fetched_monotonic_ns")}
                    published = _write(fd, key, identity, fetched, body)
            except Exception:
                pass
            try:
                _receipt(request, identity, source, "original_http", published)
            except Exception:
                pass  # Provenance I/O must not add an HTTP attempt/retry.
            return result
    finally:
        if fd is not None:
            os.close(fd)
