"""Shared version-check policy: the constants, the paths, and the cache.

WHY THIS FILE EXISTS, AND WHY IT IS STDLIB-ONLY.

Two programs need this policy and they do not share an interpreter. The CLI runs
inside its own uv-tool venv. The plugin's SessionStart hook runs under the SYSTEM
python3 -- `session-start.sh` resolves `PY="$(command -v python3)"` -- so it can
never `import probe`. The only way one definition serves both is a file the plugin
ships its own copy of, which is why `make sync-plugin-policy` exists and why
tests/test_policy_sync.py fails the moment the copies drift.

Same contract as `skills/` -> `plugins/probe-research/skills/`: edit THIS file,
never the plugin copy.

WHAT WAS DUPLICATED BEFORE THIS MODULE.

Three values were written twice each:

  - TTL/BACKOFF          version_check.py only (the CLI had no cached fetch at all)
  - the cache path       version_check.py recomputed XDG_CACHE_HOME + probe/
  - the autoupdate STATE path
                         version_check.py recomputed what autoupdate.py owns

The third is the dangerous one. The hook reads that file to decide whether to
spawn an upgrade at all; if the two paths ever disagreed, auto-update would stop
running while `probe doctor` -- reading the other path -- kept reporting it as
enabled, with a `last_attempt` that had genuinely succeeded, months ago.

FILE FORMAT COMPATIBILITY.

Both JSON files here are read and written by INDEPENDENTLY VERSIONED programs. A
CI sync proves the two copies matched in the repo at release time; it proves
nothing about a user's machine, where the plugin and the CLI update on their own
schedules (see main.py's note on `--channel`). A new CLI meeting an old plugin's
copy of this module is the normal case, not the edge case. So:

  - additive fields ONLY -- never rename a key, never repurpose one
  - unknown keys are IGNORED, never an error
  - a missing key defaults to the reading that preserves OLD behaviour

`autoupdate.load()` already follows this informally for `plugin_ok`. It is written
down here because this change adds the first new field since, and the moment to
state the rule is the first field, not the third.

The cost of breaking it is quiet rather than loud: a reader that raises on an
unrecognized field turns a perfectly good cache into "no cache AND the last fetch
failed", so it refetches every single time AND applies the failure backoff. Slower
and noisier at once, with nothing in the logs to say why.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Policy constants. `PROBE_VERSION_TTL` remains the documented override; these
# are the DEFAULTS, which is the number that governs every real install because
# almost nobody sets the env var.
# ---------------------------------------------------------------------------


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


# 15 minutes, not a day. A day was chosen once to keep poll volume down, and the
# cost it actually bought was invisibility: four releases went out one afternoon
# and a machine that had cached the manifest that morning would have compared
# against a `latest` OLDER than what it already had -- concluded it was ahead,
# said nothing, and not looked again for another 21 hours.
#
# This TTL is also what makes the CLI trigger cheap. A training loop shelling out
# to `probe log` a thousand times in ten minutes performs ONE network fetch and
# 999 reads of a 150-byte file, because staleness -- not an invocation counter --
# is what gates the network.
TTL = _int_env("PROBE_VERSION_TTL", 900)
# Minimum seconds between attempts after a FAILED fetch. Longer than TTL on
# purpose: a machine that cannot reach the API should not retry every 15 minutes.
BACKOFF = _int_env("PROBE_VERSION_BACKOFF", 3600)
TIMEOUT = _float_env("PROBE_VERSION_TIMEOUT", 3.0)
# A refresh claim older than this is treated as abandoned. Comfortably longer
# than TIMEOUT so a slow-but-live fetch is never stolen from, and far shorter
# than TTL so an abandoned claim cannot suppress the next refresh cycle.
REFRESH_CLAIM_SECONDS = 30

DEFAULT_BASE = "https://api.research.prbe.ai"
MANIFEST_PATH = "/v1/client-version"

STATE_DIRNAME = "probe"
STATE_FILENAME = "autoupdate.json"
LOCK_FILENAME = "autoupdate.lock"
CACHE_FILENAME = "version-check.json"
REFRESH_LOCK_FILENAME = "version-refresh.lock"


# ---------------------------------------------------------------------------
# Paths. One definition each, for both interpreters.
# ---------------------------------------------------------------------------


def state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / STATE_DIRNAME


def state_path() -> Path:
    return state_dir() / STATE_FILENAME


def lock_path() -> Path:
    return state_dir() / LOCK_FILENAME


def cache_path() -> Path:
    """Honours `PROBE_VERSION_CACHE` first -- the tests set it, and so does
    anyone isolating a machine's check from its real cache."""
    override = os.environ.get("PROBE_VERSION_CACHE")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / STATE_DIRNAME / CACHE_FILENAME


def refresh_lock_path() -> Path:
    return cache_path().parent / REFRESH_LOCK_FILENAME


# ---------------------------------------------------------------------------
# Atomic writes.
# ---------------------------------------------------------------------------


def atomic_write_json(path: Path, payload: object) -> bool:
    """Write `payload` to `path` via a UNIQUE temp file plus `os.replace`.

    The temp name carries the pid and a monotonic-ish counter because a FIXED
    `.tmp` name -- which is what both writers used before -- is shared by every
    process writing that file. Two of them racing through one temp path can
    interleave a partial write, and a truncated read of the state file lands on
    `autoupdate.load()`'s fail-soft branch, which reads as "auto-update is OFF".
    A collision there does not merely lose a diagnostic; it silently disables
    the feature.

    Returns True on success. Never raises: a read-only filesystem or a full disk
    must degrade the version check, not break the command that triggered it.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            os.replace(tmp, path)
            return True
        except Exception:
            # Leave nothing behind on the failure path; a stray .tmp in the
            # state dir outlives the process that could explain it.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The autoupdate state file. `version_check.py` used to recompute this path and
# re-implement this read; both now come from here.
# ---------------------------------------------------------------------------


def read_state() -> dict:
    """The raw autoupdate state, or `{}` when unreadable.

    Fail-soft to empty rather than raising: an unreadable state file reads as
    "auto-update is off", because defaulting a background process that mutates
    an installed package to ON when we cannot tell what the user chose is the
    wrong direction to guess.
    """
    try:
        loaded = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def autoupdate_enabled() -> bool:
    """The cheapest gate in the chain, and deliberately first.

    `enabled` defaults False, so for every install that has not opted in this is
    the only file the version check touches on a CLI invocation.
    """
    return bool(read_state().get("enabled", False))


# ---------------------------------------------------------------------------
# The manifest cache.
# ---------------------------------------------------------------------------


def read_cache(path: Path | None = None) -> tuple[dict | None, float, bool]:
    """Returns `(manifest, fetched_at, ok)`; manifest is the last-good dict or None.

    Unknown keys in the file are ignored by construction -- we read the three we
    know and never inspect the rest. That is the forward-compatibility rule in
    this module's docstring, and it is why an old copy of this file keeps working
    against a cache written by a newer one.
    """
    target = path or cache_path()
    try:
        data = json.loads(target.read_text())
        if not isinstance(data, dict):
            return None, 0.0, False
        manifest = data.get("manifest")
        if not isinstance(manifest, dict):
            manifest = None
        try:
            fetched_at = float(data.get("fetched_at", 0))
        except (TypeError, ValueError):
            fetched_at = 0.0
        return manifest, fetched_at, bool(data.get("ok", False))
    except Exception:
        return None, 0.0, False


def write_cache(manifest: object, ok: bool, path: Path | None = None) -> bool:
    """Persist the manifest and whether the fetch that produced it succeeded.

    `ok` is a message one process leaves for the other: a False here switches
    every reader -- the hook and the CLI alike -- from TTL to BACKOFF. That is
    exactly why the fetch below is shared rather than reimplemented per surface.
    A second implementation with different proxy or timeout behaviour would write
    its own idea of "the network is broken" into a field the other one obeys.
    """
    return atomic_write_json(
        path or cache_path(),
        {"fetched_at": int(time.time()), "ok": bool(ok), "manifest": manifest},
    )


def cache_is_fresh(fetched_at: float, ok: bool, now: float | None = None) -> bool:
    """True when the cached manifest may still be used without refetching."""
    age = (time.time() if now is None else now) - fetched_at
    return age < (TTL if ok else BACKOFF)


# ---------------------------------------------------------------------------
# Fetch + single-flight.
# ---------------------------------------------------------------------------


def valid_base(candidate: object) -> str | None:
    """Accept only an http(s) origin; reject file://, ftp://, etc."""
    if isinstance(candidate, str) and (
        candidate.startswith("https://") or candidate.startswith("http://")
    ):
        return candidate.rstrip("/")
    return None


def base_url() -> str:
    """`PROBE_BASE_URL`, then the CLI config, then the hosted API.

    Reads BOTH `PROBE_CONFIG_PATH` and `XDG_CONFIG_HOME`, because the two
    surfaces that resolve probe config disagree about which one wins and they
    only agree in production (`~/.config/probe/config.json`). Honouring both
    here means this module cannot be the one that diverges.
    """
    found = valid_base(os.environ.get("PROBE_BASE_URL"))
    if found:
        return found
    config = os.environ.get("PROBE_CONFIG_PATH") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "probe",
        "config.json",
    )
    try:
        with open(config) as handle:
            data = json.load(handle)
        contexts = data.get("contexts")
        if isinstance(contexts, dict):  # v2 named contexts
            active = contexts.get(data.get("current_context") or "default") or {}
            found = valid_base(active.get("base_url"))
        else:  # flat v1
            found = valid_base(data.get("base_url"))
        if found:
            return found
    except Exception:
        pass
    return DEFAULT_BASE


def fetch(url: str) -> dict:
    """GET the manifest; raise unless it is a JSON object.

    A 200 carrying something that is not an object is treated as a FAILURE, so a
    misconfigured proxy returning an HTML login page can never be cached as a
    good manifest and compared against.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest is not a JSON object")
    return data


def claim_refresh() -> bool:
    """Single-flight: True for the ONE caller that should go to the network.

    Without this, "1 fetch per TTL" is only true for serial invocations. A sweep
    launching eight runs in the same second finds eight stale caches and spawns
    eight refreshers, all fetching the same 150-byte document. O_EXCL is the
    whole mechanism, the same as `autoupdate.acquire_lock`; a claim older than
    REFRESH_CLAIM_SECONDS is treated as abandoned so a killed refresher cannot
    suppress refreshes until the process table wraps.

    Callers do NOT need to release: the claim ages out on its own, and it is
    sized well under TTL so the next cycle is never blocked by a leaked one.
    """
    path = refresh_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        age = time.time() - path.stat().st_mtime
        if age > REFRESH_CLAIM_SECONDS:
            path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except (FileExistsError, OSError):
        return False
    try:
        os.write(handle, str(os.getpid()).encode())
    finally:
        os.close(handle)
    return True


def release_refresh() -> None:
    """Best-effort early release, so a fast refresh does not hold the claim for
    the full REFRESH_CLAIM_SECONDS."""
    try:
        refresh_lock_path().unlink(missing_ok=True)
    except OSError:
        pass


def refresh(base: str | None = None) -> dict | None:
    """Fetch and cache the manifest. Returns it, or None on failure.

    On failure the last-good manifest is RETAINED and only the `ok` flag flips,
    so a network blip degrades the check to "use what we have, back off before
    trying again" rather than to "we know nothing".
    """
    manifest, _, _ = read_cache()
    try:
        fetched = fetch((base or base_url()) + MANIFEST_PATH)
        write_cache(fetched, True)
        return fetched
    except Exception:
        write_cache(manifest, False)
        return None
