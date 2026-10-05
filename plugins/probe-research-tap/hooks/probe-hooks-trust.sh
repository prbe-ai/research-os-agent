# Sourced by session-start.sh and ensure-daemon.sh: tells a Codex researcher
# when Probe's own hooks are not running in this session.
#
# Codex trusts each hooks.json entry by a hash of the whole entry and silently
# skips one it has not approved. Probe's tracking plugins (probe-research,
# probe-research-daemon) moved to one fixed entry per event, `hooks/dispatch.sh`,
# so after that upgrade every Probe hook stays off until the researcher approves
# the new entries in `/hooks`, and nothing in the session says so. This plugin's
# own entries are unchanged and still trusted, so it is the one place left that
# can say it.
#
# How it knows, with no Codex internals:
#   * dispatch.py touches $PROBE_HOOKS_STATE/alive/<session> every time it runs
#     at SessionStart or on a prompt. The file existing proves Probe's hooks run.
#   * At the real SessionStart (the payload's hook_event_name) this plugin
#     records ONCE whether the session should expect that file: an ENABLED
#     Probe plugin whose newest installed version ships hooks/dispatch.sh. A
#     session that started before the upgrade runs the old entries (no
#     heartbeat) and records 0, so it is never told to approve anything; a
#     respawn later in the session (a UserPromptSubmit payload) only ever
#     records 0, and never over an existing record.
#   * On a prompt: expected and no heartbeat -> one systemMessage, on at most
#     NUDGE_PROMPTS prompts a session. Clears once dispatch.py has run; stops at
#     once if the researcher switches the Probe plugin off (config.toml newer
#     than the record re-reads the switch).
#
# Fail-quiet throughout: any missing file, unreadable config or odd path means
# no message, never an error and never a blocked prompt.

PROBE_HOOKS_STATE="${XDG_STATE_HOME:-${HOME:-}/.local/state}/probe/hooks"

PROBE_HOOKS_NUDGE='{"systemMessage": "Probe: its hooks are switched off in this Codex session. They changed in the last Probe update, and Codex runs a changed hook only after you approve it. Type /hooks and approve the Probe Research hooks."}'

#: Prompts a session hears it on: enough to be seen, never a nag that the
#: researcher has no way to stop.
NUDGE_PROMPTS=3

# 0 when the plugin is enabled in Codex's config.toml. A missing section, file
# or `enabled = false` all mean no: a false "approve your hooks" is worse than
# none (`probe doctor` reports every case). Plain awk (mawk has no character
# classes); CR line ends and trailing comments are dropped first.
probe_hooks_enabled() {
    local key="$1" config="$2"
    [ -f "$config" ] || return 1
    awk -v want="[plugins.\"$key\"]" '
        { sub(/\r$/, ""); sub(/[ \t]+#.*$/, ""); sub(/^[ \t]+/, ""); sub(/[ \t]+$/, "") }
        $0 == want { inside = 1; next }
        /^\[/ { inside = 0 }
        inside && /^enabled[ \t]*=[ \t]*true$/ { found = 1 }
        END { exit found ? 0 : 1 }
    ' "$config" 2>/dev/null
}

# 0 when either Probe tracking plugin is enabled. $1 the marketplace.
probe_hooks_any_enabled() {
    local config name
    config="${2:-}"
    for name in probe-research probe-research-daemon; do
        probe_hooks_enabled "$name@$1" "$config" && return 0
    done
    return 1
}

# Codex only: record whether this session expects the heartbeat.
# $1 session id, $2 this plugin's root (…/cache/<marketplace>/probe-research-tap/<version>),
# $3 the payload's hook_event_name.
probe_hooks_record() {
    # The file name for a session id: the same rule dispatch.py applies.
    local sid="${1//[^A-Za-z0-9_-]/}" tap_root="${2%/}" event="${3:-}" cache marketplace expect=0 name manifest dir config
    [ -n "$sid" ] && [ -n "$tap_root" ] || return 0
    mkdir -p "$PROBE_HOOKS_STATE/expect" 2>/dev/null || return 0
    [ -e "$PROBE_HOOKS_STATE/expect/$sid" ] && return 0
    if [ "$event" = SessionStart ]; then
        cache="${tap_root%/*/*}"
        marketplace="${cache##*/}"
        config="${CODEX_HOME:-${HOME:-}/.codex}/config.toml"
        for name in probe-research probe-research-daemon; do
            probe_hooks_enabled "$name@$marketplace" "$config" || continue
            # The newest installed version, by the same manifest rule the hooks'
            # stale-root recovery uses.
            manifest="$(ls -dt "$cache/$name"/[0-9]*.[0-9]*/.codex-plugin/plugin.json 2>/dev/null | head -1 || true)"
            [ -n "$manifest" ] || continue
            dir="${manifest%/.codex-plugin/plugin.json}"
            [ -f "$dir/hooks/dispatch.sh" ] && expect=1
        done
    fi
    printf '%s\n' "$expect" >"$PROBE_HOOKS_STATE/expect/$sid" 2>/dev/null || true
    # Housekeeping, once per session: a file per session, kept two weeks.
    find "$PROBE_HOOKS_STATE/expect" "$PROBE_HOOKS_STATE/alive" -type f -mtime +14 -delete 2>/dev/null || true
    return 0
}

# Every prompt, Codex only: print the message when the session expects Probe's
# heartbeat and has none. File tests and builtin reads; a process (awk) only
# when Codex's config changed since the record. $2 this plugin's root.
probe_hooks_nudge() {
    local sid="${1//[^A-Za-z0-9_-]/}" tap_root="${2%/}" flag="" shown=0 record config cache
    [ -n "$sid" ] || return 0
    [ -e "$PROBE_HOOKS_STATE/alive/$sid" ] && return 0
    record="$PROBE_HOOKS_STATE/expect/$sid"
    [ -f "$record" ] || return 0
    IFS= read -r flag 2>/dev/null <"$record" || true
    [ "$flag" = 1 ] || return 0
    config="${CODEX_HOME:-${HOME:-}/.codex}/config.toml"
    if [ -n "$tap_root" ] && [ "$config" -nt "$record" ]; then
        # Codex's config moved (a switch, an approval): is Probe still on?
        cache="${tap_root%/*/*}"
        if probe_hooks_any_enabled "${cache##*/}" "$config"; then
            printf '1\n' >"$record" 2>/dev/null || true
        else
            printf '0\n' >"$record" 2>/dev/null || true
            return 0
        fi
    fi
    IFS= read -r shown 2>/dev/null <"$record.shown" || true
    case "$shown" in '' | *[!0-9]*) shown=0 ;; esac
    [ "$shown" -lt "$NUDGE_PROMPTS" ] || return 0
    printf '%s\n' "$((shown + 1))" >"$record.shown" 2>/dev/null || true
    printf '%s\n' "$PROBE_HOOKS_NUDGE"
}
