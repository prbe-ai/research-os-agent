"""The plugin's copy of `version_policy` must match the canonical one.

`plugins/probe-research/hooks/version_policy.py` is a COPY of
`src/probe/version_policy.py`, not a symlink, for the same reason the skills are
copied: the plugin ships self-contained. Here the constraint is harder than
convenience -- `session-start.sh` runs the hook under the SYSTEM python3, which
has no probe package on its path, so a shared import is not merely inconvenient
but impossible.

`make sync-plugin-policy` reconciles them. This guards it, because the drift is
silent in the worst way: both copies keep working, they just disagree about where
the state file lives or how long a manifest stays fresh. A disagreement about the
STATE PATH stops auto-update in the hook while `probe doctor` -- reading the other
path -- goes on reporting it enabled, with a `last_attempt` that genuinely
succeeded, months ago.

Same contract as tests/test_skills_sync.py, tests/test_parity.py and
tests/test_deploy_scope.py: guard it, never rely on someone remembering.
"""

from __future__ import annotations

import filecmp
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL = _ROOT / "src" / "probe" / "version_policy.py"
_PLUGIN_COPY = _ROOT / "plugins" / "probe-research" / "hooks" / "version_policy.py"


def test_plugin_policy_copy_matches_canonical() -> None:
    assert _CANONICAL.is_file(), "canonical src/probe/version_policy.py is missing"
    assert _PLUGIN_COPY.is_file(), (
        "plugin copy of version_policy.py is missing; run `make sync-plugin-policy`"
    )
    assert filecmp.cmp(_CANONICAL, _PLUGIN_COPY, shallow=False), (
        "plugins/probe-research/hooks/version_policy.py has drifted from "
        "src/probe/version_policy.py — run `make sync-plugin-policy` "
        "(edit the canonical file, never the plugin copy)"
    )


def test_the_copy_is_importable_with_no_probe_package_available() -> None:
    """The property the copy exists for, tested the way it actually runs.

    Importing it in THIS interpreter proves nothing -- pytest has the probe
    package on its path. The hook does not. Run a subprocess whose sys.path holds
    only the hooks directory, which is what `python3 <plugin>/hooks/version_check.py`
    produces, and confirm the module stands up alone.

    Cheap on purpose: one short-lived stdlib-only child, no network, no venv.
    """
    # `-c` puts the CWD at sys.path[0], which is the same resolution the hook
    # gets from `python3 <plugin_root>/hooks/version_check.py`. Do NOT clear
    # sys.path to simulate this: that removes the stdlib too, and the test then
    # fails on `__future__` rather than on anything real.
    #
    # Asserting `probe` never entered sys.modules is the actual claim -- the copy
    # must stand up with nothing from the package behind it.
    probe = (
        "import sys; import version_policy as p; "
        "assert 'probe' not in sys.modules, 'the copy pulled in the probe package'; "
        "print(p.TTL, p.BACKOFF, p.STATE_FILENAME, p.CACHE_FILENAME)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_PLUGIN_COPY.parent),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"the plugin's version_policy copy does not import standalone: {result.stderr}"
    )
    assert result.stdout.split() == ["900", "3600", "autoupdate.json", "version-check.json"]


def test_no_module_under_probe_duplicates_the_policy_constants() -> None:
    """The duplication this module was created to end must not creep back.

    Before `version_policy`, the TTL, the cache path and the autoupdate state path
    were each written in two places. Anyone re-adding a literal default here is
    re-opening that, so name the two files that are allowed to hold them.
    """
    offenders = []
    for path in sorted((_ROOT / "src" / "probe").rglob("*.py")):
        if path == _CANONICAL:
            continue
        text = path.read_text()
        for needle in ('"version-check.json"', "'version-check.json'", "PROBE_VERSION_TTL"):
            if needle in text:
                offenders.append(f"{path.relative_to(_ROOT)} contains {needle}")
    assert not offenders, (
        "these should read from probe.version_policy rather than redefining it: "
        f"{offenders}"
    )
