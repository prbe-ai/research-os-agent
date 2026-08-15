"""What this coding-agent session has put in Probe, answerable without a network call.

The backend already knows: every write carries the session id (see
:mod:`probe.sdk.agent_session`), and ``GET /v1/sessions/{id}/work`` reads it back.
That answer is authoritative and costs 50-90ms. This module is the LOCAL CACHE of
it, because the consumer is a terminal status line that re-renders constantly and
must never touch the network, an auth token, or a `probe` process.

    ~/.local/state/probe/sessions/<session_id>.json

The division of labour is deliberate:

  a plugin hook REFRESHES this file in the background, from the server
  a status-line script READS it, in stdlib python, in about 30ms

Refreshing from the server rather than instrumenting the SDK's create paths is
what makes the answer complete. A session's work can be created through the SDK,
the CLI, the hosted MCP, or a training script three processes deep; only one of
those runs code we could have hooked, and a status line that says "untracked"
because it missed a path is worse than no status line.

STALENESS IS THE ACCEPTED COST, and it is bounded by how often the refresher
runs, not by anything here. `updated_at` is carried so a reader can tell a fresh
answer from an abandoned one.

STDLIB ONLY, AND PYTHON 3.9. This file is vendored into the plugin's hooks
directory, where it executes under the SYSTEM python3 with no `probe` package
importable — macOS still ships 3.9, so nothing newer than that may appear here.
Two copies exist on purpose and must stay byte-identical:

    src/probe/sdk/session_marker.py                     (canonical — edit here)
    plugins/probe-research/hooks/_session_marker.py     (vendored — never edit)

`make sync-session-marker` refreshes the copy, and tests/test_session_marker_parity.py
fails CI whenever the two differ.

FAIL-SOFT THROUGHOUT. Nothing here may raise into a caller: the writer runs beside
a research write that must survive a full disk, and the reader runs on a render
path where an exception is a broken prompt.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

#: Mirrors probe.version_policy.STATE_DIRNAME. Duplicated rather than imported
#: because the vendored copy has no `probe` package to import from;
#: tests/test_session_marker.py asserts the two resolve to the same directory.
STATE_DIRNAME = "probe"
SESSIONS_DIRNAME = "sessions"

#: A marker older than this is not shown at all. A status line asserting a
#: project from a conversation someone abandoned last month is a lie with a
#: confident glyph on it; saying nothing is the honest answer.
MAX_AGE_SECONDS = 30 * 86400

_ELLIPSIS = "…"  # …
#: ONE glyph for both states, and it is FILLED in both. A hollow ring is faint
#: at terminal font sizes and reads as a rendering artefact rather than a mark.
#: Nothing is lost by using the same glyph twice: the state is carried by the
#: WORD ("untracked" / "tracked →"), which is why the dot could be spared the
#: job in the first place. Colour distinguishes them for a quick glance; the
#: word distinguishes them when colour is off, absent, or unseeable.
_DOT = "●"  # ●
_ARROW = "→"  # →
_SEPARATOR = "·"  # ·

_INDENT = "  "
_GLYPH_WIDTH = 2  # the dot plus its trailing space

#: The state is spelled OUT, not encoded in the glyph. `● folding` requires the
#: reader to already know that a filled dot means tracked; `tracked → folding`
#: does not, and a status line is read by people who did not install it.
_LABEL_TRACKED = "tracked " + _ARROW + " "
_LABEL_UNTRACKED = "untracked"
_LABEL_OFF = "tracking off"

#: A middle dot, NOT a second arrow. `tracked → folding ▸ running` puts two
#: arrow-shaped glyphs in one short segment, and the eye reads them as a
#: sequence of three things rather than as a name with a state hung off it.
_ACCENT_TEXT = " " + _SEPARATOR + " running"

#: Hard ceiling on the rendered segment, leading indent included, counted in
#: VISIBLE characters. The cap exists because the status line must not WRAP: a
#: wrapped line reflows every other segment sharing it, which is the one failure
#: that makes neighbouring output less readable rather than merely longer.
#:
#: 50 is measured, not picked. It is the smallest ceiling at which a 26-character
#: name budget survives, and 26 is what this lab's project names actually need:
#: median 18, longest 29, 27 of 28 whole. Spelling out "tracked → " cost ten
#: columns, and they were taken from the ceiling rather than from the name --
#: paying for the label with the project's own name would have defeated the
#: point of naming it. `test_this_labs_project_names_mostly_fit_whole` pins it.
MAX_SEGMENT_CHARS = 50

#: What the name may occupy. DERIVED, and derived against the LIVE width even
#: when idle, so the name keeps one budget in both states: a name that shrank the
#: moment a run started -- and grew back when it ended -- would read as the status
#: line glitching rather than as the run changing.
MAX_SLUG_CHARS = (
    MAX_SEGMENT_CHARS - len(_INDENT) - _GLYPH_WIDTH - len(_LABEL_TRACKED) - len(_ACCENT_TEXT)
)

# Basic SGR codes, never 256-colour or truecolour: the status line is rendered
# inside the user's terminal theme, and a hardcoded hex that looks right on one
# background is unreadable on the other. These resolve against whatever palette
# they already chose. Claude Code additionally dims the whole line, so treat
# these as a hint of hue rather than as emphasis.
#
# Three, and each is a state someone can name: landing, not landing, and
# deliberately switched off. A fourth colour would be a state nobody defined.
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"

# Session ids are uuids today; the bound and charset are the real guard, since
# this value becomes a filename. Matches agent_session._SESSION_RE deliberately:
# a value that could never have been sent as a header must never mint a file.
_SESSION_RE = re.compile(r"\A[A-Za-z0-9._:-]{8,200}\Z")


def valid_session_id(raw: object) -> bool:
    """Whether a value is safe to use as a marker filename."""
    return isinstance(raw, str) and bool(_SESSION_RE.match(raw))


def state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / STATE_DIRNAME


def sessions_dir() -> Path:
    return state_dir() / SESSIONS_DIRNAME


def marker_path(session_id: str) -> Path:
    return sessions_dir() / (session_id + ".json")


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def read(session_id: str) -> dict | None:
    """This session's cached work, or None.

    None for absent, unreadable, malformed, and EXPIRED alike — every one of
    them means "we cannot say what this session tracked", and a reader that
    had to tell them apart would only be able to render the same nothing.
    """
    if not valid_session_id(session_id):
        return None
    try:
        with open(marker_path(session_id), encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    updated_at = state.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return None
    # A future timestamp (clock skew, a restored backup) must not pin a marker
    # as fresh forever; treat anything outside the window as expired.
    if abs(time.time() - updated_at) > MAX_AGE_SECONDS:
        return None
    return state


def write(session_id: str, state: dict) -> bool:
    """Replace this session's marker. True when it landed.

    Atomic, and stamped here rather than by the caller so `updated_at` always
    means "when this file was written" and cannot be back-dated by a stale
    refresher losing a race with a newer one.
    """
    if not valid_session_id(session_id):
        return False
    record = dict(state)
    record["updated_at"] = time.time()
    path = marker_path(session_id)
    tmp = path.parent / (path.name + "." + str(os.getpid()) + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def from_session_work(payload: dict) -> dict:
    """The marker shape, built from a ``GET /v1/sessions/{id}/work`` body.

    Keeps only what the segment can render: the project this session touched
    and whether a run of its is live. The response is another service's schema
    and is treated as untrusted — every field is checked, and a shape we do not
    recognise degrades to "tracked nothing" rather than raising on a render path.

    WHICH PROJECT, when a session touched several: the LAST one, because the
    list arrives oldest-first and the thing a status line should name is what
    you are working on now, not what you opened the session with.
    """
    project = None
    projects = payload.get("projects")
    if isinstance(projects, list):
        for row in projects:
            if not isinstance(row, dict):
                continue
            slug = row.get("slug") or row.get("name")
            if isinstance(slug, str) and slug:
                project = slug

    # The work read carries no run STATUS, only identity. The ids are kept so the
    # renderer can intersect them with the run locks this box holds — the local
    # fast path in `is_live`, which is what makes "running" mean THIS session's
    # run rather than any run on the machine.
    run_ids = []
    runs = payload.get("runs")
    if isinstance(runs, list):
        for row in runs:
            if not isinstance(row, dict):
                continue
            entity_id = row.get("entity_id")
            if isinstance(entity_id, str) and entity_id:
                run_ids.append(entity_id)

    return {"project": project, "run_ids": run_ids}


def from_active_runs(payload: object) -> list[str]:
    """Active run ids, from a ``GET /v1/runs?...&active=true`` body.

    The server's own liveness verdict: stored status is `running` AND the newest
    substantive update or heartbeat is inside the liveness window. Machine-
    independent, which is the whole reason it is worth a second request — a run
    executing on a cluster holds its lock on THAT box and is invisible to the
    local scan.
    """
    if not isinstance(payload, list):
        return []
    ids = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        run_id = row.get("id")
        if isinstance(run_id, str) and run_id:
            ids.append(run_id)
    return ids


# ---------------------------------------------------------------------------
# Liveness: which of this session's runs are executing right now
# ---------------------------------------------------------------------------


def _fcntl():
    """The fcntl module, or None on a platform without it (Windows)."""
    try:
        import fcntl  # noqa: PLC0415

        return fcntl
    except ImportError:
        return None


def _lease_run_id(path: Path, now: float) -> str | None:
    """The run a `.lease` entry names, if the lease has not expired.

    Malformed or unreadable reads as NOT live, which INVERTS
    `probe.cli.run_lock`. That module fails closed because applying an
    auto-update into a live run costs somebody's afternoon. Here the cost is
    reversed: the only thing downstream is a word on a status line, and
    printing "running" when nothing is running is a confident lie. Uncertainty
    should say less, not more.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if float(data.get("expires_at", 0)) <= now:
            return None
    except (TypeError, ValueError):
        return None
    run = data.get("run")
    return run if isinstance(run, str) and run else None


def _flock_run_id(path: Path) -> str | None:
    """The run a `.flock` entry names, if somebody still holds the lock.

    READ-ONLY, unlike `run_lock._flock_is_held`, which deletes the entry when it
    finds it unheld. That cleanup is right on a command path that runs
    occasionally and wrong here: this executes on every status-line render, so
    it would race a starting run's own acquire and delete state it does not own.
    Probing takes the lock non-blocking and releases immediately; a second
    `open()` is a new open file description, so this reads correctly even when
    the holder is this process.
    """
    module = _fcntl()
    if module is None:
        return None  # cannot prove liveness on this platform: say nothing
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    run = data.get("run")
    if not (isinstance(run, str) and run):
        return None
    try:
        handle = open(path, "a+")  # noqa: SIM115 -- closed below
    except OSError:
        return None
    try:
        module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)
    except BlockingIOError:
        return run  # somebody holds it: a live run
    except (OSError, ValueError):
        return None
    else:
        try:
            module.flock(handle.fileno(), module.LOCK_UN)
        except (OSError, ValueError):
            pass
        return None  # nobody held it; leave the leftover for run_lock to reap
    finally:
        handle.close()


def live_run_ids() -> set[str]:
    """Run ids currently executing on THIS machine, from the run-lock directory.

    Bounded and non-mutating. `probe.cli.run_lock` owns the format and the
    cleanup; this only looks.
    """
    live = set()
    try:
        directory = state_dir() / "runs"
        entries = sorted(directory.iterdir())
    except OSError:
        return live
    now = time.time()
    for entry in entries[:512]:  # sanity bound, mirroring run_lock.MAX_SCAN_ENTRIES
        try:
            if entry.suffix == ".lease":
                found = _lease_run_id(entry, now)
            elif entry.suffix == ".flock":
                found = _flock_run_id(entry)
            else:
                continue
        except OSError:
            continue
        if found:
            live.add(found)
    return live


def is_live(state: dict | None) -> bool:
    """Whether a run this session opened is executing. TWO SOURCES, OR'd.

    **The server is the source of truth.** `active_run_ids` is what
    `GET /v1/runs?foreign_key=<agent>_session_id:<id>&active=true` said, and it is
    the only one of the two that can see a run executing on a CLUSTER: that run
    holds its lock on the machine running it, not on this laptop. Reading only
    local locks made a remote training job — much of what this team actually runs
    — indistinguishable from no run at all.

    **The local locks are a fast path, not a fallback.** They are ground truth for
    a local process (the kernel releases an flock on SIGKILL and OOM, which no
    heartbeat can promise) and they are current between refreshes, so a run
    started seconds ago shows before the next fetch lands. The intersection with
    `run_ids` is what keeps that honest: "a run is live on this box" is
    `run_lock`'s question and would light up for a colleague's sweep in another
    terminal.

    OR rather than AND because the two see different things and neither is
    complete. Both false is the only "not running".
    """
    if not isinstance(state, dict):
        return False

    active = state.get("active_run_ids")
    if isinstance(active, list) and any(isinstance(run_id, str) and run_id for run_id in active):
        return True

    run_ids = state.get("run_ids")
    if not isinstance(run_ids, list) or not run_ids:
        return False
    return bool(set(run_ids) & live_run_ids())


def prune(max_age_seconds: float = MAX_AGE_SECONDS) -> None:
    """Drop markers for sessions long finished. Never raises.

    os.scandir rather than a `find` shell-out, for the reason the telemetry
    hook's prune already documents: `find /tmp` on macOS matches the symlink
    and silently descends into nothing.
    """
    try:
        cutoff = time.time() - max_age_seconds
        with os.scandir(sessions_dir()) as entries:
            for entry in entries:
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    continue
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Is Probe configured on this machine at all
# ---------------------------------------------------------------------------


def config_path() -> Path:
    """Mirrors `probe.sdk.config` and `_telemetry_core._config_path`."""
    override = os.environ.get("PROBE_CONFIG_PATH")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "probe" / "config.json"


def tracking_off_path(session_id: str) -> Path:
    """Where "this conversation is not research" is recorded, per session.

    A DECLARATION, unlike everything else in this module, which is observation.
    The rest answers "did work land"; this answers "should any". They are
    different facts and a session can hold both -- someone can turn tracking off
    in a conversation that already created a project, and the honest reading of
    that is "no more", not "none ever".

    Per session, beside the marker, because the answer is about THIS
    conversation. A machine-wide off switch would silence a researcher's next
    session too, which is precisely the surprise nobody wants from a mute button.
    """
    return sessions_dir() / (session_id + ".off")


def tracking_off(session_id: str) -> bool:
    """Whether this session was explicitly taken out of tracking."""
    return valid_session_id(session_id) and tracking_off_path(session_id).is_file()


def set_tracking_off(session_id: str, off: bool) -> bool:
    """Turn tracking off (or back on) for this session. True when it landed."""
    if not valid_session_id(session_id):
        return False
    path = tracking_off_path(session_id)
    try:
        if off:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ended by the researcher\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def notify_flag_path() -> Path:
    """Opt-in marker for the change NOTICE, the Codex-shaped half of this feature.

    Codex has no surface a computed segment can render into — its status line is a
    picker over built-in items — so the same information is delivered as a message
    when it CHANGES instead of as a line that is always there. Enabling that is
    still opt-in, and this file is the opt-in, exactly as the install directory is
    for the status line.

    In the probe state dir rather than under either agent's config: one flag, read
    by a hook that both agents load, and it outlives a reinstall of either.
    """
    return state_dir() / "statusline-notify"


def notify_enabled() -> bool:
    return notify_flag_path().is_file()


def state_key(state: dict | None, *, live: bool) -> str:
    """What "changed" MEANS, as one comparable string.

    Only the two things the notice actually reports. `updated_at` moves on every
    refresh and `run_ids` churns as runs come and go, so comparing whole markers
    would fire a notice several times a minute while nothing a reader cares about
    had changed.
    """
    project = state.get("project") if isinstance(state, dict) else None
    if not (isinstance(project, str) and project):
        return "untracked"
    return f"{project}|{'running' if live else 'idle'}"


def message(state: dict | None, *, live: bool = False) -> str:
    """One line for an agent with no status line to hang a segment on.

    Built from the same labels the segment uses, so the two surfaces cannot drift
    into describing the same state differently. No colour and no glyph: this is a
    sentence in a transcript, not a mark on a line.
    """
    project = state.get("project") if isinstance(state, dict) else None
    if not (isinstance(project, str) and project):
        return "Probe: this session is not tracked yet."
    text = "Probe: " + _LABEL_TRACKED + project
    if live:
        text += _ACCENT_TEXT
    return text


def read_notified(session_id: str) -> str | None:
    """The state key this session was last told about, or None."""
    if not valid_session_id(session_id):
        return None
    try:
        return (sessions_dir() / (session_id + ".notified")).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def write_notified(session_id: str, key: str) -> None:
    """Record what we just said. Never raises.

    Written AFTER the message is emitted rather than before: a crash between the
    two costs a repeated notice, which is a great deal better than a silent one
    the reader never sees.
    """
    if not valid_session_id(session_id):
        return
    path = sessions_dir() / (session_id + ".notified")
    tmp = path.parent / (path.name + "." + str(os.getpid()) + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(key, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def configured() -> bool:
    """Whether this machine has a credential for Probe. Cheap, and fail-soft.

    `_telemetry_core` answers the same question and is vendored alongside this
    file, so loading it would have been the obvious reuse -- and it was, until it
    was measured. It imports `urllib.request` for its own sending, which costs
    ~23ms of interpreter startup, on a path whose entire budget is one status-line
    render. So the question is re-answered here with `json` and `os` and nothing
    else. Reuse is the rule; a hot path is the exception that earns a duplicate,
    and this one is thirteen lines that mirror an interface, not logic.

    Both config shapes, for the reason the core documents: reading only the flat
    v1 shape silently missed every install the wizard had produced.
    """
    if os.environ.get("PROBE_TOKEN") or os.environ.get("PROBE_MCP_TOKEN"):
        return True
    try:
        with open(config_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    contexts = data.get("contexts")
    if isinstance(contexts, dict):
        active = contexts.get(data.get("current_context") or "default")
        data = active if isinstance(active, dict) else {}
    return bool(data.get("token") or data.get("mcp_token"))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _paint(text: str, code: str, color: bool) -> str:
    """`text` in `code`, closed immediately. The ONLY place an SGR is emitted.

    One function, so "every code opened is closed" is a property of this file
    rather than a habit at four call sites: an unterminated run bleeds into
    whatever the neighbouring status-line segment prints next.
    """
    return code + text + _RESET if color else text


def _elide(slug: str, limit: int = MAX_SLUG_CHARS) -> str:
    if len(slug) <= limit:
        return slug
    return slug[: limit - 1] + _ELLIPSIS


def render(
    state: dict | None,
    *,
    configured: bool,
    live: bool = False,
    color: bool = True,
    off: bool = False,
) -> str:
    """The status-line segment. One line, bounded, self-delimiting, or empty.

    THE SEGMENT MUST SURVIVE ANY NEIGHBOUR. It shares one line with whatever
    else the user has chained into `statusLine`, so four properties are load-
    bearing rather than cosmetic:

    * **No newline, ever.** Claude Code splits the command's stdout on newlines
      and renders each as its own status row; a stray one silently restructures
      somebody else's status line.
    * **A two-space indent and a glyph in front.** Output is concatenated with
      the neighbour's, so without a leading gap `…main● folding` fuses into one
      unreadable token. The glyph is the anchor that says a new thing started.
    * **Bounded width.** Overflow wraps the whole line, which reflows every
      segment on it — the one way this can make other output WORSE rather than
      just longer. Hence the elision and the ceiling.
    * **One coloured character, always closed, never counted.** Only the dot is
      painted (`_paint` is the single place an SGR is emitted, and it closes what
      it opens — an unterminated run bleeds into whatever prints next). Layout is
      computed on PLAIN text and colour applied afterwards: measuring a string
      with escape sequences in it counts bytes nobody can see, and the segment
      would silently elide a name that fit. `color=False` is for a terminal that
      cannot take it, and for tests asserting on text.

    Empty string when Probe is not configured on this machine: someone who does
    not use it should not spend a single column on being told so.
    """
    if not configured:
        return ""

    if off:
        # Muted, because it is a state the reader CHOSE. Yellow would nag about
        # a decision they already made, and silence would be indistinguishable
        # from the segment being broken.
        return _INDENT + _paint(_DOT, _DIM, color) + " " + _LABEL_OFF

    # THE DOT IS THE ONLY COLOURED THING, in either state. Every word stays the
    # terminal's default, so the segment reads like its neighbours and the eye
    # has exactly one place to check. Green means landing, yellow means it is
    # not -- yellow rather than dim, because untracked is the state worth
    # noticing, and dim tells the reader to skip precisely when they should look.
    project = state.get("project") if isinstance(state, dict) else None
    if not (isinstance(project, str) and project):
        return _INDENT + _paint(_DOT, _YELLOW, color) + " " + _LABEL_UNTRACKED

    # THE NAME YIELDS, THE LABEL AND ACCENT DO NOT. `MAX_SLUG_CHARS` reserves
    # both widths whether or not the accent is showing, so truncation only ever
    # costs characters of the project name -- eliding "· runn…" or "tracke… →"
    # would spend the reader's attention on the parts they can already infer.
    accent = _ACCENT_TEXT if live else ""
    return _INDENT + _paint(_DOT, _GREEN, color) + " " + _LABEL_TRACKED + _elide(project) + accent
