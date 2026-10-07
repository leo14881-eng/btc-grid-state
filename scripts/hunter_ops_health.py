"""Bounded, read-only Fast Watch evidence for the server ops connector."""
import datetime as dt
import json
import os
import re
import stat

HEALTH_PATH = '/run/hunter-fast-watch/health.json'
MAX_BYTES = 1000000
MAX_AGE_SECONDS = 60


def read_health(path=HEALTH_PATH, now=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('HEALTH_NOT_REGULAR_FILE')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('HEALTH_TOO_LARGE')
    finally:
        os.close(fd)
    health = json.loads(raw)
    if (health.get('real_order_count') != 0
            or health.get('real_trading_enabled') is not False
            or health.get('capital_authority') != 'NONE_SHADOW_ONLY'
            or health.get('formal_writer') is not False
            or health.get('mode') != 'OBSERVATION_ONLY'):
        raise ValueError('SHADOW_BOUNDARY_NOT_VERIFIED')
    observed = dt.datetime.fromisoformat(health['generated_at'].replace('Z', '+00:00'))
    if observed.tzinfo is None:
        raise ValueError('HEALTH_TIMESTAMP_INVALID')
    age = ((now or dt.datetime.now(dt.timezone.utc)) - observed).total_seconds()
    if not 0 <= age <= MAX_AGE_SECONDS:
        raise ValueError('HEALTH_STALE_OR_FUTURE')
    source = health.get('source_sha', '')
    if (not re.fullmatch('[0-9a-f]{40}', source)
            or health.get('deployment_evidence', {}).get('evidence_snapshot_sha') != source):
        raise ValueError('HEALTH_SHA_MISMATCH')
    return health


def enrich_deployment(base, health=None):
    result = dict(base)
    def value(key):
        record = base.get(key, {})
        return record.get('stdout', '').strip() if record.get('returncode') == 0 else 'UNKNOWN'
    result.update(base_checkout_sha=value('head'),
                  cached_origin_main_sha=value('cached_origin_main'),
                  cached_origin_is_live_lookup=False,
                  runtime_evidence_status='UNAVAILABLE')
    for job in ('discovery', 'research', 'monitor', 'watchdog'):
        result['last_' + job + '_source_sha'] = 'UNKNOWN'
    result['authoritative_main_readback_sha'] = 'UNKNOWN'
    if health is not None:
        evidence = dict(health['deployment_evidence'])
        for key in ('base_checkout_sha', 'cached_origin_main_sha', 'cached_origin_is_live_lookup'):
            evidence.pop(key, None)
        result.update(evidence)
        result.update(runtime_evidence_status='FRESH_PINNED_JOB_EVIDENCE',
                      fast_watch_loaded_code_sha=health.get('loaded_code_source_sha', 'UNKNOWN'),
                      fast_watch_health_generated_at_utc=health['generated_at'],
                      fast_watch_status=health.get('status', 'UNKNOWN'))
    return result


SENTINEL_EVIDENCE_PATH = '/var/lib/sentinel-evidence/preview.json'


def read_sentinel_evidence(path=SENTINEL_EVIDENCE_PATH, now=None):
    """Receipt freshness does not upgrade individual source freshness or analysis."""
    fd=os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('EVIDENCE_NOT_REGULAR_FILE')
        with os.fdopen(fd,'rb',closefd=False) as stream: raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES: raise ValueError('EVIDENCE_TOO_LARGE')
    finally: os.close(fd)
    row=json.loads(raw)
    if (row.get('schema')!='sentinel_public_evidence_v1'
            or row.get('mode')!='EVIDENCE_PREVIEW_ONLY'
            or row.get('confirmation_status')!='ANALYSIS_NOT_PORTED'
            or row.get('capital_authority')!='NONE_SHADOW_ONLY'
            or row.get('real_order_count')!=0 or row.get('real_trading_enabled') is not False
            or row.get('formal_writer') is not False): raise ValueError('EVIDENCE_BOUNDARY_INVALID')
    observed=dt.datetime.fromisoformat(row['generated_at'].replace('Z','+00:00'))
    if observed.tzinfo is None: raise ValueError('EVIDENCE_TIMESTAMP_INVALID')
    age=((now or dt.datetime.now(dt.timezone.utc))-observed).total_seconds()
    if not 0<=age<=600: raise ValueError('EVIDENCE_STALE_OR_FUTURE')
    for key in ('source_sha','evidence_snapshot_main_sha'):
        if not re.fullmatch('[0-9a-f]{40}',row.get(key,'')):raise ValueError('EVIDENCE_SHA_INVALID')
    if any(not (key.startswith('btc_') or key in ('etf_latest_complete','macro_treasury_daily'))
           for key in row['evidence']): raise ValueError('EVIDENCE_SCOPE_INVALID')
    return row
