#!/usr/bin/env bash
# Restore a backup into a SEPARATE scratch database and print per-table row counts.
#
#   scripts/restore.sh backups/health-YYYYmmddTHHMMSSZ.dump.age [target_db]
#
# The target (default health_restore) is dropped and recreated; the live `health`
# database is never touched. Decryption keys:
#   *.age  BACKUP_AGE_IDENTITY, else BACKUP_KEY_FILE (default /etc/healthos/backup.key)
#   *.gpg  BACKUP_KEY_FILE passphrase file
#   *.dump legacy plaintext dump
# A restored archive is only usable by the app together with the same
# GOOGLE_TOKEN_ENCRYPTION_KEY that encrypted its OAuth refresh tokens.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

fail() {
  echo "restore: $*" >&2
  exit 1
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || {
  echo "usage: restore.sh BACKUP_FILE [target_db]" >&2
  exit 2
}
backup="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
target="${2:-health_restore}"
[ -f "$backup" ] || fail "no such backup: $1"
[[ "$target" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || fail "target database name must match [a-z_][a-z0-9_]*"
case "$target" in
  health | postgres | template0 | template1) fail "refusing to restore over '$target'; pick a scratch database" ;;
esac
key_file="${BACKUP_KEY_FILE:-/etc/healthos/backup.key}"

case "$backup" in
  *.age)
    command -v age >/dev/null || fail "age is required to decrypt $backup"
    identity="${BACKUP_AGE_IDENTITY:-$key_file}"
    [ -r "$identity" ] || fail "age identity $identity is not readable"
    decrypt=(age --decrypt --identity "$identity" "$backup")
    ;;
  *.gpg)
    command -v gpg >/dev/null || fail "gpg is required to decrypt $backup"
    [ -r "$key_file" ] || fail "passphrase file $key_file is not readable"
    decrypt=(gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$key_file"
      --decrypt "$backup")
    ;;
  *.dump) decrypt=(cat "$backup") ;;
  *) fail "unknown backup type: $backup" ;;
esac

cd "$ROOT"
psql_in() {
  docker compose exec -T postgres psql -U health -d "$1" -v ON_ERROR_STOP=1 \
    --tuples-only --no-align --field-separator=' ' --command "$2"
}

docker compose exec -T postgres dropdb -U health --if-exists "$target"
docker compose exec -T postgres createdb -U health "$target"
"${decrypt[@]}" | docker compose exec -T postgres \
  pg_restore -U health --dbname="$target" --no-owner --exit-on-error

count_sql="SELECT table_name, (xpath('/row/c/text()', query_to_xml(format(
  'SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text
  FROM information_schema.tables
  WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"

restored="$(psql_in "$target" "$count_sql")"
[ -n "$restored" ] || fail "restored database $target has no tables"
live="$(psql_in health "$count_sql" 2>/dev/null || true)"

echo "Restored $(basename "$backup") into database '$target'."
printf '%-40s %12s %12s\n' table restored live
while read -r table rows; do
  live_rows="$(printf '%s\n' "$live" | awk -v t="$table" '$1 == t { print $2 }')"
  printf '%-40s %12s %12s\n' "$table" "$rows" "${live_rows:--}"
done <<<"$restored"
echo "Alembic revision: $(psql_in "$target" 'SELECT version_num FROM alembic_version')"
echo "Drop the scratch copy when done: docker compose exec -T postgres dropdb -U health $target"
