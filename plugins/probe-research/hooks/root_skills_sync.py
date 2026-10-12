#!/usr/bin/env python3
"""Keep Probe's skills current in the agent's OWN skills folder.

Run from the SessionStart hook, under the system python, so it imports nothing
but the stdlib and the vendored core beside it. This is what makes the plugin
the delivery channel again: the plugin carries `agent-skills/`, and every
session start copies it into `~/.claude/skills/`, where Claude Code names each
skill without a plugin prefix. See `_root_skills_core.py` for why they are not
simply left in the plugin.

Silent and fail-open by contract, like every other SessionStart step: a skill
that could not be copied must never stop a session from starting.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness_registry  # noqa: E402
import _root_skills_core as core  # noqa: E402


def _skills_root() -> Path | None:
    """The running harness's own skills folder, or None where it has none.

    Asked of the registry beside us rather than named here: both plugins ship
    this hook, it fires under every harness, and only the one with
    `root_skills` wants these files. Codex and Kimi read the plugin's
    `agent-skills/` by manifest key, so writing into their homes would
    duplicate rather than move. `home_dir` honours the override variable, the
    same rule `agent_rules.memory_path` follows: a machine that moved its
    config must not get files written to the real home.
    """
    harness = _harness_registry.get_registry().detect()
    if harness is None or not harness.can(_harness_registry.CAPABILITY_ROOT_SKILLS):
        return None
    home = harness.home_dir()
    return None if home is None else home / "skills"


def _plugin_root() -> Path:
    """This plugin's directory.

    The env vars first, then OUR OWN LOCATION. `session-start.sh` computes
    `PLUGIN_ROOT` but never exports it, so only Claude Code's
    `CLAUDE_PLUGIN_ROOT` actually reaches here -- an install that wires these
    hooks without plugin-scoped env got no skills at all, silently, and the
    plugin no longer carries them either. We are inside `<plugin>/hooks/`, so
    the answer is always on hand.
    """
    for name in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "KIMI_PLUGIN_ROOT"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return Path(value)
    return Path(__file__).resolve().parents[1]


def main() -> int:
    root = _skills_root()
    if root is None:
        return 0
    plugin_root = _plugin_root()
    wanted = core.slugs(plugin_root)
    if not wanted:
        return 0
    core.apply(root, plugin_root, wanted)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail-open: the session matters more than the skills being fresh.
        sys.exit(0)
