#!/usr/bin/env python3
"""Turn repeated Hunter research gaps into a bounded, persistent completion queue.

This worker does not invent facts. It records attempts and terminal evidence states so
candidates cannot remain RESEARCH_INCOMPLETE forever merely because a source does not
publish a requested fact.
"""
import datetime as dt, json, pathlib
ROOT=pathlib.Path("research/results")
DOS=ROOT/"hunter-candidate-dossiers.json"
OUT=ROOT/"hunter-research-completion.json"
MAX_ATTEMPTS=6
TERMINAL_PREFIX=("No evidence-grounded bear/base/bull terminal valuation",
                 "Verified fact missing: bear_market_cap_usd",
                 "Verified fact missing: base_market_cap_usd",
                 "Verified fact missing: bull_market_cap_usd")
def read(p,d):
    try:return json.loads(p.read_text())
    except (OSError,ValueError):return d
def main():
    now=dt.datetime.now(dt.timezone.utc).isoformat()
    dos=read(DOS,{})
    old=read(OUT,{})
    prev=old.get("assets") or {}
    assets={}
    for d in dos.get("dossiers") or []:
        sym=d["asset"]; p=prev.get(sym) or {}; attempts=int(p.get("attempts",0))+1
        questions=list(dict.fromkeys(d.get("research_questions") or []))
        # Market-cap scenarios are analyst assumptions, not discoverable facts. They must
        # never keep a discovery candidate in an infinite evidence loop.
        nondiscoverable=[q for q in questions if q.startswith(TERMINAL_PREFIX)]
        discoverable=[q for q in questions if q not in nondiscoverable]
        terminal=attempts>=MAX_ATTEMPTS
        assets[sym]={"attempts":attempts,"last_attempt_utc":now,
          "discoverable_open_questions":discoverable,
          "non_discoverable_assumptions":nondiscoverable,
          "completion_state":("EVIDENCE_EXHAUSTED__DECIDE_WITH_KNOWN_UNKNOWNS" if terminal
                              else "EVIDENCE_COLLECTION_ACTIVE"),
          "terminal":terminal}
    report={"schema":"hunter_research_completion_v1","as_of_utc":now,
            "max_attempts_before_terminal":MAX_ATTEMPTS,"assets":assets}
    OUT.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
    print(json.dumps({"assets":len(assets),"terminal":sum(x["terminal"] for x in assets.values())}))
if __name__=="__main__":main()
