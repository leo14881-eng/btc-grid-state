"""Real Linux child for pipe-barrier races and death after atomic link.

Only an in-memory HTTP fixture is used; no live request or production path.
"""
import json
import os
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research import hunter_replay_history_cache as cache
from hunter_replay_history_experiment import NOW, MONO, BOOT, Response, body_for, encoded


def revised(raw):
    rows = json.loads(raw)
    for row in rows:
        for column in (1, 2, 3, 4):
            row[column] = f"{float(row[column]) + 7:.4f}"
    return encoded(rows)


def main():
    mode, role, url = sys.argv[1:]
    cache._clock = lambda: (NOW, MONO, BOOT)
    real_link = cache.os.link

    def link(*args, **kwargs):
        real_link(*args, **kwargs)
        if mode == "crash":
            os._exit(73)  # No finally/cleanup: both names remain linked.
        if role == "first":
            print("LINKED", flush=True)
            assert sys.stdin.readline().strip() == "COMMIT"

    cache.os.link = link
    raw = body_for(url)
    if role == "second":
        raw = revised(raw)

    def fetch(request, timeout):
        assert timeout == 20
        if mode == "race":
            # Both children must miss and enter the original fetch before either
            # may publish. The first then holds the real directory flock at LINK.
            print("FETCH_READY", flush=True)
            assert sys.stdin.readline().strip() == "FETCH"
        return Response(raw, request.full_url)

    req = urllib.request.Request(url, headers={"User-Agent": "hunter-tail-risk-replay/1.0", "Accept": "application/json"})
    rows = cache.load_json(req, fetch)
    print("RESULT " + json.dumps({"rows": rows}), flush=True)


if __name__ == "__main__":
    main()
