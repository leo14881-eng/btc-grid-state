#!/usr/bin/env python3
"""Collect primary-source URL leads for current early and continuation candidates.

CoinGecko URLs are discovery hints, NOT verified official evidence or capital
authority. Contract, unlock and value-capture claims still require dated
independent primary-source review. Rotate requests to avoid rate-limit stalls.
"""
import datetime as dt
import json
import importlib.util
import os
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request

ROOT=pathlib.Path("research/results")
DOSSIERS=ROOT/"hunter-candidate-dossiers.json"
CACHE=ROOT/"hunter-primary-source-cache.json"
OUT=ROOT/"hunter-primary-source-leads.json"
CG=os.getenv("HUNTER_COINGECKO_API","https://api.coingecko.com/api/v3")
_spec=importlib.util.spec_from_file_location(
    "hunter_api_cooldown",pathlib.Path(__file__).resolve().parent/"hunter_api_cooldown.py")
cooldown=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cooldown)
BUDGET=6

def safe_url(url):
    if not isinstance(url,str):return None
    p=urllib.parse.urlparse(url.strip())
    if p.scheme!="https" or not p.hostname:return None
    host=p.hostname.lower()
    if host in {"localhost","127.0.0.1","0.0.0.0"} or host.endswith(".local"):
        return None
    return url.strip()

def clean_urls(urls,max_n=5):
    return list(dict.fromkeys(u for u in (safe_url(x) for x in (urls or [])) if u))[:max_n]

def fetch(coin_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,100}",coin_id):
        raise ValueError("INVALID_COINGECKO_ID")
    query=urllib.parse.urlencode({"localization":"false","tickers":"false",
        "market_data":"false","community_data":"false","developer_data":"false"})
    url=CG+"/coins/"+urllib.parse.quote(coin_id,safe="")+"?"+query
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-primary-source-discovery/1.0",
                                             "Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=7) as response:
        return json.load(response)

def lanes(dossiers):
    by={d["asset"]:d for d in dossiers.get("dossiers") or []}
    early=[x for x in dossiers.get("early_entry_watchlist") or [] if x in by]
    cont=[x for x in dossiers.get("continuation_watchlist") or [] if x in by]
    other=[x for x in by if x not in early and x not in cont]
    order=[]
    for i in range(max(len(early),len(cont))):
        if i<len(early):order.append(early[i])
        if i<len(cont):order.append(cont[i])
    order.extend(other)
    return [by[s] for s in order]

def extract(data,expected_sym,expected_id,now):
    if data.get("id")!=expected_id or str(data.get("symbol") or "").upper()!=expected_sym:
        raise ValueError("THIRD_PARTY_COIN_ID_OR_SYMBOL_MISMATCH")
    links=data.get("links") or {}
    repos=links.get("repos_url") or {}
    out={"asset":expected_sym,"coingecko_id":expected_id,
         "discovered_at_utc":now.isoformat(),
         "homepage_candidates":clean_urls(links.get("homepage")),
         "whitepaper_candidates":clean_urls([links.get("whitepaper")]),
         "explorer_candidates":clean_urls(links.get("blockchain_site")),
         "github_candidates":clean_urls(repos.get("github")),
         "platform_contracts_unverified":data.get("platforms") or {},
         "source_url":"https://www.coingecko.com/en/coins/"+expected_id,
         "evidence_status":"UNVERIFIED_LINK_DISCOVERY_ONLY__NOT_OFFICIAL_ATTESTATION"}
    return out

def build(dossiers,cache,now,fetcher=fetch,budget=BUDGET):
    order=lanes(dossiers)
    old=cache.get("assets") or {}
    updated=dict(old);failures={};fetched=0;retry_after=None
    eligible=[d for d in order if isinstance(
        (d.get("nonprice_observations") or {}).get("coingecko_id"),str)]
    if not eligible:
        return {"schema":"hunter_primary_source_leads_v1",
                "as_of_utc":now.isoformat(),"dossier_as_of_utc":dossiers["as_of_utc"],
                "target_count":0,"fresh_fetched":0,"cache_reused":0,
                "failures":{},"leads":{},
                "capital_authority":"NONE__UNVERIFIED_LINKS"},dict(cache)
    cursor=int(cache.get("cursor",0))%len(eligible)
    selected=[eligible[(cursor+i)%len(eligible)] for i in range(len(eligible))]
    cache_reused=0;new_cursor=cursor
    for d in selected:
        sym=d["asset"];coin_id=d["nonprice_observations"]["coingecko_id"]
        new_cursor=(new_cursor+1)%len(eligible)
        previous=updated.get(sym) or {}
        try:
            age=(now-dt.datetime.fromisoformat(previous["discovered_at_utc"])).total_seconds()/3600
        except (KeyError,ValueError,TypeError):age=float("inf")
        if previous.get("coingecko_id")==coin_id and 0<=age<168:
            cache_reused+=1
            continue
        if fetched>=budget:break
        fetched+=1
        try:
            updated[sym]=extract(fetcher(coin_id),sym,coin_id,now)
        except urllib.error.HTTPError as exc:
            failures[sym]="HTTP_"+str(exc.code)+": "+str(exc)[:120]
            if exc.code==429:
                retry_after=exc.headers.get("Retry-After") if exc.headers else None
                failures["_source_rate_limit"]="COINGECKO_429__SHARED_COOLDOWN"
                break
        except Exception as exc:
            failures[sym]=type(exc).__name__+": "+str(exc)[:150]
    # Only expose currently selected dossier symbols; never mistake cached
    # links for fresh contract attestations.
    active={d["asset"] for d in order}
    leads={sym:entry for sym,entry in updated.items() if sym in active}
    result={"schema":"hunter_primary_source_leads_v1",
            "as_of_utc":now.isoformat(),"dossier_as_of_utc":dossiers["as_of_utc"],
            "target_count":len(eligible),"fresh_fetched":fetched,
            "cache_reused":cache_reused,"failures":failures,"leads":leads,
            "rate_limit_retry_after":retry_after,
            "capital_authority":"NONE__UNVERIFIED_LINKS"}
    return result,{"cursor":new_cursor,"assets":updated}

def main():
    dossiers=json.loads(DOSSIERS.read_text())
    try:cache=json.loads(CACHE.read_text())
    except (OSError,ValueError):cache={}
    now=dt.datetime.now(dt.timezone.utc)
    state=cooldown.load()
    result,updated=build(dossiers,cache,now,
                         budget=0 if cooldown.blocked(state,now) else BUDGET)
    if "_source_rate_limit" in result["failures"]:
        state=cooldown.record_429(state,now,result.get("rate_limit_retry_after"),"source_discovery")
        cooldown.save(state)
    result["shared_cooldown_active"]=cooldown.blocked(state,now)
    result["shared_cooldown_until_utc"]=state.get("blocked_until_utc") if result["shared_cooldown_active"] else None
    CACHE.write_text(json.dumps(updated,indent=2,ensure_ascii=False)+"\n")
    OUT.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n")
    print(json.dumps({k:result[k] for k in
        ("target_count","fresh_fetched","cache_reused","failures")},ensure_ascii=False))
    return 0

if __name__=="__main__":raise SystemExit(main())
