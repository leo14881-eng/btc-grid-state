#!/usr/bin/env bash
# Stage only in already-provisioned paths. No sudo/useradd/groupadd/chmod/chown,
# dependency installation, source switch, state initialization, or service start.
set -euo pipefail
REPO=${1:?usage: hunter_bybit_stage.sh SOURCE_REPO EXACT_REVIEWED_SHA}
HEAD_SHA=${2:?EXACT_REVIEWED_SHA_REQUIRED}
[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo INVALID_RELEASE_SHA; exit 2; }
test "$(git -C "$REPO" rev-parse "$HEAD_SHA^{commit}")" = "$HEAD_SHA"
test "$(id -u)" -eq 0 || { echo 'BLOCKED: approved maintenance identity required'; exit 1; }
bash "$(dirname "$0")/hunter_bybit_preflight.sh"
PREFIX=/opt/hunter-bybit-stream
STATE=/var/lib/hunter-bybit-stream
UNIT=/etc/systemd/system/hunter-bybit-stream.service
# Refuse overwrite/upgrade and writable code roots; no implicit permission repair.
for path in "$PREFIX" "$PREFIX/venv"; do
  test "$(stat -c %u "$path")" = 0 || { echo "BLOCKED: root ownership required $path"; exit 1; }
  mode=$(stat -c %a "$path")
  (( (8#$mode & 0022) == 0 )) || { echo "BLOCKED: writable code root $path"; exit 1; }
done
test ! -e "$PREFIX/code" && test ! -L "$PREFIX/code"
test ! -e "$PREFIX/release-head" && test ! -L "$PREFIX/release-head"
test ! -e "$UNIT" && test ! -L "$UNIT"
test "$(stat -c %U "$STATE")" = hunter-bybit-stream
test "$(stat -c %G "$STATE")" = hunter-bybit-data
test "$(stat -c %a "$STATE")" = 2770
# Only existing paths/identities are used; runuser does not add memberships.
runuser -u hunter-bybit-stream -- test -r "$STATE"
runuser -u hunter-bybit-stream -- test -w "$STATE"
runuser -u hunter-bybit-stream -- test -x "$STATE"
umask 0022
mkdir "$PREFIX/code"
git -C "$REPO" archive "$HEAD_SHA" | tar --no-same-owner -x -C "$PREFIX/code"
printf '%s\n' "$HEAD_SHA" >"$PREFIX/release-head"
cp "$PREFIX/code/deploy/systemd/hunter-bybit-stream.service" "$UNIT"
systemd-analyze verify "$UNIT"
echo "STAGED_NOT_STARTED $HEAD_SHA"
echo 'Review actual consumer access, then explicitly initialize/prepare and observe per deploy/hunter-bybit-kline-install.md.'
