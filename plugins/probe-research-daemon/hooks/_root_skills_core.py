"""Probe's skills in the agent's OWN skills folder, so they carry no plugin prefix.

Claude Code names a skill in a plugin `<plugin>:<slug>`; the same skill in
`~/.claude/skills/<slug>/` is just `<slug>`. gstack's ~50 skills live there,
which is why its are `/plan-eng-review` and ours were
`/probe-research-daemon:plan-research-review` (Richard, 2026-10-12).

MOVED, NEVER DUPLICATED. Claude Code does not dedupe: with the plugin also
shipping them a session lists BOTH spellings, and the always-loaded catalog
carries every description twice. So the plugins keep their skills in
`agent-skills/` -- Claude Code scans only `skills/` and has no manifest key to
opt out (verified: with the folder renamed the plugin still loads its hooks and
contributes zero skills). Codex and Kimi name their folder explicitly, so they
read `agent-skills/` and nothing about them changes; pi uses its package
manifest and already names skills bare.

STDLIB ONLY, AND VENDORED BESIDE THE HOOKS (`_root_skills_core.py`,
`make sync-root-skills-core`, pinned by tests/test_root_skills_core_parity.py).
The session-start hook is what keeps these copies current, and it runs under the
SYSTEM python with no `probe` importable -- the same reason `_session_marker.py`
and `version_policy.py` are vendored. Doing it in the hook also means a new
plugin works with ANY CLI version: if this needed a new CLI subcommand, every
machine whose plugin updated first would sit with no skills at all until its CLI
caught up.

THE PLUGIN REMAINS THE DELIVERY CHANNEL. These are copies of what the installed
plugin carries. Owning the files without refreshing them from the plugin would
repeat the `POINTER_VERSION` mistake: a file no release can reach goes stale the
day the wording changes and stays stale forever.

WE NEVER TOUCH A FOLDER WE CANNOT PROVE IS OURS. `probe`, `track-work` and
`edit-notes` are generic names in a namespace someone else may already use
(gstack's own rule, for the same reason). Ours carry a marker file; a foreign
folder of that name is left alone and the skill installs as `probe-<slug>`.
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

#: Dropped in every folder we create, so a later pass can tell our copy from a
#: researcher's own skill of the same name. Its body names where to edit
#: instead, for whoever finds the folder and wonders.
MARKER = ".probe-installed"

#: What a collision installs as, rather than overwriting someone else's skill.
COLLISION_PREFIX = "probe-"

#: The plugin folder Claude Code must not scan. Codex and Kimi name it in their
#: manifests (`"skills": "./agent-skills/"`).
PLUGIN_SKILLS_DIR = "agent-skills"


def is_ours(folder: Path) -> bool:
    """A symlink is never ours, whatever it points at: `rmtree` refuses one, and
    a researcher who symlinked their own copy of a skill in means it to stay."""
    return not folder.is_symlink() and (folder / MARKER).is_file()


def _foreign(path: Path) -> bool:
    """Exists and is not provably ours -- so we must not write it or delete it."""
    return (path.is_symlink() or path.exists()) and not is_ours(path)


def _content_matches(src: Path, dst: Path) -> bool:
    """Whether `dst` holds exactly `src`'s files and bytes, marker aside."""
    if dst.is_symlink() or not dst.is_dir():
        return False
    want = {p.relative_to(src): p for p in src.rglob("*") if p.is_file()}
    have = {p.relative_to(dst): p for p in dst.rglob("*") if p.is_file() and p.name != MARKER}
    if set(want) != set(have):
        return False
    return all(want[rel].read_bytes() == have[rel].read_bytes() for rel in want)


def _same(src: Path, dst: Path) -> bool:
    """Whether `dst` already holds `src`, so a refresh can skip the write.

    Compares the file list and the bytes. The session-start hook runs this on
    every start, and rewriting a folder a session may be reading from is both
    pointless and a way to hand a half-written SKILL.md to a live agent.
    """
    return is_ours(dst) and _content_matches(src, dst)


def is_current(src: Path, dst: Path) -> bool:
    """Whether `dst` is our copy of `src` exactly as `apply` leaves it: marked
    ours, the same files, the same bytes. A copy a plugin update left behind
    reads False (`root_skills_sync.py skill-call` asks before a Skill call)."""
    return _same(src, dst)


def plugin_name(plugin_root: Path) -> str:
    """The plugin's NAME, not its version.

    Claude Code installs to `.../cache/<marketplace>/<plugin>/<version>/`, so
    the basename is the version: the marker shipped reading
    `"source": "0.130.0"`, which tells whoever finds the folder nothing about
    where it came from, which is the field's only job.
    """
    name = plugin_root.name
    if name and all(ch.isdigit() or ch == "." for ch in name):
        return plugin_root.parent.name or name
    return name


def slugs(plugin_root: Path) -> tuple[str, ...]:
    """The skills this plugin carries. The ONE place that answers it, so the
    CLI and the hook cannot disagree about what a profile contains."""
    src = Path(plugin_root) / PLUGIN_SKILLS_DIR
    return tuple(sorted(p.parent.name for p in src.glob("*/SKILL.md"))) if src.is_dir() else ()


def _write(dst: Path, src: Path, slug: str, plugin: str) -> None:
    """Put the plugin's copy of `slug` at `dst`, marked, in ONE visible step.

    THE MARKER IS WRITTEN BEFORE THE FOLDER TAKES ITS REAL NAME, and the switch
    is a rename. Copying in place and marking afterwards left a window --
    a killed hook, a full disk, two sessions starting together -- where our own
    folder existed UNMARKED. `is_ours` then read false forever, so the next pass
    treated it as the researcher's: a stale `probe` that no release could reach
    plus a duplicate `probe-probe`, and nothing ever healed it (reproduced).

    The plugin's copy is already the main agent's render with no `versions/`
    (`make sync-plugin-skills` does that), so this is a plain copy: the writer's
    version of `track-work` names a daemon-only option and must never land here.
    """
    staging = dst.parent / f".{dst.name}.probe-new"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src, staging)
    (staging / MARKER).write_text(
        json.dumps({"source": plugin, "skill": slug, "edit": str(src)}) + "\n",
        encoding="utf-8",
    )
    if dst.is_symlink() or dst.exists():
        # A symlink is never ours (see `prune`), so this only runs for a folder
        # `apply` already proved is ours or is being replaced by rename.
        dst.unlink() if dst.is_symlink() else shutil.rmtree(dst)
    os.replace(staging, dst)


#: How long `apply` waits for another writer to finish before it gives up. A
#: pass takes milliseconds; this only bounds a stuck or very slow one.
LOCK_WAIT_S = 1.0
_LOCK_POLL_S = 0.05


@contextmanager
def _one_writer(root: Path, wait_s: float = LOCK_WAIT_S):
    """Serialize `apply` per skills folder, so two writers cannot interleave.

    Several sessions starting together is the normal case on a shared box
    (`/loop`, `claude -p` fan-out, restored tmux panes). Interleaved, each racer
    computed its own keep-set and `prune` deleted the folders the others had
    just written -- a session left with no `probe` skill at all, which is the
    consent and off switch (reproduced with six concurrent callers).

    A writer that finds the lock held WAITS for it, up to `wait_s`: the holder
    may be writing an older copy's text (a session start on v1 while the
    updater refreshes v2), so leaving the work to it left v1's text in place.
    Waiting costs nothing when the writers agree: the second pass finds the
    bytes already there and writes nothing. Fail-open both ways: no `fcntl`
    (Windows) runs unlocked, and a lock still held after `wait_s` skips the pass.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield True
        return
    import time

    root.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        handle = open(root / ".probe-sync.lock", "a+")  # noqa: SIM115
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(_LOCK_POLL_S)
        yield True
    except OSError:
        yield True
    finally:
        if handle is not None:
            handle.close()


def apply(
    skills_root: Path,
    plugin_root: Path,
    wanted: "tuple[str, ...] | list[str]",
    still_installed: "Callable[[], bool] | None" = None,
) -> dict:
    """Make `skills_root` hold EXACTLY `slugs` of ours, from `plugin_root`.

    EXACTLY, because the two install profiles differ in what the agent must NOT
    be told: the agent profile ships `track-work`, the daemon profile does not,
    and a `track-work` left behind across a Who-records switch would have the
    agent recording inline while the daemon records the same session.

    Returns `{"installed": [...], "refreshed": [...], "removed": [...],
    "renamed": [...], "notes": [...]}`. Never raises for a missing source: an
    older plugin beside a newer CLI still has `skills/`, which that plugin's own
    sessions list themselves, and writing from a guess would be worse.

    `still_installed`: asked again once the lock is held (the caller's own
    "is this copy the installed one?", reading the install ledger afresh). A
    writer checks before it reaches the lock, and an update can land in
    between: a session start on v1 that was held up while the updater installed
    and refreshed v2 then wrote v1's text back over v2's (reproduced). False:
    nothing is written.
    """
    out: dict[str, list[str]] = {
        "installed": [], "refreshed": [], "removed": [], "renamed": [], "notes": [],
    }
    src_root = Path(plugin_root) / PLUGIN_SKILLS_DIR
    if not src_root.is_dir():
        out["notes"].append(f"{Path(plugin_root).name} has no {PLUGIN_SKILLS_DIR}/; left the skills folder alone")
        return out

    root = Path(skills_root)
    plugin = plugin_name(Path(plugin_root))
    with _one_writer(root) as mine:
        if not mine:
            out["notes"].append("another session is installing Probe's skills; left them to it")
            return out
        if still_installed is not None and not still_installed():
            out["notes"].append(f"{plugin} is no longer the installed copy; left the skills folder alone")
            return out
        keep = set()
        for slug in wanted:
            src = src_root / slug
            if not (src / "SKILL.md").is_file():
                continue
            dst = root / slug
            if _foreign(dst) and not _content_matches(src, dst):
                # Theirs, and not our bytes: leave it exactly as it is.
                dst = root / f"{COLLISION_PREFIX}{slug}"
                if _foreign(dst) and not _content_matches(src, dst):
                    # Even the fallback name is theirs. Two folders of theirs is
                    # not a reason to delete one, so this skill goes uninstalled
                    # and says so, rather than writing over their work.
                    out["notes"].append(
                        f"{slug} and {dst.name} are both skills of yours; did not install ours"
                    )
                    continue
                if not _same(src, dst):
                    _write(dst, src, slug, plugin)
                    out["renamed"].append(dst.name)
                    out["notes"].append(f"{slug} is already a skill of yours; installed ours as {dst.name}")
            elif _same(src, dst):
                pass
            elif dst.exists() or dst.is_symlink():
                # Includes an UNMARKED folder holding our exact bytes: a crash
                # between copy and marker. Re-adopt it instead of forking a
                # `probe-` duplicate that nothing would ever clean up.
                _write(dst, src, slug, plugin)
                out["refreshed"].append(slug)
            else:
                _write(dst, src, slug, plugin)
                out["installed"].append(slug)
            keep.add(dst.name)

        out["removed"] = list(prune(root, keep))
    return out


def prune(skills_root: Path, keep: "set[str] | frozenset[str]") -> tuple[str, ...]:
    """Delete OUR folders under `skills_root` that `keep` does not name."""
    root = Path(skills_root)
    if not root.is_dir():
        return ()
    gone = []
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not folder.is_dir():
            # `rmtree` raises on a symlink, and in the hook that exception was
            # swallowed -- one symlinked folder stopped the sync on that machine
            # for good, with no signal anywhere (reproduced).
            continue
        if folder.name not in keep and is_ours(folder):
            shutil.rmtree(folder)
            gone.append(folder.name)
    return tuple(gone)
