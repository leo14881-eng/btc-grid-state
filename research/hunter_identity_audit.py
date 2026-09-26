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

ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"
MARKET=ROOT/"hunter-market-enrichment.json"
FACTS=pathlib.Path("research/hunter-verified-facts.json")
OUT=ROOT/"hunter-identity-audit.json"

# Explicit review list, never suffix-only exclusion: PUMP, ARB, etc. are valid.
KNOWN_TOKENIZED_EQUITY={"AAPLB","MSFTB","MSTRB","NFLXB","NOKB","GOOGLB",
    "TSLAB","AMZNB","NVDAB","META B"}
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
        if not ident.get("native_chain"):
            return "BLOCKED",["NATIVE_CHAIN_MISSING"]
        # Native assets may lack token contracts, but still need independent
        # exchange-chain corroboration before an automated capital proposal.
        return "ANALYST_ATTESTED_ONLY",["INDEPENDENT_NATIVE_CHAIN_CORROBORATION_MISSING"]
    chain=ident.get("platform")
    address=normalize_contract(ident.get("contract_address"))
    if not chain or not address:
        return "BLOCKED",["CHAIN_OR_CONTRACT_MISSING"]
    cg=third_party.get(sym) or {}
    platforms=cg.get("platforms") or {}
    external=normalize_contract(platforms.get(chain))
    if not external:
        return "ANALYST_ATTESTED_ONLY",["INDEPENDENT_PLATFORM_CONTRACT_UNAVAILABLE"]
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
            if status=="THIRD_PARTY_CORROBORATED":
                status="UNVERIFIED"
        if typ!="SPOT_TOKEN_UNVERIFIED":
            blockers.append("ASSET_TYPE_REQUIRES_INDEPENDENT_REVIEW")
        if len(coin.get("venues") or [])>1:
            blockers.append("CROSS_VENUE_CONTRACT_MAPPING_UNVERIFIED")
        assets[sym]={"asset_class":typ,"identity_status":status,
                     "coingecko_symbol_only_id":(cg.get(sym) or {}).get("id"),
                     "blockers":blockers,
                     "capital_identity_pass":status=="THIRD_PARTY_CORROBORATED"
                     and typ=="SPOT_TOKEN_UNVERIFIED"
                     and "CROSS_VENUE_CONTRACT_MAPPING_UNVERIFIED" not in blockers}
        counts[status]=counts.get(status,0)+1
    return {"schema":"hunter_identity_audit_v1","as_of_utc":now.isoformat(),
            "scan_as_of_utc":scan["as_of_utc"],"universe_count":len(coins),
            "counts":counts,"ticker_collisions":sorted(collisions),
            "assets":assets,"capital_authority":"NONE__ANALYST_EVIDENCE_AND_EXECUTION_GATES_SEPARATE"}

def main():
    now=dt.datetime.now(dt.timezone.utc)
    report=build(json.loads(SCAN.read_text()),
                 json.loads(MARKET.read_text()),
                 json.loads(FACTS.read_text()),now)
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"universe_count":report["universe_count"],
                      "identity_counts":report["counts"],
                      "ticker_collisions":report["ticker_collisions"]}))
    return 0

if __name__=="__main__":raise SystemExit(main())
