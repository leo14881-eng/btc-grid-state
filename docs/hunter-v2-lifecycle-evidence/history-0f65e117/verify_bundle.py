#!/usr/bin/env python3
"""Verify deterministic bundle integrity/counts, optionally against actual Git blobs.

--repo verifies the independent first-parent publication inventory and each cycle
against its real portfolio blob. Re-running generate_history.py additionally
re-executes every historical pure PP function. No working-tree files are read.
"""
import argparse
import collections
import gzip
import hashlib
import json
import pathlib
import subprocess
import sys

FIXED_REF='0f65e11751484a6efbe9fe3e1defe2c310795433'
ENTRY_COMMIT='48610e148c7a29193f54244e7a561c253732ddfe'
EXPECTED={'published_snapshots':1538,'cycle_rows':3076,'unique_decisions':7008,
          'unique_events':2,'legacy_rows':670,'persistent_rows':2406}
TARGETS={'ENA':'SHV2-20261005T075147-ENA-b10cd5','PENDLE':'SHV2-20261005T075147-PENDLE-ea9b45'}


def check(value,message):
    if not value:raise ValueError(message)


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def sha(data):return hashlib.sha256(data).hexdigest()


def verify(bundle,repo=None):
    bundle=pathlib.Path(bundle).resolve(strict=True)
    manifest=json.loads((bundle/'manifest.json').read_bytes())
    check(manifest['baseline_main_sha']==FIXED_REF,'Wrong pinned ref')
    check(manifest['counts']==EXPECTED,'Manifest counts differ')
    for name,record in manifest['files'].items():
        check(pathlib.Path(name).name==name,'Unsafe manifest path')
        data=(bundle/name).read_bytes()
        check(sha(data)==record['sha256'] and len(data)==record['bytes'],'File hash/size mismatch: '+name)
    datasets={}
    for record in manifest['artifacts']:
        name=record['file'];check(pathlib.Path(name).name==name,'Unsafe artifact path')
        data=(bundle/name).read_bytes()
        check(sha(data)==record['compressed_sha256'],'Compressed hash mismatch: '+name)
        check(data[:3]==b'\x1f\x8b\x08' and data[3]==0 and data[4:8]==b'\0'*4,
              'Non-deterministic gzip header: '+name)
        raw=gzip.decompress(data)
        check(sha(raw)==record['uncompressed_sha256'] and len(raw)==record['uncompressed_bytes'],
              'Uncompressed hash/size mismatch: '+name)
        check(raw.endswith(b'\n'),'Missing JSONL trailing newline')
        rows=[json.loads(line) for line in raw.splitlines()]
        check(len(rows)==record['rows'],'JSONL row-count mismatch: '+name)
        check(raw==b''.join(canonical(row)+b'\n' for row in rows),'Noncanonical JSONL: '+name)
        datasets[name]=rows
    check(set(datasets)=={'cycles.jsonl.gz','decisions.jsonl.gz','publications.jsonl.gz','source_objects.jsonl.gz'},
          'Unexpected artifact set')
    summary=json.loads((bundle/'summary.json').read_bytes())
    check(summary['baseline_main_sha']==FIXED_REF and summary['counts']==EXPECTED,'Summary mismatch')
    cycles=datasets['cycles.jsonl.gz'];publications=datasets['publications.jsonl.gz']
    decisions=datasets['decisions.jsonl.gz'];events=summary['target_events']
    counts={'published_snapshots':len(publications),'cycle_rows':len(cycles),
            'unique_decisions':len(decisions),'unique_events':len(events),
            'legacy_rows':sum(r['phase']=='LEGACY_PP_REFERENCE_COST_PREDICATE' for r in cycles),
            'persistent_rows':sum(r['phase']=='PERSISTED_EXIT_ESTIMATE_PP_STATE' for r in cycles)}
    check(counts==EXPECTED,'Actual artifact counts differ')
    check(len({sha(canonical({k:v for k,v in d.items() if k!='first_observed_commit'})) for d in decisions})==len(decisions),
          'Duplicate unique decision')
    check(len({r['commit'] for r in publications})==len(publications),'Duplicate publication')
    by_commit=collections.defaultdict(list)
    for row in cycles:by_commit[row['commit']].append(row)
    check(set(by_commit)=={p['commit'] for p in publications},'Publication/cycle set mismatch')
    for commit,rows in by_commit.items():
        check(len(rows)==2 and {r['shadow_id'] for r in rows}==set(TARGETS.values()),'Missing/duplicate target cycle')
        check(all(not r['pp_exit_predicate_and_positive_net'] for r in rows),'Unexpected positive PP predicate')
        check(all(r['decisions'] and not r['duplicate_mark'] for r in rows),'Empty/duplicate cycle')
    for asset,target in TARGETS.items():
        rows=[r for r in cycles if r['asset']==asset]
        check(len(rows)==1538 and all(r['shadow_id']==target for r in rows),'Asset count/identity mismatch')
        check(len({canonical(r['tranches']) for r in rows})==1,'Tranche mutation')
        check(len({r['at'] for r in rows})==len(rows),'Duplicate mark')
        check(all(r['persisted_state_matches_replay'] and r['replayed_protection_result']['evidence_status']=='FRESH'
                  and r['generation']==r['actual_protection_lifecycle']['last_generation_id']
                  for r in rows if r['phase']=='PERSISTED_EXIT_ESTIMATE_PP_STATE'),'Persistent proof mismatch')
    check(all(e['type']=='SHADOW_V2_BUY' for e in events) and {e['shadow_id'] for e in events}==set(TARGETS.values()),
          'Unexpected target events')
    source_checked=False
    if repo:
        # Import only the bundled verified helper, never a repository entry point.
        import generate_history as helper
        check(sha(pathlib.Path(helper.__file__).read_bytes())==manifest['files']['generate_history.py']['sha256'],
              'Loaded generator helper differs from the verified bundle')
        from generate_history import Git, PATHS, strict_json
        g=Git(repo)
        try:
            check(g.run('rev-parse','--verify',FIXED_REF+'^{commit}').decode().strip()==FIXED_REF,'Pinned source missing')
            chain=g.run('rev-list','--reverse','--first-parent',FIXED_REF).decode().splitlines()
            check(ENTRY_COMMIT in chain,'Entry absent from first-parent source')
            start=chain.index(ENTRY_COMMIT);check(start>0,'Incomplete entry ancestry')
            previous=g.path(chain[start-1],PATHS['portfolio']);actual_inventory=[]
            for commit in chain[start:]:
                blob=g.path(commit,PATHS['portfolio'])
                check(blob is not None,'Source portfolio deleted')
                if blob!=previous:
                    actual_inventory.append((commit,blob));previous=blob
            check(actual_inventory==[(p['commit'],p['portfolio_blob']) for p in publications],
                  'Independent first-parent source inventory mismatch')
            objects=datasets['source_objects.jsonl.gz']
            check(len({o['oid'] for o in objects})==len(objects),'Duplicate source object')
            object_lines=subprocess.check_output(g.command('cat-file','--batch-check'),
                input=''.join(o['oid']+'\n' for o in objects).encode(),env=g.env).decode().splitlines()
            check(len(object_lines)==len(objects) and all(len(line.split())==3 for line in object_lines),
                  'Missing source object; no lazy fetch is permitted')
            source_decisions={}
            for index,pub in enumerate(publications):
                commit=pub['commit'];raw=g.read(pub['portfolio_blob'],'blob');state=strict_json(raw)
                for row in by_commit[commit]:
                    check(sha(raw)==row['portfolio_sha256'],'Portfolio SHA256 mismatch')
                    for key,oid in row['source_blobs'].items():
                        check(g.path(commit,PATHS[key])==oid,'Source blob attribution mismatch')
                    matches=[(location,p) for location in ('open_positions','closed_positions','closed_trade_archive')
                             for p in state.get(location,[]) if p.get('shadow_id')==row['shadow_id']]
                    check(len(matches)==1 and matches[0][0]=='open_positions','Source target missing/duplicate/closed')
                    pos=matches[0][1]
                    for source_key,row_key in [('last_marked_at_utc','at'),('last_price','price'),('tranches','tranches'),
                        ('mfe_pct','mfe_pct'),('mae_pct','mae_pct'),('health_state','health_state'),
                        ('last_exit_estimate','actual_last_exit_estimate'),('protection_lifecycle','actual_protection_lifecycle')]:
                        check(pos.get(source_key)==row.get(row_key),'Cycle does not match source position: '+source_key)
                    ds=[d for d in state.get('decisions',[]) if d.get('shadow_id')==row['shadow_id'] and d.get('at')==row['at']]
                    check(ds==row['decisions'],'Cycle decision source mismatch')
                es=[e for e in state.get('events',[]) if e.get('shadow_id') in set(TARGETS.values())]
                check(es==events,'Target source events differ')
                for decision in state.get('decisions',[]):
                    if decision.get('shadow_id') in set(TARGETS.values()):
                        source_decisions.setdefault(sha(canonical(decision)),dict(decision,first_observed_commit=commit))
                if (index+1)%200==0:print(json.dumps({'git_publications_verified':index+1}),flush=True)
            check(list(source_decisions.values())==decisions,'Unique decisions differ from actual source history')
            source_checked=True
        finally:g.close()
    return {'status':'PASS','baseline_main_sha':FIXED_REF,'counts':counts,
            'git_sources_verified':source_checked,'manifest_sha256':sha((bundle/'manifest.json').read_bytes()),
            'scope':'Bundle integrity and source correspondence; generator replay scope/limitations remain in summary.json'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',required=True)
    parser.add_argument('--repo',help='Optional full clone for actual Git-source verification')
    args=parser.parse_args()
    print(json.dumps(verify(args.bundle,args.repo),ensure_ascii=False),flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('BUNDLE_VERIFICATION_FAILED: '+str(exc),file=sys.stderr)
        sys.exit(1)
