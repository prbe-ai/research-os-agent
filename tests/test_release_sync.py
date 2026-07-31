"""`client-version.json` must agree with the versions it advertises.

The manifest is what the SessionStart hook reads to decide whether to nudge a
user to update. Nothing tied it to the versions actually on main, so the two
drifted silently — and the failure mode is the worst kind: CI green, content
correct, release tagged, and the rollout reaching NOBODY, because every client
compares itself against a manifest still naming the previous version.

Both halves of this had already broken by the time the guard was written:

  * `plugin.json` was hand-bumped 0.13.0 -> 0.13.1 without the manifest. Three
    skill-description releases shipped to main while `plugin.latest` still said
    0.13.0, so no installed plugin was ever told to update.
  * `pyproject.toml` sat at 0.26.0 while PyPI's latest was 0.25.0 and
    `cli.latest` said 0.25.0 — a version bumped by one PR and never released,
    found only by hand during an unrelated pre-flight.

Same contract as tests/test_skills_sync.py: that one guards the three COPIES of
the skill text against each other; this guards the VERSION NUMBERS. Content
drift was covered, release drift was not.

The invariant holds naturally when you release through `release.yml`, which
writes the version file and the manifest in a single commit. What this test
actually forbids is hand-editing a version file — which is precisely the thing
that cost the 0.13.1 rollout.

NOTE the `min` fields are deliberately NOT checked. They are compatibility
floors that lag `latest` on purpose; pinning them together would defeat them.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = _ROOT / "client-version.json"
_PLUGIN_JSON = _ROOT / "plugins" / "probe-research" / ".claude-plugin" / "plugin.json"
_PYPROJECT = _ROOT / "pyproject.toml"

_REMEDY = (
    "Release through the workflow, which writes both files in one commit:\n"
    "  gh workflow run release.yml -f plugin_version=X.Y.Z          # plugin\n"
    "  gh workflow run release.yml -f version=X.Y.Z -f bump_manifest=true  # CLI\n"
    "Hand-editing a version file looks like it ships and silently does not."
)


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_manifest_plugin_latest_matches_plugin_json() -> None:
    advertised = _manifest()["plugin"]["latest"]
    actual = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    assert advertised == actual, (
        f"client-version.json plugin.latest is {advertised!r} but plugin.json is "
        f"{actual!r}. The SessionStart hook nudges off the manifest, so plugin "
        f"{actual} ships to nobody.\n{_REMEDY}"
    )


def test_manifest_cli_latest_matches_pyproject() -> None:
    advertised = _manifest()["cli"]["latest"]
    with _PYPROJECT.open("rb") as fh:
        actual = tomllib.load(fh)["project"]["version"]
    assert advertised == actual, (
        f"client-version.json cli.latest is {advertised!r} but pyproject.toml is "
        f"{actual!r}. Either the CLI was bumped without releasing it, or it was "
        f"released without bumping the manifest.\n{_REMEDY}"
    )
