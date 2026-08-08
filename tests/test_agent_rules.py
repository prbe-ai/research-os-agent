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


def test_claude_config_dir_moves_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A researcher who moved their config dir must not get a second, dead file.

    Writing to a hardcoded ~/.claude there produces a CLAUDE.md Claude Code
    never reads, and the wizard would report success over a file with no effect.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert agent_rules.memory_path() == tmp_path / "elsewhere" / "CLAUDE.md"

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert agent_rules.memory_path() == Path.home() / ".claude" / "CLAUDE.md"


def test_codex_home_uses_the_global_agents_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex does not read Claude's global CLAUDE.md."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert agent_rules.memory_path("codex") == tmp_path / "codex-home" / "AGENTS.md"

    monkeypatch.delenv("CODEX_HOME")
    assert agent_rules.memory_path("codex") == Path.home() / ".codex" / "AGENTS.md"


def test_codex_wizard_writes_agents_md_not_claude_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from probe.cli import setup as wizard

    monkeypatch.setenv("PROBE_AGENT", "codex")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))

    messages = wizard.apply_agent_rules(True)

    assert messages
    assert (tmp_path / "codex-home" / "AGENTS.md").exists()
    assert not (tmp_path / "claude-home" / "CLAUDE.md").exists()


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

    # PRESENT-and-unusable, not absent: absent is what made the caller append.
    assert agent_rules.installed_version(memory) == 0
    with pytest.raises(agent_rules.DamagedBlock):
        agent_rules.install(memory)

    assert memory.read_text() == f"{_USER_TEXT}\n{agent_rules.BEGIN_MARKER}\nhalf a block\n"


def test_a_stray_marker_never_eats_the_researchers_own_rules(memory: Path) -> None:
    """The one that got away.

    An orphan BEGIN made `install` append (None read as "absent"), leaving TWO
    opens and one close. The NEXT run then spanned from the orphan open to our
    real close and rewrote everything between -- the researcher's own rules,
    gone, while the wizard printed "Refreshed the Probe block". Two ordinary
    runs, no warning, no backup.

    A researcher can get a stray marker just by pasting the PR that introduced
    it: #153's body quotes the marker in a fenced block.
    """
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text(f"{agent_rules.BEGIN_MARKER}\n\n{_USER_TEXT}")

    for _ in range(2):
        with pytest.raises(agent_rules.DamagedBlock):
            agent_rules.install(memory)

    assert "Never write in a Git repo's primary checkout" in memory.read_text()


def test_a_second_complete_block_is_damage_not_a_target(memory: Path) -> None:
    """Two full pairs: rewriting either one is a guess about which is ours."""
    memory.parent.mkdir(parents=True, exist_ok=True)
    agent_rules.install(memory)
    memory.write_text(memory.read_text() + _USER_TEXT + agent_rules.render_block())

    with pytest.raises(agent_rules.DamagedBlock):
        agent_rules.install(memory)
    with pytest.raises(agent_rules.DamagedBlock):
        agent_rules.remove(memory)
    assert "Never write in a Git repo's primary checkout" in memory.read_text()


def test_a_file_we_cannot_decode_is_reported_not_raised(memory: Path) -> None:
    """One latin-1 character in a researcher's own CLAUDE.md used to take the
    whole wizard down with a traceback, mid-install: UnicodeDecodeError is a
    ValueError, so the `except OSError` around the call never saw it."""
    from probe.cli import setup as wizard

    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes("# Caf\xe9 rules\n".encode("latin-1"))

    with pytest.raises(UnicodeDecodeError):
        agent_rules.install(memory)

    messages = wizard.apply_agent_rules(True)
    assert messages and "Could not update" in messages[0]
    assert memory.read_bytes() == "# Caf\xe9 rules\n".encode("latin-1")


def test_removal_reports_a_file_it_could_not_read(memory: Path) -> None:
    """Swallowing the read error rendered as silence: the researcher unticked
    the row, saw nothing printed, and the rule kept firing every session."""
    from probe.cli import setup as wizard

    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes("caf\xe9\n".encode("latin-1"))

    messages = wizard.apply_agent_rules(False)
    assert messages and "Could not update" in messages[0]


def test_the_write_is_atomic(memory: Path) -> None:
    """A truncate-then-write on the user's global memory file leaves it in
    pieces if the process dies mid-write -- and a truncated file is exactly the
    stray-marker state that costs the researcher their rules."""
    import inspect

    source = inspect.getsource(agent_rules)
    assert "write_text_atomic" in source
    assert ".write_text(" not in source, "every write to CLAUDE.md must be atomic"


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
    body = " ".join(agent_rules.POINTER_BODY.split())
    assert "When the work is research" in body
    assert "BEFORE designing or scaffolding" in body


def test_the_rule_requires_prior_knowledge_search_before_design() -> None:
    """The global file is the only instruction loaded before skill selection.

    Naming only the tracking skills leaves Codex able to start a new project
    without first discovering the team's existing experiments and decisions.
    The standing rule must make the read step explicit while leaving detailed
    tool procedure in the plugin and skills, where releases can update it.
    """
    body = " ".join(agent_rules.POINTER_BODY.split())
    assert "Before proposing a research direction or implementation" in body
    assert "search the Probe Research knowledge base" in body
    assert "`probe-research` MCP read surface" in body
    assert "captured coding-agent sessions" in body
    assert "local repository" in body
    assert "If that read surface is unavailable, say so" in body


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
