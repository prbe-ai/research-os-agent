"""The CLAUDE.md pointer block writes into a file the researcher owns.

Every test here is about not damaging that file. The block is worth writing only
because `CLAUDE.md` is always in context; the rules a researcher wrote for
themselves are the reason the file matters, and clobbering them would be a worse
outcome than never having written anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from probe.cli import agent_rules

_USER_TEXT = """# My rules

## Highest priority: worktree only

- Never write in a Git repo's primary checkout or on `main`/`master`.
"""


@pytest.fixture()
def memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path / "CLAUDE.md"


def test_claude_config_dir_moves_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A researcher who moved their config dir must not get a second, dead file.

    Writing to a hardcoded ~/.claude there produces a CLAUDE.md Claude Code
    never reads, and the wizard would report success over a file with no effect.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert agent_rules.memory_path() == tmp_path / "elsewhere" / "CLAUDE.md"

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert agent_rules.memory_path() == Path.home() / ".claude" / "CLAUDE.md"


def test_install_creates_the_file_and_its_parent(memory: Path) -> None:
    assert not memory.exists()
    assert agent_rules.install(memory) is True
    assert agent_rules.is_installed(memory)
    assert agent_rules.is_current(memory)
    assert "probe-research:start-research-work" in memory.read_text()


def test_install_preserves_existing_content_byte_for_byte(memory: Path) -> None:
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text(_USER_TEXT)

    agent_rules.install(memory)
    after = memory.read_text()

    assert after.startswith(_USER_TEXT)
    assert agent_rules.BEGIN_MARKER in after


def test_install_is_idempotent(memory: Path) -> None:
    """A second wizard run must not append a second block.

    Re-running the wizard is normal -- it is how a capability gets turned on
    later -- so an append-only install would stack blocks until the file is
    mostly ours.
    """
    agent_rules.install(memory)
    first = memory.read_text()

    assert agent_rules.install(memory) is False
    assert memory.read_text() == first
    assert first.count(agent_rules.BEGIN_MARKER) == 1


def test_a_stale_block_is_rewritten_in_place(memory: Path) -> None:
    """The wording is versioned, and this file is unreachable by a release.

    A machine that ticked the row once and never re-ran the wizard is the only
    place an outdated block can live, so the refresh has to replace rather than
    append -- and must not disturb what surrounds it.
    """
    memory.parent.mkdir(parents=True, exist_ok=True)
    old = agent_rules.render_block(version=agent_rules.POINTER_VERSION - 1)
    memory.write_text(f"{_USER_TEXT}\n{old}\n## After\n\nmine\n")

    assert agent_rules.installed_version(memory) == agent_rules.POINTER_VERSION - 1
    assert agent_rules.is_installed(memory) and not agent_rules.is_current(memory)

    assert agent_rules.install(memory) is True
    after = memory.read_text()

    assert after.count(agent_rules.BEGIN_MARKER) == 1
    assert agent_rules.is_current(memory)
    assert after.startswith(_USER_TEXT)
    assert "## After" in after and "mine" in after


def test_remove_takes_only_the_block(memory: Path) -> None:
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text(_USER_TEXT)
    agent_rules.install(memory)

    assert agent_rules.remove(memory) is True
    after = memory.read_text()

    assert agent_rules.BEGIN_MARKER not in after
    assert "Never write in a Git repo's primary checkout" in after
    assert agent_rules.remove(memory) is False


def test_remove_leaves_a_file_we_created_empty_rather_than_deleting_it(memory: Path) -> None:
    """Unticking a row is not permission to unlink a file in the user's home."""
    agent_rules.install(memory)
    agent_rules.remove(memory)

    assert memory.exists()
    assert memory.read_text() == ""


def test_an_opening_marker_with_no_close_is_refused_not_nested(memory: Path) -> None:
    """Hand-deleting the end marker must not produce a block inside a block.

    Treating a half-open block as absent and appending would leave the file with
    an unterminated marker wrapping our new one, and every later read would then
    span both.
    """
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text(f"{_USER_TEXT}\n{agent_rules.BEGIN_MARKER}\nhalf a block\n")

    assert agent_rules.installed_version(memory) is None
    agent_rules.install(memory)

    text = memory.read_text()
    assert text.count(agent_rules.END_MARKER) == 1


def test_a_block_with_an_unparseable_version_is_present_not_absent(memory: Path) -> None:
    """Version 0, never None -- None would make the caller append a duplicate."""
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text(
        f"{agent_rules.BEGIN_MARKER}\n<!-- vBOGUS -->\nbody\n{agent_rules.END_MARKER}\n"
    )

    assert agent_rules.installed_version(memory) == 0
    assert agent_rules.is_installed(memory)
    assert not agent_rules.is_current(memory)

    agent_rules.install(memory)
    assert memory.read_text().count(agent_rules.BEGIN_MARKER) == 1


def test_the_rule_is_conditional_on_the_work_being_research() -> None:
    """This file is user-global, so it loads while fixing an unrelated CSS bug.

    An unconditional "register a project before you start" in that position
    teaches the agent that the block does not apply to it, which costs the
    block its authority everywhere including the sessions it was written for.
    """
    body = agent_rules.POINTER_BODY
    assert "When the work is research" in body
    assert "BEFORE designing or scaffolding" in body


def test_the_block_names_skills_and_not_commands_that_rot() -> None:
    """Surfaces are stable; procedures are not, and this copy is unreachable.

    In eight days the note vocabulary was added (#144), replaced by NOTES.md
    (#150) and re-triggered (#149). A block naming `probe note add --kind` would
    now be teaching a command that does not exist, on every machine that ever
    ran the wizard, with no release able to correct it.
    """
    body = agent_rules.POINTER_BODY
    assert "probe-research:start-research-work" in body
    assert "probe-research:track-research-work" in body

    for rotted in ("probe note add", "--kind", "--supersedes", "probe notes write"):
        assert rotted not in body, f"block names a command that can rot: {rotted}"
