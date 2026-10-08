#!/usr/bin/env bash
# Stock-only direct-server entry point. Installation/activation is an operator step.
set -euo pipefail
umask 077

usage() {
  echo "Usage: $0 {main|monitor|replay} {--preview|--publish}" >&2
  exit 2
}
[[ $# -eq 2 ]] || usage
job=$1
mode=$2
case "$job" in main|monitor|replay) ;; *) usage ;; esac
case "$mode" in --preview|--publish) ;; *) usage ;; esac

install_root=${STOCK_SHADOW_INSTALL_ROOT:-/opt/stock-shadow}
state_root=${STOCK_SHADOW_STATE_ROOT:-/var/lib/stock-shadow}
python=${STOCK_SHADOW_PYTHON:-$install_root/venv/bin/python3}
for path in "$install_root" "$state_root" "$python"; do
  [[ "$path" == /* ]] || { echo "Stock paths must be absolute" >&2; exit 2; }
done
[[ -x "$python" ]] || { echo "Stock Python is not executable" >&2; exit 2; }
source_mirror=$install_root/source
work_root=$state_root/work
runtime_root=$state_root/runtime
preview_root=$runtime_root/preview-checkouts
mkdir -p "$work_root" "$runtime_root/launches" "$preview_root"
launch_dir=$(mktemp -d "$runtime_root/launches/$(date -u +%Y%m%dT%H%M%SZ)-$job.XXXXXX")
launch_id=${launch_dir##*/}
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
run_dir=
checkout=
source_commit=
status=starting

write_receipt() {
  local rc=$1
  {
    printf 'launch_id=%s\njob=%s\nmode=%s\n' "$launch_id" "$job" "$mode"
    printf 'status=%s\nexit_code=%s\n' "$status" "$rc"
    printf 'started_at=%s\nfinished_at=%s\n' "$started_at" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'source_commit=%s\ncheckout=%s\n' "$source_commit" "$checkout"
    printf 'checkout_retained=%s\n' "$([[ "$mode" == --preview && -n "$checkout" ]] && echo true || echo false)"
  } > "$launch_dir/launcher-receipt.txt.tmp"
  mv "$launch_dir/launcher-receipt.txt.tmp" "$launch_dir/launcher-receipt.txt"
}
finish() {
  local rc=$?
  trap - EXIT
  if [[ "$status" != skipped_lock_busy && "$status" != lock_wait_timeout ]]; then
    if [[ $rc -eq 0 ]]; then status=completed; else status=failed; fi
  fi
  write_receipt "$rc"
  # Only remove the private directory allocated by this invocation. Preview
  # checkouts stay inspectable and have a separate, disabled push destination.
  if [[ "$mode" == --publish && -n "$run_dir" ]]; then
    rm -rf -- "$run_dir"
  fi
  echo "STOCK_SHADOW_LAUNCH status=$status receipt=$launch_dir/launcher-receipt.txt"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
exec > >(tee -a "$launch_dir/launcher.log") 2>&1

# Every stock job and both modes share one lock. Monitor skips busy ticks; main
# and replay wait a bounded interval so a monitor cannot silently drop a scan.
# The unchanged engines use actual execution time, never a backdated tick time.
# This never locks Hunter jobs.
exec 9>"$state_root/writer.lock"
case "$job" in
  monitor)
    if ! flock -n 9; then
      status=skipped_lock_busy
      echo "Stock writer lock is busy; skipped monitor tick"
      exit 0
    fi
    ;;
  main|replay)
    if [[ "$job" == main ]]; then lock_wait=600; else lock_wait=1200; fi
    if ! flock -w "$lock_wait" 9; then
      status=lock_wait_timeout
      echo "Stock writer lock unavailable after ${lock_wait}s; $job not executed"
      exit 75
    fi
    ;;
esac

[[ "$(git --git-dir="$source_mirror" rev-parse --is-bare-repository)" == true ]] || {
  echo "Expected a dedicated bare stock source mirror" >&2
  exit 2
}
remote=$(git --git-dir="$source_mirror" remote get-url origin)
# Never persist credential-bearing HTTPS URLs in checkout configuration/logs.
if [[ "$remote" =~ ^https?://[^/]*@ ]]; then
  echo "Use the approved credential helper/SSH route, not credentials in a URL" >&2
  exit 2
fi
# Auto maintenance forks repack into this service's 50% CPU budget and can keep
# the writer lock alive after a failed preflight. Explicit maintenance remains
# available outside this latency-sensitive job; no global Git setting changes.
git -c gc.auto=0 -c maintenance.auto=false --git-dir="$source_mirror" fetch --quiet --no-tags origin refs/heads/main:refs/heads/main
if [[ "$mode" == --preview ]]; then
  run_dir=$(mktemp -d "$preview_root/$launch_id.XXXXXX")
else
  run_dir=$(mktemp -d "$work_root/$job.XXXXXX")
fi
checkout=$run_dir/repo
# An independent clone: no shared object store and no changes to another checkout.
git -c gc.auto=0 -c maintenance.auto=false clone --quiet --no-hardlinks --no-checkout "$source_mirror" "$checkout"
# Only this disposable checkout inherits the no-auto-maintenance policy.
git -C "$checkout" config gc.auto 0
git -C "$checkout" config maintenance.auto false
git -C "$checkout" remote set-url origin "$remote"
git -C "$checkout" checkout --quiet --detach refs/remotes/origin/main
source_commit=$(git -C "$checkout" rev-parse HEAD)
git -C "$checkout" config user.name stock-shadow-bot
git -C "$checkout" config user.email stock-shadow-bot@users.noreply.github.com
if [[ "$mode" == --preview ]]; then
  git -C "$checkout" config remote.origin.pushurl disabled://stock-shadow-preview
  mkdir -p "$checkout/.git/hooks"
  printf '#!/bin/sh\necho "Stock preview pushes are disabled" >&2\nexit 1\n' > "$checkout/.git/hooks/pre-push"
  chmod 0700 "$checkout/.git/hooks/pre-push"
  git -C "$checkout" config core.hooksPath "$checkout/.git/hooks"
fi
mkdir -p "$run_dir/tmp"
export TMPDIR=$run_dir/tmp
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export PATH="$(dirname "$python"):$PATH"
# An inherited development path/home must not substitute another checkout's code
# for the detached source selected above. Dependencies belong in the stock venv.
unset PYTHONPATH PYTHONHOME
cd "$checkout"
status=running
echo "STOCK_SHADOW_LAUNCH job=$job mode=$mode source=$source_commit launch=$launch_id"
# The runner binds writer, epoch, source, run and generation from this HEAD,
# enforces the owner fence again at persistence, and retains run receipts locally.
# No --force, direct workflow dispatch, strategy arguments, or takeover fallback.
"$python" -m research.stock_shadow.server.runner "$job" "$mode" --runtime-dir "$runtime_root"
