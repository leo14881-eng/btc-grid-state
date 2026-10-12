#!/usr/bin/env python3
"""Paired full monitor + publication replay, synthetic HTTP and local Git only."""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys


def request_identity(report):
    return sorted(json.dumps({k:r[k] for k in ('category','path','query','lane','bytes','response_sha256')},sort_keys=True)
                  for r in report['http_requests'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--code-ref',default='HEAD')
    p.add_argument('--fixture-ref',default='390fca24a4bcd3537739f0a4caa48dba8e070aab')
    p.add_argument('--iterations',type=int,default=3)
    p.add_argument('--history-delay',type=float,default=.2)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    if not 1<=args.iterations<=100:raise ValueError('INVALID_ITERATIONS')
    code=subprocess.check_output(['git','rev-parse',args.code_ref],text=True).strip()
    output=Path(args.output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    samples=[]
    for i in range(args.iterations):
        pair={}
        for workers in ((1,4) if i%2==0 else (4,1)):
            target=output.with_name(output.stem+f'-pair{i}-workers{workers}.json')
            result=subprocess.run([sys.executable,str(Path(__file__).with_name('hunter_monitor_phase_replay.py')),
                '--code-ref',code,'--fixture-ref',args.fixture_ref,'--history-workers',str(workers),
                '--history-delay',str(args.history_delay),'--output',str(target)],capture_output=True,text=True)
            if result.returncode:raise RuntimeError(result.stdout+result.stderr)
            pair[workers]=json.loads(target.read_text(encoding='utf-8'))
        for key in ('input_manifest_sha256','output_sha256','decisions_events_sha256','request_counts','historical_by_lane'):
            if pair[1][key]!=pair[4][key]:raise AssertionError('AB_MISMATCH '+key)
        if request_identity(pair[1])!=request_identity(pair[4]):raise AssertionError('AB_HTTP_MISMATCH')
        for workers,r in pair.items():
            if not r['published_verified'] or not r['proof_verified']:raise AssertionError('AB_PUBLICATION_NOT_VERIFIED')
            r=dict(r);r.pop('http_requests');r['pair']=i;samples.append(r)
        print('VERIFIED_HISTORY_AB_PAIR',i,flush=True)
    metrics={}
    selectors={'monitor_body':lambda r:r['phases']['monitor_body'],
        'closed_history':lambda r:r['function_seconds']['refresh_closed_observations'],
        'decision_functions':lambda r:r['function_seconds']['manage_existing_positions'],
        'json_writes':lambda r:r['function_seconds']['atomic_json_write'],
        'validation':lambda r:r['phases']['current_state_validation'],
        'publication_and_proof':lambda r:r['phases']['persistence_including_proof'],
        'proof':lambda r:r['function_seconds']['proof_publication_including_readback'],
        'body_validation_publication':lambda r:sum(r['phases'].values())}
    for label,select in selectors.items():
        before=[select(r) for r in samples if r['history_workers']==1]
        after=[select(r) for r in samples if r['history_workers']==4]
        metrics[label]={'serial_median':statistics.median(before),'parallel_median':statistics.median(after),
                        'serial_samples':before,'parallel_samples':after}
    report=dict(code_ref=code,fixture_ref=args.fixture_ref,iterations=args.iterations,
        history_delay=args.history_delay,actual_network_measured=False,synthetic_responses=True,
        p95_p99_supported=False,prepare_tests_and_scheduler_excluded=True,
        exact_outputs_requests_and_final_main_proof_verified=True,metrics=metrics,samples=samples)
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(metrics,indent=2))


if __name__=='__main__':main()
