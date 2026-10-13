"""Isolated, in-memory snapshot-copy candidate; never a production selector.

Call run_codec_probe(generation) after the existing scratch seed, then use
installed_codec() around a complete diagnostic leg. All leg threads and all
candidate Frozen objects must be finished before the context exits. No payload
bytes leave this module through its report, and no external bytes reach marshal.
"""
from contextlib import contextmanager
import copy
import json
import marshal
import math
import sys
import time
from unittest.mock import patch

from research import hunter_binance_stream_consumer as consumer
from research.hunter_binance_stream_store import content_hash


def _marshal_encode(value):
    return marshal.dumps(value, 4)


@contextmanager
def installed_codec():
    """Patch only consumer's two bindings; never mutate the json module itself.

Objects created under this context cannot outlive it: Frozen uses consumer.json
again on export. The caller must join every diagnostic leg thread before exit.
"""
    old_encode, old_json = consumer._encode_json_tree, consumer.json
    if getattr(old_json, '_snapshot_codec_probe', False):
        raise RuntimeError('SNAPSHOT_CODEC_PROBE_MUST_NOT_NEST')
    authority = object()

    class OwnedBytes(bytes):
        def __new__(cls, value, token):
            if token is not authority:
                raise TypeError('SNAPSHOT_CODEC_PRIVATE_PAYLOAD_REQUIRED')
            return super().__new__(cls, value)

    def encode(value):
        # Same exact-type, unaliased, acyclic, finite gate as _encode_json_tree.
        # marshal supports more types; none of those widen this fast path.
        pending = [value]; seen = set()
        while pending:
            item = pending.pop(); kind = type(item)
            if kind is dict or kind is list:
                identity = id(item)
                if identity in seen:
                    return None
                seen.add(identity)
                if kind is dict:
                    if any(type(key) is not str for key in item):
                        return None
                    pending.extend(item.values())
                else:
                    pending.extend(item)
            elif kind is float:
                if not math.isfinite(item):
                    return None
            elif kind not in (str, int, bool, type(None)):
                return None
        try:
            return OwnedBytes(_marshal_encode(value), authority)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return None

    class JsonProxy:
        _snapshot_codec_probe = True

        def __getattr__(self, name):
            return getattr(old_json, name)

        def loads(self, value, *pos, **kwargs):
            if type(value) is OwnedBytes:
                if pos or kwargs:
                    raise TypeError('SNAPSHOT_CODEC_DOES_NOT_ACCEPT_JSON_HOOKS')
                return marshal.loads(value)
            # Ordinary JSON inputs retain JSON behavior. Raw external bytes,
            # including another context's marker type, are never unmarshalled.
            return old_json.loads(value, *pos, **kwargs)

    consumer._encode_json_tree = encode
    consumer.json = JsonProxy()
    try:
        yield
    finally:
        consumer._encode_json_tree = old_encode
        consumer.json = old_json


def _assert_copy(original, copied):
    """Compare exact types/order/value and mutable graph identity to deepcopy."""
    forward, reverse = {}, {}

    def visit(left, right):
        assert type(left) is type(right), (type(left).__name__, type(right).__name__)
        if isinstance(left, (dict, list)):
            assert left is not right, 'MUTABLE_COPY_ALIASES_INPUT'
            identity, copied_identity = id(left), id(right)
            if identity in forward:
                assert forward[identity] == copied_identity, 'ALIAS_OR_CYCLE_CHANGED'
                return
            assert copied_identity not in reverse, 'DISTINCT_CONTAINERS_COLLAPSED'
            forward[identity] = copied_identity; reverse[copied_identity] = identity
            if isinstance(left, dict):
                assert list(left) == list(right), 'DICT_ORDER_CHANGED'
                for (key, value), (other_key, other_value) in zip(left.items(), right.items()):
                    visit(key, other_key); visit(value, other_value)
            else:
                assert len(left) == len(right)
                for value, other in zip(left, right):
                    visit(value, other)
        elif isinstance(left, tuple):
            assert len(left) == len(right)
            for value, other in zip(left, right):
                visit(value, other)
        elif type(left) is float:
            assert (math.isnan(left) and math.isnan(right)) or left == right
            if left == 0:
                assert math.copysign(1, left) == math.copysign(1, right), 'SIGNED_ZERO_CHANGED'
        else:
            assert left == right
    visit(original, copied)


def _snapshot(value):
    return dict(status='READY', windows={'btcusdt@kline_5m': {'rows': [[1, '2', 3]]}}, extra=value)


def _oracles():
    checks = []
    unicode_text = '\x00\u4e2d\U0001f642e\u0301\ud800\udfff'
    values = dict(z=None, a=[True, False, 0, 2**130, -(2**130), -0.0, 0.0,
        math.nextafter(0.0, 1.0), sys.float_info.max, -sys.float_info.max],
        text={unicode_text: unicode_text, 'last': [[], {}]})
    original = _snapshot(values)
    frozen = consumer.Frozen(original, checked_at=1)
    assert isinstance(frozen._snapshot_json, bytes)
    first = frozen.generation; second = frozen.generation
    _assert_copy(original, first); _assert_copy(first, second)
    original['extra']['z'] = 'mutated input'
    first['extra']['text']['last'][0].append('mutated export')
    second['windows']['btcusdt@kline_5m']['rows'].clear()
    again = frozen.generation
    assert again['extra']['z'] is None and again['extra']['text']['last'][0] == []
    assert len(again['windows']['btcusdt@kline_5m']['rows']) == 1
    checks.append('plain_types_order_unicode_surrogates_bigint_finite_signed_zero_and_copy_isolation')

    shared = []; cyclic = []; cyclic.append(cyclic)
    class Values(list):
        pass
    class Custom:
        def __deepcopy__(self, memo):
            return {'custom_deepcopy': ['preserved']}
    alternatives = [dict(one=shared, two=shared), cyclic, (1, 2), {1:'int', ('k',):'tuple'},
                    Values([1, {'nested': []}]), Custom(), float('nan'), float('inf'), -float('inf')]
    for value in alternatives:
        original = _snapshot(value)
        frozen = consumer.Frozen(original, checked_at=1)
        assert frozen._snapshot_json is None, 'NON_JSON_GRAPH_ENTERED_FAST_PATH'
        expected = copy.deepcopy(original)
        _assert_copy(expected, frozen.generation)
        _assert_copy(frozen.generation, frozen.generation)
    checks.append('alias_cycle_tuple_keys_subclass_custom_deepcopy_nonfinite_fallback')

    # Same failure classes as the old encoder retain the existing deepcopy path.
    for error in (TypeError('codec'), ValueError('codec'), OverflowError('codec'), RecursionError('codec')):
        with patch(__name__+'._marshal_encode', side_effect=error):
            original = _snapshot({'nested': []})
            frozen = consumer.Frozen(original, checked_at=1)
            assert frozen._snapshot_json is None
            _assert_copy(original, frozen.generation)
    checks.append('encoder_error_deepcopy_fallback')

    assert consumer.json.loads('{"regular":1}') == {'regular': 1}
    assert consumer.json.loads(b'{"regular":1}') == {'regular': 1}
    with patch.object(marshal, 'loads', side_effect=AssertionError('EXTERNAL_BYTES_UNMARSHALLED')):
        try:
            consumer.json.loads(b'N')  # A marshal None payload, not a marked cache value.
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError('EXTERNAL_MARSHAL_PAYLOAD_ACCEPTED')
    checks.append('external_bytes_never_unmarshalled_and_json_module_unchanged')

    frozen = consumer.Frozen(_snapshot({'nested': []}), checked_at=1)
    error = consumer.CacheReadError._from_frozen('fixture rejection', frozen)
    _assert_copy(frozen.generation, error.generation)
    error.generation['extra']['nested'].append('changed error')
    assert frozen.generation['extra']['nested'] == []
    checks.append('owned_error_generation')
    return checks


class _NoDelivery:
    def finish_read(self, *args, **kwargs):
        raise RuntimeError('CODEC_PROBE_HAS_NO_LIVE_DELIVERY_AUTHORITY')


def _measure(operation):
    wall = time.perf_counter_ns(); cpu = time.thread_time_ns()
    result = operation()
    return result, dict(wall_us=(time.perf_counter_ns()-wall)//1000,
                        thread_cpu_us=(time.thread_time_ns()-cpu)//1000)


def _measure_generation(generation):
    payload, encoded = _measure(lambda: consumer._encode_json_tree(generation))
    assert payload is not None, 'REAL_GENERATION_MUST_QUALIFY_FOR_COPY_PROBE'
    copied, decoded = _measure(lambda: consumer.json.loads(payload))
    cut = generation['generation_end_ms']
    frozen, constructed = _measure(lambda: consumer.Frozen(generation, checked_at=cut,
        reader=_NoDelivery(), fence={}, names=list(generation['windows']),
        clock=lambda: cut, started_at=cut))
    exported, export = _measure(lambda: frozen.generation)
    _assert_copy(generation, copied); _assert_copy(generation, exported)
    _assert_copy(exported, frozen.generation)
    digest = content_hash(generation)
    assert content_hash(copied) == digest and content_hash(exported) == digest
    # Untimed negative oracles: a faster copy must not bypass either hash gate.
    tampered = dict(generation, generation_sha256='codec-probe-tampered')
    try:
        consumer.Frozen(tampered, checked_at=cut, reader=_NoDelivery(), fence={},
            names=list(generation['windows']), clock=lambda: cut, started_at=cut)
    except ValueError as error:
        assert str(error) == 'BINANCE_FINAL_GENERATION_HASH'
    else:
        raise AssertionError('CONSTRUCTOR_ACCEPTED_TAMPERED_GENERATION_HASH')
    first_name = next(iter(generation['windows']))
    frozen._generation['windows'][first_name]['rows'][0][1] = 'codec-probe-tampered'
    try:
        frozen._validate_at(cut, list(generation['windows']))
    except ValueError as error:
        assert str(error) == 'BINANCE_CACHE_VALIDITY_MISSING_EXPIRED_OR_MISMATCHED'
    else:
        raise AssertionError('DELIVERY_VALIDATION_ACCEPTED_MUTATED_PRIVATE_WINDOW')
    assert content_hash(generation) == digest, 'PROBE_MUTATED_INPUT'
    return dict(encode=encoded, owned_decode=decoded, frozen_constructor=constructed,
        export=export, cache_bytes=len(payload.encode('utf-8')) if type(payload) is str else len(payload),
        constructor_scope='Actual Frozen hash/proof validation; no live delivery check; sentinel rejects finish_read',
        copy_and_content_hash_oracle=True, constructor_rejects_bad_generation_hash=True,
        validation_rejects_mutated_private_window=True)


def run_codec_probe(generation):
    """One uninstrumented copy sample per codec on the same already-seeded input.

    This does not seed, touch SQLite, change thread scheduling, or establish
    consumer readiness. Return only JSON-safe metrics and completed assertions.
    """
    old_encode, old_json = consumer._encode_json_tree, consumer.json
    old_loads, old_dumps = json.loads, json.dumps
    baseline = _measure_generation(generation)
    with installed_codec():
        checks = _oracles()
        assert json.loads is old_loads and json.dumps is old_dumps
        candidate = _measure_generation(generation)
    assert consumer._encode_json_tree is old_encode and consumer.json is old_json
    try:
        with installed_codec():
            raise RuntimeError('PROBE_RESTORE_SENTINEL')
    except RuntimeError as error:
        assert str(error) == 'PROBE_RESTORE_SENTINEL'
    assert consumer._encode_json_tree is old_encode and consumer.json is old_json
    return dict(scope='ISOLATED_CODEC_CANDIDATE_NOT_A_FIX_OR_ACCEPTANCE',
        samples_per_codec=1, same_input_generation=True,
        qualification='Exact plain unaliased acyclic JSON tree, finite floats; otherwise original deepcopy',
        storage='Private in-process self-produced marshal format 4 bytes; no external bytes unmarshalled',
        measurement='thread_time_ns CPU and perf_counter_ns wall; fixed default-then-candidate order; no speed assertion',
        oracle_checks=checks+['context_restoration_on_normal_and_exceptional_exit'],
        baseline_json=baseline, candidate_marshal=candidate)
