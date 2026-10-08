# Stock Shadow direct-server deployment and cutover

## Status and scope

These are deployment **templates**, an offline-tested launcher, and an operator
runbook. They do not establish that anything is installed, enabled, or running on
the server. The currently available server connection is read-only; installation,
identity/authentication setup, cutover, and real timer acceptance remain operator
steps requiring the appropriate authorization and a writable server route.

This deployment concerns only Stock Shadow. Do not edit, restart, disable, reuse,
or reconfigure Hunter, Sentinel, crypto units, their environment files, their
checkouts, their locks, or their credentials. Do not install a heartbeat takeover
or a fallback workflow dispatcher.

Preserve all of the following:

- `HYBRID_ENTRY_V1_POSITION_STATE_V3` and the already agreed one-month strategy
  freeze. Record the original freeze dates; migration does not restart the month
  or authorize changes to BUY/ADD/SELL thresholds or their semantics.
- The original portfolio, tranches, closed positions, and complete trade ledger
  on latest `main`. No empty initialization, reseed, historical rewrite, resetting
  null values, new cohort, or copying an old preview over the live ledger.
- Existing free/public API behavior and approved provider access. No paid feed,
  subscription, new provider, real brokerage order, or new API scope.
- Replay's actual hourly `:23` decision clocks and **20-minute market-data delay**:
  a five-minute bar is usable only when its close is at/before the delayed cutoff.
  This data-delay rule is distinct from the runner's 20-minute execution budget.
- Existing New York exchange-session, holiday, weekend, and early-close gates.
  These units never supply `--force`. A delayed launch uses the real current
  clock; it does not invent missed historical observations or backdate a run.
- The currently documented monitor behavior. Migration does not add BUY, ADD,
  or SELL functionality to the five-minute quote/health monitor.

## Layout and execution contract

| Path | Purpose | Owner/access |
| --- | --- | --- |
| `/opt/stock-shadow/bin/run-job.sh` | Reviewed installed launcher | Root-owned, not writable by service |
| `/opt/stock-shadow/venv/` | Dedicated approved Python and test dependency | Root-owned, read-only to service |
| `/opt/stock-shadow/source/` | Dedicated bare Git mirror | `stock-shadow`, writable for fetch only |
| `/etc/stock-shadow/stock-shadow.env` | Stock-specific environment only | Root-owned; service group can read |
| `/var/lib/stock-shadow/work/` | Disposable publication checkouts and temporary files | `stock-shadow`, private |
| `/var/lib/stock-shadow/writer.lock` | One lock shared by all three jobs and preview mode | `stock-shadow` |
| `/var/lib/stock-shadow/runtime/` | Persistent run receipts, logs, previews, operator evidence | `stock-shadow`, private |

The launcher refreshes the dedicated mirror's `main`, makes an independent clone
without shared/hardlinked objects, checks out that source detached, and invokes:

```sh
/opt/stock-shadow/venv/bin/python3 -m research.stock_shadow.server.runner JOB \
  --publish --runtime-dir /var/lib/stock-shadow/runtime
```

`JOB` is `main`, `monitor`, or `replay`. The Python runner, not an environment file,
binds source commit, writer, owner epoch, job, run ID, and generation. It enforces
the Git-authoritative owner fence and publication/readback checks. Configuration
authority is `.github/stock-runtime.json` on `main`, never a local heartbeat.

Preview uses `--preview` instead. It retains its isolated checkout under
`runtime/preview-checkouts/` and outputs under `runtime/previews/<generation>/`.
The checkout has a disabled push URL and a rejecting pre-push hook. Normal
publication checkouts are removed on exit. An inherited `PYTHONPATH` or
`PYTHONHOME` is removed so it cannot select code from another checkout.

`runtime/launches/<launch-id>/launcher.log` and `launcher-receipt.txt` remain after
cleanup. The runner additionally writes `runtime/run-<generation>.json`, including
publication verification or failure details. A launcher exit of zero alone is not
proof of a market run, ownership admission, successful publication, or readback.
Inspect the runner receipt and Git result manifest. A SIGKILL, host power loss, or
full filesystem may prevent a final receipt; treat unfinished records as unknown.

### Exact production timer contract

| Job | UTC timer | Lock policy | Runner budget | Service outer limit |
| --- | --- | --- | --- | --- |
| Main | Every hour at `:23:00`, all days | Wait at most 600 s | 20 min | 40 min |
| Monitor | Mon–Fri at `:02,:07,...,:57` each hour | Skip busy tick immediately | 6 min | 10 min |
| Replay | Mon–Fri at `22:30:00` | Wait at most 1200 s | 20 min | 45 min |

The outer limits include lock wait, fetching, startup, and cleanup; they do not
extend the runner's actual execution budget. Tests are inside the runner budget.
Main/replay lock timeout exits 75 with `lock_wait_timeout`; a monitor overlap is
recorded as `skipped_lock_busy`. Persistent contention is an acceptance failure,
not an instruction to bypass the lock. Timers have `Persistent=false`,
`RandomizedDelaySec=0`, and `AccuracySec=1s`. Missed downtime ticks are not replayed.
Systemd does not create parallel instances of the same already-active service.

All services run as `stock-shadow`, without added capabilities, with a read-only
system filesystem except the two stock paths above. They request `Nice=15`,
`CPUWeight=10`, `CPUQuota=50%`, `IOWeight=10`, `MemoryHigh=512M`, `MemoryMax=1G`, and
`TasksMax=64`. Confirm compatibility and capacity on the actual host; do not
silently weaken limits to complete acceptance. The preview template has the same
user, environment, sandbox, and resource limits, no timer, and no enable target.

## Staged operator runbook

Do not skip a gate or combine ownership changes with a data reset. Record the
operator, approvals, commit hashes, UTC timestamps, and evidence for each stage.
The epoch values below assume the first migration starts at epoch 1. If the
authoritative epoch is already higher, stop and plan monotonically increasing
epochs rather than reusing these values.

### Stock-only Cloudflare cron control (cutover preparation)

The cron-control patch must remain unpublished while the host is unavailable or
GitHub is still the intended primary. Publish it only at the coordinated cutover
checkpoint, after host readiness, `paused/2` readback and legacy-run drain. It sets
the stock Wrangler desired-state cron list to `[]`; it does not deploy anything
by itself. The stock deployment workflow has **only** `workflow_dispatch` (no
push/schedule trigger). The Hunter deployment push filters contain only Hunter
paths. Scheduler CI has read-only permissions. Stock main's existing
`research/stock_shadow/**` push filter can create a normal job on merge, which is
why paused authority and legacy workflow disablement must precede this merge.

The existing `Deploy Stock Shadow Scheduler` workflow now has `cron_control`:

- `cron_control=true, read_only=true`: inspect the precise stock schedules,
  unchanged deployment/settings, and fresh Git authority; never mutate.
- `cron_control=true, read_only=false`: the only write is one
  `PUT /accounts/{existing account}/workers/scripts/stock-shadow-scheduler/schedules`
  with JSON `[]`, followed by an independent GET requiring an actual empty list.
- Both modes require `expected_worker_exists=true`, the inspected
  `expected_worker_version` UUID, a fresh `expected_main_sha`, and unchanged
  stock-control source with `paused/2` throughout. The last accepted Worker
  version before this cutover was `3e4ba1b5-1de0-4ae1-8ed5-2e260b689803`; inspect
  again instead of assuming it remains current.
- Cron-control mode skips account-wide inventory, the GitHub dispatch token,
  Wrangler deployment, and secret upload. It uses the existing stock Cloudflare
  token solely for authenticated requests to this Worker's schedules/settings/
  deployments endpoints. Redirects are rejected and no raw token, account,
  request header, provider body, or exception is logged.
- The only accepted preexisting cron is
  `3,8,13,18,23,28,33,38,43,48,53,58 * * * *`; an already-empty list is idempotent.
  Unexpected schedules or deployment/binding changes stop the operation.
- A timed-out/uncertain PUT is never retried automatically. Only fresh GET
  reconciliation is attempted. Failure or ambiguous readback keeps the cutover
  unverified; do not advance ownership or enable timers.

Fresh GET verifies **stored configuration**, not delivery propagation. Cloudflare
documents up to 15 minutes for cron changes. Keep `paused/2`, maintain exclusive
operator control, and drain the three stock workflows during that window. After
at least 900 seconds, run the same cron-control **read-only** mode again and
require empty schedules, the same version/settings and unchanged authority.
Where available inspect scheduled-event evidence too; no events alone does not
prove deletion. The 10-minute workflow timeout does not cover this waiting period.
Reverify the host connection before committing `server/3`.

Cloudflare does not provide a cross-provider atomic CAS for this operation.
The shared Actions deployment concurrency group serializes this workflow's
modes; fresh Git/provider checks detect observed races but cannot prevent an
uncoordinated external operator changing Cloudflare between requests. No other
operator may deploy/edit this stock Worker or advance authority during the
operation. Never treat a stale heartbeat as a lease or permission to take over.

Retain the before/after receipt, source/run IDs, original singleton cron and
version as rollback evidence. Controlled failback must use monotonically newer
paused/GitHub epochs and the latest verified ledger. Re-enabling the old cron
requires a separately coordinated restoration of the exact original cron and
Wrangler desired state, with fresh provider readback and the same propagation
wait; this disable-only helper never restores it automatically. Do not restore
an old portfolio or trade file. An empty desired-state list is intentional:
omitting `triggers.crons` would leave existing remote schedules unchanged.

Provider references: [schedule PUT](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/subresources/schedules/methods/update/),
[schedule GET](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/subresources/schedules/methods/get/),
[cron removal and propagation](https://developers.cloudflare.com/workers/configuration/cron-triggers/#remove-a-cron-trigger).

### 1. Merge guarded execution with GitHub still the owner (epoch 1)

1. Review and merge the migration code, guarded stock workflows, exact runtime
   configuration, deployment templates, and tests together with:
   `owner=github`, `epoch=1`, `shadow_only=true`, `automatic_failover=false`, and
   `jobs=[main, monitor, replay]`.
2. Require the migration CI and complete stock tests to pass on that exact commit.
   Check that the strategy engines and canonical portfolio/trades have not been
   changed by the deployment-only work. Record original freeze dates and current
   authoritative ledger IDs/hashes and counts.
3. Inventory every stock entry point: the three GitHub workflows, queued/manual
   reruns, stock-only Cloudflare scheduler cron/manual dispatches, and any other
   documented stock dispatch source. Stop new legacy dispatches as needed.
4. **Drain legacy unfenced runs.** Runs queued/started against old workflow code
   can bypass the new owner gate. Wait for their terminal results or cancel them
   and verify they have actually stopped. A merged guard or an idle queue is not
   evidence that an older running writer is fenced. Check scheduled, queued,
   waiting, requested, pending, and in-progress runs, including reruns.

Do not proceed while any old unfenced stock writer can still publish. Keep all
Hunter and crypto scheduler paths unchanged.

### 2. Pause authority, then freeze the cutover baseline (epoch 2)

1. Commit only the authorized control change to `owner=paused`, `epoch=2` while
   preserving the other runtime fields. Fetch/read it back from remote `main`.
2. Disable the three legacy stock production workflows and stock scheduler
   dispatches after the drain. The workflow names are `stock-shadow.yml`,
   `stock-shadow-position-monitor.yml`, and `stock-shadow-replay.yml`. Leave the
   stock migration test workflow available. Disabling triggers is defense in
   depth; the owner/epoch fence is still mandatory.
3. Verify any new guarded GitHub invocation is non-admitted while paused; do not
   test an old unfenced revision. Verify all server production timers are absent
   or inactive. Drain any remaining guarded jobs before saving the baseline.
4. Fetch the resulting latest `main`. Save its exact commit, the runtime config,
   and exact portfolio/trades bytes outside the checkout under a private
   `runtime/cutover/` evidence directory. Record open-position count, tranche count,
   closed count, trade-event count, and file SHA-256 hashes. Preserve the complete
   stock result subtree as a read-only evidence snapshot if space permits.

This snapshot is evidence, not a future restore source. Every next generation
must start from fresh authoritative `main`, including on failback.

### 3. Provision stock-only paths and install inactive templates

This step is **not executable through the current read-only server connector**.
Obtain an authorized operator/server execution route first. User/group creation,
credential setup, new persistent access, package installation, and system changes
must follow their applicable approvals. Do not request, retrieve, print, copy,
or commit secrets as part of this runbook.

- Reuse an already suitable dedicated `stock-shadow` identity, or explicitly
  provision that system user/group with home `/var/lib/stock-shadow` and no login
  shell after approval. Never run these units as the Hunter account or root.
- Create the root-owned installation/bin directory and the service-owned source,
  state, work, and runtime directories with minimal permissions. Create the source
  mirror's parent/empty destination so the service user can populate it.
- Create a dedicated Python 3.12-compatible virtual environment and install the
  reviewed test dependency from the approved package source. Reproduce the CI
  versions and record them; do not install or upgrade packages in the Hunter
  environment. The launcher never installs dependencies itself.
- Establish only the existing approved Git authentication route for this service
  identity. A new key, token, OAuth grant, broader scope, or persistent credential
  placement needs approval. Read access alone does not demonstrate push access.
  Never put an HTTPS token/password in a remote URL or copy a Hunter credentials
  directory wholesale.
- Seed `/opt/stock-shadow/source` as its own bare mirror of the verified repository
  `https://github.com/leo14881-eng/btc-grid-state.git`, using that approved route.
  Do not share another checkout's Git objects or worktree. Verify it is bare and
  its `origin` resolves to the authorized repository without embedded secrets.
- Create `/etc/stock-shadow/stock-shadow.env` from the example, populated through
  the approved secure route with only the stock provider variables actually
  needed. Recommended mode is `0640`, owner `root:stock-shadow`. Do not set writer,
  epoch, source, job, run, generation, diagnostic-force, or Python path variables.

After those steps, the following are **operator-only examples**, from a verified
checkout of the reviewed deployment commit. They install files, not enable jobs:

```sh
sudo install -o root -g root -m 0755 deploy/stock-shadow/run-job.sh /opt/stock-shadow/bin/run-job.sh
sudo install -o root -g root -m 0644 deploy/stock-shadow/systemd/*.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/stock-shadow/systemd/*.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/stock-shadow-*.service /etc/systemd/system/stock-shadow-*.timer
sudo systemctl daemon-reload
```

Compare installed bytes/hashes with the reviewed commit. Confirm all three timers
remain disabled/inactive. Confirm the service user can read its environment,
execute its interpreter, use its approved Git route, and write only the intended
stock locations. If paths are customized, update and review both launcher config
and unit sandbox paths together; the environment does not broaden unit access.

### 4. Paused previews, code checks, and non-trading canaries

Keep authority at `paused/2` and production timers inactive. Run in the following
order, waiting for each preview to finish:

```sh
sudo systemctl start stock-shadow-preview@main.service
sudo systemctl start stock-shadow-preview@monitor.service
sudo systemctl start stock-shadow-preview@replay.service
```

Preview can call the existing approved data providers and can mutate only its
retained isolated copy. It must not publish or initialize a new live ledger.
Live preview output may differ from an offline fixture as market time advances.

Require all of these gates:

1. Full stock and migration tests pass from the exact source/venv. Run the
   deployment tests as well: `python -m pytest -q deploy/stock-shadow/test_deployment.py`.
   Confirm stale-code, changed-ledger, owner/epoch flip, rejected/lost push,
   cross-job serialization, atomic snapshot/readback, and off-session cases are
   covered by the migration/stock suites. Inspect receipts; do not substitute a
   hand-edited success record for a test.
2. Preview receipts identify source, owner epoch, job, run and generation; results
   stay under runtime and no preview commit reaches remote `main`. Push URL and
   rejecting hook remain in the retained preview checkout. Run source code is
   exactly the fetched source, with no inherited development import path.
3. Portfolio/trades satisfy the existing ledger validator. Historical events,
   original tranches/opened times, and closed history remain intact. No provider
   response failure becomes zero-valued invented accounting or a reset book.
4. Validate summary/review provenance against their actual portfolio generation
   and recompute open net P&L where the reporting audit requires it. A stale
   derived ADD P&L/report mismatch is a disclosed accounting limitation, not a
   reason to rewrite the ledger or an acceptable hidden “pass.” Resolve the
   acceptance decision explicitly before cutover.
5. Verify an off-session/non-trading canary against unchanged market gates. If the
   live date is in session, use the established offline fixtures; do not force an
   off-hours production run or change the clock/session policy to manufacture it.
6. Compare remote `main`'s live stock files with the paused baseline after every
   preview. They must remain unchanged. Inspect CPU, memory, exit status, API
   health, storage use, and surrounding Hunter service health without modifying
   those services.

Do not activate if a preview fails, receipts are missing, strategy/ledger scope
changes, a stock input/code change invalidates the source, or a resource/auth gate
is unresolved. Fix authorized deployment defects and repeat affected checks from
fresh `main`. Never promote preview files into production.

### 5. Explicit server ownership and activation (epoch 3)

1. Obtain the cutover approval and record the accepted preview/test evidence.
   Stop/drain preview instances, confirm legacy stock dispatches remain disabled,
   and verify no unrelated service configuration changed.
2. Commit `owner=server`, `epoch=3` to authoritative `main`; preserve the ledger
   and every other runtime safety field. Fetch/read back that exact control state.
3. Only then enable/start the three stock timers:

```sh
sudo systemctl enable --now stock-shadow-main.timer stock-shadow-monitor.timer stock-shadow-replay.timer
```

Do not run extra forced catch-up jobs. The first natural ticks fetch current
`main` and receive fresh generation IDs. The owner/epoch fence must still reject
GitHub writers and stale earlier server generations.

### 6. Real timer and publication acceptance

Do not call deployment complete on the basis of `active (waiting)` alone. Observe
at least one natural eligible timer invocation for **each** job, including the
actual weekday `22:30 UTC` replay, and an eligible session monitor/main execution.
An off-session no-op cannot establish in-session market-data success.

```sh
systemctl list-timers --all 'stock-shadow-*'
systemctl show stock-shadow-main.timer stock-shadow-monitor.timer stock-shadow-replay.timer \
  -p LastTriggerUSec -p NextElapseUSecRealtime -p ActiveState
systemctl show stock-shadow-main.service stock-shadow-monitor.service stock-shadow-replay.service \
  -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp
journalctl -u stock-shadow-main.service -u stock-shadow-monitor.service -u stock-shadow-replay.service --since 'today' --no-pager
```

For each real invocation, retain:

- Scheduled tick and actual start/end in UTC, service status, lock outcome,
  source commit, run ID, generation, owner and epoch.
- A successful `runtime/run-<generation>.json` with
  `status=PUBLISHED_AND_READ_BACK` and `verified=true`, plus
  `STOCK_POST_PUSH_READBACK_OK` evidence. Inspect failed/no-owner/skipped records
  separately; they are not successful market runs.
- A fresh remote fetch and `research/results/stock-shadow/runtime-JOB-v1.json`
  whose generation/source/epoch/job and file hashes match the runner receipt and
  actual remote snapshot. Check manifest/health outcome, not just latest commit
  author or a journal success string. Portfolio/trades must be one validated
  original-ledger continuation.
- Accurate summary/review links to the same portfolio generation, free-provider
  health, and no out-of-stock files in the result commit. Replay publishes only
  its result and execution manifest; it must not alter the portfolio/trades.
- No new legacy GitHub stock write, no duplicate writer, no prolonged lock
  starvation, and no material regression to the untouched Hunter workload.

If a readback acknowledgement is missing or a push result is ambiguous, inspect
remote commit ancestry and manifests before retrying. Do not blindly repeat an
engine generation or restore an earlier ledger. Remain in acceptance until all
required real events are observed, or stop for a specific blocker/decision.

### 7. Explicit failback, always through paused authority

No stale heartbeat, error counter, watchdog, or cloud outage may take ownership.
Failback is an operator decision using the latest ledger:

1. Commit `owner=paused`, `epoch=4` and read back remote `main`. This fences
   outstanding epoch-3 server generations before any GitHub writer is restored.
2. Disable/stop **only** the three stock timers and drain/stop active stock
   production/preview services. Retain receipts/logs and verify terminal states.
   Do not delete a lock held by a process. If the server is unreachable, document
   that fact and verify the authoritative fence; do not claim host shutdown.
3. Fetch the latest authoritative portfolio/trades and validate the complete
   ledger. Diagnose any ambiguous publication by remote readback. Never roll
   back result files or force an old snapshot over later valid events.
4. Verify the guarded GitHub workflows/tests at current `main`. Commit
   `owner=github`, `epoch=5`, read it back, then re-enable only the authorized
   stock workflow/dispatch paths. Keep server timers disabled.
5. Verify a real GitHub run starts from that current ledger, is admitted at epoch
   5, publishes only allowed stock files, and has successful authoritative
   readback. Continue observing the required stock schedules until ownership and
   operation are established. Any later migration uses higher epochs.

## Retention and operational gaps

- Stock job fetches use command-scoped `gc.auto=0` and `maintenance.auto=false`;
  disposable checkouts carry the same local settings. This prevents background
  repacks sharing the job's CPU quota or retaining its writer lock. Global Git
  settings and Hunter repositories are unchanged. Explicit maintenance remains
  available: schedule it separately on the stock source mirror after checking
  active stock jobs/locks and resource headroom. Objects can accumulate in the
  mirror, so monitor its disk/object count; this change does not install a new
  maintenance timer or delete objects. Disposable checkouts are still cleaned.
- Preflight regression failures publish runner-authored failure health only when
  the canonical checkout remains unchanged. Publication uses the existing owner,
  epoch, full input CAS, metadata-only manifest and authoritative readback; no
  partial ledger is published. Test mutations, fencing or CAS conflicts leave
  only the local failure receipt. The full regression and 180-second budget are
  retained; failure health never turns a failed run into success.
  Separate `main-preflight-health-v1.json` / `monitor-preflight-health-v1.json`
  preserve the last engine health records. `runtime-<job>-v1.json` carries
  `RUN_FAILED` with mode `preflight` for the failed generation; consumers must
  inspect that current manifest rather than treating an old engine SUCCESS as
  fresh. A later successful job replaces the manifest with RUN_COMPLETED;
  the older preflight file stays bound to its original source/run/generation.
  Failures before authoritative admission/fence/credential checks complete
  remain local launcher receipts; they do not acquire publication authority.

- Retain receipts, logs, cutover evidence, and preview outputs through the
  original freeze and migration/failback acceptance. Plan capacity and approved
  archival/retention separately; no automatic destructive cleanup is installed.
- Inspect only this stock namespace for orphaned work after a crash. Confirm no
  matching process or lock is active before an authorized cleanup. Never remove
  another job's lock or worktree to “unstick” a run.
- The local test suite exercises temporary Git repositories and stub runners; it
  does not test production credentials, real APIs, a service account, host
  systemd permissions/resource enforcement, production push rules, or real timers.
  Its systemd check validates syntax with a local executable in user mode; host
  system-manager validation of the unchanged installed units is still required.
- This change installs no automatic alerting or takeover policy. During staged
  acceptance the operator owns observation of failures, skipped/starved ticks,
  disk use, API health, and missing verified receipts. Any ongoing alert delivery
  destination/permission must be agreed separately.
