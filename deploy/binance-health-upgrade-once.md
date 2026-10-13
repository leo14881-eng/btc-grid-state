# One approved Binance collector upgrade

This is a review candidate. Nothing here has been run on the coin server. The
existing Ops connector remains read-only; denied SSH/WSL/runtime paths are not an
installation channel. Do not retry them, create credentials, or grant permissions.

The implementation reuses the verified 2026-10-13 02:37 PR124 readback, including
the complete installation receipt, two guard hashes, stopped invocation IDs,
budget boot and preserved historical-UNKNOWN rows. It does not ask the operator
to repeat that diagnostic export. Only facts that must still be true are checked
again by the installer's read-only preflight. A mismatch exits before creating an
attempt, starting/stopping a service, opening SQLite, fetching Git, or swapping code.

## Fixed source and payload

- Preparation baseline: `b584270500750c2d7720cc784ad76d3e5b1fa402`.
- Existing runtime: `f4e8ad4de377a19763f4803e4fd56882c27d887a`.
- Candidate runtime: `33abf19c7a91855ba2efc2e65e1d383055494d75` (PR126).
- Runtime lock SHA256: `417ec411c5cdc17ab597873e585b4b8ebb7ac270f9f4fbe8dffac2213d89a6c1`.
- Complete runtime set: 110 old files, 114 new files; six modified and four added.
  The builder checks the exact set and every hash/mode. It excludes current result
  files and ledgers except the unchanged, hash-pinned policy configuration.
- Two previously reviewed helpers are bundled at their existing exact hashes.
  Their production entrypoints, disposition/probe routines and old acceptance
  supervisor are not called. The new executor uses only named preparation,
  immutable-code staging and collector-identity child helpers.

`build_binance_health_upgrade_bundle.py` builds from these immutable Git objects;
it never fetches or contacts a server. The bundle includes `SHA256SUMS`; final
delivery must additionally give the reviewed package head and archive SHA256.
An unreviewed working-tree bundle is not eligible for installation.

## One approval covers these concrete actions only

1. Use the operator's existing authorized Linux root terminal and transfer the
   reviewed bundle to a **new**, root-owned mode-0700 directory named
   `/tmp/hunter-binance-health-upgrade-<40-character-package-head>`. No existing
   account, group, permission, ACL, credential, firewall, sandbox or dependency
   changes. Missing prerequisites abort; this does not authorize obtaining root.
2. Launch exactly one new transient unit,
   `hunter-binance-health-upgrade-33abf19c.service`: Type=exec, Restart=no,
   RuntimeMaxSec=1800, TimeoutStopSec=240, KillMode=control-group, independent
   ExecStopPost cleanup. It checks its actual PID1 properties and invocation.
3. Revalidate current old head/all installed bytes, the two consumed guards,
   previous supervisor/collector stopped invocations, no queued job, no collector
   UID process/cgroup occupant, no unreviewed drop-in, original dependency and
   budget boot. Never reset an old guard, failed unit, budget or ownership record.
4. Create the separate exclusive `health-upgrade-once-33abf19c...json` guard and
   invocation-specific evidence directory. The old guard bytes remain unchanged.
   Read the stopped SQLite using the existing collector UID; preserve halt,
   cooldown, active leases and historical UNKNOWN. Any mismatch consumes the new
   attempt and exits; there is no owner disposition, probe, reset or second try.
5. Fetch current main into a unique **audit ref** in the existing repository;
   never checkout/reset/rebase that repository or write its result files. Capture
   the complete authoritative natural CEX scan; it must be <=20 minutes old on
   admission and pass the original <=30 minute manifest check. No Discovery job
   or trading workflow is dispatched. Run shortly after a normal `:17` Discovery
   publication to avoid consuming an attempt on stale scope.
6. Preserve opaque state evidence and the complete old code/head. Stage and verify
   the pinned new code, swap code/head using reviewed immutable renames, prepare
   and archive the complete manifest/receipt as the existing collector user, then
   add **only** `/etc/systemd/system/hunter-binance-stream.service.d/90-health-upgrade.conf`.
   It replaces ExecStart with the same public-spot collector plus
   `--health-supervision`, sets TimeoutStopSec=90, and binds this invocation in a
   non-secret environment marker. No base-unit permissions or consumer settings
   change. Run daemon-reload and verify the effective settings.
7. Submit exactly one collector start. A pending/activating start is not treated
   as stopped: cleanup owns the durable start intent and unique configuration,
   cancels/stops that attempt, then requires no pending job, PID, UID processes or
   cgroup occupants. After a collector invocation is known, replacement invocation
   stops are refused and reported. Never stop an unrelated/new replacement unit.
8. Verify 300 seconds of advancing, bound health samples within a 1200-second
   acceptance window. Full roster/ACK/shard/source/epoch/manifest/budget constraints
   remain mandatory. DEGRADED with data UNKNOWN can pass collector health only.
   Success leaves this collector running; independent normal-exit cleanup must
   recheck health and persist the final receipt. Failure or supervisor death uses
   the independent cleanup hook and a bounded stop, saving final audit evidence.

This approval does **not** merge PR126 or another PR, enable a boot-time service,
change a consumer selector, trigger natural jobs, alter a strategy, write the
historical portfolio ledger, trade, modify Bybit/Sentinel, or include PR35/98.

## Operator steps after the exact approval

The installation payload must be delivered through an existing permitted channel;
the agent cannot use the refused SSH/WSL/ops paths to upload or execute it. Verify
the delivered archive SHA256 against the final review, then unpack its fixed files
into the new directory above with the existing root identity. Keep that directory
until all supervisor exit evidence has been read: the independent hook needs it.

The single execution command is:

```sh
bash /tmp/hunter-binance-health-upgrade-PACKAGE_HEAD/launch.sh --approved-collector-upgrade-once
```

Replace `PACKAGE_HEAD` with the exact delivered 40-character package commit, not
the collector SHA. The launch script checks all package hashes and read-only
preflight before dispatch. Its flag is an accident-prevention check, not approval.
Do not rerun a consumed guard or a failed dispatch to manufacture another attempt.

Read-only progress and final evidence:

```sh
systemctl --no-pager show hunter-binance-health-upgrade-33abf19c.service \
  -p ActiveState -p SubState -p MainPID -p InvocationID -p Result -p ExecMainStatus -p Job
journalctl --no-pager -u hunter-binance-health-upgrade-33abf19c.service -n 120
systemctl --no-pager show hunter-binance-stream.service \
  -p ActiveState -p SubState -p MainPID -p InvocationID -p Job
```

Use the guard's exact invocation-specific evidence directory. A zero launcher
return only confirms dispatch. Require the final independent receipt **and**
PID1's terminal supervisor result for that invocation. An `acceptance.json` left
before a later hook failure is not final success. A failed final receipt or stop
uncertainty must remain explicit; no automatic retry/rollback/start is provided.

An interrupted swap may be restored by the reviewed swap routine before start.
Other preparation failures can leave code, manifest or the candidate drop-in
staged but stopped. They retain backups and evidence for a separately reviewed
recovery. Do not delete those artifacts, reset the guard or start the service by
hand. External root changes during this attempt can invalidate ownership proof;
in that case cleanup refuses to stop a replacement and reports manual review.

## What this delivers, and the remaining consumer work

PR126 already contains the new Binance reader health gates. Research early
signals, forward daily research and Monitor BTC5m already have cache call sites.
Their normal isolated task code must include PR126 before switching selectors;
old main readers do not enforce DRAINING/instrument/quiet recovery proofs.

| Consumer | Original complete demand | Code still required |
| --- | --- | --- |
| Research early signals | 409 pairs x 15m/1h = 818 topics at this snapshot | None beyond PR126 reader; approved selector and natural publication proof remain |
| Forward daily research | Every originally selected Binance pair, 15..35 daily rows; most recent cycle had 32 Binance daily consumers | None beyond PR126 reader; never shrink the selected set for acceptance |
| Monitor systemic risk | BTCUSDT 5m, 16-row window and 13 completed closes | None beyond PR126 reader; requires natural 5min source proof |
| Monitor position signals | Original selected holdings + BTC, 25x15m and 5x1h; current 80 assets + BTC = 162 topics | Add one complete-set frozen read helper and pass the same Frozen into rolling 1h/4h and micro; keep selection, formulas and failures |

The persistent Binance roster remains 1228 topics (all 409 pairs x 15m/1h/1d plus
BTC5m). 4h returns use native 1h bars; adding a native 4h stream is unnecessary.
Directory/book/price and longer historical reconstruction retain their actual
REST semantics and budgets. Quote streams are not substitutes for these Klines.

Monitor's smallest remaining patch is two runtime modules and 6–8 targeted cases
for full-set/BTC binding, no fallback, formula/provenance equivalence, expiry and
unchanged position rotation, followed by existing Monitor/CAS/policy regressions.
Estimated implementation/review effort is 2–3 hours, excluding production approval
and natural-cycle observation. This executor does not silently include that patch.

Bybit already has its own unit, store, manifest preparation, SERVER_DIRECT route
and complete-set reader in main. At 04:17:25 it was still VIA_WORKER with eight
Kline batches: 101 signals, 40 failed symbols (80 interval failures, BYBIT_HTTP_403),
not 141/141. Complete public-spot scope is 141 Bybit-only bases + BTC = 284 topics
(15/60). Existing runtime still reconnects the whole session on a silent topic
after 90 seconds and replenishes its budget only after full READY; it does not
inherit PR126's Binance health/quiet logic. Bounded observation is possible after
its separate install approval; a reproduced silence problem needs an independent
runtime repair and tests before promising stable cutover. A direct endpoint denial
must halt; this is not permission to bypass it or fall back to Worker.

Bybit holdings currently require zero management subscriptions because there are
no Bybit positions. Future holdings need a separate consumer/roster contract;
current management still uses direct REST. The Alpha signed Worker proxy is also
outside this public-spot migration and must not be declared removed or migrated
under this approval.

The shortest staged delivery is collector deployment/health evidence, then the
small Binance Monitor patch and matching reader cutover, then Bybit's bounded
full-scope runtime verification and necessary fixes. Health, complete consumer
READY and natural publication are separate acceptance facts. No 2–4 hour promise
for total migration follows from a collector acceptance.
