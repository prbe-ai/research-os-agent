# Sourced by session-start.sh and session-end.sh: which python runs the tap.
#
# The tap needs Python 3.11 or newer (enum.StrEnum, imported by `tap owner`,
# `tap record`, `tap watch`, `tap finish` and `tap start`; tap/start.py
# MIN_PYTHON). The hooks used to run it on the first `python3` on PATH, and on a
# Mac that is Apple's 3.9 until something newer is installed. Every `-m tap`
# then died on an ImportError the hooks swallow: no session was uploaded and no
# daemon started, while the Probe plugin's own hooks (fine on 3.9) kept working
# and `probe doctor` said "capturing". A team recorded no session at all for its
# first three days that way (2026-10-20..23).
#
# The hooks still parse their payloads with plain `python3` (any 3.x does that).
# Only the tap runs on what `tap_python` finds, the first of these that works:
#   1. the plugin's own .venv (a developer checkout), as before
#   2. `python3` on PATH, as before
#   3. the Probe CLI's interpreter: the one the service's runtime.json names
#      (the CLI writes it at every start), else where `uv tool install
#      probe-research` puts it. The Probe service already runs the tap there.
#   4. python3.14 .. python3.11 on PATH
# Each candidate costs one `python -S` start (~10 ms); a machine whose python3
# is new enough pays one. SessionStart remembers its answer in the tap's state
# folder (TAP_PYTHON_CACHE), so SessionEnd, inside Claude Code's 1.5 s exit
# budget, starts no python to ask again.
#
# None of them: session-start.sh says so to the researcher and leaves
# TAP_PYTHON_MISSING in the tap's state folder for `probe doctor` to name.

#: Beside `.disabled` in the tap's state folder (`probe doctor` reads both).
TAP_PYTHON_MISSING=".python-missing"
#: The interpreter the last SessionStart chose, for SessionEnd. In the state
#: folder (the researcher's own), never /tmp: SessionEnd runs what it names.
TAP_PYTHON_CACHE=".python"

# 0 when "$1" is an interpreter the tap runs on.
tap_python_ok() {
    [ -n "$1" ] && [ -x "$1" ] || return 1
    "$1" -S -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        </dev/null >/dev/null 2>&1
}

# A path as this bash runs it: Git Bash takes /c/... (cygpath), others as given.
tap_bash_path() {
    case "${OSTYPE:-}" in
        msys* | cygwin*) cygpath -u "$1" 2>/dev/null || printf '%s\n' "$1" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# The Probe CLI's interpreters, one per line: runtime.json's, then uv's. The
# same two places, per platform, as tap/service/paths.py (runtime_path,
# fallback_python). runtime.json is one line of `json.dump`: a Windows path
# comes with each backslash doubled, and a path with any other escape (é)
# is skipped (the uv path still covers that install).
tap_cli_pythons() {
    local runtime uv_python text found
    local pattern='"python"[[:space:]]*:[[:space:]]*"(([^"\]|\\\\)+)"'
    case "${OSTYPE:-}" in
        msys* | cygwin*)
            runtime="${LOCALAPPDATA:-${HOME:-}/AppData/Local}/probe/service/runtime.json"
            uv_python="${APPDATA:-${HOME:-}/AppData/Roaming}/uv/tools/probe-research/Scripts/python.exe"
            ;;
        *)
            runtime="${HOME:-}/.local/state/probe/service/runtime.json"
            uv_python="${HOME:-}/.local/share/uv/tools/probe-research/bin/python"
            ;;
    esac
    runtime=$(tap_bash_path "$runtime")
    if [ -r "$runtime" ]; then
        text=$(cat "$runtime" 2>/dev/null || true)
        if [[ "$text" =~ $pattern ]]; then
            found="${BASH_REMATCH[1]}"
            tap_bash_path "${found//\\\\/\\}"
        fi
    fi
    tap_bash_path "$uv_python"
}

# "$1" made absolute against this folder, symlinks kept (a venv's python is a
# symlink, and resolving it would lose the venv). `command -v` answers a
# relative path for a relative PATH entry (`.venv/bin`), which means another
# interpreter, or none, from any other folder.
tap_abs() {
    case "$1" in
        /* | [A-Za-z]:[\\/]*) printf '%s\n' "$1" ;;
        *) printf '%s/%s\n' "$PWD" "$1" ;;
    esac
}

# Prints the interpreter the tap runs on, as an absolute path, and returns 0;
# prints nothing and returns 1 when this machine has none. $1: the plugin root.
tap_python() {
    local candidate name
    for candidate in "${1:-}/.venv/bin/python3" "$(command -v python3 2>/dev/null || true)"; do
        if [ -n "$candidate" ] && tap_python_ok "$candidate"; then
            tap_abs "$candidate"
            return 0
        fi
    done
    while IFS= read -r candidate; do
        if tap_python_ok "$candidate"; then
            tap_abs "$candidate"
            return 0
        fi
    done < <(tap_cli_pythons)
    for name in python3.14 python3.13 python3.12 python3.11; do
        candidate=$(command -v "$name" 2>/dev/null) || continue
        if tap_python_ok "$candidate"; then
            tap_abs "$candidate"
            return 0
        fi
    done
    return 1
}

# The interpreter SessionStart chose ($1: the tap's state folder), when it still
# runs; nothing otherwise. A file read, no python start.
tap_python_cached() {
    local cached=""
    [ -r "${1:-}/$TAP_PYTHON_CACHE" ] || return 1
    IFS= read -r cached <"$1/$TAP_PYTHON_CACHE" 2>/dev/null || true
    case "$cached" in /* | [A-Za-z]:[\\/]*) ;; *) return 1 ;; esac  # written absolute
    [ -x "$cached" ] || return 1
    printf '%s\n' "$cached"
}

# The tap's state folder for Claude Code or Codex, as session-start.sh and
# tap/config.py plugin_dir() find it ($1: the source). Any other harness's comes
# from its registry row (`tap hook-env`'s TAP_PLUGIN_DIR).
tap_state_dir() {
    if [ "${1:-}" = codex ]; then
        if [ -n "${PRBE_CODEX_TAP_PLUGIN_DIR:-}" ]; then
            printf '%s\n' "$PRBE_CODEX_TAP_PLUGIN_DIR"
        elif [ -d "$HOME/.codex/state/prbe-codex-tap-plugin" ] && [ ! -e "$HOME/.codex/state/probe-research-tap" ]; then
            printf '%s\n' "$HOME/.codex/state/prbe-codex-tap-plugin"
        else
            printf '%s\n' "$HOME/.codex/state/probe-research-tap"
        fi
    else
        printf '%s\n' "${PROBE_RESEARCH_TAP_PLUGIN_DIR:-$HOME/.claude/plugins/probe-research-tap}"
    fi
}

# What `python3` on PATH is, for the researcher: "python3 3.9.6", or "no python3".
tap_python_seen() {
    local found version
    found=$(command -v python3 2>/dev/null) || {
        printf 'no python3\n'
        return 0
    }
    version=$("$found" -S -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' \
        </dev/null 2>/dev/null || true)
    case "$version" in
        '' | *[!0-9.]*) printf 'a python3 that does not run\n' ;;
        *) printf 'python3 %s\n' "$version" ;;
    esac
}
