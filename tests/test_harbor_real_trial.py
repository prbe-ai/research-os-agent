"""Real-Harbor compatibility: stage an ACTUAL Harbor trial, not a fabricated tree.

test_harbor_export.py checks stage_trial_export against a hand-built trial dir;
this runs a genuine Harbor 0.18 trial (oracle agent — applies the task solution,
no model, no GPU) and stages the *real* on-disk trial tree it produces, so it
catches drift between our parser and Harbor's actual output.

Needs Docker + the harbor package; ``@pytest.mark.harbor`` auto-skips otherwise
(so this file collects cleanly on a laptop and runs on a Docker/CI host).

Step 3a: trial-tree capture only. The in-container begin/end sandbox snapshot is
step 3b.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from probe.connectors.harbor import parse_trial, stage_trial_export


def _write_oracle_task(task_dir: Path) -> None:
    """A minimal single-step Harbor task whose solution writes one file."""
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "solution").mkdir()
    (task_dir / "tests").mkdir()
    (task_dir / "task.toml").write_text(
        "version = \"1.0\"\n"
        "[task]\nname = \"probe/real-harbor-oracle\"\nauthors = []\n"
        "[verifier]\ntimeout_sec = 60.0\n"
        "[agent]\ntimeout_sec = 60.0\n"
        "[environment]\nbuild_timeout_sec = 300.0\ncpus = 1\nmemory_mb = 512\nstorage_mb = 1024\ngpus = 0\n"
        "[verifier.env]\n\n[solution.env]\n"
    )
    (task_dir / "instruction.md").write_text("Create /app/out.txt containing `done`.\n")
    (task_dir / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /app\n")
    (task_dir / "solution" / "solve.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf 'done\\n' > /app/out.txt\n"
    )
    (task_dir / "tests" / "test.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /logs/verifier\n"
        "if [ \"$(cat /app/out.txt 2>/dev/null)\" = \"done\" ]; then\n"
        "  printf '1\\n' > /logs/verifier/reward.txt; exit 0\nfi\n"
        "printf '0\\n' > /logs/verifier/reward.txt; exit 1\n"
    )


async def _run_oracle_trial(task_dir: Path, trials_dir: Path) -> Path:
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
    from harbor.trial.trial import Trial

    trials_dir.mkdir(parents=True, exist_ok=True)
    config = TrialConfig(
        task=TaskConfig(path=task_dir),
        trials_dir=trials_dir,
        agent=AgentConfig(name="oracle"),  # applies solve.sh; model-free
        environment=EnvironmentConfig(type="docker", delete=True),
    )
    trial = await Trial.create(config)
    await trial.run()
    return Path(trial.paths.trial_dir).resolve()


@pytest.mark.harbor
def test_stage_a_real_harbor_oracle_trial(tmp_path):
    task_dir = tmp_path / "task"
    _write_oracle_task(task_dir)
    trial_dir = asyncio.run(_run_oracle_trial(task_dir, tmp_path / "trials"))

    # Harbor really produced the single-step trial tree we parse.
    assert (trial_dir / "result.json").is_file()
    assert (trial_dir / "config.json").is_file()

    staged = stage_trial_export(
        trial_dir,
        tmp_path / "cap" / "real-trial",
        run_id=None,
        step_index=0,
        environment={"type": "docker"},
        correlation={"trial_id": trial_dir.name, "task_id": "probe/real-harbor-oracle"},
        expected_paths=("config.json", "result.json"),
        expand=False,
    )

    manifest = json.loads(staged.capture_manifest_path.read_text())
    capture = manifest.get("capture", {})

    # The SDK captured Harbor's real reward (the oracle solved the task).
    # parse_trial reads it from result.json's verifier_result (== 1.0), and
    # stage_trial_export records it in the capture manifest at verifier.reward.
    # The export-request DESCRIPTOR stores only reward_key (the metric name), not
    # the value — by design: the reward is logged as a metric at capture/consume
    # time (capture_trial), so it reaches Probe on both the direct and the durable
    # stage->export->consume paths. No reward is dropped.
    assert parse_trial(trial_dir).reward == 1.0
    assert manifest.get("verifier", {}).get("reward") == 1.0

    # Every declared file was collected + hashed, and completeness is complete.
    assert capture.get("completeness", {}).get("status") == "complete"
    files = manifest.get("files") or []
    assert files and all(item.get("content_hash") for item in files if item.get("role") != "symlink")

    # The producer wrote a probe-harbor-export/1 descriptor + a recovery archive.
    assert Path(staged.request_path).is_file()
    descriptor = json.loads(Path(staged.request_path).read_text())
    assert descriptor["correlation"]["step_index"] == 0  # the training-step join key
    assert staged.archive_path is None or Path(staged.archive_path).is_file()
