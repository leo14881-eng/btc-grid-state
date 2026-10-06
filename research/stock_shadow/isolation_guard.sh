#!/usr/bin/env bash
set -euo pipefail
tracked=$(git diff --name-only HEAD)
untracked=$(git ls-files --others --exclude-standard --exclude='**/__pycache__/**' --exclude='.pytest_cache/**')
while IFS= read -r file; do
  case "$file" in
    research/results/stock-shadow/*|'') ;;
    *) echo "Isolation violation: $file"; exit 1 ;;
  esac
done < <(printf '%s\n%s\n' "$tracked" "$untracked" | sort -u)
