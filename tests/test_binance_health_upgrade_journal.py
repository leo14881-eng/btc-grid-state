import copy
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('health_journal', ROOT/'scripts/hunter_binance_health_upgrade_journal.py')
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)
EPOCH = 'e'*32
OTHER = 'f'*32
INVOCATION = 'a'*32
NAMES = ['btcusdt@kline_15m', 'ethusdt@kline_1h']
EXPECTED = dict(source_head='b'*40, manifest_sha256='c'*64,
                roster_hash=journal.content_hash(sorted(NAMES)),
                collector_invocation=INVOCATION, topics=NAMES, epoch=EPOCH)


def health(epoch=EPOCH):
    return dict(event='BINANCE_COLLECTOR_HEALTH', contract='binance_health_v1', epoch=epoch,
        source_head=EXPECTED['source_head'], manifest_sha256=EXPECTED['manifest_sha256'],
        roster_hash=EXPECTED['roster_hash'], topics=len(NAMES), publication_verified=False,
        shadow_only=True, real_trading_enabled=False, capital_authority='NONE_SHADOW_ONLY',
        service_status='DEGRADED', data_status='UNKNOWN', observed_at=123456,
        work_progress={'directory': {'completed': 1, 'last_success': 123450}},
        exhausted_work=[], shards=[{'shard': 0, 'observed_at': 123450, 'evidence': 'MATCHING_PONG'}])


def closed(epoch=EPOCH):
    rows = [dict(epoch=epoch, topic=name, committed=1, duplicate=2, invalid=3,
        unprocessed_recorded=4, revisions_changed=1, received=10, last_rx=123456, conserved=True)
        for name in NAMES]
    final = dict(event='BINANCE_HEALTH_SESSION_FINAL', epoch=epoch, complete=True,
        controls=1, control_receipts=2, raw_application_frames=23, raw_frames_conserved=True,
        unattributed_invalid=1, topics=len(rows), reason='STOP_REQUESTED',
        receipt_partition_sha256=journal.content_hash(rows),
        **{k:sum(r[k] for r in rows) for k in journal.COUNTERS})
    return [dict(event='BINANCE_HEALTH_TOPIC_FINAL', **row) for row in rows] + [final]


def encode(values, invocation=INVOCATION, start=1000000, bytes_message=False, include_plan=True):
    result = []
    if include_plan and (not values or 'event' in values[0]):
        values = [journal.startup_plan(len(NAMES))] + values
    for i, value in enumerate(values):
        message = json.dumps(value)
        result.append(json.dumps(dict(MESSAGE=list(message.encode()) if bytes_message else message,
            _SYSTEMD_INVOCATION_ID=invocation, __REALTIME_TIMESTAMP=str(start+i))).encode())
    return b'\n'.join(result)


class JournalTests(unittest.TestCase):
    def parse(self, values, **kwargs):
        return journal.parse_journal(encode(values), expected=EXPECTED, **kwargs)

    def assertIncomplete(self, result, reason):
        self.assertFalse(result['complete'], result)
        self.assertIn(reason, result['completeness_errors'])

    def test_running_preserves_binding_and_explicitly_has_no_final(self):
        result=self.parse([health()])
        self.assertTrue(result['complete'])
        self.assertEqual(result['topic_coverage'][0]['final_state'], 'ACTIVE_NO_FINAL')
        self.assertFalse(result['topic_coverage'][0]['receipt_conservation_verified'])
        self.assertFalse(result['collector_accepted']);self.assertFalse(result['publication_verified'])
        self.assertEqual(result['records'][1]['evidence'],health())

    def test_strict_final_requires_terminal_record(self):
        self.assertIncomplete(self.parse([health()],require_final=True),'JOURNAL_SESSION_FINAL_MISSING')

    def test_valid_final_preserves_every_counter_and_partition_hash(self):
        values=[health()]+closed();result=self.parse(values,require_final=True)
        self.assertTrue(result['complete'],result)
        self.assertTrue(result['topic_coverage'][0]['receipt_conservation_verified'])
        self.assertEqual([x['evidence'] for x in result['records'][1:]],values)

    def test_false_conserved_flag_fails_even_if_math_balances(self):
        values=closed();values[0]['conserved']=False
        self.assertIncomplete(self.parse(values,require_final=True),'JOURNAL_TOPIC_CONSERVATION_INVALID')

    def test_topic_mismatch_and_boolean_or_negative_counter_fail(self):
        for field,value in [('received',11),('received',True),('duplicate',-1),('revisions_changed',2),('last_rx',0)]:
            with self.subTest(field=field,value=value):
                values=closed();values[0][field]=value
                self.assertIncomplete(self.parse(values,require_final=True),'JOURNAL_TOPIC_CONSERVATION_INVALID')

    def test_duplicate_topic_not_hidden_by_balanced_session_totals(self):
        values=closed();values.insert(1,copy.deepcopy(values[0]))
        self.assertIncomplete(self.parse(values,require_final=True),'JOURNAL_TOPIC_FINAL_DUPLICATE')

    def test_missing_topic_with_rehashed_subset_still_fails(self):
        values=closed();values.pop(0)
        values[-1]['receipt_partition_sha256']=journal.content_hash([{k:v for k,v in values[0].items() if k!='event'}])
        self.assertIncomplete(self.parse(values,require_final=True),'JOURNAL_SESSION_CONSERVATION_INVALID')

    def test_outside_scope_topic_fails(self):
        values=closed();values[0]['topic']='kgstusdt@kline_15m'
        self.assertIncomplete(self.parse(values,require_final=True),'JOURNAL_TOPIC_FINAL_SCHEMA_INVALID')

    def test_partition_hash_binds_order_and_full_row(self):
        for mutate in (lambda v:v.reverse(),lambda v:v[0].update(last_rx=123457)):
            values=closed();topics=values[:-1];mutate(topics);values=topics+[values[-1]]
            self.assertIncomplete(self.parse(values,require_final=True),'JOURNAL_SESSION_CONSERVATION_INVALID')

    def test_session_total_raw_frames_and_false_flags_fail(self):
        for field,value in [('committed',3),('raw_application_frames',24),('controls',3),
                            ('complete',False),('raw_frames_conserved',False),('topics',True)]:
            with self.subTest(field=field):
                values=closed();values[-1][field]=value
                self.assertIncomplete(self.parse(values,require_final=True),'JOURNAL_SESSION_CONSERVATION_INVALID')

    def test_final_without_health_preserves_no_source_event_claim(self):
        result=self.parse(closed(),require_final=True)
        self.assertTrue(result['complete'],result)
        self.assertFalse(result['topic_coverage'][0]['event_binding_verified'])
        self.assertEqual(result['expected']['source_head'],EXPECTED['source_head'])

    def test_multiple_epochs_validate_every_ended_epoch(self):
        values=[health(OTHER)]+closed(OTHER)+[health()]
        result=self.parse(values)
        self.assertTrue(result['complete'],result)
        self.assertEqual([x['final_state'] for x in result['topic_coverage']],['FINAL_VALIDATED','ACTIVE_NO_FINAL'])
        self.assertIncomplete(self.parse([health(OTHER),health()]),'JOURNAL_SESSION_FINAL_MISSING')

    def test_recovery_requires_old_final_even_when_current_looks_healthy(self):
        values=[health(OTHER),{'event':'BINANCE_HEALTH_RECOVERY','failures':1,'error_type':'TimeoutError'},health()]
        self.assertIncomplete(self.parse(values),'JOURNAL_SESSION_FINAL_MISSING')

    def test_records_cannot_resume_closed_epoch_after_recovery(self):
        values=[health(),{'event':'BINANCE_HEALTH_RECOVERY','failures':1,'error_type':'TimeoutError'},health()]
        self.assertIncomplete(self.parse(values),'JOURNAL_EVENT_AFTER_RECOVERY')

    def test_epoch_reappearing_and_event_after_final_fail(self):
        self.assertIncomplete(self.parse([health(),health(OTHER),health()]),'JOURNAL_EPOCH_REAPPEARED')
        self.assertIncomplete(self.parse([health()]+closed()+[health()]),'JOURNAL_EVENT_AFTER_FINAL')

    def test_expected_last_epoch_cannot_be_satisfied_by_old_healthy_record(self):
        self.assertIncomplete(self.parse([health(OTHER)]),'JOURNAL_LAST_EPOCH_MISMATCH')

    def test_each_binding_and_safety_boolean_is_exact(self):
        changes={'source_head':'d'*40,'manifest_sha256':'d'*64,'roster_hash':'d'*64,
                 'topics':1,'publication_verified':True,'shadow_only':1,'real_trading_enabled':0,
                 'capital_authority':'REAL','contract':'other'}
        for key,value in changes.items():
            with self.subTest(key=key):
                row=health();row[key]=value
                self.assertIncomplete(self.parse([row]),'JOURNAL_HEALTH_BINDING_MISMATCH')

    def test_invocation_missing_or_other_never_counts_as_proof(self):
        for invocation in (None,'d'*32):
            raw=encode([health()],invocation=invocation)
            self.assertIncomplete(journal.parse_journal(raw,expected=EXPECTED),'JOURNAL_INVOCATION_MISMATCH')

    def test_timestamp_missing_or_backward_fails(self):
        lines=encode([health(),health()]).splitlines()
        for timestamp in (None,'999999'):
            changed=json.loads(lines[1]);changed['__REALTIME_TIMESTAMP']=timestamp
            result=journal.parse_journal(lines[0]+b'\n'+json.dumps(changed).encode(),expected=EXPECTED)
            self.assertIncomplete(result,'JOURNAL_TIMESTAMP_MISSING' if timestamp is None else 'JOURNAL_TIMESTAMP_REGRESSION')

    def test_journal_byte_array_message_supported(self):
        result=journal.parse_journal(encode([health()]+closed(),bytes_message=True),expected=EXPECTED,require_final=True)
        self.assertTrue(result['complete'],result)

    def test_invalid_missing_ambiguous_or_non_utf8_messages_fail(self):
        for message in (None,{},[999],[True],[255]):
            raw=json.dumps({'MESSAGE':message}).encode()
            result=journal.parse_journal(raw,expected=EXPECTED)
            self.assertFalse(result['complete'])
            self.assertNotIn('evidence',result['records'][0])

    def test_plaintext_hashed_without_disclosing_environment_or_traceback(self):
        secret='Environment=API_SECRET=do-not-publish traceback text'
        raw=encode([health()])+b'\n'+json.dumps({'MESSAGE':secret}).encode()
        result=journal.parse_journal(raw,expected=EXPECTED)
        self.assertTrue(result['complete'],result)
        self.assertNotIn(secret,json.dumps(result));self.assertTrue(result['records'][-1]['raw_omitted'])

    def test_unknown_or_nested_fields_never_leak_or_silently_succeed(self):
        for mutate in (lambda v:v.update(Environment='SECRET_VALUE'),
                       lambda v:v['shards'][0].update(Environment='SECRET_VALUE'),
                       lambda v:v['work_progress']['directory'].update(token='SECRET_VALUE')):
            row=health();mutate(row);result=self.parse([row])
            self.assertIncomplete(result,'JOURNAL_EVENT_SCHEMA_UNRECOGNIZED')
            self.assertNotIn('SECRET_VALUE',json.dumps(result))

    def test_unknown_structured_event_is_incomplete(self):
        self.assertIncomplete(self.parse([dict(event='BINANCE_FUTURE_EVENT',epoch=EPOCH)]),'JOURNAL_EVENT_SCHEMA_UNRECOGNIZED')

    def test_empty_partial_json_duplicate_keys_and_nonfinite_fail(self):
        for raw in (b'', b'{', b'{"MESSAGE":"x","MESSAGE":"y"}',
                    b'{"MESSAGE":"{\"event\":NaN}"}'):
            self.assertFalse(journal.parse_journal(raw,expected=EXPECTED)['complete'])

    def test_record_limit_and_message_byte_limits_fail_closed(self):
        self.assertIncomplete(journal.parse_journal(encode([health()]),expected=EXPECTED,record_limit=1),'JOURNAL_RECORD_LIMIT')
        raw=json.dumps({'MESSAGE':'x'*(journal.MAX_MESSAGE_BYTES+1)}).encode()
        self.assertIncomplete(journal.parse_journal(raw,expected=EXPECTED),'JOURNAL_MESSAGE_UNAVAILABLE')

    def test_http_and_failed_work_retained_with_exact_topic_scope(self):
        events=[health(),dict(event='BINANCE_HTTP_LIFECYCLE',epoch=EPOCH,topic=NAMES[0],request_number=1,
            attempt=1,go=True,response_complete=True,process_reaped=True,owner_retired=True,http_status=200),
            dict(event='BINANCE_HEALTH_WORK_FAILED',epoch=EPOCH,kind='quiet',topics=[NAMES[0]],attempt=1,error_type='ValueError')]
        result=self.parse(events);self.assertTrue(result['complete'],result)
        self.assertEqual(result['records'][2]['evidence'],events[1])
        events[1]['topic']='other'
        self.assertIncomplete(self.parse(events),'JOURNAL_HTTP_TOPIC_OUTSIDE_SCOPE')

    def test_invalid_expected_roster_or_invocation_rejected_before_parse(self):
        for change in ({'topics':NAMES+NAMES},{'roster_hash':'d'*64},{'collector_invocation':None}):
            with self.assertRaisesRegex(ValueError,'JOURNAL_EXPECTED_BINDING_INVALID'):
                journal.parse_journal(b'',expected=dict(EXPECTED,**change))

    def test_exact_reviewed_runtime_final_event_contract(self):
        # Extract only the real pure final method from the pinned reviewed source;
        # no production modules are imported, and no network or service is touched.
        source=subprocess.check_output(['git','-C',str(ROOT),'show',
            '33abf19c7a91855ba2efc2e65e1d383055494d75:research/hunter_binance_health.py'],timeout=20)
        tree=ast.parse(source)
        protocol=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Protocol')
        method=next(n for n in protocol.body if isinstance(n,ast.FunctionDef) and n.name=='final')
        namespace={'stream':types.SimpleNamespace(emit=lambda row:events.append(row)),
                   'content_hash':journal.content_hash,'CONTRACT':'binance_health_v1','DurableStopError':RuntimeError}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<reviewed-Protocol.final>','exec'),namespace)
        counts={name:dict(epoch=EPOCH,topic=name,committed=1,duplicate=2,invalid=3,
                         unprocessed_recorded=4,revisions_changed=1) for name in NAMES}
        store=types.SimpleNamespace(counts=lambda epoch:counts,read_health=lambda:{},store_health=lambda value:None)
        evidence=types.SimpleNamespace(lock=threading.Lock(),topics={name:dict(received=10,last_rx=123456) for name in NAMES})
        subject=types.SimpleNamespace(store=store,evidence=evidence,epoch=EPOCH,
            raw_frames=23,control_receipts=2,invalid_envelopes=1,controls=1)
        source=subprocess.check_output(['git','-C',str(ROOT),'show',
            '33abf19c7a91855ba2efc2e65e1d383055494d75:research/hunter_binance_stream.py'],timeout=20)
        tree=ast.parse(source)
        plan=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='plan')
        constants=[n for n in tree.body if isinstance(n,ast.Assign) and any(
            isinstance(t,ast.Name) and t.id in ('SHARD_SIZE','MAX_SHARDS') for t in n.targets)]
        namespace['roster_hash']=lambda names:journal.content_hash(sorted(names))
        exec(compile(ast.Module(body=constants+[plan],type_ignores=[]),'<reviewed-plan>','exec'),namespace)
        events=[namespace['plan'](NAMES),health()];namespace['final'](subject,'STOP_REQUESTED')
        result=self.parse(events,require_final=True)
        self.assertTrue(result['complete'],result)
        self.assertEqual(result['startup_plan'],events[0])
        self.assertTrue(result['topic_coverage'][0]['receipt_conservation_verified'])

    def test_startup_plan_requires_exact_roster_derived_values_and_types(self):
        for change in ({'topics':3},{'sockets':2},{'max_inflight':True},{'network_capacity_verified':0},
                       {'incoming_control_budget':'other'},{'Environment':'SECRET_VALUE'}):
            result=self.parse([dict(journal.startup_plan(len(NAMES)),**change),health()])
            self.assertIncomplete(result,'JOURNAL_STARTUP_PLAN_INVALID')
            self.assertNotIn('SECRET_VALUE',json.dumps(result))

    def test_missing_duplicate_or_late_startup_plan_is_not_complete(self):
        missing=journal.parse_journal(encode([health()],include_plan=False),expected=EXPECTED)
        self.assertIncomplete(missing,'JOURNAL_STARTUP_PLAN_MISSING')
        plan=journal.startup_plan(len(NAMES))
        for values in ([plan,plan,health()],[plan,health(),plan]):
            self.assertIncomplete(self.parse(values),'JOURNAL_STARTUP_PLAN_DUPLICATE_OR_LATE')

    def test_actual_full_scope_plan_uses_three_sockets_for_1228_topics(self):
        names=['coin%dusdt@kline_15m'%n for n in range(1228)]
        expected=dict(EXPECTED,topics=names,roster_hash=journal.content_hash(sorted(names)))
        row=health();row.update(topics=len(names),roster_hash=expected['roster_hash'])
        plan=journal.startup_plan(len(names))
        self.assertEqual(plan['sockets'],3)
        result=journal.parse_journal(encode([plan,row]),expected=expected)
        self.assertTrue(result['complete'],result)


if __name__=='__main__':unittest.main()
