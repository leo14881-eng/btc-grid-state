# Current main readback and acceptance boundary

Source: `a72291b192d1b8983f5671212dcc17fa05d7c7bf`; formal portfolio: `2026-10-10T22:01:38.181245+00:00`. This maps existing main fields. New PR receipts are not deployed. All ten current positions are open and HOLD, PP UNARMED, with no recorded hard invalidation. Full exact reasons are in `hunter-v2-lifecycle-evidence/current_formal_position_states.json`.

| Asset | Existing health / thesis | Confirmations | Recovery | Action |
|---|---|---:|---|---|
| NEXO | WEAKENING | 1 | LOSS_RECOVERY | HOLD |
| HUMA | WEAKENING | 1 | LOSS_RECOVERY | HOLD |
| HAEDAL | WEAKENING | 1 | LOSS_RECOVERY | HOLD |
| IO | THESIS_INVALIDATED | 5 | PERSISTENT_INVALIDATION | HOLD |
| STX | THESIS_INVALIDATED | 13 | PERSISTENT_INVALIDATION | HOLD |
| SYRUP | THESIS_INVALIDATED | 25 | PERSISTENT_INVALIDATION | HOLD |
| GIGGLE | THESIS_INVALIDATED | 10 | PERSISTENT_INVALIDATION | HOLD |
| ENA | THESIS_INVALIDATED | 25 | PERSISTENT_INVALIDATION | HOLD |
| PENDLE | THESIS_INVALIDATED | 14 | PERSISTENT_INVALIDATION | HOLD |
| ENJ | THESIS_INVALIDATED | 22 | PERSISTENT_INVALIDATION | HOLD |

The proposed replay marks all ten EVIDENCE_PENDING because original raw source packets were not retained. It preserves previously confirmed health separately; UNKNOWN is not a new failure or hard invalidation. No rebound SELL is authorized. Full original-thesis evaluation and an approved holding-versus-exit risk model remain unresolved.

The single authoritative full historical audit is frozen at `0f65e11751484a6efbe9fe3e1defe2c310795433`: 1,538 publications / 3,076 rows / 7,008 unique decisions. Earlier incremental JSONs are superseded historical checkpoints. The `history-0f65e117` bundle and primary repair report contain the reproduction procedure and limitations.

Runtime readback of this implementation is pending an approved merge and normal successful Monitor execution. PR98 remains draft, with no merge, deployment, production ledger change or workflow dispatch. Read the exact final head CI separately; previous head green checks do not validate later edits.

Final local focused run: 207 tests PASS, including 34 offline contract tests. The four historical gzip blobs were confirmed present in GitHub; user-approved publication is resuming. Exact revised-head Ubuntu CI remains pending; see the publication-resume report. The old PR head 3a20e3c green checks do not cover this revision. The new pure kernel has no production callers and cannot authenticate a historical thesis or authorize a trade. All ten current-position contract coverage checks are UNKNOWN because the complete original predicate manifest was not retained; this result is not a new health failure.
