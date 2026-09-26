#!/usr/bin/env python3
"""Fresh official Bybit snapshot from an explicitly configured regional runner.

Never route blocked GitHub US traffic through a proxy. Run this collector only
on infrastructure authorized to reach Bybit from its actual egress country.
The checksum detects accidental file corruption, NOT source authentication:
repository write access and the trusted collector runner are the trust boundary.
"""
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import urllib.request

DEFAULT=pathlib.Path("research/results/hunter-bybit-regional-snapshot.json")
SOURCE="OFFICIAL_BYBIT_V5_AUTHORIZED_REGIONAL_RUNNER"
MAX_AGE_SECONDS=45*60

def checksum(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),
                                      ensure_ascii=False).encode()).hexdigest()

def validate(snapshot,now,max_age_seconds=MAX_AGE_SECONDS):
    if not isinstance(snapshot,dict) or snapshot.get("source")!=SOURCE:
        raise ValueError("BYBIT_REGIONAL_SOURCE_UNTRUSTED")
    if snapshot.get("schema")!="hunter_bybit_regional_v1":
        raise ValueError("BYBIT_REGIONAL_SCHEMA_INVALID")
    if not isinstance(snapshot.get("captured_at_utc"),str):
        raise ValueError("BYBIT_REGIONAL_TIMESTAMP_MISSING")
    at=dt.datetime.fromisoformat(snapshot["captured_at_utc"].replace("Z","+00:00"))
    if at.tzinfo is None:raise ValueError("BYBIT_REGIONAL_TIMESTAMP_NO_TZ")
    age=(now-at).total_seconds()
    if age<0 or age>max_age_seconds:
        raise ValueError("BYBIT_REGIONAL_SNAPSHOT_STALE_OR_FUTURE")
    body={k:v for k,v in snapshot.items() if k!="snapshot_sha256"}
    if snapshot.get("snapshot_sha256")!=checksum(body):
        raise ValueError("BYBIT_REGIONAL_SNAPSHOT_CHECKSUM_MISMATCH")
    status=snapshot.get("venue_status")
    rows=snapshot.get("rows")
    if not isinstance(status,dict) or not isinstance(rows,list) or not rows:
        raise ValueError("BYBIT_REGIONAL_MISSING_COVERAGE")
    if (status.get("missing_or_invalid")!=[] or
        status.get("active_pairs")!=len(rows) or
        status.get("valid_pairs")!=len(rows)):
        raise ValueError("BYBIT_REGIONAL_INCOMPLETE_COVERAGE")
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or row.get("venue")!="bybit":
            raise ValueError("BYBIT_REGIONAL_WRONG_VENUE")
        base=row.get("base");pair=row.get("pair")
        if not isinstance(base,str) or not base or pair!=base+"USDT" or pair in seen:
            raise ValueError("BYBIT_REGIONAL_INVALID_OR_DUPLICATE_PAIR")
        seen.add(pair)
        for k in ("price","volume_24h_usdt","change_24h_pct"):
            v=row.get(k)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):
                raise ValueError("BYBIT_REGIONAL_INVALID_MARKET_VALUE")
        if row["price"]<=0 or row["volume_24h_usdt"]<0:
            raise ValueError("BYBIT_REGIONAL_INVALID_PRICE_OR_VOLUME")
    return rows,{**status,"source":SOURCE,
                 "captured_at_utc":snapshot["captured_at_utc"],
                 "egress_country":snapshot.get("egress_country")}

def load(path=DEFAULT,now=None):
    if now is None:now=dt.datetime.now(dt.timezone.utc)
    data=json.loads(pathlib.Path(path).read_text())
    return validate(data,now)

def egress_country():
    req=urllib.request.Request("https://www.cloudflare.com/cdn-cgi/trace",
                               headers={"User-Agent":"hunter-regional-collector/1.0"})
    with urllib.request.urlopen(req,timeout=10) as r:
        fields=dict(line.split("=",1) for line in r.read(2048).decode().splitlines()
                    if "=" in line)
    return fields.get("loc")

def collect(now=None,fetcher=None,country=None):
    if now is None:now=dt.datetime.now(dt.timezone.utc)
    if country is None:country=egress_country()
    expected=os.getenv("HUNTER_BYBIT_RUNNER_COUNTRY","").upper()
    if not expected or country!=expected or country in ("US","CN"):
        raise RuntimeError("BYBIT_RUNNER_COUNTRY_NOT_CONFIRMED_OR_BLOCKED")
    if fetcher is None:
        import hunter_cex_scan
        fetcher=hunter_cex_scan.bybit_with_fallback
    rows,status=fetcher()
    payload={"schema":"hunter_bybit_regional_v1","source":SOURCE,
             "captured_at_utc":now.isoformat(),"egress_country":country,
             "rows":rows,"venue_status":status}
    payload["snapshot_sha256"]=checksum(payload)
    validate(payload,now)
    return payload

def main():
    snapshot=collect()
    DEFAULT.parent.mkdir(parents=True,exist_ok=True)
    tmp=DEFAULT.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot,indent=2,ensure_ascii=False)+"\n")
    tmp.replace(DEFAULT)
    print(json.dumps({"regional_bybit_complete":True,
                      "pairs":len(snapshot["rows"]),
                      "egress_country":snapshot["egress_country"],
                      "captured_at_utc":snapshot["captured_at_utc"]}))
    return 0

if __name__=="__main__":raise SystemExit(main())
