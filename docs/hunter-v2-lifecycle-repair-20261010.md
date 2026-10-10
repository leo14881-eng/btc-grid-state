# Hunter V2 lifecycle repair - draft, not deployed

## Baselines and limits

Latest integrated source review: `a72291b192d1b8983f5671212dcc17fa05d7c7bf` (portfolio `2026-10-10T22:01:38.181245+00:00`). PR99 observability changes and intervening runtime publications are integrated; their files are retained unchanged from main. The full historical audit is deliberately frozen at `0f65e11751484a6efbe9fe3e1defe2c310795433`, through 2026-10-10 18:31:32.491156 UTC. Current-position replay and the frozen historical audit have distinct scopes. PR98 remains a draft; it has not been merged or deployed. Publication resumed after explicit evidence-upload approval; see hunter-v2-pr98-publication-resume.md for the integration boundary and read the exact remote-head CI before acceptance.

This change does not modify production results, historical events/ledgers, workflow schedules, V1 policy, Sentinel or PR35. Shared helpers preserve V1's default behavior. Shadow only: capital authority NONE_SHADOW_ONLY, real trading false, real order count zero, capital 20,000 / ordinary 17,000 / reserve 3,000 USDT. No fixed percentage or time stop, capital-pressure liquidation, or fabricated historical SELL.

## Historical Profit Protection: partially proven root cause

The complete controlled evidence bundle contains 1,538 first-parent portfolio publications, 3,076 ENA/PENDLE rows, 7,008 distinct decisions and exactly the two original BUY events. All 670 legacy arithmetic and 2,406 persistent protection rows match. Each target is present exactly once and open in every publication; no target tranche change, disappearance, reset or generated SELL is hidden by filtering. The generator independently inventories 4,655 first-parent commits and validates actual Git object hashes.

| Asset | First saved MFE >= 2% (2026-10-05 UTC) | Peak | Subsequent relevant observation |
|---|---|---|---|
| ENA | 11:56:17.090224, 0.2604, +3.0471% | 12:01:19.446565, 0.2608, +3.2054% | After 12:14:50, 81m34s publication gap; 13:36:24 price 0.2533, reference-model net -0.1590738222 USDT |
| PENDLE | 12:07:30.437823, 2.56, +2.0327% | 12:09:53.689543, 2.576, +2.6704% | 13:42:39 at 2.526: giveback 1.992839% (<2), net +4.319298; after 13:52:06, 56m24s gap; 14:48:30 at 2.464, net -20.3314526 |

The old theoretical protection reference levels were ENA ~0.2557460458 (+1.2054% raw) and PENDLE ~2.525820336 (+0.6704% raw). At the first saved threshold breach, net profit was already nonpositive. Neither saved breach should have generated a protection SELL under the actual positive-net condition. No saved cycle proves a positive executable protection exit was lost to CAS, generation, peak arithmetic or a branch defect.

At the first MFE crossings the legacy rule recalculated an armed predicate from MFE; a durable ARMED state did not yet exist. Persistent protection arrived with PR46 on October 6; both targets' first persisted states were already net negative and UNARMED. Historical MFE cannot retroactively arm live protection. All 2,406 persisted target rows remain UNARMED; they do not test a positive protection SELL.

The key gaps may contain an exit window. Unpublished execution, intragap books, exact runner checkouts and rejected writes cannot be recovered from the saved record. Version attribution is the published main tree, not proof of the runner's checkout in a concurrent write. The history replay covers pure PP predicates/state using saved exit estimates, not the full manager, raw-book recomputation, CAS, scheduler or all-system ledger. OHLC is not substituted for missing Monitor decisions.

## Final implementation

- Reuse canonical health/degraded/recovery/protection fields and persist a final `last_monitor_decision`; summary `position_monitor_states` exposes state, thesis, confirmation count, hard invalidation, PP arm/peak/net-USDT floor, recovery, action/reason/time and generation. The final allocator phase updates ADD or rejected HOLD after management, and repeated proposals in that generation do not add another tranche/event.
- Persist actual Binance asset and BTC 1h/4h rows and 15m receipts through both hourly Research and five-minute Monitor. Check source timestamps, values, symbols, continuity and cross-market window alignment. Rewrapping one closed window cannot produce additional degradation confirmations. Recovery and invalidation counts use consecutive closed windows; missing windows reset counts.
- Keep Bybit's own verified public spot packets and source-window identity; never relabel them Binance. Structure can be evaluated from its closed bars, while unavailable taker-buy demand remains UNKNOWN.
- Closed bars establish weakness; a current partial bar cannot establish weakness, but observed strong recovery vetoes a rebound review exit. Fresh hard-invalidation evidence is checked independently of unrelated stale signal data and retained with actual thresholds/values/source receipts.
- Reject stale/older market observations before financial marks or protection mutate. Preserve CAS, Single Writer, generation validation, summary consistency and five-minute scheduling.

**Rebound risk SELL is disabled.** LOSS_RECOVERY and REBOUND_EXIT_REVIEW persist evidence and reasons only. The previous support/resistance distance proxy was removed: geometric distance is not an approved holding-risk model. Numeric entry comparisons are not proof of full original-thesis revalidation. These dimensions explicitly remain UNKNOWN/PARTIAL_DATA, including when other inputs are fresh. Ordinary thesis invalidation cannot authorize a losing SELL. Implementing an approved original-thesis evaluator and risk model is an unresolved prerequisite for that future strategy extension.

## Positive PP proof and verification boundary

Nine actual published PROM cycles (eight Monitor, one interleaved Research) retain exact raw books. Pure liquidation/protection replay matches all nine. The manager adapter reproduces one SELL at 2026-10-10 04:31:25 UTC: estimated net +16.20245091 USDT; peak net +39.30538881 and floor 19.30538881. Duplicate execution emits no second SELL. The fixture and lifecycle source are checked with explicitly normalized LF SHA256 and Git blob hashes on Windows and Linux.

Current all-open adapter replay: ten positions, zero new SELL, unchanged event prefix, closed ledger and tranches; duplicate run is identical. Missing historical raw candles/relative packets yield EVIDENCE_PENDING rather than fabricated validation. This adapter is not a full original production invocation. Main does not contain the new receipts yet. Post-merge main readback of an actual accepted Monitor generation remains pending; no production workflow was dispatched to manufacture it.

Local focused regression currently passes 207 tests, including 34 new offline original-thesis contract tests, actual PP, source-time/window counterexamples, recovery, hourly producer-to-allocator, Bybit and circuit paths. Eight synthetic V1 before/after scenarios are identical. Full Windows discovery has Linux-specific fcntl/Bash/O_NOFOLLOW limitations; the exact final Ubuntu PR CI is the acceptance source, and its result must be read separately. Test counts overlap and are not summed. The [original-thesis contract design](hunter-v2-original-thesis-contract-design.md) and [risk model proposal](hunter-v2-rebound-risk-model-proposal.md) explicitly document source authentication, policy approval and activation work still outstanding.

## Reproduce

```sh
python docs/hunter-v2-lifecycle-evidence/history-0f65e117/generate_history.py --repo FULL_CLONE --ref 0f65e11751484a6efbe9fe3e1defe2c310795433 --out NEW_EMPTY_DIRECTORY
python docs/hunter-v2-lifecycle-evidence/history-0f65e117/verify_bundle.py --bundle docs/hunter-v2-lifecycle-evidence/history-0f65e117 --repo FULL_CLONE
python scripts/test_hunter_history_bundle_guards.py --bundle docs/hunter-v2-lifecycle-evidence/history-0f65e117 --repo FULL_CLONE --out NEW_EMPTY_TEST_DIRECTORY
python scripts/replay_hunter_v2_lifecycle.py --ref a72291b192d1b8983f5671212dcc17fa05d7c7bf
python -m unittest discover -s tests -p 'test_hunter*.py'
```

The portable bundle verifier checks all artifacts/counts; `--repo` additionally validates the first-parent inventory and actual Git source blobs. The generator fully reexecutes historical PP functions. Missing objects, wrong ref, changed target identity/tranches/events, duplicate target, source/state mismatch or partial history fail with nonzero exit. No network fetching is implicit. Seven evidence guard tests include forged cycle data with recomputed artifact hashes rejected by real Git comparison. Bundle manifest SHA256: `3e9fde2e4ac212e35bafb38e693c2525f90e03b34939b08ab9d4a62b3c42abf6`.

Primary source: [ENA first crossing](https://github.com/leo14881-eng/btc-grid-state/commit/daf86392e1da811e8939f2dd95fe68173b71edd7), [PENDLE first crossing](https://github.com/leo14881-eng/btc-grid-state/commit/1fd7c1d94283e69f29a0a54a7044ba7c8be959a1), [actual PROM exit](https://github.com/leo14881-eng/btc-grid-state/commit/ca41d741d43519281cf8bdb53e246d36f95c2b46). Every history row links its exact source commit/blob in the bundle.
