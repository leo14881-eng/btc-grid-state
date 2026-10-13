#!/usr/bin/env bash
# Fetch the previously reviewed installer from an immutable public commit.
# Caller supplies audited old hash and uid:gid:mode; never embed private evidence here.
set -Eeuo pipefail
(( $# == 2 )) || { printf '%s\n' 'REQUIRES_AUDITED_OLD_SHA_AND_META' >&2; exit 2; }
OLD_SHA=$1
OLD_META=$2
[[ "$OLD_SHA" =~ ^[0-9a-f]{64}$ && "$OLD_META" =~ ^[0-9]+:[0-9]+:[0-7]{3,4}$ ]] || { printf '%s\n' 'INVALID_AUDIT_ARGUMENTS' >&2; exit 2; }
umask 077
INSTALL_DIR=$(mktemp -d /tmp/pr105-wrapper-install.XXXXXXXX)
INSTALLER="$INSTALL_DIR/pr105-wrapper-deploy.sh"
printf 'RETAINED_INSTALLER_DIR=%s\n' "$INSTALL_DIR"
curl --disable --proto '=https' --tlsv1.2 --fail --silent --show-error \
  --connect-timeout 10 --max-time 60 --max-filesize 65536 \
  --output "$INSTALLER" \
  'https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/657766942a5604e73d06561fdb9155bff45af664/validation/pr105-wrapper-deploy/pr105-wrapper-deploy.sh'
printf '%s  %s\n' '4f8ea4f32a8232f5f5a6a083d9ae85aed7b0bdc01e774aebb961e441c9f12e44' "$INSTALLER" | sha256sum -c -
bash -n "$INSTALLER"
bash "$INSTALLER" audit
printf 'ROLLBACK_INSTALLER=%s\n' "$INSTALLER"
bash "$INSTALLER" install "$OLD_SHA" "$OLD_META"
