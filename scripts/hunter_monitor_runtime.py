"""Monitor primary/fallback admission based only on post-push read-back proof."""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile

from scripts.hunter_monitor_persist import git, require

PROOF_PATH = 'research/results/hunter-runtime-monitor-health.json'
HEALTH_PATH = 'research/results/hunter-scheduler-health.json'
MAX_AGE_SECONDS = 600


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def should_run(config, proof, health_raw, now, is_ancestor):
    if config.get('shadow_only') is not True or config.get('primary_jobs', {}).get('monitor') is not True:
        return True
    try:
        h = json.loads(health_raw)
        completed = dt.datetime.fromisoformat(proof['completed_at_utc'].replace('Z', '+00:00'))
        scan_completed = dt.datetime.fromisoformat(h['monitor_completed_at_utc'].replace('Z', '+00:00'))
        age = (now - completed).total_seconds()
        scan_age = (now - scan_completed).total_seconds()
        commit = proof['main_readback_head_sha']
        valid = (
            proof.get('schema') == 'hunter_runtime_job_health_v1'
            and proof.get('job') == 'monitor' and proof.get('status') == 'SUCCESS'
            and proof.get('source') == 'VULTR_SYSTEMD'
            and proof.get('capital_authority') == 'NONE_SHADOW_ONLY'
            and proof.get('real_trading_enabled') is False
            and proof.get('main_readback_verified') is True
            and h.get('schema') == 'hunter_scheduler_health_v1'
            and h.get('trigger_source') == 'VULTR_SYSTEMD'
            and h.get('shadow_only') is True
            and type(h.get('real_order_count')) is int and h['real_order_count'] == 0
            and proof.get('monitor_generation_id') == h.get('current_generation_id')
            == h.get('last_successful_monitor_generation_id')
            and proof.get('scheduler_health_sha256') == digest(health_raw)
            and 0 <= age < MAX_AGE_SECONDS and 0 <= scan_age < MAX_AGE_SECONDS
            and isinstance(commit, str) and len(commit) == 40 and is_ancestor(commit))
        return not valid
    except (KeyError, TypeError, ValueError, OverflowError):
        return True


def publish_verified(expected, generation, commit):
    # Called ONLY after the complete monitor snapshot was read back exactly.
    health_raw = expected['hunter-scheduler-health.json']
    h = json.loads(health_raw)
    if h.get('trigger_source') != 'VULTR_SYSTEMD' or os.environ.get('GITHUB_ACTIONS') == 'true':
        return  # GitHub fallback may never advertise itself as the primary.
    doc = dict(schema='hunter_runtime_job_health_v1', job='monitor', status='SUCCESS',
               source='VULTR_SYSTEMD', capital_authority='NONE_SHADOW_ONLY',
               real_trading_enabled=False, main_readback_verified=True,
               main_readback_head_sha=commit, monitor_generation_id=generation,
               scheduler_health_sha256=digest(health_raw),
               completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
    raw = json.dumps(doc, indent=2, sort_keys=True) + '\n'
    remote = git('remote', 'get-url', 'origin').stdout.strip()
    for attempt in range(5):
        with tempfile.TemporaryDirectory(prefix='monitor-proof-') as directory:
            subprocess.run(['git', 'clone', '--quiet', '--shared', '.', directory], check=True)
            def aux(*args, check=True):
                return subprocess.run(['git', '-C', directory, *args], check=check,
                                      capture_output=True, text=True)
            aux('remote', 'set-url', 'origin', remote)
            aux('fetch', 'origin', 'main')
            require(aux('merge-base', '--is-ancestor', commit, 'origin/main', check=False).returncode == 0,
                    'MONITOR_PROOF_COMMIT_NOT_AUTHORITATIVE')
            require(aux('show', 'origin/main:' + HEALTH_PATH).stdout == health_raw,
                    'MONITOR_PROOF_REJECTED_STALE_GENERATION')
            aux('checkout', '--detach', 'origin/main')
            target = pathlib.Path(directory, PROOF_PATH)
            target.write_text(raw)
            aux('config', 'user.name', 'hunter-vultr-shadow')
            aux('config', 'user.email', 'hunter-vultr-shadow@localhost')
            aux('add', '-f', '--', PROOF_PATH)
            aux('commit', '-m', 'ops: verified Hunter monitor primary health')
            pushed = aux('push', 'origin', 'HEAD:main', check=False)
            if pushed.returncode:
                require(not any(token in pushed.stderr for token in
                                ('could not read Username', 'Authentication failed',
                                 'Permission denied', 'error: 403')),
                        'MONITOR_PROOF_PUSH_AUTHENTICATION_FAILED')
                continue
            aux('fetch', 'origin', 'main')
            require(aux('show', 'origin/main:' + PROOF_PATH).stdout == raw,
                    'MONITOR_PROOF_READBACK_MISMATCH')
            require(aux('show', 'origin/main:' + HEALTH_PATH).stdout == health_raw,
                    'MONITOR_PROOF_SUPERSEDED_GENERATION')
            print('HUNTER_MONITOR_PRIMARY_PROOF_VERIFIED', generation, flush=True)
            return
    raise RuntimeError('MONITOR_PROOF_PUSH_RETRY_EXHAUSTED')


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['gate'])
    p.parse_args()
    try:
        health_raw = pathlib.Path(HEALTH_PATH).read_text()
    except OSError:
        health_raw = '{}'
    run = should_run(load('.github/hunter-runtime.json'), load(PROOF_PATH), health_raw,
                     dt.datetime.now(dt.timezone.utc),
                     lambda c: git('merge-base', '--is-ancestor', c, 'origin/main', check=False).returncode == 0)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            f.write('run=' + str(run).lower() + '\n')
    print('HUNTER_MONITOR_RUNTIME_GATE', 'RUN_BACKUP' if run else 'VULTR_RECENT_VERIFIED_SKIP')


if __name__ == '__main__':
    main()
