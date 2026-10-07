# Hunter replay recovery — 2026-10-07

## Actual incident and restoration

Natural missed-replay unit 2026-10-07 16:55:04–16:57:10 UTC failed at Persist replay only with AUX_PUSH_RETRY_EXHAUSTED; subsequent health publication failed with JOB_HEALTH_PUSH_FAILED. Underlying push stderr was discarded. Historical reason cannot be established as authentication, race, or policy from those old messages.

PR #69: feature 7b3523338a2481c8cde4c0fa3f6d38b5d3c80524; merge 5109d9434870fd24f7e8413fd6732e5feef8c43e. Added explicit push-error classification and bounded redacted diagnostics; only explicit races or transient transport failures use existing retries. Authentication, policy and unknown refusals stop without bypass. 24 related /558 full Hunter tests passed; GitHub CI37667294388 SUCCESS.

Actual user-authorized manual systemd retry:
- Unit start: 2026-10-07T18:31:11Z.
- Runner start: 2026-10-07T18:31:36.409329+00:00.
- Persist replay only: 18:32:10.127302–18:32:17.239191 UTC SUCCESS.
- Output main readback: 18:32:18 UTC, source/readback 5109d9434870fd24f7e8413fd6732e5feef8c43e, one exact result file.
- Health completed: 18:32:18.551431 UTC; actual GitHub health publication/readback confirmed18:32:24 UTC.
- Unit finished SUCCESS. This is manual restoration, not a natural scheduler cycle and not proof of the historical bottom-level cause.

## Automatic recovery gap addressed by this change

Previously watchdog read only GitHub job health. If publishing failure health also failed, a previous SUCCESS document concealed the real failed run. It also accepted only AUX_CAS_REJECTED_STALE_WRITER, ignoring proven retry-exhausted races/transient transport failures.

Each real systemd job now writes an atomic owner-only local receipt under /var/lib/hunter-job-health before attempting GitHub health publication. This is local health evidence only: local_publication_only=true, github_health_publication_verified=false. It cannot claim GitHub health publication or change portfolios, capital or trade events. Per-job flock, chronological rejection, fsync and replace prevent stale/concurrent receipt overwrites.

Watchdog considers both current main health and local receipts, using the newest run. Newer success suppresses an older failure. Only fresh, timezone-qualified, VULTR_SYSTEMD, shadow-boundary, exact replay-persistence failures are eligible:
- AUX_CAS_REJECTED_STALE_WRITER
- AUX_PUSH_RETRY_EXHAUSTED_RACE
- AUX_PUSH_RETRY_EXHAUSTED_TRANSPORT_FAILED

Recovery requests the existing full replay systemd unit, whose existing flock and fresh-main disposable checkout remain authoritative. It never republishes stale output. Authentication/policy/unknown errors, validators, data failures, stale/future/malformed receipts remain ineligible. Requested does not mean recovered: runtime status stays RECOVERY_REQUESTED_NOT_VERIFIED until real execution and readback succeed.

Baseline39 related tests passed; after50 related tests and569 full Hunter tests passed on isolated server checkout using existing Python venv. New tests cover local failure without GitHub receipt, both directions of newer-success suppression, stale writer, conflict, permissions, exact error markers, shadow constraints and invalid evidence.

## Boundaries and pending evidence

No real orders/API, no strategy/BUY/ADD/SELL changes, no portfolio writer, no capital change. V2hardcap20K ordinary17K reserve3K; tail4K remains stress diagnostic. Existing five-minute monitor and hourly research remain.

Local-receipt deployment and subsequent watchdog consumption require actual runtime verification. No production failure or fake trade is manufactured for acceptance. Persistent Task disabling, server model/notification transport, cross-platform writer handoff, 24h A/B and true ARM/EXIT samples are separate unresolved matters. Overall PARTIAL_FIX.
