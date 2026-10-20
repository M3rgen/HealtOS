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
#
# Other settings: BACKUP_HOST_DIR (default ./backups), BACKUP_RETENTION_DAYS (default 14).
# Both may come from the environment or the project's .env file.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

env_file_value() {
  # Reads one plain KEY=value from .env without sourcing it (it holds secrets).
  [ -f .env ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | tail -n 1 | sed "s/^[\"']//; s/[\"']$//"
}

fail() {
  echo "backup: $*" >&2
  exit 1
}

backup_dir="${BACKUP_HOST_DIR:-$(env_file_value BACKUP_HOST_DIR)}"
backup_dir="${backup_dir:-./backups}"
retention_days="${BACKUP_RETENTION_DAYS:-$(env_file_value BACKUP_RETENTION_DAYS)}"
retention_days="${retention_days:-14}"
key_file="${BACKUP_KEY_FILE:-/etc/healthos/backup.key}"
recipient="${BACKUP_AGE_RECIPIENT:-}"

case "$retention_days" in
  '' | *[!0-9]*) fail "BACKUP_RETENTION_DAYS must be a whole number of days" ;;
esac
[ "$retention_days" -ge 1 ] || fail "BACKUP_RETENTION_DAYS must be at least 1"

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

mkdir -p "$backup_dir"
chmod 700 "$backup_dir" 2>/dev/null || true
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output="$backup_dir/health-$stamp.dump.$method"
partial="$output.partial"
trap 'rm -f "$partial"' EXIT

docker compose --profile backup run --rm -T backup | "${encrypt[@]}" >"$partial"
[ -s "$partial" ] || fail "encrypted dump is empty"
mv "$partial" "$output"
trap - EXIT

# Retention: only after a successful new backup; includes legacy plaintext dumps.
find "$backup_dir" -maxdepth 1 -type f \
  \( -name 'health-*.dump' -o -name 'health-*.dump.age' -o -name 'health-*.dump.gpg' \) \
  -mtime "+$retention_days" -print -delete >&2

echo "$output"
