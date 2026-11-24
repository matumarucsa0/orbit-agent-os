# shellcheck shell=bash
# Shared helpers for backup.sh, restore.sh and scheduler.sh.

log() {
    printf '%s [orbit-backup] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

die() {
    log "error: $*"
    exit 1
}

_minutes() {
    local hours=${1%:*} minutes=${1#*:}
    echo $((10#$hours * 60 + 10#$minutes))
}

# Unix time when the current quiet hours (QUIET_HOURS, as in the app) end; nothing outside them.
quiet_hours_end() {
    local spec=${QUIET_HOURS:-22:00-08:00}
    spec=${spec// /}
    local start end now
    start=$(_minutes "${spec%-*}")
    end=$(_minutes "${spec#*-}")
    now=$(_minutes "$(date +%H:%M)")
    if ((start <= end)); then
        ((now >= start && now < end)) || return 0
    else
        ((now >= start || now < end)) || return 0
    fi
    echo $(($(date +%s) / 60 * 60 + (end - now + 1440) % 1440 * 60))
}

# Push a failure through ntfy (NTFY_URL / NTFY_TOPIC / NTFY_TOKEN from .env). Raised during the
# quiet hours, ntfy holds it until they end.
notify_failure() {
    [ -n "${NTFY_TOPIC:-}" ] || return 0
    local args=(-fsS -m 20 -H "Title: Orbit backup failed" -H "Tags: warning" -H "Priority: high")
    [ -z "${NTFY_TOKEN:-}" ] || args+=(-H "Authorization: Bearer $NTFY_TOKEN")
    local delay
    delay=$(quiet_hours_end)
    [ -z "$delay" ] || args+=(-H "Delay: $delay")
    curl "${args[@]}" -d "$1" "${NTFY_URL:-https://ntfy.sh}/$NTFY_TOPIC" >/dev/null ||
        log "could not send the failure push"
}
