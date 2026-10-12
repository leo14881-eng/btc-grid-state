# Monitor decision clock reliability audit

Baseline: `44327057a565e86faa6eb62465504e34c6fc0b6d`, read from GitHub main
on 2026-10-12. Independent shallow checkout and branch
`codex/monitor-reliability-20261012`. No AGENTS.md or repository skill file was
present in the pinned tree. Existing boundary, lock acceptance and CI instructions
were reviewed before editing.

## Reproduction and narrow fix

Monitor previously sampled one evaluation clock before V1 and reused it for V2.
V1 management, historical requests, serialization and summaries all run before
V2. Evidence freshness in V2 therefore understated elapsed time. The regression
advances a fake clock by 20 seconds during V1, using a source book already 590
seconds old and the real liquidation freshness validator. Before the fix V2
receives V1's clock; after the fix the existing 600-second validator returns
UNKNOWN for the now 610-second-old book. This is synthetic boundary evidence,
not a claim of a historical incorrect SELL.

Lane-start sampling alone was insufficient: a 20-second portfolio-load or
manager delay could still turn a 590-second source into a stale SELL; V1 ADD
could likewise consume evidence aged 3610 seconds against its existing
3600-second limit. The real Monitor/manager deterministic fixtures reproduce
those outcomes with a frozen clock and reject them with the injected live clock.

Monitor now injects a current UTC clock after loading and into the shared
`hunter_shadow_trader_v2.manage_existing_positions` manager. It samples per
holding, after decision/capacity/identity work, and revalidates evidence for the
selected SELL path after protection/recovery work. The default manager clock
remains the supplied fixed `now` for existing callers; offline Monitor replay
can explicitly inject a frozen clock. Venue revalidation uses pre-consumption
metadata so the accepted generation does not reject itself as a duplicate.
Original market/source timestamps, generation, evidence and all thresholds
remain intact.
No shared writer, lock, Research, notification or WebSocket code changes.
No policy, historical result, capital or permissions changes. SHADOW ONLY,
real_trading_enabled=false, NONE_SHADOW_ONLY and V2's 20000 total remain intact.

## Runtime evidence, separate from candidate tests

At the baseline, the primary proof references publication
`0be226c239e34025c5fa545c0c7b8941e7147b83`, generation 20:45 UTC, completed
20:46:29 UTC. Its scheduler SHA256 matches the checked-in scheduler bytes
(UTF-8 text). The baseline commit only updates that proof; its parent is the
referenced publication. Scheduler duration is 39.032 seconds, missed buckets 0,
real orders 0. Monitor reports 175 V1 and 7 V2 open positions, no new ADD/SELL,
no management/exit-book refresh failures, and no closed-observation quote gaps.
V1 lifecycle duration is 11.122551 seconds and V2 3.208505 seconds.

Seven sampled authoritative scheduler snapshots at 19:50, 19:55, 20:05, 20:15,
20:25, 20:35 and 20:45 UTC all report zero missed buckets, with execution durations
33.451–39.760 seconds. Twelve consecutive publication commits were listed from
19:50 through 20:45. This supports recent recovery; it does not establish the
cause of older reported timeouts, installed wrapper hashes, total lock wait,
absence of intervening failed attempts, or complete runtime acceptance.

## Contracts checked

- Missing Binance open-holding quotes abort; closed-only gaps remain explicit
  observation metadata. Bybit holdings require venue-matched evidence instead.
- Empty lanes retain the existing seven-file publication validation.
- V2 Monitor deliberately defers ADD to the full capital allocator; capacity
  waits, absent signals and normal HOLD states are not execution failures.
- Host runner refreshes state after lock acquisition. Generation admission skips
  already published buckets. CAS rejects protected concurrent changes, retries
  from a fresh checkout, and exact authoritative readback precedes success.
- Research and Monitor use the same host lock; cross-host writers still rely on
  CAS. Linux flock/process cancellation tests are required in CI. Windows unit
  results alone cannot prove host-level isolation or timeout recovery.

## Verification commands and deployment acceptance

```sh
python -m unittest discover -s tests -p 'test_hunter_monitor_decision_clock.py' -v
python -m unittest tests.test_hunter_monitor_boundaries tests.test_hunter_position_monitor tests.test_hunter_lifecycle_state tests.test_hunter_scheduler_health tests.test_hunter_monitor_runtime tests.test_hunter_monitor_persist tests.test_hunter_monitor_snapshot -v
python -m unittest discover -s tests -p 'test_hunter*.py' -v
git diff --exit-code -- research/results research/hunter-capital-state.json
```

The decision-clock module contains nine deterministic tests covering source
acquisition, inter-lane work, portfolio load, manager decision/identity/protection,
ADD capacity delays, per-holding expiry, unchanged generation, normal no-delay
SELL/ADD, V2 deferred proposals, fixed-clock replay and default-caller parity.
Network and ledger IO are mocked. Other targeted suites use offline fixtures
and temporary local Git origins. The
complete Hunter suite is an existing PR-triggered Ubuntu gate; do not dispatch
the trading or deployment workflows. Record the final PR head SHA and each CI
run separately from runtime evidence.

After separately authorized deployment, inspect installed code/wrapper hashes,
service results and lock phase logs during natural Monitor/Research overlap.
Validate lane timestamps against actual evidence age, including near-expiry
source evidence, and confirm stale data cannot authorize exits or additions.
Check duplicate buckets, failed/timeout attempts followed by recovery, protected
CAS rejection, exact seven-file readback at its own published SHA, primary proof
digest/generation, unchanged historical events and the fixed capital limits.
No workflow dispatch, deployment, merge or historical replay is part of this PR.
