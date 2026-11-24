#!/usr/bin/env bash
# PostToolUse (Edit|Write|MultiEdit): format an edited Python file with ruff. Never blocks.
set -uo pipefail

input="$(cat)"
file="$(printf '%s' "$input" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))
except Exception:
    print("")' 2>/dev/null)"

case "$file" in
  *.py)
    [[ -f "$file" ]] || exit 0
    cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" || exit 0
    # migrations/versions is excluded in pyproject.toml (force-exclude), so ruff leaves it alone.
    uv run --no-sync ruff format --quiet -- "$file" >/dev/null 2>&1 || true
    ;;
esac
exit 0
