"""Offline CI measurements; never invokes an engine, service or publication."""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def measure(command, directory):
    metrics = directory / 'time.json'
    with (directory / 'command.log').open('wb') as log:
        result = subprocess.run(['/usr/bin/time', '-f', '{"seconds":%e,"peak_rss_kib":%M}', '-o', str(metrics), *command],
                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=240)
    if result.returncode:
        print((directory / 'command.log').read_text()[-12000:], flush=True)
        raise RuntimeError('OFFLINE_VALIDATION_FAILED')
    return json.loads(metrics.read_text())


def summarize(rows):
    values = sorted(r['seconds'] for r in rows)
    return dict(n=len(values), median_seconds=statistics.median(values),
                p95_seconds=values[math.ceil(len(values)*.95)-1],
                p99_seconds=values[math.ceil(len(values)*.99)-1],
                quantile_method='nearest_rank; small samples do not estimate production tails',
                max_peak_rss_kib=max(r['peak_rss_kib'] for r in rows))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    samples = {name: [] for name in ('pure_original', 'pure_cold_compare', 'pure_hot_compare', 'pure_hot_read_only',
                                    'full_original', 'full_cold_compare', 'full_hot_compare')}
    pure = [sys.executable, '-I', '-S', '-B', str(ROOT / 'scripts/hunter_validation_receipt.py'), '--child']
    full_code = "import subprocess,sys; subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_hunter*.py'],check=True)"
    with tempfile.TemporaryDirectory(prefix='hunter-receipt-benchmark-') as temp:
        directory = Path(temp)
        keydir = directory / 'keys'; keydir.mkdir(mode=0o700)
        key = keydir / 'key'
        with os.fdopen(os.open(key, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'wb') as stream: stream.write(os.urandom(32))
        for kind, count in (('pure', 30), ('full', 3)):
            for i in range(count):
                cache = directory / ('%s-%s' % (kind, i)); cache.mkdir(mode=0o700)
                env = dict(HUNTER_VALIDATION_RECEIPTS='compare', HUNTER_VALIDATION_RECEIPT_DIR=str(cache), HUNTER_VALIDATION_RECEIPT_KEY=str(key))
                def observation(expected, read_only=False):
                    local = dict(env, HUNTER_VALIDATION_RECEIPTS='read-only' if read_only else 'compare')
                    return ("from scripts.hunter_validation_receipt import observe; "
                            "r=observe(%r); print(r); assert r['status']=='OBSERVED_ONLY' and r['hit'] is %r and r['tests_skipped']==0" % (local, expected))
                baseline = pure if kind == 'pure' else [sys.executable, '-B', '-c', full_code]
                samples[kind+'_original'].append(measure(baseline, directory))
                for label, expected in (('cold', False), ('hot', True)):
                    code = (full_code+'; ' if kind == 'full' else '') + observation(expected)
                    samples[kind+'_'+label+'_compare'].append(measure([sys.executable, '-B', '-c', code], directory))
                if kind == 'pure':
                    samples['pure_hot_read_only'].append(measure([sys.executable, '-B', '-c', observation(True, True)], directory))
                print('OFFLINE_BENCHMARK_PROGRESS', kind, i+1, count, flush=True)
    report = dict(scope='GitHub Linux offline, identical checked-out inputs; no production speedup claim',
                  source=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
                  python=sys.version, samples=samples, summary={name:summarize(rows) for name,rows in samples.items()},
                  notes=['Full samples include original complete discovery each time; comparison adds work.',
                         'Candidate has seven tests; both cold and hot comparison run all seven.',
                         'No CPU/disk page-cache flushing. Cold means no receipt; hot means valid receipt.',
                         'Peak RSS is /usr/bin/time maximum resident set for command, not simultaneous tree total.',
                         'P95/P99 with n=3 or 30 mostly reflect maxima and are not production tail estimates.'])
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
    print(json.dumps(report['summary'], indent=2), flush=True)


if __name__ == '__main__': main()
