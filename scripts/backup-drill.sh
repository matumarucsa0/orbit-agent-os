#!/usr/bin/env bash
# End-to-end test of the production stack's backup and restore, in a throwaway compose project
# with its own database, key and backup directory. It never touches the real stack or .env.
# CI runs it; run it anywhere with Docker:  scripts/backup-drill.sh
#
#   1. start db + app from docker-compose.prod.yml (the app migrates Postgres on start)
#   2. add a person, start the backup service: its first run backs up right away (local copy +
#      "off-site" copy in a local directory) and it turns healthy
#   3. change the data, run a --check restore, then a real restore
#   4. the data is back as it was at backup time, and the app is healthy again
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash: keep container paths as they are
cd "$(dirname "$0")/.."

id="drill-$$"
dir="backups/$id"
mkdir -p "$dir/backups"
random() { od -An -N24 -tx1 /dev/urandom | tr -d ' \n'; }

cat >"$dir/drill.env" <<ENV
ORBIT_ENV_FILE=$dir/drill.env
BACKUP_DIR=./$dir/backups
POSTGRES_PASSWORD=$(random)
ORBIT_DOMAIN=localhost
ORBIT_URL=https://localhost
ORBIT_API_TOKEN=$(random)
ORBIT_SECRET_KEY=$(random)
ICS_TOKEN=$(random)
ORBIT_WEB_PASSWORD_HASH='\$argon2id\$v=19\$m=65536,t=3,p=4\$ZHJpbGw\$ZHJpbGw'
SCHEDULER_ENABLED=false
NTFY_TOPIC=
BACKUP_TARGET=/backups/offsite
ENV

compose=(docker compose -p "orbit-$id" -f docker-compose.prod.yml --env-file "$dir/drill.env")
export ORBIT_COMPOSE="${compose[*]}"

cleanup() {
    status=$?
    # Files in the bind mount belong to root in the container; remove them from there.
    "${compose[@]}" run --rm --no-deps -T --entrypoint rm backup -rf /backups/offsite \
        /backups/.last-success >/dev/null 2>&1 || true
    "${compose[@]}" run --rm --no-deps -T backup sh -c 'rm -f /backups/orbit-*' >/dev/null 2>&1 || true
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$dir"
    if ((status == 0)); then echo "DRILL PASSED"; else echo "DRILL FAILED" >&2; fi
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }

people() {
    "${compose[@]}" exec -T app python -c '
from sqlalchemy import select
from orbit.db import make_engine
from orbit.domain import Person
from orbit.settings import get_settings
from sqlalchemy.orm import Session
with Session(make_engine(get_settings().database_url)) as s:
    print(",".join(sorted(s.scalars(select(Person.display_name)))))'
}

add_person() {
    "${compose[@]}" exec -T app python -c '
import sys
from orbit.db import make_engine
from orbit.domain import Person
from orbit.settings import get_settings
from sqlalchemy.orm import Session
with Session(make_engine(get_settings().database_url)) as s:
    s.add(Person(display_name=sys.argv[1], search_name_norm=sys.argv[1].lower(), archived=False))
    s.commit()' "$1"
}

expect() {
    if [ "$1" != "$2" ]; then
        echo "expected '$2', got '$1'" >&2
        exit 1
    fi
    echo "ok: $2"
}

step "build"
"${compose[@]}" build --quiet app backup

step "generate a throwaway age key"
"${compose[@]}" run --rm --no-deps -T backup age-keygen 2>/dev/null >"$dir/key.txt"
echo "BACKUP_AGE_RECIPIENT=$(grep -o 'age1[0-9a-z]*' "$dir/key.txt")" >>"$dir/drill.env"

step "start db + app (migrates Postgres)"
"${compose[@]}" up -d --wait db app
"${compose[@]}" exec -T app python -m orbit.cli smoke-test --url http://127.0.0.1:8000

step "seed and back up"
add_person "Paťo Drill"
"${compose[@]}" up -d --wait backup
"${compose[@]}" logs backup
local_copies=$(find "$dir/backups" -maxdepth 1 -name 'orbit-*.dump.age' | wc -l | tr -d ' ')
offsite_copies=$(find "$dir/backups/offsite" -name 'orbit-*.dump.age' | wc -l | tr -d ' ')
expect "$local_copies/$offsite_copies" "1/1"
expect "$(head -c 21 "$dir"/backups/orbit-*.dump.age)" "age-encryption.org/v1"

step "the backup service stops promptly"
start=$(date +%s)
"${compose[@]}" stop backup
expect "$(( $(date +%s) - start < 8 ))" "1"

step "change the data after the backup"
add_person "Added after the backup"
expect "$(people)" "Added after the backup,Paťo Drill"

step "restore --check (live data untouched)"
# The key on stdin, as with `ssh server '... /dev/stdin' < key` from a laptop.
check_output=$(scripts/restore.sh --check latest /dev/stdin <"$dir/key.txt")
echo "$check_output"
grep -Eq '^ *person *\| *1$' <<<"$check_output" || { echo "check didn't list 1 person" >&2; exit 1; }
expect "$(people)" "Added after the backup,Paťo Drill"

step "a wrong key is refused"
"${compose[@]}" run --rm --no-deps -T backup age-keygen 2>/dev/null >"$dir/wrong.txt"
if scripts/restore.sh --check latest "$dir/wrong.txt" >/dev/null 2>&1; then
    echo "restore accepted a wrong key" >&2
    exit 1
fi
echo "ok: refused"

step "real restore"
scripts/restore.sh --yes latest "$dir/key.txt"
expect "$(people)" "Paťo Drill"
"${compose[@]}" exec -T app python -m orbit.cli smoke-test --url http://127.0.0.1:8000
