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

def fetch_coin_registry():
    """One bulk request: all active CoinGecko IDs + platform/contract mappings."""
    url=CG+"/coins/list?"+urllib.parse.urlencode({"include_platform":"true","status":"active"})
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-identity-registry/2.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=20) as response:
        data=json.load(response)
    if not isinstance(data,list) or not data:
        raise ValueError("COINGECKO_BULK_REGISTRY_INVALID")
    rows=[]
    for x in data:
        if not isinstance(x,dict): continue
        cid=x.get("id"); sym=str(x.get("symbol") or "").upper()
        if not cid or not sym: continue
        rows.append({"id":cid,"symbol":sym,"name":x.get("name"),
                     "platforms":x.get("platforms") or {},
                     "contract_as_of_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
                     "source_url":"https://www.coingecko.com/en/coins/"+cid})
    return rows

def enrich_bulk_registry(market,cache,now,fetch=fetch_coin_registry,universe_symbols=None):
    """Refresh CoinGecko identity registry in one request; reuse fresh cache on 429/outage."""
    cache=dict(cache or {})
    try:
        age=(now-dt.datetime.fromisoformat(cache.get("_bulk_as_of_utc",""))).total_seconds()/3600
    except Exception: age=float("inf")
    rows=cache.get("_bulk_rows") or []
    meta={"mode":"BULK_COINS_LIST_INCLUDE_PLATFORM","network_requests":0,"cache_used":False,"error":None}
    if rows and 0 <= age <= 24:
        meta["cache_used"]=True
    else:
        try:
            rows=fetch(); meta["network_requests"]=1
            cache["_bulk_as_of_utc"]=now.isoformat(); cache["_bulk_rows"]=rows
        except urllib.error.HTTPError as exc:
            meta["error"]="HTTP_"+str(exc.code)
            if not rows: raise
            meta["cache_used"]=True
        except Exception as exc:
            meta["error"]=type(exc).__name__+":"+str(exc)[:120]
            if not rows: raise
            meta["cache_used"]=True
    # Build identity coverage from the ALL-active CoinGecko registry, not only
    # the market-cap paginated /coins/markets subset.  /coins/markets remains
    # useful for market/supply observations, but must never define the identity
    # universe.  Only unique ticker matches are auto-added; collisions remain
    # fail-closed for contract/project disambiguation.
    bulk_by_id={str(x.get("id")):x for x in rows if isinstance(x,dict) and x.get("id")}
    bulk_by_symbol,bulk_collisions=source_candidates(rows)
    leads=[]; seen=set()
    for lead in market.get("coingecko") or []:
        if not isinstance(lead,dict): continue
        item=dict(lead); sym=str(item.get("symbol") or "").upper()
        reg=bulk_by_id.get(str(item.get("id")))
        if sym in bulk_collisions:
            # A duplicate ticker in the global registry is not by itself a
            # contradiction when the current market feed already resolved a
            # concrete CoinGecko ID. Keep the collision as audit metadata;
            # only unresolved symbol-only matches must fail closed.
            item["_bulk_symbol_collision"]=True
            item["_bulk_symbol_collision_present"]=True
        if reg and str(reg.get("symbol") or "").upper()==sym:
            item["platforms"]=reg.get("platforms") or {}
            item["contract_as_of_utc"]=cache.get("_bulk_as_of_utc") or now.isoformat()
            item["source_url"]=reg.get("source_url")
        leads.append(item); seen.add(sym)
    # Add unique registry-only identities for Binance symbols absent from the
    # paginated market feed.  Deliberately do not copy market-cap/supply fields:
    # this stage establishes identity only.
    for sym,reg in bulk_by_symbol.items():
        if universe_symbols is not None and sym not in universe_symbols: continue
        if sym in seen: continue
        leads.append({"id":reg.get("id"),"symbol":sym,"name":reg.get("name"),
                      "platforms":reg.get("platforms") or {},
                      "contract_as_of_utc":cache.get("_bulk_as_of_utc") or now.isoformat(),
                      "source_url":reg.get("source_url"),
                      "identity_source":"COINGECKO_ALL_ACTIVE_REGISTRY"})
    meta["registry_rows"]=len(rows)
    meta["registry_symbol_collisions"]=len(bulk_collisions)
    meta["matched_market_leads"]=sum(1 for x in leads if x.get("contract_as_of_utc"))
    meta["registry_only_identity_leads"]=sum(1 for x in leads if x.get("identity_source")=="COINGECKO_ALL_ACTIVE_REGISTRY")
    return dict(market,coingecko=leads),cache,meta

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
    # New symbols must not require a pre-populated manual registry.
    # Market enrichment supplies leads; verified facts tighten the mapping.
    symbols=sorted(set(by)|set(assets))
    for sym in symbols:
        if sym in collisions:continue
        fact=assets.get(sym) or {}
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
        # Automated lane: Binance executable symbol + a unique current CoinGecko
        # ID is sufficient to establish a third-party identity lead. Explicit
        # ticker collisions are removed by source_candidates() and never pass.
        cg=third_party.get(sym) or {}
        try:
            age=(now-dt.datetime.fromisoformat(cg["contract_as_of_utc"])).total_seconds()/3600
        except Exception:
            age=float("inf")
        if cg.get("id") and 0 <= age <= 24:
            return "THIRD_PARTY_UNIQUE_ID_CORROBORATED",[]
        return "UNVERIFIED",["THIRD_PARTY_IDENTITY_UNAVAILABLE"]
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
        if sym in collisions or (cg.get(sym) or {}).get("_bulk_symbol_collision"):
            blockers.append("COINGECKO_TICKER_COLLISION")
            if status in ("THIRD_PARTY_CORROBORATED","THIRD_PARTY_NATIVE_CORROBORATED","THIRD_PARTY_UNIQUE_ID_CORROBORATED"):
                status="UNVERIFIED"
        if typ!="SPOT_TOKEN_UNVERIFIED":
            blockers.append("ASSET_TYPE_REQUIRES_INDEPENDENT_REVIEW")
        if len(coin.get("venues") or [])>1:
            blockers.append("CROSS_VENUE_CONTRACT_MAPPING_UNVERIFIED")
        assets[sym]={"asset_class":typ,"identity_status":status,
                     "contract_evidence":{
                         "contract_verified":fact.get("contract_verified") is True,
                         "official":dict(fact.get("identity") or {}),
                         "independent":{k:(cg.get(sym) or {}).get(k) for k in
                             ("id","platforms","asset_platform_id","contract_as_of_utc","source_url")}},
                     "coingecko_symbol_only_id":(cg.get(sym) or {}).get("id"),
                     "blockers":blockers,
                     "capital_identity_pass":status in ("THIRD_PARTY_CORROBORATED",
                                                         "THIRD_PARTY_NATIVE_CORROBORATED",
                                                         "THIRD_PARTY_UNIQUE_ID_CORROBORATED")
                     and typ=="SPOT_TOKEN_UNVERIFIED"
                     and "CROSS_VENUE_CONTRACT_MAPPING_UNVERIFIED" not in blockers}
        counts[status]=counts.get(status,0)+1
    return {"schema":"hunter_identity_audit_v1","as_of_utc":now.isoformat(),
            "scan_generation_id":scan.get("generation_id"),
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
    # Primary path is one bulk CoinGecko request, not N per-asset calls.
    try:
        scan=json.loads(SCAN.read_text())
        market,cache,bulk=enrich_bulk_registry(market,cache,now,universe_symbols=set((scan.get("coins") or {}).keys()))
    except urllib.error.HTTPError as exc:
        if exc.code==429:
            state=cooldown.record_429(state,now,exc.headers.get("Retry-After") if exc.headers else None,"identity_bulk")
            cooldown.save(state)
        raise
    save_contract_cache(cache,CONTRACT_CACHE)
    report=build(json.loads(SCAN.read_text()),market,registry,now)
    report["identity_registry_sync"]=bulk
    report["third_party_contract_lookup"]={"mode":"EXCEPTION_ONLY","fetched":0,"failures":{}}
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"universe_count":report["universe_count"],
                      "identity_counts":report["counts"],
                      "ticker_collisions":report["ticker_collisions"]}))
    return 0

if __name__=="__main__":raise SystemExit(main())
