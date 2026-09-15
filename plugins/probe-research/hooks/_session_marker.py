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
import stat
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
_LABEL_TRACKED = "tracking " + _ARROW + " "
_LABEL_TRACKING_BARE = "tracking"
#: Kept for readers that still resolve the switch to a BOOLEAN. `render` only
#: reaches it when no three-valued state was passed in, which is the shape a
#: pre-three-state caller has. New callers pass `session_state` and get one of
#: the two labels below instead.
_LABEL_NOT_TRACKING = "not tracking"

#: The two non-recording states, spelled the way the CLI, the config file and
#: the deny reason spell them. A status line that said "reading only" while
#: `probe session status` said "read-only" would make the reader translate
#: between two vocabularies for one fact -- which is the drift the shared-label
#: design exists to prevent. Both are SHORTER than `not tracking` (9 and 3
#: against 12), so the width cap in MAX_SEGMENT_CHARS is not touched by them.
_LABEL_READ_ONLY = "read-only"
_LABEL_OFF = "off"

#: A middle dot, NOT a second arrow. `tracked → folding ▸ running` puts two
#: arrow-shaped glyphs in one short segment, and the eye reads them as a
#: sequence of three things rather than as a name with a state hung off it.
_ACCENT_TEXT = " " + _SEPARATOR + " running"

#: The third state: tracking is ON and the work IS landing, but no
#: transcript-capture daemon is live for this conversation.
#:
#: A HALF-FILLED dot, not a hollow one. Hollow is faint at terminal font sizes
#: (see `_DOT`), and the reader must be able to tell this from `not tracking` at
#: a glance; the WORD carries the meaning for anyone whose terminal renders both
#: dots the same. The dot stays GREEN when painted, because green means
#: "landing" and the work still is — yellow already belongs to `not tracking`,
#: and reusing it would make a degraded session look like a switched-off one.
_DOT_DEGRADED = "◐"  # ◐
_LABEL_NO_CAPTURE = " " + _SEPARATOR + " no capture: "

#: Below this many columns an elided project name identifies nothing — `a-rea…`
#: is not a name, it is noise wearing one. When the reason is long enough to
#: push the name under this floor, the name is dropped ENTIRELY and the segment
#: falls back to `_LABEL_TRACKING_BARE`, which this file already renders for
#: "tracking is on, nothing filed yet".
_MIN_SLUG_CHARS_DEGRADED = 8

#: Hard ceiling on the rendered segment, leading indent included, counted in
#: VISIBLE characters. The cap exists because the status line must not WRAP: a
#: wrapped line reflows every other segment sharing it, which is the one failure
#: that makes neighbouring output less readable rather than merely longer.
#:
#: 51 is measured, not picked. It is the smallest ceiling at which a 26-character
#: name budget survives, and 26 is what this lab's project names actually need:
#: median 18, longest 29, 27 of 28 whole. At 25 that drops to 15 of 20 on the
#: pinned sample -- one character of label costs four whole names, because real
#: slugs cluster right at the boundary.
#:
#: THE LABEL IS PAID FOR BY THE CEILING, NEVER BY THE NAME. Spelling out
#: "tracking → " costs eleven columns; taking them from the name would elide the
#: project the segment exists to identify. When the label changes length, this
#: number moves with it. `test_this_labs_project_names_mostly_fit_whole` pins it.
MAX_SEGMENT_CHARS = 51

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


def tracking_signal_path(session_id: str) -> Path:
    """Where THE DECISION lives: is this conversation being tracked.

    One file, three readable values -- `on`, `off`, and absent. Absent is not a
    third STATE, it is the absence of a decision, and `is_tracking` resolves it
    from what the session has actually recorded. Two states reach the reader;
    the undecided case just has to pick one of them honestly.

    Per session, beside the marker, because the answer is about THIS
    conversation. A machine-wide switch would decide the next one too.
    """
    return sessions_dir() / (session_id + ".tracking")


def _legacy_off_path(session_id: str) -> Path:
    """The 0.27-0.29 spelling: presence of `<sid>.off` meant "not tracking".

    Read, never written. A researcher who turned tracking off before upgrading
    must not silently come back ON because the file was renamed underneath them
    -- that is precisely the decision the file exists to remember.
    """
    return sessions_dir() / (session_id + ".off")


def tracking_signal(session_id: str) -> str | None:
    """`"on"`, `"off"`, or None when nobody has decided yet.

    THE CANONICAL FILE ANSWERS FIRST, and that is what stops the two files from
    ever disagreeing inside this codebase. `set_session_state` publishes twice --
    `.state`, then the compat `.tracking` -- and two writers interleaving those
    four writes can leave `.state=off` beside `.tracking=on`. Reading `.tracking`
    directly, half the surfaces here (the status-line refresh, the capture check,
    the wizard) would keep recording while the guard refused every write: one
    setting, two answers, and nothing saying which is winning.

    So `.tracking` is a file we WRITE for old clients and do not READ while the
    canonical one exists. A torn pair is then eventually consistent rather than
    contradictory -- the next write repairs it, and until then everything here
    agrees.
    """
    if not valid_session_id(session_id):
        return None
    try:
        raw = state_path(session_id).read_text(encoding="utf-8").strip().lower()
        if raw in STATES:
            return "on" if raw == STATE_FULL else "off"
    except OSError:
        pass
    try:
        value = tracking_signal_path(session_id).read_text(encoding="utf-8").strip().lower()
        if value in ("on", "off"):
            return value
    except OSError:
        pass
    return "off" if _legacy_off_path(session_id).is_file() else None


def set_tracking(session_id: str, on: bool) -> bool:
    """Record a BOOLEAN decision. True when it landed.

    THE FALSE BRANCH IS `read-only`, NOT `off`. Every caller of this function
    predates the third state, and what they have always meant by False is what
    `probe session untrack` has always documented: "records nothing further".
    That is `read-only` precisely -- the switch has never gated reads. Routing
    them to the new `off` would take reads away from people who asked only to
    stop recording, which is the same mistake `session_state` refuses to make
    when it migrates a stored marker.

    The hard `off` is reachable only by naming it: `set_session_state(sid,
    STATE_OFF)`, which is what the new switch calls.
    """
    return set_session_state(
        session_id, STATE_FULL if on else STATE_READ_ONLY
    )


def set_tracking_if_absent(session_id: str, on: bool) -> bool:
    """Record the session's STARTING value, ONLY when nobody has decided yet.
    True when THIS call decided.

    The seed half of the toggle. A session starts at the effective default for
    its INITIAL cwd (`resolve_tracking_default(initial_cwd)`) and the file exists
    from SessionStart onward, so "undecided" is not a state any reader has to
    resolve for itself: one setting, one file, always present.

    THE VALUE IS A PARAMETER, never a literal `on`. Seeding `on` on a machine
    whose initial-cwd default is `off` would let automation author a declaration the
    researcher did not make -- the default IS the researcher saying which value
    a new session starts at, and it is the same setting the toggle flips, not a
    weaker kind of preference. A DECISION is never touched either: an existing
    file in either direction, and the legacy `<sid>.off` spelling, all block
    the seed.

    Exclusive by construction, not by check-then-write. The content is written
    to a UNIQUE temp file and PUBLISHED with os.link, which fails with EEXIST
    rather than replacing. A researcher typing the toggle to `off` while the
    first write is in flight keeps their `off`, and a crash between create and
    publish cannot strand a half-written signal: the target either does not
    exist or carries complete content.

    The temp name carries pid AND a nanosecond stamp, and is created O_EXCL.
    Naming it by pid alone lets two THREADS of one process share it, and the
    loser then truncates a file the winner has already published — a reader
    sees an empty marker. `tempfile.mkstemp` would say this in one call and
    costs ~17ms of import (it pulls in shutil/lzma); this module is imported on
    every status-line render, where that is the whole budget. Hence os.open.

    THE LEGACY SPELLING IS RE-CHECKED AFTER PUBLISHING. `os.link` arbitrates
    `<sid>.tracking` only, so an old client writing `<sid>.off` between the read
    and the publish would leave both files on disk with `on` winning the read —
    automation silently overriding an explicit opt-out, which is the one thing
    this must never do. Losing that race is repaired by removing what we just
    published, not by keeping it.
    """
    return set_session_state_if_absent(
        session_id, STATE_FULL if on else STATE_READ_ONLY
    )


def is_tracking(signal: str | None, *, default: bool | None = None) -> bool:
    """The one boolean the whole surface renders. TWO STATES, never three.

    An explicit per-session decision wins in BOTH directions -- that is what
    makes the toggle a toggle, and it is why a researcher who turns tracking off
    stays off no matter what any default says.

    With no decision, this machine's default answers. Since SessionStart seeds
    the signal from that default, a missing file now means only that the seed
    never ran -- a session started before this shipped, or a hook that could
    not write -- so resolving it against the same default is what keeps those
    sessions reading the value the researcher chose rather than a third
    behaviour nobody selected.

    `default=None` reads the machine's setting. Callers that already parsed the
    config pass it in so the render path parses the file once.
    """
    if signal == "off":
        return False
    if signal == "on":
        return True
    return default_tracking() if default is None else default


# ---------------------------------------------------------------------------
# THE THREE-VALUED SWITCH.
#
#   full        reads and writes. What "tracking on" has always meant.
#   read-only   reads allowed, records nothing new. What "tracking off" has
#               always meant -- the switch never gated reads.
#   off         no Probe calls at all, and no session-start injections.
#
#          /probe            /probe            /probe
#   full ----------> read-only ----------> off ----------> full
#         (stop            (stop             (resume
#         writing)         reading)          everything)
#
#   Each press removes exactly one capability, so the cycle is learnable after
#   one lap. Reversing that (full -> off -> read-only) would ADD capability on
#   the second press, which reads as random.
#
# TWO FILES, ONE DECISION. `<sid>.state` is canonical and three-valued.
# `<sid>.tracking` keeps being WRITTEN with the two-valued projection and is
# never read by new code. That is not bookkeeping: `tracking_signal` above
# returns its value only when it is exactly "on" or "off", and ANY other answer
# -- an unknown word, or a missing file -- falls through to `default_tracking()`
# -> DEFAULT_TRACKING -> True. So an older client that met a three-valued file,
# or met no file at all after a clean cutover, would resolve an opt-out to
# TRACKING ON and silently record work the researcher declined. The compat write
# is what makes that unsayable, and it is the same trick `<sid>.off` already
# plays in the other direction.
# ---------------------------------------------------------------------------

STATE_FULL = "full"
STATE_READ_ONLY = "read-only"
STATE_OFF = "off"

#: Every state, and the order `toggle` advances through. One tuple, because a
#: separate cycle list is a second place to forget a state.
STATES = (STATE_FULL, STATE_READ_ONLY, STATE_OFF)

#: What a machine that has said nothing gets. `full`, for the same reason
#: DEFAULT_TRACKING is True: tracking must not depend on anyone remembering to
#: ask for it. Shipping `read-only` here would quietly stop recording for
#: everyone on upgrade -- a product change wearing a plumbing change's clothes.
DEFAULT_STATE = STATE_FULL


def state_path(session_id: str) -> Path:
    """Where the three-valued decision lives. Canonical; see the block above."""
    return sessions_dir() / (session_id + ".state")


def normalize_state(raw: object) -> "str | None":
    """A recognized state name, or None.

    Accepts the spellings a person actually types. `read-only` carries a hyphen,
    and a hyphen is the one character someone reliably gets wrong, so the three
    obvious near-misses resolve rather than reading as unrecognized -- which, per
    `default_session_state`, would mean falling back to `full` and recording work
    somebody tried to decline.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    # THE CANONICAL NAMES FIRST. They are not all in the legacy synonym tuples
    # -- `full` is in none of them -- and leaning on those alone meant the one
    # spelling this file documents everywhere was the one it rejected.
    if value in STATES:
        return value
    if value in TRACKING_READ_ONLY_VALUES:
        return STATE_READ_ONLY
    if value in TRACKING_OFF_VALUES:
        return STATE_OFF
    if value in TRACKING_ON_VALUES:
        return STATE_FULL
    return None


def next_state(state: str) -> str:
    """The state one press of the bare switch lands on. Wraps."""
    try:
        return STATES[(STATES.index(state) + 1) % len(STATES)]
    except ValueError:
        return STATE_FULL


def state_allows_writes(state: "str | None") -> bool:
    """Only `full` records anything new."""
    return state == STATE_FULL


def state_allows_reads(state: "str | None") -> bool:
    """`full` and `read-only` may look things up. Only `off` may not.

    An UNKNOWN state reads as allowed, deliberately. Everywhere else in this
    file ambiguity resolves toward the shipped posture, and a corrupted marker
    that silently stopped an agent finding prior work would be invisible: it
    looks exactly like the work not existing.
    """
    return state != STATE_OFF


def session_state(session_id: str) -> "str | None":
    """This conversation's state, or None when nobody has decided yet.

    MIGRATION LIVES HERE, and it is the most consequential rule in the switch.
    A legacy `off` marker was written by somebody who meant "stop recording,
    keep searching" -- the switch has never gated reads. That is the new
    `read-only` exactly. Mapping it to the new `off` would retroactively take
    away reads they never gave up, so it maps to `read-only` and the hard `off`
    is reachable only from an explicit new-format write.

        .state = full | read-only | off   ->  that
        .tracking = "on"                  ->  full
        .tracking = "off"                 ->  read-only
        <sid>.off present                 ->  read-only
        nothing                           ->  None (undecided)
    """
    if not valid_session_id(session_id):
        return None
    try:
        raw = state_path(session_id).read_text(encoding="utf-8").strip().lower()
        if raw in STATES:
            return raw
    except OSError:
        pass
    # No canonical file: fall back to the legacy spellings and MIGRATE them.
    # Read those directly rather than through `tracking_signal`, which now
    # consults `.state` first and would re-walk a file we just missed.
    legacy = _legacy_tracking_value(session_id)
    if legacy == "on":
        return STATE_FULL
    if legacy == "off":
        return STATE_READ_ONLY
    return STATE_READ_ONLY if _legacy_off_path(session_id).is_file() else None


def _publish_atomically(path: Path, content: str) -> bool:
    """Write `content` to `path` so no reader can ever see it half-written.

    A plain `write_text` truncates first, so a concurrent reader -- and there is
    always one, the status line renders on every turn -- can observe an EMPTY
    marker and resolve it as "no decision". Write to a unique temp and
    `os.replace`, which is atomic on POSIX and on Windows.

    `os.replace`, not `os.link`: this is a LAST-WRITER-WINS file, unlike the
    exclusive publish in `set_session_state_if_absent`, which must fail rather
    than overwrite.
    """
    tmp = "%s.tmp-%d-%d" % (str(path), os.getpid(), time.time_ns())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _write_compat_tracking(session_id: str, state: str) -> None:
    """Leave older clients a value they cannot misread. Best effort.

    `full` -> on; `read-only` and `off` -> off. Both non-recording states project
    to `off` because that is the strongest thing a two-valued reader can be told,
    and it is TRUE of both: neither records. Failure here is not fatal -- the
    canonical file already landed -- but it is the difference between an old CLI
    reading an opt-out and an old CLI inventing consent.
    """
    if _publish_atomically(
        tracking_signal_path(session_id), "on\n" if state == STATE_FULL else "off\n"
    ):
        try:
            _legacy_off_path(session_id).unlink(missing_ok=True)
        except OSError:
            pass


def _legacy_tracking_value(session_id: str) -> "str | None":
    """The two-valued file's raw decision, or None. Never consults `.state`."""
    try:
        value = tracking_signal_path(session_id).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return value if value in ("on", "off") else None


def set_session_state(session_id: str, state: str) -> bool:
    """Record the decision. True when the canonical file now reads `state`."""
    if not valid_session_id(session_id) or state not in STATES:
        return False
    if not _publish_atomically(state_path(session_id), state + "\n"):
        return False
    _write_compat_tracking(session_id, state)
    return True


def set_session_state_if_absent(session_id: str, state: str) -> bool:
    """Seed the STARTING state, only when nobody has decided. True when we did.

    Exclusive by construction, exactly as `set_tracking_if_absent` is and for the
    same reasons -- see that docstring for why `os.link` rather than a
    check-then-write, and why the temp name carries pid AND a nanosecond stamp.
    The legacy spellings are re-checked after publishing for the same reason too:
    an old client writing `<sid>.off` between the read and the publish must not
    be overridden by automation.
    """
    if not valid_session_id(session_id) or state not in STATES:
        return False
    if session_state(session_id) is not None:
        return False
    path = state_path(session_id)
    tmp_path = "%s.tmp-%d-%d" % (str(path), os.getpid(), time.time_ns())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(state + "\n")
        try:
            os.link(tmp_path, path)
        except OSError:
            return False  # EEXIST: someone decided between the read and the publish
        # BOTH LEGACY SPELLINGS ARE RE-CHECKED AFTER PUBLISHING, and the second
        # one is new with this file. `os.link` arbitrates `<sid>.state` only, so
        # a decision that landed in either older spelling between our read and
        # our publish would otherwise be overwritten by the compat write below --
        # automation silently overriding an explicit opt-out, which is the one
        # thing this must never do. Losing that race is repaired by removing what
        # we just published, not by keeping it.
        try:
            published = path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            published = ""
        if published != state:
            # A NEWER WRITER took the canonical file. `os.link` refuses to
            # replace, but `set_session_state` publishes with `os.replace`,
            # which does, so what is on disk is now somebody's explicit
            # decision. Leave it and report that we did not decide. Deleting
            # here is how a seed erased an explicit `off` that had landed a
            # microsecond earlier, leaving only its compat file to migrate back
            # to read-only and silently reopen reads the researcher had closed.
            return False
        legacy = _legacy_tracking_value(session_id)
        expected = "on" if state == STATE_FULL else "off"
        if _legacy_off_path(session_id).is_file() or (
            legacy is not None and legacy != expected
        ):
            # An OLDER CLIENT wrote a legacy opt-out while we were publishing.
            # Only a value that DISAGREES counts: the one we are about to write
            # ourselves agrees by construction.
            path.unlink(missing_ok=True)
            return False
        _write_compat_tracking(session_id, state)
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


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


def _capture_reason(state: dict | None) -> str:
    """Why no transcript daemon is watching this session, or `""` for silence.

    The marker's `capture` object is written by the background refresh hook, and
    the reason is one of the closed vocabulary in `probe.cli.capture_state`.
    SILENCE IS THE DEFAULT, and deliberately so: a marker written by an older
    refresh hook carries no `capture` key at all, and every shape this function
    does not recognise — a missing key, a null, a non-string reason, a `running`
    that is not exactly `False` — means nobody measured anything. Rendering a
    guess there would put a fabricated diagnosis on somebody's status line.

    A multi-line reason is refused rather than flattened: the one thing the
    segment may never do is emit a newline, and a reason that needs two lines is
    not a reason this vocabulary produced.

    Shared by `render` and `message` so the two surfaces cannot come to disagree
    about when capture is missing.
    """
    capture = state.get("capture") if isinstance(state, dict) else None
    if not isinstance(capture, dict) or capture.get("running") is not False:
        return ""
    reason = capture.get("reason")
    if not (isinstance(reason, str) and reason) or "\n" in reason or "\r" in reason:
        return ""
    return reason


def message(state: dict | None, *, live: bool = False, tracking: bool = True) -> str:
    """One line for an agent with no status line to hang a segment on.

    Built from the same labels the segment uses, so the two surfaces cannot drift
    into describing the same state differently. No colour and no glyph: this is a
    sentence in a transcript, not a mark on a line.

    `tracking` defaults to True because that is the only state this notice has
    ever been emitted in — `statusline_notify.py` reaches here having already
    resolved the switch. It is a parameter rather than an assumption so a caller
    that has NOT resolved it cannot accidentally claim a capture failure matters
    to a session that is recording nothing.

    NO WIDTH CAP HERE, unlike `render`. This is a sentence in a transcript, which
    wraps harmlessly; the segment shares one line with strangers, which does not.
    """
    project = state.get("project") if isinstance(state, dict) else None
    if not (isinstance(project, str) and project):
        return "Probe: this session is not tracked yet."
    text = "Probe: " + _LABEL_TRACKED + project
    if live:
        text += _ACCENT_TEXT
    reason = _capture_reason(state) if tracking else ""
    if reason:
        text += ", but no transcript capture: " + reason
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


#: Ships ON. Tracking is the default posture: a session is tracked unless
#: somebody said otherwise, which is what stops tracking from depending on
#: anyone remembering to ask for it.
DEFAULT_TRACKING = True

#: Where a researcher's own default lives in the config file. TOP LEVEL, never
#: inside a context: `sdk.config.clear_context` replaces a context wholesale
#: (`contexts[name] = {}`, a deliberate fail-closed wipe), so a preference
#: stored there would be erased by `probe logout` and tracking would silently
#: come back on. It would also make the default follow whichever TENANT is
#: selected, which is not what "all my sessions" means.
DEFAULTS_KEY = "defaults"
TRACKING_DEFAULT_KEY = "session_tracking"

#: The THREE-VALUED default, in its own key beside the two-valued one.
#:
#: A separate key, not a widened vocabulary in the old one, and the reason is
#: the same collision the flip claim hit: `session_tracking: "off"` was written
#: when `off` meant "stop recording, keep searching". That is `read-only` now.
#: Reading the new hard `off` out of that key would silently take reads away
#: from every folder and machine already configured -- the exact migration
#: mistake `session_state` refuses to make for a session marker.
#:
#: So writers write BOTH: this key three-valued, the old one as the safe
#: two-valued projection for clients that only know it. Readers prefer this one
#: and fall back to the old one THROUGH the legacy mapping.
STATE_DEFAULT_KEY = "session_state"

#: The spellings `default_tracking` recognizes, exported so the ONE other
#: surface that reasons about the env override (the wizard's settings screen)
#: cannot drift from the parser: a value outside both sets is IGNORED, and a
#: screen that calls it an override anyway misdiagnoses a typo.
TRACKING_OFF_VALUES = ("off", "0", "false", "no", "disabled")
TRACKING_ON_VALUES = ("on", "1", "true", "yes", "enabled")

#: The third set, exported beside the other two so the wizard and the CLI cannot
#: drift from this parser. Four spellings for one state looks generous until you
#: remember the failure mode: an unrecognized value resolves to `full`, so a
#: researcher who typed `readonly` and got recorded anyway would have no way to
#: tell a typo from a bug.
TRACKING_READ_ONLY_VALUES = ("read-only", "readonly", "read_only", "ro")

# Folder configs are repository-controlled settings, never data blobs. Their bound
# protects startup/status paths from devices and giant files before JSON parsing can
# fail soft. Machine configs remain size-unbounded for backwards compatibility.
FOLDER_CONFIG_MAX_BYTES = 64 * 1024


def _parse_tracking_value(value: object) -> bool | None:
    """A recognized stored/env tracking value, or None when it is unknown."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in TRACKING_OFF_VALUES:
            return False
        if normalized in TRACKING_ON_VALUES:
            return True
    return None


def _read_config_text(
    path: Path,
    *,
    require_regular: bool = False,
    max_bytes: int | None = None,
) -> str:
    """Read a machine config normally or a folder config through a validated fd."""
    if not require_regular:
        return path.read_text(encoding="utf-8")

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path} is not a regular file")
        if max_bytes is not None and info.st_size > max_bytes:
            raise ValueError(f"{path} exceeds the {max_bytes}-byte size limit")
        chunks: list[bytes] = []
        remaining = None if max_bytes is None else max_bytes + 1
        while remaining is None or remaining:
            chunk = os.read(fd, 16 * 1024 if remaining is None else min(16 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        raw = b"".join(chunks)
        if max_bytes is not None and len(raw) > max_bytes:
            raise ValueError(f"{path} exceeds the {max_bytes}-byte size limit")
        return raw.decode("utf-8")
    finally:
        os.close(fd)


def _read_config() -> dict:
    """The config file as a dict, or `{}`. ONE parse, two answers.

    `configured()` and `default_tracking()` both need this file, and it is read
    on every status-line render, so parsing it twice would double the only I/O
    on that path. Small enough that the cost is the interpreter, not the file:
    measured at ~0.018ms to open and parse a realistic config.
    """
    try:
        data = json.loads(_read_config_text(config_path()))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def tracking_env_override() -> bool | None:
    """The RECOGNIZED `PROBE_SESSION_TRACKING` value, or None.

    None covers both "unset" and "unrecognized" -- a typo is ignored, never
    read as `off` (see `default_tracking`). Split out so a writer can ask
    "is the env holding this setting down?" without re-implementing the
    parse: while a recognized override is active, toggling the STORED default
    from the effective value writes `not env` into the file invisibly, and
    the stored value can never be set equal to the env's -- the wizard hides
    its toggle row on this answer instead.
    """
    # BOTH VARIABLES, PROJECTED. The three-valued reader has its own
    # `state_env_override`, but half this codebase still asks the boolean
    # question -- the status-line refresh, the capture check, the wizard's
    # settings screen. If `PROBE_SESSION_STATE=off` were invisible here, those
    # surfaces would keep behaving as though the session were recording while
    # the guard refused every write: one setting, two answers, nothing saying
    # which is winning. That is the exact failure this pair of functions exists
    # to prevent, so the projection happens once, here.
    explicit = normalize_state(os.environ.get("PROBE_SESSION_STATE"))
    if explicit is not None:
        return explicit == STATE_FULL
    return _parse_tracking_value(os.environ.get("PROBE_SESSION_TRACKING"))


def _tracking_value(data: dict) -> bool | None:
    """The stored default as a BOOLEAN, from whichever key carries it.

    Writers put both keys down, so the old one is normally enough. This reads
    the new one as a fallback for the config somebody edited BY HAND -- setting
    `session_state` alone is the obvious thing to do once it is documented, and
    without this fallback that edit would move the guard and leave the status
    line, the capture check and the wizard reading the shipped default.
    """
    defaults = data.get(DEFAULTS_KEY)
    if not isinstance(defaults, dict):
        return None
    value = _parse_tracking_value(defaults.get(TRACKING_DEFAULT_KEY))
    if value is not None:
        return value
    state = normalize_state(defaults.get(STATE_DEFAULT_KEY))
    return None if state is None else state == STATE_FULL


def default_tracking(config: dict | None = None) -> bool:
    """This machine's default for sessions nobody has decided about.

    `PROBE_SESSION_TRACKING` wins, matching the env-beats-file rule the rest of
    the client follows -- but it is an OVERRIDE and never the home for this
    setting. A dock-launched Claude Code sources no shell profile, so a default
    exported from a shell rc applies in a terminal session and not in a
    dock-launched one: the same setting, two answers, which is worse than having
    no setting at all.

    Anything unrecognised reads as the shipped default rather than as `off`. A
    typo in a config file must not silently stop recording someone's research.
    """
    override = tracking_env_override()
    if override is not None:
        return override
    data = _read_config() if config is None else config
    value = _tracking_value(data)
    return DEFAULT_TRACKING if value is None else value


def state_env_override() -> "str | None":
    """The RECOGNIZED `PROBE_SESSION_TRACKING` value as a state, or None.

    Same variable as the boolean override, widened. Keeping one variable matters
    more than the tidiness of a second: a machine that exported the old name and
    a machine that exported a new one would disagree about the same setting, and
    nothing would say which was winning.
    """
    explicit = normalize_state(os.environ.get("PROBE_SESSION_STATE"))
    if explicit is not None:
        return explicit
    # The old variable, read as what it meant when it was the only one.
    return _legacy_state_from_tracking(os.environ.get("PROBE_SESSION_TRACKING"))


def _legacy_state_from_tracking(value: object) -> "str | None":
    """A two-valued stored default, read as what it MEANT: on -> full, off -> read-only."""
    parsed = _parse_tracking_value(value)
    if parsed is None:
        return None
    return STATE_FULL if parsed else STATE_READ_ONLY


def _stored_state(defaults: object) -> "str | None":
    """The state a `defaults` block carries, new key first, else the legacy one."""
    if not isinstance(defaults, dict):
        return None
    value = normalize_state(defaults.get(STATE_DEFAULT_KEY))
    if value is not None:
        return value
    return _legacy_state_from_tracking(defaults.get(TRACKING_DEFAULT_KEY))


def default_session_state(config: dict | None = None) -> str:
    """This machine's default state for sessions nobody has decided about.

    Env beats file, matching the rest of the client. Anything UNRECOGNIZED reads
    as DEFAULT_STATE rather than as a quieter state -- the rule `default_tracking`
    already states for recording ("a typo in a config file must not silently stop
    recording someone's research") applied to reads as well, which is where it
    now also bites.
    """
    override = state_env_override()
    if override is not None:
        return override
    data = _read_config() if config is None else config
    value = _stored_state(data.get(DEFAULTS_KEY))
    return DEFAULT_STATE if value is None else value


def folder_config_path(folder: str | os.PathLike[str]) -> Path:
    """The absolute config path owned by exactly ``folder``."""
    return Path(os.path.abspath(os.fspath(folder))) / ".probe" / "config.json"


def _append_config_error(errors: list[str] | None, message: str) -> None:
    if errors is not None and message not in errors:
        errors.append(message)


def _folder_tracking_override(path: Path, errors: list[str] | None = None) -> bool | None:
    try:
        raw = _read_config_text(
            path,
            require_regular=True,
            max_bytes=FOLDER_CONFIG_MAX_BYTES,
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _append_config_error(errors, f"{path}: could not be read ({exc})")
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        _append_config_error(errors, f"{path}: malformed JSON ({exc})")
        return None
    if not isinstance(data, dict):
        _append_config_error(errors, f"{path}: config is not a JSON object")
        return None
    if DEFAULTS_KEY not in data:
        return None
    defaults = data[DEFAULTS_KEY]
    if not isinstance(defaults, dict):
        _append_config_error(errors, f"{path}: defaults is not a JSON object")
        return None
    if TRACKING_DEFAULT_KEY not in defaults and STATE_DEFAULT_KEY not in defaults:
        return None
    # Same projection as `_tracking_value`, for the same reason: a folder config
    # carrying only the new key must not read as "no override here" to every
    # surface that still asks the boolean question.
    value = _tracking_value({DEFAULTS_KEY: defaults})
    if value is None:
        key = TRACKING_DEFAULT_KEY if TRACKING_DEFAULT_KEY in defaults else STATE_DEFAULT_KEY
        _append_config_error(errors, f"{path}: invalid defaults.{key} value")
    return value


def folder_tracking_override(
    folder: str | os.PathLike[str], *, errors: list[str] | None = None
) -> bool | None:
    """The override stored by exactly ``folder``, without inheritance."""
    return _folder_tracking_override(folder_config_path(folder), errors)


def resolve_tracking_default(
    cwd: str | os.PathLike[str] | None,
    config: dict | None = None,
    *,
    errors: list[str] | None = None,
) -> tuple[bool, str]:
    """The effective new-session default and its diagnostic source."""
    override = tracking_env_override()
    if override is not None:
        return override, "environment"

    try:
        current = Path(os.path.abspath(os.getcwd() if cwd is None else os.fspath(cwd)))
    except (OSError, TypeError, ValueError) as exc:
        _append_config_error(errors, f"{cwd}: could not resolve working directory ({exc})")
        current = None

    while current is not None:
        path = current / ".probe" / "config.json"
        value = _folder_tracking_override(path, errors)
        if value is not None:
            return value, str(path)
        parent = current.parent
        if parent == current:
            break
        current = parent

    data = _read_config() if config is None else config
    value = default_tracking(data)
    if _tracking_value(data) is None:
        return value, "shipped"
    machine_path = Path(os.path.abspath(os.fspath(config_path())))
    return value, str(machine_path)


def _folder_state_override(path: Path, errors: list[str] | None = None) -> "str | None":
    """One folder's stored state, or None. Reuses the boolean reader's I/O rules.

    Reads the SAME key the boolean override reads (`defaults.session_tracking`),
    because it is the same setting with a wider vocabulary. A second key would
    let one folder hold two answers.
    """
    try:
        raw = _read_config_text(
            path, require_regular=True, max_bytes=FOLDER_CONFIG_MAX_BYTES
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _append_config_error(errors, f"{path}: could not be read ({exc})")
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        _append_config_error(errors, f"{path}: malformed JSON ({exc})")
        return None
    if not isinstance(data, dict):
        _append_config_error(errors, f"{path}: config is not a JSON object")
        return None
    if DEFAULTS_KEY not in data:
        return None
    defaults = data[DEFAULTS_KEY]
    if not isinstance(defaults, dict):
        _append_config_error(errors, f"{path}: defaults is not a JSON object")
        return None
    # SAME DIAGNOSTICS AS THE TWO-VALUED READER. A folder config is
    # repository-controlled and a typo in it is somebody's mistake to find, so
    # an unusable value is REPORTED and then ignored -- never silently swallowed
    # into the shipped default, which is what makes a typo indistinguishable
    # from "no override here".
    present = [k for k in (STATE_DEFAULT_KEY, TRACKING_DEFAULT_KEY) if k in defaults]
    if not present:
        return None
    value = _stored_state(defaults)
    if value is None:
        _append_config_error(errors, f"{path}: invalid defaults.{present[0]} value")
    return value


def resolve_state_default(
    cwd: str | os.PathLike[str] | None,
    config: dict | None = None,
    *,
    errors: list[str] | None = None,
) -> tuple[str, str]:
    """The effective new-session STATE and its diagnostic source.

    Walks the same ladder as `resolve_tracking_default` -- env, then each
    `.probe/config.json` from cwd upward, then the machine file, then shipped --
    so the two can never disagree about which file won, only about how many
    values that file is allowed to hold.
    """
    override = state_env_override()
    if override is not None:
        return override, "environment"

    try:
        current = Path(os.path.abspath(os.getcwd() if cwd is None else os.fspath(cwd)))
    except (OSError, TypeError, ValueError) as exc:
        _append_config_error(errors, f"{cwd}: could not resolve working directory ({exc})")
        current = None

    while current is not None:
        path = current / ".probe" / "config.json"
        value = _folder_state_override(path, errors)
        if value is not None:
            return value, str(path)
        parent = current.parent
        if parent == current:
            break
        current = parent

    data = _read_config() if config is None else config
    stored = _stored_state(data.get(DEFAULTS_KEY))
    if stored is None:
        return DEFAULT_STATE, "shipped"
    return stored, str(Path(os.path.abspath(os.fspath(config_path()))))


def write_folder_tracking_default(
    folder: str | os.PathLike[str], on: bool | None
) -> Path:
    """Set this exact folder's default; None removes the override to inherit."""
    from probe.sdk.config import (
        ConfigUnreadable,
        _config_lock,
        _load_json_file,
        _save_json_file,
        _write_target,
    )

    path = folder_config_path(folder)
    target = _write_target(path)
    with _config_lock(target, _resolved=True):
        data = _load_json_file(
            target,
            strict=True,
            migrate=False,
            require_regular=True,
            max_bytes=FOLDER_CONFIG_MAX_BYTES,
        )
        defaults = data.get(DEFAULTS_KEY)
        if DEFAULTS_KEY in data and not isinstance(defaults, dict):
            raise ConfigUnreadable(
                f"{path} has a non-object {DEFAULTS_KEY}. Refusing to overwrite it."
            )
        if on is None:
            if isinstance(defaults, dict):
                defaults.pop(STATE_DEFAULT_KEY, None)
                defaults.pop(TRACKING_DEFAULT_KEY, None)
                if not defaults:
                    data.pop(DEFAULTS_KEY, None)
        else:
            if not isinstance(defaults, dict):
                defaults = {}
                data[DEFAULTS_KEY] = defaults
            defaults[TRACKING_DEFAULT_KEY] = "on" if on else "off"
            defaults[STATE_DEFAULT_KEY] = STATE_FULL if on else STATE_READ_ONLY
        _save_json_file(
            target,
            data,
            private=False,
            _resolved=True,
            max_bytes=FOLDER_CONFIG_MAX_BYTES,
        )
    return path


def write_default_tracking(on: bool) -> Path:
    """Persist this machine's session-tracking default; returns the config path.

    TOP LEVEL, never inside a context -- see DEFAULTS_KEY. Written through the
    config module's own primitives, and each one guards a real loss:

    * `_config_lock` -- `save_file` alone is atomic but not isolated, so this
      write racing a `probe login` would restore its stale snapshot over the
      fresh token and report success doing it.
    * `load_file(strict=True)` -- the raw file may be a v1 flat blob. A raw
      read-modify-write leaves `defaults` beside the v1 keys, and the NEXT
      canonical write migrates every one of those keys into the context --
      burying the preference where `default_tracking` never looks, so capture
      silently comes back on. The migrating loader lands the file in v2 shape
      with `defaults` at the top level, where it stays. Strict also means an
      unparseable or non-object file RAISES (`ConfigUnreadable`) instead of
      being replaced by just this preference.
    * The empty-file seed matches `save_context`'s, so a fresh config is born
      v2 rather than being mistaken for v1 on its next read.

    Imported lazily: this module renders on the status-line hot path, and
    vendored copies may not ship the writer at all -- reading the default must
    keep working there even though setting it cannot.
    """
    from probe.sdk.config import (
        CONFIG_VERSION,
        DEFAULT_CONTEXT,
        _config_lock,
        _load_json_file,
        _save_json_file,
        _write_target,
    )

    path = config_path()
    target = _write_target(path)
    with _config_lock(target, _resolved=True):
        data = _load_json_file(target, strict=True, migrate=True) or {
            "version": CONFIG_VERSION,
            "current_context": DEFAULT_CONTEXT,
            "contexts": {},
        }
        defaults = data.get(DEFAULTS_KEY)
        data[DEFAULTS_KEY] = defaults if isinstance(defaults, dict) else {}
        data[DEFAULTS_KEY][TRACKING_DEFAULT_KEY] = "on" if on else "off"
        # BOTH KEYS, OR THIS WRITE IS A NO-OP. Readers prefer `session_state`,
        # so leaving a stale `full` there while this sets `session_tracking: off`
        # means the wizard reports tracking off and every new session keeps
        # recording. False maps to read-only -- what this boolean has always
        # meant: stop recording, keep searching.
        data[DEFAULTS_KEY][STATE_DEFAULT_KEY] = STATE_FULL if on else STATE_READ_ONLY
        _save_json_file(target, data, private=True, _resolved=True)
    return path


def write_default_state(state: str) -> Path:
    """Persist this machine's default STATE; returns the config path.

    The same key, the same file, the same locking as `write_default_tracking` --
    only the vocabulary is wider. One key, because it is one setting: a second
    key would let a machine hold two answers to "what does a new session start
    at" with nothing to say which won.

    An older client reading `read-only` here sees an unrecognized value and falls
    back to the SHIPPED default rather than to off, which is the documented rule
    (`default_tracking`) and the safe direction: a machine whose default is
    read-only records nothing under a new client, and records under an old one.
    That is visible on the dashboard. The reverse -- an old client reading it as
    a hard stop -- would silently lose work.
    """
    if state not in STATES:
        raise ValueError(f"expected one of {STATES}, got {state!r}")
    from probe.sdk.config import (
        CONFIG_VERSION,
        DEFAULT_CONTEXT,
        _config_lock,
        _load_json_file,
        _save_json_file,
        _write_target,
    )

    path = config_path()
    target = _write_target(path)
    with _config_lock(target, _resolved=True):
        data = _load_json_file(target, strict=True, migrate=True) or {
            "version": CONFIG_VERSION,
            "current_context": DEFAULT_CONTEXT,
            "contexts": {},
        }
        defaults = data.get(DEFAULTS_KEY)
        data[DEFAULTS_KEY] = defaults if isinstance(defaults, dict) else {}
        data[DEFAULTS_KEY][STATE_DEFAULT_KEY] = state
        # The compat projection, for clients that only know the old key.
        data[DEFAULTS_KEY][TRACKING_DEFAULT_KEY] = "on" if state == STATE_FULL else "off"
        _save_json_file(target, data, private=True, _resolved=True)
    return path


def write_folder_state_default(
    folder: str | os.PathLike[str], state: "str | None"
) -> Path:
    """Set this exact folder's default state; None removes the override."""
    if state is not None and state not in STATES:
        raise ValueError(f"expected one of {STATES}, got {state!r}")
    from probe.sdk.config import (
        ConfigUnreadable,
        _config_lock,
        _load_json_file,
        _save_json_file,
        _write_target,
    )

    path = folder_config_path(folder)
    target = _write_target(path)
    with _config_lock(target, _resolved=True):
        data = _load_json_file(
            target,
            strict=True,
            migrate=False,
            require_regular=True,
            max_bytes=FOLDER_CONFIG_MAX_BYTES,
        )
        defaults = data.get(DEFAULTS_KEY)
        if DEFAULTS_KEY in data and not isinstance(defaults, dict):
            raise ConfigUnreadable(
                f"{path} has a non-object {DEFAULTS_KEY}. Refusing to overwrite it."
            )
        if state is None:
            if isinstance(defaults, dict):
                defaults.pop(STATE_DEFAULT_KEY, None)
                defaults.pop(TRACKING_DEFAULT_KEY, None)
                if not defaults:
                    data.pop(DEFAULTS_KEY, None)
        else:
            if not isinstance(defaults, dict):
                defaults = {}
                data[DEFAULTS_KEY] = defaults
            defaults[STATE_DEFAULT_KEY] = state
            defaults[TRACKING_DEFAULT_KEY] = "on" if state == STATE_FULL else "off"
        _save_json_file(
            target,
            data,
            private=False,
            _resolved=True,
            max_bytes=FOLDER_CONFIG_MAX_BYTES,
        )
    return path


def configured(config: dict | None = None) -> bool:
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
    data = _read_config() if config is None else config
    if not data:
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


def _no_capture_clause(reason: str) -> str:
    """The `· no capture: …` suffix as it hangs off `_LABEL_TRACKING_BARE`.

    THE LAST STOP FOR THE WIDTH CAP. With no project name left to give up, the
    reason itself is elided. The vocabulary in `probe.cli.capture_state` is short
    enough that this never fires today, but this renderer reads whatever a
    refresh hook of ANY vintage wrote into the marker, and an unbounded field on
    a status line wraps the whole line — which reflows every other segment on it.
    """
    if not reason:
        return ""
    room = (
        MAX_SEGMENT_CHARS
        - len(_INDENT)
        - _GLYPH_WIDTH
        - len(_LABEL_TRACKING_BARE)
        - len(_LABEL_NO_CAPTURE)
    )
    return _LABEL_NO_CAPTURE + _elide(reason, room)


def render(
    state: dict | None,
    *,
    configured: bool,
    tracking: bool,
    live: bool = False,
    color: bool = True,
    session_state: "str | None" = None,
) -> str:
    """The status-line segment. One line, bounded, self-delimiting, or empty.

    TWO STATES OF THE SWITCH: tracking, or not. The caller resolves which via
    `is_tracking`; this only renders it. An earlier version carried a third —
    "tracking off" as something distinct from "untracked" — and that was a
    mistake. A reader does not care WHY nothing is being recorded, only whether
    anything is, and the third state made them decode a distinction that changed
    nothing they would do.

    `tracked, not captured` is a third state of a DIFFERENT question, and it
    earns its place by the same test the rejected one failed: it changes what the
    reader does. The work is landing — projects, runs, metrics, artifacts, all of
    it — and the CONVERSATION is not being recorded. Only the researcher can
    decide whether that matters for this session, and they cannot decide it
    without being told. It renders as a SUFFIX on the tracking segment, never as
    a replacement for it, because a line reading only "no capture" says the
    opposite of what is true.

    THE SEGMENT MUST SURVIVE ANY NEIGHBOUR. It shares one line with whatever else
    is chained into `statusLine`, so four properties are load-bearing:

    * **No newline, ever.** Claude Code splits this command's stdout on newlines
      and renders each as its own status row.
    * **A two-space indent and a glyph in front.** Output is concatenated with the
      neighbour's; without the gap `…main● tracking` fuses into one token.
    * **Bounded width.** Overflow wraps the line, reflowing every segment on it —
      the one way this makes other output worse rather than merely longer.
    * **One coloured character, always closed, never counted.** Only the dot is
      painted (`_paint` is the single place an SGR is emitted), and layout is
      computed on PLAIN text — measuring a coloured string counts invisible bytes.

    Empty when Probe is not configured on this machine: someone who does not use
    it should not spend a column being told so.
    """
    if not configured:
        return ""

    if not tracking:
        # THE SWITCH NOW HAS THREE POSITIONS AND TWO OF THEM ARE NOT RECORDING,
        # and the difference is the one thing a reader can act on: under
        # `read-only` an agent will still find prior work, under `off` it will
        # not and will not know what it missed. That passes the same test the
        # rejected third state failed and `no capture` passed -- it changes what
        # the reader does.
        #
        # `session_state=None` is a caller from before the third state. It gets
        # the two-state word it has always got rather than a guess.
        label = {
            STATE_READ_ONLY: _LABEL_READ_ONLY,
            STATE_OFF: _LABEL_OFF,
        }.get(session_state, _LABEL_NOT_TRACKING)
        return _INDENT + _paint(_DOT, _YELLOW, color) + " " + label

    # Read PAST the `not tracking` return above on purpose: capture is a fact
    # about a session that is recording, and naming it for one that is not would
    # be an answer to a question nobody asked.
    reason = _capture_reason(state)
    head = _INDENT + _paint(_DOT_DEGRADED if reason else _DOT, _GREEN, color) + " "

    project = state.get("project") if isinstance(state, dict) else None
    if not (isinstance(project, str) and project):
        # Tracking is on, nothing filed yet. State it without inventing a name.
        return head + _LABEL_TRACKING_BARE + _no_capture_clause(reason)

    if not reason:
        # THE NAME YIELDS, THE LABEL AND ACCENT DO NOT: `MAX_SLUG_CHARS` reserves
        # both widths whether or not the accent shows, so truncation only ever
        # costs characters of the project name.
        accent = _ACCENT_TEXT if live else ""
        return head + _LABEL_TRACKED + _elide(project) + accent

    # DEGRADED — WHAT YIELDS FIRST, AND WHY.
    #
    # The name, and it yields all the way to nothing. The reason is the only
    # actionable thing on the line — it names what to fix — and it is on screen
    # nowhere else; the project name is on the dashboard, in `probe session
    # status`, and usually in the previous turn's own output. `no capture:
    # interp…` names nothing to fix, so the reason is the LAST thing cut, and
    # only after the name has been given up entirely (`_no_capture_clause`).
    #
    # The live-run accent goes with the name, and hands the reason back its ten
    # columns. One qualifier per segment stays legible; `· running · no capture:
    # halted` reads as a list of unrelated facts, and the accent returns the
    # moment capture is healthy — which is also the moment it is worth reading.
    room = (
        MAX_SEGMENT_CHARS
        - len(_INDENT)
        - _GLYPH_WIDTH
        - len(_LABEL_TRACKED)
        - len(_LABEL_NO_CAPTURE)
        - len(reason)
    )
    if room >= _MIN_SLUG_CHARS_DEGRADED:
        return head + _LABEL_TRACKED + _elide(project, room) + _LABEL_NO_CAPTURE + reason
    return head + _LABEL_TRACKING_BARE + _no_capture_clause(reason)
