"""The plugin's skill copies must match the canonical `skills/`.

`plugins/probe-research/skills/` is a COPY of `skills/`, not a symlink — the plugin
ships self-contained, so the duplication is deliberate. `make sync-plugin-skills`
reconciles them and nothing enforced it, which meant an edit to `skills/` shipped a
plugin still teaching the old thing, silently and indefinitely.

That failure is invisible in the worst way: the tests pass, the MCP is correct, and
only the AGENT is wrong — it drives a capable tool with stale instructions. Since
"thin harness, fat skills" puts the knowledge of which view to ask for INTO these
files, a drifted copy is not a docs nit; it is half the product being wrong.

Same contract as tests/test_parity.py and tests/test_deploy_scope.py: guard it,
never rely on someone remembering. CI (`ci.yml`, `release.yml`) and the MCP deploy
(`deploy-mcp.yml`) all run `pytest -q`, so this blocks the rollout too.
"""

from __future__ import annotations

import filecmp
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL = _ROOT / "skills"
_PLUGIN = _ROOT / "plugins" / "probe-research" / "skills"

# Mirrors the Makefile's sync-plugin-skills list.
_SYNCED = ("track-experiment", "manage-research-asset", "publish-experiment")


def _files(root: Path) -> dict[str, Path]:
    """Every file under `root`, keyed by its path relative to root."""
    return {
        str(path.relative_to(root)): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


@pytest.mark.parametrize("skill", _SYNCED)
def test_plugin_skill_copy_matches_canonical(skill: str) -> None:
    canonical, plugin = _CANONICAL / skill, _PLUGIN / skill
    assert canonical.is_dir(), f"canonical skill {skill} is missing"
    assert plugin.is_dir(), f"plugin copy of {skill} is missing; run `make sync-plugin-skills`"

    left, right = _files(canonical), _files(plugin)
    assert sorted(left) == sorted(right), (
        f"{skill}: plugin copy has a different file list than skills/{skill} "
        f"— run `make sync-plugin-skills`"
    )
    drifted = [name for name in left if not filecmp.cmp(left[name], right[name], shallow=False)]
    assert not drifted, (
        f"{skill}: {drifted} differ from the canonical skills/{skill} "
        f"— run `make sync-plugin-skills` (edit skills/, never the plugin copy)"
    )


def test_every_canonical_skill_is_covered_by_this_guard() -> None:
    """A new skill must be added to the Makefile's sync list AND to `_SYNCED`.

    Without this, adding skills/foo/ without wiring the sync would leave the plugin
    silently missing it while every parametrized case above still passed."""
    on_disk = {path.name for path in _CANONICAL.iterdir() if path.is_dir()}
    assert on_disk == set(_SYNCED), (
        f"skills/ holds {sorted(on_disk)} but this guard and the Makefile's "
        f"sync-plugin-skills cover {sorted(_SYNCED)}"
    )


def test_the_wheel_ships_the_canonical_skills_not_a_third_copy() -> None:
    """The THIRD copy path nothing guarded.

    `pyproject.toml` ships `skills` as shared-data
    (`share/probe-research/skills`). That is a third distribution of the same
    files, and unlike the plugin copy it had no guard at all — a wheel built
    from a tree with drifted skills would install them and nothing would fail.

    Point it at the canonical directory, and assert it stays pointed there: the
    moment it names a copy instead, the copy can drift the way the plugin's did.
    """
    import tomllib

    with (_ROOT / "pyproject.toml").open("rb") as fh:
        config = tomllib.load(fh)
    shared = config["tool"]["hatch"]["build"]["targets"]["wheel"]["shared-data"]
    assert "skills" in shared, (
        "the wheel no longer ships skills/ — if that is deliberate, delete this "
        "guard; if it now ships a COPY, point it back at skills/"
    )


def test_no_skill_names_a_tool_that_does_not_exist() -> None:
    """A skill naming a retired tool teaches an agent to call nothing.

    This is the failure that actually shipped: the installed copy of
    track-experiment was measured 30 lines behind the repo, still teaching a
    surface that had moved. No test can reach a user's installed cache, but this
    at least stops the SOURCE from naming tools that are gone.
    """
    import re

    # Read the declared tool names from the server source rather than standing a
    # server up: this guard is about the SKILLS being consistent with the code,
    # and it should not need a client, a fake backend, or an event loop to say so.
    server_src = (_ROOT / "src" / "probe" / "mcp" / "server.py").read_text()
    declared = set(re.findall(r"^    def ([a-z_]+)\(", server_src, re.M))

    referenced: dict[str, set[str]] = {}
    for skill_dir in _CANONICAL.iterdir():
        if not skill_dir.is_dir():
            continue
        text = (skill_dir / "SKILL.md").read_text()
        # Tool-shaped mentions: `name(` or `name` in backticks.
        found = set(re.findall(r"`(browse_research|search_knowledge|get_entity|research_\w+)", text))
        referenced[skill_dir.name] = found

    for skill, names in referenced.items():
        unknown = sorted(n for n in names if n not in declared)
        assert not unknown, f"{skill} names tools that do not exist: {unknown}"


def test_user_facing_docs_do_not_advertise_the_retired_surface() -> None:
    """The README and the setup command are what a NEW user reads first.

    The skills guard above covers `skills/`, which is why this slipped: the
    README and `plugins/*/commands/` were pointing new users at the five names
    that disappear next release. Deprecated names may be MENTIONED (they still
    answer), but not presented as the surface.
    """
    for rel in ("README.md", "plugins/probe-research/commands/probe-research-setup.md"):
        text = (_ROOT / rel).read_text()
        assert "browse_research" in text, f"{rel} does not mention the current surface"
        assert "search_knowledge" in text, f"{rel} does not mention the current surface"
        assert "get_entity" in text, f"{rel} does not mention the current surface"
        # If it names a deprecated tool it must say so within a few lines.
        if "research_resolve" in text:
            assert "deprecat" in text.lower(), (
                f"{rel} names retired tools without marking them deprecated"
            )
