#!/bin/bash
# Restore an encrypted backup. The age identity (private key) is read from stdin, so it's never
# stored on the server:
#
#   restore.sh [--check] <backup> < key.txt
#
# <backup>: "latest" (newest in BACKUP_TARGET), a file name in BACKUP_TARGET, or /backups/<file>.
# The backup is restored into a new database "orbit_restore" and verified there, then:
#   --check  its contents are listed and it is dropped again; the live database is untouched
#   default  it replaces the live database, which is kept as <name>_before_restore_<UTC time>.
#            Stop the app first (scripts/restore.sh does all of this).
set -euo pipefail
source /usr/local/lib/orbit-backup/lib.sh

check=0
if [ "${1:-}" = "--check" ]; then
    check=1
    shift
fi
[ $# -eq 1 ] || die "usage: restore.sh [--check] latest|<file name>|/backups/<file> < key.txt"
[ ! -t 0 ] || die "pipe the age identity (private key) into stdin: restore.sh ... < key.txt"

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cat >"$work/identity"
[ -s "$work/identity" ] || die "no age identity on stdin"

source=$1
if [ "$source" = "latest" ]; then
    source=$(rclone lsf --files-only --include 'orbit-*.dump.age' "$BACKUP_TARGET" | sort | tail -n 1)
    [ -n "$source" ] || die "no backups found in BACKUP_TARGET"
fi
if [[ $source == /* ]]; then
    file=$source
else
    log "downloading $source"
    rclone copyto "$BACKUP_TARGET/$source" "$work/$source"
    file=$work/$source
fi

log "decrypting $(basename "$file")"
age --decrypt --identity "$work/identity" --output "$work/orbit.dump" "$file"
pg_restore --list "$work/orbit.dump" >/dev/null

live=$PGDATABASE
scratch=orbit_restore
admin() { PGOPTIONS="-c client_min_messages=warning" psql -X -q -v ON_ERROR_STOP=1 -d postgres "$@"; }

admin -c "DROP DATABASE IF EXISTS $scratch WITH (FORCE)" -c "CREATE DATABASE $scratch"
pg_restore --no-owner --no-privileges --exit-on-error --single-transaction \
    --dbname "$scratch" "$work/orbit.dump"
log "restored into $scratch:"
psql -X -v ON_ERROR_STOP=1 -d "$scratch" \
    -c "SELECT version_num AS schema_revision FROM alembic_version" -c "
    SELECT table_name AS \"table\",
           (xpath('/row/n/text()', query_to_xml(
               format('SELECT count(*) AS n FROM %I', table_name), false, true, '')))[1]::text::int AS rows
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name <> 'alembic_version'
    ORDER BY 1"

if ((check)); then
    admin -c "DROP DATABASE $scratch"
    log "check passed; the live database was not touched"
    exit 0
fi

previous="${live}_before_restore_$(date -u +%Y%m%d%H%M%S)"
admin -o /dev/null \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$live'" \
    -c "ALTER DATABASE \"$live\" RENAME TO \"$previous\"" \
    -c "ALTER DATABASE $scratch RENAME TO \"$live\""
log "restored: $live now holds the backup; the previous database is kept as $previous"
log "drop it once everything looks right: DROP DATABASE \"$previous\""
