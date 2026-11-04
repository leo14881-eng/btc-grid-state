# Binance collector health and consumer readiness

Initial fixed main baseline: `20ed3ec97a50f91a9ba34b6bddf14849d9cc62de`.
This is an offline-reviewed, opt-in runtime contract, not deployment permission.
PR98, Sentinel, live Bybit, strategy, execution-price checks, V2 20000/17000/3000,
SingleWriter/CAS/generation/dedup, the five-minute Monitor and historical trading
ledgers remain unchanged. The old PR124 guard and upgrade supervisor stay sealed.

## One change, separately verifiable boundaries

1. **Transport and shutdown integrity.** Preserve the f4e8ad4 HTTP cancellation,
   after-GO drain/reap and durable ownership protections. Subscribe to the complete
   roster. Require current-session ACKs and fresh, explicitly observed matching
   PONGs or validated market frames from every shard. A quiet topic is not a dead
   shard. A dead shard, invalid protocol, time rollback, storage failure, denied or
   uncertain HTTP ownership still stops admission and enters bounded recovery.
   Stop producers before draining. On drain expiry persist every remaining market
   observation's topic, receipt time, native open time, exchange event time,
   canonical payload hash and reason; preserve
   recovery obligations. For each topic the final mutually exclusive accounting is
   `received = committed + duplicate + invalid + unprocessed_recorded`.
   Control frames have separate counters. A duplicate requires the same native
   open time, exchange event time and complete payload (including close flag).
   A conflicting key is invalid; a changed event is not a duplicate merely because
   OHLC values are unchanged. Failure to persist reconciliation cannot claim a
   complete final report. A hard process/host kill is not a graceful-drain success.

2. **Data evidence.** Keep service health separate from every consumer's unchanged
   freeze. Background history and topic repairs never promote REST to WS. Pending,
   failed, missing, expired, invalid-symbol and wrong-epoch data remain UNKNOWN.
   Connection health, instrument status, native Kline readiness and quiet REST
   verification share source/observed_at/verified_at/expires_at/result/reason and
   bind exchange=BINANCE, market=spot, symbol, interval, epoch/generation, roster
   hash and manifest hash. Fields irrelevant to one record type cannot stand in
   for that type's required fields. REST verification must occur after the current
   epoch starts; old historical candle bodies may survive but old proofs cannot.
   Quiet verification requires explicit fresh `exchangeInfo` TRADING status and an
   active REST read for that symbol/interval. Compare the same latest closed native
   candle, never a field-by-field equality race on the changing current candle.
   Mismatch requests a scoped repair, remains UNKNOWN and requires a later clean
   verification. Failed/timed-out/expired checks are unusable. A verified quiet
   label still cannot satisfy the current-epoch WS requirement or execution price.
   Across a native boundary, check/repair that topic's closed history; never invent
   an empty candle or infer its close from elapsed time alone.

3. **Acceptance and consumers.** The new read-only health supervisor requires 300
   continuous seconds of the same epoch, all ACKs, fresh shard evidence, advancing
   collector samples, bounded queue/repair work and no global stop. Its only success
   is `collector_accepted`; it also publishes full-roster data status and
   `publication_verified=false`. It never starts/stops a service or edits a guard.
   The old PR124 supervisor cannot consume this contract and must not be reused.

## Consumer impact (existing behavior retained)

| Consumer | Actual required cache scope | Degraded/UNKNOWN effect |
| --- | --- | --- |
| Discovery early signals | Complete Binance roster, 15m and 1h, including BTC | Any missing required topic rejects the complete generation; never label a filtered subset complete |
| Forward Research | Selected symbol, 1d | Missing facts for that request; no REST/Worker fallback |
| Monitor systemic Klines | BTCUSDT 5m | Missing BTC evidence remains HIGH/fail-closed; unrelated topic failures need not reject a qualified BTC window |
| Monitor position signals/prices/books | Existing rolling REST, ticker and book inputs | Not migrated by this patch; their own source/freshness checks remain mandatory |

The Monitor cannot manufacture a smaller required position set. Any future cache
migration must freeze the full set required by that invocation, including benchmark
dependencies, and separately validate execution-price freshness. Candle period is
never permission to relax the realtime price threshold. No new REST fallback is
enabled. A future explicit REST consumer contract must retain its real source and
LOCAL_REQUEST_CUTOFF clock, independent validity, unchanged semantic tests, and no
Worker fallback; it is not equivalent to this WS cache contract.

## Limits and acceptance cases

Keep the shared maximum four HTTP owners and 200ms minimum Kline admission spacing,
with weighted directory admission, 30s request bounds, denial/backoff and five
recovery failures. The public directory read has a fixed scope and bounded batches;
it does not expose trading endpoints or credentials. Quiet backlog cannot cause
unbounded polling or masquerade as complete validation. Expired status/verification
stays UNKNOWN. Metrics include shard evidence age, ACK coverage, seed/repair backlog,
quiet verification result/age/expiry, per-topic rejection reason and final receipt
partition. Health evidence never resets a failed data-readiness window.

Required offline cases: N quiet topics with healthy shards; an entirely silent shard;
explicit non-TRADING required symbols; crossing a native boundary without WS;
reconnect retaining historical bodies while rejecting old epoch/proofs; 300s health
acceptance with consumer UNKNOWN; quiet REST match/mismatch/expiry/failure/timeout;
normal versus deadline-limited shutdown drain, duplicate/conflicting-key/invalid
frames, durable discard and scoped recovery; late/rejected HTTP completion and
durable ownership failures. No public network, service manager, ledger or trading
workflow is exercised. Linux CI additionally verifies real loopback/socket/process
cleanup and guarded filesystem effects.

The incident proves incompatible observed freshness windows, not the exchange-side
cause of sparse updates, successful long-running boundary repair, or elimination
of every other fault. New tests provide only their stated offline evidence.

## Candidate operation and read-only acceptance

No deployment or restart is authorized by this patch. Do not run the old PR124
one-shot supervisor, remove its consumed guard, or invoke the initial-install
stage script over the existing installation. The example systemd drop-in only
adds `--health-supervision` and a 90-second stop bound; it is not installed here.
An operator must first obtain a separately approved update entry point, pin the
reviewed head, verify the complete runtime inventory and dependencies, and review
the drop-in plus a new one-shot supervision plan. Do not modify denied SSH/WSL,
filesystem paths, service identities, permissions, or consumer selectors.

After an independently approved operator has staged that exact head and started
the candidate, these commands are read-only (run from its code directory):

```sh
HEAD_SHA=$(cat /opt/hunter-binance-stream/release-head)
/opt/hunter-binance-stream/venv/bin/python -B -m scripts.hunter_binance_health_check \
  --manifest /var/lib/hunter-binance-stream/manifest.json \
  --source-head "$HEAD_SHA" --watch
systemctl show hunter-binance-stream.service -p ActiveState -p SubState -p MainPID
journalctl -u hunter-binance-stream.service --since '-20 min' --no-pager
```

The health command exits zero only after 300 seconds of advancing, same-binding
health samples. `DEGRADED`, `data_status=UNKNOWN` and `publication_verified=false`
can be an honest successful *collector* acceptance; none authorizes publishing
consumer results. Each consumer still needs its full original required set and
its original source, close, epoch and 90-second constraints. The checker makes no
claim that its fixed roster manifest is a fresh Discovery input.

Directory reads use at most 20 exact symbols and weight 20; Klines use weight 2.
Each work item has at most three failures per session, separate repair/quiet
budgets and 30/60/120-second backoff. Exhausted items remain UNKNOWN and are
reported by count, bounded sample and digest, with individual failure audit events.
This is an explicitly degraded data outcome, not a reason to restart healthy
shards. Any active work stalled beyond 45 seconds or nonempty queue without
commit progress for 30 seconds stops the session. Directory success cannot hide
stuck Kline work. A quiet mismatch schedules re-verification even if WS resumes.
Quiet proof expires at the next native boundary at the latest. Instrument expiry
is checked again when the consumer finishes reading.

At stop entry `health_phase=DRAINING` and the health record are persisted together;
proofs are revoked before socket close or after-GO HTTP cleanup. `connected` stays
internally writable for that epoch's bounded receipt drain, while both consumer
and health gates reject DRAINING. It becomes zero before final STOPPED reporting.
Deadline leftovers stay unusable until a later REST window contains their exact
valid native starts and was requested after receipt. Invalid/non-native starts
are never cleared by a range comparison. Hard-kill or failed persistence cannot
claim conserved graceful shutdown.

Offline checks (Linux, disposable checkout; no public-market IO):

```sh
python -m pip install -r deploy/hunter-binance-stream-requirements.txt
python -I -B -X utf8 scripts/check_binance_stream_offline.py
python -I -B -X utf8 scripts/check_binance_public_transport.py
python -I -B -X utf8 scripts/check_binance_health_offline.py
```

The health suite includes real loopback WebSocket PONG/close semantics on pinned
websockets 15.0.1; the transport suite exercises real child-process reap and lease
retirement, including directory responses. Local Windows loopback access was
denied by policy, so Linux CI is the evidence for those socket/process cases.
