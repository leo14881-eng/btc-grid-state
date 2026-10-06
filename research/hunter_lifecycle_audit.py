"""Read-only chronological ledger replay; never creates fills or rewrites state."""
import argparse
import json
import subprocess


def replay(repo, ref, assets):
    path = 'research/results/hunter-shadow-v2-portfolio.json'
    def git(*args):
        return subprocess.check_output(['git', '-C', repo, *args], text=True)
    pinned = git('rev-parse', ref).strip()
    commits = git('log', '--reverse', '--format=%H', pinned, '--', path).splitlines()
    result = {}; versions = 0
    for sha in commits:
        try:
            state = json.loads(git('show', sha + ':' + path))
        except (subprocess.CalledProcessError, ValueError):
            continue
        versions += 1
        positions = state.get('open_positions', []) + state.get('closed_positions', []) + state.get('closed_trade_archive', [])
        for p in positions:
            if p.get('asset') not in assets:
                continue
            ident = p.get('shadow_id') or p['asset'] + str(p.get('opened_at_utc'))
            row = result.setdefault(ident, {'asset': p['asset'], 'shadow_id': ident, 'entry_at': p.get('opened_at_utc'), 'observations': [], 'execution_window': 'UNVERIFIABLE_HISTORICAL_EXECUTION_WINDOW', 'running_sampled_mfe_pct': 0})
            price = p.get('last_price'); tranches = p.get('tranches', [])
            at = p.get('last_marked_at_utc')
            if at and price and tranches and not any(o['at'] == at for o in row['observations']):
                n = sum(t['notional_usdt'] for t in tranches)
                entry = sum(t['price'] * t['notional_usdt'] for t in tranches) / n
                raw = (price / entry - 1) * 100
                # Each decision sees only this and earlier ledger observations.
                row['running_sampled_mfe_pct'] = max(row['running_sampled_mfe_pct'], raw)
                qty = sum(t['notional_usdt'] / (t['price'] * (1 + (t.get('buy_slippage_bps', 0) + 10) / 10000)) for t in tranches)
                net = qty * price * .999 - n
                breach = row['running_sampled_mfe_pct'] >= 2 and (raw <= .55 or row['running_sampled_mfe_pct'] - raw >= 2)
                o = {'at': at, 'commit': sha, 'generation_id': p.get('profit_protection_review', {}).get('generation_id') or state.get('last_cycle_generation_id'), 'reference_price': price, 'sampled_raw_pct': raw, 'running_sampled_mfe_pct': row['running_sampled_mfe_pct'], 'reference_net_pnl_usdt': net, 'net_provenance': 'INFERRED_REFERENCE_MODEL_10BPS_NOT_EXECUTABLE', 'legacy_breach': breach, 'health_state': p.get('health_state'), 'recovery_state': p.get('recovery_state'), 'live_armed_state': p.get('protection_lifecycle'), 'ledger_provenance': 'KNOWN'}
                row['observations'].append(o)
                for key, condition in [('first_sampled_arm', raw >= 2), ('first_sampled_positive_breach', breach and net > 0), ('first_sampled_nonpositive', net <= 0), ('first_loss_recovery', p.get('recovery_state') == 'LOSS_RECOVERY'), ('first_nonpositive_after_arm', 'first_sampled_arm' in row and net <= 0), ('first_breach_after_arm', breach)]:
                    if condition and key not in row:
                        row[key] = o
            if p.get('closed_at_utc'):
                row['actual_ledger_exit'] = {'at': p['closed_at_utc'], 'reason': p.get('exit_reason'), 'net_pnl_usdt': p.get('net_pnl_usdt'), 'provenance': 'KNOWN_LEDGER_NOT_EXCHANGE_FILL'}
    return {'schema': 'hunter_lifecycle_historical_audit_v1', 'pinned_main': pinned, 'portfolio_versions': versions, 'shadow_only': True, 'real_order_count': 0, 'historical_executable_window_count': 0, 'limitations': ['Ledger reference prices do not prove historical bid depth or exchange time.', 'Missing timer buckets and exact fillability require unavailable historical receipts/logs.', 'No future price participates in any past observation.', 'No simulated historical SELL, profit, release, or improvement claim.'], 'positions': list(result.values())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', default='.')
    parser.add_argument('--ref', default='origin/main')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with open(args.output, 'w') as out:
        json.dump(replay(args.repo, args.ref, {'ENA', 'PENDLE', 'HUMA', 'MOVR', 'PARTI', 'ETHFI'}), out, indent=2)
