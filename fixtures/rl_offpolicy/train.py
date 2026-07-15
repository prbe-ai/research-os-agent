"""An off-policy RL training run, annotated with the Probe Research SDK exactly as we'd
expect a researcher (or their coding agent) to instrument it.

What gets tracked:
  - a run on the spine (experiment + hypothesis + config)
  - a non-disruptive code/env snapshot (execution record -> run.env_ref)
  - shadow-SoT foreign keys (as if the platform also logged to W&B / S3)
  - training metrics per episode (episode_return, td_loss, mean_q, epsilon, buffer_size)
  - a dimensioned metric (per-actor return, dimensions={"actor": i})
  - a trajectory span per actor-episode (the rollout boundary)
  - artifacts: a checkpoint pointer + a rollout .jsonl (the trajectory convention)
  - finish + an immutable experiment version (the "box")

Run standalone against a live Probe Research:
    export PROBE_BASE_URL=https://api.research.prbe.ai PROBE_TOKEN=probe_pat_xxxxxxxx
    python -m fixtures.rl_offpolicy.train

Or drive it with a client you built: run_training(client, cwd=".").
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import time

import probe

from .env import ChainWalk, QLearner, ReplayBuffer

HYPARAMS = {
    "n_states": 8,
    "actors": 3,
    "episodes": 40,
    "lr": 0.5,
    "gamma": 0.98,
    "eps_start": 1.0,
    "eps_end": 0.05,
    "eps_decay": 0.9,
    "buffer": 5000,
    "updates_per_episode": 64,
    "batch_size": 32,
    "seed": 0,
}


def run_training(client: "probe.Client", *, hyperparams: dict | None = None, run_name: str | None = None, cwd: str | None = None):
    hp = {**HYPARAMS, **(hyperparams or {})}

    # 1) open a run on the spine (experiment + hypothesis + config)
    run = client.run(
        experiment="offpolicy-chainwalk-qlearning",
        hypothesis="epsilon-greedy Q-learning with experience replay reaches the goal and TD loss converges within 40 episodes",
        name=run_name or f"qlearn-{int(time.time())}",
        source="local",
        config=hp,
    )

    # 2) non-disruptive code + env capture (execution record -> run.env_ref).
    #    Best-effort: needs a git repo; skip cleanly if unavailable.
    try:
        run.snapshot(cwd=cwd, include_env=False, include_gpu=False)
    except Exception:  # noqa: BLE001 - capture must never fail the run
        pass

    # 3) shadow source-of-truth foreign keys (as if a platform also logged elsewhere)
    run.link(wandb_run_id=f"wandb-{run.id[:8]}", s3_prefix=f"s3://mock-ckpts/{run.id}")

    envs = [ChainWalk(hp["n_states"]) for _ in range(hp["actors"])]
    buffer = ReplayBuffer(hp["buffer"])
    agent = QLearner(hp["n_states"], lr=hp["lr"], gamma=hp["gamma"], seed=hp["seed"])
    sampler = random.Random(hp["seed"])
    epsilon = hp["eps_start"]
    first_trajectory: list[dict] | None = None
    last_returns: list[float] = []

    for ep in range(hp["episodes"]):
        returns: list[float] = []
        for actor, env in enumerate(envs):
            s = env.reset()
            done = False
            ret = 0.0
            steps = 0
            trajectory: list[dict] = []
            while not done:
                a = agent.act(s, epsilon)
                s2, r, done = env.step(a)
                buffer.add((s, a, r, s2, done))
                trajectory.append({
                    "seq": steps, "action_type": "env_step",
                    "inputs": {"state": s, "action": a},
                    "outputs": {"next_state": s2, "reward": r, "done": done},
                })
                s, ret, steps = s2, ret + r, steps + 1
            returns.append(ret)

            # a rollout span = the trajectory boundary for this actor-episode
            run.span(
                "rollout",
                name=f"ep{ep}-actor{actor}",
                step_index=ep,
                status="completed",
                attributes={"actor": actor, "return": round(ret, 3), "length": steps, "epsilon": round(epsilon, 3)},
            )
            # per-actor return as a *dimensioned* series
            run.log({"actor_return": ret}, step=ep, dimensions={"actor": str(actor)})
            if ep == 0 and actor == 0:
                first_trajectory = trajectory

        # learner: off-policy TD updates sampled from the replay buffer
        td_losses: list[float] = []
        mean_q = 0.0
        for _ in range(hp["updates_per_episode"]):
            batch = buffer.sample(hp["batch_size"], sampler)
            if not batch:
                break
            td, mean_q = agent.update_batch(batch)
            td_losses.append(td)
        td_loss = sum(td_losses) / max(1, len(td_losses))

        # 4) the per-episode training metrics
        run.log(
            {
                "episode_return": sum(returns) / len(returns),
                "td_loss": td_loss,
                "mean_q": mean_q,
                "epsilon": epsilon,
                "buffer_size": float(len(buffer)),
            },
            step=ep,
        )
        last_returns = returns
        epsilon = max(hp["eps_end"], epsilon * hp["eps_decay"])

    # 5) artifacts. The checkpoint is recorded as a pointer (realistic: the platform
    #    wrote the file to S3; we record where it lives). Use path= to upload bytes
    #    via presign when R2 is configured.
    run.log_artifact(
        "qtable-final",
        uri=f"s3://mock-ckpts/{run.id}/qtable.json",
        kind="checkpoint",
        content_type="application/json",
        meta={"shape": [hp["n_states"], 2]},
    )
    # a rollout trajectory as a .jsonl artifact (fold #13 convention). path= uploads
    # via presign when R2 is available; otherwise it fails open to a reference.
    if first_trajectory is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for line in first_trajectory:
                fh.write(json.dumps(line) + "\n")
            traj_path = fh.name
        run.log_artifact("rollout-ep0.jsonl", path=traj_path, kind="trajectory")
        os.unlink(traj_path)

    # 6) close the box + mint an immutable experiment version manifest
    run.finish("completed", summary={"episodes": hp["episodes"], "final_return": sum(last_returns) / len(last_returns)})
    client.experiment_version(run.experiment_id, label=f"{run.name}-v1")
    return run


def main() -> None:
    client = probe.Client()  # PROBE_BASE_URL / PROBE_TOKEN from env or `probe login`
    run = run_training(client, cwd=os.getcwd())
    print(f"run: {run.id}  short_id: {run.short_id}")
    client.close()


if __name__ == "__main__":
    main()
