"""V1 acceptance separates executable EARLY signals from venue research samples."""
def partition(early, universe):
    excluded=set((((universe.get("venue_status") or {}).get("binance") or {}).get("excluded_bstocks") or []))
    assert excluded, "CRYPTO_SCOPE_BSTOCK_CLASSIFICATION_MISSING"
    assert early.get("scan_generation_id") and early.get("scan_generation_id")==universe.get("generation_id"), "V1_EARLY_GENERATION_MISMATCH"
    executable=set(); research=set()
    recorded={x.get("base"):x for x in early.get("all_signals") or []}
    for signal in early.get("early") or []:
        base=signal.get("base")
        if not base or base in excluded:continue
        if signal.get("execution_supported") is False and signal.get("shadow_market_supported") is not True:
            venues=set(((universe.get("coins") or {}).get(base) or {}).get("venues") or [])
            assert signal.get("source_venue")=="bybit" and venues=={"bybit"}, "V1_UNSUPPORTED_SIGNAL_SCOPE_INVALID"
            assert recorded.get(base)==signal, "V1_EARLY_RESEARCH_SAMPLE_MISSING"
            research.add(base)
        else:executable.add(base)
    assert len(executable)+len(research)<=int(early.get("early_count") or 0), "V1_EARLY_COUNT_MISMATCH"
    return executable,research
