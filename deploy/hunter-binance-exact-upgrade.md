# Exact Binance code upgrade, without state recovery

This entry targets only `99f75edcf2c2f95d59e5cf797051442639dc64ce` →
`99836e5362340725f806d5df6df1ca9835065bdd`. It does not start, restart, enable,
stop or reload services. Bybit's installation, the unit, virtual environment,
strategy config, consumers, accounts and existing permissions are unchanged.
New code receives the same immutable modes as the current installation.

The default is a plan. `--check` is read-only. `--apply` and `--rollback` are
future maintenance actions through the already-authorized root terminal, not
actions taken by preparing/testing this entry. Do not use the dual-venue startup
helper or the fresh-install stage script for this upgrade.

```bash
python3 -B /path/to/reviewed/hunter_binance_upgrade.py
python3 -B /path/to/reviewed/hunter_binance_upgrade.py \
  --repo /opt/shadow-runner/btc-grid-state --check
```

The local repository must already contain both exact commit objects. The entry
never fetches, checks out, resets or updates any branch. It verifies the full
installed runtime whitelist against the old Git object and stages the same
whitelist from the new one. Only four files differ: three public/stream Python
modules and the offline transport check's expected test count. Other generated
results, portfolios and ledgers are excluded. A mismatch fails before swapping.

Before any mutation, require Binance inactive/failed with MainPID zero, no process
under its dedicated UID, and an empty service cgroup. Hold its existing collector
lock and every existing request lock. Unknown/live owners stop the operation.
The private `upgrade-<uuid>` evidence directory retains raw cache/WAL/SHM,
manifest and request-lock copies, hashes, a runtime file manifest, old
release-head, and a fsynced phase journal. Old, staged and rolled-back code trees
are recorded immutable siblings of `code`; same-parent renames preserve their
directory modes. Raw copies are evidence, not a claim of
SQLite snapshot consistency. Cache files must remain byte-identical during the
code upgrade. Existing markers, leases, halt and cooldown are never changed.

After review, the code-only operation is:

```bash
python3 -B /path/to/reviewed/hunter_binance_upgrade.py \
  --repo /opt/shadow-runner/btc-grid-state --apply
```

Success reports `UPGRADED_NOT_STARTED` and the evidence directory. The code/head
pair requires multiple filesystem renames, not one atomic transaction. Ordinary
swap failures restore the old pair; a killed process can leave an intermediate
pair. Keep the unit stopped and use the recorded directory to restore it:

```bash
python3 -B /path/to/reviewed/hunter_binance_upgrade.py \
  --repo /opt/shadow-runner/btc-grid-state \
  --rollback /opt/hunter-binance-stream/upgrade-EXACT_RECORDED_UUID
```

Rollback validates both saved and active content before moving anything, keeps
the new code as evidence, restores the old pair and leaves the current cache
bytes unchanged. It does not start the unit. Unknown hashes are never overwritten.
No new account, ACL, sudo path, security setting, package or dependency is needed;
an unavailable authorized writable entry remains a blocker.

## Orphan remains a separate blocker

One `denied=0` marker, one lease, `connected=0`, and zero topic timestamps do not
prove that HTTP completed, was never sent, or received no denied response. An
unlocked request lock proves absence of its supervisor's lock, not the outcome.
Process/cgroup absence can establish that no worker is still alive, but cannot
recover a lost HTTP response. The deployed schema contains no per-request final
response/reap receipt. Preserve the existing stop if contemporaneous evidence
cannot establish completion/no-GO for that specific request. This entry provides
no clear/reset/reinitialize/alternate-host operation. Any future reviewed state
recovery must identify the exact request and retained evidence separately;
upgrading code is not authorization or evidence to remove the marker.

Keep Binance stopped after this entry until that review is resolved. Then prepare
only Binance from a fresh natural-cycle scan, explicitly start only its collector,
and verify full-roster READY and subsequent natural publication before switching
consumers. SHADOW ONLY, real=false and NONE_SHADOW_ONLY remain unchanged.
