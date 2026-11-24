#!/bin/sh
set -eu

alembic upgrade head

# `docker compose run --rm app python -m orbit.cli ...`: run that command (after migrating).
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# FORWARDED_ALLOW_IPS: the reverse proxy's address, so its client IP/scheme headers are trusted
# (docker-compose.prod.yml sets "*": only Caddy can reach the app there).
exec uvicorn orbit.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
