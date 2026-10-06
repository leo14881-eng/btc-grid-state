"""Read-only version view over existing job health; never fetch or publish state.

The caller supplies a pinned evidence ref and the server connector's base-checkout
response. Cached origin/main is labelled as cached, never a live GitHub lookup.
"""
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import subprocess

UNKNOWN = 'UNKNOWN'
JOBS = ('discovery', 'research', 'monitor', 'watchdog')


def deployment_view(server, jobs, scheduler_raw, evidence_sha, is_ancestor, target=None):
    def cached_value(key):
        record = server.get(key, {})
        return record.get('stdout', '').strip() if record.get('returncode') == 0 else UNKNOWN
    result = {'schema': 'hunter_deployment_version_view_v1',
              'base_checkout_sha': cached_value('head'),
              'cached_origin_main_sha': cached_value('cached_origin_main'),
              'cached_origin_is_live_lookup': False,
              'evidence_snapshot_sha': evidence_sha,
              'authoritative_main_readback_sha': UNKNOWN,
              'job_source_provenance': {}, 'contains_target_commit': {},
              'readback_provenance': 'UNKNOWN', 'read_only': True}
    scheduler = json.loads(scheduler_raw) if scheduler_raw else {}
    verified = []
    for job in JOBS:
        health = jobs.get(job, {})
        source = health.get('source_head_sha')
        provenance = 'PUBLISHED_JOB_HEAD; START_HEAD_NOT_SEPARATELY_RECORDED'
        coherent = True
        if job == 'monitor':
            coherent = (health.get('monitor_generation_id') == scheduler.get('current_generation_id')
                        == scheduler.get('last_successful_monitor_generation_id')
                        and health.get('scheduler_health_sha256') == hashlib.sha256(scheduler_raw.encode()).hexdigest())
            source = scheduler.get('state_revision') if coherent else None
            provenance = 'MATCHED_SCHEDULER_STATE_REVISION' if coherent else 'MONITOR_PROOF_MISMATCH'
        source = source or UNKNOWN
        result['last_' + job + '_source_sha'] = source
        result['job_source_provenance'][job] = provenance if source != UNKNOWN else 'UNKNOWN'
        result['contains_target_commit'][job] = (
            is_ancestor(target, source) if target and source != UNKNOWN else UNKNOWN)
        readback = health.get('main_readback_head_sha')
        if (coherent and health.get('status') == 'SUCCESS'
                and health.get('source') == 'VULTR_SYSTEMD'
                and health.get('main_readback_verified') is True
                and health.get('capital_authority') == 'NONE_SHADOW_ONLY'
                and health.get('real_trading_enabled') is False
                and readback and is_ancestor(readback, evidence_sha)):
            try:
                completed = dt.datetime.fromisoformat(health['completed_at_utc'].replace('Z', '+00:00'))
                if completed.tzinfo is not None:
                    verified.append((completed, job, readback))
            except (KeyError, ValueError, TypeError):
                pass
    if verified:
        _, job, readback = max(verified)
        result.update(authoritative_main_readback_sha=readback,
                      readback_provenance='LATEST_VERIFIED_JOB_READBACK:' + job)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--evidence-ref', required=True,
                        help='Explicit pinned/fetched ref; this command never fetches it')
    parser.add_argument('--server-status-json', type=pathlib.Path)
    parser.add_argument('--target-commit')
    args = parser.parse_args()
    def git(*argv):
        return subprocess.run(['git', '-C', args.repo, *argv], capture_output=True, text=True)
    resolved = git('rev-parse', '--verify', args.evidence_ref + '^{commit}')
    if resolved.returncode:
        parser.error('EVIDENCE_REF_NOT_AVAILABLE')
    sha = resolved.stdout.strip()
    def read(path):
        output = git('show', sha + ':' + path)
        return output.stdout if output.returncode == 0 else ''
    jobs = {job: json.loads(raw) if raw else {} for job in JOBS
            for raw in [read('research/results/hunter-runtime-' + job + '-health.json')]}
    server = json.loads(args.server_status_json.read_text()) if args.server_status_json else {}
    def ancestor(a, b):
        return git('merge-base', '--is-ancestor', a, b).returncode == 0
    print(json.dumps(deployment_view(server, jobs,
        read('research/results/hunter-scheduler-health.json'), sha, ancestor, args.target_commit), indent=2))


if __name__ == '__main__':
    main()
