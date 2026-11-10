# RL frameworks and Harbor

Lookup for `instrument-code`: what to paste for each trainer Probe has an adapter for. Each records the
trainer's metrics AND every attempt (a rollout, a trajectory) as a **sample**: its task and scenario, reward
and parts, which weights made it, which step trained on it, and whether it `passed`, `failed` (graded below
the full reward) or hit an `error` (never graded). Read your framework's section, then run CHECK THE CAPTURE
on a two-step run before the long one.

| framework | install where it trains | the door |
|---|---|---|
| Miles: upstream `radixark/miles`, and the July fork `prbe-ai/miles@bcd95a00128a` | `probe-research[cli]` | `python -m probe.connectors.miles.launch <train script> ...` plus Miles hook flags |
| ART | `probe-research` | `probe.integrations.art.track(model)` |
| SkyRL | `probe-research` | `trainer.override_entrypoint=probe.integrations.skyrl.entrypoint` |
| verl | `probe-research` | `python -m probe.integrations.verl.launch -- python3 -m verl.trainer.main_ppo ...` |
| TRL | `probe-research` | `report_to="probe"` plus `probe.integrations.trl.track(trainer)` |
| Harbor 0.20-0.25 | `probe-research[cli,harbor]` | `harbor run ... --plugin probe`, or `probe trial add RUN jobs/<job>` after the job |

## RULES FOR ALL OF THEM:

- **The launchers keep THE ONE RULE for you.** Miles' and verl's launchers and SkyRL's entrypoint open the
  run BEFORE Ray starts and put Probe's `worker_process_setup_hook`, the `PROBE_*` settings and the run id in
  the job's runtime env, so every Ray actor configures itself. A `register()` in the driver alone records
  nothing from the actors. Let the framework start Ray: verl, in a process where Ray is already up, runs
  without Probe and says so.
- **Project.** Set `PROBE_PROJECT=<slug>` (ART and TRL also take `project=`); Miles also reads
  `PROBE_EXPERIMENT`, and defaults to project `miles`, experiment `miles-runs`. Under `probe exec` (or any
  `PROBE_RUN_ID`) they record onto that run instead.
- **Credentials in Ray.** Ray's dashboard shows a job's runtime env, so a launcher never copies your token
  into it: workers get a token for this run only, minted from yours (or the run token `probe exec` gave the
  launcher). A project-bound token cannot mint one: set `PROBE_MILES_WORKER_TOKEN`,
  `PROBE_SKYRL_WORKER_TOKEN` or `PROBE_VERL_WORKER_TOKEN` to the credential workers should use, knowing the
  dashboard shows it. With none, workers warn, record nothing and train on.
- **Task and scenario.** The scenario is the outside values a task is posed under (`{"set": "P2", "as_of":
  "2024-06-30"}`). It is part of the task's identity: the same prompt under two scenarios is two tasks, and
  every read filters on it (`--where scenario.set=P2`). With no `task_id`, the task key is a hash of the
  prompt.

  | framework | task key | scenario |
  |---|---|---|
  | Miles | `sample.metadata["task_id"]` | `sample.metadata["scenario"]`, a dict |
  | ART | `trajectory.metadata["task_id"]` or `["scenario_id"]` | metadata keys named by `track(model, scenario=["set", "as_of"])`, or a callable `(trajectory, group) -> dict` |
  | SkyRL | dataset column `task_id` | a dict column `scenario`, plus the columns named in `PROBE_SKYRL_SCENARIO=set,as_of` |
  | verl | `extra_info["task_id"]` | `extra_info["scenario"]`, a dict; `reward_model.ground_truth` is the label |
  | TRL | dataset column `task_id` | a `scenario` column, or columns named by `track(trainer, scenario=["set", "as_of"])`; `label` / `answer` / `solution` / `ground_truth` is the label |
  | Harbor | Harbor's task id (`local:` / `git:` / `package:`) | dataset, its version and hash, the task digest: all automatic |

- **Resume.** A relaunch from a checkpoint saved at step k reopens the SAME run with `Rewind(step=k+1)`:
  metric points from k+1 on are deleted and written again, and the earlier attempt's samples past k leave
  every read (`probe samples list RUN --include rewound` shows them). Keep the run's identity the same
  across relaunches (each section says which) and pass no `on_conflict` yourself.
- **No W&B.** Each adapter drops W&B or refuses to start beside it; nothing needs a W&B key.
- **Fail open.** A Probe error never stops training: a hook warns once, counts it, and the capture answers
  no. `PROBE_MODE=disabled` turns every adapter off.
- **Per-token arrays** (token ids, logprobs, entropy, the loss mask) are on by default;
  `PROBE_SAMPLES_PER_TOKEN=off` or `sample:0.1` for long agentic runs (error samples are always kept).

## MILES:

```bash
pip install "probe-research[cli]"         # in the image every Miles node runs
export PROBE_PROJECT=gsm-posttrain PROBE_EXPERIMENT=grpo-baseline
python -m probe.connectors.miles.launch train_async.py <your Miles args> --save /ckpt/grpo-1 \
  --custom-rollout-log-function-path probe.connectors.miles.log_samples \
  --custom-eval-rollout-log-function-path probe.connectors.miles.log_eval_samples
# sync mode only (Miles refuses it with --fully-async): dropped groups as samples too
#   --rollout-all-samples-process-path probe.connectors.miles.log_dropped_samples
```

- **Upstream args are frozen** (pydantic, no extra fields): there is no `--use-probe` flag, and never
  `setattr(args, ...)` in your script or a hook. The backend runs in every process the launcher registered
  it in; `PROBE_MILES=off` keeps it off. The hooks' own settings are Miles flags:
  `--probe-sample-metrics '{"agent/turns": "metadata.turns"}'` (extra per-sample numbers) and
  `--probe-reward-max 1.0` (the reward a sample passes at).
- **Each hook slot takes one function.** A job already using one calls Probe's from its own, with the same
  arguments (`probe.connectors.miles.log_samples(rollout_id, args, samples, rollout_extra_metrics,
  rollout_time)` returns False, so Miles still logs).
- **The July fork** turns a backend on with an args flag. Write this wrapper and launch it in the script's
  place (`python -m probe.connectors.miles.launch train_probe.py <your Miles args>`):

  ```python
  # train_probe.py, beside train_async.py
  import asyncio
  import train_async as miles_train     # or train
  from miles.utils.arguments import parse_args
  from miles.utils.tracking_utils.tracking import finish_tracking
  from probe.connectors.miles import register

  args = parse_args()
  register(args)                        # sets args.use_probe; the fork's actors read the run from args
  try:
      asyncio.run(miles_train.train(args))
  finally:
      finish_tracking()
  ```

- **Compute timeline** (upstream): `--use-miles-dashboard --dump-details /work/dump` also records phases per
  rank as spans, SGLang engine series (queue, running requests, KV cache) with an `engine` dimension, and
  the GPU map in the run's config.
- **Harbor inside Miles.** In the agent function that runs the Harbor trial, return
  `"external_key": str(result.id)` (the `TrialResult.id`) in the sample's metadata: Miles' sample and Harbor's
  trial become one sample. Stage that trial's files with the same key (`stage_trial_export(...,
  external_key=key)`, the file door in SKILL.md) and they link to it.
- **Run identity and resume.** The run is `MILES_RUN_ID`, else one named from the `--save` folder. Relaunch
  with the same `--save` and `--load` its `iter_N`: the run reopens rewound to N+1.
- **Worker credential**: `PROBE_MILES_WORKER_TOKEN`.

## ART:

```python
import art
import probe.samples
from probe.integrations.art import track

# WANDB_API_KEY must be unset: ART opens a W&B run whenever it is, so track() refuses to start
model = art.TrainableModel(name="gsm-rl", project="gsm", base_model="Qwen/Qwen2.5-0.5B-Instruct",
                           report_metrics=[])
tracker = track(model, project="gsm-posttrain", scenario=["set", "as_of"])   # + trainer=pipeline_trainer
await model.register(backend)              # LocalBackend
# ... your loop: model.log(groups, split="train" | "val"), backend.train(model, groups)
tracker.finish()                           # or let the process exit
```

- Records ART's metrics under ART's names, every `Trajectory` as a sample (a group's exception as an error
  sample, `val` / `test` as eval samples), each `backend.train` as that step's consumption, weight version
  and checkpoint (a LoRA up to 256 MiB uploaded, a bigger one recorded where it is), and a `PipelineTrainer`'s
  stale and zero-variance drops and queue depth.
- **RFT-SFT lineage.** An SFT example another run's weights wrote carries
  `metadata["probe_generated_by"] = probe.samples.generated_by(run=RL_RUN_ID, step=N)` before
  `model.train_sft(...)`: this run then derives from that one. Without it the writer is the base model.
- **ServerlessBackend** is W&B's own training service and needs `WANDB_API_KEY`, which `track()` refuses: use
  `LocalBackend`.
- **Run identity and resume**: `art:<project>/<name>`; a model resumed at checkpoint k reopens it rewound to
  k+1.

## SKYRL:

```bash
export PROBE_PROJECT=gsm-posttrain PROBE_SKYRL_SCENARIO=set,as_of
uv run --isolated --extra fsdp --with probe-research -m skyrl.train.entrypoints.main_base \
  trainer.override_entrypoint=probe.integrations.skyrl.entrypoint trainer.logger=console \
  trainer.project_name=gsm trainer.run_name=grpo-1 <your overrides...>
# fully async: the same entrypoint plus trainer.fully_async.enabled=true
```

- **Only `skyrl.train.entrypoints.main_base` reads `trainer.override_entrypoint`.** SkyRL's other mains
  (`examples/train/fully_async/main_fully_async.py`, the Harbor ones under `examples/train_integrations/harbor/`)
  do not, and run without Probe. SkyRL's example scripts (`run_gsm8k.sh`) end in `$@`, so they take the
  override as one more argument, but their `uv run --isolated` line needs `--with probe-research` too.
- SkyRL's default W&B logger becomes `console`, with a warning; every scalar row also goes to the run.
- **Harbor inside SkyRL.** When the generator is SkyRL's `HarborGenerator`, each Harbor trial merges into its
  SkyRL sample (needs `probe-research[cli]` beside SkyRL). SkyRL's Harbor mains are not that entrypoint (above),
  so this needs a custom experiment; it is unit-tested only.
- **Run identity and resume**: `skyrl:<trainer.project_name>/<trainer.run_name>`; SkyRL's default `test_run` (or an
  empty `run_name`) gets a key of its own (`...@<8 hex>`). Each save writes `probe_run.json` into `global_step_k/`; `trainer.resume_mode`
  `latest` or `from_path` reopens the attempt it names, rewound to k+1. A checkpoint without it (an older CLI's,
  a `from_path` bucket path) resumes a name you chose by name; on `test_run` or an empty name it opens a new run. `latest` on a
  bucket `ckpt_path` opens a new attempt (Probe cannot read the bucket's pointer).
- **Worker credential**: `PROBE_SKYRL_WORKER_TOKEN`.

## VERL:

```bash
pip install probe-research                # in the verl image
export PROBE_PROJECT=gsm-posttrain
python -m probe.integrations.verl.launch -- python3 -m verl.trainer.main_ppo \
  trainer.project_name=gsm trainer.experiment_name=grpo-1 <your overrides...>
# also -m verl.experimental.fully_async_policy.fully_async_main, -m verl.experimental.one_step_off_policy.main_ppo
```

- The launcher puts `probe` in `trainer.logger` (`['console','probe']`) and takes W&B out, with a warning.
  Each training batch is read in memory before the actor update: no `trainer.rollout_data_dir` needed.
- Not recorded yet: prompt groups a filter drops before the update.
- **Run identity and resume**: `verl:<trainer.project_name>/<trainer.experiment_name>`; verl's default `gsm8k` (or an
  empty `experiment_name`) gets a key of its own (`...@<8 hex>`). Each save (PPO and SFT) writes `probe_run.json` into `global_step_k/`;
  `trainer.resume_mode=auto` (or `resume_path`) reopens the attempt it names, rewound to k+1. A checkpoint
  without it (an older CLI's, a bucket path) resumes a name you chose by name; on `gsm8k` or an empty name it opens a new run.
- **Worker credential**: `PROBE_VERL_WORKER_TOKEN`.

## TRL:

```python
import probe.integrations.huggingface      # registers report_to="probe": the metrics
from probe.integrations.trl import track

args = GRPOConfig(output_dir="out/grpo-1", run_name="grpo-1", report_to="probe", ...)
trainer = GRPOTrainer(model=model, reward_funcs=rewards, args=args, train_dataset=ds)  # + task_id, scenario columns
track(trainer, project="gsm-posttrain")    # the samples, on the same run
trainer.train()
```

- Covers `GRPOTrainer`, `RLOOTrainer`, the experimental `AsyncGRPOTrainer`, and distillation
  (`DistillationTrainer`, `ServerDistillationTrainer`, `GKDTrainer`, the experimental `GOLDTrainer`,
  `AsyncDistillationTrainer`); `SFTTrainer` with `track(trainer, record_sft=True)` records each example once.
- **Distillation.** Name a teacher that is a Probe checkpoint: `track(trainer,
  teacher="checkpoint:<run>@step<N>")`, and the student run derives from it. GKD records teacher-written
  sequences only with `seq_kd=True`.
- Only the main process records; W&B in `report_to` is taken out, with a warning.
- **Run identity and resume**: `trl:<run_name>` (else the output folder's name);
  `trainer.train(resume_from_checkpoint=...)` at step k reopens it rewound to k+1.

## HARBOR:

```bash
pip install "probe-research[cli,harbor]"  # beside harbor 0.20-0.25
harbor run <your usual flags> --plugin probe --pk run=$RUN --pk step=0
#   --pk model_ref=checkpoint:<run>@step<N>   the agent serves a Probe checkpoint: this run derives from it
#   --pk project=<slug>                       no --pk run: a run keyed by the job id (job resume continues it)
#   --pk files=false                          samples only, no trial files
probe trial add $RUN jobs/<job> --step 0  # a job already run; --whole-run when the job is the whole run
```

- One sample per attempt, retries kept (the earlier one reads `superseded`); a multi-step trial is one
  sample per step. A reward Harbor marks invalid is no reward (`dropped(invalid_reward)`). An agent timeout
  the verifier still graded reads `failed`; a verifier that never ran reads `error`. Files upload after
  Harbor's own secret scrub.
- Only the plugin sees every retry: Harbor deletes a retried attempt's folder, so a local job folder holds
  final attempts only (`harbor jobs download --include-retries` keeps them).
- **Library mode** (your code owns the `Trial`): `probe.connectors.harbor.instrument_trial(trial, run=run,
  step=N)` before `trial.run()`, or `await run_trial(task_dir, trials_dir=..., run=run, step=N)`. It seals nothing:
  call `run.samples.seal(N)` once the step's trials are done, or the capture answers no.

## YOUR OWN LOOP:

No adapter: write the samples with `run.samples`. Each part is checked against the sample contract
before it queues: a part sending a field its role does not own (a generator part with `consumed_step`) is
dropped with a warning. A generator part's `prompt` and `label` become its task's, sent once per task.

```python
run = probe.init(project="gsm-posttrain", config=cfg)
run.samples.plan(expected_steps=steps, reward_max=1.0)          # 1.0 = a passing reward
for step in range(steps):
    rollouts = generate(policy, step)                           # made by the weights of version `step`
    run.samples.log([{"key": f"train:{step}:{i}", "task_key": r.task_id, "scenario": r.scenario,
                      "prompt": r.prompt, "label": r.answer,
                      "group_key": f"train:{step}:{r.group}", "split": "train", "scheduled_step": step,
                      "generated_by": [{"model": {"kind": "policy", "version": step}}],
                      "response": r.text, "reward": r.reward} for i, r in enumerate(rollouts)])
    run.samples.log([{"key": f"train:{step}:{i}", "consumed_step": step, "consumed_version": step}
                     for i in range(len(rollouts))], role="trainer")
    update(policy, rollouts)
    run.samples.commit(step)                                    # its consumed samples read `trained`
    run.samples.seal(step)                                      # every sample of this step is sent
run.samples.plan(final_step=steps - 1, is_closed=True)
```

## CHECK THE CAPTURE:

On a two-step run, before the long one:

```bash
probe samples capture $RUN                                      # yes, or no with the reasons
probe samples stats $RUN --by step --measure mean_reward,count  # = the trainer's own mean reward per step
probe samples list $RUN --result error --limit 5                # what broke; one sample's log: probe samples logs SAMPLE
probe samples stats $RUN --by host --where result=error         # where errors come from (also engine, worker, error_reason)
probe checkpoint best $RUN --metric eval/val                    # the checkpoints landed, with their lineage
```

- Capture answers yes or no, never unknown, with reason codes: `in_progress`, `unsealed_step` (a step no
  producer closed), `missing_samples` (fewer arrived than a seal declared), `not_committed`,
  `producer_open` / `producer_silent` / `producer_gone`, `bundle_missing` (a log or token bundle),
  `batch_refused`, `completion_unproven`.
- The stats mean must equal the framework's own logged mean (Miles `rollout/raw_reward`, ART `reward`) over
  the same steps: a gap means samples went missing or were counted twice. `error` samples are left out of
  means and counted beside them.
- `probe samples tasks $RUN` lists one task per prompt and scenario. A task per attempt means the prompt
  the hash keys changes every time (a seed, a timestamp): set `task_id`.

## LIVE: WHILE IT RUNS:

Every adapter also writes each rollout as a LIVE sample, from its start to its end. Read and act on them:

```bash
probe samples live $RUN                     # running now: elapsed, host, phase, what the agent is doing
probe samples live $RUN --hung --json       # no progress for 10 min (--hung-after 3m), or a silent producer
probe samples live $RUN --slow              # slower than its task's p90 and 2x its step's median
probe samples live $RUN --failing           # errors in the last 10 min by reason, host and task
probe samples live $RUN --state ended       # how each ended and why; abandoned = a crash or a resume, superseded = retried
probe samples tail $RUN SAMPLE              # its turns as they happen, until it ends (where watch works)
probe samples cancel $RUN SAMPLE --reason "tool loop"   # or retry, where its framework allows (table); prints the answer
probe samples commands $RUN                 # who asked what, and what happened
probe watch $RUN --until samples.live:state=hung --timeout 3600   # sleep until one hangs (exit 0)
```

- Judged on the server's clock: a box with a wrong clock never makes a sample look hung, and a crash or a resume
  reads `abandoned`, never hung.
- The agent that launched the run hears of hung samples through the Probe daemon (`samples_hung`). With no daemon
  (inline, or a plain script), wait for them: `probe watch $RUN --until samples.live:state=hung`.
- An ended sample shows its outcome and why: `dropped (group_has_aborted)` is a Miles sibling dropped with a canceled
  sample's group.
- An ask a framework cannot do safely is refused at once, with why; one no running producer took reads `expired`.
  The live list can show an end a few seconds late, so an ask on a try that just ended is refused (`the sample
  already ended`): pick a newer one.
- A sample that ends `ungraded` ended cleanly; a judge grades it later.

| framework | live samples | watch | cancel | retry |
|---|---|---|---|---|
| Miles | every try, before its first request (`PROBE_MILES_LIVE=0` off; not the legacy v1 rollout) | agent rollouts | generate rollouts, by an SGLang abort; **Miles then drops the sample's whole group** from training (the answer names it) | generate rollouts, in the same slot: the group still trains |
| Harbor | every trial, with its phases | refused | refused: it makes `harbor run` exit 1; after the job, `harbor job resume -p <job>` re-runs cancelled trials | refused |
| ART | only rollouts you decorate (below) | refused | refused: it raises in your gather, and ART's default `max_exceptions=0` stops the step | refused |
| SkyRL | every trajectory; Harbor inside SkyRL, every try | refused | refused: SkyRL retries a failed trial and masks its prompt group | refused |
| verl | every agent loop | refused | refused | refused |
| TRL | every row of a GRPO, RLOO or distillation batch; none in AsyncGRPO, GKD, GOLD | refused | refused | refused |

ART awaits your own rollout coroutines, so decorate the rollout:

```python
tracker = track(model, project="gsm-posttrain")

@tracker.record_rollout(task_key=lambda model, scenario: scenario.id)   # the key its trajectory's task_id holds
async def rollout(model, scenario) -> art.Trajectory:
    ...
    tracker.send_progress(phase="tool", turns=turn, activity="tool bash: pytest -x")   # each model or tool call
    return trajectory
```

Each call is one live sample until it returns, and its trajectory names it. A rollout that sends no progress for
10 min reads hung. It ends graded by the reward it set, else `ungraded`; a raise ends it `error` and still reaches
your gather.

## LIVE IN YOUR OWN HARNESS: THE PROXY:

No adapter, or an agent loop the adapter cannot see: give each rollout's agent its own model URL. Probe's proxy, in
your process, forwards every call to the real server byte for byte and records the rollout's progress and, while
`probe samples tail` watches it, its turns.

```python
run.samples.attempt_start(key, task_key=task.id, scheduled_step=step, host=socket.gethostname())
url = run.samples.live_url(key, upstream="http://router:30000/v1")   # any OpenAI or Anthropic API root
result = run_agent(task, base_url=url)                               # the agent's client calls url, not upstream
run.samples.attempt_end(key, "passed" if result.reward >= 1.0 else "failed")
```

- It never breaks training: it never waits on Probe's server; `live_url` returns `upstream` itself when the proxy is
  off (`PROBE_LIVE_PROXY=0`) or unhealthy; a proxy that dies fails only its own rollouts (`error`, `probe_proxy`).
- Cancel is off: `live_url(..., can_cancel=True)` lets `probe samples cancel` stop that rollout (its calls then answer
  `403 probe_canceled`, which the OpenAI and Anthropic SDKs never retry). Only where your loop ends a rollout cleanly
  on that error.
- Another language, or a launcher that cannot host it: run it beside the harness.

```bash
probe samples proxy --upstream http://router:30000/v1 --run $RUN   # prints one JSON line; "control" is $CONTROL
curl -s -X POST "$CONTROL/rollouts" -d '{"task_key": "gsm-17"}'    # {"attempt_key", "url"}: url is the agent's
curl -s -X POST "$CONTROL/rollouts/$KEY/end" -d '{"outcome": "passed"}'
```
