#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: restore.sh /path/to/health.dump" >&2
  exit 2
fi
: "${DATABASE_URL_PG:?DATABASE_URL_PG must be set}"
pg_restore --clean --if-exists --no-owner --dbname="$DATABASE_URL_PG" "$1"
