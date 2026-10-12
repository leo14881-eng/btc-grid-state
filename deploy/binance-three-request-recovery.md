# One supervised Binance recovery for the 2026-10-12 17:19 incident

This entry is specific to epoch `51c723c224194317a49329ba8751d77f` and the uploaded
report SHA256 `7c05690bbe062fec2638ab5f6b61fe35cd40617306acf35a9f25d383900c1f15`.
It is not a generic orphan cleaner. Default invocation prints a plan; only
`--run` performs the explicitly authorized recovery through the existing root
server console. Do not use a previously denied SSH, WSL or ops execution route.

The historical outcome of all three public HTTP requests remains **UNKNOWN**.
A new successful HTTP response establishes present connectivity only. The entry
archives each old request/lease in `manual_http_dispositions` before removing its
active blocker, in the same SQLite transaction. No trading ledger is touched.

## Fixed deployment and admission

The old runtime is `99836e5362340725f806d5df6df1ca9835065bdd`; the candidate is
`8cb92bd7e39251e1997e1b363a5e9500c8344268`. The full 110-file manifest is
`binance-upgrade-revision-manifest.json`; only its four enumerated runtime files
change. The Binance-private copy of the shared transport helper is included;
the live Bybit installation, units, permissions and consumers are unchanged.

Before mutation or market HTTP, the entry verifies the deployed package, stopped
unit, empty collector UID/cgroup, exclusive locks, exact diagnostic report and
DB/manifest byte hashes. It refuses another epoch, boot, acquisition times,
request count, lease mismatch, denied request, persistent halt or active cooldown.
It fetches a fresh complete scan to a private Git ref without checking out or
overwriting main. The scan must be at most 20 minutes old; it is never refreshed
by changing its timestamp or triggering a trading workflow.

Each invocation receives a new UUID and its own exclusive report path. Reports
from old preflight failures, including the former fixed-path report, are never
deleted, replaced or reused. Before fetching a new scan, admission checks the
incident attempt marker, deployed package/stopped state and original source
bytes. An unchanged incident that failed only scan freshness can pass these
checks again. The same marker and byte checks repeat under maintenance locks.

The durable incident marker is now written before backups or any live SQLite
open, including a read-only open that could create WAL/SHM files. Once that
marker exists, even an interrupted partial attempt is refused before another
fetch, public GET or service start. Only a separately reviewed recovery could
proceed; deleting a report never resets the incident marker.

Private raw and consistent SQLite backups, code rollback material and intent
receipts are saved before disposition. A durable exclusive incident-attempt
receipt prevents another probe on rerun. The one BTCUSDT 5m public GET uses the
remaining fourth budget slot while the three old slots stay occupied. There is
no automatic probe retry or budget reset. A valid 16-row response and actual
child reap are necessary but not sufficient: original rows, budget guards and
boot are checked again before the atomic archive/retirement.

## One manual execution

After reviewing the exact PR head, use the single pinned command block supplied
with that head. It fetches that package and its fixed runtime/helper objects,
extracts the standalone entry into `/tmp`, checks its SHA256, then runs it using
the existing `/opt/hunter-binance-stream/venv/bin/python`. No account, key,
permission, dependency or service configuration is created or repaired.

The entry starts `hunter-binance-stream.service` once. Existing bounded internal
reconnect behavior remains unchanged; the wrapper never restarts the unit.
Every 15 seconds it samples the real cache without market HTTP. The complete
roster must become READY within 900 seconds and stay READY in one epoch for
300 seconds, within an overall 1200-second acceptance window. Every blocking
acceptance call is bounded by the remaining time and checked again on return.
The latest fresh CACHE_AUDIT must independently report at least 300 seconds
on the existing runtime continuous-health timer, bound to the exact code,
manifest, roster and epoch; later reset/recovery events invalidate it. The
shutdown and evidence-save phase can follow that acceptance deadline. Zero active owners, archived
UNKNOWN evidence, native windows, closed-bar validation, 90-second freshness
and source/manifest binding are required. Missing observation or a nondurable
acceptance report cannot authorize leaving the service running.

On halt, cooldown, new orphan, service exit, changed evidence or expired acceptance
window, the entry stops the unit, verifies stopped state and preserves evidence.
It does not retry, clear new unknown owners, reset start limits or roll back state.
A stop timeout still triggers independent stopped-state verification and report
saving. If stopping cannot be verified it reports `STOP_UNCONFIRMED_MANUAL_REVIEW`.

The invocation's result is saved to the unique path printed at entry:

`/tmp/hunter-binance-recovery-51c723c224194317a49329ba8751d77f-<invocation_uuid>.report.json`

Its path and SHA256 are printed. Detailed private backups remain in the printed
`/opt/hunter-binance-stream/upgrade-*` evidence directory. Never upload its raw
owner-token files; the result report uses anonymous request sequence numbers.
If report creation, writing or fsync fails after start, the unit is stopped and
independently checked. The console says `report_persisted=false`; a separate
private failure report preserves the stop result when its directory remains
writable. A partial report is evidence, never acceptance by itself.
Do not rerun after the incident marker has been created. Code rollback is a separately reviewed
use of the existing revision adapter and its evidence directory; it must not
restore retired active rows or reset denial/cooldown.

## What acceptance does not claim

READY for 300 seconds is collector acceptance, not proof of consumer migration.
Natural 5m/15m/hour boundaries, a native 1d event and the next natural consumer
publications still require their own receipts. No selector is changed here.
All output remains SHADOW ONLY, real=false, capital_authority NONE_SHADOW_ONLY;
V2 20000/17000/3000, SingleWriter/CAS/generation, dedup and 5-minute Monitor stay
unchanged. There is no Worker fallback, trading workflow, Sentinel change or
PR35/98 merge.
