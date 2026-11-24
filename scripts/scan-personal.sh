#!/usr/bin/env bash
# Scan the repository files for personal data and secrets. Exit 1 on any hit.
#
#   scripts/scan-personal.sh            # tracked + untracked (not ignored) files
#   scripts/scan-personal.sh PATH...    # only these files or directories
#
# Two layers:
#   1. generic regexes (keys and tokens, private keys, e-mail addresses, IBANs, phone numbers,
#      machine-local hostnames, files that must never be committed). Matches that are listed in
#      .pre-publish-allow (invented values: documentation IBANs, example.* addresses, test phone
#      numbers) are ignored.
#   2. your own strings, one extended regex per line, read from an untracked file: by default
#      .pre-publish-patterns in the repository root (git-ignored; copy .pre-publish-patterns.example),
#      or the file named by PRE_PUBLISH_PATTERNS (keep it outside the repo).
#
# Output is "file:line [kind]" only. It never prints the matched text, so the log cannot leak the
# very thing it looks for. A line carrying the marker "scan:ok" is skipped (use it for the owner's
# credit line only). LICENSE and the name-day data are skipped for your own patterns.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PATTERNS_FILE="${PRE_PUBLISH_PATTERNS:-.pre-publish-patterns}"
ALLOW_FILE=".pre-publish-allow"
hits=0

if [[ $# -gt 0 ]]; then
  files=()
  while IFS= read -r -d '' f; do files+=("$f"); done < <(find "$@" -type f -print0)
else
  files=()
  while IFS= read -r -d '' f; do files+=("$f"); done < <(git ls-files -z --cached --others --exclude-standard)
fi

# Never scan the files that define the patterns, lock files or binaries (grep -I skips binaries).
scan_files=()
for f in "${files[@]}"; do
  case "$f" in
    uv.lock | .pre-publish-allow | .pre-publish-patterns | .pre-publish-patterns.example | scripts/scan-personal.sh) continue ;;
  esac
  [[ -f "$f" ]] && scan_files+=("$f")
done

report() { # file:line [kind]
  hits=$((hits + 1))
  echo "$1 [$2]"
}

# --- files that must never be committed ---
for f in "${scan_files[@]}"; do
  base="${f##*/}"
  case "$f" in
    .env.example) ;;
    .env | .env.* | *.db | *.sqlite | *.sqlite3 | *.dump | *.age | *.shortcut | backups/* | .claude/settings.local.json)
      report "$f" "forbidden file" ;;
  esac
  [[ "$base" == ".DS_Store" ]] && report "$f" "forbidden file"
done

# --- generic patterns: "kind<TAB>regex" ---
allow_re=""
if [[ -f "$ALLOW_FILE" ]]; then
  allow_re="$(grep -vE '^[[:space:]]*(#|$)' "$ALLOW_FILE" | paste -sd'|' -)"
fi

generic() {
  local kind="$1" regex="$2" line match
  # -o prints only the match, so it can be compared with the allow list; the text is not printed.
  while IFS= read -r line; do
    match="${line#*:*:}"
    if [[ -n "$allow_re" ]] && printf '%s\n' "$match" | grep -qiE -- "$allow_re"; then
      continue
    fi
    report "${line%%:*}:$(echo "$line" | cut -d: -f2)" "$kind"
  done < <(grep -nHIoE -- "$regex" "${scan_files[@]}" 2>/dev/null || true)
}

generic "secret" 'sk-ant-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
generic "credential assignment" '(api[_-]?key|token|secret|passw(or)?d)[a-z_]*[=:] *["'"'"']?[A-Za-z0-9+/_-]{24,}'
generic "e-mail" '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'
generic "iban" '\b[A-Z]{2}[0-9]{2}( ?[0-9A-Z]{4}){3,7}\b'
generic "phone" '\+[0-9]{2,3}[ -]?[0-9]{2,4}[ -]?[0-9]{3,4}[ -]?[0-9]{3,4}\b|\b00[1-9][0-9]{1,2}[ -][0-9]{2,4}[ -][0-9]{3,4}[ -][0-9]{3,4}\b'
generic "local hostname" '[A-Za-z0-9-]+\.(lan|ts\.net|duckdns\.org|internal)\b'

# --- your own strings ---
if [[ -f "$PATTERNS_FILE" ]]; then
  patterns="$(grep -vE '^\s*(#|$)' "$PATTERNS_FILE" || true)"
  if [[ -n "$patterns" ]]; then
    private_files=()
    for f in "${scan_files[@]}"; do
      case "$f" in LICENSE | data/namedays_*.json) continue ;; esac
      private_files+=("$f")
    done
    while IFS= read -r line; do
      [[ "$line" == *"scan:ok"* ]] && continue
      report "$(echo "$line" | cut -d: -f1,2)" "private pattern"
    done < <(printf '%s\n' "$patterns" | grep -nHIiE -f - "${private_files[@]}" 2>/dev/null || true)
  fi
else
  echo "note: no $PATTERNS_FILE (copy .pre-publish-patterns.example); generic checks only." >&2
fi

if [[ $hits -gt 0 ]]; then
  echo "scan-personal: $hits hit(s). Remove or replace with invented values; see CONTRIBUTING.md." >&2
  exit 1
fi
echo "scan-personal: clean (${#scan_files[@]} files)."
