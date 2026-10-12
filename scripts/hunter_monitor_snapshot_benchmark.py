#!/usr/bin/env python3
"""Paired offline A/B on copied data and file-only Git; never run a strategy."""
import argparse
import contextlib
import io
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from unittest import mock

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import hunter_monitor_persist as publisher
from scripts import hunter_lock_capacity as capacity


def legacy_snapshot(ref=None):
    # Frozen baseline from 9e97739; keep the seven independent Git reads.
    return {name: (publisher.git('show', f'{ref}:{publisher.RESULTS}{name}').stdout if ref
                   else Path(publisher.RESULTS + name).read_text()) for name in publisher.NAMES}


def measure(fn, *args):
    start = time.perf_counter()
    value = fn(*args)
    return value, time.perf_counter() - start


def summary(samples):
    ordered = sorted(samples)
    return dict(n=len(samples), median=statistics.median(samples),
                empirical_p95=ordered[math.ceil(.95 * len(samples)) - 1],
                empirical_p99=ordered[math.ceil(.99 * len(samples)) - 1],
                maximum=max(samples), seconds=samples,
                tail_sla_established=False)


def run(source, ref, iterations, cycles):
    source = Path(source).resolve()
    before = capacity.working_manifest(source)
    raw, source_info = capacity.read_source(source, ref)
    payload_hash = capacity.stable_payload(raw)
    candidate = publisher.snapshot
    result = dict(schema='hunter_snapshot_ab_v1', source=source_info,
                  baseline_code_sha='9e97739a3e52db0979d3100284e960f47db68984',
                  candidate_code_sha=capacity.git_at(source, 'rev-parse', 'HEAD').stdout.strip(),
                  candidate_source_sha256=capacity.digest((source / 'scripts/hunter_monitor_persist.py').read_bytes()),
                  environment=dict(python=sys.version, platform=platform.platform(),
                                   git=capacity.git_at(source, '--version').stdout.strip()),
                  real_trading_enabled=False, capital_authority='NONE_SHADOW_ONLY',
                  network_measured=False, primary_proof_measured=False,
                  note='Empirical quantiles only; no production p95/p99 or whole-cycle SLA.')
    with tempfile.TemporaryDirectory(prefix='hunter-snapshot-ab-') as directory:
        root = Path(directory).resolve()
        remote, seed = root / 'origin.git', root / 'seed'
        capacity.git_at(root, 'init', '--bare', '--initial-branch=main', str(remote))
        capacity.git_at(root, 'clone', str(remote), str(seed))
        for key, value in [('user.name', 'offline-ab'), ('user.email', 'offline-ab@localhost')]:
            capacity.git_at(seed, 'config', key, value)
        capacity.write_snapshot(seed, raw)
        capacity.git_at(seed, 'add', '.')
        capacity.git_at(seed, 'commit', '-m', 'isolated snapshot baseline')
        capacity.git_at(seed, 'push', 'origin', 'main')
        samples = {'legacy': [], 'batch': []}
        commands = {}
        def safe_git(*args, **kwargs):
            commands[args[0]] = commands.get(args[0], 0) + 1
            return capacity.git_at(seed, *args, **kwargs)
        with mock.patch.object(publisher, 'git', side_effect=safe_git), contextlib.redirect_stdout(io.StringIO()):
            for i in range(iterations):
                for name, fn in ([('legacy', legacy_snapshot), ('batch', candidate)][::1 if i % 2 == 0 else -1]):
                    commands.clear()
                    actual, seconds = measure(fn, 'main')
                    publisher.require(actual == raw, 'AB_SNAPSHOT_CONTENT_MISMATCH')
                    samples[name].append(seconds)
                    result[name + '_snapshot_git_calls'] = dict(commands)
        result['snapshot'] = {name: summary(values) for name, values in samples.items()}

        # Both arms start with the same bytes, independent bare main and object
        # history. Alternate order; retain real CAS, commit, push and readback.
        samples = {'legacy': [], 'batch': []}
        writers = {}
        for name in samples:
            arm_remote, writer = root / (name + '.git'), root / name
            capacity.git_at(root, 'clone', '--bare', str(remote), str(arm_remote))
            capacity.git_at(root, 'clone', str(arm_remote), str(writer))
            for key, value in [('user.name', 'offline-ab'), ('user.email', 'offline-ab@localhost')]:
                capacity.git_at(writer, 'config', key, value)
            writers[name] = writer
        original_directory = Path.cwd()
        try:
            for i in range(cycles):
                for name, fn in ([('legacy', legacy_snapshot), ('batch', candidate)][::1 if i % 2 == 0 else -1]):
                    writer = writers[name]
                    os.chdir(writer)
                    local_git = lambda *a, **kw: capacity.git_at(writer, *a, **kw)
                    base = local_git('rev-parse', 'HEAD').stdout.strip()
                    generation = '2000-01-01T00:%02d:00Z' % i
                    docs = capacity.next_snapshot(capacity.parse(raw), generation)
                    expected = capacity.serialize(docs)
                    capacity.write_snapshot(writer, expected)
                    log = io.StringIO()
                    with mock.patch.object(publisher, 'git', side_effect=local_git), \
                         mock.patch.object(publisher, 'snapshot', side_effect=fn), \
                         mock.patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), \
                         contextlib.redirect_stdout(log):
                        _, seconds = measure(publisher.persist, base, generation)
                        actual = publisher.snapshot('origin/main')
                    publisher.require('SERVER_POST_PUSH_READBACK_OK' in log.getvalue(), 'AB_READBACK_MISSING')
                    publisher.require(actual == expected, 'AB_PUBLICATION_CONTENT_MISMATCH')
                    publisher.require(capacity.stable_payload(actual) == payload_hash, 'AB_HISTORY_MUTATED')
                    samples[name].append(seconds)
            for writer in writers.values():
                count = int(capacity.git_at(writer, 'rev-list', '--count', 'HEAD').stdout)
                publisher.require(count == cycles + 1, 'AB_COMMIT_COUNT_MISMATCH')
        finally:
            os.chdir(original_directory)
        result['publication_including_cas_push_readback'] = {name: summary(values) for name, values in samples.items()}
        result['history_and_capital_unchanged'] = True
    result['source_worktree_unchanged'] = before == capacity.working_manifest(source)
    publisher.require(result['source_worktree_unchanged'], 'AB_SOURCE_MUTATED')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', default='.')
    p.add_argument('--source-ref', default='HEAD')
    p.add_argument('--iterations', type=int, default=100)
    p.add_argument('--cycles', type=int, default=12)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    publisher.require(2 <= args.iterations <= 1000 and 2 <= args.cycles <= 50, 'INVALID_AB_SAMPLE_COUNT')
    result = run(args.source, args.source_ref, args.iterations, args.cycles)
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    for stage in ('snapshot', 'publication_including_cas_push_readback'):
        print(stage, json.dumps({arm: {k: v for k, v in metrics.items() if k != 'seconds'}
                                for arm, metrics in result[stage].items()}))


if __name__ == '__main__':
    main()
