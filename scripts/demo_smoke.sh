#!/usr/bin/env bash
# Smoke test of the demo instance: seed a throwaway database, boot the server with the offline
# fake Claude, check /healthz and /api/v1/context with the bearer token, stop the server.
# Needs uv; touches nothing outside a temporary directory. Port: PORT (default 8765).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PORT="${PORT:-8765}"
WORK="$(mktemp -d)"
server_pid=""

cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

token() { uv run python -c "import secrets; print(secrets.token_urlsafe(32))"; }

API_TOKEN="$(token)"
export DATABASE_URL="sqlite:///$WORK/demo.db"
export ORBIT_API_TOKEN="$API_TOKEN"
ORBIT_SECRET_KEY="$(token)"
ICS_TOKEN="$(token)"
export ORBIT_SECRET_KEY ICS_TOKEN
ORBIT_WEB_PASSWORD_HASH="$(
  DEMO_PW="$(token)" uv run python -c \
    "import os; from orbit.services.passwords import hash_password; print(hash_password(os.environ['DEMO_PW']))"
)"
export ORBIT_WEB_PASSWORD_HASH
export ORBIT_URL="http://127.0.0.1:$PORT"
export ORBIT_CLAUDE_MODE=fake
export OWNER_NAME=Alex
export SCHEDULER_ENABLED=false

uv run alembic upgrade head >/dev/null 2>&1
uv run python -m orbit.cli seed-demo

uv run uvicorn orbit.main:app --host 127.0.0.1 --port "$PORT" --log-level warning &
server_pid=$!

for _ in $(seq 1 60); do
  if curl -fsS "$ORBIT_URL/healthz" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo "The server stopped before it was ready." >&2
    exit 1
  fi
  sleep 0.5
done

health="$(curl -fsS "$ORBIT_URL/healthz")"
echo "healthz: $health"
case "$health" in *'"status":"ok"'*) ;; *) echo "healthz is not ok" >&2; exit 1 ;; esac

curl -fsS -H "Authorization: Bearer $API_TOKEN" "$ORBIT_URL/api/v1/context" -o "$WORK/context.json"
uv run python - "$WORK/context.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
people = len(data["people"]["people"])
goals = len(data["goals"]["goals"])
assert people >= 8, f"expected demo people, got {people}"
assert goals >= 3, f"expected demo goals, got {goals}"
print(f"context: {people} people, {goals} goals")
PY

status="$(curl -s -o /dev/null -w '%{http_code}' "$ORBIT_URL/api/v1/context")"
[[ "$status" == "401" || "$status" == "403" ]] || { echo "context without a token returned $status" >&2; exit 1; }
echo "Demo smoke test passed."
