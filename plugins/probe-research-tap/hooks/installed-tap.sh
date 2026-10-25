# Sourced FIRST by every tap hook script (session-start.sh, ensure-daemon.sh,
# session-end.sh, turn-end.sh): run the INSTALLED tap's copy of the script
# instead of this one, so a tap fix reaches a session that is already open at
# its next event, with no restart and nothing for the person to do.
#
# Why here and not in hooks.json: a session keeps the hooks.json it started
# with, and each entry runs `<the folder the session started with>/hooks/<script>`
# (`${CLAUDE_PLUGIN_ROOT}`, Codex's `$PLUGIN_ROOT`, Kimi's `$KIMI_PLUGIN_ROOT`).
# The entries never change: Codex trusts each one by a hash of the whole entry
# and silently skips a changed one until the person approves it again in
# `/hooks` (tests/test_installed_copy.py pins them). So the script an entry
# runs is where a session moves forward: this file, in the folder the session
# started with, hands the event to the installed copy's same script, whose own
# code then runs it. The Probe plugin's hooks do the same in python
# (`hooks/dispatch.py` `hand_over`, `installed_root`); these rules are its rules:
#
#   Claude Code  the one folder Claude Code's install ledger
#                (`installed_plugins.json`) keeps under this plugin's
#                `<plugin>@<marketplace>`, when every entry there is user scope,
#                it is another version folder beside this one and it is whole.
#                Both ways: a release rolled back stops running in open sessions
#                too.
#   Codex        the newest version folder beside this one (by version number)
#                inside Codex's plugin cache, newer than this one, carrying
#                Codex's manifest, whole. (Codex replaces the version folder on
#                an update: the entries' own fallback already runs the newest
#                one when the start folder is gone.)
#   Kimi Code    the folder Kimi's plugin list (`plugins/installed.json`, beside
#                `plugins/managed/`) records for this plugin, when it is another
#                folder of this plugin in `managed/`, carrying Kimi's manifest,
#                whole. A version folder Probe pruned is a link to the kept one
#                (`probe.cli.kimi_config._prune_versions`).
#
# Anything else, or anything unexpected (no ledger, two copies listed, a link
# out of the cache, a half-written copy, Windows paths): the event runs here, as
# before. At most one move per event (PROBE_TAP_FOLLOWED, taken out of the
# environment by the copy that receives it, so nothing it starts inherits it).
#
# Cost: no program is run to decide. The list is read in one subshell
# (`$(<file)`, a fork of this bash), and searched with regular expressions
# only: the usual answer (this IS the installed copy) is one regex match over
# the list and a few small ones over this plugin's own entry; a move is one
# `exec`. Measured under bash 3.2.57 (macOS's /bin/bash), each real hook through
# its hooks.json entry against the same hook without the hand-over: +3 ms at an
# 8 KB ledger (20 plugins), +5 ms at 37 KB (100 plugins); a move adds one bash
# start (3-5 ms more). Codex's newest-folder pick runs `date` and `stat` once,
# only when a newer folder is there.
#
# bash 3.2 (macOS /bin/bash): no `read -N`, no associative arrays, no `${x,,}`,
# and NEVER a pattern operator (`${x//p/}`, `${x#*p}`) or a `case` glob over the
# whole list: bash 3.2's pattern matching grows much faster than the text (an
# 8 KB ledger took 1.8 s per event, a 36 KB one past every hook's timeout).
# `read` is no better there (one system call per byte). Regular expressions run
# in the C library, in linear time. tests/test_installed_copy.py pins all this.
# Fail-open: nothing here exits or returns non-zero.

#: Set while an event moves to the installed copy (to the folder it moved from).
TAP_FOLLOWED_ENV=PROBE_TAP_FOLLOWED
#: A ledger or plugin list bigger than this is not read (each is ~2 KB).
TAP_LIST_MAX=262144
#: The copy to move to, once `tap_installed_*` found one.
TAP_TARGET=""
#: A Codex version folder younger than this (its top folders' times) may still
#: be being written by Codex: it takes no event yet.
TAP_SETTLE_S=5
#: What the installed copy must hold before an event moves to it, besides the
#: script itself: every file a tap hook script sources or runs, and the tap
#: modules the hooks and the service start from. Never remove one of these
#: from the tap: an older tap would stop handing over to it (a stub will do;
#: tests/test_installed_copy.py freezes each release's list). Not this file:
#: tap 0.10.17 has none, and every script sources it behind `[ -f ]`, so an
#: open session follows a rollback to 0.10.17 too.
TAP_WHOLE_FILES="hooks/tap-python.sh hooks/probe-hooks-trust.sh hooks/session-start.sh tap/__init__.py tap/__main__.py tap/main.py tap/hosted.py"

# "$1" is a version folder's name (digits and dots, at least one dot).
tap_is_version() {
    case "$1" in '' | .* | *. | *..* | *[!0-9.]*) return 1 ;; esac
    case "$1" in *.*) return 0 ;; esac
    return 1
}

# 0 when version "$1" is newer than version "$2" (numbers, not text: 0.10.0 is
# newer than 0.9.9).
tap_version_newer() {
    local a="$1" b="$2" x y
    while [ -n "$a" ] || [ -n "$b" ]; do
        x="${a%%.*}"
        y="${b%%.*}"
        [ -n "$x" ] || x=0
        [ -n "$y" ] || y=0
        # Leading zeros are not octal here.
        x=$((10#$x))
        y=$((10#$y))
        [ "$x" -gt "$y" ] && return 0
        [ "$x" -lt "$y" ] && return 1
        if [ "$a" = "${a#*.}" ]; then a=""; else a="${a#*.}"; fi
        if [ "$b" = "${b#*.}" ]; then b=""; else b="${b#*.}"; fi
    done
    return 1
}

# A whole tap at "$1" for script "$2" and manifest folder "$3": the script it
# runs, every file in TAP_WHOLE_FILES, the harness's manifest. A copy still
# being written (or half deleted) never takes an event.
tap_whole() {
    local file
    [ -x "$1/hooks/$2" ] && [ -x "$1/hooks/session-start.sh" ] && [ -f "$1/$3/plugin.json" ] || return 1
    for file in $TAP_WHOLE_FILES; do
        [ -f "$1/$file" ] || return 1
    done
    return 0
}

# 0 when the folder "$1" and its hooks/ and tap/ were last written at least
# TAP_SETTLE_S ago (GNU `stat -c`, else BSD `stat -f`; anything unreadable is
# not settled). Asked only of a Codex folder newer than the session's.
tap_settled() {
    local now times when
    now=$(date +%s 2>/dev/null) || return 1
    times=$(stat -c %Y "$1" "$1/hooks" "$1/tap" 2>/dev/null || stat -f %m "$1" "$1/hooks" "$1/tap" 2>/dev/null) || return 1
    case "$now" in '' | *[!0-9]*) return 1 ;; esac
    [ -n "$times" ] || return 1
    for when in $times; do
        case "$when" in '' | *[!0-9]*) return 1 ;; esac
        [ $((now - when)) -ge "$TAP_SETTLE_S" ] || return 1
    done
    return 0
}

# "$1" as a POSIX extended regular expression that matches it literally, into
# TAP_ERE. Character by character on a short path: no pattern operator.
tap_ere() {
    local text="$1" char i=0
    TAP_ERE=""
    while [ "$i" -lt "${#text}" ]; do
        char="${text:$i:1}"
        case "$char" in
            '.' | '[' | '(' | ')' | '*' | '+' | '?' | '{' | '|' | '^' | '$' | '\') TAP_ERE="$TAP_ERE\\$char" ;;
            *) TAP_ERE="$TAP_ERE$char" ;;
        esac
        i=$((i + 1))
    done
}

# The text of regular file "$1" into TAP_TEXT, or 1 (none, or bigger than
# TAP_LIST_MAX). One subshell: bash 3.2's `read` makes a system call per byte
# (26 ms for a 36 KB ledger), `$(<file)` reads it whole (1 ms). Unquoted, and
# no `${#TAP_TEXT}` or `[ -n "$TAP_TEXT" ]`: bash 3.2 copies the text for each
# (2-3 ms more at 36 KB); one character past the bound says it is too big.
tap_read_list() {
    TAP_TEXT=""
    { [ -f "$1" ] && [ -r "$1" ]; } || return 1
    TAP_TEXT=$(<"$1")
    [ -z "${TAP_TEXT:$TAP_LIST_MAX:1}" ] || { TAP_TEXT=""; return 1; }
}

# Claude Code: TAP_TARGET from the install ledger, for the copy at "$1"
# (`<cache>/<marketplace>/<plugin>/<version>`), script "$2".
tap_installed_claude() {
    local root="$1" script="$2" parent market key ledger base re block scan path=""
    local rest objects scopes users
    parent="${root%/*}"
    market="${parent%/*}"
    key="${parent##*/}@${market##*/}"
    case "$key" in *[!A-Za-z0-9._@-]*) return 0 ;; esac
    if [ -n "${CLAUDE_CODE_PLUGIN_CACHE_DIR:-}" ]; then
        base="${CLAUDE_CODE_PLUGIN_CACHE_DIR}"
        case "$base" in "~" | "~/"*) base="${HOME:-}${base#\~}" ;; esac
        ledger="${base%/}/installed_plugins.json"
    else
        base="${CLAUDE_CONFIG_DIR:-}"
        case "$base" in "~" | "~/"*) base="${HOME:-}${base#\~}" ;; esac
        [ -n "$base" ] || base="${HOME:-}/.claude"
        ledger="${base%/}/plugins/installed_plugins.json"
    fi
    tap_read_list "$ledger" || return 0
    # This plugin's entries first, by one regex over the ledger: `"<key>": [
    # {...}, ... ]` (no nested arrays). Everything else reads that block only.
    re="\"${key//./\\.}\"[[:space:]]*:[[:space:]]*\\[([^]]*)\\]"
    [[ $TAP_TEXT =~ $re ]] || return 0
    block="${BASH_REMATCH[1]}"
    TAP_TEXT=""
    # Escaped characters (a Windows path) are not read here: stay.
    case "$block" in *\\*) return 0 ;; esac
    # Exactly one install folder named, whatever how many entries name it; the
    # usual answer, this copy, ends it here.
    scan="$block"
    re='"installPath"[[:space:]]*:[[:space:]]*"([^"]*)"'
    while [[ $scan =~ $re ]]; do
        if [ -z "$path" ]; then
            path="${BASH_REMATCH[1]%/}"
        elif [ "${BASH_REMATCH[1]%/}" != "$path" ]; then
            return 0
        fi
        scan="${scan#*"${BASH_REMATCH[0]}"}"
    done
    [ -n "$path" ] && [ "$path" != "$root" ] && [ "${path%/*}" = "$parent" ] && [ ! -L "$path" ] || return 0
    # Every entry user scope: as many objects as `"scope": "user"`, and as many
    # `"scope"` keys (a project-scoped install is per project, which this does
    # not know).
    rest="${block//\{/}"
    objects=$((${#block} - ${#rest}))
    rest="${block//\"scope\"/}"
    scopes=$(((${#block} - ${#rest}) / 7))
    users=0
    scan="$block"
    re='"scope"[[:space:]]*:[[:space:]]*"user"'
    while [[ $scan =~ $re ]]; do
        users=$((users + 1))
        scan="${scan#*"${BASH_REMATCH[0]}"}"
    done
    { [ "$objects" -gt 0 ] && [ "$objects" = "$scopes" ] && [ "$scopes" = "$users" ]; } || return 0
    tap_whole "$path" "$script" .claude-plugin || return 0
    TAP_TARGET="$path"
}

# Codex: TAP_TARGET, the newest whole version folder beside "$1" in Codex's
# cache (`<cache>/<marketplace>/<plugin>/<version>`), newer than it.
tap_installed_codex() {
    local root="$1" script="$2" parent cache current best="" name folder
    parent="${root%/*}"
    cache="${CODEX_HOME:-}"
    case "$cache" in "~" | "~/"*) cache="${HOME:-}${cache#\~}" ;; esac
    [ -n "$cache" ] || cache="${HOME:-}/.codex"
    cache="${cache%/}/plugins/cache"
    [ "${parent%/*/*}" = "$cache" ] || return 0
    current="${root##*/}"
    tap_is_version "$current" || return 0
    best="$current"
    for folder in "$parent"/*; do
        name="${folder##*/}"
        tap_is_version "$name" || continue
        tap_version_newer "$name" "$best" || continue
        [ ! -L "$folder" ] || continue
        tap_whole "$folder" "$script" .codex-plugin || continue
        best="$name"
        TAP_TARGET="$folder"
    done
    # Codex picks are by listing, so the newest may be one Codex is writing
    # right now (it deletes the old folder when it is done): wait for it.
    if [ -n "$TAP_TARGET" ] && ! tap_settled "$TAP_TARGET"; then
        TAP_TARGET=""
    fi
    return 0
}

# Kimi Code: TAP_TARGET, the folder Kimi's plugin list records for this plugin
# (`<kimi>/plugins/managed/<id>@<version>`, or Kimi's own `managed/<id>`).
tap_installed_kimi() {
    local root="$1" script="$2" managed id one path
    managed="${root%/*}"
    [ "${managed##*/}" = managed ] || return 0
    id="${root##*/}"
    id="${id%%@*}"
    [ -n "$id" ] || return 0
    tap_read_list "${managed%/*}/installed.json" || return 0
    # The record's root for this plugin's folder: one regex over the list for
    # the first, one more for a second (`.*`: the last). Two that differ: stay.
    # Accepted: records [NEW, this, NEW] for one plugin read as NEW (first and
    # last agree; a middle one is not seen). Kimi's list keeps one record per
    # plugin, and Probe's install and Kimi's own each write one, so three never
    # happen; were they there, the folder named first and last is taken (the
    # Probe plugins' hooks, under dispatch.py's stricter one-root check, stay).
    tap_ere "$managed/$id"
    one="\"root\"[[:space:]]*:[[:space:]]*\"($TAP_ERE(@[^\"\\]*)?)/?\""
    if ! [[ $TAP_TEXT =~ $one ]]; then
        TAP_TEXT=""
        return 0
    fi
    path="${BASH_REMATCH[1]%/}"
    if [[ $TAP_TEXT =~ $one.*$one ]] && [ "${BASH_REMATCH[3]%/}" != "$path" ]; then
        TAP_TEXT=""
        return 0
    fi
    TAP_TEXT=""
    [ "$path" != "$root" ] && [ ! -L "$path" ] || return 0
    tap_whole "$path" "$script" .kimi-plugin || return 0
    TAP_TARGET="$path"
}

# `tap_run_installed "$0" "$@"`: exec the installed copy's same script, with
# the harness's root variable pointing at it; return to run here otherwise.
tap_run_installed() {
    local self="${1:-}" script hooks root var old
    if [ $# -gt 0 ]; then shift; fi
    if [ -n "${PROBE_TAP_FOLLOWED:-}" ]; then
        # Moved here already: run it, and nothing this starts inherits it.
        unset PROBE_TAP_FOLLOWED
        return 0
    fi
    case "${OSTYPE:-}" in msys* | cygwin* | win*) return 0 ;; esac
    case "$self" in /*) ;; *) return 0 ;; esac
    script="${self##*/}"
    hooks="${self%/*}"
    [ "${hooks##*/}" = hooks ] || return 0
    root="${hooks%/*}"
    [ -n "$root" ] || return 0
    TAP_TARGET=""
    # Which harness runs the session, as dispatch.py reads it: Codex sets
    # PLUGIN_ROOT, Kimi KIMI_PLUGIN_ROOT, Claude Code CLAUDE_PLUGIN_ROOT.
    if [ -n "${PLUGIN_ROOT:-}" ]; then
        var=PLUGIN_ROOT
        tap_installed_codex "$root" "$script"
    elif [ -n "${KIMI_PLUGIN_ROOT:-}" ]; then
        var=KIMI_PLUGIN_ROOT
        tap_installed_kimi "$root" "$script"
    elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
        var=CLAUDE_PLUGIN_ROOT
        tap_installed_claude "$root" "$script"
    else
        return 0
    fi
    TAP_TEXT=""
    [ -n "$TAP_TARGET" ] || return 0
    old="${!var:-}"
    export "$var=$TAP_TARGET"
    export PROBE_TAP_FOLLOWED="$root"
    # A copy that cannot be run after all (gone since the check): here.
    shopt -s execfail
    exec "$TAP_TARGET/hooks/$script" "$@"
    shopt -u execfail
    export "$var=$old"
    unset PROBE_TAP_FOLLOWED
    TAP_TARGET=""
    return 0
}
:
