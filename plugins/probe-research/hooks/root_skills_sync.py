#!/usr/bin/env python3
"""Keep Probe's skills current in the agent's OWN skills folder.

Run from the SessionStart hook, under the system python, so it imports nothing
but the stdlib and the vendored core beside it. This is what makes the plugin
the delivery channel again: the plugin carries `agent-skills/`, and every
session start copies it into `~/.claude/skills/`, where Claude Code names each
skill without a plugin prefix. See `_root_skills_core.py` for why they are not
simply left in the plugin. A copy the install ledger says is NOT the installed
one (an older version folder, Codex's cache under a Codex session that inherited
Claude Code's environment) writes nothing: its text would only undo the
installed plugin's in every open session.

`root_skills_sync.py skill-call` is the same sync as the PreToolUse route on
Claude Code's Skill tool (routes.json), for a session left open across a plugin
update. Claude Code re-reads its skills folder when a file there changes, so an
open session reads whatever these copies say -- but only a session START
refreshed them, so after an update made from outside Probe (`/plugin update`,
`claude plugin update`) every open session kept the old text until some session
started. (`probe update` and the Probe service's updater refresh them right
after they update the plugin: `probe.cli.root_skills.refresh`.) Before a Skill
call on one of Probe's skills, this compares the copy with the installed
plugin's (the dispatcher has already handed the event to the installed copy,
`dispatch.hand_over`), and when it is behind, refreshes every copy and refuses
THIS call, asking for a fresh one. Refreshing alone is not enough: Claude Code
renders the call from the text it already loaded, whatever the hook wrote
(measured on 2.1.296: a rewrite in PreToolUse, even with a 2 s wait inside the
hook, still served the old text), and the file watcher brings the new text in
about a second later -- in time for the retry, not for this call. A skill the
person TYPES (`/probe off`) makes no Skill call: it reads what the last refresh
left (the updater's, a session start's, or an earlier Skill call's).

Bounded both ways. The route writes only from the installed copy, and not at all
while another plugin that keeps these same copies is installed too (the other
Probe profile, left over from a Who-records switch): two profiles each putting
back their own `probe` refused every call to it, for good. And it refuses at most
once per session, skill and installed copy (`REFUSED_DIR`), whatever else
rewrites the copies in between.

Codex needs none of this: it lists a plugin's skills again on every turn, from
the version folder its cache holds now, and reads a `$skill`'s file when it is
invoked (verified with codex-cli 0.163.0: after `codex plugin add` replaced
1.0.0 with 1.0.1, the next turn listed and injected 1.0.1's SKILL.md).

Silent and fail-open by contract, like every other SessionStart step: a skill
that could not be copied must never stop a session from starting, and a Skill
call is refused only when this call made its copy current.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness_registry  # noqa: E402
import _root_skills_core as core  # noqa: E402

#: The argument routes.json's PreToolUse route on the Skill tool passes.
SKILL_CALL = "skill-call"
#: The tool that route is on (its matcher, `^Skill$`).
SKILL_TOOL = "Skill"

#: How long Claude Code takes to load a skill file that changed: its watcher
#: waits for the file to hold still for 1 s (checked every 0.5 s), then debounces
#: the reload 0.3 s (Claude Code 2.1.296). The refusal goes out after this, so
#: the agent's next call finds the new text already loaded. Only a call that
#: found its copy behind waits.
WATCHER_SETTLE_S = 2.0

#: Under `$XDG_STATE_HOME/probe/hooks/`: one empty file per refusal, named for the
#: session, the skill and the installed copy, so each is refused at most once.
REFUSED_DIR = "skill-refreshed"
#: Older ones are removed when a refusal is recorded.
REFUSED_KEEP_S = 14 * 24 * 3600


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


def _dispatcher():
    """The dispatcher beside us (it reads the install ledger): the one already
    loaded when it is ours (an in-process route), else ours by explicit path."""
    here = os.path.dirname(os.path.abspath(__file__))
    loaded = sys.modules.get("dispatch")
    if loaded is not None and os.path.dirname(os.path.abspath(getattr(loaded, "__file__", "") or "")) == here:
        return loaded
    import importlib.util

    spec = importlib.util.spec_from_file_location("_root_skills_dispatch", os.path.join(here, "dispatch.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _installed_alone(dispatcher, plugin_root: Path) -> bool:
    """This folder is THE installed copy of its plugin, per the ledger read now,
    and no other installed plugin keeps the same copies (`_shares_the_copies`)."""
    copies = dispatcher.installed_copies(os.environ) or {}
    return dispatcher.is_installed_copy(str(plugin_root), os.environ, copies) and not _shares_the_copies(
        copies, plugin_root
    )


def _listed(plugin_root: Path, dispatcher) -> "set | None":
    """The folders the install ledger names for this plugin, read now (None:
    not listed, or no readable ledger)."""
    copies = dispatcher.installed_copies(os.environ)
    return (copies or {}).get(dispatcher.ledger_key(str(plugin_root)))


def superseded(plugin_root: Path, dispatcher=None, was_listed: bool = False) -> bool:
    """Is this copy no longer the one Claude Code loads? Then writing its text
    would undo the installed one's. Superseded: the ledger names ANOTHER folder
    for this plugin, or `was_listed` (this copy WAS installed when first asked)
    and the ledger no longer names it at all (the plugin was removed: a
    Who-records switch to the other profile). A plugin the ledger never listed
    (a `--plugin-dir` checkout) or no readable ledger: not superseded."""
    listed = _listed(plugin_root, dispatcher or _dispatcher())
    if not listed:
        return was_listed
    return os.path.realpath(plugin_root) not in listed


def _shares_the_copies(copies: dict, plugin_root: Path) -> bool:
    """Is another plugin installed that keeps these same copies (it ships this
    hook): the other Probe profile? Each would put back its own text."""
    mine = os.path.realpath(plugin_root)
    name = os.path.basename(__file__)
    return any(
        path != mine and os.path.isfile(os.path.join(path, "hooks", name))
        for paths in copies.values()
        for path in paths
    )


def called_skill(payload: dict) -> str:
    """The skill a Skill tool call names, as its folder would be named, or "".

    A `plugin:skill` name is a plugin's own skill, never one of these copies."""
    if not isinstance(payload, dict) or payload.get("tool_name") != SKILL_TOOL:
        return ""
    tool_input = payload.get("tool_input")
    name = tool_input.get("skill") if isinstance(tool_input, dict) else None
    if not isinstance(name, str):
        return ""
    name = name.strip().lstrip("/")
    name = name.split()[0] if name else ""
    return "" if ":" in name or "/" in name or name.startswith(".") else name


def _source_slug(name: str, wanted: "tuple[str, ...]") -> "str | None":
    """Which of the plugin's skills the folder `name` holds: itself, or the
    one a collision installed as `probe-<slug>`. None: the plugin has none."""
    if name in wanted:
        return name
    if name.startswith(core.COLLISION_PREFIX) and name[len(core.COLLISION_PREFIX):] in wanted:
        return name[len(core.COLLISION_PREFIX):]
    return None


def _refused_path(payload: dict, name: str, plugin_root: Path) -> Path:
    import hashlib

    session = str(payload.get("session_id") or "")
    key = "\0".join((session, name, os.path.realpath(plugin_root)))
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "probe" / "hooks" / REFUSED_DIR / hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:32]


def _first_refusal(path: Path) -> bool:
    """Record this refusal; False when it was recorded before (let the call run)."""
    if path.exists():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        # Unrecorded, so this could repeat: let the call run instead.
        return False
    cutoff = time.time() - REFUSED_KEEP_S
    try:
        old_ones = list(path.parent.iterdir())
    except OSError:
        old_ones = []
    for old in old_ones:
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            # Another session pruned it first.
            continue
    return True


def _refusal(name: str, plugin_root: Path, removed: bool) -> str:
    plugin = core.plugin_name(plugin_root)
    version = plugin_root.name if plugin_root.name != plugin else ""
    which = f"{plugin} {version}".strip()
    if removed:
        reason = (
            f"The installed Probe plugin ({which}) no longer has the `{name}` skill, so its old copy "
            f"was removed. Carry on without it."
        )
    else:
        reason = (
            f"The `{name}` skill had changed in the installed Probe plugin ({which}) and its copy here "
            f"was behind; it has just been updated. This call was stopped so the old instructions are "
            f"not used: call the Skill tool for `{name}` again to load the new ones."
        )
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }})


def skill_call(payload: dict, sleep=time.sleep) -> "str | None":
    """Before a Skill call: the refusal to print when this call had to bring
    Probe's copy of the skill up to the installed plugin, else None (let it run).

    Acts only for a folder of ours (`.probe-installed`), only when this plugin
    folder IS the installed one (`dispatch.is_installed_copy`) and no other
    installed plugin keeps the same copies: a session on an older copy that
    could not hand over (a project-scope install, a `--plugin-dir` checkout)
    must never write its older text over the copies every other session reads.
    Refuses at most once per session, skill and installed copy."""
    name = called_skill(payload)
    if not name:
        return None
    root = _skills_root()
    if root is None:
        return None
    dst = root / name
    if not core.is_ours(dst):
        return None
    plugin_root = _plugin_root()
    wanted = core.slugs(plugin_root)
    if not wanted:
        return None
    slug = _source_slug(name, wanted)
    if slug is not None and core.is_current(plugin_root / core.PLUGIN_SKILLS_DIR / slug, dst):
        return None
    dispatcher = _dispatcher()
    if not _installed_alone(dispatcher, plugin_root):
        return None
    # Asked again inside the copy lock: an update can land between here and there.
    core.apply(root, plugin_root, wanted, still_installed=lambda: _installed_alone(dispatcher, plugin_root))
    if slug is None:
        made_current = not (dst.exists() or dst.is_symlink())
    else:
        made_current = core.is_current(plugin_root / core.PLUGIN_SKILLS_DIR / slug, dst)
    if not made_current:
        # Another session holds the sync, or the write failed: let the call run
        # on whatever is loaded rather than refuse it with nothing fixed.
        return None
    if not _first_refusal(_refused_path(payload, name, plugin_root)):
        return None
    sleep(WATCHER_SETTLE_S)
    return _refusal(name, plugin_root, removed=slug is None)


def main(argv: "list[str] | None" = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == [SKILL_CALL]:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            return 0
        out = skill_call(payload)
        if out:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
        return 0
    root = _skills_root()
    if root is None:
        return 0
    plugin_root = _plugin_root()
    wanted = core.slugs(plugin_root)
    if not wanted:
        return 0
    dispatcher = _dispatcher()
    first = _listed(plugin_root, dispatcher)
    if first and os.path.realpath(plugin_root) not in first:
        return 0
    # Asked again inside the copy lock: an update can land between here and
    # there, and a copy that was installed must still be (not just "unlisted").
    was_listed = bool(first)
    core.apply(
        root, plugin_root, wanted,
        still_installed=lambda: not superseded(plugin_root, dispatcher, was_listed=was_listed),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail-open: the session matters more than the skills being fresh.
        sys.exit(0)
