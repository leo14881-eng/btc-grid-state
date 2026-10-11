# Hunter shared-write-lock incident evidence

This is a read-only incident reconstruction, not runtime acceptance of a fix.
All times below are UTC on 2026-10-11. The inspection baseline is
`d0166b3d058637f7c5429bb5829439e848202ebf`. GitHub commit-path history was
queried with this SHA, so newer main changes cannot alter this reconstruction.
The baseline recursive tree contains no `AGENTS.md`.

## Research timeout and recovery receipts

| Receipt | Job start | Bind start / end | Bind result | Job end / duration |
| --- | --- | --- | --- | --- |
| [15:27 health](https://github.com/leo14881-eng/btc-grid-state/blob/2b558370eacb970f4b674d9c0dbf7b724d7f40a7/research/results/hunter-runtime-research-health.json) | 15:21:42.299792 | 15:25:10.564634 / 15:27:10.598026 | `PORTFOLIO_LOCK_WAIT_TIMEOUT`, 120.033 s | 15:27:10.598407 / 328.299 s |
| [16:28 health](https://github.com/leo14881-eng/btc-grid-state/blob/348705e69d98e7254e8a897babbaa131942dccbf/research/results/hunter-runtime-research-health.json) | 16:23:59.003419 | 16:27:29.396175 / 16:27:45.109344 | `SUCCESS`, 15.713 s | 16:28:32.350329 / 273.347 s |
| [20:27 health](https://github.com/leo14881-eng/btc-grid-state/blob/eecdeae38ff7cd205fcfb9280ae193e1fbba5d2f/research/results/hunter-runtime-research-health.json) | 20:22:11.574838 | 20:25:32.186363 / 20:27:32.212726 | `PORTFOLIO_LOCK_WAIT_TIMEOUT`, 120.026 s | 20:27:32.213163 / 320.638 s |

The failing step in both failure receipts is exactly `Bind latest authoritative
monitor state before shadow execution`. Neither receipt contains later V1/V2
execution or persistence steps. Both report `main_readback_verified=false`.
This establishes failure before this invocation's shadow capital execution; it
does not independently audit every portfolio event created by other invocations.

The 15:27 receipt commit is `2b558370eacb970f4b674d9c0dbf7b724d7f40a7`
(commit time 15:27:15); source and reported readback SHA are both
`66662e08d0ae1f6150316202dd1c6709010c2ec9`, generation
`20261011T151837722344Z`.

The 20:27 receipt commit is `eecdeae38ff7cd205fcfb9280ae193e1fbba5d2f`
(commit time 20:27:49); source and reported readback SHA are both
`de13487c8df81d6db7caf456196a236e81a31bb6`, generation
`20261011T201844641181Z`.

The recovery receipt commit is `348705e69d98e7254e8a897babbaa131942dccbf`
(commit time 16:28:35). It records successful V1 execution (15.654 s), V1
integrity check (0.818 s), V2 execution (4.832 s), V2 integrity check (0.316 s),
mixed-generation guard (0.618 s), acceptance guard (1.169 s), persistence
(21.809 s), and authoritative main readback (1.752 s).
`main_readback_verified=true`; source and readback SHA are both
`304b559665c3066b38b84dad24584321e883987b`; generation is
`20261011T162147237856Z`. The bind step includes work after lock acquisition,
so its 15.713 s is not an exact lock-wait measurement.

All three receipts specify `source=VULTR_SYSTEMD`,
`real_trading_enabled=false`, and `capital_authority=NONE_SHADOW_ONLY`.
The recovery is proven to be a subsequent successful systemd-source cycle.
The receipt alone does not distinguish timer invocation from manual systemctl
invocation; calling it a natural timer recovery requires server journal evidence.

## Monitor evidence overlapping the 20:27 Research failure

The [20:25 scheduler receipt](https://github.com/leo14881-eng/btc-grid-state/blob/1ef9090248d271bc5a80179de4c680aa6118aa97/research/results/hunter-scheduler-health.json)
was persisted in state commit `1ef9090248d271bc5a80179de4c680aa6118aa97`
(commit time 20:27:31). It reports:

- Generation `2026-10-11T20:25:00Z`, health `HEALTHY`, trigger `VULTR_SYSTEMD`.
- `monitor_started_at_utc=20:26:42.812107` and
  `monitor_completed_at_utc=20:27:28.180135`, duration 45.368 s.
- `state_revision=a8ee54d0b82a165f259d53982a3b72e8ecb2493e`.
- Previous successful generation `20:20:00Z`, previous completion 20:22:38.221623.
- `shadow_only=true`, `real_order_count=0`, missed and duplicate counts zero.

The corresponding [verified primary proof](https://github.com/leo14881-eng/btc-grid-state/blob/288c7f8e0c399c660b0121b82548c32cba14183c/research/results/hunter-runtime-monitor-health.json)
is commit `288c7f8e0c399c660b0121b82548c32cba14183c`
(commit time 20:27:42), which is the immediate parent of Research's failure
receipt commit. It records `completed_at_utc=20:27:40.285151`,
`main_readback_verified=true`, readback SHA
`1ef9090248d271bc5a80179de4c680aa6118aa97`, generation `20:25:00Z`,
`real_trading_enabled=false`, and `capital_authority=NONE_SHADOW_ONLY`.
The scheduler digest recorded in it is
`1c7c96a7bcba2aac52fb49e40aec7ff6fd6dd7e9260cba7a590551546988efc8`.
Local SHA-256 calculation over the exact fetched UTF-8 scheduler receipt bytes
matches that digest.

Thus Research's 20:25:32.186363–20:27:32.212726 wait overlaps the reported
Monitor work and state persistence; Monitor's snapshot verification timestamp
is about eight seconds after Research gives up. These are correlated timestamps,
not proof of the process that held the kernel lock during the entire wait.

## Code observations at the pinned baseline

- [Monitor wrapper](https://github.com/leo14881-eng/btc-grid-state/blob/d0166b3d058637f7c5429bb5829439e848202ebf/scripts/hunter_monitor_runner.sh#L7)
  opens FD 9 and acquires `/run/lock/hunter-position-monitor.lock` before
  disposable clone, fetch, checkout, scheduler gate, regression tests, monitor
  computation, and persistence. There is no explicit unlock before process exit.
  This is a statically verified unnecessarily broad critical section; it is not
  a measurement of how long any individual incident held the lock.
- The wrapper assigns `STARTED_AT` only after clone/fetch/checkout and records
  scheduler success before persistence. Consequently scheduler duration 45.368 s
  excludes preparation and persistence/readback/proof publication, and cannot be
  used as total lock-hold time or full wrapper-cycle duration.
- [Monitor proof publisher](https://github.com/leo14881-eng/btc-grid-state/blob/d0166b3d058637f7c5429bb5829439e848202ebf/scripts/hunter_monitor_runtime.py#L57)
  is called after the complete snapshot readback and writes `completed_at_utc`
  before its own proof clone/fetch/push/readback. Its timestamp is not the shell's
  lock-release timestamp.
- [Research lock acquisition](https://github.com/leo14881-eng/btc-grid-state/blob/d0166b3d058637f7c5429bb5829439e848202ebf/scripts/hunter_job_runner.py#L426)
  uses nonblocking exclusive flock retries every 0.2 s and a 120 s deadline.
  It retains the descriptor through later steps and main readback, closing it in
  `finally` at line 474. This proves a normal exception cleanup path exists;
  process-failure release still requires executable tests.
- The successful 16:28 receipt places `Validate shadow engines` after binding
  (0.267 s, 78 tests). It therefore demonstrates testing within the Research
  critical section under the baseline runner behavior.
- [Research rebind](https://github.com/leo14881-eng/btc-grid-state/blob/d0166b3d058637f7c5429bb5829439e848202ebf/scripts/hunter_research_rebind.py#L49)
  fetches latest main, checks protected input changes and scan generation,
  validates the latest monitor snapshot, and imports both ledgers into the
  disposable checkout. These guards must remain effective when shortening the
  critical section; no stale snapshot may be committed directly.

The baseline wrapper has no holder PID/job identity or acquisition/release
timestamps. The reviewed receipts lack lock-holder identity, complete hold
duration, queue position/fairness data, and preparation/parse/serialization/
commit/readback breakdowns. The observed successful later cycle does not rule
out recurring starvation, and the two timeouts do not prove deadlock.

## Access limits and remaining acceptance

GitHub history and small JSON receipts were read successfully. A direct read of
the approximately 1.5 MB `hunter-position-monitor.json` through the connector
returned `HTTPError: 400: GitHub Fetch could not read file contents; the file may
be too large or unsupported (Response: None)` with `INVALID_ARGUMENT`.
The scheduler and runtime-proof evidence above was independently readable.

No server journal/process/lock inspection, new runtime execution, or deployment
was performed in this evidence subtask. Server access was already reported
blocked by the parent (`Permission denied`), and no alternative access channel
was attempted. No production/history files were mutated.

This document does not establish a fix, production runtime acceptance, or
exact incident holder identity. Those remain contingent on implementation
review, concurrent/failure/CAS/idempotence tests, isolated capacity measurements,
and post-deployment runtime and authoritative-main readback evidence.
