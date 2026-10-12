# Two-venue Kline fresh installation — approved scope, execution pending CI

The user explicitly approved the account/directory/dependency/unit/empty-cache/
daemon-reload scope at 2026-10-12 12:39 UTC. No repeat permission request is needed
for that scope. Execution still requires passing Linux CI and the existing
authorized maintenance entry; no remote write entry is available to this task.
This is a prepared installation plan, not evidence that anything is installed.
The 2026-10-12 12:17 UTC inventory reported both dedicated users/groups, prefixes,
venvs, state directories and units absent. Do not ask for the same inventory
again. The installer checks those conditions immediately before its first write.
Use only the user's existing authorized maintenance terminal. No SSH/WSL retry,
new access route, security-policy bypass, live trading or strategy job is allowed.

## Exact approval scope

`venue` below means exactly `bybit` and `binance`, with separate accounts/state.

| Resource/action | Proposed setting |
| --- | --- |
| `hunter-{venue}-stream` user, `hunter-{venue}-data` group | New system identities; locked password; `/usr/sbin/nologin`; `/nonexistent` home path, no home creation; no sudo or extra memberships |
| `/opt/hunter-{venue}-stream` | New, root:root, 0755 |
| `code` below that prefix | Approved Python code and the two unit files only; root:root directories 0555; ordinary files 0444, executable files 0555; no results, portfolio or ledger files copied |
| `release-head` | root:root 0444; fixed collector release `99f75edcf2c2f95d59e5cf797051442639dc64ce` |
| `venv` | New, root-owned, no group/other write; system Python >=3.10 plus its existing venv/ensurepip support; no apt or global Python/pip upgrade |
| `/var/lib/hunter-{venue}-stream` | New, dedicated user:group, 2770; empty SQLite/lock initialized as that user with umask 0007 |
| `/etc/systemd/system/hunter-{venue}-stream.service` | New, root:root 0644, byte-identical to the reviewed collector release |
| New unit restrictions | NoNewPrivileges=true, ProtectSystem=strict, ProtectHome=true, PrivateTmp=true; ReadWritePaths only that venue's cache; Restart=no; control-group shutdown, 45s timeout; journal logging |
| Temporary staging | Root-owned private temporary wheel directory (0700, wheel 0400). Preparation uses a root-owned public-snapshot temporary directory (0755, JSON 0444). Only the installer's own temporary directories are cleaned up |
| Service manager | Verify both new unit files and one daemon-reload; no start, enable, restart, reset-failed, timer change or consumer source switch |

The existing Discovery, Research and position-monitor units are unchanged.
Empty User/Group fields on these system units are consistent with default root;
if their actual jobs continue to run as root, no group additions or sandbox
widening is needed. An internal privilege drop would require a separate access
review. Do not install the older example's SupplementaryGroups lines blindly.
New collectors cannot persist writes outside their own cache; their private
temporary storage and journal remain available. Root can still alter root-owned
code; this is protection from the service identity, not an immutable-host claim.

## Dependency and source verification

The installer reads only the fixed collector commit from the supplied local Git
repository. It never fetches, checks out, resets, pulls or updates that checkout.
Git archive entries must exactly equal the selected Python-code/unit file list;
traversal, links and special entries are rejected. The local source commit must
already exist. An approved preparatory `git fetch --no-tags origin <release>`
only acquires Git objects/FETCH_HEAD and does not overwrite the running checkout.

Dependency metadata was checked against [official PyPI 15.0.1 metadata](https://pypi.org/pypi/websockets/15.0.1/json).
The one download is the universal `websockets-15.0.1-py3-none-any.whl` from the
fixed HTTPS `files.pythonhosted.org` URL embedded in the script. Redirects and
proxies are disabled; size is bounded at 2 MB. SHA-256 must be:

```text
f7a866fbc1e97b5c617ee4116daaa09b722101d4a3c170c787450ba409f9736f
```

The downloaded wheel is installed into each venv with `--no-index --no-deps
--no-cache-dir`; no source build or other dependency download occurs. The
[pip configuration-file override](https://pip.pypa.io/en/stable/topics/configuration/#pip-config-file)
is set to the null device so system/user settings cannot redirect installation;
the child environment is an explicit allowlist, not an inherited environment.
Global/system Git configuration is also disabled. The
universal wheel does not provide the optional native accelerator. Production
queue/SQLite throughput still requires observation; offline tests make no SLA
claim. Existing system Python/CA/venv support is a prerequisite. If missing, stop
and separately approve the exact host package change; the installer cannot add it.

## Reviewed execution after permission approval

The installation scope is approved; run only after Linux validation. Pin the **installer
PR's independently reviewed full head and installer-file SHA-256** from the
handoff; these differ from the collector release and data-readback commit.
Retain the script at a root-owned, non-group/world-writable local path through
the authorized maintenance entry. Compare `sha256sum` with the handoff value
before execution. Do not pipe an unchecked download into a privileged Python.

```bash
# Use the verified existing repository and reviewed local installer paths.
REPO=/opt/shadow-runner/btc-grid-state
INSTALLER=/REPLACE/WITH/REVIEWED/hunter_kline_install.py
CODE_SHA=99f75edcf2c2f95d59e5cf797051442639dc64ce
sha256sum "$INSTALLER"
python3 -I -B "$INSTALLER"                       # plan only; no network/writes

# If the source object is absent, acquire it through the approved Git route.
git -C "$REPO" cat-file -e "$CODE_SHA^{commit}" || \
  git -C "$REPO" fetch --no-tags origin "$CODE_SHA"
test "$(git -C "$REPO" rev-parse "$CODE_SHA^{commit}")" = "$CODE_SHA"

# Only after the exact approval scope above is accepted:
python3 -I -B "$INSTALLER" --repo "$REPO" --apply
```

The success marker is `INSTALLED_NOT_STARTED <collector-release>`. No collector
has been started. Account/group creation, source extraction, dependency install,
owner/mode validation, new unit verification, new state initialization and
daemon-reload must all succeed before that marker appears. There is no implicit
repair of existing resources. Subcommands have 180s deadlines; interruption or
timeout kills/reaps the whole process group, including venv/ensurepip descendants.

## Fresh natural-cycle snapshots, with the correct source SHA

The following is a separate explicit preparation operation. Do it shortly before
an approved observation start; old manifests can age out. Both collectors must
be inactive. Fetch natural output once and freeze its full readback SHA:

```bash
git -C "$REPO" fetch origin main
READBACK_SHA=$(git -C "$REPO" rev-parse origin/main)
test "$READBACK_SHA" != "$CODE_SHA"
python3 -I -B "$INSTALLER" --repo "$REPO" --prepare-readback "$READBACK_SHA"
```

The helper reads the Bybit worker snapshot and Binance CEX scan from **that Git
commit**, verifies installed code/unit bytes against the collector release, and
validates both complete rosters/ages before writing either manifest. It passes
`--source-head "$READBACK_SHA"` to both original prepare operations. The release
SHA is explicitly rejected as a readback argument. Bybit requires <=900s snapshot
age; Binance requires <=30 minutes. A stale/incomplete snapshot is a stop, never
permission to relax validation or manually trigger Discovery/Research. Original
prepare takes each collector lock and archives source snapshots and receipts.
Preparation is not a cross-venue transaction: if the second write fails, retain
the first receipt and report exactly which phase completed; neither service is
started. Re-preparation needs explicit review, not an automatic retry.

Success is `PREPARED_NOT_STARTED readback=<data-sha> release=<code-sha>`. This
corrects the older Binance guide's use of `$EXACT_REVIEWED_SHA` in its prepare
example: **that argument must be the snapshot readback SHA, never the code SHA**.

## Failure, repeat runs, and later acceptance

Fresh installation only. Any existing dedicated account/group, prefix, state,
unit, unit drop-in or already-known systemd unit blocks installation before
provisioning. A repeated run stops without changing prior installed state. A
partial failure preserves every created account, prefix, unit and cache; do not
delete/reinitialize them to make the fresh-install check pass. Inspect the exact
failed phase and obtain a narrow recovery plan. No automatic rollback, permission
repair, denial/lease/boot reset, Worker fallback or orphan restart exists.

Installation approval does not silently authorize observation start or consumer
selection. Later acceptance requires: approved starts of these two sidecars only;
read-only complete-roster READY checks; confirmed contiguous closed bars; source
head/manifest/session/generation receipts; natural interval rollover and measured
queue/SQLite performance; then reviewed consumer configuration and natural-cycle
published cache-source receipts. See the existing [Bybit guide](hunter-bybit-kline-install.md)
and [Binance guide](hunter-binance-kline-install.md), applying the readback-SHA
correction above. Local READY alone still reports publication_verified=false.

The maintained boundaries are SHADOW ONLY, real=false, NONE_SHADOW_ONLY,
V2 20000/17000/3000, existing strategy/limits/ledger/SingleWriter/CAS/generation,
five-minute Monitor and dedup. No Sentinel or PR35/98 change is included.

## Offline evidence and its limits

`scripts/check_kline_install_offline.py` confines writes to one fresh temporary
tree, denies real network/process/account operations, credential paths and
existing results/ledger reads, and requires zero skipped tests/blocked effects.
Fixtures exercise real file extraction/modes but fake account, pip, systemd and
cache-initialization commands. They validate command contracts and stop behavior,
not successful privileged provisioning on the actual server. No server action,
live exchange call, strategy workflow, credential read or real ledger write is
part of these tests. Linux CI and the exact final hashes are recorded in the PR.
