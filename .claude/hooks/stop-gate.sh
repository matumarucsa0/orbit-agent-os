#!/usr/bin/env bash
# Stop: before the agent says it is done, run the light gate on what changed (about 10-40 seconds):
# ruff on changed Python files, the docs and codemap checks, the personal-data scan, and the tests a
# change can affect (scripts/select_tests.py --worktree) when at most 12 files are selected.
# Exit 2 sends the failure back to the agent once; a second stop in a row is let through.
set -uo pipefail

input="$(cat)"
active="$(printf '%s' "$input" | python3 -c 'import json,sys
try:
    print(str(json.load(sys.stdin).get("stop_hook_active", False)).lower())
except Exception:
    print("false")' 2>/dev/null)"
[[ "$active" == "true" ]] && exit 0

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" || exit 0
[[ -n "$(git status --porcelain 2>/dev/null)" ]] || exit 0 # nothing changed: nothing to check

out="$(mktemp)"
fail=0
run() {
  local label="$1"
  shift
  if ! "$@" >>"$out" 2>&1; then
    fail=1
    echo "FAILED: $label" >>"$out"
  fi
}

changed_py="$( (git diff --name-only HEAD; git ls-files --others --exclude-standard) 2>/dev/null | grep '\.py$' | sort -u)"
if [[ -n "$changed_py" ]]; then
  # shellcheck disable=SC2086
  run "ruff check" uv run --no-sync ruff check $changed_py
  # shellcheck disable=SC2086
  run "ruff format --check" uv run --no-sync ruff format --check $changed_py
fi
run "docs check (make docs)" uv run --no-sync python scripts/check_docs.py
run "codemap current (make codemap)" uv run --no-sync python scripts/gen_codemap.py --check
run "personal-data scan (make scan)" scripts/scan-personal.sh

selected="$(uv run --no-sync python scripts/select_tests.py --worktree 2>/dev/null)"
case "$selected" in
  "" | NONE) ;;
  ALL) echo "note: shared code changed; run make test before you finish." >>"$out" ;;
  *)
    count="$(printf '%s\n' "$selected" | wc -l | tr -d ' ')"
    if [[ "$count" -le 12 ]]; then
      # shellcheck disable=SC2086
      run "affected tests" uv run --no-sync pytest -q -x -p no:cacheprovider $selected
    else
      echo "note: $count test files are affected; run make changed." >>"$out"
    fi
    ;;
esac

if [[ $fail -ne 0 ]]; then
  {
    echo "The light gate failed. Fix this, then finish (output trimmed):"
    tail -n 40 "$out"
  } >&2
  exit 2
fi
exit 0
