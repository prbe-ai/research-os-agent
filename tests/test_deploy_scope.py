"""The MCP deploy filter must cover everything the MCP actually imports.

`.github/workflows/deploy-mcp.yml` only rebuilds the hosted MCP when certain paths
change, so a change to `cli/` or `connectors/` — which the server never loads —
costs nothing. That narrowing is only safe if the path list keeps matching the
server's real import graph.

The failure it prevents is silent and expensive: make `mcp/server.py` import a new
module, forget to widen the filter, and every later change to that module deploys
NOTHING. mcp.research.prbe.ai then serves stale code while main looks green — the
exact drift this pipeline was built to end (the manifest used to pin a mutable
`:0.5.0` that nobody re-pushed, so "what is running?" had no answer).

So: measure the closure, diff it against the filter, fail loudly. Same contract as
tests/test_parity.py — never rely on someone remembering.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "deploy-mcp.yml"
# The console entrypoint deploy/mcp/Dockerfile runs (`probe-research-mcp-http`).
_ENTRYPOINT = "probe.mcp.server"


def _mcp_import_closure() -> set[str]:
    """Repo-relative paths of every `probe.*` module the MCP entrypoint loads.

    Measured in a FRESH interpreter: importing here would read a `sys.modules`
    already polluted by the rest of the suite, making the answer depend on test
    order — it would quietly over-report and the guard would pass on a filter that
    is actually too narrow.
    """
    code = (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        f"import {_ENTRYPOINT}\n"
        "out = []\n"
        "for name in set(sys.modules) - before:\n"
        "    if not name.startswith('probe'):\n"
        "        continue\n"
        "    f = getattr(sys.modules[name], '__file__', None)\n"
        "    if f and '/probe/' in f:\n"
        "        out.append(f)\n"
        "print(json.dumps(out))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=_ROOT
    )
    if proc.returncode != 0:
        pytest.skip(f"cannot import {_ENTRYPOINT} here: {proc.stderr.strip()[:200]}")

    files: set[str] = set()
    for abs_path in json.loads(proc.stdout):
        path = Path(abs_path)
        try:  # installed editable -> the file lives under this repo's src/
            files.add(str(path.relative_to(_ROOT)))
        except ValueError:  # installed into site-packages: recover the src/ tail
            marker = f"{'probe'}/"
            tail = str(path).split(marker, 1)[-1]
            files.add(f"src/probe/{tail}")
    return files


def _workflow_paths() -> list[str]:
    """The `on.push.paths` globs, read without a YAML dependency.

    `pyyaml` is not a test dep, and the block is a flat list of quoted strings, so
    a tiny reader beats adding a dependency to guard one file.
    """
    paths: list[str] = []
    inside = False
    for line in _WORKFLOW.read_text().splitlines():
        stripped = line.strip()
        if stripped == "paths:":
            inside = True
            continue
        if not inside:
            continue
        if stripped.startswith("- "):
            item = stripped[2:].strip()
            if not item.startswith(("'", '"')):  # unquoted: a trailing comment is bare
                item = item.split("#", 1)[0].strip()
            else:  # quoted: take what is inside the quotes, ignore any trailing comment
                quote = item[0]
                end = item.find(quote, 1)
                item = item[1:end] if end > 0 else item.strip(quote)
            if item:
                paths.append(item)
        elif stripped and not stripped.startswith("#"):
            break  # a non-comment, non-item line ends the block
    return paths


def _covered(file: str, globs: list[str]) -> bool:
    for glob in globs:
        if glob.endswith("/**"):
            if file.startswith(glob[:-2]):
                return True
        elif fnmatch.fnmatch(file, glob) or file == glob:
            return True
    return False


def test_workflow_declares_paths():
    """A guard that reads nothing guards nothing."""
    paths = _workflow_paths()
    assert paths, f"no on.push.paths found in {_WORKFLOW.name}"
    assert "pyproject.toml" in paths  # deps + the console entrypoint reach the image


def test_deploy_filter_covers_every_module_the_mcp_imports():
    """Narrow the filter all you like — but never below what the server loads."""
    closure = _mcp_import_closure()
    assert closure, "measured an empty import closure; the probe cannot be right"

    globs = _workflow_paths()
    uncovered = sorted(f for f in closure if not _covered(f, globs))
    assert not uncovered, (
        f"{len(uncovered)} module(s) the MCP imports are NOT in deploy-mcp.yml's paths, "
        "so changing them would deploy nothing and mcp.research.prbe.ai would go stale:\n  "
        + "\n  ".join(uncovered)
        + "\n\nAdd them to on.push.paths (or stop importing them from probe.mcp.server)."
    )


def test_filter_is_actually_narrower_than_the_whole_package():
    """The point of the filter is to skip work. If it ever widens to `src/probe/**`
    it stops buying anything, and the closure guard above is what lets it stay tight.
    """
    globs = _workflow_paths()
    assert "src/probe/**" not in globs, (
        "the filter widened to the whole package — a cli/ or connectors/ change would "
        "rebuild and redeploy the MCP for nothing. Keep it scoped to the import closure."
    )
    # cli/ and connectors/ are real, actively-developed packages the server never loads.
    for never in ("src/probe/cli/main.py", "src/probe/connectors/harbor.py"):
        assert not _covered(never, globs), f"{never} should not trigger an MCP deploy"
