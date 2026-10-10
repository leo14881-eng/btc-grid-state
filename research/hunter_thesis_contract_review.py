"""OFFLINE DRAFT: three-valued review of an explicitly recorded thesis contract.

No policy defaults, network, writer, order path or production imports. A receipt
hash proves input integrity, not source truth. This kernel cannot authenticate an
external claim or authorize a trade. Production adapters/entry capture and policy
approval are deliberately separate, still-unimplemented prerequisites.
"""
import datetime as dt
import hashlib
import json
import math

DIMENSIONS=('execution_conditions','relative_strength','market_structure','liquidity',
            'volume_demand','candidate_evidence','fundamental_supply','venue_identity')
PROPOSITIONS=('entry_condition_still_holds','deteriorated_since_entry','hard_invalidation_confirmed')
VALUES={'TRUE','FALSE','UNKNOWN'}
IDENTITY=('asset','venue','symbol','market_type')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
        ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def instant(value):
    result=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('SOURCE_TIME_HAS_NO_TIMEZONE')
    return result


def result(value,reason,**extra):
    return dict(value=value,reason=reason,**extra)


def aggregate(values,operator='all'):
    """Kleene logic; absence is unknown, never the identity of an empty set."""
    if not values or any(x not in VALUES for x in values):return 'UNKNOWN'
    if operator=='all':return 'FALSE' if 'FALSE' in values else 'UNKNOWN' if 'UNKNOWN' in values else 'TRUE'
    if operator=='any':return 'TRUE' if 'TRUE' in values else 'UNKNOWN' if 'UNKNOWN' in values else 'FALSE'
    raise ValueError('UNSUPPORTED_BOOLEAN_OPERATOR')


def _number(value):
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
        raise ValueError('NONFINITE_OR_NONNUMERIC_MEASUREMENT')
    return value


def _resolve_packet(contract,packets,scope,slot,now,generation,check_alignment=True):
    spec=contract['sources'][slot]
    packet=packets[scope][slot]
    if not isinstance(spec,dict) or not isinstance(packet,dict):raise ValueError('SOURCE_PACKET_SCHEMA_INVALID')
    role=spec.get('role')
    if role=='POSITION':
        if spec['identity']!=contract['identity']:raise ValueError('POSITION_SOURCE_BINDING_MISMATCH')
    elif role=='BENCHMARK':
        benchmark=contract['benchmarks'][spec['benchmark_id']]
        if not benchmark.get('purpose') or benchmark['identity']!=spec['identity']:
            raise ValueError('BENCHMARK_SOURCE_BINDING_MISMATCH')
    else:raise ValueError('SOURCE_SUBJECT_ROLE_UNRECORDED')
    if packet.get('kind')!=spec['kind']:raise ValueError('SOURCE_KIND_MISMATCH')
    if packet.get('identity')!=spec['identity']:raise ValueError('SOURCE_IDENTITY_MISMATCH')
    if not packet.get('source_ref'):raise ValueError('SOURCE_REFERENCE_MISSING')
    if not packet.get('receipt_id'):raise ValueError('RECEIPT_ID_MISSING')
    if packet.get('payload_sha256')!=digest(packet['values']):raise ValueError('RECEIPT_PAYLOAD_HASH_MISMATCH')
    body={k:v for k,v in packet.items() if k!='receipt_sha256'}
    if packet.get('receipt_sha256')!=digest(body):raise ValueError('RECEIPT_METADATA_HASH_MISMATCH')
    expected_generation=generation if scope=='current' else contract['entry_generation_id']
    if not expected_generation or packet.get('generation_id')!=expected_generation:
        raise ValueError('SOURCE_GENERATION_MISMATCH')
    # A refreshed wrapper/verified_at does not replace original source observation.
    observed=instant(packet['source_observed_at']);fetched=instant(packet['fetched_at'])
    evaluated=now if scope=='current' else instant(contract['entry_at'])
    max_age=_number(spec['max_age_seconds'])
    if max_age<=0 or not 0<=(evaluated-observed).total_seconds()<=max_age:
        raise ValueError('SOURCE_OBSERVATION_STALE_OR_FUTURE')
    if not observed<=fetched<=evaluated:raise ValueError('FETCH_TIME_INVALID')
    if spec.get('requires_publication_time'):
        if instant(packet['source_published_at'])>observed:raise ValueError('SOURCE_NOT_PUBLISHED_AT_OBSERVATION')
    if spec.get('effective_time_rule'):
        effective=instant(packet['effective_from'])
        rule=spec['effective_time_rule']
        if rule=='ALREADY_EFFECTIVE':
            if effective>evaluated:raise ValueError('SOURCE_EVENT_NOT_YET_EFFECTIVE')
            if packet.get('effective_until') and instant(packet['effective_until'])<=evaluated:
                raise ValueError('SOURCE_EVENT_NO_LONGER_EFFECTIVE')
        elif rule!='SCHEDULED_FACT':raise ValueError('EFFECTIVE_TIME_RULE_UNKNOWN')
    if spec.get('closed_window_required'):
        window=packet['source_window'];start=_number(window['start_ms']);end=_number(window['end_ms'])
        if window.get('complete') is not True or not start<end<observed.timestamp()*1000:
            raise ValueError('SOURCE_WINDOW_NOT_COMPLETE_AT_OBSERVATION')
        if end-start+1!=_number(spec['window_ms']):raise ValueError('SOURCE_WINDOW_DURATION_MISMATCH')
        if (evaluated.timestamp()*1000-end)>max_age*1000:
            raise ValueError('SOURCE_WINDOW_STALE_DESPITE_NEW_OBSERVATION')
    if spec.get('alignment_group') and check_alignment:
        windows=[]
        for peer,peer_spec in contract['sources'].items():
            if peer_spec.get('alignment_group')!=spec['alignment_group']:continue
            peer_packet=_resolve_packet(contract,packets,scope,peer,now,generation,False)
            windows.append(peer_packet['source_window'])
        if any(w!=windows[0] for w in windows[1:]):raise ValueError('SOURCE_WINDOWS_NOT_ALIGNED')
    return packet


def _operand(term,contract,packets,now,generation):
    unit=term['unit']
    if term.get('scope')=='policy':
        # Only frozen original policy values; no fallback to today's constants.
        measurement=contract['original_policy'][term['field']]
        if measurement['unit']!=unit:raise ValueError('POLICY_UNIT_MISMATCH')
        value=measurement['value']
        if value is None:raise ValueError('POLICY_VALUE_UNKNOWN')
        return value,unit,dict(policy_ref=contract['policy_ref'],field=term['field'])
    scope=term['scope']
    if scope not in ('entry','current'):raise ValueError('UNSUPPORTED_OPERAND_SCOPE')
    packet=_resolve_packet(contract,packets,scope,term['receipt'],now,generation)
    measurement=packet['values'][term['field']]
    if measurement['unit']!=unit:raise ValueError('MEASUREMENT_UNIT_MISMATCH')
    value=measurement['value']
    if value is None:raise ValueError('MEASUREMENT_UNKNOWN')
    return value,unit,dict(scope=scope,receipt_id=packet['receipt_id'],source_ref=packet['source_ref'],
        identity=packet['identity'],subject_role=contract['sources'][term['receipt']]['role'],
        source_observed_at=packet['source_observed_at'],fetched_at=packet['fetched_at'],
        source_window=packet.get('source_window'),payload_sha256=packet['payload_sha256'],
        proof_level='RECORDED_RUNTIME_INPUT_INTEGRITY_ONLY')


def _expression(expr,contract,packets,now,generation,depth=0):
    if depth>8:raise ValueError('PREDICATE_NESTING_LIMIT')
    operator=expr['op']
    if operator in ('all','any'):
        nodes=expr['args']
        if not isinstance(nodes,list) or len(nodes)>32:raise ValueError('INVALID_PREDICATE_ARITY')
        children=[_safe_expression(x,contract,packets,now,generation,depth+1) for x in nodes]
        return result(aggregate([x['value'] for x in children],operator),'EXPLICIT_'+operator.upper(),children=children)
    a,unit,ap=_operand(expr['left'],contract,packets,now,generation)
    b,other,bp=_operand(expr['right'],contract,packets,now,generation)
    if unit!=other:raise ValueError('COMPARISON_UNIT_MISMATCH')
    if operator in ('ge','gt','le','lt'):
        _number(a);_number(b)
        answer={'ge':a>=b,'gt':a>b,'le':a<=b,'lt':a<b}[operator]
    elif operator in ('eq','ne'):
        if type(a)!=type(b) or not isinstance(a,(str,bool,int,float)):
            raise ValueError('COMPARISON_TYPE_MISMATCH')
        if isinstance(a,(int,float)) and not isinstance(a,bool):_number(a);_number(b)
        answer=(a==b) if operator=='eq' else (a!=b)
    else:raise ValueError('UNSUPPORTED_PREDICATE_OPERATOR')
    return result('TRUE' if answer else 'FALSE','RECORDED_PREDICATE_EVALUATED',
        original_or_threshold=b,current=a,unit=unit,operator=operator,sources=[ap,bp])


def _safe_expression(expr,contract,packets,now,generation,depth=0):
    try:return _expression(expr,contract,packets,now,generation,depth)
    except (KeyError,TypeError,ValueError,OverflowError,AttributeError) as ex:return result('UNKNOWN',str(ex))


def _evidence_complete(node):
    # FALSE AND UNKNOWN is logically FALSE, but not completely evidenced.
    return node.get('value') in ('TRUE','FALSE') and all(
        _evidence_complete(child) for child in node.get('children',[]))


def evaluate(contract,packets,identity,now,generation,shadow_id):
    """Evaluate supplied predicates, never infer an absent historical thesis.

    Deliberately not wired to manage_existing_positions. Source authentication,
    approved predicate capture and an approved risk model remain required.
    """
    report=dict(schema='hunter_thesis_contract_review_draft_v1',generation_id=generation,
        checked_at=str(now),dimensions={},excluded_entry_or_allocation_only=[],
        source_authentication='NOT_ESTABLISHED_BY_OFFLINE_KERNEL',
        capital_authority='NONE_SHADOW_ONLY',real_trading_enabled=False,
        production_activated=False,hard_exit_authorized=False,rebound_exit_authorized=False)
    issue=None
    try:
        if not isinstance(contract,dict) or not isinstance(identity,dict):raise ValueError('CONTRACT_SCHEMA_INVALID')
        if not isinstance(now,dt.datetime) or now.tzinfo is None:raise ValueError('EVALUATION_TIME_HAS_NO_TIMEZONE')
        if contract.get('schema')!='hunter_thesis_contract_draft_v1':raise ValueError('ORIGINAL_PREDICATE_MANIFEST_MISSING')
        if contract.get('identity')!=identity or any(not identity.get(k) for k in IDENTITY):raise ValueError('POSITION_IDENTITY_NOT_PROVEN')
        if not contract.get('entry_source_ref') or not contract.get('policy_ref'):raise ValueError('ORIGINAL_POLICY_OR_ENTRY_PROVENANCE_MISSING')
        if not shadow_id or contract.get('shadow_id')!=shadow_id or not contract.get('entry_generation_id'):
            raise ValueError('ORIGINAL_POSITION_LINK_MISSING_OR_MISMATCH')
        if not generation:raise ValueError('CURRENT_GENERATION_MISSING')
        if contract.get('original_policy_sha256')!=digest(contract['original_policy']):
            raise ValueError('ORIGINAL_POLICY_HASH_MISMATCH')
        if instant(contract['entry_at'])>now:raise ValueError('ENTRY_FROM_FUTURE_AT_EVALUATION')
        predicates=contract['predicates']
        if len({p['id'] for p in predicates})!=len(predicates):raise ValueError('DUPLICATE_PREDICATE_ID')
        if any(p['dimension'] not in DIMENSIONS or p['proposition'] not in PROPOSITIONS for p in predicates):
            raise ValueError('UNKNOWN_DIMENSION_OR_PROPOSITION')
    except (KeyError,TypeError,ValueError,AttributeError) as ex:issue=str(ex);predicates=[]
    if not isinstance(contract,dict):contract={}
    for dimension in DIMENSIONS:
        report['dimensions'][dimension]={}
        for proposition in PROPOSITIONS:
            items=[]
            for predicate in predicates:
                if predicate['dimension']!=dimension or predicate['proposition']!=proposition:continue
                applicability=predicate.get('applicability')
                if applicability in ('ENTRY_ONLY','ALLOCATION_ONLY'):
                    report['excluded_entry_or_allocation_only'].append(predicate['id']);continue
                if not predicate.get('version'):
                    row=result('UNKNOWN','PREDICATE_VERSION_UNRECORDED')
                elif applicability not in ('CONTINUING_THESIS','HARD_CONDITION'):
                    row=result('UNKNOWN','PREDICATE_APPLICABILITY_UNRECORDED')
                elif proposition=='hard_invalidation_confirmed' and applicability!='HARD_CONDITION':
                    row=result('UNKNOWN','ORDINARY_WEAKNESS_CANNOT_BECOME_HARD_EVIDENCE')
                else:row=_safe_expression(predicate.get('expression'),contract,packets,now,generation)
                items.append(dict(predicate_id=predicate['id'],predicate_version=predicate.get('version'),**row))
            aggregations=contract.get('aggregations')
            joins=aggregations.get(dimension) if isinstance(aggregations,dict) else None
            operator=joins.get(proposition) if isinstance(joins,dict) else None
            aggregate_value=aggregate([row['value'] for row in items],operator) if operator in ('all','any') else 'UNKNOWN'
            report['dimensions'][dimension][proposition]=dict(
                value=aggregate_value,
                evidence_complete=bool(items) and operator in ('all','any') and all(_evidence_complete(row) for row in items),
                reason=issue or ('ORIGINAL_DIMENSION_PREDICATE_NOT_RECORDED' if not items else
                    'PREDICATE_AGGREGATION_UNRECORDED' if operator not in ('all','any') else 'EXPLICIT_RECORDED_PREDICATES'),
                predicates=items)
    checks=[report['dimensions'][d]['entry_condition_still_holds']['value'] for d in DIMENSIONS]
    report['all_recorded_entry_conditions_hold']=aggregate(checks)
    report['original_thesis_coverage_complete']=all(
        report['dimensions'][d]['entry_condition_still_holds']['evidence_complete'] for d in DIMENSIONS)
    report['original_thesis_authenticated']=False
    return report


def consecutive_confirmations(observations,required_count,window_ms,series_key):
    """Source-window counter for offline tests, independent of wrapper IDs."""
    if not isinstance(required_count,int) or isinstance(required_count,bool) or required_count<1:
        raise ValueError('EXPLICIT_POSITIVE_CONFIRMATION_COUNT_REQUIRED')
    if _number(window_ms)<=0:raise ValueError('EXPLICIT_POSITIVE_WINDOW_REQUIRED')
    if not isinstance(series_key,str) or not series_key:raise ValueError('EXPLICIT_POSITION_PREDICATE_SERIES_REQUIRED')
    count=0;last=None;last_value=None;ignored=0;conflicts=0;seen={}
    for row in observations:
        if row.get('series_key')!=series_key:raise ValueError('POSITION_PREDICATE_SERIES_MISMATCH')
        end=_number(row['source_closed_at_ms']);value=row['value']
        if value not in VALUES:raise ValueError('INVALID_TRISTATE')
        if end in seen:
            ignored+=1
            if value!=seen[end]:
                conflicts+=1;seen[end]='UNKNOWN'
                if end==last or (count and last-(count-1)*window_ms<=end<=last):
                    count=0;last_value='UNKNOWN'
            continue
        seen[end]=value
        if last is not None and end<last:ignored+=1;continue
        if last is None or end-last!=window_ms:count=0
        count=count+1 if value=='TRUE' else 0
        last=end;last_value=value
    return dict(value='TRUE' if count>=required_count else 'UNKNOWN' if last_value in (None,'UNKNOWN') else 'FALSE',
        count=count,ignored_duplicate_or_old_windows=ignored,conflicting_same_window=conflicts,
        source_closed_at_ms=last,trade_authorized=False)
