"""Every `probe ...` command a skill teaches must actually exist.

`tests/test_skills_sync.py` guards the plugin copy against drifting from
`skills/`. It cannot catch the other half of the same failure: a skill that is
perfectly synced and teaches a command or flag the CLI does not have.

That failure has the same shape and is just as invisible — the tests pass, the
MCP is correct, and only the AGENT is wrong, driving a capable tool with
instructions that no longer work. A renamed flag is the likeliest cause, and
nothing else in the tree would notice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer

from probe.cli.main import app

_SKILLS = Path(__file__).resolve().parent.parent / "skills"

#: `probe verb`, or `probe group verb`. Trailing punctuation and placeholders
#: (RUN_ID, PATH, --flags) are handled by the parser below, not here.
_INVOCATION = re.compile(r"`probe ([a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)?)")

#: The same, unquoted, for inside a fenced code block.
_BARE = re.compile(r"(?:^|\s)probe ([a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)?)")

#: A skill may deliberately name a command it is telling the agent NOT to run
#: (`track-research-work` warns off `probe hook ...`). Those are prose, not
#: instructions to copy, and this test has no business policing them.
_NEGATED = re.compile(
    r"\b(do not|don't|never|instead of|rather than|reserved for|no longer)\b",
    re.IGNORECASE,
)


def _registered() -> set[str]:
    """Every command path the Typer app exposes, e.g. {"snapshot", "artifact add"}."""
    names: set[str] = set()

    def walk(t: typer.Typer, prefix: str = "") -> None:
        for command in t.registered_commands:
            name = command.name or (command.callback.__name__.replace("_", "-"))
            names.add(f"{prefix}{name}".strip())
        for group in t.registered_groups:
            sub = f"{prefix}{group.name} "
            names.add(group.name)
            if group.typer_instance is not None:
                walk(group.typer_instance, sub)

    walk(app)
    return names


def _invocations(text: str) -> list[str]:
    """Commands a reader would actually copy, from BOTH code surfaces.

    Fenced blocks and inline spans need separate handling, and getting that
    wrong makes the whole test useless: a first version matched only a literal
    backtick before `probe`, so it checked inline spans and silently ignored
    fenced blocks -- the surface that gets copy-pasted. It passed with
    `probe snapshot-shwo` sitting in a code block.
    """
    found: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            # Code. A line may continue with `\`, so scan the whole line.
            found.extend(m.strip() for m in _BARE.findall(line))
            continue
        if _NEGATED.search(line):
            continue
        found.extend(m.strip() for m in _INVOCATION.findall(line))
    return found


def _skill_files() -> list[Path]:
    return sorted(set(_SKILLS.rglob("*.md")))


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name + "/" + p.name)
def test_every_probe_command_a_skill_teaches_is_real(path):
    registered = _registered()
    bad: list[str] = []
    for candidate in _invocations(path.read_text()):
        # Prefer the two-word form when it resolves (`artifact add`), else the
        # first word (`snapshot RUN_ID` -> `snapshot`).
        if candidate in registered or candidate.split(" ")[0] in registered:
            continue
        bad.append(candidate)
    assert not bad, f"{path.relative_to(_SKILLS.parent)} teaches unknown commands: {sorted(set(bad))}"


def test_the_parser_would_actually_catch_a_typo():
    """A guard that cannot fail is not a guard."""
    registered = _registered()
    assert "snapshot" in registered and "snapshot-restore" in registered
    assert "artifact add" in registered
    assert "snapshot-restor" not in registered
