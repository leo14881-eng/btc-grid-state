"""Bounded, read-only public BTC evidence on demand. No main or artifact writes."""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import threading
import time

CODE_ROOT = Path('/opt/shadow-ops-mcp/sentinel-evidence-code')
CACHE_SECONDS = 30
ADMISSION_TIMEOUT_SECONDS = 5
_lock = threading.Lock()
_cache = None


def load_collector():
    # Operator-installed immutable code, never a caller-selected path or URL.
    manifest = json.loads((CODE_ROOT / 'manifest.json').read_text())
    sha = manifest['source_sha']
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('COLLECTOR_SOURCE_SHA_INVALID')
    file = CODE_ROOT / 'sentinel_runtime.py'
    if hashlib.sha256(file.read_bytes()).hexdigest() != manifest['collector_sha256']:
        raise ValueError('COLLECTOR_CODE_HASH_MISMATCH')
    spec = importlib.util.spec_from_file_location('sentinel_public_collector', file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, sha


def read_live_evidence():
    global _cache
    if not _lock.acquire(timeout=ADMISSION_TIMEOUT_SECONDS):
        raise ValueError('LIVE_EVIDENCE_COLLECTION_BUSY')
    try:
        collector, sha = load_collector()
        monotonic = time.monotonic()
        now = dt.datetime.now(dt.timezone.utc)
        if _cache is not None:
            cached_at, cached_sha, row = _cache
            age = (now - dt.datetime.fromisoformat(row['generated_at'])).total_seconds()
            if cached_sha == sha and 0 <= monotonic-cached_at < CACHE_SECONDS and 0 <= age < CACHE_SECONDS:
                return copy.deepcopy(row)  # preserve original source/receipt times
        evidence = collector.collect()  # existing fixed official GETs, six workers, per-request timeout
        completed = dt.datetime.now(dt.timezone.utc)
        mutation = collector.scan({}, evidence, completed)
        row = collector.public_evidence(mutation, sha, sha)
        row.update(collection_trigger='ON_DEMAND_PUBLIC_GET',
                   collector_code_sha=sha,
                   evidence_snapshot_scope='COLLECTOR_CODE_BASELINE_NOT_MAIN_STATE_READBACK',
                   main_readback_verified=False,
                   collection_duration_seconds=time.monotonic()-monotonic)
        # Do not write the hourly artifact, git, formal Sentinel state or portfolio.
        _cache = (time.monotonic(), sha, copy.deepcopy(row))
        return row
    finally:
        _lock.release()
