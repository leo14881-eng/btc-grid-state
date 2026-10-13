"""Offline candidate boundary and transactional replay fixture, never a writer.

Formal lifecycle remains legacy. This module deliberately has no live scheduler
entry point, exchange order client, GitHub writer or canonical state path.
"""
import copy
import hashlib
import json
import threading

ACTIVE_ENGINE = "legacy"
CANDIDATE_ENGINE = "persistent_v1"
ALLOWED_CONTEXTS = frozenset({"UNIT_TEST", "BLIND_REPLAY"})


def require_candidate_context(context):
    if context not in ALLOWED_CONTEXTS:
        raise ValueError("CANDIDATE_NOT_APPROVED_FOR_FORMAL_EXECUTION")


def analyze_candidate(position, order_book, signal, *, context, now,
                      generation, exchange, symbol, sell_fee_bps, params=None):
    """Compose genuine input evidence with the candidate, without any writes.

    Caller-provided ADD bypass flags are removed: an unverified external ADD
    must follow the state engine's migration review rather than spoof preflight.
    """
    require_candidate_context(context)
    from research.hunter_v2_execution import estimate_full_liquidation
    from research.hunter_v2_protection import advance
    execution = estimate_full_liquidation(position, order_book,
        decision_timestamp=now, expected_exchange=exchange,
        expected_symbol=symbol, sell_fee_bps=sell_fee_bps)
    safe_signal = copy.deepcopy(signal)
    safe_signal.pop("add_preflight_verified", None)
    state, decision = advance(position, execution, safe_signal, now, generation, params)
    return {"position": state, "decision": decision, "execution": execution,
            "formal_state_mutation": False, "persist_verified": False}


def digest(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


class ReplayTransactionFixture:
    """In-memory CAS test fixture. Not a portfolio authority or durable store.

    Complete snapshot replacement under one lock models the atomic boundary;
    it does not claim to test server filesystem fsync or GitHub transport.
    Generation is an ordered integer test sequence, not a lexical production ID.
    """
    def __init__(self, snapshot, *, context):
        require_candidate_context(context)
        self._lock = threading.Lock()
        self._snapshot = copy.deepcopy(snapshot)
        self._completed = set()

    def read(self):
        with self._lock:
            value = copy.deepcopy(self._snapshot)
            return value, digest(value)

    def commit(self, expected_sha, proposed, *, transition_id,
               generation_sequence, fail_before_commit=False,
               fail_readback=False):
        with self._lock:
            if transition_id in self._completed:
                return "ALREADY_COMMITTED"
            if digest(self._snapshot) != expected_sha:
                raise ValueError("CAS_CONFLICT")
            old_generation = self._snapshot.get("generation_sequence", -1)
            if generation_sequence < old_generation:
                raise ValueError("STALE_GENERATION")
            value = copy.deepcopy(proposed)
            value["generation_sequence"] = generation_sequence
            digest(value)  # reject invalid/non-finite JSON before the boundary
            if fail_before_commit:
                raise RuntimeError("INJECTED_CRASH_BEFORE_COMMIT")
            self._snapshot = value
            self._completed.add(transition_id)
            if fail_readback:
                raise RuntimeError("PERSISTED_BUT_READBACK_UNVERIFIED")
            if digest(self._snapshot) != digest(value):
                raise RuntimeError("READBACK_MISMATCH")
            return "PERSIST_VERIFIED"
