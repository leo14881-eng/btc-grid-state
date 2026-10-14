"""Deterministic offline transport and separate-process replay experiment.

Run: python tests/fixtures/hunter_replay_history_experiment.py
All prices are synthetic. This measures request reuse, not wall-clock speed.
"""
import contextlib
import datetime as dt
from email.message import Message
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
NOW = 1791763200.0  # 2026-10-12 00:00 UTC, fixed for both implementations.
MONO = 1000000000000
BOOT = "11111111-2222-4333-8444-555555555555"
STEP = 300000


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def receipts_from(journal):
    invalid = {line.split(" ", 1)[1] for line in journal.splitlines()
               if line.startswith("HUNTER_REPLAY_SELECTION_INVALIDATED ")}
    receipts = [json.loads(line.split(" ", 1)[1]) for line in journal.splitlines()
                if line.startswith("HUNTER_REPLAY_CONSUMPTION ")]
    return [row for row in receipts if row["receipt_id"] not in invalid]


def body_for(url):
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    start, end = int(q["startTime"]), int(q["endTime"])
    seed = sum(map(ord, q["symbol"]))
    rows = []
    for i, t in enumerate(range(start, end + 1, STEP)):
        # Recovery, broad shock and retrace exercise both detector paths.
        shock = min(max(i - 65, 0), 35) * (0.65 if q["symbol"] == "BTCUSDT" else 1.05)
        recovery = min(max(i - 110, 0), 90) * 0.32
        p = 100 + seed / 100 + (i % 7) * 0.03 - shock + recovery
        price = f"{p:.4f}"
        rows.append([t, price, f"{p + 1:.4f}", f"{p - 1:.4f}", price,
                     "10.0000", t + STEP - 1, "1000.0000", 10, "5.0000", "500.0000", "0"])
    return encoded(rows)


class Response(io.BytesIO):
    def __init__(self, body, url, status=200, content_type="application/json"):
        super().__init__(body)
        self.status, self.url = status, url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def geturl(self):
        return self.url


@contextlib.contextmanager
def stdlib_transport(requests, *handlers):
    """Real urllib + HTTP preprocessing; replace only the HTTPS connection.

    No DNS, socket or TLS traffic: HTTPResponse parses an in-memory wire response.
    Keep this below urlopen so automatic Request mutations are exercised.
    """
    class MemorySocket:
        def __init__(self, host):
            self.host, self.sent = host, bytearray()

        def sendall(self, data):
            self.sent.extend(data)

        def makefile(self, mode):
            assert mode == "rb"
            method, target, version = bytes(self.sent).split(b"\r\n", 1)[0].decode("ascii").split()
            assert method == "GET" and version == "HTTP/1.1"
            url = "https://" + self.host + target
            raw = body_for(url)
            requests.append({"url": url, "raw_sha256": hashlib.sha256(raw).hexdigest()})
            return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                              + str(len(raw)).encode() + b"\r\nConnection: close\r\n\r\n" + raw)

        def close(self):
            pass

    def connect(connection):
        assert connection.timeout == 20
        connection.sock = MemorySocket(connection.host)

    def network_forbidden(*args, **kwargs):
        raise AssertionError("offline experiment attempted network")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)
    with (mock.patch("urllib.request._opener", opener),
          mock.patch.object(http.client.HTTPSConnection, "connect", connect),
          mock.patch("socket.socket.connect", network_forbidden),
          mock.patch("socket.getaddrinfo", network_forbidden)):
        yield opener


class FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(NOW, tz)


def run_stage(module, directory):
    from research import hunter_replay_history_cache as history_cache
    requests, consumed = [], []
    journal = io.StringIO()
    original_get = module.get

    def get(url):
        rows = original_get(url)
        consumed.append({"url": url, "rows_sha256": digest(rows)})
        return rows

    output = Path(directory) / "report.json"
    with (stdlib_transport(requests),
          mock.patch("time.time", return_value=NOW),
          mock.patch.object(history_cache, "_clock", return_value=(NOW, MONO, BOOT)),
          mock.patch.object(module.dt, "datetime", FixedDateTime),
          mock.patch.object(module, "get", side_effect=get),
          mock.patch.object(module, "OUT", output), contextlib.redirect_stdout(journal)):
        report = module.run()
    # These are the existing workflow replay contract guards, left in place.
    assert report["capital_authority"] == "NONE_SHADOW_ONLY"
    assert report["real_trading_enabled"] is False
    assert report["scope"] in {"TAIL_RISK_PATH_REPLAY_NOT_STRATEGY_BACKTEST", "LEADING_RISK_PARTIAL_PATH_REPLAY_NOT_STRATEGY_BACKTEST"}
    if report["schema"] == "hunter_tail_risk_replay_v1":
        assert any("cannot prove" in x for x in report["limitations"])
    result = {"report": report, "consumed": consumed, "requests": requests}
    receipts = receipts_from(journal.getvalue())
    if receipts:
        result["source_receipts"] = receipts
    return result


def experiment():
    with tempfile.TemporaryDirectory(prefix="hunter-replay-offline-") as directory:
        root = Path(directory)
        cache = root / "cache"
        cache.mkdir(mode=0o700)
        env = dict(os.environ, HUNTER_REPLAY_CACHE_DIR=str(cache),
                   HUNTER_REPLAY_CACHE_GENERATION="offline-scan-generation",
                   HUNTER_REPLAY_CACHE_CYCLE="c" * 32,
                   HUNTER_REPLAY_CACHE_STARTED_AT=str(NOW - 1),
                   HUNTER_REPLAY_CACHE_STARTED_MONOTONIC_NS=str(MONO - 10**9),
                   HUNTER_REPLAY_CACHE_BOOT_ID=BOOT,
                   HUNTER_BINANCE_API="https://data-api.binance.vision",
                   PYTHONDONTWRITEBYTECODE="1")
        data = {}
        for mode in ("0", "1"):
            env["HUNTER_REPLAY_CACHE"] = mode
            data[mode] = {}
            for stage in ("tail", "leading"):
                command = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), "--stage", stage, directory]
                process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=True)
                data[mode][stage] = json.loads(process.stdout)
        return data


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--stage":
        if sys.argv[2] == "tail":
            from research import hunter_tail_risk_replay as module
        else:
            from research import hunter_leading_risk_replay as module
        print(json.dumps(run_stage(module, sys.argv[3]), sort_keys=True))
    else:
        print(json.dumps(experiment(), sort_keys=True, indent=2))
