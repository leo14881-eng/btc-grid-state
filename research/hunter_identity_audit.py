#!/usr/bin/env python3
"""Fail-closed identity and asset-type audit of the current Hunter universe.

CoinGecko/DefiLlama ticker matches are leads, not verified token identities.
An analyst-attested contract must agree with an independently retrieved
third-party platform+contract before an identity is marked corroborated.
No network requests or trading authority in this stage.
"""
import datetime as dt
import json
import pathlib
import os
import re
import urllib.error
import urllib.parse
import importlib.util
_spec=importlib.util.spec_from_file_location(
    'hunter_api_cooldown',pathlib.Path(__file__).resolve().parent/'hunter_api_cooldown.py')
cooldown=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cooldown)
import urllib.request

ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"
MARKET=ROOT/"hunter-market-enrichment.json"
FACTS=pathlib.Path("research/hunter-verified-facts.json")
OUT=ROOT/"hunter-identity-audit.json"
CONTRACT_CACHE=ROOT/"hunter-contract-corroboration-cache.json"
CG=os.getenv("HUNTER_COINGECKO_API","https://api.coingecko.com/api/v3")

# Explicit review list, never suffix-only exclusion: PUMP, ARB, etc. are valid.
KNOWN_TOKENIZED_EQUITY={"AAPLB","MSFTB","MSTRB","NFLXB","NOKB","GOOGLB",
    "TSLAB","AMZNB","NVDAB"}
LEVERAGED_ROOTS={"BTC","ETH","BNB","XRP","SOL","DOGE","ADA","DOT",
    "LTC","LINK","AVAX","TRX"}
LEVERAGED_SUFFIXES=("UP","DOWN","BULL","BEAR")

def classify(symbol):
    if symbol in KNOWN_TOKENIZED_EQUITY:
        return "TOKENIZED_EQUITY_REVIEW"
    if any(symbol==root+suffix for root in LEVERAGED_ROOTS
           for suffix in LEVERAGED_SUFFIXES):
        return "LEVERAGED_TOKEN_REVIEW"
    return "SPOT_TOKEN_UNVERIFIED"

def source_candidates(rows):
    by={};ambiguous=set()
    for row in rows or []:
        if not isinstance(row,dict):continue
        symbol=str(row.get("symbol") or "").upper()
        if not symbol:continue
        if symbol in by:ambiguous.add(symbol)
        else:by[symbol]=row
    for symbol in ambiguous:by.pop(symbol,None)
    return by,ambiguous

def fetch_contract_platforms(coin_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,100}",coin_id):
        raise ValueError("INVALID_COINGECKO_ID")
    query=urllib.parse.urlencode({"localization":"false","tickers":"false",
        "market_data":"false","community_data":"false","developer_data":"false"})
    url=CG+"/coins/"+urllib.parse.quote(coin_id,safe="")+"?"+query
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-contract-audit/1.0",
                                             "Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=7) as response:
        data=json.load(response)
    if data.get("id")!=coin_id or not isinstance(data.get("platforms"),dict):
        raise ValueError("COINGECKO_ID_OR_PLATFORMS_INVALID")
    return {"coin_id":coin_id,"symbol":str(data.get("symbol") or "").upper(),
            "asset_platform_id":data.get("asset_platform_id"),
            "platforms":data["platforms"],
            "source_url":"https://www.coingecko.com/en/coins/"+coin_id}


def enrich_contracts(market,registry,cache,now,fetch=fetch_contract_platforms,limit=8):
    """Only look up an independently attested contract; cache source timestamps."""
    rows=[dict(x) for x in market.get("coingecko") or [] if isinstance(x,dict)]
    by,collisions=source_candidates(rows)
    assets=registry.get("assets") or {}
    cache=dict(cache or {})
    results={};failures={};fetched=0;retry_after=None
    for sym,fact in assets.items():
        if not fact.get("contract_verified") or sym in collisions:continue
        ident=fact.get("identity") or {}
        row=by.get(sym)
        declared_id=ident.get("coingecko_id")
        if row and declared_id and row.get("id")!=declared_id:
            failures[sym]="THIRD_PARTY_MARKET_FEED_ID_CONFLICT"
            continue
        if not row:
            if not declared_id:continue
            row={"symbol":sym,"id":declared_id}
            rows.append(row)
        coin_id=row.get("id")
        if not isinstance(coin_id,str):continue
        saved=cache.get(coin_id) or {}
        try:
            age=(now-dt.datetime.fromisoformat(saved["as_of_utc"])).total_seconds()/3600
        except (KeyError,ValueError,TypeError):age=float("inf")
        if age<0 or age>24:
            if fetched>=limit:
                failures[sym]="CONTRACT_LOOKUP_RATE_LIMIT_DEFERRED"
                continue
            fetched+=1
            try:
                fresh=fetch(coin_id)
                if fresh.get("coin_id")!=coin_id or fresh.get("symbol")!=sym:
                    raise ValueError("INDEPENDENT_COIN_ID_OR_SYMBOL_MISMATCH")
                saved={"as_of_utc":now.isoformat(),**fresh}
                cache[coin_id]=saved
            except urllib.error.HTTPError as exc:
                failures[sym]="HTTP_"+str(exc.code)+": "+str(exc)[:140]
                if exc.code==429:
                    retry_after=exc.headers.get("Retry-After") if exc.headers else None
                    failures["_source_rate_limit"]="COINGECKO_429__SHARED_COOLDOWN"
                    break
                continue
            except Exception as exc:
                failures[sym]=type(exc).__name__+": "+str(exc)[:140]
                # Never use stale cached contract corroboration as fresh.
                continue
        if saved.get("coin_id")!=coin_id or saved.get("symbol")!=sym:
            failures[sym]="STALE_CACHE_ID_OR_SYMBOL_MISMATCH"
            continue
        row["platforms"]=saved.get("platforms") or {}
        row["asset_platform_id"]=saved.get("asset_platform_id")
        row["contract_as_of_utc"]=saved.get("as_of_utc")
        results[sym]=saved.get("source_url")
    market=dict(market,coingecko=rows)
    return market,cache,{"fetched":fetched,"source_urls":results,"failures":failures,
                         "retry_after":retry_after}


def normalize_contract(value):
    return str(value or "").strip().lower()

def identity_status(sym,coin,fact,third_party,now):
    if not fact.get("contract_verified"):
        return "UNVERIFIED",["OFFICIAL_CONTRACT_ATTESTATION_MISSING"]
    ident=fact.get("identity") or {}
    pair=ident.get("exchange_pair")
    if pair not in {p.get("pair") for p in coin.get("pairs") or []}:
        return "BLOCKED",["OFFICIAL_EXCHANGE_PAIR_MISMATCH"]
    source=ident.get("official_contract_source")
    at=ident.get("verified_at_utc")
    if not isinstance(source,str) or not source.startswith("https://"):
        return "BLOCKED",["OFFICIAL_CONTRACT_SOURCE_MISSING"]
    try:
        age=(now-dt.datetime.fromisoformat(at.replace("Z","+00:00"))).total_seconds()/3600
        if age<0 or age>336:return "STALE",["OFFICIAL_CONTRACT_ATTESTATION_STALE"]
    except (ValueError,TypeError,AttributeError):
        return "BLOCKED",["OFFICIAL_CONTRACT_ATTESTATION_TIME_INVALID"]
    if ident.get("native_asset"):
        if not ident.get("native_chain") or not ident.get("coingecko_id"):
            return "BLOCKED",["NATIVE_CHAIN_OR_INDEPENDENT_ID_MISSING"]
        cg=third_party.get(sym) or {}
        if cg.get("id")!=ident["coingecko_id"] or cg.get("asset_platform_id") is not None:
            return "ANALYST_ATTESTED_ONLY",["INDEPENDENT_NATIVE_ASSET_ID_UNCORROBORATED"]
        try:
            age=(now-dt.datetime.fromisoformat(cg["contract_as_of_utc"])).total_seconds()/3600
            if age<0 or age>24:raise ValueError("stale")
        except (KeyError,TypeError,ValueError):
            return "ANALYST_ATTESTED_ONLY",["INDEPENDENT_NATIVE_ID_STALE"]
        return "THIRD_PARTY_NATIVE_CORROBORATED",[]
    chain=ident.get("platform")
    address=normalize_contract(ident.get("contract_address"))
    if not chain or not address:
        return "BLOCKED",["CHAIN_OR_CONTRACT_MISSING"]
    cg=third_party.get(sym) or {}
    platforms=cg.get("platforms") or {}
    external=normalize_contract(platforms.get(chain))
    if not external:
        return "ANALYST_ATTESTED_ONLY",["INDEPENDENT_PLATFORM_CONTRACT_UNAVAILABLE"]
    try:
        external_age=(now-dt.datetime.fromisoformat(cg["contract_as_of_utc"])).total_seconds()/3600
        if external_age<0 or external_age>24:
            return "ANALYST_ATTESTED_ONLY",["THIRD_PARTY_CONTRACT_EVIDENCE_STALE"]
    except (KeyError,ValueError,TypeError):
        return "ANALYST_ATTESTED_ONLY",["THIRD_PARTY_CONTRACT_TIMESTAMP_MISSING"]
    if external!=address:
        return "BLOCKED",["THIRD_PARTY_CONTRACT_MISMATCH"]
    return "THIRD_PARTY_CORROBORATED",[]

def build(scan,market,registry,now):
    coins=scan.get("coins") or {}
    if not scan.get("binance_complete") or not coins:
        raise ValueError("INCOMPLETE_BINANCE_SCAN")
    asof=dt.datetime.fromisoformat(scan["as_of_utc"].replace("Z","+00:00"))
    if (now-asof).total_seconds()<0 or (now-asof).total_seconds()>7200:
        raise ValueError("STALE_SCAN")
    cg,collisions=source_candidates(market.get("coingecko"))
    facts=registry.get("assets") or {}
    assets={};counts={}
    for sym,coin in coins.items():
        typ=classify(sym)
        fact=facts.get(sym) or {}
        status,blockers=identity_status(sym,coin,fact,cg,now)
        if sym in collisions:
            blockers.append("COINGECKO_TICKER_COLLISION")
            if status in ("THIRD_PARTY_CORROBORATED","THIRD_PARTY_NATIVE_CORROBORATED"):
                status="UNVERIFIED"
        if typ!="SPOT_TOKEN_UNVERIFIED":
            blockers.append("ASSET_TYPE_REQUIRES_INDEPENDENT_REVIEW")
        if len(coin.get("venues") or [])>1:
            blockers.append("CROSS_VENUE_CONTRACT_MAPPING_UNVERIFIED")
        assets[sym]={"asset_class":typ,"identity_status":status,
                     "coingecko_symbol_only_id":(cg.get(sym) or {}).get("id"),
                     "blockers":blockers,
                     "capital_identity_pass":status in ("THIRD_PARTY_CORROBORATED",
                                                         "THIRD_PARTY_NATIVE_CORROBORATED")
                     and typ=="SPOT_TOKEN_UNVERIFIED"
                     and "CROSS_VENUE_CONTRACT_MAPPING_UNVERIFIED" not in blockers}
        counts[status]=counts.get(status,0)+1
    return {"schema":"hunter_identity_audit_v1","as_of_utc":now.isoformat(),
            "scan_as_of_utc":scan["as_of_utc"],"universe_count":len(coins),
            "counts":counts,"ticker_collisions":sorted(collisions),
            "assets":assets,"capital_authority":"NONE__ANALYST_EVIDENCE_AND_EXECUTION_GATES_SEPARATE"}

def save_contract_cache(cache,path):
    """Persist strict JSON, not a literal backslash-n suffix."""
    path.write_text(json.dumps(cache,ensure_ascii=False,indent=2)+"\n")

def main():
    now=dt.datetime.now(dt.timezone.utc)
    market=json.loads(MARKET.read_text())
    registry=json.loads(FACTS.read_text())
    try:cache=json.loads(CONTRACT_CACHE.read_text())
    except (OSError,ValueError):cache={}
    state=cooldown.load()
    market,cache,lookups=enrich_contracts(
        market,registry,cache,now,
        limit=0 if cooldown.blocked(state,now) else 8)
    if "_source_rate_limit" in lookups["failures"]:
        state=cooldown.record_429(state,now,lookups.get("retry_after"),"identity")
        cooldown.save(state)
    lookups["shared_cooldown_active"]=cooldown.blocked(state,now)
    save_contract_cache(cache,CONTRACT_CACHE)
    report=build(json.loads(SCAN.read_text()),market,registry,now)
    report["third_party_contract_lookup"]=lookups
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"universe_count":report["universe_count"],
                      "identity_counts":report["counts"],
                      "ticker_collisions":report["ticker_collisions"]}))
    return 0

if __name__=="__main__":raise SystemExit(main())
