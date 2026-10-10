# Final verification supplement — 2026-10-10 UTC

Latest main read: `605d9bd8494946d9d663fd84d2370238524a4e9f` (18:26:01 UTC). Current formal portfolio: 18:23:50.307584 UTC, Research generation `20261010T181809695631Z`. The source implementation is unchanged from the previously audited main. PR98 remains a draft and is not deployed.

The historical audit now covers 1,536 published versions / 3,072 ENA-PENDLE rows / 6,998 unique decisions through this portfolio. All 670 legacy arithmetic rows and 2,402 persistent-protection rows replay exactly. The added two Monitor and one Research publications contain no SELL, event change, replay mismatch or new >600-second publication gap. Historical gap classifications remain unchanged. Full cycle artifact SHA256: `0f1640be51c3f05c2f1e7aeb87dea495a501e265d11b905f77792d29a94ba3a8`.

## Current formal state, not the new PR receipt

All positions are open, their latest actions are HOLD, and their persistent Profit Protection is UNARMED (peak/floor remain unset). No position has a recorded HARD_INVALIDATION. Main does not yet have PR98's `last_monitor_decision` or projection. This table maps existing health/degradation/recovery fields without pretending the new fields are deployed.

| Asset | health_state / thesis | degradation confirmations | recovery_state | Why HOLD |
|---|---|---:|---|---|
| NEXO | WEAKENING | 0 | LOSS_RECOVERY | Weak evidence does not authorize ordinary loss exit |
| HUMA | THESIS_INVALIDATED | 68 | PERSISTENT_INVALIDATION | Persistent weak signals; no evidenced hard loss exit |
| HAEDAL | THESIS_INVALIDATED | 9 | PERSISTENT_INVALIDATION | Persistent weak signals; no evidenced hard loss exit |
| IO | WEAKENING | 0 | LOSS_RECOVERY | Weak evidence does not authorize ordinary loss exit |
| STX | THESIS_INVALIDATED | 17 | PERSISTENT_INVALIDATION | Persistent weak signals; no evidenced hard loss exit |
| SYRUP | WEAKENING | 0 | LOSS_RECOVERY | Weak evidence does not authorize ordinary loss exit |
| GIGGLE | THESIS_INVALIDATED | 27 | PERSISTENT_INVALIDATION | Persistent weak signals; no evidenced hard loss exit |
| ENA | WEAKENING | 1 | LOSS_RECOVERY | PP net nonpositive; historical MFE is not a persistent live arm |
| PENDLE | THESIS_INVALIDATED | 30 | PERSISTENT_INVALIDATION | PP net nonpositive; historical MFE is not a persistent live arm |
| ENJ | THESIS_INVALIDATED | 11 | PERSISTENT_INVALIDATION | Persistent weak signals; no evidenced hard loss exit |

Exact published decision reasons and the new-code proposed projection are preserved in `hunter-v2-lifecycle-evidence/latest_research_actual_replay.json`. No current position qualifies for a newly authorized rebound exit from the available inputs. Rebound review after deployment must acquire and retain fresh complete evidence.

## Do not mix Research and Monitor generations

The generic Monitor adapter deliberately rejected the latest main because Research updated the portfolio after the last Monitor. This is a replay-input mismatch, not a failed production Monitor. Two separate replays then passed:

* The last actual Monitor publication `d3f5bc4cb00d2866ca4a7a56bed4d7cff0ea8425`, using its own saved scalars and full raw books: all ten positions, no new SELL, duplicate immutable, historical events/ledger/tranches preserved. Full original refreshed signal envelopes and candle receipts were unavailable and are explicitly labelled as an adapter.
* The latest Research publication, using its actual persisted scan/review/liquidity/supply inputs and the actual previous Monitor portfolio: all ten positions HOLD with `MARKET_SOURCE_NOT_NEWER_NO_LIFECYCLE_MUTATION`. The source scan is 18:18:09 UTC, older than the previous Monitor mark. The new guard prevents it from moving financial marks or lifecycle state backward. Repeated execution is identical; events, closed ledger, tranches, price marks and protection state remain unchanged. The current deployed main has no such new guard, so its current health values in the table above can differ from this proposed result.

The rejected Research attempt's proposed `position_state`/`thesis_status` are `EVIDENCE_PENDING`, and `hard_invalidation` is unknown (`null`), preserving the previously confirmed health separately. Missing or out-of-order evidence must not be reported as a fresh healthy/invalidated thesis assessment.

## Tests and remaining acceptance boundary

The first Ubuntu run executed 739 tests and found one outdated fixture that omitted market generation/time while intending to test an expired signal. The fixture now supplies a fresh market envelope while keeping the signal expired; no production rule was weakened. The focused rerun passed 26 tests. Full Hunter CI passed at code commit `ca2dcbaae2539c5492ea02b3d963675dc8cbcbed`: [stability validation](https://github.com/leo14881-eng/btc-grid-state/actions/runs/38075907238), [risk gates](https://github.com/leo14881-eng/btc-grid-state/actions/runs/38075907274), [scheduler migration](https://github.com/leo14881-eng/btc-grid-state/actions/runs/38075907239). Read final PR checks for the exact final head.

The real PROM fixture and synthetic conjunctive rebound tests validate different claims; neither is a deployed runtime proof. A real future qualifying rebound exit has not occurred under this draft. Main readback of new receipts, summary projection and accepted new generation must be checked after an approved merge and normal Monitor execution. No production workflow was dispatched to manufacture that proof.

Replay safety: zero real orders, `real_trading_enabled=false`, `capital_authority=NONE_SHADOW_ONLY`, 20,000 / 17,000 / 3,000 USDT, open cost 14,000 USDT, reserve usage zero. No core PP parameter, V1 behavior, Sentinel, scheduler, ledger history or production permission was changed. The rebound risk scenario is an explicitly documented strategy extension requiring review, not a claim of statistically calibrated expected return.
