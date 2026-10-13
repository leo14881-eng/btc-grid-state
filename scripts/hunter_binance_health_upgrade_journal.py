"""Pure parser for the exact PR126 health journal contract.

Caller supplies journalctl JSON lines including MESSAGE, __REALTIME_TIMESTAMP and
_SYSTEMD_INVOCATION_ID, and an independently verified expected installation scope.
No commands, files, state, clocks or network are accessed. Receipt conservation is
an audit property, never collector acceptance or consumer READY.
"""
import hashlib
import json
import re

CONTRACT = 'binance_health_v1'
COUNTERS = ('received', 'committed', 'duplicate', 'invalid', 'unprocessed_recorded')
TOPIC_FIELDS = {'event', 'epoch', 'topic', *COUNTERS, 'revisions_changed', 'last_rx', 'conserved'}
FINAL_FIELDS = {'event', 'epoch', *COUNTERS, 'complete', 'controls', 'control_receipts',
                'raw_application_frames', 'raw_frames_conserved', 'unattributed_invalid',
                'topics', 'reason', 'receipt_partition_sha256'}
HEALTH_FIELDS = {'event', 'contract', 'epoch', 'roster_hash', 'manifest_sha256', 'source_head',
    'observed_at', 'epoch_started', 'service_status', 'all_ack', 'topics', 'shard_topics',
    'shards', 'queued', 'processed', 'active_repairs', 'seeded', 'pending_seed',
    'completed_work', 'work_failures', 'halt', 'cooldown_active', 'publication_verified',
    'shadow_only', 'real_trading_enabled', 'capital_authority', 'work_progress',
    'exhausted_work', 'exhausted_count', 'exhausted_sha256', 'oldest_active_ms',
    'queue_progress_at', 'data_status', 'data_reason', 'data_failed_topic', 'generation_end_ms'}
EVENT_FIELDS = {
    'BINANCE_HEALTH_TOPIC_FINAL': TOPIC_FIELDS,
    'BINANCE_HEALTH_SESSION_FINAL': FINAL_FIELDS,
    'BINANCE_COLLECTOR_HEALTH': HEALTH_FIELDS,
    'BINANCE_ACK_COMMITTED': {'event', 'epoch', 'shard', 'subscribe_batch', 'received'},
    'BINANCE_HTTP_LIFECYCLE': {'event', 'epoch', 'request_number', 'topic', 'attempt', 'go',
        'response_complete', 'process_reaped', 'owner_retired', 'http_status', 'error_type'},
    'BINANCE_HEALTH_WORK_FAILED': {'event', 'epoch', 'kind', 'topics', 'attempt', 'error_type'},
    'BINANCE_HEALTH_RECOVERY': {'event', 'failures', 'error_type'},
}
NESTED_FIELDS = {'shards': {'shard', 'observed_at', 'evidence'},
                 'exhausted_work': {'kind', 'scope', 'attempts'}}
MAX_BYTES = 32 * 1024 * 1024
MAX_MESSAGE_BYTES = 65536


def startup_plan(topics):
    """Exact event-less object emitted by the reviewed stream.main -> plan."""
    return dict(topics=topics, sockets=(topics+499)//500, max_topics_per_socket=500,
                rest_requests_per_attempt=topics, rest_weight_per_request=2,
                request_spacing_ms=200, max_inflight=4, recovery_deadline_seconds=900,
                incoming_control_budget='2 subscribe/s plus protocol pong; below 5/s',
                network_capacity_verified=False)


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _hex(value, count):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{%d}' % count, value) is not None


def _integer(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def _loads(raw):
    def nonfinite(_):
        raise ValueError('NONFINITE_JSON')
    return json.loads(raw, object_pairs_hook=_object, parse_constant=nonfinite)


def _safe(value, depth=0):
    if depth > 6:
        return False
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return -(2**63) <= value <= 2**63 - 1
    if isinstance(value, str):
        return len(value.encode()) <= 1024
    if isinstance(value, list):
        return len(value) <= 4000 and all(_safe(x, depth+1) for x in value)
    if isinstance(value, dict):
        return len(value) <= 64 and all(isinstance(k, str) and len(k) <= 128 and
                                      _safe(v, depth+1) for k, v in value.items())
    return False


def _shape(value):
    """Unknown fields are never copied, including nested environment/secret data."""
    allowed = EVENT_FIELDS.get(value.get('event'))
    if allowed is None or set(value) - allowed or not _safe(value):
        return False
    for name, keys in NESTED_FIELDS.items():
        if name in value and (not isinstance(value[name], list) or any(
                not isinstance(row, dict) or set(row) - keys for row in value[name])):
            return False
    if 'work_progress' in value:
        progress = value['work_progress']
        if not isinstance(progress, dict) or set(progress) - {'directory', 'repair', 'quiet'}:
            return False
        if any(not isinstance(row, dict) or set(row) - {'completed', 'last_success'}
               for row in progress.values()):
            return False
    return True


def parse_journal(raw, *, expected, require_final=False, record_limit=10000):
    """Return bounded, fail-closed evidence; malformed input sets complete=False.

    expected requires source_head (40 hex), manifest_sha256/roster_hash (64 hex),
    collector_invocation (32 hex), and topics (complete ordered list of names).
    Optional epoch binds the last observed epoch; earlier reconnected epochs must
    each have a complete final. Running last epochs explicitly have no final.
    """
    if not isinstance(raw, bytes) or type(record_limit) is not int or not 1 <= record_limit <= 10000:
        raise ValueError('JOURNAL_ARGUMENT_INVALID')
    names = expected.get('topics')
    if (not isinstance(names, list) or not 1 <= len(names) <= 4000 or
            any(not isinstance(n, str) or not n or len(n.encode()) > 512 for n in names) or
            len(set(names)) != len(names) or
            not _hex(expected.get('source_head'), 40) or
            not _hex(expected.get('manifest_sha256'), 64) or
            not _hex(expected.get('roster_hash'), 64) or
            content_hash(sorted(names)) != expected['roster_hash'] or
            not _hex(expected.get('collector_invocation'), 32) or
            ('epoch' in expected and not _hex(expected['epoch'], 32))):
        raise ValueError('JOURNAL_EXPECTED_BINDING_INVALID')
    issues = set()
    records = []
    epochs = {}
    order = []
    closed = set()
    last_timestamp = None
    report = dict(schema='binance_health_upgrade_journal_v1', raw_sha256=hashlib.sha256(raw).hexdigest(),
                  expected=dict(expected), limit=record_limit, at_limit=False, records=records,
                  topic_coverage=[], startup_plan=None, collector_accepted=False, publication_verified=False)
    if len(raw) > MAX_BYTES:
        issues.add('JOURNAL_BYTES_LIMIT')
        lines = []
    else:
        lines = raw.splitlines()
    if len(lines) >= record_limit:
        issues.add('JOURNAL_RECORD_LIMIT')
        report['at_limit'] = True
    for line in lines[:record_limit]:
        row = {'line_sha256': hashlib.sha256(line).hexdigest()}
        records.append(row)
        try:
            if len(line) > MAX_MESSAGE_BYTES * 4:
                raise ValueError('JOURNAL_LINE_OVERSIZED')
            entry = _loads(line)
            if not isinstance(entry, dict):
                raise ValueError('JOURNAL_ENTRY_INVALID')
            message = entry.get('MESSAGE')
            if isinstance(message, list) and len(message) <= MAX_MESSAGE_BYTES and all(
                    type(x) is int and 0 <= x <= 255 for x in message):
                message = bytes(message).decode('utf-8')
            if not isinstance(message, str) or len(message.encode()) > MAX_MESSAGE_BYTES:
                raise ValueError('JOURNAL_MESSAGE_UNAVAILABLE')
            row['message_sha256'] = hashlib.sha256(message.encode()).hexdigest()
            try:
                value = _loads(message)
            except (ValueError, RecursionError):
                if message.lstrip().startswith(('{', '[')):
                    raise ValueError('JOURNAL_STRUCTURED_MESSAGE_INVALID')
                row['raw_omitted'] = True
                continue
            is_plan = isinstance(value, dict) and 'event' not in value
            if is_plan:
                wanted = startup_plan(len(names))
                if (set(value) != set(wanted) or any(type(value[k]) is not type(v) or value[k] != v
                        for k, v in wanted.items())):
                    raise ValueError('JOURNAL_STARTUP_PLAN_INVALID')
            elif not isinstance(value, dict) or not _shape(value):
                raise ValueError('JOURNAL_EVENT_SCHEMA_UNRECOGNIZED')
            if entry.get('_SYSTEMD_INVOCATION_ID') != expected['collector_invocation']:
                raise ValueError('JOURNAL_INVOCATION_MISMATCH')
            timestamp = entry.get('__REALTIME_TIMESTAMP')
            if not isinstance(timestamp, str) or not re.fullmatch('[0-9]{1,20}', timestamp):
                raise ValueError('JOURNAL_TIMESTAMP_MISSING')
            timestamp = int(timestamp)
            if timestamp <= 0 or (last_timestamp is not None and timestamp < last_timestamp):
                raise ValueError('JOURNAL_TIMESTAMP_REGRESSION')
            last_timestamp = timestamp
            if is_plan:
                if report['startup_plan'] is not None or order:
                    raise ValueError('JOURNAL_STARTUP_PLAN_DUPLICATE_OR_LATE')
                report['startup_plan'] = value
                row.update(journal_us=str(timestamp), collector_invocation=expected['collector_invocation'],
                           startup_plan=value)
                continue
            row.update(journal_us=str(timestamp), collector_invocation=expected['collector_invocation'], evidence=value)
            event = value['event']
            if event == 'BINANCE_HEALTH_RECOVERY':
                if order:
                    closed.add(order[-1])
                continue
            epoch = value.get('epoch')
            if not _hex(epoch, 32):
                raise ValueError('JOURNAL_EPOCH_INVALID')
            if epoch not in epochs:
                if order:
                    closed.add(order[-1])
                order.append(epoch)
                epochs[epoch] = dict(rows=[], final=None, binding_observed=False)
            elif epoch != order[-1]:
                raise ValueError('JOURNAL_EPOCH_REAPPEARED')
            state = epochs[epoch]
            if state['final'] is not None:
                raise ValueError('JOURNAL_EVENT_AFTER_FINAL')
            if epoch in closed:
                raise ValueError('JOURNAL_EVENT_AFTER_RECOVERY')
            if event == 'BINANCE_COLLECTOR_HEALTH':
                bindings = dict(contract=CONTRACT, source_head=expected['source_head'],
                    manifest_sha256=expected['manifest_sha256'], roster_hash=expected['roster_hash'],
                    topics=len(names), publication_verified=False, shadow_only=True,
                    real_trading_enabled=False, capital_authority='NONE_SHADOW_ONLY')
                if any(type(value.get(k)) is not type(v) or value[k] != v for k, v in bindings.items()):
                    raise ValueError('JOURNAL_HEALTH_BINDING_MISMATCH')
                state['binding_observed'] = True
            elif event == 'BINANCE_HEALTH_TOPIC_FINAL':
                if set(value) != TOPIC_FIELDS or value['topic'] not in names:
                    raise ValueError('JOURNAL_TOPIC_FINAL_SCHEMA_INVALID')
                if value['topic'] in {r['topic'] for r in state['rows']}:
                    raise ValueError('JOURNAL_TOPIC_FINAL_DUPLICATE')
                numeric = (*COUNTERS, 'revisions_changed', 'last_rx')
                if (not all(_integer(value[k]) for k in numeric) or value['conserved'] is not True or
                        value['received'] != sum(value[k] for k in COUNTERS[1:]) or
                        value['revisions_changed'] > value['committed'] or
                        bool(value['received']) != bool(value['last_rx'])):
                    raise ValueError('JOURNAL_TOPIC_CONSERVATION_INVALID')
                state['rows'].append({k: v for k, v in value.items() if k != 'event'})
            elif event == 'BINANCE_HEALTH_SESSION_FINAL':
                if set(value) != FINAL_FIELDS:
                    raise ValueError('JOURNAL_SESSION_FINAL_SCHEMA_INVALID')
                state['final'] = value
                closed.add(epoch)
            elif event == 'BINANCE_HTTP_LIFECYCLE':
                if value.get('topic') not in names:
                    raise ValueError('JOURNAL_HTTP_TOPIC_OUTSIDE_SCOPE')
            elif event == 'BINANCE_HEALTH_WORK_FAILED':
                if (not isinstance(value.get('topics'), list) or not value['topics'] or
                        any(n not in names for n in value['topics'])):
                    raise ValueError('JOURNAL_WORK_TOPIC_OUTSIDE_SCOPE')
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
            reason = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_]+', str(error)) else 'JOURNAL_RECORD_INVALID'
            issues.add(reason)
            # Invalid parsed data is retained only by digest, never accidentally redacted into success.
            row.pop('evidence', None)
            row['error_code'] = reason
    if not order:
        issues.add('JOURNAL_EPOCH_EVIDENCE_MISSING')
    if report['startup_plan'] is None:
        issues.add('JOURNAL_STARTUP_PLAN_MISSING')
    if expected.get('epoch') is not None and (not order or order[-1] != expected['epoch']):
        issues.add('JOURNAL_LAST_EPOCH_MISMATCH')
    if require_final:
        closed.update(order)
    for epoch in order:
        state = epochs[epoch]
        final = state['final']
        rows = state['rows']
        valid = False
        if final is not None:
            numeric = (*COUNTERS, 'controls', 'control_receipts', 'raw_application_frames', 'unattributed_invalid', 'topics')
            valid = (all(_integer(final.get(k)) for k in numeric) and
                final.get('complete') is True and final.get('raw_frames_conserved') is True and
                final['topics'] == len(names) == len(rows) and {r['topic'] for r in rows} == set(names) and
                all(final[k] == sum(r[k] for r in rows) for k in COUNTERS) and
                final['received'] == sum(final[k] for k in COUNTERS[1:]) and
                final['controls'] <= final['control_receipts'] and
                final['raw_application_frames'] == final['control_receipts'] + final['unattributed_invalid'] + final['received'] and
                final.get('receipt_partition_sha256') == content_hash(rows))
            if not valid:
                issues.add('JOURNAL_SESSION_CONSERVATION_INVALID')
        elif epoch in closed or rows:
            issues.add('JOURNAL_SESSION_FINAL_MISSING')
        elif not state['binding_observed']:
            issues.add('JOURNAL_ACTIVE_BINDING_MISSING')
        report['topic_coverage'].append(dict(epoch=epoch, expected_topics=len(names), observed_topics=len(rows),
            final_state='FINAL_VALIDATED' if valid else 'FINAL_INCOMPLETE' if epoch in closed or rows else 'ACTIVE_NO_FINAL',
            receipt_conservation_verified=valid, event_binding_verified=state['binding_observed'],
            receipt_partition_sha256=final.get('receipt_partition_sha256') if final else None))
    report.update(complete=not issues, completeness_errors=sorted(issues), observed_epochs=order)
    return report
