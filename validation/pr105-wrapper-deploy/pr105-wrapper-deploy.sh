#!/usr/bin/env bash
# PR105: REVIEWABLE WRAPPER-ONLY DEPLOYMENT. Linux/GNU tools, existing root shell.
# This file was prepared offline; it has NOT been executed on the server.
# No SSH, credentials, service start/restart, timer change, Git checkout/reset,
# strategy invocation, portfolio-lock deletion, or ledger write occurs here.
#
# PHASE 1 -- READ ONLY (also the default):
#   bash pr105-wrapper-deploy.sh audit
# Review the unit entry, canonical target, OLD_SHA256 and OLD_META output.
# Actual installed-path/hash evidence is currently unavailable to the author.
# If any check refuses, STOP and review that evidence. Do not change constants
# or remove a guard merely to get past it. In particular, drop-ins require review.
#
# PHASE 2 -- only after the audit is reviewed, paste its two EXACT values:
#   bash pr105-wrapper-deploy.sh install 'PASTE_OLD_SHA256' 'PASTE_UID:GID:MODE'
# Placeholders are rejected before any write. No old hash is guessed/defaulted.
# Do not run another installer/manual replacement concurrently. This script
# serializes its own install/rollback instances using flock on /usr/local/bin.
# Keep the printed BACKUP_DIR. It contains old-wrapper and evidence.txt.
#
# ROLLBACK -- wrapper only; requires current target still equals this PR version:
#   bash pr105-wrapper-deploy.sh rollback 'PASTE_BACKUP_DIR' 'PASTE_OLD_SHA256' 'PASTE_UID:GID:MODE'
# Unknown current content is never overwritten. Existing ledgers remain untouched.
# A running process may finish using the old inode; neither install nor rollback
# restarts it or reverses transactions. Timer/strategy tasks are never triggered.
#
# Failure before atomic rename leaves the installed wrapper unchanged. All staged
# evidence/backups are retained on failure. If verification AFTER rename fails,
# stop and inspect the target + retained backup: do not blindly overwrite an
# unexpected current hash. Rollback performs the same hash/metadata checks.
# Existing owner, group, mode and original backup bytes are preserved. No backup
# or staging directory is automatically removed (including on interruption).
#
# AFTER INSTALL: compare reported SHA256/metadata, then use read-only systemctl
# show/journalctl and the PR105 acceptance checklist for subsequent scheduled
# Research/Monitor cycles, generation, SHADOW flags and paired main readback.
# Installation is NOT proof of runtime acceptance. No timer restart is required.

set -Eeuo pipefail
umask 077
readonly UNIT=hunter-position-monitor.service
readonly SOURCE=/opt/shadow-runner/btc-grid-state
readonly TARGET=/usr/local/bin/hunter-monitor-runner.sh
readonly PARENT=/usr/local/bin
readonly COMMIT=0fce78b355ced25cd6cebe7fb08b9ac72d095860
readonly NEW_SHA=fb44444bea78386bd283e8d062c3b22c814b5dc336573813a6cd77033c9bbb49
readonly URL="https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/$COMMIT/scripts/hunter_monitor_runner.sh"
WORK=
SWITCHED=not_attempted
die() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }
on_exit() {
  local rc=$?
  if (( rc != 0 )); then
    printf 'STOPPED rc=%s atomic_replace_state=%s\n' "$rc" "$SWITCHED" >&2
    [[ -z "$WORK" ]] || printf 'RETAINED_EVIDENCE_DIR=%s\n' "$WORK" >&2
  fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
hash() { sha256sum -- "$1" | awk '{print $1}'; }
meta() { stat -c '%u:%g:%a' -- "$1"; }
identity() { stat -c '%d:%i:%u:%g:%a:%h' -- "$1"; }
need() { command -v "$1" >/dev/null || die "MISSING_TOOL $1"; }
sha_arg() { [[ "$1" =~ ^[0-9a-f]{64}$ ]] || die INVALID_SHA256_ARGUMENT; }
meta_arg() { [[ "$1" =~ ^[0-9]+:[0-9]+:[0-7]{3,4}$ ]] || die INVALID_METADATA_ARGUMENT; }
regular() { [[ -f "$1" && ! -L "$1" ]] || die "NOT_REGULAR_NON_SYMLINK $1"; }

check_layout() {
  local loaded working entry dropins fragment
  loaded=$(systemctl show "$UNIT" -p LoadState --value)
  working=$(systemctl show "$UNIT" -p WorkingDirectory --value)
  entry=$(systemctl show "$UNIT" -p ExecStart --value)
  dropins=$(systemctl show "$UNIT" -p DropInPaths --value)
  fragment=$(systemctl show "$UNIT" -p FragmentPath --value)
  printf 'UNIT=%s\nLOAD_STATE=%s\nWORKING_DIRECTORY=%s\nUNIT_FILE=%s\n' "$UNIT" "$loaded" "$working" "$fragment"
  if [[ "$entry" =~ ^\{[[:space:]]path=([^\;[:space:]]+) ]]; then
    printf 'OBSERVED_EXECSTART_PATH=%s\n' "${BASH_REMATCH[1]}"
  fi
  # Do not print raw argv or environment: unexpected entries could contain secrets.
  [[ "$loaded" == loaded && "$working" == "$SOURCE" ]] || die UNEXPECTED_LOADED_UNIT_OR_WORKDIR
  [[ "$entry" == "{ path=$TARGET ; argv[]=$TARGET ; "* ]] || die UNEXPECTED_EXECSTART_REVIEW_REQUIRED
  [[ "${entry#*path=}" != *'path='* ]] || die MULTIPLE_EXECSTART_ENTRIES
  [[ -z "$dropins" ]] || die UNIT_DROPINS_REQUIRE_REVIEW
  printf 'EXECSTART_EXACT_PATH_NO_ARGUMENTS=%s\n' "$TARGET"
  [[ -d "$SOURCE" && "$(readlink -e -- "$SOURCE")" == "$SOURCE" ]] || die UNEXPECTED_SOURCE_PATH
  [[ -d "$PARENT" && "$(readlink -e -- "$PARENT")" == "$PARENT" ]] || die UNEXPECTED_PARENT_PATH
  regular "$TARGET"
  [[ "$(readlink -e -- "$TARGET")" == "$TARGET" ]] || die UNEXPECTED_TARGET_PATH
  [[ "$(stat -c '%h' -- "$TARGET")" == 1 ]] || die HARDLINKED_TARGET_REQUIRES_REVIEW
  [[ -x "$TARGET" ]] || die TARGET_NOT_EXECUTABLE
}

check_target() {
  regular "$TARGET"
  [[ "$(readlink -e -- "$TARGET")" == "$TARGET" ]] || die TARGET_PATH_CHANGED
  [[ "$(hash "$TARGET")" == "$1" ]] || die TARGET_HASH_CHANGED
  [[ "$(meta "$TARGET")" == "$2" ]] || die TARGET_OWNER_OR_MODE_CHANGED
}

verify_copy() {
  regular "$1"
  [[ "$(hash "$1")" == "$2" ]] || die COPY_HASH_MISMATCH
  [[ "$(meta "$1")" == "$3" ]] || die COPY_METADATA_MISMATCH
}

atomic_replace() {
  local staged=$1 desired_sha=$2 desired_meta=$3 old_sha=$4 old_meta=$5 old_identity=$6
  verify_copy "$staged" "$desired_sha" "$desired_meta"
  bash -n "$staged"                         # Syntax only, NEVER execute candidate.
  check_layout
  check_target "$old_sha" "$old_meta"
  [[ "$(identity "$TARGET")" == "$old_identity" ]] || die TARGET_INODE_CHANGED
  [[ "$(stat -c '%d' -- "$staged")" == "$(stat -c '%d' -- "$TARGET")" ]] || die CROSS_FILESYSTEM_RENAME_REFUSED
  # Flush staged bytes and durable backup before the only installed-file mutation.
  python3 - "$staged" "$WORK" "$PARENT" <<'PY'
import os, pathlib, sys
for p in (pathlib.Path(sys.argv[1]), *pathlib.Path(sys.argv[2]).glob('*')):
    if p.is_file() and not p.is_symlink():
        with p.open('rb') as f: os.fsync(f.fileno())
for directory in sys.argv[2:4]:
    fd=os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
PY
  # Recheck after flush. Separate installers must honor the directory flock.
  check_target "$old_sha" "$old_meta"
  [[ "$(identity "$TARGET")" == "$old_identity" ]] || die TARGET_INODE_CHANGED
  SWITCHED=attempting
  mv -fT -- "$staged" "$TARGET"            # Same-filesystem atomic rename.
  SWITCHED=completed
  verify_copy "$TARGET" "$desired_sha" "$desired_meta"
  python3 - "$PARENT" <<'PY'
import os,sys
fd=os.open(sys.argv[1],os.O_RDONLY | os.O_DIRECTORY)
try: os.fsync(fd)
finally: os.close(fd)
PY
  printf 'VERIFIED_TARGET=%s\nSHA256=%s\nOWNER_GROUP_MODE=%s\n' "$TARGET" "$desired_sha" "$desired_meta"
}

ACTION=${1:-audit}
case "$ACTION" in audit|install|rollback) ;; *) die 'USAGE: audit | install OLD_SHA OLD_META | rollback BACKUP_DIR OLD_SHA OLD_META';; esac
for command in systemctl readlink stat sha256sum awk; do need "$command"; done

if [[ "$ACTION" == audit ]]; then
  (( $# <= 1 )) || die AUDIT_TAKES_NO_ARGUMENTS
  # This branch creates no files, locks no files and invokes no network requests.
  check_layout
  printf 'CANONICAL_TARGET=%s\nOLD_SHA256=%s\nOLD_META=%s\nOLD_DEVICE_INODE_UID_GID_MODE_LINKS=%s\n' \
    "$(readlink -e -- "$TARGET")" "$(hash "$TARGET")" "$(meta "$TARGET")" "$(identity "$TARGET")"
  systemctl show "$UNIT" hunter-research.service -p Id -p ActiveState -p SubState -p Result -p MainPID -p TimeoutStartUSec
  printf 'PINNED_MERGE=%s\nEXPECTED_NEW_SHA256=%s\nAUDIT_ONLY_NO_INSTALL_PERFORMED\n' "$COMMIT" "$NEW_SHA"
  exit 0
fi

# Validate all caller-supplied evidence BEFORE a lock/temp/backup can be created.
if [[ "$ACTION" == install ]]; then
  (( $# == 3 )) || die INSTALL_REQUIRES_AUDITED_OLD_SHA_AND_META
  OLD_SHA=$2; OLD_META=$3
else
  (( $# == 4 )) || die ROLLBACK_REQUIRES_BACKUP_DIR_OLD_SHA_AND_META
  BACKUP_DIR=$2; OLD_SHA=$3; OLD_META=$4
fi
sha_arg "$OLD_SHA"; meta_arg "$OLD_META"
[[ "$OLD_SHA" != "$NEW_SHA" ]] || die OLD_VERSION_ALREADY_EQUALS_PR105
(( EUID == 0 )) || die EXISTING_ROOT_SHELL_REQUIRED
for command in flock mktemp cp chown chmod cmp mv bash python3; do need "$command"; done
check_layout
# Read-only directory FD: serializes THIS installer/rollback without creating or
# touching the trading lock or a persistent lock file. Kernel closes it on exit.
exec 8<"$PARENT"
flock -n 8 || die ANOTHER_WRAPPER_INSTALL_OR_ROLLBACK_IS_ACTIVE
check_layout

if [[ "$ACTION" == install ]]; then
  need curl
  check_target "$OLD_SHA" "$OLD_META"
  OLD_ID=$(identity "$TARGET")
  WORK=$(mktemp -d "$PARENT/.hunter-monitor-pr105.XXXXXXXX")
  printf 'BACKUP_DIR=%s\n' "$WORK"
  cp --preserve=all -- "$TARGET" "$WORK/old-wrapper"
  verify_copy "$WORK/old-wrapper" "$OLD_SHA" "$OLD_META"
  cmp -s -- "$TARGET" "$WORK/old-wrapper" || die BACKUP_BYTES_MISMATCH
  printf 'merge=%s\nold_sha256=%s\nold_meta=%s\nold_identity=%s\nnew_sha256=%s\n' \
    "$COMMIT" "$OLD_SHA" "$OLD_META" "$OLD_ID" "$NEW_SHA" > "$WORK/evidence.txt"
  # Fixed public GitHub URL, TLS verification on; ignore curlrc, no auth or redirect.
  curl --disable --proto '=https' --tlsv1.2 --fail --silent --show-error \
    --connect-timeout 10 --max-time 60 --max-filesize 65536 \
    --output "$WORK/downloaded-wrapper" "$URL"
  [[ "$(hash "$WORK/downloaded-wrapper")" == "$NEW_SHA" ]] || die DOWNLOADED_HASH_MISMATCH
  bash -n "$WORK/downloaded-wrapper"
  cp --preserve=all -- "$WORK/old-wrapper" "$WORK/staged-wrapper"
  cat -- "$WORK/downloaded-wrapper" > "$WORK/staged-wrapper"
  chown --reference="$WORK/old-wrapper" -- "$WORK/staged-wrapper"
  chmod --reference="$WORK/old-wrapper" -- "$WORK/staged-wrapper"
  atomic_replace "$WORK/staged-wrapper" "$NEW_SHA" "$OLD_META" "$OLD_SHA" "$OLD_META" "$OLD_ID"
  printf 'INSTALL_COMPLETE_BACKUP_RETAINED=%s\nNO_SERVICE_OR_TIMER_ACTION_PERFORMED\n' "$WORK"
else
  # The backup path must be exactly an original root-owned private staging dir.
  [[ "$BACKUP_DIR" =~ ^/usr/local/bin/\.hunter-monitor-pr105\.[A-Za-z0-9]{8}$ ]] || die INVALID_BACKUP_DIRECTORY
  [[ -d "$BACKUP_DIR" && ! -L "$BACKUP_DIR" && "$(readlink -e -- "$BACKUP_DIR")" == "$BACKUP_DIR" ]] || die UNSAFE_BACKUP_PATH
  [[ "$(stat -c '%u:%a' -- "$BACKUP_DIR")" == 0:700 ]] || die UNSAFE_BACKUP_DIRECTORY_METADATA
  verify_copy "$BACKUP_DIR/old-wrapper" "$OLD_SHA" "$OLD_META"
  check_target "$NEW_SHA" "$OLD_META"       # Refuse any other installed version.
  CURRENT_ID=$(identity "$TARGET")
  WORK=$(mktemp -d "$PARENT/.hunter-monitor-pr105-rollback.XXXXXXXX")
  printf 'ROLLBACK_EVIDENCE_DIR=%s\n' "$WORK"
  cp --preserve=all -- "$TARGET" "$WORK/pr105-wrapper-before-rollback"
  verify_copy "$WORK/pr105-wrapper-before-rollback" "$NEW_SHA" "$OLD_META"
  cp --preserve=all -- "$BACKUP_DIR/old-wrapper" "$WORK/staged-wrapper"
  atomic_replace "$WORK/staged-wrapper" "$OLD_SHA" "$OLD_META" "$NEW_SHA" "$OLD_META" "$CURRENT_ID"
  printf 'WRAPPER_ROLLBACK_COMPLETE_BACKUPS_RETAINED\nNO_LEDGER_OR_SERVICE_OR_TIMER_ACTION_PERFORMED\n'
fi
