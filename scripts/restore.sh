#!/usr/bin/env bash
# Restore Orbit from an encrypted backup. Run it on the server, in the repo directory.
#
#   scripts/restore.sh --check latest ~/orbit-backup.key   # drill: restore into a scratch DB,
#                                                          # list its contents, drop it again
#   scripts/restore.sh latest ~/orbit-backup.key           # real restore (asks first)
#
# The key can stay on your laptop:
#   ssh server 'cd /opt/orbit && scripts/restore.sh --check latest /dev/stdin' < orbit-backup.key
#
# <backup>: "latest" (newest off-site), a file name in BACKUP_TARGET, or /backups/<file> for a
# local copy. The key is the age identity whose public key is BACKUP_AGE_RECIPIENT; it is piped
# into the backup container and never stored on the server.
# A real restore stops the app, swaps the restored database in (the old one is kept as
# orbit_before_restore_<time>), then starts the app again, which migrates it if needed.
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash: keep /backups/... paths as they are

usage() {
    echo "usage: $0 [--check] [--yes] latest|<file name>|/backups/<file> <age identity file>" >&2
    exit 2
}

check=() yes=0
while [[ ${1:-} == --* ]]; do
    case $1 in
        --check) check=(--check) ;;
        --yes) yes=1 ;;
        *) usage ;;
    esac
    shift
done
[ $# -eq 2 ] || usage
backup=$1 key=$2
[ -r "$key" ] || { echo "Can't read the age identity file: $key" >&2; exit 1; }

# ORBIT_COMPOSE: set by scripts/backup-drill.sh to point at its throwaway project.
read -ra compose <<<"${ORBIT_COMPOSE:-docker compose -f docker-compose.prod.yml}"

if [ ${#check[@]} -eq 1 ]; then
    exec "${compose[@]}" run --rm -T backup restore.sh --check "$backup" <"$key"
fi

if ((!yes)); then
    echo "This replaces the live database with backup '$backup'."
    echo "The current database is kept as orbit_before_restore_<time>."
    # From the terminal: stdin may be the key (ssh ... < key).
    if ! read -rp "Type 'restore' to continue: " answer </dev/tty; then
        echo "No terminal to confirm on; pass --yes." >&2
        exit 1
    fi
    [ "$answer" = restore ] || { echo "Cancelled."; exit 1; }
fi

"${compose[@]}" stop app
# Whatever happens, bring the app back (with the old database if the restore failed early).
trap '"${compose[@]}" up -d --wait app' EXIT
"${compose[@]}" run --rm -T backup restore.sh "$backup" <"$key"
