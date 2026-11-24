#!/bin/bash
# One encrypted backup of the Orbit database:
#   pg_dump (custom format) | age -> /backups/orbit-<UTC time>.dump.age -> rclone -> BACKUP_TARGET
# The unencrypted dump never touches the disk. Copies older than BACKUP_KEEP_LOCAL_DAYS (local)
# and BACKUP_KEEP_DAYS (off-site) are pruned after a successful run.
set -euo pipefail
source /usr/local/lib/orbit-backup/lib.sh

: "${BACKUP_AGE_RECIPIENT:?is not set (the age public key backups are encrypted to)}"
: "${BACKUP_TARGET:?is not set (the off-site rclone path)}"

name="orbit-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
partial="/backups/.$name.partial"
trap 'rm -f "$partial"' EXIT

log "dumping database $PGDATABASE"
pg_dump --format=custom --no-owner --no-privileges |
    age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" --output "$partial"
mv "$partial" "/backups/$name"
log "saved /backups/$name ($(du -h "/backups/$name" | cut -f1))"

rclone copyto "/backups/$name" "$BACKUP_TARGET/$name"
log "uploaded $name off-site"

find /backups -maxdepth 1 -name 'orbit-*.dump.age' -mtime +"${BACKUP_KEEP_LOCAL_DAYS:-7}" -delete
rclone delete "$BACKUP_TARGET" --min-age "${BACKUP_KEEP_DAYS:-90}d" --include 'orbit-*.dump.age'
touch /backups/.last-success
