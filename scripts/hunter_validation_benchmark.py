"""Offline CI measurements; never invokes an engine, service or publication."""
import argparse
import json
import math
import os
import re
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def unittest_counts(output):
    """Read the final complete unittest CLI footer; missing is never zero tests."""
    matches = re.findall(r'^Ran (\d+) tests? in [0-9.]+s\r?\n\r?\n(OK(?: \([^\n]*\))?|FAILED \([^\n]*\))\r?$', output, re.M)
    if not matches: raise ValueError('FULL_SUITE_SUMMARY_MISSING')
    total, summary = matches[-1]
    counts = dict(tests=int(total), skipped=0, failures=0, errors=0,
                  expected_failures=0, unexpected_successes=0)
    if '(' in summary:
        for field in summary.split('(',1)[1].rstrip(')\r').split(', '):
            name, value = field.split('=')
            name = name.replace(' ', '_')
            if name not in counts or name == 'tests': raise ValueError('UNKNOWN_UNITTEST_COUNT')
            counts[name] = int(value)
    return counts


def measure(command, directory, full=False):
    metrics = directory / 'time.json'
    started = time.monotonic()
    with (directory / 'command.log').open('wb') as log:
        try:
            result = subprocess.run(['/usr/bin/time', '-q', '-f', '{"seconds":%e,"peak_rss_kib":%M}', '-o', str(metrics), *command],
                                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=240)
            row = json.loads(metrics.read_text())
            row['returncode'] = result.returncode
        except subprocess.TimeoutExpired:
            row = dict(seconds=round(time.monotonic()-started, 6), peak_rss_kib=None,
                       returncode=None, measurement_error='COMMAND_TIMEOUT')
    output = (directory / 'command.log').read_text(errors='replace')
    if full:
        try: row['test_counts'] = unittest_counts(output)
        except ValueError:
            row.update(test_counts=None, measurement_error='FULL_SUITE_SUMMARY_MISSING')
    if row['returncode'] != 0 or row.get('measurement_error'):
        print(output[-12000:], flush=True)
    return row


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
    pure = [sys.executable, '-B', '-c', 'from scripts.hunter_validation_receipt import run_candidate; assert run_candidate()']
    full_code = "import subprocess,sys; subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_hunter*.py'],check=True)"
    complete = False
    try:
      with tempfile.TemporaryDirectory(prefix='hunter-receipt-benchmark-') as temp:
        directory = Path(temp)
        def record(name, command):
            row = measure(command, directory, full=name.startswith('full_'))
            samples[name].append(row)
            counts = row.get('test_counts')
            if (row['returncode'] != 0 or row.get('measurement_error') or
                (name.startswith('full_') and (not counts or counts['tests'] <= 0 or
                 any(counts[k] for k in ('failures','errors','unexpected_successes'))))):
                raise RuntimeError('OFFLINE_VALIDATION_FAILED')
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
                record(kind+'_original', baseline)
                for label, expected in (('cold', False), ('hot', True)):
                    code = (full_code+'; ' if kind == 'full' else '') + observation(expected)
                    record(kind+'_'+label+'_compare', [sys.executable, '-B', '-c', code])
                if kind == 'pure':
                    record('pure_hot_read_only', [sys.executable, '-B', '-c', observation(True, True)])
                print('OFFLINE_BENCHMARK_PROGRESS', kind, i+1, count, flush=True)
      complete = True
    finally:
      successful = {name:[r for r in rows if r['returncode'] == 0 and not r.get('measurement_error')]
                    for name, rows in samples.items()}
      report = dict(scope='GitHub Linux offline, identical checked-out inputs; no production speedup claim',
                  complete=complete,
                  source=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
                  python=sys.version, samples=samples, summary={name:summarize(rows) if rows else None for name,rows in successful.items()},
                  notes=['Full samples include original complete discovery each time; comparison adds work.',
                         'Candidate has seven tests; both cold and hot comparison run all seven.',
                         'No CPU/disk page-cache flushing. Cold means no receipt; hot means valid receipt.',
                         'Peak RSS is /usr/bin/time maximum resident set for command, not simultaneous tree total.',
                         'P95/P99 with n=3 or 30 mostly reflect maxima and are not production tail estimates.'])
      Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
      print(json.dumps(report['summary'], indent=2), flush=True)


if __name__ == '__main__': main()
