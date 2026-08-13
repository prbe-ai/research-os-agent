"""Test-wide isolation backstop for the tap plugin.

The reconciler sweeps a transcript TREE, not just the file it was handed, so any
test that drives the daemon loop can reach outside its tmpdir. Left alone it
would scan the developer's real ~/.claude/projects (877 files, 672MB of history
on the machine this was written against) and — if a future change ever relaxed
the adoption gate — enqueue it. Tests must not depend on that gate holding to
stay off real user data.

Every test therefore gets a private, empty transcript root by default. A test
that wants transcripts creates them under its own root (see test_reconcile).

Mirrors the philosophy of agent/tests/conftest.py, which shims `claude`/`codex`
onto PATH so an accidental real-agent spawn fails loudly instead of wedging the
macOS keychain.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_transcript_roots(monkeypatch, tmp_path_factory):
    """Point both flavours' transcript discovery at an empty per-test directory."""
    root = tmp_path_factory.mktemp("transcripts")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PROJECTS_DIR", str(root))
    monkeypatch.setenv("PRBE_CODEX_SESSIONS_DIR", str(root / "codex"))
    yield
