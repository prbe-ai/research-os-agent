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
  * THROTTLED. The network is hit at most once per TTL (default 24h) on success,
    and no more than once per BACKOFF (default 1h) after a failure — so an offline
    machine does not re-hit the network every session. A cache file stores
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
import time
import urllib.request


def _int_env(name: str, default: int) -> int:
    """Env int that never raises at import (a bad value falls back to default)."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


TTL = _int_env("PROBE_VERSION_TTL", 86400)          # reuse a good manifest this long
BACKOFF = _int_env("PROBE_VERSION_BACKOFF", 3600)   # min seconds between attempts after a failure
TIMEOUT = _float_env("PROBE_VERSION_TIMEOUT", 3.0)
DEFAULT_BASE = "https://api.research.prbe.ai"
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
    v = str(v).strip().split()[-1]          # "probe 0.7.0" -> "0.7.0"
    for sep in ("+", "-"):                   # 0.8.0-rc1 / 0.8.0+meta -> 0.8.0
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


def _valid_base(b: object) -> str | None:
    """Accept only an http(s) origin; reject file://, ftp://, etc."""
    if isinstance(b, str) and (b.startswith("https://") or b.startswith("http://")):
        return b.rstrip("/")
    return None


def _base_url() -> str:
    b = _valid_base(os.environ.get("PROBE_BASE_URL"))
    if b:
        return b
    cfg = os.environ.get("PROBE_CONFIG_PATH") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "probe", "config.json",
    )
    try:
        with open(cfg) as f:
            data = json.load(f)
        ctxs = data.get("contexts")
        if isinstance(ctxs, dict):  # v2 named contexts
            active = ctxs.get(data.get("current_context") or "default") or {}
            b = _valid_base(active.get("base_url"))
        else:  # flat v1
            b = _valid_base(data.get("base_url"))
        if b:
            return b
    except Exception:
        pass
    return DEFAULT_BASE


def _cache_path() -> str:
    return os.environ.get("PROBE_VERSION_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
        "probe", "version-check.json",
    )


def _read_cache(path: str):
    """Returns (manifest, fetched_at, ok). manifest is the last-good dict or None."""
    try:
        with open(path) as f:
            data = json.load(f)
        manifest = data.get("manifest")
        if not isinstance(manifest, dict):
            manifest = None
        return manifest, float(data.get("fetched_at", 0)), bool(data.get("ok", False))
    except Exception:
        return None, 0.0, False


def _write_cache(path: str, manifest, ok: bool) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {"fetched_at": int(time.time()), "ok": ok, "manifest": manifest}, f
            )
        os.replace(tmp, path)
    except Exception:
        pass


def _fetch(url: str) -> dict:
    """GET the manifest; raise unless it is a JSON object (so a bad 200 is treated
    as a failure and never cached as good)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 (http(s) only, see _valid_base)
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest is not a JSON object")
    return data


def _local_cli(probe_bin: str):
    try:
        out = subprocess.run(
            [probe_bin, "--version"], capture_output=True, text=True, timeout=5
        )
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


def main() -> None:
    cache = _cache_path()
    manifest, fetched_at, ok = _read_cache(cache)
    age = time.time() - fetched_at
    # Reuse a good manifest for TTL; after a failure, wait BACKOFF before retrying.
    if age >= (TTL if ok else BACKOFF):
        try:
            manifest = _fetch(_base_url() + "/v1/client-version")
            _write_cache(cache, manifest, True)
        except Exception:
            # Keep the last-good manifest (if any); record the attempt for backoff.
            _write_cache(cache, manifest, False)

    if not isinstance(manifest, dict):
        _emit({"continue": True})

    local = {
        "cli": _local_cli(os.environ.get("PROBE_BIN") or "probe"),
        "plugin": _local_plugin(os.environ.get("PROBE_PLUGIN_JSON") or ""),
    }

    nudges, below_min = [], []
    for key, label in (("cli", "CLI"), ("plugin", "plugin")):
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
    cmds = "probe update" if has_update_cmd else (
        "uv tool upgrade probe-research && "
        "claude plugin marketplace update research-os-agent && "
        "claude plugin update probe-research@research-os-agent"
    )
    advisory = manifest.get("advisory")

    if below_min:
        head = ("⚠ Probe Research is below the minimum supported version "
                f"({_fmt(below_min)}). Update now:")
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

    _emit({
        "systemMessage": sys_msg,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        },
    })


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.stdout.write('{"continue": true}')
        sys.exit(0)
