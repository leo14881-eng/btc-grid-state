# Binance post-failure bootstrap upgrade: prepared, not executed

This is a **new, independent one-attempt package**. It does not reopen the consumed
three-request recovery. Run only after explicit approval of this package in the
existing authorized root terminal. No SSH/WSL alternate path or privilege change
is included. This document is not a claim of deployment or recovery.

## Fixed inputs and admission

| Input | Exact value |
| --- | --- |
| Package entry commit | `e0534f1ae17e4d518c6e4aa7a433e22246834fea` |
| Entry SHA256 | `664b9ac795d569d3898e80c5864ca261760be03e6bddcd1b3022744b8ac84229` |
| Installed runtime | `8cb92bd7e39251e1997e1b363a5e9500c8344268` |
| New runtime | `f4e8ad4de377a19763f4803e4fd56882c27d887a` |
| Evidence wrapper | `7bd133584a21d5cc2af1147594f24031e29fd635` |
| Code exchange helper | `db0d59e3f7a6f15d1f99a678c07c2336e4474867` |
| Previous invocation | `676227d443f4421589636a4da8760a8f` |
| Previous report SHA256 | `d7cac76b56fe51a9daca95b92609cc801ee09cb71cde9afb19c9e56f356156fe` |
| Expected stopped epoch | `4eedfa078d32421e91a15dd06aac53d6` |

Both helper sources are hash-checked before execution. The full 110-file runtime
trees are checked; only `research/hunter_binance_stream.py` and
`scripts/check_binance_public_transport.py` may differ. Policy, installed unit,
websockets 15.0.1, immutable modes/owners and absence of unit overrides are checked.
The loaded collector unit must have no automatic restart or trigger, and the
supervisor must match the exact transient-unit and independent exit-hook settings.

The previous guard must exist, bind the previous invocation/report/evidence
directory, and remain unchanged. The fixed report must say FAILED_STOPPED with
stopped_verified=true and final owners=0. These historical claims do not substitute
for a fresh stopped PID/UID/cgroup check under maintenance locks.

After the **new guard is durably created**, raw DB/WAL/SHM and manifest evidence are
copied before any live SQLite open. SQLite access runs as the existing collector
UID. The current stopped epoch must match, requests and leases must both be empty,
the original boot identity must still match, budget counters must not have gone
backward, and no halt or active cooldown may exist. All three historical UNKNOWN
rows must match their original tokens, acquisition times, disposition and evidence
hash. No request is probed, archived, deleted or relabeled by this entry.

A complete natural Binance scan at most **300 seconds old** is fetched into a
unique Git ref and validated without timestamp edits or Discovery dispatch. It is
rechecked before code exchange/manifest preparation/start. Stale input means wait
for the next natural cycle; do not trigger a trading or Discovery workflow.

## Timing and acceptance

- Fixed runtime owns its shared **900-second** monotonic initialization deadline.
  Reconnects before first READY do not extend it. Its same-epoch
  `BINANCE_INITIALIZATION_READY` is required; the supervisor's first sample time
  does not replace that proof.
- Acceptance requires the runtime's continuous READY clock to reach **300 seconds**,
  a fresh full-roster sample, zero request/lease owners, unchanged UNKNOWN rows,
  matching epoch/source/manifest/roster, complete journal evidence and safety flags.
- The acceptance deadline is **1290 seconds from systemd's actual collector process
  start**: 900 + 300 + 90 seconds of bounded sampling/audit overhead. The overhead
  does not extend initialization or relax consumer freshness. A terminal/runtime
  failure stops observation immediately; a missing initialization proof cannot
  continue beyond 990 seconds even if an incorrectly live process remains.
- The outer transient supervisor has **RuntimeMaxSec=1800**, including preparation,
  one start and acceptance. **TimeoutStopSec=240** covers its independent exit hook:
  up to 60 seconds loading pinned components, 60 stopping, 30 verifying, 30 exporting
  final journal, plus 60 seconds allowance. No automatic restart is configured.
  These bounds are separate from the command's preparation/check stage below.
- Main-process SIGTERM/SIGKILL, outer timeout, interrupted/ambiguous start, denial,
  missing evidence or report failure cannot authorize a second start. ExecStopPost
  has a Git/evidence-independent stop fallback. Unconfirmed stopping is reported
  as manual review, never as stopped or accepted.

## Exact preparation commands — only after approval, in the existing root terminal

This creates a private package directory and private Git refs. It neither checks
out the server repository nor edits its branch, units, security settings or cache.
Keep the printed package path for the later commands; do not replace the entry
with a moving branch file.

```bash
set -eu
test "$(id -u)" -eq 0
repo=/opt/shadow-runner/btc-grid-state
package=e0534f1ae17e4d518c6e4aa7a433e22246834fea
for sha in "$package" 8cb92bd7e39251e1997e1b363a5e9500c8344268 f4e8ad4de377a19763f4803e4fd56882c27d887a 7bd133584a21d5cc2af1147594f24031e29fd635 db0d59e3f7a6f15d1f99a678c07c2336e4474867; do
  git -C "$repo" fetch --no-tags --no-write-fetch-head --refmap= origin "$sha:refs/hunter-bootstrap/$sha"
done
umask 077
package_dir=$(mktemp -d /tmp/hunter-binance-bootstrap.XXXXXXXX)
entry="$package_dir/entry.py"
git -C "$repo" show "$package:scripts/hunter_binance_bootstrap_upgrade_once.py" > "$entry"
printf '%s  %s\n' 664b9ac795d569d3898e80c5864ca261760be03e6bddcd1b3022744b8ac84229 "$entry" | sha256sum --check --strict
chmod 500 "$entry"
printf 'PINNED_ENTRY=%s\n' "$entry"
/usr/bin/python3 -I -B "$entry"
/usr/bin/python3 -I -B "$entry" --check
```

`--check` verifies source, installation, stopped processes, old evidence and fresh
scan, but opens no live SQLite and consumes no new attempt. It does not prove the
current owners are zero; that exact state check occurs after the durable new guard
inside `--run`, before any code change/start. Missing or changed evidence stops the
process for review; do not alter the evidence to satisfy admission.

## One explicitly approved start

Run this block once only after the new package, CI and independent review have been
accepted. It launches a detached service-manager-owned supervisor, with no PTY,
pipe or terminal-dependent child. It creates a new transient root unit; this is an
additional execution action the user must explicitly approve. No existing unit is
rewritten, enabled, reset or restarted.

```bash
test -f "$entry"
printf '%s  %s\n' 664b9ac795d569d3898e80c5864ca261760be03e6bddcd1b3022744b8ac84229 "$entry" | sha256sum --check --strict
systemd-run --unit=hunter-binance-bootstrap-f4e8ad4 --service-type=exec \
  --property=Restart=no --property=RuntimeMaxSec=1800 \
  --property=TimeoutStopSec=240 --property=KillMode=control-group \
  --property=StandardInput=null --property=StandardOutput=journal --property=StandardError=journal \
  "--property=ExecStopPost=/usr/bin/python3 -I -B $entry --cleanup" \
  /usr/bin/python3 -I -B "$entry" --run
journalctl --utc --no-pager --all -u hunter-binance-bootstrap-f4e8ad4.service -o short-iso
```

The permanent new guard is
`/opt/hunter-binance-stream/bootstrap-upgrade-once-f4e8ad4de377a19763f4803e4fd56882c27d887a.json`.
It binds the systemd invocation and a unique `upgrade-<32 hex>` evidence directory.
It is exclusive and created before live-state access. Any partial write, fsync
failure, failed state validation, preparation failure or completed attempt after
that boundary consumes the attempt. Do not remove either guard, reset a start
limit or rerun to obtain another attempt. The operation lock covers preparation,
supervision and explicit rollback.

## Readback and final authority

```bash
systemctl --no-pager show hunter-binance-bootstrap-f4e8ad4.service \
  -p LoadState -p ActiveState -p Result -p ExecMainCode -p ExecMainStatus -p ExecStopPost -p InvocationID
systemctl --no-pager show hunter-binance-stream.service \
  -p ActiveState -p SubState -p MainPID -p InvocationID
journalctl --utc --no-pager --all -u hunter-binance-bootstrap-f4e8ad4.service -o json
cat /opt/hunter-binance-stream/bootstrap-upgrade-once-f4e8ad4de377a19763f4803e4fd56882c27d887a.json
```

Read the bound evidence directory from that guard. `completion.json` and
`acceptance-report.json` are **provisional** until the independent hook finishes.
Success requires all of the following together:

1. The same supervisor invocation completed successfully, including its exit hook:
   main exit code `exited`, status `0`, overall unit result `success`. Inspect both
   the unit result and durable systemd journal; a successful transient unit can be
   garbage-collected, so missing live properties alone are not proof of success.
   Require the durable PID 1 success record: `_PID=1`,
   `UNIT=hunter-binance-bootstrap-f4e8ad4.service`, `INVOCATION_ID` equal to the
   new guard, and `MESSAGE` ending `: Deactivated successfully.`. It must follow
   the same invocation's `FINAL_SUPERVISOR_RECEIPT` from the exit hook. Retain the
   full JSON record, including `MESSAGE_ID`, cursor and monotonic timestamp.
   systemd's [unit success implementation](https://github.com/systemd/systemd/blob/v255/src/core/unit.c)
   identifies this terminal event; the Linux fixture prints the actual PID 1
   fields for every tested outcome. `Started`, main exit 0, a hook receipt alone,
   or merely finding no failure does not prove final success. Missing positive
   terminal evidence means UNKNOWN; do not start again to recreate evidence.
2. `supervisor-final.json` says `ACCEPTED_AFTER_NORMAL_SUPERVISOR_EXIT`, binds the same
   invocation, and its report path/SHA256 match the complete acceptance report.
3. No `failure-completion.json`, `supervisor-final-failure-*.json`, independent stop
   fallback or later failure exists for this invocation. A failure takes precedence
   over any earlier accepted-looking file, including a partially persisted final
   file. Check the complete unit journal, not only its last line.
4. The collector is still active with the accepted collector invocation. The final
   sample and audit match the fixed new source, manifest, roster and epoch; READY
   continuity is at least 300 seconds, owners are zero, historical UNKNOWN content
   is unchanged, and `journal.complete=true` without missing pages or record cap.

Failure reports preserve incomplete evidence and its error; no absence of evidence
counts as success. Keep the raw backup, consistent SQLite backup, source snapshot,
old/new manifests, old code, both guards and all reports. A stop timeout is followed
by independent PID/UID/cgroup verification. If that also fails, stop status remains
unconfirmed and requires manual review.

## Failure and rollback boundary

No automatic code rollback, second start, new HTTP probe or owner disposition is
performed after the attempt. Interrupted code exchange uses the reviewed helper's
code-only restore when the old/new halves can be verified; ambiguous bytes are
preserved for review. A stopped failed collector remains on the installed new code
until an explicit rollback is approved. Never restore the old DB, retired owners,
budget, manifest or guards from backup.

To request a stop through the supervised path, explicitly run
`systemctl stop hunter-binance-bootstrap-f4e8ad4.service`. The hook stops the collector.
If the supervisor has already disappeared, an explicit collector stop and separate
verification require review of the current state; do not infer stopped from absence
of the transient unit.

After separate rollback approval, with the exact evidence path from the new guard:

```bash
/usr/bin/python3 -I -B "$entry" --rollback /opt/hunter-binance-stream/upgrade-REPLACE_WITH_GUARD_EVIDENCE_ID
```

The entry refuses rollback while the supervisor is active or the collector is not
quiet. It verifies the exact old/new trees and receipt, restores only code/head,
checks cache bytes stayed unchanged, records rollback and never starts anything.
The new and old guards remain consumed; rollback does not authorize another start.

## Scope retained

SHADOW ONLY; real=false; capital_authority NONE_SHADOW_ONLY; V2 20000/17000/3000;
SingleWriter/CAS/generation/5-minute Monitor/dedup, native closed bars and 90-second
consumer freshness remain unchanged. There is no Worker fallback, Discovery
dispatch/selector switch, trading workflow, Sentinel/live Bybit modification,
historical-ledger rewrite, roster filtering, REST concurrency increase or single
topic isolation change. READY collection is not proof of consumer migration.
The original production no-frame cause is still unproven; this package cannot
promise recovery if a required topic remains quiet.
