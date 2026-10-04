import json, os, time, urllib.parse, urllib.request, urllib.error

BASE = "https://data.alpaca.markets/v2/stocks/bars"

def fetch(symbols, start="2026-09-01T00:00:00Z", end="2026-10-03T23:59:59Z", feed="iex"):
    headers = {
        "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
        "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"],
        "User-Agent": "stock-shadow-alpaca-diagnostic/1.0",
    }
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": start,
        "end": end,
        "limit": 10000,
        "feed": feed,
        "adjustment": "all",
    }
    url = BASE + "?" + urllib.parse.urlencode(params)
    t = time.time()
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.load(resp)
            bars = body.get("bars", {})
            return {
                "status": resp.status,
                "feed": feed,
                "requested": len(symbols),
                "returned_symbols": len(bars),
                "bars": sum(len(v) for v in bars.values()),
                "next_page": bool(body.get("next_page_token")),
                "seconds": round(time.time()-t, 3),
                "sample_fields": sorted(next(iter(bars.values()))[0].keys()) if bars and next(iter(bars.values())) else [],
            }
    except urllib.error.HTTPError as e:
        return {"status": e.code, "feed": feed, "reason": str(e.reason), "body": e.read().decode("utf-8","replace")[:500]}

def main():
    if not os.getenv("APCA_API_KEY_ID") or not os.getenv("APCA_API_SECRET_KEY"):
        raise SystemExit("missing Alpaca secrets")
    groups = [
        ["AAPL"],
        ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","JPM","XOM","UNH"],
    ]
    for g in groups:
        print(json.dumps(fetch(g), sort_keys=True))
    print(json.dumps(fetch(groups[-1], feed="sip"), sort_keys=True))
if __name__ == "__main__":
    main()
