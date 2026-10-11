# Hunter Monitor boundary repair

## Baseline and isolation

The three failures were reproduced on `fe00133adfae455b7599a5c2f416bef95140a6a7`.
The implementation was then rebased onto current main
`0fce78b355ced25cd6cebe7fb08b9ac72d095860`, including merged PR105, and its
publication guard was reread and used unchanged. Neither baseline contains an
`AGENTS.md`. The changed files have no intersection with PR101, PR105 or PR106.

No checked-in ledger, policy, V1 admission rule, Sentinel component, lock, runner
or persistence guard is changed. SHADOW only: `real_trading_enabled=false`,
`capital_authority=NONE_SHADOW_ONLY`; V2 remains 20000 total / 17000 ordinary /
3000 reserve. No deployment, merge, or server wrapper installation is included.

## Reproduced failures and resulting behavior

1. A closed observation subject without a quote raised
   `FAST_MONITOR_MARKET_DATA_MISSING CLOSED` before either lane ran. Required
   quotes now come from open holdings. Closed-only quote gaps are explicit in
   the Monitor result and position observation metadata. Missing open-holding
   quotes still abort; the venue-specific Bybit management gate is unchanged.
2. No open positions plus finished observations returned `NO_OPEN_POSITIONS`
   without `results`, causing `MONITOR_LANES_MISSING`. That branch now emits
   both formal empty lanes. The seven-file publication guard remains mandatory;
   mismatched summaries, invalid capital, and wrong generations still fail.
3. Two 5-minute candles could mark a 72-hour horizon complete because only a
   peak was required. Each horizon now needs a continuous range of completed
   candles, with explicit expected/observed/missing counts. Duplicate candles
   cannot fill a gap; contradictory duplicates, malformed timestamps, invalid
   highs and unfinished candles cannot certify coverage.

Valid coverage also requires a finite horizon return. NaN/Infinity or malformed
return metadata stays `BACKFILL_PENDING`, is excluded from completed horizons
and aggregate coverage, and cannot leak into reported maxima or averages.

## Coverage and continuation

Coverage is explicitly at **completed 5-minute candle** precision. Only candles
wholly inside each requested window are used. For a non-aligned timestamp, the
partial starting/ending candle is excluded and its duration disclosed in
`start_edge_excluded_ms` / `end_edge_excluded_ms`. Thus an aligned 72h window
requires 864 candles; a window offset by 30 seconds requires 863 eligible
candles and discloses 270s + 30s of edges. This does not claim tick-level extrema
at those edges, include future highs, or invent missing history.

One HTTP page (at most 1000 candles, 4s timeout) consumes one unit of the existing
per-lane request budget (default 8). Each visited position requests one page.
A constant-size checkpoint stores the contiguous cursor, counts and horizon
peaks, and resumes at the first gap across process restarts. The existing
rotation keeps other observations progressing. Monitor advances and persists
migration checkpoints before the portfolio write, leaving blocked manual-exit
holdings untouched; summary construction does not perform another backfill.

Legacy booleans/horizon records without coverage proof no longer certify
completion or authorize archival. Existing archives are not rewritten or
backfilled; unverified archived records remain provisional in aggregate
coverage. Bybit history stays explicitly unavailable until venue-matched
historical acquisition exists. Trade events, exit fills, realized PnL and
entry/exit decisions are not reconstructed by this observer.

## Validation and runtime acceptance

Offline reproduction and new regressions:

```sh
python -m unittest discover -s tests -p 'test_hunter_monitor_boundaries.py' -v
python -m unittest discover -s tests -p 'test_hunter_observation_history.py' -v
python -m unittest discover -s tests -p 'test_hunter*.py' -v
git diff --exit-code -- research/results research/hunter-capital-state.json
```

The PR's Ubuntu workflow runs the complete Hunter suite, including the merged
PR105 lock, CAS and generation tests. Some legacy filesystem tests encounter
Windows sandbox atomic-replace restrictions; Linux CI is the integration gate.
The empty-lane integration test executes the actual Monitor and scheduler-health
write, then the unchanged `persist()` validation/CAS/commit/push/readback path
against a temporary local bare Git repository. It checks byte-for-byte retained
ledgers, retry idempotence and rejection of missing lanes or a wrong generation.
Its `OFFLINE_TEST` trigger cannot publish a server primary proof.

After a separately authorized merge/deployment, runtime acceptance still needs
natural Monitor cycles showing: a closed-only quote gap alongside managed open
holdings; a valid empty two-lane publication and verified readback; checkpoint
progress across cycles under the request budget; permanent data gaps remaining
pending; and completion only after all eligible candles arrive. Confirm
unchanged historical trade events and capital constraints. PR105's server
wrapper is not installed by this PR, so its runtime acceptance remains separate.
