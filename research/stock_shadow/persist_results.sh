#!/usr/bin/env bash
# Rebase only this run's output files onto the latest main, preserving other writers.
set -euo pipefail
mode=${1:?expected main, monitor, or replay}
case "$mode" in
  main|monitor) output_scope=research/results/stock-shadow/ ;;
  replay) output_scope=research/results/stock-shadow/replay-v1.json ;;
  *) echo "Unknown persistence mode: $mode"; exit 2 ;;
esac
source_ref=$(git rev-parse HEAD)
mapfile -t changed < <(git diff --name-only "$source_ref" -- "$output_scope")
if [ "${#changed[@]}" -eq 0 ]; then exit 0; fi
result_dir=$(mktemp -d)
trap 'rm -rf "$result_dir"' EXIT
for file in "${changed[@]}"; do
  mkdir -p "$result_dir/$(dirname "$file")"
  cp "$file" "$result_dir/$file"
done
git config user.name "stock-shadow-bot"
git config user.email "stock-shadow-bot@users.noreply.github.com"
for attempt in 1 2 3; do
  git fetch origin main
  if ! git diff --quiet "$source_ref" origin/main -- research/stock_shadow research/stock_shadow_scheduler_health.py tests/test_stock_shadow.py tests/test_stock_shadow_scheduler_health.py .github/workflows/stock-shadow.yml .github/workflows/stock-shadow-position-monitor.yml .github/workflows/stock-shadow-replay.yml; then
    echo "Stock Shadow code changed during this run; refusing stale-result persistence"
    exit 42
  fi
  if [ "$mode" != replay ] && ! git diff --quiet "$source_ref" origin/main -- research/results/stock-shadow/portfolio-v1.json research/results/stock-shadow/trades-v1.json; then
    echo "Stock Shadow ledger changed during this run; refusing stale-result persistence"
    exit 43
  fi
  git reset --hard origin/main
  for file in "${changed[@]}"; do
    mkdir -p "$(dirname "$file")"
    cp "$result_dir/$file" "$file"
  done
  git add -- "${changed[@]}"
  if git diff --cached --quiet; then exit 0; fi
  git commit -m "data: update stock shadow $mode results"
  if git push origin HEAD:main; then
    echo "STOCK_SHADOW_PERSISTED source=$source_ref mode=$mode"
    exit 0
  fi
  sleep "$attempt"
done
echo "Failed to persist Stock Shadow results after retries"
exit 1
