# Coordinated Binance recovery: review candidate

This change is **not deployed or enabled**. It starts from main
`6ceef0cea2d3689f49d651c6c8c20cde1ffafb3e` and includes PR130
`52f7545814c1aa0313a800abf0c23c2d6a7bbd59`, preserving the PR126 reader
composition. Obtain the exact candidate from the draft PR head; the Binance
acceptance workflow checks out that head, not the synthetic merge commit. Existing deployment/export bundles
are unchanged. Do not merge, install or restart from this document without the
separately authorized server entry point and deployment review.

## Gap addressed

Legacy HTTP ownership and market writes shared one SQLite writer. An HTTP
response could be complete and its child reaped while retirement remained
unconfirmed. Neither a process heartbeat nor received=committed WS totals
prove HTTP retirement or consumer readiness. The previously observed legacy
request/lease remains a deployment blocker; this patch never deletes it.

The optional `--coordinated-control` mode adds an independent `control.sqlite3`
with short serialized transactions for admission, rate charges, ownership,
quarantine, publication intents and per-topic revocation versions. No control
transaction waits for HTTP, a market transaction or a Future. There is no
dedicated writer daemon/thread. The existing collector process owns the work.

Coordinated market writes enter a FIFO queue shared by Store instances for the
same canonical database path within one process. Continuous WS batches cannot
overtake an already queued directory/history publication. Admission has its own
explicit deadline, at most two seconds and no longer than the caller's timeout;
the SQLite busy timeout remains at most 250ms, with no hidden BEGIN retry.
Waiting holds no database transaction or HTTP owner. Reads and an existing
publication's nested helpers bypass the queue. Diagnostics distinguish queue
admission from SQLite begin/body/commit failures. This provides local writer
ordering, not cross-process fairness or a two-second transaction-duration bound.

The second full-load failure was queue exhaustion, with no corresponding BEGIN
lock error. A paired scratch measurement isolated the effect of simultaneous
whole-roster reads: generic snapshot `deepcopy` dominated the reader profile
and write-body time increased while BEGIN remained below 1ms on average.
The follow-up avoids unused bar columns and one repeated same-frame topic
query, and uses a detached JSON snapshot copy for plain JSON trees. Unusual
Python values retain the old deepcopy path. Public copies, proof/hash checks,
delivery fences, per-frame savepoints, revision retention and receipt accounting
remain intact. Neither queue capacity nor publication frequency is relaxed.
Rejected delivery audit copies also use the already-owned JSON snapshot;
non-JSON graphs retain deepcopy behavior. This is a copy-cost reduction, not
evidence that the sustained queue-capacity failure has been resolved.
The per-frame history gate now returns aggregate scalars from SQLite instead
of materializing every stored history row. Its exact needed-start set, history
floor and confirmation truthiness remain equivalent; malformed stored starts
use the old Python path. Tests compare all durable tables and frozen outputs
against the immutable prior hot path, including invalid and duplicate frames.

Each database reports its own phase histogram, count, maximum and cumulative
microseconds. P99 is explicitly a histogram bucket upper bound over the Store
or Control instance lifetime, not an exact percentile or sliding window.
Connection observations include configured timeout, BEGIN type, and measured
`PRAGMA busy_timeout`/journal mode. Failures preserve the original extended
SQLite error code/name where Python exposes them; missing values are unknown.
The observed Linux starvation error was SQLITE_BUSY (5), not proof of
SQLITE_BUSY_SNAPSHOT (517). A scratch-only stale-snapshot regression tests 517
reporting without claiming it happened on the server. Python added the error
attributes in 3.11; `Connection.setconfig` (3.12) is not an error-code switch.
See the [Python exception attributes](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Error)
and [connection configuration API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.setconfig).

The loopback reconnect fixture captures the original collector traceback and
connection context, plus independent in-memory phase metrics from both files.
It does not perform extra competing database reads after each watcher BUSY.
Final cleanup reads retain unknown counts when unavailable, and all watcher
SQLite failures still fail acceptance. Bounded, hashed log chunks preserve
large failure records without relying on one oversized CI assertion line.
Control intent and scope JSON decoding now happens after copying rows from
one read transaction and closing it. Native owner checks remain inside the
snapshot, and the independent gate and final fence checks remain active.
Event-controlled regressions show that paused decoding no longer pins a
competing commit. This does not establish the sole cause of the Linux BUSY5.

Market publication has three steps: durable intent and scope revocation;
atomic market rows plus receipt; control reconciliation. Replaying a committed
receipt is idempotent. A quiet-bar mismatch commits its corrective rows and
reverification latch with an UNKNOWN receipt; it cannot release the scope.
Only a subsequent valid verification can recover it. Monotonic intent order
prevents replaying an old success from clearing a newer failure. Directory evidence binds
the complete dependent symbol/interval scope and the canonical status/filter
payload. Missing identities, symbols or required filters fail closed.

Four classes share four workers: directory, initial history, gap repair, quiet
verification. Each eligible class gets a turn in a four-class rotation. Shared
rate limits, acknowledgement and retry backoff can delay admission; there is
no guaranteed wall-clock completion time. The original 600 weight/minute pacing
and official directory weight 20 are retained; 21 directory batches use 420
weight, leaving limited history/quiet capacity. Full-scope production convergence
must be measured, not inferred from four active workers. REST remains necessary for official
directory data and bounded history/repair. No Worker fallback is introduced.

Only continuously held native owner identity, response/completion evidence and
confirmed child reaping permit retirement. A recoverable cleanup problem
quarantines the exact scope, permanently discards that response and grants one
explicit re-verification attempt after safe resource reclamation. Missing or
ambiguous proof, orphan owners, control corruption or a failed recovery stop
the collector. There is no TTL lease deletion. Shutdown uses a shared deadline;
unfinished work is recorded as unresolved, never as successfully reaped.

Re-verification retains the original job key, kind and dependency scope even
when newer WS observations remove the ordinary quiet/repair condition. The
same acknowledgement, backoff, per-topic exclusion and shared admission limits
still apply. The one re-verification opportunity also respects the existing
three-failure ceiling; reaching that ceiling or failing after the recovery
claim stops explicitly with the gate preserved, rather than leaving hidden
pending work or granting another recovery attempt.

Consumers take a fresh control fence before freezing market rows, then a new
market/control read after computation and before delivery. Revocation ABA,
epoch/manifest/roster changes, expiry, missing topics and draining invalidate
the result. Early and Monitor also recheck immediately before their actual
use boundary. Immutable local evidence is checked without re-freezing history.
Monitor carries an optional guard into the shared existing-position manager:
each position checks before its decision, immediately before ADD, and after
identity I/O before signal-dependent exit/recovery evaluation. A rejected
frozen generation stays rejected for that invocation, including its copied
signal/health fields and deferred ADD. Independent book/supply invalidation
and profit protection remain available. The default path without a cache
guard keeps its existing behavior. Early also rechecks after history parsing
and after temporary-file verification, before replacement.
Old readers reject the v2 contract. Keep the collector and readers on the same
reviewed source; a mixed-version installation is not an acceptance test.

These checks establish validity at their observation points; they do not hold
a database transaction across a portfolio action or filesystem publication.
Do not describe them as atomic with those writes, or reuse a cached signal for
later actions without the action's own check. Natural production consumer
publication remains a separate acceptance requirement.

## Scope and invariants

Subscribe to all actual native intervals: roster symbols 15m/1h/1d and BTCUSDT
5m. For 409 symbols this is 1,228 topics; 4h remains derived from 1h. Early's
whole required set, Monitor's existing management selection plus BTC, Daily's
existing rotation and BTC's closed 5m history remain unchanged. Quiet REST does
not refresh the 90-second WS clock. A single stale required topic can keep an
entire consumer set UNKNOWN. `collector_inventory` and actual consumer
acceptance are reported separately by the ops checker.

The receipt boundary is arrival after protocol parsing, before queue admission.
A queue-rejected market frame is still received and must become a durable
`unprocessed_recorded` receipt with its exact scope and event identity. It is
never counted as accepted/committed. Deterministic tests exercise the real
session pump with a one-entry queue, check revocation before drain, and verify
scoped repair in the same and a new epoch without clearing another topic's
unresolved receipt. The full-load acceptance queue remains 4,096 entries.

SHADOW ONLY; real trading false; capital authority NONE_SHADOW_ONLY. No changes
to V2 20000/17000/3000, SingleWriter, CAS, generation, the five-minute Monitor,
deduplication, historical ledger, Sentinel or trading workflows. The separate
historical event-range REST reader retains its original page budget; this does
not establish one governor for every process sharing a public IP.

The old Fast Watch bookTicker/aggTrade service is not a kline collector. Bybit's
separate 15m/60m work is not enabled by this change; the official Worker snapshot
and its batch counts must not be presented as completed server migration.

## Reproduce the candidate without server access

Run from an isolated Linux checkout of the exact draft PR head. These commands
write only test scratch directories and allow only guarded loopback fixtures:

```sh
git rev-parse HEAD
python -c 'import sys,sqlite3; print(sys.version); print(sqlite3.sqlite_version)'
python -m pip install -r deploy/hunter-binance-stream-requirements.txt
python -I -B -X utf8 scripts/check_binance_stream_offline.py
python -I -B -X utf8 scripts/check_binance_quarantine_regression.py
python -I -B -X utf8 scripts/check_binance_signal_use_regression.py
python -I -B -X utf8 scripts/check_binance_public_transport.py
python -I -B -X utf8 scripts/check_binance_health_offline.py
python -I -B -X utf8 scripts/check_binance_coordinated_offline.py
# Same fixture, with only three cache hot-path modules pinned to ec3d49b9:
python -I -B -X utf8 scripts/check_binance_write_load.py --prior-cache
# Separate one-hour wall-clock run; followed by the real WS process reconnect:
python -I -B -X utf8 scripts/check_binance_write_load.py --seconds 3600
```

The coordinated suite requires Linux and zero skips. It exercises real native
locks, child HTTP exit, crash-before/after-market-commit, full-topic WS writes,
WAL checkpoint pressure, UTC midnight/Vietnam 07:00, interval boundaries and
actual consumer use fences. Synthetic timestamps do not prove a production
midnight observation. Windows portable SQLite tests are supplementary evidence.
The two regression scripts intentionally require four assertion failures on
the immutable `0b15bd7dde4d90519f316f1a146fd13da6f6b91a` modules: two scheduler
cases and two actual V2 Monitor/engine cases. Cache fixtures and other
dependencies are from the candidate; this is not a replay of the complete
prior deployment. Import errors, skipped cases or blocked effects fail these
scripts instead of counting as a reproduction.

The additional whole-roster test fully initializes all 1,228 histories before
at least 30 wall-clock seconds of queued writes, whole-scope reads, checkpoints
and directory/quiet/repair work. Linux uses real rate-governed HTTP children;
the Windows body fixture does not reproduce that transport or its pacing.
Its metrics report initialized rows, READY scopes, queue delays, SQLite errors
and ownership terminals. Initial full-roster history uses fixture bodies and
real dual-database publication; it does not time a cold 1,228-topic REST backfill.
A separate Linux collector process subscribes three topics over two epochs:
it must reconnect to a loopback WS server, reject the first epoch's frozen
read, acknowledge and seed the second epoch, and close both sessions with
conserved receipts and retired HTTP owners. Neither fixture establishes a
natural UTC boundary or full-roster collector WS reconnection.

Ordinary discovery keeps a 32-second load target. The one-hour command opts
into 3,600 real elapsed seconds at the same 1,228-topic/0.8-second offered
cadence, 4,096-entry queue, 100-frame batches and checkpoint pressure. It uses
a declared fixed UTC origin plus real elapsed time, with explicit prior-bar
WS closes across synthetic 15m/1h boundaries. It is not natural-clock evidence.
Logs include periodic progress, failure-safe drain observations, all-sample
receipt/batch latency histograms, quiet completion waits and per-topic receipt
partitions. Reader rejections and non-READY durations remain visible; occasional
READY reads must not be described as continuous readiness. The dedicated
PR131 Linux job waits for the short acceptance jobs and uses the same head.
Workers share a start gate after HTTP and thread preparation; setup time is
excluded from the load target. Batch and consumer read/delivery CPU and wall
time are separate, and checkpoints sample maximum WAL size. Thread CPU clock
resolution is reported, so zero samples on a coarse clock are not zero cost.
Test collection explicitly orders the load before the real-process reconnect,
including in the one-hour mode; it does not rely on alphabetical class order.

The prior-cache comparison loads only the immutable ec3d49b9 stream store,
health store and consumer modules. Candidate coordination, diagnostics and
fixtures are retained to measure the same offered load. It is not a replay
of the complete prior deployment. A queue-capacity failure is the expected
comparison outcome; unrelated exceptions, missing tests or blocked effects
cannot count as that reproduction. No benchmark result permits deployment.

The PR131 `contention-profile` CI job is a separate diagnostic, not an
acceptance gate. It seeds one scratch database pair, restores the same paths
and file identities before each eight-second leg, and compares writes with
and without a continuous full-scope reader. Both legs retain 1,228 topics per
0.8-second target round, the 4,096-entry queue, 100-frame batch limit and
0.2-second PASSIVE checkpoints. HTTP and the recovery scheduler are absent.
Queue saturation remains an explicit outcome with its actual elapsed time;
it does not count as eight completed seconds or permission to run the hour gate.
The `binance-contention-<python>-<head>` artifact contains `paired8-report.json`
with actual offered/processed counts, queue and WAL samples, batch boundaries,
thread/process CPU, nested read-phase timings and per-topic receipt partitions.
Samples are not cross-thread atomic, phase timings are inclusive, and the
fixed no-reader-first order does not establish GIL causation. Source hashes
and the real checkout SHA are separate from synthetic fixture identities.
Use this evidence to select the next bounded experiment; do not relax the
full-load gate or infer production behavior from the diagnostic.
The optional `--probe-switch-interval 0.0005` adds one full-reader leg in the
same process, restoring the same seed and changing only Python's diagnostic
thread-switch interval. The original interval is restored in `finally`; both
actual values are recorded. This tests scheduling sensitivity without
changing production settings. Improvement cannot establish GIL as the only
cause; absence of improvement cannot exclude long C operations holding it.
The separate `--probe-snapshot-codec` option checks copy compatibility and
measures a private, in-memory binary-copy candidate against the existing JSON
copy using the same generation. It adds a full-reader leg at the original
switch interval. Only bytes produced from the accepted plain-tree input in
that process are decoded; no binary cache is loaded from a file or network.
All generation/window hashes, proof, time and delivery fence checks remain.
The candidate is applied only within this diagnostic and restored afterwards;
faster copying alone does not establish sustainable writer throughput.

## Offline provisioning and reviewed installation

These are reviewable commands for a **future authorized deployment**, not
actions performed by this PR. First establish the exact stopped service,
invocation, candidate checkout, state path and existing account/group access
through the approved server entry point. Retain source head, journal and
read-only SQLite backup evidence. Do not enter previously denied SSH, WSL or
stock runtime paths and do not expand permissions.

The existing `hunter_binance_stage.sh` is first-install-only: it deliberately
refuses an existing code directory, release-head or unit. It is not an upgrade
command for the currently installed collector. An upgrade package must be
prepared against the actual source/unit hashes and existing approved paths,
with conditional replacement and retained rollback artifacts, after those
read-only facts are available. This PR does not authorize guessed paths or
overwriting the installed package.

The available Ops connector has rejected reading the actual Binance unit and
service journal. Obtain a fixed, redacted evidence export through an approved
maintenance entry before deployment: unit/drop-ins and service identity;
code/release hashes; Python/SQLite/websockets versions and journal modes;
directory ACLs, file identities and disk capacity; legacy request/lease,
budget/boot/epoch records; and the matching invocation/process/lock evidence.
Absence of a PID alone cannot prove response completion or child reaping. An
unverifiable request stays unresolved; neither deleting its marker nor creating
a new state database is an approved migration path.

Provisioning requires the existing collector.lock, exclusive offline ownership,
`connected != 1`, no legacy requests/leases/halt, matching boot identity and no
unexpired cooldown. It carries forward spacing and conservatively charges a
legacy window whose usage cannot be established. An orphan, stale boot, missing
access or partial provisioning is a manual-review blocker, not permission to
delete state, choose a fresh database or reset the rate budget.

```sh
# In the approved candidate checkout, using its approved service identity:
python -B scripts/provision_binance_control.py \
  --state /var/lib/hunter-binance-stream/state.sqlite3
```

This creates the sibling control DB and gate but does not start a service.
Review `deploy/systemd/hunter-binance-coordinated.conf.example` against the
actual unit. Its only feature switch adds `--coordinated-control` to health
supervision; Restart=no and control-group shutdown remain. No unit install,
daemon-reload or restart is automated here. Do not activate a consumer selector
as part of collector installation. Verify its separate authorization first.

The patch does not change journal mode or filesystem permissions. Inspect the
actual runtime SQLite version and modes before considering WAL. WAL still has
one writer per database and does not make two databases atomic. Any WAL rollout
must use a runtime containing the SQLite WAL-reset fix (3.51.3+, or the relevant
3.44.6/3.50.7 backport) and reviewed read-only WAL/SHM permissions. No forced
checkpoint runs on the collector request hot path. Keep control audit size and
disk free space visible; this version retains control audit records and does
not install an automatic retention/deletion policy.

## Acceptance and rollback boundary

Record exact source SHA, service invocation/PID, boot, epoch, manifest hash,
roster hash, topic count and actual consumer scope. Preserve HTTP call/attempt
terminal counts and hashes, unresolved/held owners, quiet/repair queue waits,
transaction phase timings and SQLite error codes. Require WS receipt
conservation, complete terminal partition and zero unexplained held work.
Health supervision alone cannot grant data READY. Check each actual consumer
scope and its final delivery fence using the ops checker; preserve UNKNOWN
reasons and do not shrink a failing required set.

Observe initial history, reconnect, a real native close boundary and the
separately approved five-minute consumer run before claiming migration. A
successful offline suite establishes code behavior, not these server facts.
On failure, stop via the approved service control, keep both databases and the
gate/owner evidence, and leave consumers UNKNOWN. Do not revert a selector to
Worker to hide incomplete migration. Do not downgrade over an active v2 epoch,
delete the control DB, clear a halt, or reset budget/lease records automatically.

References: [SQLite transactions](https://www.sqlite.org/lang_transaction.html),
[snapshot isolation](https://www.sqlite.org/isolation.html),
[WAL caveats and fixes](https://www.sqlite.org/wal.html),
[Binance Spot REST contract](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md).
