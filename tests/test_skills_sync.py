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
