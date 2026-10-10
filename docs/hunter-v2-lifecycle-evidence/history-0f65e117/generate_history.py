#!/usr/bin/env python3
"""Rebuild the pinned ENA/PENDLE published-history evidence from a full Git clone.

No working-tree file is read and no repository file/ref is written. Only the
explicit --out directory is written. This is versioned PP predicate/state replay,
not a full manager, liquidation, exchange-fill, scheduler or writer replay.
"""
import argparse
import ast
import collections
import copy
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import pathlib
import re
import subprocess
import sys

FIXED_REF = '0f65e11751484a6efbe9fe3e1defe2c310795433'
ENTRY_COMMIT = '48610e148c7a29193f54244e7a561c253732ddfe'
ENTRY_AT = '2026-10-05T07:51:47.857517+00:00'
TARGETS = {'ENA': 'SHV2-20261005T075147-ENA-b10cd5',
           'PENDLE': 'SHV2-20261005T075147-PENDLE-ea9b45'}
PATHS = {
    'portfolio': 'research/results/hunter-shadow-v2-portfolio.json',
    'monitor': 'research/results/hunter-position-monitor.json',
    'scan': 'research/results/hunter-cex-universe-run.json',
    'engine': 'research/hunter_shadow_trader_v2.py',
    'lifecycle': 'research/hunter_lifecycle_state.py',
    'rules': 'research/results/hunter-shadow-rules.json',
}
EXPECTED = {'published_snapshots': 1538, 'cycle_rows': 3076,
            'unique_decisions': 7008, 'unique_events': 2,
            'legacy_rows': 670, 'persistent_rows': 2406}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha256(canonical(value))


def when(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'Timestamp has no timezone: '+value)
    return result


def strict_json(data):
    def reject(value):
        raise ValueError('Nonfinite JSON constant: '+value)
    return json.loads(data, parse_constant=reject)


class Git:
    def __init__(self, repo):
        self.repo = pathlib.Path(repo).resolve(strict=True)
        self.env = dict(os.environ, GIT_NO_REPLACE_OBJECTS='1',
                        GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0')
        require(self.run('rev-parse', '--is-shallow-repository').strip() == b'false',
                'A full, non-shallow clone is required')
        require(self.run('rev-parse', '--show-object-format').strip() == b'sha1',
                'This pinned evidence requires a SHA-1 Git repository')
        self.proc = subprocess.Popen(self.command('cat-file', '--batch'),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self.env)
        self.small = {}
        self.trees = {}
        self.objects = set()

    def command(self, *args):
        return ['git', '--no-replace-objects', '-c', 'diff.renames=false',
                '-C', str(self.repo), *args]

    def run(self, *args):
        return subprocess.check_output(self.command(*args), env=self.env)

    def read(self, oid, expected_type=None):
        require(bool(re.fullmatch('[0-9a-f]{40}', oid)), 'Invalid object ID')
        self.objects.add(oid)
        if oid in self.small:
            kind, data = self.small[oid]
        else:
            self.proc.stdin.write((oid+'\n').encode('ascii'))
            self.proc.stdin.flush()
            parts = self.proc.stdout.readline().decode('ascii').split()
            require(len(parts) == 3 and parts[0] == oid,
                    'Missing or malformed Git object: '+oid)
            kind, size = parts[1], int(parts[2])
            data = self.proc.stdout.read(size)
            require(len(data) == size and self.proc.stdout.read(1) == b'\n',
                    'Truncated Git object: '+oid)
            actual = hashlib.sha1((kind+' '+str(size)).encode()+b'\0'+data).hexdigest()
            require(actual == oid, 'Git content hash mismatch: '+oid)
            if size < 150000:
                self.small[oid] = (kind, data)
        require(expected_type is None or kind == expected_type,
                'Wrong Git object type: '+oid)
        return data

    def tree(self, oid):
        if oid not in self.trees:
            raw = self.read(oid, 'tree'); offset = 0; entries = {}
            while offset < len(raw):
                space = raw.index(b' ', offset)
                end = raw.index(b'\0', space)
                name = raw[space+1:end].decode('utf-8')
                entries[name] = raw[end+1:end+21].hex()
                offset = end+21
            require(offset == len(raw), 'Malformed Git tree')
            self.trees[oid] = entries
        return self.trees[oid]

    def path(self, commit, path):
        raw = self.read(commit, 'commit')
        require(raw.startswith(b'tree '), 'Commit lacks tree')
        oid = raw.split(b'\n', 1)[0][5:].decode('ascii')
        for part in path.split('/'):
            oid = self.tree(oid).get(part)
            if oid is None:
                return None
        return oid

    def json_path(self, commit, name):
        oid = self.path(commit, PATHS[name])
        require(oid is not None, 'Missing source path: '+name)
        return strict_json(self.read(oid, 'blob')), oid

    def close(self):
        self.proc.stdin.close()
        require(self.proc.wait() == 0, 'git cat-file failed')


def functions(source, names, namespace, source_id):
    tree = ast.parse(source)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    require({n.name for n in nodes} == set(names), 'Missing pure function: '+source_id)
    # Only selected function declarations execute. No repository module imports,
    # initialization, manager, writer or exchange entry point is invoked.
    exec(compile(ast.Module(body=nodes, type_ignores=[]), source_id, 'exec'), namespace)
    return namespace, tree


def uses_persistent_protection(tree):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id == 'lifecycle' and n.func.attr == 'protect'
               for n in ast.walk(tree))


def write_json(path, value):
    path.write_bytes(canonical(value)+b'\n')


def write_gzip_rows(path, rows):
    raw = b''.join(canonical(row)+b'\n' for row in rows)
    with path.open('wb') as stream:
        with gzip.GzipFile(filename='', mode='wb', fileobj=stream,
                           mtime=0, compresslevel=9) as zipped:
            zipped.write(raw)
    return {'file': path.name, 'compressed_sha256': sha256(path.read_bytes()),
            'uncompressed_sha256': sha256(raw), 'uncompressed_bytes': len(raw),
            'rows': len(rows)}


def generate(repo, ref, out):
    require(ref == FIXED_REF, 'This audit is deliberately pinned to '+FIXED_REF)
    out = pathlib.Path(out).resolve()
    require(not out.exists() or not any(out.iterdir()), 'Output directory must be new or empty')
    out.mkdir(parents=True, exist_ok=True)
    g = Git(repo)
    try:
        require(g.run('rev-parse', '--verify', ref+'^{commit}').decode().strip() == ref,
                'Pinned commit did not resolve exactly')
        chain = g.run('rev-list', '--reverse', '--first-parent', ref).decode().splitlines()
        require(ENTRY_COMMIT in chain, 'Entry is absent from the pinned first-parent history')
        entry_index = chain.index(ENTRY_COMMIT)
        require(entry_index > 0, 'Missing history before entry')

        # Independent inventory walks every first-parent commit's actual tree,
        # including merges and non-monotonic commit timestamps. It does not use
        # the path-filtered history used to obtain metadata below.
        previous_blob = g.path(chain[entry_index-1], PATHS['portfolio'])
        inventory = []
        for commit in chain[entry_index:]:
            blob = g.path(commit, PATHS['portfolio'])
            require(blob is not None, 'Portfolio removed at '+commit)
            if blob != previous_blob:
                inventory.append({'commit': commit, 'portfolio_blob': blob})
                previous_blob = blob
        require(len(inventory) == EXPECTED['published_snapshots'],
                'Unexpected publication count: '+str(len(inventory)))
        print(json.dumps({'inventory_publications': len(inventory),
                          'first_parent_commits_since_entry': len(chain)-entry_index}), flush=True)
        raw = g.run('log', '--first-parent', '--diff-merges=first-parent', '--raw',
            '--no-renames', '--no-abbrev', '--format=COMMIT %H %cI %s',
            ref, '--', *PATHS.values()).decode('utf-8')
        metadata = {}; current = None
        for line in raw.splitlines():
            if line.startswith('COMMIT '):
                _, commit, at, subject = line.split(' ', 3)
                current = {'at': at, 'subject': subject, 'changed': {}}
                metadata[commit] = current
            elif line.startswith(':'):
                fields, path = line.split('\t', 1)
                current['changed'][path] = fields.split()[3]
        require(all(x['commit'] in metadata and PATHS['portfolio'] in metadata[x['commit']]['changed']
                    for x in inventory), 'Independent inventory and diff history disagree')

        rows = []; decisions = {}; events = {}; versions = {}; prior = {}
        immutable_tranches = {}; immutable_entry = {}; previous_events = None
        seen_marks = {a: set() for a in TARGETS}
        generations = {a: collections.Counter() for a in TARGETS}
        engine_cache = {}; life_cache = {}; source_bytes = 0
        phases = collections.Counter(); cycle_kinds = collections.Counter()
        target_ids = set(TARGETS.values()); latest_positions = {}
        for index, item in enumerate(inventory):
            commit = item['commit']; meta = metadata[commit]
            blobs = {k: g.path(commit, path) for k, path in PATHS.items()}
            require(blobs['portfolio'] == item['portfolio_blob'], 'Inventory blob mismatch')
            require(all(blobs[k] for k in ('portfolio','engine','rules','scan')), 'Required path absent')
            portfolio_bytes = g.read(blobs['portfolio'], 'blob'); source_bytes += len(portfolio_bytes)
            state = strict_json(portfolio_bytes)
            found = collections.defaultdict(list)
            for location in ('open_positions','closed_positions','closed_trade_archive'):
                for pos in state.get(location, []):
                    if pos.get('shadow_id') in target_ids:
                        found[pos['shadow_id']].append((location, pos))
            positions = {}
            for asset, target in TARGETS.items():
                require(len(found[target]) == 1,
                        'Target missing/duplicate across open, closed, archive: '+commit+' '+asset)
                location, pos = found[target][0]
                require(location == 'open_positions',
                        'Target closed: pinned all-open analysis requires explicit exit review '+commit+' '+asset)
                require(pos['asset'] == asset and pos['opened_at_utc'] == ENTRY_AT,
                        'Target identity/entry timestamp changed')
                positions[asset] = pos
                identity = {k: pos.get(k) for k in ('shadow_id','asset','opened_at_utc')}
                if asset not in immutable_tranches:
                    immutable_tranches[asset] = copy.deepcopy(pos['tranches'])
                    immutable_entry[asset] = identity
                require(pos['tranches'] == immutable_tranches[asset],
                        'Tranches changed; ADD/cashflow replay required '+commit+' '+asset)
                require(identity == immutable_entry[asset], 'Immutable entry changed')

            current_events = [e for e in state.get('events', []) if e.get('shadow_id') in target_ids]
            require(len(current_events) == 2, 'Unexpected target event count at '+commit)
            require(all(e.get('type') == 'SHADOW_V2_BUY' for e in current_events),
                    'Target SELL/ADD/other event requires explicit review '+commit)
            require(collections.Counter(e['shadow_id'] for e in current_events) ==
                    collections.Counter({target: 1 for target in target_ids}), 'Duplicate target event')
            require(previous_events is None or current_events == previous_events,
                    'Target event changed, disappeared or reordered '+commit)
            previous_events = copy.deepcopy(current_events)
            for event in current_events:
                events[digest(event)] = event
            for decision in state.get('decisions', []):
                if decision.get('shadow_id') in target_ids:
                    decisions.setdefault(digest(decision), dict(decision, first_observed_commit=commit))

            constants = strict_json(g.read(blobs['rules'], 'blob'))['common']
            key = (blobs['engine'], digest(constants))
            if key not in engine_cache:
                source = g.read(blobs['engine'], 'blob').decode('utf-8')
                ns, tree = functions(source,
                    {'finite','weighted_entry','total_notional','raw_return','net_pnl','profit_protection'},
                    dict(math=math, **constants), blobs['engine'])
                engine_cache[key] = (ns, uses_persistent_protection(tree))
            engine, persistent = engine_cache[key]
            phase = 'PERSISTED_EXIT_ESTIMATE_PP_STATE' if persistent else 'LEGACY_PP_REFERENCE_COST_PREDICATE'
            version_key = (blobs['engine'], blobs['lifecycle'], blobs['rules'])
            versions.setdefault(version_key, {'engine_blob': blobs['engine'],
                'lifecycle_blob': blobs['lifecycle'], 'rules_blob': blobs['rules'],
                'first_seen_commit': commit, 'phase': phase,
                'version_basis': 'PUBLISHED_MAIN_TREE_NOT_RUNNER_CHECKOUT_PROOF'})

            monitor_changed = PATHS['monitor'] in meta['changed']
            observed = strict_json(g.read(blobs['monitor'] if monitor_changed else blobs['scan'], 'blob'))
            generation = ((observed.get('evidence_refresh') or {}).get('generation_id')
                          if monitor_changed else state.get('last_cycle_generation_id'))
            generation = generation or state.get('last_cycle_generation_id')
            market_at = observed.get('as_of_utc')
            kind = 'MONITOR_PUBLICATION' if monitor_changed else 'RESEARCH_OR_OTHER_PUBLICATION'
            item.update(commit_at=meta['at'], target_locations={a:'open_positions' for a in TARGETS},
                        target_event_count=len(current_events), cycle_kind=kind)
            for asset, pos in positions.items():
                at = pos.get('last_marked_at_utc')
                require(at is not None, 'Missing position mark time')
                now = when(at); price = pos.get('last_price')
                require(isinstance(price, (float,int)) and math.isfinite(price) and price > 0,
                        'Invalid reference price')
                matching = [d for d in state.get('decisions', [])
                            if d.get('shadow_id') == pos['shadow_id'] and d.get('at') == at]
                require(bool(matching), 'No matching target decision at '+commit+' '+asset)
                require(all(d.get('price') == price for d in matching), 'Decision/reference price mismatch')
                require(at not in seen_marks[asset], 'Duplicate marked timestamp '+commit+' '+asset)
                if asset in prior:
                    require(now > when(prior[asset]['last_marked_at_utc']), 'Non-increasing mark timestamp')
                seen_marks[asset].add(at)
                repeated = bool(generation and generations[asset][generation])
                if generation:
                    generations[asset][generation] += 1
                net = engine['net_pnl'](pos, price)
                legacy = engine['profit_protection'](pos, price)
                row = {'asset':asset, 'shadow_id':pos['shadow_id'], 'commit':commit,
                    'commit_at':meta['at'], 'commit_subject':meta['subject'],
                    'source_blobs':blobs, 'portfolio_sha256':sha256(portfolio_bytes),
                    'cycle_kind':kind, 'at':at, 'generation':generation, 'market_at':market_at,
                    'phase':phase, 'price':price, 'mfe_pct':pos.get('mfe_pct'),
                    'mae_pct':pos.get('mae_pct'), 'health_state':pos.get('health_state'),
                    'tranches':pos['tranches'], 'reference_net_pnl_usdt':net,
                    'legacy_protection':legacy, 'actual_last_exit_estimate':pos.get('last_exit_estimate'),
                    'actual_protection_lifecycle':pos.get('protection_lifecycle'),
                    'decisions':matching, 'target_event_hashes':[digest(e) for e in current_events if e['asset']==asset],
                    'repeated_generation':repeated, 'duplicate_mark':False,
                    'parameters':{k:constants[k] for k in ('PROTECT_ARM_PCT','GIVEBACK_MAX_PCT','MIN_PROTECTED_NET_PCT','FEE_BPS')},
                    'version_basis':'PUBLISHED_MAIN_TREE_NOT_RUNNER_CHECKOUT_PROOF',
                    'full_manager_replayed':False, 'raw_book_liquidation_replayed':False}
                if asset in prior:
                    row['elapsed_since_previous_published_mark_seconds'] = (now-when(prior[asset]['last_marked_at_utc'])).total_seconds()
                if persistent:
                    require(blobs['lifecycle'] and asset in prior, 'Missing prior lifecycle input')
                    execution = pos.get('last_exit_estimate') or {}
                    life_state = pos.get('protection_lifecycle') or {}
                    require(execution.get('status') == 'SHADOW_RECEIPT_ESTIMATE', 'Missing executable estimate')
                    require(generation and generation == life_state.get('last_generation_id'),
                            'Generation source and persistent state mismatch')
                    require(life_state.get('last_observed_at_utc') == at, 'PP timestamp mismatch')
                    require(market_at and 0 <= (now-when(market_at)).total_seconds() <= 600,
                            'Stale/missing market input')
                    require(execution.get('fetched_at') and 0 <= (now-when(execution['fetched_at'])).total_seconds() <= 600,
                            'Stale/missing exit estimate')
                    require(not repeated, 'Repeated persistent generation needs separate review')
                    if blobs['lifecycle'] not in life_cache:
                        ns, _ = functions(g.read(blobs['lifecycle'],'blob').decode('utf-8'),
                                          {'fresh','protect'}, {'dt':dt,'math':math}, blobs['lifecycle'])
                        life_cache[blobs['lifecycle']] = ns
                    before = copy.deepcopy(prior[asset])
                    # Tranches were proved invariant above. MFE is a supplied
                    # recorded input, not an independent replay of mark updates.
                    before['mfe_pct'] = pos.get('mfe_pct',0)
                    result = life_cache[blobs['lifecycle']]['protect'](before,price,net,
                        copy.deepcopy(execution),now,generation,market_at,
                        constants['PROTECT_ARM_PCT'],constants['GIVEBACK_MAX_PCT'],constants['MIN_PROTECTED_NET_PCT'])
                    require(before.get('protection_lifecycle') == life_state,
                            'Persistent PP replay mismatch '+commit+' '+asset)
                    require(result.get('evidence_status') == 'FRESH', 'Unexpected PP evidence status')
                    row.update(replayed_protection_result=result, persisted_state_matches_replay=True,
                               replay_status='EXACT_PP_STATE_FROM_PERSISTED_ESTIMATE',
                               pp_exit_predicate_and_positive_net=bool(result['exit'] and execution['net_pnl_usdt']>0))
                else:
                    row.update(replay_status='EXACT_LEGACY_PP_REFERENCE_COST_ARITHMETIC',
                               pp_exit_predicate_and_positive_net=bool(legacy['exit'] and net>0))
                require(not row['pp_exit_predicate_and_positive_net'],
                        'Positive PP exit predicate observed; explicit manager review required')
                rows.append(row); phases[phase] += 1; cycle_kinds[kind] += 1
                prior[asset] = copy.deepcopy(pos); latest_positions[asset] = pos
            if (index+1) % 100 == 0:
                print(json.dumps({'published_snapshots_read':index+1,'cycle_rows':len(rows),
                                  'at':max(p['last_marked_at_utc'] for p in positions.values())}),flush=True)

        counts = {'published_snapshots':len(inventory), 'cycle_rows':len(rows),
                  'unique_decisions':len(decisions), 'unique_events':len(events),
                  'legacy_rows':phases['LEGACY_PP_REFERENCE_COST_PREDICATE'],
                  'persistent_rows':phases['PERSISTED_EXIT_ESTIMATE_PP_STATE']}
        require(counts == EXPECTED, 'Pinned audit counts changed: '+repr(counts))
        assets = {}
        for asset, pos in latest_positions.items():
            rs = [r for r in rows if r['asset']==asset]
            repeated_rows = [dict(at=r['at'],commit=r['commit'],generation=r['generation']) for r in rs if r['repeated_generation']]
            require(len(repeated_rows)==1 and all(r['commit']=='b6eafbe322e992acab22fc799d26639484783746' for r in repeated_rows),
                    'Unexpected repeated-generation history')
            gaps = [dict(before=a['at'],after=z['at'],seconds=(when(z['at'])-when(a['at'])).total_seconds(),
                         before_commit=a['commit'],after_commit=z['commit'])
                    for a,z in zip(rs,rs[1:]) if (when(z['at'])-when(a['at'])).total_seconds()>600]
            first = next(r for r in rs if r['mfe_pct']>=2)
            assets[asset] = {'published_rows':len(rs), 'monitor_rows':sum(r['cycle_kind']=='MONITOR_PUBLICATION' for r in rs),
                'first_observed_mfe_ge_2':{k:first[k] for k in ('at','price','mfe_pct','commit','phase')},
                'repeated_generation_rows':repeated_rows, 'gaps_over_600_seconds':gaps,
                'tranche_unique_versions':1, 'target_location':'open_positions',
                'latest_at':rs[-1]['at'], 'health_state':pos.get('health_state'),
                'degraded_cycles':pos.get('degraded_cycles'), 'recovery_state':pos.get('recovery_state'),
                'latest_protection_lifecycle':pos.get('protection_lifecycle'),
                'persistent_state_counts':dict(collections.Counter((r.get('actual_protection_lifecycle') or {}).get('state','ABSENT') for r in rs)),
                'last_decisions':[{'action':d['action'],'reasons':d['reasons']} for d in rs[-1]['decisions']]}
        summary = {'schema':'hunter_pinned_pp_history_v2','status':'PASS_WITH_DECLARED_HISTORICAL_LIMITATIONS',
            'baseline_main_sha':ref,'entry_commit':ENTRY_COMMIT,'counts':counts,
            'coverage_start_utc':min(r['at'] for r in rows),'coverage_end_utc':max(r['at'] for r in rows),
            'source_versions':list(versions.values()),'source_portfolio_bytes_read':source_bytes,
            'assets':assets,'target_events':list(events.values()),
            'proof_scope':{'all_first_parent_portfolio_publications_inventoried':True,
                'all_targets_present_once_in_open_closed_or_archive':True,'all_targets_remained_open':True,
                'tranches_invariant':True,'target_events_exactly_preserved':True,
                'published_tree_code_versioned':True,'runner_checkout_version_verified':False,
                'full_manager_replayed':False,'raw_book_liquidation_replayed':False,
                'exchange_fills_verified':False,'scheduler_or_writer_execution_verified':False},
            'limitations':[
                'Published main snapshots do not prove uncommitted/rejected writer execution or what happened between sampled marks.',
                'Code is attributed to each publication tree, not proven to be the concurrent runner checkout.',
                'Legacy replay covers PP and reference-cost arithmetic; its old raw full-quantity books were not retained.',
                'Persistent replay consumes recorded last_exit_estimate; it does not reconstruct depth liquidation or full manager branches.',
                'MFE is a recorded input; mark-update logic is not independently replayed. Tranches are independently proved invariant.',
                'All target persistent states are UNARMED. ARMED, peak and SELL branch verification requires the separate actual PROM fixture.',
                'The 600-second gap count is a sampling diagnostic, not a calendar/scheduler overdue classification.',
                'Only ENA/PENDLE target events are checked; this is not a whole-system ledger audit.'],
            'production_files_modified':False,'historical_fills_fabricated':False}
        artifacts = []
        artifacts.append(write_gzip_rows(out/'cycles.jsonl.gz',rows))
        artifacts.append(write_gzip_rows(out/'decisions.jsonl.gz',list(decisions.values())))
        artifacts.append(write_gzip_rows(out/'publications.jsonl.gz',inventory))
        write_json(out/'summary.json',summary)
        required_objects = sorted(g.objects)
        artifacts.append(write_gzip_rows(out/'source_objects.jsonl.gz',[{'oid':oid} for oid in required_objects]))
        source_root = pathlib.Path(__file__).resolve().parent
        for name in ('generate_history.py','verify_bundle.py'):
            data = (source_root/name).read_bytes()
            (out/name).write_bytes(data)
        manifest = {'schema':'hunter_pp_history_bundle_v2','baseline_main_sha':ref,'entry_commit':ENTRY_COMMIT,
            'counts':counts,'gzip':{'mtime':0,'filename':'','compresslevel':9},'artifacts':artifacts,
            'files':{name:{'sha256':sha256((out/name).read_bytes()),'bytes':(out/name).stat().st_size}
                     for name in ('summary.json','generate_history.py','verify_bundle.py')},
            'git_requirements':{'full_clone':True,'object_format':'sha1','replace_objects_disabled':True,
                                'lazy_fetch_disabled':True,'all_read_objects_content_hash_verified':True},
            'reproduce':['python generate_history.py --repo FULL_CLONE --ref '+ref+' --out NEW_EMPTY_DIRECTORY',
                         'python verify_bundle.py --bundle BUNDLE_DIRECTORY --repo FULL_CLONE'],
            'source_attribution':'Git blobs in cycles and publication inventory; published tree versions, not runner checkout proof'}
        write_json(out/'manifest.json',manifest)
        print(json.dumps({'status':summary['status'],'counts':counts,'out':str(out),
                          'manifest_sha256':sha256((out/'manifest.json').read_bytes())}),flush=True)
    finally:
        g.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',required=True,help='Full Git clone; working-tree state is ignored')
    parser.add_argument('--ref',default=FIXED_REF,help='Exactly the pinned authoritative main SHA')
    parser.add_argument('--out',required=True,help='New or empty evidence bundle directory')
    args=parser.parse_args()
    generate(args.repo,args.ref,args.out)


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print('AUDIT_FAILED_CLOSED: '+str(exc),file=sys.stderr)
        sys.exit(1)
