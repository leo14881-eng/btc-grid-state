"""Guarded real-code journal footprint and 300-line/30KB tail reconstruction."""
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
sys.dont_write_bytecode = True
WIRE = json.loads(Path(sys.argv[1]).read_text())
NODE_VALIDATION = Path(sys.argv[2]).read_text().splitlines()
assert WIRE['fixture_only'] is True
from research import hunter_bybit_worker as worker
from research import hunter_http_evidence as evidence


def unpack(lines):
    records = []
    grouped = {}
    for line in lines:
        prefix, raw = line.split(' ', 1)
        if prefix.endswith('_GROUP'):
            group = json.loads(raw)
            key = (prefix, group['group_id'])
            by_part = grouped.setdefault(key, {})
            assert group['part'] not in by_part, 'DUPLICATE_PART'
            by_part[group['part']] = group
        elif prefix in ('HUNTER_BYBIT_REQUEST_OBSERVATION', 'HUNTER_BYBIT_HTTP_EVIDENCE'):
            records.append(json.loads(raw))
    for (_, digest), by_part in grouped.items():
        first = by_part[0]
        assert set(by_part) == set(range(first['parts'])), 'MISSING_PART'
        rows = []
        for part in range(first['parts']):
            chunk = by_part[part]
            assert chunk['parts'] == first['parts'] and chunk['record_total'] == first['record_total']
            rows.extend(chunk['records'])
        assert len(rows) == first['record_total']
        encoded = json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()
        assert hashlib.sha256(encoded).hexdigest() == digest, 'GROUP_DIGEST_MISMATCH'
        records.extend(rows)
    return records


def retrieve(journal):
    """Exact fixed-tail model: last N lines then first 30000 bytes; overlap one.

    No cursor is invented. The caller must finish before newer lines evict the
    required cycle from the last 300. Oversized unrelated lines are not silently
    discarded: a non-progressing page fails this acceptance.
    """
    assert len(journal) <= 300, 'CYCLE_OUTSIDE_READER_WINDOW'
    remaining = len(journal)
    recovered = []
    reads = 0
    while remaining:
        raw = ''.join(journal[-remaining:]).encode()[:30000]
        complete = raw[:raw.rfind(b'\n')+1].decode().splitlines(keepends=True)
        assert complete, 'NO_COMPLETE_LINE'
        reads += 1
        if recovered:
            assert recovered[-1] == complete[0], 'OVERLAP_LOST'
            recovered.extend(complete[1:])
        else:
            recovered.extend(complete)
        if len(complete) == remaining:
            break
        assert len(complete) > 1, 'TAIL_READER_CANNOT_ADVANCE'
        remaining -= len(complete)-1
    assert recovered == journal, 'INCOMPLETE_READBACK'
    return recovered, reads


def main():
    blocked = []
    with tempfile.TemporaryDirectory(prefix='hunter-log-volume-') as directory:
        scratch = Path(directory).resolve()
        tempfile.tempdir = str(scratch)
        def guard(event, args):
            if (event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.gethostbyname',
                          'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg',
                          'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork')
                    or event.startswith(('os.exec', 'os.spawn'))):
                blocked.append(event)
                raise RuntimeError('OFFLINE_EFFECT_BLOCKED')
            if event == 'open':
                path, mode, flags = args
                if not isinstance(path, int) and ((isinstance(mode, str) and any(c in mode for c in 'wax+'))
                        or isinstance(flags, int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
                    target = Path(os.fsdecode(path)).resolve()
                    if scratch not in target.parents and target != scratch:
                        blocked.append(event)
                        raise RuntimeError('OFFLINE_WRITE_BLOCKED')
        sys.addaudithook(guard)
        previous = Path.cwd()
        os.chdir(scratch)
        try:
            # Same Python discovery test selection as the deployed workflow.
            validation = io.StringIO()
            loader = unittest.TestLoader()
            suites = [loader.discover(str(ROOT/'tests'), pattern=pattern)
                      for pattern in ('test_hunter_cex_scan.py', 'test_hunter_bybit*.py')]
            with contextlib.redirect_stdout(validation), contextlib.redirect_stderr(validation):
                result = unittest.TextTestRunner(stream=validation, verbosity=2).run(unittest.TestSuite(suites))
            assert result.wasSuccessful() and not result.skipped and not blocked
            prelude = validation.getvalue().splitlines()+NODE_VALIDATION
            reports = []
            for mode in ('success', 'partial', 'all_failed'):
                wire = WIRE[mode]
                calls = []
                def opened(request, timeout):
                    sent = json.loads(request.data) if request.data else None
                    match = [row for row in wire['rows'] if row['path'] == request.selector and row['sent'] == sent]
                    assert len(match) == 1
                    calls.append((request.selector, sent))
                    response = io.BytesIO(json.dumps(match[0]['body']).encode())
                    response.status = 200
                    response.headers = match[0]['headers']
                    return response
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch.object(worker, 'DEFAULT', scratch/'snapshot.json'), \
                        patch.object(evidence.urllib.request, 'urlopen', side_effect=opened):
                    _, status = worker.collect({'BTC'}, dt.datetime.fromtimestamp(WIRE['end']/1000, dt.timezone.utc),
                        worker.request, lambda: (WIRE['bases']+['BTC'], {'cache_hit': True, 'network_requests': 0}))
                    evidence.log_generation_binding('20261005T180700000000Z', status)
                assert len(calls) == 9 and wire['upstream_calls'] == 285
                diagnostic = output.getvalue().splitlines()
                assert all(len((line+'\n').encode()) <= evidence.GROUP_LINE_BYTES for line in diagnostic)
                records = unpack(diagnostic)
                upstream = [row for row in records if row.get('layer') == 'bybit_upstream']
                failures = [row for row in records if row.get('schema') == 'hunter_http_log_v1']
                expected_failed = {'success': 0, 'partial': 80, 'all_failed': 284}[mode]
                assert len(upstream) == 285 and len(failures) == expected_failed
                assert sum(row['http_status'] == 403 for row in upstream) == expected_failed
                assert len({(row['worker_request_id'], row['upstream_job_index']) for row in upstream}) == 285
                assert status['signal_pairs'] == {'success': 141, 'partial': 101, 'all_failed': 0}[mode]
                assert status['signal_complete'] is (mode == 'success')
                # Measured validation output + explicit 80-line runner/publication
                # allowance, not a claim about arbitrary journal flooding.
                payload = prelude+diagnostic+[f'RUNNER_ALLOWANCE {index}' for index in range(80)]
                journal = [('J'*256)+line+'\n' for line in payload]
                reconstructed, reads = retrieve(journal)
                # Validation itself can contain synthetic diagnostic lines; only
                # the contiguous measured collector slice is the target cycle.
                recovered_slice = reconstructed[len(prelude):len(prelude)+len(diagnostic)]
                assert [line[256:].rstrip('\n') for line in recovered_slice] == diagnostic
                assert unpack([line[256:].rstrip('\n') for line in recovered_slice]) == records
                reports.append(dict(mode=mode,assets=141,upstream_requests=285,failed_windows=expected_failed,
                    diagnostic_lines=len(diagnostic),validation_lines=len(prelude),runner_allowance_lines=80,
                    total_lines=len(journal),max_diagnostic_line_bytes=max(len((line+'\n').encode()) for line in diagnostic),
                    max_journal_line_bytes=max(len(line.encode()) for line in journal),tail_reads=reads,
                    exact_readback=True,blocked_effects=len(blocked)))
            # Make the limit explicit: 301 lines cannot be certified by this model.
            try:
                retrieve(['x\n']*301)
                raise AssertionError('MISSING_WINDOW_LIMIT')
            except AssertionError as exc:
                assert str(exc) == 'CYCLE_OUTSIDE_READER_WINDOW'
            assert not blocked
            print('HUNTER_LOG_VOLUME_ACCEPTANCE '+json.dumps(reports,sort_keys=True))
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    main()
