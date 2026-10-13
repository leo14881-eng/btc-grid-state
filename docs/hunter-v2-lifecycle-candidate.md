# V2 lifecycle candidate review — 2026-10-06

## Decision

**NOT ENOUGH EVIDENCE TO REPLACE CURRENT V2.** This branch adds an offline
candidate, not a production switch. The scheduler, legacy engine, official
portfolios, ledger, V1 gates and live capital configuration are unchanged.
`hunter_v2_candidate.ACTIVE_ENGINE` remains `legacy`; candidate entry requires
`UNIT_TEST` or `BLIND_REPLAY`. There is no exchange order API in these modules.

## Implemented design

* `hunter_v2_execution.py`: immutable BUY tranche cash debit and effective
  quantity, full-size bid-depth VWAP liquidation, SELL fee and source freshness.
  VWAP already includes spread/depth impact: diagnostics are not deducted twice.
  Missing historical BUY evidence or quantity is UNKNOWN. Explicit legacy
  derivation is research-only and never execution-verified.
* `hunter_v2_protection.py`: one persistent main state, monotonic absolute USDT
  floor, independent deduplicated incidents, fresh qualified RUNNER signals and
  positive-net exit intent. Initialization never converts historical raw MFE to
  an executable peak. Negative gap-through creates an incident and pending exit,
  not a forced loss sale. Existing HARD_INVALIDATION stays a separate legacy
  responsibility; this candidate does not remove it.
* ADD preflight rejects full-position net PnL below the existing floor. An
  unverified external ADD retains state/peak/floor and enters migration review;
  its cost or changed denominator cannot trigger an immediate mathematical SELL.
  Floor percentages preserve their original cash basis for audit; absolute USDT
  is the operative protection strength. The controller removes caller-supplied
  preflight bypass flags. An eventual live adapter must bind a successful
  `check_add` receipt to the exact proposed tranche and execution snapshot.
* `hunter_v2_capital.py`: fixed 20K exposure, 17K ordinary band, 3K qualified
  reserve, independent market/systemic/circuit/concentration/marginal gates.
  Realized profits cannot enlarge 20K. Tail calibration produces a separate
  warning. Reserve requires complete fresh evidence, not score alone. Actual
  settled cash must be supplied; UNKNOWN cash cannot admit a trade. Existing
  market reduction still applies: default STRONG permits 19K, not necessarily
  all 20K. All new thresholds are candidate parameters, not approved policy.

## Replay and evidence boundaries

The report inventories current OPEN/CLOSED plus recoverable earlier position IDs
from Git history, preserving erased/failed assets. Split is chronological before
evaluation. Fixed BUY/ADD are selected only when their timestamps precede each
observation. It compares raw/net arming and giveback/capture hypotheses at
0/5/10/25/50 bps extra SELL friction. Closed candles are reference observations:
no assumption about high/low ordering and no executable historical fill claim.

The candidate state engine is actually exercised by an explicit ASSUMPTION_ONLY
adapter. Such states cannot obtain verified CLOSED receipts. True performance,
portfolio competition, freed-capital new BUY and Reserve replay remain
UNVERIFIABLE wherever contemporaneous book/research/identity/supply evidence is
missing. Null metrics are not zero results. No historical capital is released.

PR #34 was rechecked: draft, open, unmerged. Its ENA/PENDLE prices remain
COUNTERFACTUAL_REFERENCE_CLOSE_NOT_EXECUTED, never official SELL events or PnL.

Reproduce from the report's frozen commit and engine hash:

```bash
python -m unittest discover -s tests -p 'test_hunter*.py' -q
python research/hunter_v2_lifecycle_replay.py --repo . --ref origin/main \
  --minute-bars docs/replays/inputs/binance-history-20261006.json \
  --candidate-module research/hunter_v2_protection.py \
  --output /tmp/v2-replay.json
```

Use the frozen commit instead of `origin/main` to reproduce a historical report.
Freeze code/module and all input hashes before any further parameter selection.

## Engineering validation versus production acceptance

Tests cover state retention across reload/generation, unknown/stale data, ADD,
runner admission, incident deduplication, full-depth costs, hard exposure and
Reserve qualifications. In-memory transaction tests cover overlap, CAS conflict,
stale generation, crash before atomic replacement, read-back failure and retry.
They do **not** prove filesystem fsync, server restart, actual GitHub failure
injection or a scheduled candidate run. Existing transport regression tests run
alongside them; no new candidate is deployed to Vultr.

Before any approved activation: implement and test an exact tranche-bound ADD
preflight receipt, atomic SELL event + CLOSED + cash transition, durable execution
receipt deduplication, real Single Writer admission/CAS and verified origin/main
read-back. Then complete independent holdout/portfolio evidence and two real
scheduled cycles. Feature switch remains legacy until explicit approval.

Rollback to legacy on duplicate SELL, lost state, cap/reserve breach, generation
mix, CAS/read-back failure, ledger contamination or incorrect PnL. Never erase
valid incident records or rewrite historical transactions during rollback.
