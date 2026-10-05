"""Kimi Code: which session a wire belongs to, and what the daemon reads from it.

Real Kimi Code 2.1.1 wires (agent/tests/fixtures/kimi_code). A Kimi wire's
header names no session, so identity comes from the two facts Kimi itself
writes: the session folder's name and the `id` in its `state.json`.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from tap import companion_observe
from tap.session_identity import validate_identity
from tap.session_journal import ReconciliationRequired

TUI_ID = "0d2f2223-e34e-4cd1-87e3-b8d8d39aa229"
RUN_ID = "9b40c4c4-d58a-4ed7-9372-cfecf8469b05"


def _session(fixtures: Path, sid: str) -> Path:
    return fixtures / "sessions" / "wd_proj_846899122802" / f"session_{sid}"


def _main_wire(fixtures: Path, sid: str) -> Path:
    return _session(fixtures, sid) / "agents" / "main" / "wire.jsonl"


def _copy_session(fixtures: Path, sid: str, tmp_path: Path) -> Path:
    target = tmp_path / "wd_proj_x" / f"session_{sid}"
    shutil.copytree(_session(fixtures, sid), target)
    return target


def test_identity_is_the_session_folder_and_its_state_file(kimi_fixtures) -> None:
    proof = validate_identity(_main_wire(kimi_fixtures, TUI_ID), "kimi_code", TUI_ID)
    assert proof["native_session_id"] == TUI_ID
    assert proof["source_cwd"] == "/home/researcher/kimi-fixture/proj"
    assert proof["identity_method"] == "producer-record-v1"


def test_another_session_s_id_is_refused(kimi_fixtures) -> None:
    with pytest.raises(ReconciliationRequired):
        validate_identity(_main_wire(kimi_fixtures, TUI_ID), "kimi_code", RUN_ID)
    with pytest.raises(ReconciliationRequired):
        validate_identity(_main_wire(kimi_fixtures, TUI_ID), "kimi_code", str(uuid.uuid4()))


def test_a_state_file_naming_another_session_is_refused(kimi_fixtures, tmp_path) -> None:
    session = _copy_session(kimi_fixtures, TUI_ID, tmp_path)
    state = json.loads((session / "state.json").read_text())
    state["id"] = f"session_{RUN_ID}"
    (session / "state.json").write_text(json.dumps(state))
    with pytest.raises(ReconciliationRequired):
        validate_identity(session / "agents" / "main" / "wire.jsonl", "kimi_code", TUI_ID)


def test_a_missing_state_file_is_refused_not_guessed(kimi_fixtures, tmp_path) -> None:
    session = _copy_session(kimi_fixtures, TUI_ID, tmp_path)
    (session / "state.json").unlink()
    with pytest.raises(ReconciliationRequired):
        validate_identity(session / "agents" / "main" / "wire.jsonl", "kimi_code", TUI_ID)


def test_a_file_without_kimi_s_header_is_refused(kimi_fixtures, tmp_path) -> None:
    session = _copy_session(kimi_fixtures, TUI_ID, tmp_path)
    wire = session / "agents" / "main" / "wire.jsonl"
    lines = wire.read_text().splitlines(keepends=True)
    wire.write_text("".join(lines[1:]))  # no `metadata` first line
    with pytest.raises(ReconciliationRequired):
        validate_identity(wire, "kimi_code", TUI_ID)


def _events(wire: Path):
    raw = wire.read_bytes()
    lines, offset = [], 0
    for line in raw.splitlines(keepends=True):
        offset += len(line)
        lines.append((offset, line.rstrip(b"\n")))
    return companion_observe.parse_lines("kimi_code", lines)


def test_the_daemon_reads_prompts_replies_calls_and_results_once(kimi_fixtures) -> None:
    events = _events(_main_wire(kimi_fixtures, RUN_ID))
    by_role = {}
    for event in events:
        by_role.setdefault(event.role, []).append(event)
    assert [e.text for e in by_role[companion_observe.USER]] == [
        "RUN: echo hello-from-kimi\nWRITE: notes.txt :: lr sweep notes\nSAY: Both steps ran.",
        "RUN: ls\nSAY: Listed.",
    ]
    assert [e.text for e in by_role[companion_observe.ASSISTANT]] == [
        "Running a command.", "Writing a file.", "Both steps ran.", "Running a command.", "Listed.",
    ]
    calls = by_role[companion_observe.TOOL_CALL]
    assert [(c.tool, c.call_id, c.tool_input.get("command")) for c in calls] == [
        ("Bash", "call_1", "echo hello-from-kimi"),
        ("Write", "call_2", None),
        ("Bash", "call_7", "ls"),
    ]
    results = by_role[companion_observe.TOOL_RESULT]
    # The daemon needs tool OUTPUT (a run id first appears there).
    assert (results[0].call_id, results[0].text) == ("call_1", "hello-from-kimi\n")


def test_the_daemon_never_reads_injected_text_as_a_prompt(kimi_fixtures) -> None:
    texts = [e.text for e in _events(_main_wire(kimi_fixtures, TUI_ID)) if e.role == companion_observe.USER]
    assert texts == [
        "ASK ;; RUN: sleep 12; echo slept ;; SAY: Sweep starts from your pick.",
        "also log the val loss please",
    ]


def test_a_researcher_shell_command_is_a_call_and_its_output() -> None:
    def record(phase: str, text: str) -> tuple[int, bytes]:
        origin = {"kind": "shell_command", "phase": phase}
        line = {"type": "context.append_message", "agentId": "main",
                "message": {"role": "user", "content": [{"type": "text", "text": text}],
                            "toolCalls": [], "origin": origin}, "time": 1}
        return 1, json.dumps(line).encode()

    events = companion_observe.parse_lines("kimi_code", [
        record("input", "<bash-input>\nprobe run start --name a &amp;&amp; echo ok\n</bash-input>"),
        record("output", "<bash-stdout>run_id: 1</bash-stdout><bash-stderr></bash-stderr>"),
    ])
    assert [(e.role, e.tool_input.get("command"), e.text) for e in events] == [
        (companion_observe.TOOL_CALL, "probe run start --name a && echo ok", ""),
        (companion_observe.TOOL_RESULT, None, "run_id: 1"),
    ]
