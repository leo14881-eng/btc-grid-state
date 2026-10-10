# Exact ENA/PENDLE manual shadow stop: wired draft, NOT EXECUTED

Request ID: Sentinel_035030c90a0881919c51c31e1ac68700.
Original request time: 2026-10-10T22:04:13.099124Z.
Reason: USER_REQUESTED_MANUAL_STOP_LOSS.
The optional user_text field is omitted to avoid encoding ambiguity. The request
ID, timestamp, explicit reason and exact authorized position IDs are immutable.

## Scope and execution status

This draft now wires the exact one-time request into the existing natural V2
position monitor. Merging/using this code in the approved runner will activate
the request on its next naturally admitted monitor cycle. This task has NOT
merged, deployed, manually triggered a trading workflow, or changed live state.
No actual new SELL/realized loss is claimed by this draft or synthetic CI.

Only SHV2-20261005T075147-ENA-b10cd5 and
SHV2-20261005T075147-PENDLE-ea9b45 are eligible. Both require their original
single 1000 USDT tranche, BINANCE_SPOT identity and 10 bps fee model.
The BYBIT execution_channel label never selects a venue. New positions in the
same asset are ineligible. No arbitrary force-sell CLI or generic request API
exists. V1, Sentinel, PR35, PR98 and capital/policy settings are not modified.

## Verified baseline and preservation of others' work

Initial complete reads used main e6e0e8b1963d6a80bfd935174418925af30a4987.
Its complete recursive tree contains no AGENTS.md/.agents/SKILL.md.

| File under research/results | Fixed Git blob |
| --- | --- |
| hunter-shadow-v2-portfolio.json | 416d546767bf46ee503559eba95bc03ab0693849 |
| hunter-shadow-v2-summary.json | a2273042e230fe3be2d489c82ffd9b16b6dcca8f |
| hunter-position-monitor.json | f617043f6a397b15374ddf1fa0ec97f29b8fd18c |
| hunter-scheduler-health.json | 1d7488d6e33ba85ceb5f733a31194df592fb5305 |
| hunter-shadow-v2-overfilter-guard.json | 1de21941370bda102b40040ba423815f5c697956 |

At that snapshot (22:06:39.520060Z): 10 open, 24 closed, 14000 USDT deployed.
ENA model quantity 3951.2110854549505; PENDLE 397.990745361763.
Initial draft final readback main 270ae616c11969c949ce8fab19f1ca164ff03db0,
portfolio blob e54327a7eb475285fd1f2b5b91eaa97135486f3f, still had both open.
No historical receipt price is reused for the actual request.

Integration work is based on main cb3891809653f56499849bcc47159b5f1908e417.
Its monitor, writer and repository runner sources match the initial baseline;
its other files are preserved through a merge-parent commit, not overwritten
from the older draft tree. No PR98 branch/files are used or rewritten.

## Natural monitor hook and unchanged publication contract

research/hunter_position_monitor.py::run_lane calls the pure stage function
only for V2 with an exact authorized shadow_id still open. The order is:
configure V2; update current risk observation; record the natural scan
generation; stage the manual request; run ordinary management for other
holdings; build existing summary; let the original publisher validate/publish.
SELL generation_id uses that actual natural Monitor generation. Request ID
and manual_event_id remain separate; no artificial MANUAL scheduler generation
is introduced. The V2 monitor summary separately reports user_manual and
automatic_strategy exit cohorts; overall realized PnL and risk include both.

The hook reads the same newly acquired raw exit books from liq.snapshots.
It samples datetime.now(UTC) at the actual V2 invocation, AFTER any slow V1
processing. Maximum book age is 30 seconds. It does not use the earlier
evaluated_at timestamp to pretend stale books are fresh. A stale/failed book
safely BLOCKS that target until a later natural cycle reacquires it.

Pending blocked exact target positions are excluded from ordinary SELL/ADD
evaluation for that cycle and restored in their original order. They cannot
fall through to a reference-price SELL with a different reason. Other holdings
retain the ordinary scheduled monitor behavior. The hook itself targets no
other position and never starts a full Research cycle.

Successful target copies use lifecycle.liquidation's FULL held quantity across
observed bids and existing buy/exit fees; no last-price or best-bid shortcut.
The handler calls exit_analysis, refresh_post_exit_status,
update_loss_exit_guard, register_exit_for_reentry, record and trade_event.
Its staged copy preserves unrelated positions/history; later normal monitor
observation/compaction behavior is unchanged.

Receipt and request ledger are embedded in V2 portfolio; the staged audit is
embedded in the V2 result inside hunter-position-monitor.json. No eighth file,
new write permission, new host lock, new publisher or fake scheduler generation
is introduced. scripts/hunter_monitor_persist.py and the runner are unchanged:
the existing seven-file validator, protected-input CAS, non-force push and
exact main readback remain the only formal publication route. New modules and
tests already match the writer's protected research/hunter_ and test_hunter_
prefixes. Existing CAS conflicts discard the generation and re-enter on fresh
main/market evidence, as covered by the existing offline real-Git race tests.

Audit status remains NOT_PUBLISHED in the staged data. Only the original
publisher's authoritative readback is execution confirmation. An uncertain push
must be resolved from main receipts before any retry or success report.

## Durable idempotency and notification compatibility

Each target's permanent receipt contains request/time/reason, its
manual_event_id, exact raw book and SHA256, execution calculation, source-main
SHA, before-state hash and actual execution time. The audit also hashes the
staged state. The request ledger survives ordinary monitor generations.

The real compact_closed_history drops manual fields and has a 5000-row cap.
Retry therefore validates the independent permanent receipt, recomputes its
full-depth cost and matches remaining archived exit facts when present. It
does not require bounded events or closed/archive rows to remain forever.
Unexpected receipt targets, reopened exact IDs, conflicting receipts or
remaining inconsistent history fail closed. Successful targets never sell twice.

Events use manual_event_id for request correlation and do NOT set event_id.
The notification consumer retains its unchanged canonical digest ID for
selection, output and acknowledgement. An observed notification acknowledgement
therefore suppresses the same manual SELL on replay.

## Existing risk effects and review evidence

Current systemic observation is applied BEFORE manual exits, preventing the
same observation from immediately advancing recovery after the loss.
Two losing exits trigger the EXISTING two-consecutive-loss circuit rule:
TRIPPED, released 2000 USDT quarantined, and reentry risk-locks. Manual reasons
remain separately identifiable; no guard is bypassed. Replays add no new loss
count, quarantine or events. 20000/17000/3000 limits remain unchanged.

BTC-relative exit metrics are explicit UNKNOWN (null/status), since the pure
manual handler does not claim an independently fresh BTC mark. Normal
post-exit lifecycle follows 1/6/24/48/72-hour observation windows for weekend
review. Keep manual intervention separate from strategy-triggered exits,
including actual request-to-execution delay.

## Offline acceptance and remaining approval

Tests use synthetic portfolios/books only and cover run_lane integration,
full-depth costs, partial/new-cycle retry, real compact archive and rolloff,
notification acknowledgement/replay, stale-after-V1 timing, wrong identity/fee,
current risk-observation ordering and no repeat circuit side effects.
Integration output passes the unchanged seven-file validator and exact
readback checks with natural scheduler generation. Existing offline writer
tests cover protected/nonprotected Git races and failed publication recovery.
CI has read-only contents permission; no production workflow is triggered.

Before merge, parent must review the exact final head and its CI. This code is
now ACTIVE-on-next-natural-monitor if approved/merged, unlike the original
pure-handler draft. Installed host launcher equivalence remains unverified:
an upstream runtime_file read of /usr/local/bin/hunter-monitor-runner.sh was
denied. Do not retry another path/interface to bypass that denial.

Actual completion still requires approved runner/source evidence and main
readback containing each new manual SELL, quantity, venue, VWAP, simulated net
PnL, reason, request/time, unique manual_event_id, partial status and fixed
commit/blob hashes. If those are unavailable, report NOT SOLD/NOT VERIFIED.
