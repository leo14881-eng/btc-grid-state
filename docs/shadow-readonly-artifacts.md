# Read-only shadow artifact proposal

This module is an offline producer, with no installed service, timer, publication,
notification send or ACK/cursor writer. Run from the repository root with
`python -B -m scripts.shadow_readonly_artifacts`. Its only output is JSON on stdout.

Required inputs are `--source-sha` (full immutable Git commit), `--as-of` (aware
timestamp), `--freshness-budgets-json` (job name to maximum age in seconds),
`--report-start`, `--report-end` (aware timestamps), and `--report-timezone` (IANA).
`--repo` selects an existing local repository. No fetching occurs. Every input is
read with `git show <same SHA>:<allowlisted path>`, so dirty files and moving refs
cannot mix generations. The caller must establish that the pinned SHA came from
authoritative main; this offline tool cannot establish remote recency itself.

## Scope and evidence

The inspected baseline is `a43425a286055a6cd9e6593549c9bc6422003bc7`.
There are no AGENTS.md or .agents/skills files in its tree. Hunter primary job
flags are true. Stock authority is server/epoch 3, covering main/monitor/replay.
Published Watchdog health says SUCCESS but its nested FastWatch health is
PARTIAL_FAST_PATH_DEGRADED. The aggregate preserves that limitation. These Git
receipts are evidence of published observations, not a live server inspection.

The producer reports nine job receipts, explicit age budgets, missing/future
timestamps, failed/overdue receipts, V2 portfolio/summary generation and time,
monitor/scheduler generation and exact byte hash, and stock manifest/ledger hashes
and run/source binding. Portfolio observation IDs and monitor bucket IDs are
different namespaces: the observation must fall inside the bound monitor run.

Existing portfolio validators and reporting functions are reused. The capital
inspection reports the unchanged V2 20,000 total / 17,000 ordinary / 3,000 reserve
limits. It does not evaluate every strategy risk rule or alter allocations. The
overall result remains PARTIAL because live deployment and full risk coverage
are not established. Missing or invalid evidence is UNKNOWN, never empty success.
Age budgets are caller-supplied observation budgets, not strategy thresholds or
market-session-aware scheduler deadlines. Weekend/holiday schedule semantics
still require an approved policy before production alerting.

## Candidate delivery contract

The existing notification helper supplies canonical IDs and validation. Exclude
the union of ACK, reserved, pending and unresolved IDs (including IDs in content
hash maps); conflicting content fails closed. An opaque pending batch without
event IDs fails closed. The bootstrap/event cutoff remains mandatory. No receipt,
pending batch, reservation, ACK, retry count or last-success field is changed.

Parallel identical calls produce the same artifact ID and byte-equivalent JSON.
Each candidate carries the exact cursor hash and fixed source. This is duplicate
*candidate* determinism, not a delivery lock or an exactly-once guarantee. ChatGPT
remains the single notification consumer/writer. Before any future send, it must
revalidate current authoritative source and cursor, then apply its existing
exclusive reservation/CAS protocol. A stale candidate must be recomputed, never
automatically sent or resent. This proposal does not implement that consumer.

## Daily review and unresolved deployment decisions

The report interval is explicit and half-open `[start, end)`. Report currency is
the existing USDT accounting unit. Identical trade rows are deduplicated; same
identity with different content and duplicate closed records are UNKNOWN.
Closed-position P&L uses `closed_at`. Current open valuation is labelled as the
snapshot value, not historical interval-end valuation. Missing prices remain
PARTIAL with null total P&L and a separately labelled known subset. A ledger
watermark before report end remains PARTIAL. Future report ends are UNKNOWN.

No 08:23 daily-review contract was found in the inspected stock timer, workflow
and scheduler configuration; the existing hourly :23 main job is not evidence of
an 08:23 daily report. Confirm the reporting timezone and interval (calendar day,
exchange session, or trailing window) before configuring a schedule. Confirm
session-aware freshness budgets and the intended rule-risk inventory separately.
FastWatch remains the original acceptance scope; this adds no FastWatch service.

The dedicated PR/push CI runs offline unit tests only, with contents:read and no
production dispatch. No deployment workflow, runtime authority, portfolio, trade
ledger, strategy threshold or exit rule is modified.
