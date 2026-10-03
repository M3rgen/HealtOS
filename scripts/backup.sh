#!/bin/sh
set -eu

backup_dir="${BACKUP_DIR:-/backups}"
retention_days="${BACKUP_RETENTION_DAYS:-30}"
mkdir -p "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output="$backup_dir/health-$stamp.dump"
: "${DATABASE_URL_PG:?DATABASE_URL_PG must be set}"
pg_dump --format=custom --compress=9 --file="$output" "$DATABASE_URL_PG"
find "$backup_dir" -type f -name 'health-*.dump' -mtime "+$retention_days" -delete
echo "$output"
