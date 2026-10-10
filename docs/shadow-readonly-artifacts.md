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
and run/source binding. Hunter summary counts, realized P&L and capital amounts
must reconcile to portfolio rows; duplicate shadow IDs across open, closed and
archived rows are rejected. This does not claim unrealized liquidation-value or
all strategy-risk reconciliation. Portfolio observation IDs and monitor bucket IDs are
different namespaces: the observation must fall inside the bound monitor run.

Existing portfolio validators and reporting functions are reused. The capital
inspection reports the unchanged V2 20,000 total / 17,000 ordinary / 3,000 reserve
limits. It does not evaluate every strategy risk rule or alter allocations. The
overall result remains PARTIAL because live deployment and full risk coverage
are not established. Missing or invalid evidence is UNKNOWN, never empty success.
Age budgets are caller-supplied observation budgets, not strategy thresholds or
market-session-aware scheduler deadlines. `age_budget_exceeded` is an observation,
not a scheduler failure: `scheduled_run_overdue=UNKNOWN` and
`calendar_coverage=UNVERIFIED` explicitly prevent weekend/holiday pauses being
represented as proven missed runs. Calendar-aware policy is not implemented.

Every Hunter receipt requires the correct job/schema/source, explicit shadow
safety fields, a true main-readback attestation, full source/readback SHAs, and
their locally verified ancestry to the pinned evidence commit. Missing or
shallow history remains UNKNOWN; the reader never fetches it. Stock manifests
require their own safety/identity/source evidence but still remain PARTIAL for
freshness coverage because they do not contain an independent final readback
receipt. A corrupt/unsafe Hunter receipt blocks new notification candidates.

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

The original daily-review requirement has now been recovered: send at **08:23
Vietnam local time**, covering the **previous complete Vietnam calendar day**.
This is an established requirement, recovered from the original instruction,
not a new user decision. Private source conversation links are not published.
For example, the October 5 08:23 report covers October 4 00:00 inclusive through
October 5 00:00 exclusive at UTC+07:00, including all subsecond events before
midnight. That equals `[2026-10-03T17:00:00Z, 2026-10-04T17:00:00Z)`. It must not
be replaced with the previous US exchange session, even when the reporting day
is a weekend or holiday.

The original stored scheduler TZID has also been recovered as **Asia/Bangkok**
from the historical configuration.
08:23 in that zone is 01:23 UTC. This historical evidence is not a current
scheduler readback. For example, the October 11 report covers
`[2026-10-09T17:00:00Z, 2026-10-10T17:00:00Z)`.

For the October 5 example, pass `--report-start 2026-10-04T00:00:00+07:00`,
`--report-end 2026-10-05T00:00:00+07:00`, and
`--report-timezone Asia/Bangkok`. Tests also check the equivalent modern
UTC+07:00 boundaries with `Asia/Ho_Chi_Minh`.
The existing hourly :23 main job does not prove deployment of this daily report.
No service, timer, or automation is installed by this proposal. Confirm only the
remaining session-aware freshness budgets and intended rule-risk inventory
separately; do not ask the user to reconfirm the recovered daily-report semantics.
FastWatch remains the original acceptance scope; this adds no FastWatch service.

The dedicated PR/push CI runs offline unit tests only, with contents:read and no
production dispatch. No deployment workflow, runtime authority, portfolio, trade
ledger, strategy threshold or exit rule is modified.

## Independent review regression evidence

Before the review fix, regression assertions reproduced false COMPLETE results
for corrupted summary counts/amounts, duplicate position identities and invalid
receipt safety/readback fields (15 methods ran, 13 failures). After the fix these
cases fail closed, including new-candidate suppression. The original isolated
baseline lacks the source/readback ancestry needed for the stronger proof and
now honestly reports UNKNOWN instead of the earlier candidate-ready result.
Neither a successful offline test nor a published Git manifest establishes that
latest-origin acquisition, server publication/readback or live scheduling has
been implemented by this proposal. It remains uninstalled and undeployed.
