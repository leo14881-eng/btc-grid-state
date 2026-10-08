# Hunter auxiliary fresh-source recovery — 2026-10-08

## Scope and safety
SHADOW SIMULATION ONLY. No private order APIs. No portfolio second writer, strategy changes, BUY thresholds, protection parameters or capital changes. Sentinel full migration was canceled by the user; existing Sentinel tasks stay enabled.

## Confirmed production failure
Natural Discovery started 2026-10-08T01:17:00Z. During its run PR #82 changed the authoritative code read set. Auxiliary publication correctly rejected stale source with AUX_CAS_REJECTED_STALE_WRITER at 01:17:22Z. Failure health/readback completed at 01:17:28Z. Research did not start. This natural generation remains FAILED.

The subsequent authorized Watchdog run at 01:20:24Z succeeded but did not recover Discovery: its publication-recovery allowlist covered replay jobs only. Requested recovery is not recovered execution.

## Minimal correction
The scheduled auxiliary launcher retains one same-job flock across at most three complete generations. Only exact CAS/race/exhausted transport publication errors for Discovery/Blind Replay/Missed Replay permit a fresh clone/fetch of latest main and full recomputation. It never retries stale outputs. Authentication, data, validation and unknown errors fail closed. Research/Watchdog do not enter this whole-job retry path. Critical setup errors propagate even inside conditional shell execution; cleanup leaves the deleted checkout before removing it.

Watchdog now admits a recent Discovery publication CAS failure through its existing strict recovery eligibility checks. Collection/authentication failures do not qualify. Recovery requests are independently verified after execution.

## Executed tests
7 launcher tests exercise real temporary Git repositories, new source SHA/cwd on retry, bounded attempts, exact error classification, same-job concurrent exclusion and cleanup. 17 Watchdog eligibility tests pass. Full Hunter suite: 630 tests passed in 33.141 seconds. bash -n and git diff --check passed. No simulated fixture was written to production.

## Acceptance boundary
Code merge does not install /usr/local/bin/hunter-scheduled-job.sh. The installed wrapper must be checksum guarded, atomically replaced, read back, and then exercised on the server. Explicit recovery of the failed hour is USER_AUTHORIZED_RECOVERY, not a successful natural 01:17 generation. The next natural complete hourly chain remains independently required. Production fault injection is not used to manufacture CAS evidence.
