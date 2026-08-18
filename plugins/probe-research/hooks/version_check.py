#!/usr/bin/env python3
"""Probe Research version check. Runs on SessionStart AND on PreCompact.

Prints a Claude Code hook JSON to stdout:
  - up to date, or no data        -> {"continue": true}
  - a newer version is available   -> {"systemMessage": ...,
                                       "hookSpecificOutput": {additionalContext}}

WHY PRECOMPACT TOO.

SessionStart was the only trigger, and it fires once per session. That is fine
for anyone who opens and closes sessions through the day, and useless for the
researcher this tool is built for: they keep a handful of sessions alive for
weeks, and their Probe work goes through the HOSTED MCP, which runs no code on
their machine at all. Such a laptop can sit months behind with a perfectly
healthy updater that simply never gets an occasion to run.

PreCompact is the occasion. It fires when a session's context is compacted,
which is a thing that only happens to long-lived sessions -- so it targets
exactly the population SessionStart misses, and stays quiet for everyone else.

ON PRECOMPACT THIS HOOK IS SILENT, and that is deliberate on two counts. The
output contract is not the same one (`hookSpecificOutput.hookEventName` is
`SessionStart` below, and additionalContext is that event's channel), so
reusing this payload there would be emitting a shape for the wrong event.
And a nudge injected mid-compaction is noise at the worst possible moment --
the user did not do anything, the agent is busy, and the same message already
rendered when the session began. PreCompact exists here to APPLY, not to talk:
it runs the same gate chain and spawns the same detached upgrade, then prints
`{"continue": true}` and gets out of the way.

Contract:
  * FAIL-OPEN. Any error prints {"continue": true} and exits 0 — a broken check
    never blocks a session. (session-start.sh is the outer backstop.)
  * SYNCHRONOUS. The comparison finishes before we print, because the
    systemMessage is only delivered if it is in this hook's stdout.
  * THROTTLED. The network is hit at most once per TTL (default 15m) on success,
    and no more than once per BACKOFF (default 1h) after a failure — so an offline
    machine does not re-hit the network every session. BACKOFF stays longer than
    TTL on purpose: retrying something that just failed should be less eager than
    refreshing something that worked. A cache file stores
    {fetched_at, ok, manifest}; within TTL we compare against the cached manifest
    (no network) so the nudge still renders every session until the user upgrades.
    A failed/invalid fetch keeps the last-good manifest (never evicts it) and
    records the attempt so the backoff applies.

Resolution order for the API origin mirrors the CLI (sdk.config.resolve):
  PROBE_BASE_URL env  ->  ~/.config/probe/config.json base_url  ->  hosted default,
  restricted to http(s) so a stray file://ftp:// origin can't be fetched.
Semver comparison prefers packaging.version and falls back to a normalized
numeric-triplet compare (handles 0.8 vs 0.8.0 and ignores pre-release/build
suffixes) when packaging is not importable in the system python.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import NoReturn

# pathlib, time and urllib left with the helpers that moved to version_policy.
# The shared policy: TTL/BACKOFF, the cache and state paths, the cache reader and
# writer, and the fetch. `make sync-plugin-policy` copies it here from
# src/probe/version_policy.py and tests/test_policy_sync.py fails if the copies
# drift. This is a sibling import -- sys.path[0] is this script's directory when
# session-start.sh runs `python3 <plugin_root>/hooks/version_check.py` -- because
# the system python3 has no probe package to import from.
#
# Three of the values below used to be defined here as well as in the CLI. The
# autoupdate STATE path was the dangerous one: this file recomputed what
# autoupdate.py owned, and a divergence would have stopped auto-update while
# `probe doctor` kept reporting it healthy.
import version_policy

# The vendored session marker (same sibling-import mechanics as version_policy;
# `make sync-session-marker` keeps it byte-identical to the SDK copy). Imported
# at module load, where sys.path[0] is guaranteed to be the hooks directory --
# an import deferred into _tracking_off() would resolve against whatever path
# the caller happens to have.
import _session_marker

TTL = version_policy.TTL
BACKOFF = version_policy.BACKOFF
TIMEOUT = version_policy.TIMEOUT
DEFAULT_BASE = version_policy.DEFAULT_BASE


# The CLI release that introduced `probe update`. The nudge points at that one
# command only for CLIs >= this; older ones get the raw commands (which get them
# to a version that has it). CI keeps this == the released version (see release.yml).
UPDATE_CMD_MIN_CLI = "0.8.1"

# Which hook event we are running under. hooks.json exports this ONLY for
# PreCompact, so an unset value means SessionStart -- the reading that preserves
# the old behaviour, which matters because an older hooks.json can ship alongside
# a newer copy of this file (the plugin and CLI version independently).
HOOK_EVENT_ENV = "PROBE_HOOK_EVENT"
PRECOMPACT = "precompact"

# session-start.sh parses SessionStart's stdin and exports the `source` field
# here. "compact" means this session just lost everything that was not written
# down -- the one moment a standing instruction can still recover it, because
# PreCompact has no context channel and no agent turn runs between the event
# and the summarizer.
SESSION_SOURCE_ENV = "PROBE_SESSION_SOURCE"
COMPACT_SOURCE = "compact"
RESUME_SOURCE = "resume"
# The session id, parsed from the same stdin by session-start.sh. It keys the
# tracking signal (`probe session untrack` -> sessions/<id>.tracking = off),
# which is the researcher's DECLARATION that this conversation is not
# research. The declaration outlives the model's context -- the signal is a
# file, the skill text that carried it is summarizer fodder -- so the context
# boundary is exactly where a hook must re-assert it.
SESSION_ID_ENV = "PROBE_SESSION_ID"
# The nudge is CONDITIONAL on the domain ("the team's ML work") for the same
# reason the pointer block is: this hook is user-global, and an unconditional
# order to do Probe work in a dotfiles session teaches the agent to ignore the
# block. And it asks only for what the summary STILL SHOWS -- the compacted
# span itself is gone, and inviting its reconstruction would land invented
# decisions in team-visible notes as provenance. The event list mirrors the
# cadence prose in track-research-work's description and the pointer body.
COMPACT_CONTEXT = (
    "Context was just compacted. If this session involves the team's ML work, "
    "reconcile Probe before continuing: append what the summary still shows "
    "that is not yet recorded -- decisions, data processing steps, deletions, "
    "config changes, user overrides -- to the project's notes, and re-check "
    "the state of any open run. Do not reconstruct details the summary no "
    "longer carries. The probe-research:track-research-work skill has the "
    "current commands."
)
# What replaces the nudge when the researcher untracked this session. The
# toggle-research-tracking skill promises "no more tracking nudges", and before this
# branch existed the plugin itself broke that promise at the worst moment: a
# compaction rebuilt the model's context without the skill text, then this
# hook told the fresh context to reconcile Probe. Restating the contract is
# the fix in BOTH directions -- the nudge is suppressed, and the off state
# survives the boundary. Injected on resume too: same marker, same rebuilt
# context. Wording mirrors the skill; drift between them would have the two
# surfaces describing one state differently.
TRACKING_OFF_CONTEXT = (
    "Research tracking is OFF for this conversation -- this machine starts "
    "sessions untracked, or the researcher turned it off with "
    "/toggle-research-tracking. Do not create Probe "
    "projects, experiments or runs, do not write notes or Project Summary "
    "Markdown, and do not raise tracking at all, including as a reminder or a "
    "closing caveat. Reading Probe is still fine, and the actual work continues "
    "as normal. `/toggle-research-tracking on` turns tracking back on."
)


def _emit(obj: dict) -> NoReturn:
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def _seed_tracking_signal() -> None:
    """Give this session its STARTING tracking value, once, at SessionStart.

    One setting, one file. The signal is written here from the machine default
    so every reader -- the statusline, `probe session status`, the off contract
    below, tracking_guard's warn -- resolves the SAME value from the SAME
    place. Before this, an absent file meant "undecided" and each reader
    decided for itself what that was worth: the statusline resolved it against
    the default and rendered `off`, while this hook and the guard looked only
    for an explicit marker and so read a default-off machine as tracking. That
    is how a session showing `untracked` filled the dashboard with projects.

    Idempotent and never overrides a decision (`set_tracking_if_absent`), so a
    resume or post-compact start re-runs it harmlessly. Fail-soft: a seed that
    cannot be written leaves the file absent, which every reader now resolves
    against the same default anyway -- the seed makes the state explicit, it is
    not what makes it correct.
    """
    session_id = os.environ.get(SESSION_ID_ENV) or ""
    if not session_id:
        return  # PreCompact carries no id; the session was seeded at its start.
    try:
        _session_marker.set_tracking_if_absent(
            session_id, _session_marker.default_tracking()
        )
    except Exception:
        pass


def _tracking_off() -> bool:
    """Whether this session is untracked -- by declaration or by default.

    `is_tracking`, not the raw signal. A machine whose default is `off` HAS
    made the declaration: the default is the researcher choosing the value a
    new session starts at, which is the same setting the toggle flips, so
    honouring it only when someone re-typed it per session is honouring it
    nowhere. Doubt still reads False -- no id, invalid id, unreadable state --
    because an uncertain read must degrade to the nudge rather than to silence.
    """
    session_id = os.environ.get(SESSION_ID_ENV) or ""
    if not session_id:
        return False
    try:
        signal = _session_marker.tracking_signal(session_id)
        return not _session_marker.is_tracking(signal)
    except Exception:
        return False


def _start_context() -> str | None:
    """What to inject at a session start, or None.

    THE OFF CONTRACT GOES IN AT EVERY START, boundary or not. It used to be
    injected only on compact/resume, on the reasoning that those are where a
    declaration gets lost -- but a FRESH start was the one that carried nothing
    at all, and a fresh start is where an agent picks up a global instruction
    to register its work in Probe. The state was on the status line, which is
    chrome the model cannot read. So the model's first and only information
    about tracking was the instruction telling it to record, and it recorded:
    that is how an untracked session created a project while displaying
    `untracked`. A session must be TOLD, in the one channel it actually reads.

    Everything else is unchanged. A tracking session still gets the reconcile
    nudge after a compaction and silence on resume and on a fresh start --
    injecting a nudge there would be new nagging, not a fix.

    PreCompact runs this same file (see docstring) and must stay silent there:
    additionalContext is SessionStart's channel, and mid-compaction there is
    nobody left to read it.
    """
    if os.environ.get(HOOK_EVENT_ENV) == PRECOMPACT:
        return None
    if _tracking_off():
        return TRACKING_OFF_CONTEXT
    source = os.environ.get(SESSION_SOURCE_ENV)
    if source not in (COMPACT_SOURCE, RESUME_SOURCE):
        return None
    return COMPACT_CONTEXT if source == COMPACT_SOURCE else None


def _final(obj: dict) -> NoReturn:
    """Merge the post-compaction nudge into whatever main() was going to say.

    Every exit from main() goes through here so the nudge cannot be lost to an
    early return (up-to-date, no manifest, malformed cache). An update nudge
    already occupying additionalContext is appended to, never clobbered.
    """
    ctx = _start_context()
    if ctx:
        hso = obj.setdefault("hookSpecificOutput", {"hookEventName": "SessionStart"})
        prior = hso.get("additionalContext")
        hso["additionalContext"] = f"{prior}\n\n{ctx}" if prior else ctx
    _emit(obj)


def _ver_str(v: str) -> str:
    """Bare version for display: 'probe 0.7.0' -> '0.7.0'."""
    return str(v).strip().split()[-1] if v and str(v).strip() else str(v)


# The manifest is fetched over the network and parts of it are echoed into the
# session's systemMessage and the model-facing additionalContext. Comparison
# logic can take the strings as-is, but anything DISPLAYED must be shaped like
# a version -- a compromised or misconfigured origin must not get free
# instruction text into every session start.
_DISPLAY_VER = re.compile(r"[0-9A-Za-z.+~_-]{1,32}")


def _safe_ver(v) -> str:
    s = _ver_str(v)
    return s if isinstance(s, str) and _DISPLAY_VER.fullmatch(s or "") else "?"


def _triplet(v: str):
    """Normalized (major, minor, patch); ignores a leading token and any
    pre-release/build suffix. None if unparseable."""
    if not v:
        return None
    v = str(v).strip().split()[-1]  # "probe 0.7.0" -> "0.7.0"
    for sep in ("+", "-"):  # 0.8.0-rc1 / 0.8.0+meta -> 0.8.0
        v = v.split(sep, 1)[0]
    try:
        nums = [int(p) for p in v.split(".")]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def _remote_gt_local(local: str, remote: str) -> bool:
    """True iff remote is strictly newer than local."""
    try:
        from packaging.version import Version  # type: ignore

        return Version(str(remote)) > Version(str(local))
    except Exception:
        lp, rp = _triplet(local), _triplet(remote)
        if lp is None or rp is None:
            return False
        return rp > lp


# _valid_base / _base_url / _cache_path / _read_cache / _write_cache / _fetch all
# moved to version_policy, which the CLI shares. They are re-exported here under
# their old private names so the rest of this file (and its tests) read the same.
_valid_base = version_policy.valid_base
_base_url = version_policy.base_url
_fetch = version_policy.fetch


def _local_cli(probe_bin: str):
    try:
        out = subprocess.run([probe_bin, "--version"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return (out.stdout or "").strip() or None
    except Exception:
        return None
    return None


def _local_plugin(plugin_json: str):
    try:
        with open(plugin_json) as f:
            return json.load(f).get("version")
    except Exception:
        return None


def _local_tap():
    """The transcript tap's installed version, or None if it is not installed.

    Read from the tap's own state dir rather than Claude Code's plugin cache:
    the cache path is version-qualified (…/probe-research-tap/<version>/) so
    finding it means globbing and guessing which of several cached copies is
    live, while `.installed_version` is written by the tap's SessionStart hook
    and names the version that actually RAN. That is the one worth warning
    about — a cached-but-never-run copy has captured nothing.

    None (not installed / never run) is a normal answer, and main() skips any
    component whose local version is unknown, so users without the tap are
    never nudged about it.
    """
    if os.environ.get("PROBE_AGENT") == "codex":
        path = os.environ.get("PRBE_CODEX_TAP_PLUGIN_DIR")
        if not path:
            state = os.path.join(os.path.expanduser("~"), ".codex", "state")
            current = os.path.join(state, "probe-research-tap")
            legacy = os.path.join(state, "prbe-codex-tap-plugin")
            path = legacy if os.path.isdir(legacy) and not os.path.exists(current) else current
    else:
        path = os.environ.get("PROBE_RESEARCH_TAP_PLUGIN_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude", "plugins", "probe-research-tap"
        )
    try:
        with open(os.path.join(path, ".installed_version")) as f:
            return (f.read() or "").strip() or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Auto-update (opt-in via `probe setup`).
#
# The upgrade is spawned DETACHED and this hook returns immediately. The hook is
# synchronous by contract -- its systemMessage cannot come from a background
# process -- and `probe update` allows itself 300s, so applying inline would let
# a Claude Code session hang for up to five minutes before you could type.
# Nothing is lost by deferring: a plugin update only takes effect on restart
# anyway, so a background upgrade lands for the NEXT session either way.
#
# `probe update --yes` records its own outcome, which is the only way a detached
# run can report failure. `probe doctor` prints it.
# ---------------------------------------------------------------------------


def _autoupdate_settings() -> dict:
    """Read the opt-in state written by `probe setup`. Fail-soft to OFF.

    This used to recompute the state path that autoupdate.py owns. It now shares
    one definition -- the divergence that duplication invited would have stopped
    auto-update here while `probe doctor`, reading the other path, went on
    reporting it enabled with a months-old success.
    """
    return version_policy.read_state()


def _spawn_autoupdate(probe_bin: str) -> None:
    """Fire the upgrade and forget it. Never raises into the hook."""
    settings = _autoupdate_settings()
    if not settings.get("enabled"):
        return
    # No `--channel`: there is one channel, and the flag it used to pass did
    # nothing. Newer CLIs still ACCEPT it (hidden and ignored) because a plugin
    # updates on the user's schedule, so older copies of this file keep working.
    try:
        subprocess.Popen(  # noqa: S603 - resolved binary, no shell
            # `wizard --action update`, not the deprecated `probe update`.
            # Old CLIs do not have the wizard, so fall back below.
            [probe_bin, "wizard", "--action", "update", "--yes"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # survives this hook exiting
        )
    except (OSError, ValueError):
        pass  # fail-open: a broken auto-update must never block a session


def main() -> None:
    # Before anything else: a session must know its own tracking value from its
    # first moment, or the readers that fire earliest race the seed.
    _seed_tracking_signal()
    manifest, fetched_at, ok = version_policy.read_cache()
    # Reuse a good manifest for TTL; after a failure, wait BACKOFF before retrying.
    #
    # This hook is SYNCHRONOUS by contract and so it fetches INLINE, unlike the
    # CLI, which hands the refresh to a detached process. Its systemMessage only
    # reaches the session if it is in this process's stdout, so there is nothing
    # to hand off to.
    #
    # The fetch goes through the module-level `_fetch` (which IS
    # version_policy.fetch) rather than version_policy.refresh, so this file keeps
    # one substitutable seam for its own tests. What matters for correctness is
    # shared either way: the cache format, the TTL, and what `ok` means.
    if not version_policy.cache_is_fresh(fetched_at, ok):
        # Single-flight. If a CLI invocation is already refreshing, reuse what we
        # have instead of making the identical request a second time.
        if version_policy.claim_refresh():
            try:
                manifest = _fetch(_base_url() + version_policy.MANIFEST_PATH)
                version_policy.write_cache(manifest, True)
            except Exception:
                # Keep the last-good manifest; record the attempt for backoff.
                version_policy.write_cache(manifest, False)
            finally:
                version_policy.release_refresh()

    if not isinstance(manifest, dict):
        _final({"continue": True})

    local = {
        "cli": _local_cli(os.environ.get("PROBE_BIN") or "probe"),
        "plugin": _local_plugin(os.environ.get("PROBE_PLUGIN_JSON") or ""),
        "tap": _local_tap(),
    }

    nudges, below_min = [], []
    for key, label in (("cli", "CLI"), ("plugin", "plugin"), ("tap", "transcript tap")):
        info = manifest.get(key)
        if not isinstance(info, dict):  # a malformed field disables only that key
            continue
        latest, minv, cur = info.get("latest"), info.get("min"), local.get(key)
        if not cur or not latest:
            continue
        if _remote_gt_local(cur, latest):
            nudges.append((label, _safe_ver(cur), _safe_ver(latest)))
        if minv and _remote_gt_local(cur, minv):  # cur < min
            below_min.append((label, _safe_ver(cur), _safe_ver(minv)))

    if not nudges and not below_min:
        _final({"continue": True})

    def _fmt(items):  # items: (label, current, target)
        return ", ".join(f"{label} {cur} → {target}" for label, cur, target in items)

    # Prefer the single `probe update` command, but only for CLIs new enough to have
    # it; older CLIs get the raw sequence (which upgrades them to one that does).
    local_cli = local.get("cli")
    has_update_cmd = bool(local_cli) and not _remote_gt_local(local_cli, UPDATE_CMD_MIN_CLI)
    # The raw sequence updates the tap too when that is what is stale —
    # otherwise the nudge names a component and then hands over commands that
    # cannot fix it. `probe update` covers all three itself.
    tap_stale = any(label == "transcript tap" for label, _, _ in nudges + below_min)
    cmds = (
        "probe update"
        if has_update_cmd
        else (
            "uv tool upgrade probe-research && "
            "claude plugin marketplace update research-os-agent && "
            "claude plugin update probe-research@research-os-agent"
            + (" && claude plugin update probe-research-tap@research-os-agent" if tap_stale else "")
        )
    )
    advisory = manifest.get("advisory")

    if below_min:
        head = (
            "⚠ Probe Research is below the minimum supported version "
            f"({_fmt(below_min)}). Update now:"
        )
        summary = _fmt(below_min)
    else:
        head = f"⚠ Probe Research update available — {_fmt(nudges)}. Update:"
        summary = _fmt(nudges)

    sys_msg = f"{head} {cmds} (restart Claude Code to apply)."
    if isinstance(advisory, str) and advisory.strip():
        # Human-facing only, and bounded: one line, capped, so a hostile
        # manifest cannot paste paragraphs of instructions into the session.
        sys_msg += f" Note: {' '.join(advisory.split())[:200]}"

    ctx = (
        f"The Probe Research client is out of date ({summary}). If the user wants "
        "to update, tell them to run `uv tool upgrade probe-research` and "
        "`claude plugin update probe-research@research-os-agent`, then restart "
        "Claude Code. Do not nag; only act if they ask."
    )

    # An update exists. If the user opted in, apply it in the background; the
    # nudge below still renders this session, because the upgrade only takes
    # effect on the next one.
    _spawn_autoupdate(os.environ.get("PROBE_BIN") or "probe")

    # PreCompact applies and says nothing. See the module docstring: the payload
    # below is SessionStart's output contract, and a nudge delivered mid-compaction
    # interrupts work the user did not start to repeat a message they already saw
    # when the session opened. The spawn above is the whole reason PreCompact is
    # wired up, and it has already happened by this line.
    if os.environ.get(HOOK_EVENT_ENV) == PRECOMPACT:
        _final({"continue": True})

    _final(
        {
            "systemMessage": sys_msg,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": ctx,
            },
        }
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.stdout.write('{"continue": true}')
        sys.exit(0)
