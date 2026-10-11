# Portfolio lock change: acceptance and operating limits

This PR is SHADOW ONLY. It changes orchestration and diagnostics, not strategy,
capital limits, trade rules, or existing history. Do not merge or deploy based on
this document alone. The parent task reviews the PR and runtime authorization.

## Scope and authority

Monitor clone, fetch/checkout and its regression suite now run before admission.
Once admitted on the existing lock inode, it fetches main again, invalidates the
preparation if tested code/configuration changed, refreshes the disposable
checkout, and evaluates the current generation. The gate and computation run
only after this refresh. Final CAS, atomic seven-file publication and exact
authoritative readback remain required. A CAS retry releases the lock before
preparing a new attempt and never reuses old decisions.

Research keeps its input/generation guards and fresh two-ledger binding. Its
engine tests move before admission; capital review and its gate report are
recomputed immediately after binding, before either shadow trader. Runtime
configuration is a protected Research input. Test fixtures must supply that
configuration before their initial baseline, not introduce it as an unrelated
concurrent change.

The shared helper uses blocking kernel flock with the existing 120-second
admission timeout. This removes the old Research 200ms polling window; Linux
does not promise FIFO fairness. Each acquisition logs role, PID, generation,
base SHA, wait, phase and hold durations. The lock-file receipt is diagnostic,
not kernel ownership proof. It can be stale after SIGKILL. Never delete the lock.
Closing the supervisor's descriptor releases the lock only after all inherited
copies close. Research children inherit it, and cancellation kills only the
session/process group created for that step before the supervisor closes it.

## Work that still belongs inside the lock

- The fresh authority fetch, current generation gate and current-ledger binding.
- Monitor market/evidence requests selected from current holdings; their results
  feed lifecycle decisions. Existing per-request timeouts remain unchanged.
  The Monitor ticker request has a 25-second timeout. Other request counts depend
  on holdings, selected refresh symbols, venues and risk evidence; there is no
  constant full-cycle network count or latency guarantee.
- State CAS fetch/rebase/push and exact main readback. On the ordinary changed
  Monitor state path, the persistence helper performs two CAS fetches, one state
  push and one readback fetch (four remote Git operations). Fresh binding adds
  one fetch. Primary proof publication adds fetch, push, fetch (three remote Git
  operations) plus a local shared clone. Thus the successful first-attempt host
  path has eight remote Git operations after admission, excluding market HTTP.
  Retry races increase this count. A duplicate/no-change path performs fewer.
- Research final CAS/publication and readback similarly retain network calls.
  Research health publication happens after portfolio-lock release.

The existing Monitor systemd `TimeoutStartSec=240` covers preparation, waiting,
evaluation, retries and publication together. It is not a 240-second hold
allowance. Research retains the existing 90-second critical-phase budget and
now applies the remainder to final readback too; terminating a failed shell
allows up to five additional seconds of TERM grace before killing its group.
The Research service's outer timeout is 1800 seconds. Git subprocesses have no
new individual network timeout; these outer deadlines remain the bound. A
timeout after a successful remote push can leave published state with failed
verification, so later acceptance must inspect main rather than blindly rerun.

Monitor already records market/evidence/lifecycle timings. New persistence
events record snapshot loading, parsing, each Git operation, exact readback and
primary proof publication. Lock events capture the surrounding phases and
total hold. Capacity reports measure parsing, serialization, local Git commit,
push/readback and contention separately; nested timings must not be summed.

## Verification boundaries

Linux CI exercises real flock contention, wait timeout, process failure,
inherited descriptors, cancellation, protected CAS conflicts, harmless concurrent
diagnostic publication, duplicate execution and exact two-ledger readback.
Temporary local origins and fake/publication fixtures cannot place real orders.
Capacity runs use copies at 1x/2x/5x history size and immutable source hashes.
Their two workers model the shared publisher; they are not full live Research
and Monitor strategy runs. Scenario elapsed time includes setup and teardown.
Synthetic duplicated history is for byte-volume measurement only and must never
be treated as valid portfolio history or installed on a server.

Before deployment, check the final tested PR SHA is based on a newly read main
and preserves PR104. Verify only approved code/test/workflow/docs paths changed.
After an approved deployment, use the next scheduled cycles: obtain the actual
installed hashes, service termination status, new lock events, and both runtime
receipts. Confirm `real_trading_enabled=false`,
`capital_authority=NONE_SHADOW_ONLY`, generation agreement and exact readback of
the publication at its recorded SHA. A subsequent legitimate writer can change
latest main; compare each receipt to its own authoritative snapshot and ancestry.
Inspect duplicate-generation skips and pre/post histories for duplicate events
or rejected stale decisions. A green PR and an older successful timer cycle do
not complete these runtime checks.

## Minimal read-only server evidence bundle

Run in the already authorized server shell. This only reads status, hashes,
lock diagnostics, recent logs and current receipts; it does not start a service,
fetch/reset a checkout, deploy, mutate a ledger, or change a lock.

```bash
cd /opt/shadow-runner/btc-grid-state || exit 1
date -u
git rev-parse HEAD origin/main
git status --short
systemctl show hunter-position-monitor.service hunter-research.service \
  -p Id -p ActiveState -p SubState -p Result -p MainPID -p ExecMainStatus \
  -p ActiveEnterTimestamp -p ExecMainStartTimestamp -p ExecMainExitTimestamp \
  -p TimeoutStartUSec -p KillMode
sha256sum /usr/local/bin/hunter-monitor-runner.sh \
  /usr/local/bin/hunter-scheduled-job.sh \
  scripts/hunter_monitor_runner.sh scripts/hunter_job_runner.py \
  scripts/hunter_monitor_persist.py scripts/hunter_research_rebind.py \
  scripts/hunter_portfolio_lock.py
lslocks --notruncate --output PID,COMMAND,TYPE,MODE,PATH | grep -F hunter-position-monitor.lock || true
stat -c 'lock inode=%i bytes=%s modified=%y' /run/lock/hunter-position-monitor.lock
head -c 4096 /run/lock/hunter-position-monitor.lock
printf '\n'
journalctl -u hunter-position-monitor.service -u hunter-research.service \
  --since '2 hours ago' --no-pager -o short-iso | \
  grep -E 'HUNTER_PORTFOLIO_LOCK|TIMING|HUNTER_JOB_STEP|READBACK|READ_BACK|REBOUND|CAS_|TIMEOUT|ALREADY_PROCESSED|SUCCESS|Failed|failed' | tail -n 300
python3 - <<'PY'
import hashlib,json,pathlib
root=pathlib.Path('research/results')
for name in ('hunter-runtime-research-health.json','hunter-runtime-monitor-health.json','hunter-scheduler-health.json'):
    p=root/name
    if not p.exists():
        print(name,'MISSING'); continue
    raw=p.read_bytes(); d=json.loads(raw)
    keys=('status','started_at_utc','completed_at_utc','duration_seconds',
          'source_head_sha','main_readback_head_sha','main_readback_verified',
          'scan_generation_id','monitor_generation_id','current_generation_id',
          'last_successful_monitor_generation_id','real_trading_enabled',
          'capital_authority','shadow_only','real_order_count','error')
    print(name,hashlib.sha256(raw).hexdigest(),json.dumps({k:d[k] for k in keys if k in d},sort_keys=True))
    for s in d.get('steps',[]):
        if any(w in s.get('name','') for w in ('Bind latest','Revalidate capital','Persist Hunter','read-back')):
            print(json.dumps({k:s[k] for k in ('name','status','seconds','lock_wait_seconds','started_at_utc','completed_at_utc') if k in s},sort_keys=True))
PY
```

`origin/main` above is the server's cached ref, not independent freshness proof.
Use the connected GitHub read-only API to read current main and the receipt's
exact referenced commit separately. Do not include keys, tokens, environment
dumps or command lines with credentials in evidence.

At implementation time this executor's SSH attempt failed before authentication:
`ssh: connect to host 80.240.19.101 port 22: Permission denied`.
The supplied server screenshot does not establish this executor's connectivity.
No alternative channel or permission workaround was attempted.
