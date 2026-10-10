# ENA/PENDLE one-time shadow exit: staged implementation, NOT EXECUTED

Request: `Sentinel_035030c90a0881919c51c31e1ac68700` at
`2026-10-10T22:04:13.099124Z`. Reason: `USER_REQUESTED_MANUAL_STOP_LOSS`.
This request does not authorize real orders, general price/time stops, historical
backfills of SELL events, or a change to V1, Sentinel, PR35 or capital limits.

## Verified baseline

Read baseline: `e6e0e8b1963d6a80bfd935174418925af30a4987`, main observed during
this task. Complete recursive tree contains no AGENTS.md/.agents/SKILL.md.
All contents below were fetched completely (large JSON via Git blob):

| File under research/results | Git blob |
| --- | --- |
| hunter-shadow-v2-portfolio.json | 416d546767bf46ee503559eba95bc03ab0693849 |
| hunter-shadow-v2-summary.json | a2273042e230fe3be2d489c82ffd9b16b6dcca8f |
| hunter-position-monitor.json | f617043f6a397b15374ddf1fa0ec97f29b8fd18c |
| hunter-scheduler-health.json | 1d7488d6e33ba85ceb5f733a31194df592fb5305 |
| hunter-shadow-v2-overfilter-guard.json | 1de21941370bda102b40040ba423815f5c697956 |

At baseline, the V2 summary is as of 22:06:39.520060Z: 10 open, 24 closed,
14,000 USDT deployed. ENA and PENDLE are open, each with a single 1,000 USDT
tranche. Model quantities are 3951.2110854549505 and 397.990745361763.
Both execution venues are BINANCE_SPOT with 10 bps fees. Their BYBIT channel
labels are not pricing venues. Old receipt prices are deliberately not used
for this request. Fresh acquisition must happen inside the eventual writer.

## What this draft implements

`research.hunter_manual_stop_once.stage` is a pure staging function with no
network, filesystem, exchange orders or publication. It returns a deep-copied
candidate state and an audit explicitly marked NOT_PUBLISHED. It is not an
installed production command and cannot itself sell the authoritative holdings.

The exact request, timestamp, reason, two position IDs, original single-tranche
quantities, execution identities and fee model are pinned. No other asset is
eligible. Fresh complete Binance depth (maximum 30 seconds old) is passed to
the existing `lifecycle.liquidation`. Insufficient depth, wrong venue, invalid
or stale books fail closed per target. No last/best-bid shortcut is used.

The staged transaction calls existing exit_analysis, refresh_post_exit_status,
update_loss_exit_guard, register_exit_for_reentry, record and trade_event.
Existing events/closed rows and other open positions stay unchanged. No compact
or history truncation is run. BTC-relative returns are UNKNOWN (null with an
explicit status), because this path does not fetch a BTC mark.

Each target has a deterministic event ID and a durable request receipt in the
portfolio, independently of the five-minute generation and bounded events.
Receipts contain the actual request time, actual evaluation time, exact raw
book, book hash, source-main SHA, before-state hash and cost calculation.
Partial success is explicit; a retry reloads main and stages only unfinished
targets. Archived closed trades are checked on retries too. The raw and staged
portfolio hashes are included in the separate audit. No historical date is
written as the execution time.

Existing loss-exit side effects are material: if both exits lose money, they
increment loss counters, quarantine the released 2,000 USDT under the existing
circuit logic, and risk-lock reentry. This is existing policy behavior, not a
new fixed stop or a capital-limit change. 20k/17k/3k settings remain unchanged.

## Required integration and approval before execution

There is no verified one-time production writer entry point. The current
monitor publisher validates seven files and scheduler success for its natural
five-minute generation. Reusing it unchanged would misrepresent monitor work.
The installed host launcher is NOT verified; the server runtime_file request
for /usr/local/bin/hunter-monitor-runner.sh was denied upstream. Do not try
alternate paths or interfaces to bypass that denial.

The smallest integration should consume the exact request inside the existing
NATURAL monitor, not create a separate local writer that cannot hold the server
lock. No manual Research run, new server permission, generic force-sell API or
standalone publisher is proposed.

1. Parent reviews this staged handler and approves the concrete monitor hook.
   The hook is V2-only and accepts only this exact checked-in request, not
   arbitrary targets/reasons. Nothing runs merely by importing this draft.
2. At the next naturally scheduled monitor under its existing host lock, its
   isolated checkout fetches current main. The existing acquisition already
   refreshes all V2 exit books. Call stage with those fresh receipts, actual
   evaluation time and source-main SHA, before ordinary V2 position management.
   Unfinished targets remain blocked if receipts are unavailable. Other assets
   receive only their existing ordinary scheduled handling; the manual handler
   cannot target them. Do not manually trigger a full cycle on this request.
3. Persist the request ledger/receipt in V2 portfolio and rebuild V2 summary
   through the existing builder. Preserve real natural monitor/scheduler
   generation semantics. This is a genuine scheduled monitor completion, not
   an invented artificial scheduler success. Use the existing seven-file
   validator/CAS/push/readback under the original SingleWriter.
4. Add integration tests for target processing before normal management, summary
   loss/count consistency, natural scheduler generation, durable receipt/event
   preservation, unchanged V1 logic, and CAS retry on a fresh authoritative
   checkout. The existing CAS protects research/hunter_ modules and portfolio
   files; additionally protect any new request path. Never reuse quotes or
   staged decisions across a protected-input conflict.
5. On uncertain push, inspect authoritative main receipts first. A successful
   report requires actual new SELL IDs, quantities, receipt VWAP/net PnL,
   reason/request timestamp, partial outcomes, commit/blob hashes and exact
   main readback. Verify the deployed runner via an approved mechanism; do not
   assume the denied installed launcher equals repository code.

Parent review/explicit approval of the monitor hook and merge are still
required. This draft deliberately includes only the pure handler, offline tests
and this integration proposal. It neither hooks natural monitoring nor claims
to have a production execution CLI. The existing denied runtime path must not
be retried or bypassed.

Offline tests cover full depth costs, partial retry, cross-generation/archive
idempotency, stale/future/crossed/insufficient books, venue/fee/quantity/scope
rejection, existing-history preservation and loss/reentry side effects. CI has
read-only contents permission and synthetic fixtures only; it does not run
Research or fetch markets.

## Weekend review

When execution is safely published, retain the actual receipts and compare
1/6/24/48/72-hour post-exit observations through the normal lifecycle. Review
manual intervention separately from strategy-triggered exits, including delay
from request to execution and the reason the request needed a new entry point.
Do not count this draft, its synthetic test SELLs, or an unpublished staged
state as real current shadow exits.
