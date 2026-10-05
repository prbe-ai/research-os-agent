"""Kimi Code: which files are sessions, which session each is, and how a hook
with no transcript path finds the one it means.

Kimi writes `sessions/<wd>/session_<uuid>/agents/main/wire.jsonl` for the
session and `agents/agent-N/wire.jsonl` for each subagent. Every file has the
same name, so the session id is the folder above it, and only the main wire is
ever a session. Real Kimi Code 2.1.1 trees (agent/tests/fixtures/kimi_code).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tap import harness_registry, kimi_discovery, owner, reconcile, sources
from tap.storage import Storage

MAIN_IDS = {
    "0d2f2223-e34e-4cd1-87e3-b8d8d39aa229",
    "9b40c4c4-d58a-4ed7-9372-cfecf8469b05",
    "c1c22c25-6382-4e79-8f00-a1a45e3c8e77",
    "e240efe5-da6a-4407-9c61-91d8a3a53d24",
}
SUB_ID = "e240efe5-da6a-4407-9c61-91d8a3a53d24"


def _kimi():
    return harness_registry.get_registry().get("kimi_code")


@pytest.fixture
def as_kimi(monkeypatch, tmp_path):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "kimi_code")
    monkeypatch.setenv("PROBE_KIMI_TAP_PLUGIN_DIR", str(tmp_path / "state"))
    (tmp_path / "state" / "logs").mkdir(parents=True)
    return tmp_path / "state"


@pytest.fixture
def kimi_home(kimi_fixtures, tmp_path, monkeypatch):
    """A KIMI_CODE_HOME holding the fixture sessions and Kimi's own index of
    them, its absolute paths moved to where the copy lives."""
    home = tmp_path / "kimi-home"
    shutil.copytree(kimi_fixtures / "sessions", home / "sessions")
    index = (kimi_fixtures / "session_index.jsonl").read_text()
    (home / "session_index.jsonl").write_text(
        index.replace("/home/researcher/kimi-fixture/kimi", str(home))
    )
    monkeypatch.setenv("KIMI_CODE_HOME", str(home))
    monkeypatch.delenv("PROBE_KIMI_SESSIONS_DIR", raising=False)
    return home


def test_discovery_finds_exactly_the_main_wires(as_kimi, kimi_fixtures, monkeypatch) -> None:
    monkeypatch.setenv("PROBE_KIMI_SESSIONS_DIR", str(kimi_fixtures / "sessions"))
    found = reconcile._candidate_transcripts()
    assert len(found) == 4
    assert all(p.parent.name == "main" for p in found)
    assert {reconcile.session_id_for(p) for p in found} == MAIN_IDS


def test_a_subagent_wire_is_never_a_session(as_kimi, kimi_fixtures) -> None:
    sub = (kimi_fixtures / "sessions" / "wd_proj_846899122802" / f"session_{SUB_ID}"
           / "agents" / "agent-0" / "wire.jsonl")
    assert sub.is_file(), "the fixture lost its subagent wire; this test would prove nothing"
    assert reconcile.session_id_for(sub) is None


def test_the_id_is_the_nearest_folder_ending_in_a_uuid(as_kimi, tmp_path) -> None:
    outer = "11111111-2222-4333-8444-555555555555"
    inner = "66666666-7777-4888-9999-aaaaaaaaaaaa"
    path = tmp_path / f"x_{outer}" / "wd_p" / f"session_{inner}" / "agents" / "main" / "wire.jsonl"
    assert reconcile.session_id_for(path) == inner
    assert reconcile.session_id_for(tmp_path / "wd_p" / "session_x" / "agents" / "main" / "wire.jsonl") is None


def test_the_root_follows_kimi_code_home(as_kimi, kimi_home) -> None:
    assert reconcile.transcript_root() == kimi_home / "sessions"
    assert {reconcile.session_id_for(p) for p in reconcile._candidate_transcripts()} == MAIN_IDS


def test_the_sweep_adopts_a_logged_session_and_never_its_subagent(as_kimi, kimi_home) -> None:
    (as_kimi / "logs" / f"{SUB_ID}.log").write_text("spawned\n")
    storage = Storage(as_kimi / "state.db")
    try:
        gaps = reconcile.find_gaps(storage, now=2**40, horizon_s=2**41)
    finally:
        storage.close()
    assert [(g.session_id, g.path.parent.name) for g in gaps] == [(SUB_ID, "main")]


@pytest.mark.parametrize("source", ["claude_code", "codex"])
def test_claude_and_codex_discovery_is_still_rglob(monkeypatch, tmp_path, source) -> None:
    """Their rows name no pattern; the default must walk exactly what
    rglob("*.jsonl") walked, top-level files and dot-folders included."""
    root = tmp_path / "root"
    for rel in ("a.jsonl", "p/b.jsonl", "p/q/c.jsonl", ".hidden/d.jsonl", "p/e.json", "p/agent-1.jsonl"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("{}\n")
    monkeypatch.setenv("PROBE_TAP_SOURCE", source)
    monkeypatch.setenv(sources.harness(sources.get(source)).transcripts["root_env"], str(root))
    assert sources.harness(sources.get(source)).transcript_pattern() == "**/*.jsonl"
    assert sorted(reconcile._candidate_transcripts()) == sorted(root.rglob("*.jsonl"))


# --- a hook payload's session id -> its transcript -----------------------------


def _main_wire(home: Path, sid: str) -> Path:
    return home / "sessions" / "wd_proj_846899122802" / f"session_{sid}" / "agents" / "main" / "wire.jsonl"


@pytest.mark.parametrize("raw", [f"session_{SUB_ID}", SUB_ID])
def test_the_index_names_the_main_wire(kimi_home, raw) -> None:
    assert kimi_discovery.find_transcript(_kimi(), raw) == _main_wire(kimi_home, SUB_ID)


def test_the_newest_index_line_wins(kimi_home, tmp_path) -> None:
    moved = tmp_path / "elsewhere" / f"session_{SUB_ID}"
    with (kimi_home / "session_index.jsonl").open("a") as handle:
        handle.write(json.dumps({"sessionId": f"session_{SUB_ID}", "sessionDir": str(moved),
                                 "workDir": "/w"}) + "\n")
    # Named by Kimi's index before the file exists: the daemon waits for it.
    assert kimi_discovery.find_transcript(_kimi(), SUB_ID) == moved / "agents" / "main" / "wire.jsonl"


def test_a_line_far_from_the_end_is_still_found(kimi_home) -> None:
    index = kimi_home / "session_index.jsonl"
    filler = "".join(
        json.dumps({"sessionId": f"session_00000000-0000-4000-8000-{n:012d}",
                    "sessionDir": f"/nowhere/{n}", "workDir": "/w"}) + "\n"
        for n in range(3000)  # several read blocks, so lines straddle block edges
    )
    index.write_text(index.read_text() + filler)
    assert kimi_discovery.find_transcript(_kimi(), SUB_ID) == _main_wire(kimi_home, SUB_ID)


def test_without_an_index_line_the_pattern_finds_it(kimi_home) -> None:
    (kimi_home / "session_index.jsonl").unlink()
    assert kimi_discovery.find_transcript(_kimi(), SUB_ID) == _main_wire(kimi_home, SUB_ID)


def test_an_unknown_session_has_no_transcript(kimi_home) -> None:
    assert kimi_discovery.find_transcript(_kimi(), "00000000-0000-4000-8000-000000000000") is None


def test_an_index_line_naming_a_subagent_folder_is_not_trusted(kimi_home) -> None:
    index = kimi_home / "session_index.jsonl"
    sub_dir = _main_wire(kimi_home, SUB_ID).parent.parent / "agent-0"
    index.write_text(index.read_text() + json.dumps(
        {"sessionId": f"session_{SUB_ID}", "sessionDir": str(sub_dir), "workDir": "/w"}) + "\n")
    found = kimi_discovery.find_transcript(_kimi(), SUB_ID)
    assert found == _main_wire(kimi_home, SUB_ID)  # the pattern, not the bad line


# --- who owns a Kimi session -------------------------------------------------------


def test_a_kimi_process_owns_its_session_even_inside_another_agent(monkeypatch) -> None:
    """Kimi titles its node process `kimi-code` (seen under a real 2.1.1
    session). Started from a Claude Code shell, the nearer Kimi still owns it."""
    chain = {
        100: owner._Proc(101, "t-hook", False, frozenset({"bash"})),
        101: owner._Proc(102, "t-kimi", False, frozenset({"kimi-code"})),
        102: owner._Proc(103, "t-shell", False, frozenset({"bash"})),
        103: owner._Proc(1, "t-claude", False, frozenset({"claude"})),
    }
    monkeypatch.setattr(owner, "_proc", lambda pid: chain.get(pid))
    found = owner.find_owner(100)
    assert (found.pid, found.name) == (101, "kimi-code")
