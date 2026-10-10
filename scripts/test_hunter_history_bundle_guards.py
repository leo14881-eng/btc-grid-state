#!/usr/bin/env python3
"""Exercise deterministic compression and fail-closed bundle/source checks.

Writes only the explicit new --out directory. Corrupt test copies are retained
there for inspection; the input bundle and Git repository remain untouched.
"""
import argparse
import gzip
import hashlib
import io
import json
import pathlib
import shutil
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',required=True)
    parser.add_argument('--repo',required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    bundle=pathlib.Path(args.bundle).resolve(strict=True)
    out=pathlib.Path(args.out).resolve()
    if out.exists():raise ValueError('Test output must not exist')
    out.mkdir(parents=True)
    sys.path.insert(0,str(bundle))
    import generate_history as generator
    import verify_bundle as verifier
    results=[]

    def rejected(name,run,expected):
        try:run()
        except Exception as exc:
            if expected not in str(exc):raise
            results.append({'test':name,'status':'PASS','rejection':str(exc)})
        else:raise ValueError('Invalid evidence accepted: '+name)

    verifier.verify(bundle)
    results.append({'test':'valid_bundle_integrity','status':'PASS'})
    for path in sorted(bundle.glob('*.gz')):
        stream=io.BytesIO()
        with gzip.GzipFile(filename='',fileobj=stream,mode='wb',mtime=0,compresslevel=9) as gz:
            gz.write(gzip.decompress(path.read_bytes()))
        if stream.getvalue()!=path.read_bytes():raise ValueError('Compression not deterministic: '+path.name)
    results.append({'test':'all_gzip_bytes_reproducible','status':'PASS'})

    corrupt=out/'corrupt-compressed';shutil.copytree(bundle,corrupt)
    data=bytearray((corrupt/'cycles.jsonl.gz').read_bytes());data[-5]^=1
    (corrupt/'cycles.jsonl.gz').write_bytes(data)
    rejected('compressed_corruption_rejected',lambda:verifier.verify(corrupt),'Compressed hash mismatch')

    counts=out/'wrong-counts';shutil.copytree(bundle,counts)
    manifest=json.loads((counts/'manifest.json').read_bytes());manifest['counts']['cycle_rows']-=1
    generator.write_json(counts/'manifest.json',manifest)
    rejected('count_mismatch_rejected',lambda:verifier.verify(counts),'Manifest counts differ')

    summary=out/'changed-summary';shutil.copytree(bundle,summary)
    data=json.loads((summary/'summary.json').read_bytes());data['assets']['ENA']['health_state']='FORGED'
    generator.write_json(summary/'summary.json',data)
    rejected('summary_corruption_rejected',lambda:verifier.verify(summary),'File hash/size mismatch')

    forged=out/'forged-cycle-rehashed';shutil.copytree(bundle,forged)
    rows=[json.loads(x) for x in gzip.decompress((forged/'cycles.jsonl.gz').read_bytes()).splitlines()]
    rows[0]['price']+=0.123
    artifact=generator.write_gzip_rows(forged/'cycles.jsonl.gz',rows)
    manifest=json.loads((forged/'manifest.json').read_bytes())
    manifest['artifacts']=[artifact if x['file']=='cycles.jsonl.gz' else x for x in manifest['artifacts']]
    generator.write_json(forged/'manifest.json',manifest)
    rejected('forged_cycle_with_recomputed_hashes_rejected_by_git',
             lambda:verifier.verify(forged,args.repo),'Cycle does not match source position: last_price')
    rejected('unpinned_ref_rejected',lambda:generator.generate(args.repo,'0'*40,out/'wrong-ref'),
             'deliberately pinned')
    report={'status':'PASS','tests':results,
            'tested_manifest_sha256':hashlib.sha256((bundle/'manifest.json').read_bytes()).hexdigest(),
            'source_repository_modified':False}
    generator.write_json(out/'guard_test_results.json',report)
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
