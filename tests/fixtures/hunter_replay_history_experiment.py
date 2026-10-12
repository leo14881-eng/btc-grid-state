"""Deterministic offline transport and separate-process replay experiment.

Run: python tests/fixtures/hunter_replay_history_experiment.py
All prices are synthetic. This measures request reuse, not wall-clock speed.
"""
import contextlib
import datetime as dt
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.parse
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
NOW = 1791763200.0  # 2026-10-12 00:00 UTC, fixed for both implementations.
STEP = 300000


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


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


class FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(NOW, tz)


def run_stage(module, directory):
    requests, consumed = [], []
    original_get = module.get

    def fetch(request, timeout):
        assert timeout == 20
        raw = body_for(request.full_url)
        requests.append({"url": request.full_url, "raw_sha256": hashlib.sha256(raw).hexdigest()})
        return Response(raw, request.full_url)

    def get(url):
        rows = original_get(url)
        consumed.append({"url": url, "rows_sha256": digest(rows)})
        return rows

    def network_forbidden(*args, **kwargs):
        raise AssertionError("offline experiment attempted network")

    output = Path(directory) / "report.json"
    with (mock.patch("urllib.request.urlopen", side_effect=fetch),
          mock.patch("socket.socket.connect", network_forbidden),
          mock.patch("socket.getaddrinfo", network_forbidden),
          mock.patch("time.time", return_value=NOW),
          mock.patch.object(module.dt, "datetime", FixedDateTime),
          mock.patch.object(module, "get", side_effect=get),
          mock.patch.object(module, "OUT", output), contextlib.redirect_stdout(io.StringIO())):
        report = module.run()
    # These are the existing workflow replay contract guards, left in place.
    assert report["capital_authority"] == "NONE_SHADOW_ONLY"
    assert report["real_trading_enabled"] is False
    assert report["scope"] in {"TAIL_RISK_PATH_REPLAY_NOT_STRATEGY_BACKTEST", "LEADING_RISK_PARTIAL_PATH_REPLAY_NOT_STRATEGY_BACKTEST"}
    if report["schema"] == "hunter_tail_risk_replay_v1":
        assert any("cannot prove" in x for x in report["limitations"])
    return {"report": report, "consumed": consumed, "requests": requests}


def experiment():
    with tempfile.TemporaryDirectory(prefix="hunter-replay-offline-") as directory:
        root = Path(directory)
        cache = root / "cache"
        cache.mkdir(mode=0o700)
        env = dict(os.environ, HUNTER_REPLAY_CACHE_DIR=str(cache),
                   HUNTER_REPLAY_CACHE_GENERATION="offline-scan-generation",
                   HUNTER_REPLAY_CACHE_CYCLE="c" * 32,
                   HUNTER_REPLAY_CACHE_STARTED_AT=str(NOW - 1),
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
