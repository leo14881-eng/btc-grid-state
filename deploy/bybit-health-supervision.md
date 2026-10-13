# Bybit health supervision candidate

This release is an opt-in candidate, not an installation or a migration receipt.
The execution baseline is `de2735a79bae6d6cf2a5ab7a0490e614e2c24e82`.
The existing service stays on its original command until the optional drop-in is
explicitly installed through an approved maintenance entry point. No such write
entry point has been verified in this task. Do not retry the denied SSH/WSL paths,
change permissions, use another endpoint after a denial, or fall back to Worker.

The readback at main `9514d3aacea2e7bf0067d40a4e27f6ed74c20bd2` has
`captured_at_utc=2026-10-13T05:17:25.650910+00:00`, 141 successful signals and
zero signal failures, but still `OFFICIAL_BYBIT_V5_VIA_WORKER` with eight Kline
batches. That is not evidence of server-cache consumption.

## Scope and acceptance boundaries

- Keep every captured required base plus BTC, native `15` and `60` minute bars.
  The verified 141-base scope requires 284 topics. Four-hour signals retain their
  existing one-hour-bar formula. Never substitute quote subscriptions or reduce
  the roster to liquid symbols. A changed manifest must be reviewed as a new scope.
- `HEALTHY` or `DEGRADED` supervision permits a sustained connection to stay up;
  it does not grant data READY. A quiet topic gets bounded direct REST validation
  of the same closed native bar and current spot Trading identity. REST and pong
  never refresh its WS timestamps. Data still requires the complete frozen roster,
  original 90-second limits, exact source/epoch binding and confirmed closed bars.
- The existing host budget covers directory reads, initialization, repair and
  quiet verification. A 403, uncertain request, unreaped child or failed durable
  denial write stops for review. No alternate domain, proxy or Worker fallback.
- Session exit first revokes consumer readiness, stops admission, reaps producers
  and classifies received market frames as committed, duplicate, invalid or
  durably unprocessed. A multibar frame is one receipt. Keep every per-topic final
  record and the session partition digest. A killed or incomplete session is not
  a successful conservation receipt.
- The same release's Discovery consumer and ops reader are required. The distinct
  health connected marker makes an old reader return UNKNOWN. The reviewed CLI
  refuses to run its legacy collector against a health-contract cache. Do not
  remove health metadata, denial state, leases, receipts or the circuit breaker
  to force a downgrade or retry.
- No strategy, fee, V2 20000/17000/3000 allocation, SingleWriter/CAS/generation,
  five-minute Monitor, deduplication, historical ledger, trading workflow,
  Sentinel or Binance activation changes are part of this candidate. Keep
  `real_trading_enabled=false`, `capital_authority=NONE_SHADOW_ONLY`, SHADOW ONLY.

## Reproduce offline before requesting installation

Use the exact reviewed PR head, not a moving branch. On Linux with Python 3.10 or
3.12 and the repository's existing pinned dependencies:

```sh
python -m pip install -r deploy/hunter-bybit-stream-requirements.txt
python -I -B -X utf8 scripts/check_bybit_stream_offline.py
python -B scripts/check_bybit_public_transport.py
python -I -B -X utf8 scripts/check_bybit_health_offline.py
bash -n scripts/hunter_bybit_preflight.sh scripts/hunter_bybit_stage.sh
```

The health acceptance harness allows only loopback sockets and the fixed test
HTTP helper; SQLite and all test writes stay in temporary scratch. Passing these
fixtures does not measure exchange throughput or prove production permissions.

## Installation review, then one explicit supervised start

Use [the existing provisioning runbook](hunter-bybit-kline-install.md) to inspect
the actual identities, preprovisioned venv, shared SQLite directory and consumers.
`scripts/hunter_bybit_preflight.sh` is read-only. Stop at any denied or missing
prerequisite; this candidate does not create accounts or repair permissions.

The existing stage script supports a fresh installation only and refuses an
existing code directory, release receipt or unit. Do not repurpose it for an
upgrade. An existing installation requires a separately reviewed exact old-to-new
file plan, a stopped collector, preserved state and receipts, and an approved
write entry point. Neither path is authorized merely by reading this document.

After the exact release, same-version consumers and fresh original manifest are
installed through that approved plan, the optional file
`deploy/systemd/hunter-bybit-stream-health.conf` is the complete drop-in candidate
for `/etc/systemd/system/hunter-bybit-stream.service.d/90-health-supervision.conf`.
It changes only ExecStart's health flag and TimeoutStopSec to 90 seconds. The
original user/group, sandbox, shared state, `Restart=no`, KillMode and start limit
remain unchanged. The extra stop time allows bounded HTTP completion and durable
receipt writes; it is not a guarantee that force-killed receipts can be recovered.

The separate deployment approval must identify the release SHA, existing and new
files, actual service/consumer paths, manifest digest and topic count, preserved
state, one start, stop conditions and rollback review. Do not enable a consumer
selector or auto-start retry in the collector-only observation phase.

After that explicit approval, verify the effective unit before the single start:

```sh
systemd-analyze verify /etc/systemd/system/hunter-bybit-stream.service
systemctl daemon-reload
systemctl show hunter-bybit-stream.service -p ExecStart -p User -p Group -p Restart -p KillMode -p TimeoutStopUSec
systemctl start hunter-bybit-stream.service
systemctl show hunter-bybit-stream.service -p InvocationID -p MainPID -p ActiveState -p SubState -p Result
```

Archive journal records for that exact InvocationID, the installed source-head
receipt and full manifest. Require advancing health evidence for 300 seconds,
all original topics ACKed, current-connection pong/validated-frame evidence,
bounded queue/job progress and no durable global halt. Report quiet topics and
data UNKNOWN truthfully. An all-ACK connection or 141/141 old Worker snapshot is
not publication acceptance. If a stop is needed, do not loop starts:

```sh
systemctl stop hunter-bybit-stream.service
systemctl show hunter-bybit-stream.service -p InvocationID -p MainPID -p ActiveState -p SubState -p Result
```

Check the exact invocation's per-topic and session-final receipts, actual process
exit and conserved counts. Preserve failure evidence if any item is incomplete.

## Consumer acceptance remains a separate gate

The local check uses the installed same-version reader and a freshly captured
authoritative manifest; it does not publish or switch sources:

```sh
cd /opt/hunter-bybit-stream/code
/opt/hunter-bybit-stream/venv/bin/python -B -m scripts.hunter_bybit_stream_ops check --snapshot /var/lib/hunter-bybit-stream/manifest.json
```

UNKNOWN exits nonzero and is not repaired by that command. READY only certifies
that check's frozen cut. Before separately approved selector activation, prove
the actual consumer identity can read the shared state, install the same health
reader, and validate natural Discovery publication with exact cache source,
generation/manifest hashes, full required topic coverage and zero Kline Worker
batches. Directory/order-book/history REST remain permitted under their budgets.
Bybit currently has no holdings; future holding subscriptions and consumers need
their own verified demand before any management-cache claim.
