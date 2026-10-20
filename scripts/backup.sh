#!/usr/bin/env bash
# Encrypted, timestamped PostgreSQL backup for the Compose deployment.
#
# Runs on the Docker host (systemd timer: deploy/systemd/healthos-backup.timer).
# The Compose `backup` profile streams `pg_dump --format=custom` to stdout and
# this script encrypts the stream before anything touches the disk.
#
# Encryption key (never in .env, never next to the backups):
#   1. BACKUP_AGE_RECIPIENT=age1...  -> age, public key only on the host (preferred)
#   2. BACKUP_KEY_FILE (default /etc/healthos/backup.key, mode 600):
#        an age identity (AGE-SECRET-KEY-...) -> age
#        any other content                    -> gpg --symmetric passphrase
#   Keep a copy of the key (or the age identity) off this host, e.g. in a password
#   manager: without it every backup is unreadable.
#
# Backups are kept forever. Nothing is ever deleted unless BACKUP_RETENTION_DAYS
# is explicitly set (environment, else .env) to a positive whole number of days;
# unset, empty, 0, "never" or anything else means never delete (a value set in
# the environment, even empty, overrides .env). Old backups are never deleted to
# make room.
#
# Free-space guard: the script refuses to start (exit 1, no file written) when
# the backup directory has less free space than BACKUP_MIN_FREE_MB, or by
# default twice the newest dump (at least 1 GiB), or 5 GiB before the first
# dump. BACKUP_MIN_FREE_MB=0 disables the guard.
#
# Every run writes <BACKUP_HOST_DIR>/last-backup-status.json (result, time,
# file, size, free space, message), on success and on failure.
#
# Other settings: BACKUP_HOST_DIR (default ./backups). All settings may come
# from the environment or the project's .env file. stdout carries only the path
# of the new backup; everything else goes to stderr.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

GIB=$((1024 * 1024 * 1024))
STATUS_NAME=last-backup-status.json

env_file_value() {
  # Reads one plain KEY=value from .env without sourcing it (it holds secrets).
  [ -f .env ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | tail -n 1 | sed "s/^[\"']//; s/[\"']$//"
}

setting() {
  local name="$1" value
  value="${!name:-}"
  [ -n "$value" ] || value="$(env_file_value "$name")"
  printf '%s' "$value"
}

backup_dir="$(setting BACKUP_HOST_DIR)"
backup_dir="${backup_dir:-./backups}"
key_file="${BACKUP_KEY_FILE:-/etc/healthos/backup.key}"
recipient="${BACKUP_AGE_RECIPIENT:-}"

output=""
partial=""
size_bytes=""
required_bytes=""
retention_label="never"
pruned_count=0
failure_reason=""
status_written=0

json_string() {
  printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037')"
}

json_number() {
  if [ -n "$1" ]; then printf '%s' "$1"; else printf 'null'; fi
}

free_bytes_of() {
  # POSIX df: 1024-byte blocks, "Available" is column 4 of the last line.
  local kib
  kib="$(df -Pk "$1" 2>/dev/null | awk 'END { print $4 }')" || return 1
  case "$kib" in '' | *[!0-9]*) return 1 ;; esac
  printf '%s' "$((kib * 1024))"
}

human() {
  awk -v b="$1" 'BEGIN { printf "%.2f GiB", b / 1073741824 }'
}

write_status() {
  # write_status RESULT MESSAGE; atomic replace, never fatal.
  local result="$1" message="$2" free tmp file_json
  mkdir -p "$backup_dir" 2>/dev/null || return 0
  free="$(free_bytes_of "$backup_dir" || true)"
  if [ -n "$output" ] && [ "$result" = success ]; then
    file_json="$(json_string "$output")"
  else
    file_json=null
  fi
  tmp="$backup_dir/.$STATUS_NAME.tmp"
  if ! {
    printf '{\n'
    printf '  "timestamp": %s,\n' "$(json_string "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
    printf '  "result": %s,\n' "$(json_string "$result")"
    printf '  "message": %s,\n' "$(json_string "$message")"
    printf '  "file": %s,\n' "$file_json"
    printf '  "size_bytes": %s,\n' "$(json_number "$size_bytes")"
    printf '  "free_bytes": %s,\n' "$(json_number "$free")"
    printf '  "required_free_bytes": %s,\n' "$(json_number "$required_bytes")"
    printf '  "retention_days": %s,\n' "$(json_string "$retention_label")"
    printf '  "pruned_files": %s\n' "$pruned_count"
    printf '}\n'
  } >"$tmp" 2>/dev/null || ! mv -f "$tmp" "$backup_dir/$STATUS_NAME" 2>/dev/null; then
    rm -f "$tmp" 2>/dev/null
    echo "backup: could not write $backup_dir/$STATUS_NAME" >&2
    return 0
  fi
  status_written=1
}

on_exit() {
  local rc=$?
  [ -z "$partial" ] || rm -f "$partial"
  if [ "$rc" -ne 0 ] && [ "$status_written" = 0 ]; then
    write_status failure "${failure_reason:-backup.sh exited with status $rc}"
  fi
}
trap on_exit EXIT
# Make a systemd stop/timeout or Ctrl-C run the EXIT handler too.
trap 'exit 143' TERM HUP
trap 'exit 130' INT

fail() {
  failure_reason="$*"
  echo "backup: $*" >&2
  exit 1
}

# Retention is opt-in: old dumps are deleted only when BACKUP_RETENTION_DAYS is a
# positive whole number of days. Set in the environment (even to an empty value), the
# environment wins and .env is not read; otherwise .env decides. Anything else (unset,
# empty, 0, never, negative, not a number) deletes nothing; an unrecognised value only
# warns, so a typo can neither delete backups nor stop them. 36500 (about 100 years)
# works and is effectively never.
if [ -n "${BACKUP_RETENTION_DAYS+set}" ]; then
  retention_days="$BACKUP_RETENTION_DAYS"
else
  retention_days="$(env_file_value BACKUP_RETENTION_DAYS)"
fi
case "$(printf '%s' "$retention_days" | tr '[:upper:]' '[:lower:]')" in
  '' | never | *[!0-9]*)
    case "$retention_days" in
      '' | [Nn][Ee][Vv][Ee][Rr]) ;;
      *) echo "backup: BACKUP_RETENTION_DAYS='$retention_days' is not a positive whole number of days; keeping every backup" >&2 ;;
    esac
    retention_days="" ;;
  *)
    retention_days="$(printf '%s' "$retention_days" | sed 's/^0*//')"
    # 0 keeps everything; more than 9 digits is beyond any lifetime (and bash arithmetic).
    [ "${#retention_days}" -le 9 ] || retention_days="" ;;
esac
[ -z "$retention_days" ] || retention_label="$retention_days"

min_free_mb="$(setting BACKUP_MIN_FREE_MB)"
case "$min_free_mb" in
  *[!0-9]*) fail "BACKUP_MIN_FREE_MB must be a whole number of MiB (0 disables the free-space guard)" ;;
esac

check_key_file() {
  [ -f "$key_file" ] || return 1
  [ -r "$key_file" ] || fail "$key_file is not readable by $(id -un)"
  # Group/other permission bits must all be unset (chmod 600 or 400).
  [ "$(ls -ln "$key_file" | cut -c5-10)" = "------" ] \
    || fail "$key_file must not be readable by group or others; run: chmod 600 $key_file"
  [ -s "$key_file" ] || fail "$key_file is empty"
}

if [ -n "$recipient" ]; then
  command -v age >/dev/null || fail "BACKUP_AGE_RECIPIENT is set but age is not installed"
  method=age
  encrypt=(age --encrypt --recipient "$recipient")
elif check_key_file; then
  if grep -q '^AGE-SECRET-KEY-' "$key_file"; then
    command -v age >/dev/null || fail "$key_file is an age identity but age is not installed"
    method=age
    encrypt=(age --encrypt --identity "$key_file")
  else
    command -v gpg >/dev/null || fail "gpg is required for a passphrase key file"
    method=gpg
    encrypt=(gpg --batch --quiet --yes --pinentry-mode loopback --symmetric
      --cipher-algo AES256 --passphrase-file "$key_file" --output -)
  fi
else
  fail "no backup key: set BACKUP_AGE_RECIPIENT (e.g. in /etc/healthos/backup.env) or create $key_file (mode 600); keys do not belong in .env"
fi

mkdir -p "$backup_dir" || fail "cannot create $backup_dir"
chmod 700 "$backup_dir" 2>/dev/null || true

# Free-space guard: refuse before anything is written; never delete to make room.
newest=""
for candidate in "$backup_dir"/health-*.dump "$backup_dir"/health-*.dump.age "$backup_dir"/health-*.dump.gpg; do
  [ -f "$candidate" ] || continue
  if [ -z "$newest" ] || [ "$candidate" -nt "$newest" ]; then newest="$candidate"; fi
done
if [ -n "$min_free_mb" ]; then
  required_bytes=$((10#$min_free_mb * 1024 * 1024))
  basis="BACKUP_MIN_FREE_MB=$min_free_mb"
elif [ -n "$newest" ]; then
  required_bytes=$((2 * $(wc -c <"$newest")))
  [ "$required_bytes" -ge "$GIB" ] || required_bytes=$GIB
  basis="twice the newest dump, at least 1 GiB"
else
  required_bytes=$((5 * GIB))
  basis="5 GiB before the first dump"
fi
if [ "$required_bytes" -gt 0 ]; then
  free_before="$(free_bytes_of "$backup_dir")" || fail "cannot read free space of $backup_dir (df failed)"
  if [ "$free_before" -lt "$required_bytes" ]; then
    fail "refusing to start: $(human "$free_before") free in $backup_dir, need $(human "$required_bytes") ($basis). No backup was written or deleted. Add disk space or move old backups to other storage (docs/RUNBOOK.md, 'Disk space for backups'), or set BACKUP_MIN_FREE_MB."
  fi
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output="$backup_dir/health-$stamp.dump.$method"
partial="$output.partial"

if ! docker compose --profile backup run --rm -T backup | "${encrypt[@]}" >"$partial"; then
  fail "pg_dump or encryption failed; no backup was written"
fi
[ -s "$partial" ] || fail "encrypted dump is empty; no backup was written"
mv "$partial" "$output"
partial=""
size_bytes="$(wc -c <"$output" | tr -d ' ')"

# Opt-in retention: only after a successful new backup, only health-* dumps
# (including legacy plaintext ones). Off by default.
if [ -n "$retention_days" ]; then
  pruned="$(find "$backup_dir" -maxdepth 1 -type f \
    \( -name 'health-*.dump' -o -name 'health-*.dump.age' -o -name 'health-*.dump.gpg' \) \
    ! -path "$output" -mtime "+$retention_days" -print -delete)"
  if [ -n "$pruned" ]; then
    printf '%s\n' "$pruned" >&2
    pruned_count="$(printf '%s\n' "$pruned" | wc -l | tr -d ' ')"
    echo "backup: BACKUP_RETENTION_DAYS=$retention_days deleted $pruned_count old dump(s)" >&2
  fi
fi

free_after="$(free_bytes_of "$backup_dir" || true)"
write_status success "backup written"
echo "backup: wrote $output ($size_bytes bytes); free space in $backup_dir: $(human "${free_after:-0}")" >&2
echo "$output"
