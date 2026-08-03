#!/usr/bin/env python3
"""Run one arm of the instruction eval.

MANUAL. Needs a live MCP endpoint and credentials, and costs real tokens per run
(~150 short agent sessions for a full three-arm sweep). It is not wired into CI
on purpose: a slow, expensive, stochastic check gets marked non-blocking the
first time it goes red, and then you have the cost with none of the signal.

Each run is capped at the FIRST meaningful action -- the first tool call, or the
first attempt to write code. That is the whole measurement, so letting a run
continue past it buys nothing and costs tokens.

EXCEPT for `forbid_commands` tasks. There the measurement is an action the agent
must NOT take, which by definition cannot be observed at the first tool call --
the agent has to be given room to reach it. Those tasks set `max_turns`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ARMS = {
    # arm -> (server instructions, tool surface, skills)
    "baseline": ("old", "old", "old"),
    "instructions_only": ("new", "old", "old"),
    "full": ("new", "new", "new"),
}


def _first_tool_call(trace: list[dict]) -> str | None:
    for event in trace:
        if event.get("type") == "tool_use":
            return event.get("name", "").rsplit("__", 1)[-1]
    return None


def _first_tool_failed(trace: list[dict]) -> bool:
    """Did the FIRST tool call come back an error?

    Reading the name alone scores intent, not outcome, and the two came apart
    badly: when the asset registry was retired the instructions kept mandating
    `get_entity(ref="asset:<name>")`, every such call 422'd, and this eval scored
    each one a hit because the right tool name appeared first. A mandated call
    that cannot succeed is not compliance -- it is the instruction being broken --
    so an errored first call is now a MISS, and the eval can see the whole class
    of "the docs name a route with nothing behind it".
    """
    call_id: str | None = None
    for event in trace:
        if event.get("type") == "tool_use":
            call_id = event.get("id")
            break
    else:
        return False
    for event in trace:
        if event.get("type") != "tool_result":
            continue
        # Match on id when the trace carries one; otherwise the first result is
        # the first call's, since the run is capped at one meaningful action.
        if call_id is not None and event.get("tool_use_id") not in (None, call_id):
            continue
        return bool(event.get("is_error"))
    return False


def _forbidden_command(trace: list[dict], patterns: list[str]) -> str | None:
    """First trace command matching a forbidden pattern, if any.

    Some instructions do not change WHICH tool fires first -- they change what the
    agent does with the result. `_first_tool_call` cannot see that: it stops at the
    first tool_use. Scoring an action the agent must NOT take is still reading the
    trace, not judging prose, so it stays inside this eval's discipline.
    """
    for event in trace:
        if event.get("type") != "tool_use":
            continue
        blob = json.dumps(event.get("input") or {})
        for pattern in patterns:
            if pattern in blob:
                return pattern
    return None


def _correct(task: dict, called: str | None, trace: list[dict] | None = None) -> bool:
    forbidden = task.get("forbid_commands") or []
    if forbidden:
        # The measure IS the abstention. A task can forbid an action without
        # constraining the route the agent takes to decide against it.
        return _forbidden_command(trace or [], forbidden) is None
    expected = task.get("expect_first_tool") or []
    if not expected:
        # NEGATIVE control: the right behaviour is to call NONE of these tools.
        return called is None
    if called not in expected:
        return False
    # The call must also have WORKED. See _first_tool_failed.
    return not _first_tool_failed(trace or [])


def run_task(task: dict, arm: str, repeat: int) -> dict:
    """One agent session. Returns a scoreable record.

    The agent invocation is deliberately a subprocess against the real CLI
    rather than an in-process harness: the thing under test is what a REAL
    client does with these instructions, and an in-process rig would quietly
    diverge from the deployed prompt assembly.
    """
    cmd = [
        os.environ.get("EVAL_AGENT_CMD", "claude"),
        "-p", task["prompt"],
        "--output-format", "stream-json",
        "--max-turns", str(task.get("max_turns", 2)),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    trace = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    called = _first_tool_call(trace)
    return {
        "arm": arm,
        "task_id": task["id"],
        "repeat": repeat,
        "first_tool": called,
        "first_tool_failed": _first_tool_failed(trace),
        "forbidden_hit": _forbidden_command(trace, task.get("forbid_commands") or []),
        "correct": _correct(task, called, trace),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=sorted(ARMS))
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    tasks = yaml.safe_load((HERE / "tasks.yaml").read_text())
    out = args.out or HERE / "results" / f"{args.arm}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    instructions, surface, skills = ARMS[args.arm]
    print(
        f"arm={args.arm}  instructions={instructions} tools={surface} skills={skills}\n"
        f"{len(tasks)} tasks x {args.repeats} repeats = {len(tasks) * args.repeats} runs",
        file=sys.stderr,
    )
    print(
        "NOTE: configure the MCP server for this arm before running -- this script "
        "does NOT rewrite the deployed instructions for you, deliberately. Silently "
        "mutating a shared server mid-eval is how arms get mislabelled.",
        file=sys.stderr,
    )

    with out.open("w") as fh:
        for task in tasks:
            for repeat in range(args.repeats):
                record = run_task(task, args.arm, repeat)
                fh.write(json.dumps(record) + "\n")
                fh.flush()  # a killed sweep keeps the runs it already paid for
                mark = "ok " if record["correct"] else "MISS"
                note = " (errored)" if record["first_tool_failed"] else ""
                print(
                    f"  {mark} {task['id']:<28} -> {record['first_tool']}{note}",
                    file=sys.stderr,
                )
    print(f"\nwrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
