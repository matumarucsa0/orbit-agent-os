#!/usr/bin/env bash
# Make the weekly review with Claude Code (headless, on a Claude subscription): SPEC §2.18.
#
#   scripts/weekly-review.sh            what the timer runs: does nothing unless a review is due
#   scripts/weekly-review.sh --force    make this week's review now (also retries a failed one)
#
# It asks Orbit what Claude should read (prepare), has Claude Code answer with no tools allowed
# (the data holds text other people wrote, so the session must not be able to act on it), and
# sends the answer back (submit). Orbit checks it like any other answer. Settings come from
# ORBIT_REVIEW_ENV (default /etc/orbit-review.env): ORBIT_URL, ORBIT_REVIEW_TOKEN and, optionally,
# CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), REVIEW_CLAUDE_MODEL, CLAUDE_BIN, NTFY_URL,
# NTFY_TOPIC, NTFY_TOKEN (a failure push, at most once a day).
set -euo pipefail

ENV_FILE="${ORBIT_REVIEW_ENV:-/etc/orbit-review.env}"
# shellcheck source=/dev/null
source "$ENV_FILE"
: "${ORBIT_URL:?is not set in $ENV_FILE}"
: "${ORBIT_REVIEW_TOKEN:?is not set in $ENV_FILE}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
[[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] || export CLAUDE_CODE_OAUTH_TOKEN
STATE_DIR="${STATE_DIRECTORY:-$HOME/.local/state/orbit-review}"
CLAUDE_TIMEOUT="${CLAUDE_TIMEOUT:-1200}"
API="${ORBIT_URL%/}/api/v1/review-agent"

log() { echo "[orbit-review] $*" >&2; }

force=""
[[ "${1:-}" == "--force" ]] && force="?force=1"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
review_id=""

# A failure push, at most once a day (the timer runs hourly).
alert() {
    [[ -n "${NTFY_TOPIC:-}" ]] || return 0
    mkdir -p "$STATE_DIR"
    local stamp="$STATE_DIR/last-alert" today
    today="$(date +%F)"
    [[ "$(cat "$stamp" 2>/dev/null || true)" != "$today" ]] || return 0
    local auth=()
    [[ -z "${NTFY_TOKEN:-}" ]] || auth=(-H "Authorization: Bearer $NTFY_TOKEN")
    curl -sS -m 20 ${auth[@]+"${auth[@]}"} -H "Title: Weekly review failed" \
        -d "$1" "${NTFY_URL:-https://ntfy.sh}/$NTFY_TOPIC" >/dev/null 2>&1 && echo "$today" >"$stamp"
    return 0
}

api() { # api METHOD PATH [JSON-FILE] -> status code; body in $work/reply.json
    local method="$1" path="$2" body="${3:-}" args=()
    [[ -z "$body" ]] || args=(-H "Content-Type: application/json" --data-binary "@$body")
    curl -sS -m 120 -o "$work/reply.json" -w '%{http_code}' -X "$method" \
        -H "Authorization: Bearer $ORBIT_REVIEW_TOKEN" ${args[@]+"${args[@]}"} "$API$path"
}

die() {
    log "failed: $1"
    if [[ -n "$review_id" ]]; then
        jq -n --arg error "$1" '{error: $error}' >"$work/fail.json"
        api POST "/$review_id/fail" "$work/fail.json" >/dev/null 2>&1 || true
    fi
    alert "Weekly review failed: $1"
    exit 1
}

# 1. What is there to read?
code="$(api POST "/prepare$force")" || die "could not reach Orbit"
[[ "$code" != 204 ]] || exit 0 # nothing due, or already made
[[ "$code" == 200 ]] || die "prepare answered $code"
cp "$work/reply.json" "$work/prepared.json"
review_id="$(jq -r .review_id "$work/prepared.json")"
jq -r .system "$work/prepared.json" >"$work/system.txt"
jq -r .prompt "$work/prepared.json" >"$work/prompt.txt"
jq -c .schema "$work/prepared.json" >"$work/schema.json"
log "review $review_id: asking Claude Code"

# `timeout` is in GNU coreutils (the server); without it (macOS) Claude Code just runs unbounded.
with_timeout() {
    if command -v timeout >/dev/null 2>&1; then timeout "$CLAUDE_TIMEOUT" "$@"; else "$@"; fi
}

# Claude Code reads CLAUDE.md and settings from the directory it runs in: use an empty one, so
# the review sees only what Orbit sent (and the call stays small).
cd "$work"

# 2. Ask Claude Code, with every tool switched off. Prints its JSON envelope to $work/answer.json.
ask() { # ask EXTRA-TEXT
    local model_args=()
    [[ -z "${REVIEW_CLAUDE_MODEL:-}" ]] || model_args=(--model "$REVIEW_CLAUDE_MODEL")
    { cat "$work/prompt.txt"; printf '%s' "$1"; } |
        with_timeout "$CLAUDE_BIN" -p --output-format json \
            --json-schema "$(cat "$work/schema.json")" --tools "" --no-session-persistence \
            --strict-mcp-config --disable-slash-commands \
            --system-prompt "$(cat "$work/system.txt")" ${model_args[@]+"${model_args[@]}"} >"$work/answer.json" ||
        return 1
    jq -e '.is_error == false' "$work/answer.json" >/dev/null || return 1
    jq -e '.structured_output // (.result | fromjson)' "$work/answer.json" >"$work/output.json"
}

# 3. Send the answer back. A 422 carries what was wrong with it: one more try with that.
extra=""
for attempt in 1 2; do
    ask "$extra" || die "Claude Code gave no usable answer"
    used_model="$(jq -r '(.modelUsage // {}) | keys | first // empty' "$work/answer.json")"
    jq -n --slurpfile out "$work/output.json" --arg model "$used_model" --slurpfile env "$work/answer.json" '
        {output: $out[0], model: ($model | select(. != "")),
         input_tokens: (($env[0].usage.input_tokens // 0) + ($env[0].usage.cache_creation_input_tokens // 0)
                        + ($env[0].usage.cache_read_input_tokens // 0)),
         output_tokens: ($env[0].usage.output_tokens // 0)}' >"$work/submit.json"
    code="$(api POST "/$review_id/submit" "$work/submit.json")" || die "could not reach Orbit"
    if [[ "$code" == 200 ]]; then
        log "review $review_id is ready"
        exit 0
    fi
    [[ "$code" == 422 && "$attempt" == 1 ]] || die "submit answered $code"
    extra="

Your previous answer was rejected: $(jq -c .detail "$work/reply.json" | head -c 1500). Return a corrected object."
done
