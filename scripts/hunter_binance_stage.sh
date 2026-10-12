#!/usr/bin/env bash
# Stage only in already-provisioned paths. No sudo/useradd/groupadd/chmod/chown,
# dependency installation, source switch, state initialization, or service start.
set -euo pipefail
REPO=${1:?usage: hunter_binance_stage.sh SOURCE_REPO EXACT_REVIEWED_SHA}
HEAD_SHA=${2:?EXACT_REVIEWED_SHA_REQUIRED}
[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo INVALID_RELEASE_SHA; exit 2; }
test "$(git -C "$REPO" rev-parse "$HEAD_SHA^{commit}")" = "$HEAD_SHA"
test "$(id -u)" -eq 0 || { echo 'BLOCKED: approved maintenance identity required'; exit 1; }
bash "$(dirname "$0")/hunter_binance_preflight.sh"
PREFIX=/opt/hunter-binance-stream
STATE=/var/lib/hunter-binance-stream
UNIT=/etc/systemd/system/hunter-binance-stream.service
# Refuse overwrite/upgrade and writable code roots; no implicit permission repair.
for path in "$PREFIX" "$PREFIX/venv"; do
  test "$(stat -c %u "$path")" = 0 || { echo "BLOCKED: root ownership required $path"; exit 1; }
  mode=$(stat -c %a "$path")
  (( (8#$mode & 0022) == 0 )) || { echo "BLOCKED: writable code root $path"; exit 1; }
done
test ! -e "$PREFIX/code" && test ! -L "$PREFIX/code"
test ! -e "$PREFIX/release-head" && test ! -L "$PREFIX/release-head"
test ! -e "$UNIT" && test ! -L "$UNIT"
test "$(stat -c %U "$STATE")" = hunter-binance-stream
test "$(stat -c %G "$STATE")" = hunter-binance-data
test "$(stat -c %a "$STATE")" = 2770
# Only existing paths/identities are used; runuser does not add memberships.
runuser -u hunter-binance-stream -- test -r "$STATE"
runuser -u hunter-binance-stream -- test -w "$STATE"
runuser -u hunter-binance-stream -- test -x "$STATE"
umask 0022
mkdir "$PREFIX/code"
git -C "$REPO" archive "$HEAD_SHA" | tar --no-same-owner -x -C "$PREFIX/code"
printf '%s\n' "$HEAD_SHA" >"$PREFIX/release-head"
cp "$PREFIX/code/deploy/systemd/hunter-binance-stream.service" "$UNIT"
systemd-analyze verify "$UNIT"
echo "STAGED_NOT_STARTED $HEAD_SHA"
echo 'Review actual consumer access, then explicitly initialize/prepare and observe per deploy/hunter-binance-kline-install.md.'
