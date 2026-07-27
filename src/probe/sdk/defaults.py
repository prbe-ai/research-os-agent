"""Contextual defaults for a launch. Only ``default_run_name`` is still wired in.

``default_run_name`` remains the default for a run's name: naming a RUN has never
been the ambiguous part, and a timestamp is a fine answer.

``default_experiment_slug`` is wired into ``client.run()`` again: it derives the
slug from the git repo, then the running script, so a bare ``client.run()``
works. What made that dangerous was never the derivation — it was that deriving
also CREATED the experiment silently. Creating one now requires a hypothesis, so
a derived slug that does not exist stops with an error instead of filing work
under whatever directory you happened to be in.

``auto_hypothesis`` and ``AUTO_HYPOTHESIS_PREFIX`` have NO production caller and
are not coming back: the ``[auto]`` placeholder was first-write-wins, so it
became permanent unless a human noticed. Kept one release so an external caller
gets a deprecation rather than an AttributeError; they go next release.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .agent_session import detect_agent

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
    """Best-effort marker for the coding agent driving this process, if any.

    Detection lives in `agent_session.AGENTS` so this and session attribution
    cannot disagree about what counts as "running under Claude Code". Note the
    two differ on purpose downstream: an agent is worth NAMING in a hypothesis
    even when its transcripts are not captured, so this reports Cursor and
    Codex while `resolve_agent_session` deliberately does not.
    """
    spec = detect_agent()
    return spec.display if spec is not None else None


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
