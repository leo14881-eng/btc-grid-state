# Shared V1/V2 post-exit reassessment (draft; not deployed)

This change removes the sell-price -3% reset / +1% breakout prerequisite from
post-liquidation BUY only. It does not change ADD, SELL, profit protection,
loss-exit policy, market-feature calculation, or the five-minute monitor's
existing-position-only scope.

`hunter_reentry.py` owns the shared lifecycle. `hunter_shadow_trader_v2.py`
captures new exit context and passes complete current source data to it from
the hourly BUY loop. V1's configured shared engine calls the same evaluator.
Approval means reassessment, never a BUY promise: V1 still uses broad discovery
and no total capital cap; V2 still requires Capital Review, local execution
checks, the unchanged EARLY anchor/chase checks, and 20000/17000/3000 accounting.

## Evidence and causes

Two ordered post-exit observations describe either continued strength with
rising price, or a sampled pullback followed by rising price and restored
strength. This is a trajectory relation, not a new fixed percentage, timed
cooldown, or profit claim. Existing health and positive 1h/4h relative momentum
plus acceleration supply trend confirmation. A one-observation spike, falling
second observation, or still-weak recovery cannot qualify.

Both observations require fresh source/signal/scan times after the exit, matching
generation, new signal content, and new normalized bid/ask content. Timestamps,
generation IDs, sequence IDs, and reference price are excluded from the book
content hash. Where available, exchange sequence must also advance. Bybit's
source timestamp must independently be fresh. Seen hashes and the observation
watermark persist through restart. Merely wrapping old data with new IDs fails.
No source collection or kline/return semantics changed.

For profit protection, compute current full-quantity liquidation using the OLD
tranches, execution venue, fees and buy/exit slippage. Net proceeds must exceed
the OLD persisted protected floor. A new BUY label or resetting a new position's
peak cannot clear that cause. The old EXITED lifecycle remains untouched.

Loss/hard-exit asset locks require independent fresh NORMAL market evidence,
no active admission circuit, healthy current evidence and explicit clearance
of every recorded cause. Liquidity causes use their existing hard thresholds;
identity needs the current matched Identity Audit's affirmative pass; supply
needs current verified supply evidence with its source. Unknown/manual causes
stay locked; omission of an old blocker is insufficient. This module does not
change loss-freeze policy. With PR102 present it uses
`loss_freeze.circuit_for_admission(state)`.

Legacy registry context may be read from exactly one matching existing closed
trade with original tranches. This only populates new registry metadata; it
does not rewrite closed trades, events, PnL, or protection history. Missing
original basis/cause remains UNKNOWN. Current post-exit samples establish their
own new sequence; they do not claim to reproduce a historical observation.

Audit reasons distinguish UNKNOWN, no new evidence, unresolved cause, trend
not restored, BUY review failure and insufficient capital. Registry and decision
audit retain content hashes, source watermarks and cost estimates.

## Complete-strategy label and PR102 integration

Exact note: **2026.0.10.11 新策略**. Stable version:
`hunter-2026-0-10-11-v1`. Only a newly executed BUY's position and BUY event get
these fields; ADD events and all historical records are untouched.

This PR alone deliberately cannot claim the complete strategy. Annotation
requires the shared PR102 `engine.loss_freeze` module with
`POLICY == LOSS_AND_MARKET_V1`, the shared risk admission adapter, and the
current lane's activated `loss_control.policy`. A legacy circuit still completing
its original recovery does not qualify. No date/env flag fakes readiness.

Integration reference reviewed: PR102 head
`de8283d32a54bff570f69dbc709e9fa81b3bd0c3`. Integrate its risk imports/adapters,
quarantine handling and monitor adapter unchanged. Resolve the shared engine's
reentry-function conflict in favor of this evaluator while keeping the PR102
admission circuit source. Run both full suites and combined label tests against
the exact combined head before parent approval. Neither draft may be deployed
as the complete release on its own. PR98/35 and Sentinel are excluded.

## Verification scope

Fixtures are explicitly synthetic; no claim of real profitable replay. Tests
cover continuation, recovery, false breakout, cost-aware PP refusal, unchanged
source payloads, future/stale/missing/old evidence, restart and duplicate cycles,
hard causes, both actual BUY loops, lane gate differences, original anchor,
capital, labels, and immutable historical records. Existing Hunter regression
tests cover Single Writer/CAS/generation and monitor behavior. All testing uses
offline/in-memory or temporary fixtures; no production portfolio is written.

SHADOW ONLY: real orders remain zero, capital authority NONE_SHADOW_ONLY. No
merge, deployment, scheduled job, or live trading workflow is authorized here.


## Independent-review fixes (source contract v2)

Contract mismatches require matching original chain/address and current official
plus independent contract evidence, using Identity Audit's existing source
validity contracts. A ticker-only/native-ID capital pass never resolves a
contract conflict. Generic ASSET_IDENTITY_MISMATCH is interpreted only when the
captured original audit identifies supported concrete causes. Missing original
contract proof remains UNKNOWN. Identity Audit and Capital Review only gain
provenance metadata; their existing pass/fail rules are unchanged.

Collectors now retain the original Binance REST 1h/4h asset/BTC and 15m micro
windows: open/close boundaries, OHLCV/quote values, observed time, endpoint and
confirmation flags. Live partial candles remain partial; formulas/sample rows
are unchanged. Reentry validates all five packets, content hashes, continuity,
source identity, freshness and post-exit coverage. Re-fetching an old completed
window or changing only a score/timestamp is insufficient. New window identity
with identical numbers is admitted as new evidence; normal trend conditions
still apply. Bybit reentry without this validated source contract stays UNKNOWN;
this change does not modify the separate Bybit collection work.

A genuinely new missing/invalid observation interrupts confirmation and creates
a restart watermark. Delayed packets before that watermark cannot restart it.
An omitted candidate in a new valid review also interrupts it. Duplicate/old
packets do not advance or interrupt the setup. The previous observation must
still satisfy the existing MAX_EVIDENCE_AGE_SECONDS contract at confirmation;
otherwise it seeds a new setup. No trading cooldown or new time threshold was
introduced. Consequently sparse hourly observations cannot bridge an expired
setup; the code does not invent evidence between collections.

## Common release attestation (supersedes lane-local label activation above)

Production labeling is disabled without an explicitly supplied
research/results/hunter-strategy-release.json. This task never writes that file.
The reviewed combined deployment owner supplies schema hunter_common_release_v1,
release_id, enabled=true, strategy_version, activated_at_utc, SHADOW-only fields,
components from hunter_strategy_release.component_receipt(engine), and separate
V1/V2 lane_activation_ids matching each loss_control.activated_at_utc.

Receipt hashes bind the actual engine, reentry, provenance, release gate, loss
module, source/identity/review producers and policy bytes. Both authoritative lane
portfolios must contain fresh evaluated runtime state, the current loss policy
and completed legacy migrations. A bare policy string, fake callable, stale
peer, old policy, unfinished other-lane migration, or partial/mismatched code
cannot label a BUY. The manifest is consumed read-only and does not enable
trading. Missing release evidence changes annotations only, never BUY/ADD gates.
The exact note and stable strategy_version are unchanged. Historical/ADD events
are never backfilled.

Replay ID/hash arrays remain durable per exit. The unused growing derived-signal
array is no longer appended; bounded compaction of the remaining replay sets is
deferred rather than risk accepting a previously consumed packet after pruning.
