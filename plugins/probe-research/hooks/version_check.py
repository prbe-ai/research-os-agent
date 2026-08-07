#!/usr/bin/env python3
"""Probe Research SessionStart version check.

Prints a Claude Code SessionStart hook JSON to stdout:
  - up to date, or no data        -> {"continue": true}
  - a newer version is available   -> {"systemMessage": ...,
                                       "hookSpecificOutput": {additionalContext}}

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
import subprocess
import sys

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

TTL = version_policy.TTL
BACKOFF = version_policy.BACKOFF
TIMEOUT = version_policy.TIMEOUT
DEFAULT_BASE = version_policy.DEFAULT_BASE


# The CLI release that introduced `probe update`. The nudge points at that one
# command only for CLIs >= this; older ones get the raw commands (which get them
# to a version that has it). CI keeps this == the released version (see release.yml).
UPDATE_CMD_MIN_CLI = "0.8.1"


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def _ver_str(v: str) -> str:
    """Bare version for display: 'probe 0.7.0' -> '0.7.0'."""
    return str(v).strip().split()[-1] if v and str(v).strip() else str(v)


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
        _emit({"continue": True})

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
            nudges.append((label, _ver_str(cur), latest))
        if minv and _remote_gt_local(cur, minv):  # cur < min
            below_min.append((label, _ver_str(cur), minv))

    if not nudges and not below_min:
        _emit({"continue": True})

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
        sys_msg += f" Note: {advisory}"

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

    _emit(
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
