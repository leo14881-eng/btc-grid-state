# Local review corrections and counterexample results

The remote draft PR98 still points to `3a20e3c8379f4724a12e4ad4b300146672b838bf`. That version has the review defects below. Its previous green CI does not validate the corrected local work. Do not merge the remote head as if it contains these changes. Local commit `251f3d55b3fe5928ef61efbc84023c3a70853d5f` and the original review bundle remain preserved; later commits retain them as ancestors.

## Functional diff against deployed main

| File | Concrete change and purpose |
|---|---|
| `research/hunter_lifecycle_v2.py` | Generation-bound source admission, exact Binance/Bybit receipts, independent hard-source assessment, closed-window weakness, partial live recovery veto, final position receipt and summary projection; rebound risk comparison explicitly UNKNOWN and SELL authorization closed |
| `research/hunter_shadow_trader_v2.py` | Invoke V2 lifecycle admission/review/final receipt, reject stale/older market before marks or protection, preserve exact original BUY metadata, finalize after actual capital allocation, reject repeated same-generation ADD; removed draft SELL_RISK_EXIT branch |
| `research/hunter_lifecycle_state.py` | Optional V2 closed-window clock for consecutive recovery/invalidation observations; existing V1 time-based default unchanged |
| `research/hunter_position_monitor.py` | Retain actual asset/BTC and 15m raw source receipts for position review |
| `research/hunter_early_signals.py` | Carry corresponding actual receipts through hourly Research into the unchanged capital-review candidate signal |
| `research/hunter_bybit_management.py` | Retain Bybit's own asset/BTC kline packets with identities and source windows; unavailable demand stays UNKNOWN |
| `research/hunter_v2_entry_theses.json` | Exact-match historical original BUY metadata, no fabricated condition, price, time or SELL |
| `scripts/hunter_monitor_persist.py` | Validate final per-position receipt and summary projection against generation before persistence |
| `scripts/replay_hunter_v2_lifecycle.py` | Read-only adapter using actual persisted current positions/scalars/books and previous published portfolio, with explicit missing-input boundaries |
| `scripts/test_hunter_history_bundle_guards.py` | Check frozen history bundle corruption, object inventory and actual-Git source verification, including rehashed forged content |
| `research/hunter_thesis_contract_review.py` | New offline-only typed TRUE/FALSE/UNKNOWN contract kernel; no production caller, no source-authentication claim, no trade authorization |

Tests and deterministic fixtures cover these paths. Documentation includes the frozen historical corpus and verifier, current main states, actual PROM arm/peak/exit sequence, V1 equivalence, source/risk design and explicit remaining gaps. No parameter raises 20k/17k/3k, no fixed percentage/time stop is introduced, no V1 policy or Sentinel code is edited, no production result or workflow is part of the diff against the integrated main snapshot.

## Same-input red / green reproduction

Six synthetic tests were rerun against an isolated copy of the exact old production source `3a20e3c` and the corrected local source, using identical current test inputs. Old source: **five failures and one error**; corrected source: **six passes**. Network calls are forbidden in each manager replay. These are unit counterexamples, not ENA historical executions or newly emitted production events. Exact input/source hashes and LF-normalized logs are retained in `hunter-v2-lifecycle-evidence/review_counterexamples_red_green.json` and its adjacent red/green logs. Original capture hashes are recorded separately from the normalized log hashes.

| Counterexample | Old draft failure | Corrected assertion |
|---|---|---|
| Missing candidate/supply evidence | Losing `SELL_RISK_EXIT` allowed | HOLD, exit=false, events empty |
| Same closed bars in three new wrappers | Repeated confirmation could create losing risk SELL | At most one closed-window confirmation, events empty |
| Old relative-source timestamp with new wrapper | Signal labelled FRESH | UNKNOWN, HOLD, no event |
| Current partial-bar recovery | `live_recovery_veto` absent | Recovery veto true, closed weak-bar measurement remains separate |
| Fresh hard book with unrelated stale signal | Early EVIDENCE_PENDING masked hard evidence | Existing hard rule evaluated from its own fresh book and documented hard SELL in synthetic replay |
| Geometry and five entry values used as risk model | Risk comparison labelled OBSERVED | UNKNOWN, original-thesis revalidation=false, risk exit=false |

Additional green regressions cover stale underlying BTC rows, cross-hour asset/BTC mismatch, actual consecutive closed windows, recovery followed by a missing-window gap, and final allocator ADD/rejection receipt with an identical duplicate replay. The allocator integration also runs through the real hourly source producer and capital review, without patching its acceptance gate.

The offline original-thesis kernel has 34 tests, including independent-review counterexamples for FALSE hiding incomplete evidence, implicit hard AND, wrong-position source, null input, future entry, unrelated old-window conflict, near-double source TTL and null aggregation. All were corrected before the final run. Hash validation is deliberately not described as external source authentication.

## Validation and remaining acceptance work

- Combined focused suite: **207 tests PASS**, 18.893 seconds. This includes the original 173 tests plus the 34 new offline tests; do not add overlapping individual runs to this count.
- Current ten-position adapter replay at main `112b9740d53dd7351756548e76e3d1e67efdd3c5`: no new SELL, repeated run unchanged, events/closed ledger/tranches unchanged; safety limits remain 20,000 / 17,000 / 3,000 and shadow authority only.
- Frozen ENA/PENDLE history: 1,538 publications, 3,076 target rows, 7,008 distinct decisions; legacy/persistent PP rules match all saved rows. Missing historical publication windows remain unknowable and are not filled with OHLC or invented SELL.
- Actual PROM adapter: exactly one historical positive-net PP SELL reproduced, no duplicate on replay. It is not proof of every historical full manager/CAS invocation.
- V1 synthetic before/after equivalence: eight unchanged cases. Shared helpers retain default behavior.
- Full Windows discovery is not accepted as full CI because Linux fcntl/Bash/O_NOFOLLOW checks cannot run here. Exact revised-head Ubuntu CI is still required after publication; old remote-head CI is not evidence for these revisions.
- Original eight-dimension thesis capture/authentication and future holding-risk model remain design work. The pure offline prototype cannot enable a production exit. A new versioned maintenance policy for legacy positions, any non-hard loss-exit permission and quantitative validation criteria need explicit policy decisions; no default is silently borrowed from entry RR.
- Publication of the evidence bundle awaits the existing upload approval. No retry, alternate upload interface, remote ref update, merge, deployment or production workflow dispatch was used to bypass it. Runtime main readback of the repaired implementation remains pending.
