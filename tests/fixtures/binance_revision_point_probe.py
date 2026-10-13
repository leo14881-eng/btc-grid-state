"""Test-only F5 revision point projection; NEVER production-selected.

The public snapshot is compared against the original full grouping query before
profiling. This substitution does not change store source or SQLite state.
"""
from contextlib import contextmanager
from unittest.mock import patch

from research import hunter_binance_stream_store as source

ORIGINAL_COPY = source._copy_revision_rows


def indexed_copy(db, query, parameters):
    if 'FROM revisions r' not in query or 'GROUP BY topic,start' not in query:
        raise AssertionError('REVISION_POINT_ONLY_EXPECTED_QUERY')
    if len(parameters) < 3 or parameters[0] != parameters[1]:
        raise AssertionError('REVISION_POINT_CUTOFF_OR_SCOPE_INVALID')
    cut = parameters[0]
    names = parameters[2:]
    if not 1 <= len(names) <= 400 or len(set(names)) != len(names):
        raise AssertionError('REVISION_POINT_TOPIC_SCOPE_INVALID')
    for name in names:
        source.split_topic(name)
    marks = ','.join('(?)' for _ in names)
    interval = "substr(req.topic,instr(req.topic,'@kline_')+7)"
    step = 'CASE '+interval+' '+' '.join(
        "WHEN '"+kind+"' THEN "+str(value)
        for kind,value in source.STEPS.items())+' ELSE NULL END'
    limit = 'CASE '+interval+' '+' '.join(
        "WHEN '"+kind+"' THEN "+str(value)
        for kind,value in source.LIMITS.items())+' ELSE NULL END'
    projected = (
        'WITH RECURSIVE requested(topic) AS (VALUES '+marks+'), '
        'req(topic,step,limit_n) AS (SELECT req.topic,'+step+','+limit+' FROM requested req), '
        'seq(n) AS (SELECT 0 UNION ALL SELECT n+1 FROM seq WHERE n+1<?), '
        'wanted(topic,start) AS (SELECT req.topic,((? / req.step)-seq.n)*req.step '
        'FROM req JOIN seq ON seq.n<req.limit_n) '
        'SELECT r.* FROM wanted w JOIN revisions r ON r.topic=w.topic AND r.start=w.start '
        'AND r.source_time=(SELECT MAX(p.source_time) FROM revisions p '
        'WHERE p.topic=w.topic AND p.start=w.start AND p.observed<=? AND p.source_time<=?)'
    )
    return ORIGINAL_COPY(db, projected, (*names,max(source.LIMITS.values()),cut,cut,cut))


@contextmanager
def installed():
    with patch.object(source,'_copy_revision_rows',indexed_copy):
        yield


def prove_equivalence(market, names, stamp):
    """Same SQLite and all original public validity/authority checks."""
    baseline = market.read(names,stamp)
    with installed():
        candidate = market.read(names,stamp)
    if candidate.generation != baseline.generation:
        raise AssertionError('REVISION_POINT_GENERATION_MISMATCH')
    if candidate.fence_sha256 != baseline.fence_sha256:
        raise AssertionError('REVISION_POINT_CONTROL_FENCE_MISMATCH')
    for frozen in (baseline,candidate):
        frozen.check_delivery()
    return dict(same_generation=True,same_fence=True,
                topic_count=len(names),generation_sha256=baseline.generation['generation_sha256'],
                proof_validation_preserved=True,scope='SAME_DB_SAME_READ_CUTOFF_TEST_ONLY')
