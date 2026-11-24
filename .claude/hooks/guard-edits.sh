#!/usr/bin/env bash
# PreToolUse (Edit|Write|MultiEdit): refuse edits that must never be made by hand.
# Reads the tool call as JSON on stdin; exit 2 blocks the edit and shows the message to the agent.
#   - .env files other than .env.example, database files, the private patterns file
#   - an applied migration (a migrations/versions file that is already committed): add a new one
#   - tests/prompt_hashes.json and evals/results/baseline-recorded.json: regenerate with the commands
set -uo pipefail

input="$(cat)"
file="$(printf '%s' "$input" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))
except Exception:
    print("")' 2>/dev/null)"
[[ -z "$file" ]] && exit 0

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
rel="${file#"$root"/}"

block() {
  echo "Blocked: $1" >&2
  exit 2
}

case "$rel" in
  .env.example) exit 0 ;;
  .env | .env.* | */.env | */.env.*) block "$rel holds secrets and is never edited by an agent. Change .env.example for new settings." ;;
  .pre-publish-patterns) block "$rel is the owner's private pattern list; edit .pre-publish-patterns.example instead." ;;
  *.db | *.sqlite | *.sqlite3 | *.dump | *.age) block "$rel is data, not code." ;;
  tests/prompt_hashes.json) block "regenerate it with: make prompts-snapshot (after bumping the prompt version)." ;;
  evals/results/baseline-recorded.json) block "regenerate it with: uv run python -m evals.run --mode recorded --json evals/results/baseline-recorded.json" ;;
  migrations/versions/*.py)
    if git -C "$root" cat-file -e "HEAD:$rel" >/dev/null 2>&1; then
      block "$rel is an applied migration. Never edit one; add a new revision (see the add-migration skill)."
    fi
    ;;
esac
exit 0
