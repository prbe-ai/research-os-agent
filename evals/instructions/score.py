#!/usr/bin/env python3
"""Score instruction-eval runs.

Reads run records (one JSON object per line) and reports, per arm, the fraction
of runs whose FIRST tool call was the one the task called for.

Deliberately dumb about prose: it reads the tool-call trace only. Whether the
agent explained itself well is a different question from whether it looked
before it acted, and only the second one is what the instructions claim to change.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

ARMS = ("baseline", "instructions_only", "full")


def _wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval.

    Normal-approximation intervals are wrong at the edges -- and the edges are
    exactly where this lands (0/50 and 50/50 are both plausible results). An
    interval that says [1.0, 1.0] for 50/50 would let a small sample read as
    certainty.
    """
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    d = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def score(records: list[dict]) -> dict:
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_arm[record["arm"]].append(record)

    report: dict = {"arms": {}, "per_task": defaultdict(dict)}
    for arm, runs in by_arm.items():
        hits = sum(1 for r in runs if r["correct"])
        low, high = _wilson(hits, len(runs))
        report["arms"][arm] = {
            "runs": len(runs),
            "hits": hits,
            "rate": round(hits / len(runs), 3) if runs else 0.0,
            "ci95": [round(low, 3), round(high, 3)],
        }
        per_task: dict[str, list[bool]] = defaultdict(list)
        for r in runs:
            per_task[r["task_id"]].append(r["correct"])
        for task_id, outcomes in per_task.items():
            report["per_task"][task_id][arm] = round(
                sum(outcomes) / len(outcomes), 2
            )
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", type=Path)
    args = ap.parse_args()

    records: list[dict] = []
    for path in args.results:
        for line in path.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    if not records:
        print("no records")
        return 1

    report = score(records)
    print(f"{'arm':<20} {'runs':>5} {'hits':>5} {'rate':>6}  95% CI")
    for arm in ARMS:
        row = report["arms"].get(arm)
        if not row:
            continue
        lo, hi = row["ci95"]
        print(f"{arm:<20} {row['runs']:>5} {row['hits']:>5} {row['rate']:>6.3f}  [{lo:.2f}, {hi:.2f}]")

    base = report["arms"].get("baseline", {}).get("rate")
    instr = report["arms"].get("instructions_only", {}).get("rate")
    full = report["arms"].get("full", {}).get("rate")
    if base is not None and instr is not None:
        print(f"\ninstructions alone:  {base:.3f} -> {instr:.3f}  ({instr - base:+.3f})")
    if instr is not None and full is not None:
        print(f"everything else:     {instr:.3f} -> {full:.3f}  ({full - instr:+.3f})")

    print("\nper task (rate by arm):")
    for task_id, arms in sorted(report["per_task"].items()):
        cells = "  ".join(f"{a}={arms.get(a, float('nan')):.2f}" for a in ARMS if a in arms)
        print(f"  {task_id:<28} {cells}")

    # Overlapping intervals are the honest read of a null result, and saying so
    # here stops the delta above from being quoted as a finding on its own.
    if base is not None and instr is not None:
        b, i = report["arms"]["baseline"]["ci95"], report["arms"]["instructions_only"]["ci95"]
        if b[1] >= i[0]:
            print(
                "\nNOTE: baseline and instructions_only intervals OVERLAP -- this "
                "sample does not establish an instructions effect. Do not quote "
                "the delta as if it did."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
