# Fixture: off-policy RL run, annotated with the SDK

A tiny but **real** off-policy RL training run (tabular Q-learning with an experience
replay buffer on a chain-walk MDP), instrumented with the `ros` SDK the way we'd
expect a researcher or their coding agent to do it. No ML deps; the agent genuinely
learns, so the tracked metrics move for real.

Files:
- `env.py` — `ChainWalk` MDP + `ReplayBuffer` + tabular `QLearner` (off-policy).
- `train.py` — the training loop, annotated with the SDK. `run_training(client)` is
  importable; `python -m fixtures.rl_offpolicy.train` runs it standalone.

## What it tracks

| SDK call | What lands |
|---|---|
| `client.run(experiment, hypothesis, config)` | the run on the spine + hyperparams |
| `run.snapshot()` | content-addressed execution record → pins `run.env_ref`¹ |
| `run.link(wandb_run_id=, s3_prefix=)` | `run.foreign_keys`¹ (shadow source-of-truth) |
| `run.log({episode_return, td_loss, mean_q, epsilon, buffer_size}, step=ep)` | per-episode metric series |
| `run.log({actor_return}, dimensions={"actor": i})` | a **dimensioned** series per actor |
| `run.span("rollout", ...)` | one trajectory span per actor-episode |
| `run.log_artifact("qtable-final", uri=...)` | a checkpoint pointer |
| `run.log_artifact("rollout-ep0.jsonl", path=...)` | a trajectory `.jsonl`² (fold #13) |
| `run.finish(...)` + `client.experiment_version(...)` | close the box + immutable manifest |

¹ `env_ref` / `foreign_keys` land on the real columns only against research-os **with
PR #15** (RunPatch parity). Without it they silently no-op; the metrics still land.
² `path=` uploads bytes via presign when R2 is configured; otherwise it fails open to a
reference (no crash, no lost run).

## Run it against a live research-os

```bash
pip install -e .                      # from the repo root: installs `ros` + `exp`
exp login --base-url https://api.research.prbe.ai --token ros_pat_xxxxxxxx
#   or: export ROS_BASE_URL=... ROS_TOKEN=ros_pat_...
python -m fixtures.rl_offpolicy.train
```

Prints the run id + petname. Then in the dashboard (or via `exp bundle <run>`) you'll
see the metric series climbing, the rollout spans, and the artifacts.

Verified end-to-end: the RL run drives a live research-os on real Postgres and every
tracked series/span/artifact reads back (see `research-os` `tests/integration/test_rl_fixture_lands.py`).
