# Bybit public spot candidate: default off

PR101 now includes the public instruments/tickers adapter as well as the Kline
sidecar. It is a reviewable implementation, not a deployed service. The branch
integrates main `778e74459cf6d76ce4bea83a666c04dda4adc68c` (including PR105/107)
without changing their files relative to that main. No production selector,
service, timer, credential, account permission or strategy threshold was changed.

## Selection and covered callers

| Selection | Discovery metadata | Discovery Klines | Research spot availability |
| --- | --- | --- | --- |
| Unset / `HUNTER_BYBIT_SPOT_SOURCE=WORKER` | Existing Worker path | Existing `HUNTER_BYBIT_KLINE_SOURCE` selection, default WORKER | Existing Worker path |
| `HUNTER_BYBIT_KLINE_SOURCE=SPOT_CACHE` alone | Existing Worker path | Single-generation read-only cache | Existing Worker path |
| `HUNTER_BYBIT_SPOT_SOURCE=SERVER_DIRECT` | Fixed official public HTTP | Same single-generation cache | Same direct instruments adapter |

SERVER_DIRECT requires SPOT_CACHE if the Kline selector is explicitly present.
Unknown selectors, conflicting selectors and `HUNTER_BYBIT_DIRECT_DISABLED=1`
fail before IO. The deployed disabled flag is not changed or overridden.
Missing cache, denied direct access or bad metadata never selects another source.

`hunter_bybit_direct.Adapter` is shared by Discovery and availability. It keeps
one complete instruments receipt for 24h in the existing SQLite state, with a
host-level refresh flock. It preserves full-list validation, BTC/ETH anchors,
Trading/USDT and xstocks filtering. Corrupt/future cache is rejected. A listing
contradiction uses the existing exactly-one full refresh and one new bulk ticker
request. Each generation fetches a bulk ticker; there is no per-coin ticker loop.

The authenticated `availability.alpha()` implementation and call remain intact.
Existing held-position `hunter_bybit_management` traffic is also unchanged and
outside this budget. At P valid positions with no early failure it can make
2+4P public requests, with four position workers and its existing 12s socket
setting. Adapting that module crosses the strategy release/hash and runtime
permission boundary; it requires its own scoped review. Do not claim that all
server traffic or all Worker dependencies have been unified or removed.

## Receipts, manifests and source identity

The direct snapshot uses `hunter_bybit_direct_snapshot_v1` and
`OFFICIAL_BYBIT_V5_SPOT_DIRECT_CACHE`; metadata is
`OFFICIAL_BYBIT_V5_SPOT_DIRECT`, and Klines retain
`OFFICIAL_BYBIT_V5_SPOT_CACHE`. Full instruments/ticker response receipts carry
capture time, actual receive time, source/path identity and checksum. Validators
rederive listing coverage and ticker row values and bind the listing receipt.
Worker/cache-only snapshots cannot silently accept the direct metadata contract.

The same existing output filename and publisher/CAS/readback path remain.
There is no second result or ledger writer. Cold direct metadata can create a
valid UNKNOWN manifest with no signals, using the authoritative Binance exclusion
set and selected Bybit-only assets plus BTC. It needs no fabricated Worker
receipt. Adopting a changed roster still requires an explicit validated manifest
and collector restart; no automatic roster operator was installed.

## Cancellable public HTTP and cooperative budget

All three implemented request families use `hunter_bybit_public.Client`:
full instruments, bulk tickers, and production Kline REST bootstrap/repair.
They share the pre-existing `/var/lib/hunter-bybit-stream/state.sqlite3`, with
>=200ms admission spacing and at most four held leases. This is a cooperative
local budget, not proof of Bybit quota acceptance or total same-IP accounting.
Clients use existing-only SQLite opens and cannot create an empty replacement
budget. The collector's explicit state initialization is separate.

Each admitted request runs a disposable Linux HTTP child. This is not another
resident collector. The child accepts only the fixed official host and allowed
spot paths/parameters, has no proxy or redirect, receives no credential env,
and cannot read the SQLite state. It execs first, sets parent-death SIGKILL,
checks the parent, announces READY, and waits for GO before DNS/network work.
The supervisor commits an owned request marker and rechecks the global gate
before GO. No preexec callback runs inside a threaded parent.

A monotonic total deadline defaults to 30s and covers budget wait, process startup,
DNS, connect/TLS, headers, body and IPC; the inner socket timeout is 10s. Cancellation
or expiry kills the child and waits up to 1s for reap. Complete responses are
usable only after reap. Trusted completion atomically retires marker and lease,
with each SQLite busy wait at most 250ms. Rolled-back lock contention can retry
only coordination transactions within the original deadline; it never retries
HTTP or disk faults. Marker and lease stay held throughout completion retries.
A deadline is a cancellation boundary, not a hard real-time OS scheduling promise:
if the OS cannot reap the child, the request stays stopped with its lease held.
No Future timeout pretends an open socket has ended.

Before-GO cancellation may clean up a proven-unsent request. After GO, incomplete,
malformed, cancelled or otherwise uncertain outcomes retain marker plus lease
and block future requests after their owner lock closes. Reaping alone cannot
prove that an unreported 403 did not occur. There is no automatic lease TTL,
orphan reset, DB recreation or alternate source. A child that outlives its
supervisor is killed by the Linux parent-death signal; the marker remains.

A 403 header marks a pending global stop before body processing. If that write
fails, the supervisor first closes its owner lock so other clients see an orphan,
then kills/reaps the child; it reports that denial persistence failed. Complete
frequency403 classification preserves >=600s cooldown; country/unknown403 is a
manual-review halt. Partial/slow403 remains pending/manual-review. Only complete
429/5xx and the existing API retry codes receive at most three attempts within
the same total request deadline. Unknown transport outcomes are never retried.

Admission and cooldown use a boot-bound monotonic clock. Wall-clock changes
cannot shorten them. A changed boot or a nonempty old budget without that clock
domain blocks HTTP, WS startup and cache consumption pending manual review.
The old explicit-clock repair seam exists only for injected offline fixtures;
real entrypoints have no relaxed-clock selection. Corrupt request markers stop
before any owner-file path is opened.

## Offline evidence and its limits

Linux CI runs two independent guarded suites on Python 3.10 and 3.12:

- `python -I -B -X utf8 scripts/check_bybit_stream_offline.py`: 105 fixture tests;
  no network/DNS/exec and only temporary writes. Forked workers inherit the guard.
  Six different checkout directories contend for one budget. A second six-process
  test mixes instruments/ticker/Kline clients and asserts >=200ms admission and
  an observed peak of exactly four held requests. Default-off dispatch, consumer
  formulas, listing expiry/contradiction, receipts, bootstrap UNKNOWN, cooldown,
  restart, migration and storage/denial failures are covered.
- `python -B scripts/check_bybit_public_transport.py`: eight real process/socket
  tests against literal 127.0.0.1 only. Each exec child installs its own audit guard.
  Slow headers/body, simulated stalled resolution, cancellation, lost403 event,
  pending403 and supervisor death prove actual socket closure or retained leases.
  The 1s deadline fixtures require completion within 2.5s; that is CI evidence,
  not a French-server shutdown SLA or a live DNS/TLS performance measurement.

Exact commit/check results are recorded in PR101. Windows Python execution is
blocked by the local OS, so runtime checks use the authorized offline GitHub CI;
no alternate interpreter or security bypass was attempted. Independent static
review identified legacy-clock, one-character symbol and failed-denial-admission
gaps; all three now have focused regression coverage. No real market request,
rate stress test or strategy workflow was invoked.

## Production prerequisites, not executed

1. Verify the exact release SHA, actual runtime accounts/launcher/env, Python and
   SQLite versions, Linux parent-death behavior, time sync, disk/inodes/headroom,
   current services and pinned `websockets==15.0.1`. Read-only server status is
   not installation authority. No denied SSH or launcher route was bypassed.
2. Review shared state provisioning and schema/boot migration. Metadata requesters
   need DB/journal and request/refresh-lock write access; this is broader than the
   existing read-only Kline consumer. SQLite offers no per-table OS permissions.
   Actual account rights are unknown. Do not grant them implicitly, create a
   per-checkout budget, or clear old cooldown/leases. A broker would be a separate
   design, not an implemented capability of this candidate.
3. Review one collector, `Restart=no` during observation, bounded stop/start
   controls, resource/log retention, roster adoption and recovery ownership.
   Inventory held-position and other same-IP traffic separately. No unit/timer or
   dependency has been installed by this PR.
4. Separately authorize natural-cycle observation and source activation. Require
   complete roster, source receipts, 15m/hour rollover, cold/warm timing, actual
   shutdown, metadata turnover and unchanged single-writer CAS/readback. No manual
   strategy rerun or live order is part of acceptance. GitHub executors without
   the host state retain their existing explicit source.

Unresolved403, incomplete metadata, stale/corrupt cache, missing budget coverage
or unacceptable latency blocks activation. Preserve state and receipts on stop.
A future rollback is a reviewed source decision at a generation boundary; it
must not bypass a denial or mix a partially produced generation's sources.

See [the sidecar and freshness contract](bybit-stream-sidecar.md). No actual
French-server access, throughput, geography or sustained-latency evidence was
obtained here; those problems are not claimed solved.
