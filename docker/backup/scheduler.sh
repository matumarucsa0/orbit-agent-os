#!/bin/bash
# The backup service's main process: one backup every day at BACKUP_TIME (local time, TZ), and
# one right away when the last successful backup is more than a day old (first start, missed
# nights). A failed run is pushed through ntfy; the container healthcheck turns unhealthy too.
set -uo pipefail
source /usr/local/lib/orbit-backup/lib.sh

backup_time=${BACKUP_TIME:-03:30}
stamp=/backups/.last-success

trap 'exit 0' TERM INT

run_backup() {
    if ! backup.sh; then
        log "backup failed"
        notify_failure "The nightly backup failed; see: docker compose -f docker-compose.prod.yml logs backup"
    fi
}

if [ ! -f "$stamp" ] || [ -n "$(find "$stamp" -mmin +1440)" ]; then
    run_backup
fi
while true; do
    now=$(date +%s)
    next=$(date -d "$(date +%Y-%m-%d) $backup_time" +%s)
    ((next > now)) || next=$((next + 86400))
    log "next backup at $(date -d "@$next" '+%Y-%m-%d %H:%M')"
    # In the background, so `docker stop` (TERM) interrupts the wait.
    sleep $((next - now)) &
    wait $!
    run_backup
done
