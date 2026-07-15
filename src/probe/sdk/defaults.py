"""Opinionated first-run defaults: derive experiment / run-name / hypothesis from context.

W&B-style zero-decision launches without giving up the hypothesis-first model:
the experiment slug falls back to the git repo (then the running script), run
names are timestamped, and a brand-new experiment gets an explicitly-marked
``[auto]`` hypothesis composed from the same context. The auto hypothesis is a
placeholder by design — replace it with ``client.update_experiment(...)`` or
``probe experiment set ID --hypothesis '...'`` (first-write-wins only applies to
the create; PATCH always updates).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AUTO_HYPOTHESIS_PREFIX = "[auto]"

# Script stems that identify the interpreter/runner, not the experiment.
_GENERIC_STEMS = {"", "-", "-c", "python", "python3", "ipython", "ipykernel_launcher"}


def _git(cwd: str | None, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "adhoc"


def _script_stem() -> str | None:
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    return stem if stem not in _GENERIC_STEMS else None


def _agent_context() -> str | None:
    """Best-effort marker for the coding agent driving this process, if any."""
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "Claude Code session"
    if os.environ.get("CURSOR_TRACE_ID"):
        return "Cursor session"
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_THREAD_ID"):
        return "Codex session"
    return None


def default_experiment_slug(cwd: str | None = None) -> str:
    """git repo name -> running script stem -> "adhoc"."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top:
        return _slugify(Path(top).name)
    stem = _script_stem()
    return _slugify(stem) if stem else "adhoc"


def default_run_name(now: datetime | None = None) -> str:
    """Timestamped fallback; the backend additionally mints a petname short_id."""
    now = now or datetime.now(timezone.utc)
    return f"run-{now:%Y%m%d-%H%M%S}"


def auto_hypothesis(slug: str, cwd: str | None = None) -> str:
    """Compose a marked placeholder hypothesis from ambient context.

    Sources, in order of usefulness: git repo@branch, the launching script, and
    the coding-agent session (when detectable from the environment). Always
    prefixed with ``[auto]`` so readers and reviews can tell it apart from a
    researcher-stated hypothesis.
    """
    parts: list[str] = []
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top:
        repo = Path(top).name
        branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        parts.append(f"{repo}@{branch}" if branch and branch != "HEAD" else repo)
    stem = _script_stem()
    if stem:
        parts.append(f"{stem}.py" if not Path(sys.argv[0]).suffix else Path(sys.argv[0]).name)
    agent = _agent_context()
    if agent:
        parts.append(agent)
    context = ", ".join(parts) if parts else "no ambient context"
    return (
        f"{AUTO_HYPOTHESIS_PREFIX} Exploratory runs for '{slug}' ({context}). "
        "Replace with a real hypothesis: probe experiment set <id> --hypothesis '...'"
    )
