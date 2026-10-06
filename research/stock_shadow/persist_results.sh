#!/usr/bin/env bash
# Shared GitHub/server authority and atomic stock-only publication.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$script_dir/server/persist.py" "${1:?expected main, monitor, replay, or health}"
