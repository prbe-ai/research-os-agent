#!/usr/bin/env python3
"""Keep this session's status-line marker warm, from the authoritative answer.

`GET /v1/sessions/{id}/work` knows what a coding-agent session has put in Probe,
because every client write stamps its session id on a header. This hook caches
that answer into the marker file so the status line can render it with no network
and no credential on the render path.

WHY A SERVER READ RATHER THAN INSTRUMENTING THE WRITE PATHS. A session's work can
be created through the SDK in a training process, the `probe` CLI in a shell, the
hosted MCP (which runs no code on this machine at all), or a script three
processes deep. Only some of those are ours to hook, and a status line that says
"untracked" because it missed one is worse than no status line. One read covers
every path by construction.

CONTRACT — this is a hook, so it inherits the house rules:

  * NEVER BLOCKS. The observing process throttles, then hands the fetch to a
    DETACHED child (`start_new_session`, as telemetry.py's sender does) and
    returns. A slow or unreachable API costs the tool call nothing.
  * SILENT ALWAYS. Exit 0 whatever happens; print nothing on stdout, which for a
    PostToolUse hook is a channel that can alter the session.
  * NOT TELEMETRY, so deliberately NOT gated on `PROBE_TELEMETRY`. That
    killswitch turns off analytics about the user; this is a feature the user
    asked for by installing the status line. `PROBE_STATUSLINE=off` disables it.
  * SELF-HOST SAFE. It talks to whatever base_url the client is configured with
    and to nothing else — there is no vendor endpoint involved, so the hosted-only
    gate that telemetry needs does not apply here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

#: Ceiling on how often the network is touched per session. The hook fires after
#: every matching tool call, and a session doing a hundred of them in a minute
#: needs one read, not a hundred. Small enough that a `probe run start` shows up
#: in the status line while the researcher is still looking at the terminal.
REFRESH_SECONDS = 5

#: The fetch is detached, so this bounds nothing user-visible; it exists so a
#: hung socket cannot leave a child alive for the rest of the session.
FETCH_TIMEOUT = 10


def _load(name: str):
    """A vendored sibling module by explicit path — see statusline.py's `_load`."""
    import importlib.util  # noqa: PLC0415

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Files `probe statusline install` copies out of this directory, and that this
#: hook keeps current. See `probe.cli.statusline.RENDERER_FILES` -- the installer
#: side owns the list; this is its runtime twin.
RENDERER_FILES = ("statusline.py", "_session_marker.py")


def install_dir() -> str:
    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(claude, "probe-research-statusline")


def sync_renderer() -> None:
    """Refresh the installed renderer copies from this plugin. Never raises.

    The status line points at a stable directory rather than at this plugin,
    because the plugin's own path carries its version number and would break on
    every release. The cost of that choice is that the copy goes stale, so this
    closes the loop at the one moment it is free: SessionStart, right after an
    update would have landed.

    ONLY WHEN ALREADY INSTALLED. An absent directory means the user never ran
    `probe statusline install`, and a hook that created it would be installing a
    status line nobody asked for.
    """
    target = install_dir()
    if not os.path.isdir(target):
        return
    source = os.path.dirname(os.path.abspath(__file__))
    for name in RENDERER_FILES:
        src, dst = os.path.join(source, name), os.path.join(target, name)
        try:
            if not os.path.isfile(src):
                continue
            with open(src, "rb") as handle:
                new = handle.read()
            try:
                with open(dst, "rb") as handle:
                    if handle.read() == new:
                        continue
            except OSError:
                pass
            tmp = dst + "." + str(os.getpid()) + ".tmp"
            with open(tmp, "wb") as handle:
                handle.write(new)
            os.replace(tmp, dst)
        except OSError:
            continue


def installed() -> bool:
    """Whether `probe statusline install` was ever run. THE GATE FOR ALL OF THIS.

    Nothing here is worth a single request unless something will render the
    answer. Without this gate every plugin user paid three API calls per refresh
    for a segment they may never have opted into — and under Codex, which has no
    status-line surface at all, that spend could never buy anything: its plugin
    manifest declares skills, mcpServers and interface, and there is nowhere for
    a rendered line to go.

    The install directory is the right signal because `probe statusline install`
    is the only thing that creates it, and `uninstall` is the only thing that
    would have a reason to remove it. An opt-in feature should cost nothing at
    all to the people who did not opt in.

    The notify flag counts too: under Codex the segment can never render, so
    `install` opts into the on-change notice instead — and that notice reads the
    same marker, so it needs the same refresh behind it.
    """
    if os.path.isdir(install_dir()):
        return True
    try:
        return bool(_load("_session_marker").notify_enabled())
    except BaseException:  # noqa: BLE001 - a hook contracted to silence
        return False


def disabled() -> bool:
    return (os.environ.get("PROBE_STATUSLINE") or "").strip().lower() in {
        "off",
        "0",
        "false",
        "no",
        "disabled",
    }


def due(marker, session_id: str) -> bool:
    """Whether enough time has passed since the last refresh.

    Reads the marker's own timestamp rather than a side-car clock file: one
    fewer thing to keep in step, and a marker that was just written is by
    definition a fresh answer.
    """
    try:
        path = marker.marker_path(session_id)
        return (time.time() - os.path.getmtime(path)) >= REFRESH_SECONDS
    except OSError:
        return True  # no marker yet: the first refresh is always due


#: Agents whose session id the server records on `runs.foreign_keys`. Must track
#: `app.runs.agent_session.CAPTURED_AGENTS` and the SDK's capability table; an
#: agent named here that the server does not record simply returns no rows.
AGENT_KEYS = ("claude_code", "codex", "pi")


def _get(base_url: str, token: str, path: str, params: dict | None = None):
    """One authenticated GET, decoded. None on any failure — never raises."""
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "User-Agent": "probe-statusline-refresh",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def fetch_work(base_url: str, token: str, session_id: str) -> dict | None:
    """The session's projects and runs. None on failure."""
    payload = _get(
        base_url, token, "/v1/sessions/" + urllib.parse.quote(session_id, safe="") + "/work"
    )
    return payload if isinstance(payload, dict) else None


def fetch_active_run_ids(base_url: str, token: str, session_id: str, marker) -> list:
    """Run ids the SERVER considers live, for runs originating in this session.

    `?active=true` is the server's own verdict — status is running AND the newest
    substantive update or heartbeat is inside the liveness window — so it sees a
    run executing on a cluster, which the local lock scan structurally cannot.

    The `foreign_key` lookup keys on the ORIGINATING session
    (`<agent>_session_id`, written first-write-wins when the run is created), so
    this is precisely "runs this conversation opened".

    One request per captured agent, because a session id alone does not say which
    agent issued it. One cheap indexed lookup per captured agent, off the
    render path.
    """
    ids = []
    for agent in AGENT_KEYS:
        payload = _get(
            base_url,
            token,
            "/v1/runs",
            {"foreign_key": agent + "_session_id:" + session_id, "active": "true"},
        )
        ids.extend(marker.from_active_runs(payload))
    return ids


#: The `/tmp` filename prefix the tap's watcher files carry, per agent. A
#: MIRROR of `tap.config.watcher_prefix` and `probe.cli.capture_state`, kept by
#: hand because this file is a hook: it runs with no `probe` package and no
#: `tap` package on its path, so it cannot import either of them. The parity
#: test in `agent/tests/test_watcher_prefix_parity.py` is what keeps the copies
#: equal — the drift is otherwise silent, because a wrong prefix just never
#: finds a pid file and reads as "no daemon".
_WATCHER_PREFIX = {"codex": "prbe-codex-tap"}
_WATCHER_PREFIX_DEFAULT = "probe-research-tap"

#: The tap's durable state directory, per agent — the SECOND convention this
#: file mirrors by hand, for the same reason and under the same parity test.
#: Canonically `tap.config.plugin_dir()` (which reads the env name and the
#: state root off `tap.sources`) and `probe.cli.capabilities.tap_plugin_dir`.
#:
#: WHY THE STATE DIR AND NOT JUST THE PID FILE. A missing pid file has two
#: causes that are nothing alike: a daemon that died, and a tap that was never
#: installed. Reading only `/tmp` collapses them into `not started`, so the
#: segment on a tracking-only machine blames a crashed daemon that never
#: existed, and the one row in the spec's edge-case table that exists to say
#: "an explicit opt-out is never silently reversed" renders as a fault report.
_TAP_PLUGIN_NAME = "probe-research-tap"
#: The standalone Codex tap's directory, kept as a ONE-WAY fallback exactly as
#: `tap.config.plugin_dir()` keeps it: an existing install stays where its
#: state already is, a clean install gets the unified name.
_CODEX_LEGACY_PLUGIN_NAME = "prbe-codex-tap-plugin"
_TAP_PLUGIN_DIR_ENV = {
    "codex": "PRBE_CODEX_TAP_PLUGIN_DIR",
    "pi": "PROBE_PI_TAP_PLUGIN_DIR",
}
_TAP_PLUGIN_DIR_ENV_DEFAULT = "PROBE_RESEARCH_TAP_PLUGIN_DIR"


def _agent_source(env=None) -> str:
    """Which harness this session belongs to, by the hook's own evidence.

    `PROBE_AGENT` when the plugin exported one, else Codex's own thread id as
    the tell, else claude_code — the same default `tap.sources.DEFAULT_SOURCE_ID`
    and `tap.config.capture_source()` fall back to when nothing says otherwise.
    """
    active = os.environ if env is None else env
    return active.get("PROBE_AGENT") or ("codex" if active.get("CODEX_THREAD_ID") else "claude_code")


def _tap_plugin_dir(source: str, env=None) -> str:
    """Where the tap keeps its durable state for `source`. Never raises.

    A hand mirror of `tap.config.plugin_dir()`; an unrecognized source falls to
    claude_code's row, matching `probe.cli.capabilities.tap_plugin_dir`.
    """
    active = os.environ if env is None else env
    override = active.get(_TAP_PLUGIN_DIR_ENV.get(source, _TAP_PLUGIN_DIR_ENV_DEFAULT))
    if override:
        return override
    home = os.path.expanduser("~")
    if source == "codex":
        current = os.path.join(home, ".codex", "state", _TAP_PLUGIN_NAME)
        legacy = os.path.join(home, ".codex", "state", _CODEX_LEGACY_PLUGIN_NAME)
        if os.path.exists(legacy) and not os.path.exists(current):
            return legacy
        return current
    if source == "pi":
        return os.path.join(home, ".pi", "agent", "state", _TAP_PLUGIN_NAME)
    return os.path.join(home, ".claude", "plugins", _TAP_PLUGIN_NAME)


def _capture_reading(session_id: str) -> dict:
    """Is a transcript daemon live for this session, and if not, why? Stdlib only.

    COMPUTED HERE, IN THE DETACHED CHILD, AND NEVER IN THE RENDERER.
    `statusline.py` renders in ~26ms measured and its contract is no network, no
    `probe` subprocess, and nothing of ours imported but one vendored module by
    explicit path. Putting this in the marker means the segment gains a FIELD TO
    READ rather than a syscall to make, and the render path is untouched.

    THE GATE ORDER IS `probe.cli.capture_state.session_capture_state`'s, and the
    order is the meaning: installed, then not switched off, then running. Each
    answer is a different sentence for the reader — `not installed` is a
    decision of theirs being honoured, `killswitch` likewise, and only
    `not started` is a fault. Two implementations that ordered these
    differently would put two different words on the same machine.

    THE ONE DELIBERATE GAP is `halted` (the tap's 401 latch). Naming it means
    opening the tap's sqlite state db AND resolving the winning credential to
    fingerprint it, which is `capture_state._halted`'s job in a process that
    can import the tap. This hook runs every few seconds for the life of a
    session; a halted daemon reads as `not started` here, which is wrong about
    the cause but right about the fact, and `probe session status` and
    `probe doctor` both name it exactly.

    A pid that exists is taken at face value here, where `probe.cli.capture_state`
    additionally asks `ps` what the process IS. That check is a subprocess, and
    this runs every few seconds for the life of a session; the accident it
    guards against is pid reuse inside the refresh window, which costs one
    over-optimistic reading that the next refresh corrects.
    """
    source = _agent_source()

    plugin_dir = _tap_plugin_dir(source)
    if not os.path.exists(plugin_dir):
        return {"running": False, "reason": "not installed"}
    if os.path.exists(os.path.join(plugin_dir, ".disabled")):
        return {"running": False, "reason": "killswitch"}

    prefix = _WATCHER_PREFIX.get(source, _WATCHER_PREFIX_DEFAULT)
    path = os.path.join("/tmp", prefix + "-watcher-" + session_id + ".pid")
    try:
        with open(path, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
        if pid <= 0:
            return {"running": False, "reason": "not started"}
        os.kill(pid, 0)
    except (OSError, ValueError):
        return {"running": False, "reason": "not started"}
    return {"running": True, "reason": "running"}


def refresh(session_id: str) -> None:
    """Fetch and store. Runs in the DETACHED child, never in the hook."""
    core = _load("_telemetry_core")
    marker = _load("_session_marker")
    config = core.read_cli_config()
    token = core.effective_token(config)
    if not token:
        return
    base_url = core.effective_base_url(config)
    work = fetch_work(base_url, token, session_id)
    if work is None:
        # Offline, or a token that cannot read this session. Leave the previous
        # marker in place: a stale project name is a better answer than
        # flickering to "untracked" every time the wifi drops.
        return
    state = marker.from_session_work(work)
    # Liveness is a SEPARATE failure domain from identity. If this call fails we
    # still store the project — the segment simply falls back to the local locks
    # for the accent, which is exactly what the fast path is for.
    state["active_run_ids"] = fetch_active_run_ids(base_url, token, session_id, marker)
    # Local, and stored alongside the server's answer so the segment renders both
    # halves of one state from one file. Written LAST so a reading is never
    # published without the identity it qualifies: `no capture` on a segment that
    # does not yet name a project is a warning about nothing.
    state["capture"] = _capture_reading(session_id)
    marker.write(session_id, state)


def spawn(session_id: str) -> None:
    """Hand the fetch to a detached copy of this script and return at once."""
    try:
        subprocess.Popen(  # noqa: S603 - sys.executable + this file
            [sys.executable, os.path.abspath(__file__), "fetch", session_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Detached child: do the work, then get out. Re-checks the gate because it
    # was spawned some milliseconds ago and an uninstall in between should stop
    # the request rather than let one last one through.
    if argv[:1] == ["fetch"]:
        session_id = argv[1] if len(argv) > 1 else ""
        if session_id and not disabled() and installed():
            try:
                refresh(session_id)
            except BaseException:  # noqa: BLE001 - detached and silent by contract
                pass
        return 0

    # Hook: gate, parse, throttle, spawn. The gate comes FIRST — it is one stat,
    # and it is the common case for anyone who never installed the status line,
    # so it should cost them nothing beyond that.
    if disabled() or not installed():
        return 0
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    try:
        marker = _load("_session_marker")
        if not marker.valid_session_id(session_id):
            return 0

        # MAINTENANCE FIRST, and specifically BEFORE the tracking-off gate.
        # Refreshing the installed renderer is housekeeping, not tracking, and a
        # session that turned tracking OFF needs it MORE than one that did not:
        # gating it behind the switch meant such a session never picked up a new
        # renderer, so it could not learn to display `tracking off` and showed a
        # stale, wrong state forever. Pruning is the same shape of chore.
        if payload.get("hook_event_name") == "SessionStart":
            marker.prune()
            sync_renderer()

        # Not tracking: stop spending REQUESTS on it. This asks the SAME resolver
        # the segment renders from, rather than only checking for an explicit
        # `off` -- otherwise a machine whose default is off would keep paying
        # three API calls per refresh for a segment that can never read as
        # tracking. The two must agree or one of them is lying.
        signal = marker.tracking_signal(session_id)
        if signal is not None:
            tracking = marker.is_tracking(signal)
        else:
            cwd = payload.get("cwd")
            cwd = cwd if isinstance(cwd, str) and cwd else None
            tracking, _source = marker.resolve_tracking_default(cwd)
        if not tracking:
            return 0
        if payload.get("hook_event_name") != "SessionStart" and not due(marker, session_id):
            return 0
    except BaseException:  # noqa: BLE001 - a hook contracted to silence
        return 0

    spawn(session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
