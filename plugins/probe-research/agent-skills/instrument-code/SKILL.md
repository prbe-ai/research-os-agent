---
name: instrument-code
description: Wires the Probe SDK (`probe.init`, `probe.log`) into a script that trains, evaluates, sweeps or serves, so that it records run-level metrics, and an RL trainer (Miles, ART, SkyRL, verl, TRL) or a Harbor job, so that it records every rollout as a sample. Use when writing or changing any such script, launching a training script on another machine, when a trainer already has its own integration (Miles, ART, SkyRL, verl, TRL, Harbor, Ray), and when a run finished green but recorded nothing, less than expected, or onto the wrong run. HARD TRIGGER - ENSURE YOU READ BEFORE ANY PAID GPU HOURS.
---
# Instrument code

Put the SDK in scripts you write. Use `probe exec` only for code you can't edit,
or a launcher that only submits the job. Both open the run.

Example:

```python
import probe
probe.init(project="folding", description="DockQ sweep over sampling temperature", tags=["dockq"],
           config=args, intent="does a lower sampling temperature improve DockQ?")
# Every run needs a project, named by its slug or id (a name never finds one); a new slug,
# lowercase words joined by "-", creates it. A run naming none raises. In the `daemon` state,
# on this machine pass project=probe.FROM_ENV (`probe exec --project-from-env`): the run goes
# to this session's project, which the daemon sets (`probe session home show` prints it); before it
# is set, probe.init() waits for it (up to 90 s, said on stderr). FROM_ENV also reads
# PROBE_PROJECT and this computer's default project (an AttributeError there means that
# environment's probe-research is older: upgrade it). On another machine pass a slug; the
# daemon may move the run.
# `on` state: name the experiment you created too, e.g.
# probe.init(project="folding", experiment="dockq-sweep", question="does a lower sampling temperature improve DockQ?")
probe.log({"loss": 0.42}, step=42)   # ambient: no handle to thread through call frames
probe.finish()
```

Pass the run's whole config object as is - `args`, a Hydra `DictConfig` (never a
resolved copy), a dataclass or a dict - not a few picked keys. Keep credentials
out of it. A trainer integration that already records config needs none.

## FILES AND LOGS ARE CAPTURED FOR YOU:

When a run ends - `finish()`, a crash, the script exiting, or a hard death (a
segfault, the OOM killer, SIGTERM) - the SDK saves what it produced, on any
machine, including remote jobs:

- every file the run CREATED OR CHANGED in its working folder, as
  `outputs/<its path there>`; `.git`, `.venv`, `node_modules`, caches,
  credential-shaped names (`.env`, `*.pem`, `.pgpass`) and, in a home folder,
  every dot-entry (`.kube`, `.docker`) are skipped
- everything it PRINTED, from `import probe` (model loading included) to its
  exit, C-level output too, as the run's Logs (stored as `probe/run.log`,
  first 2 MB + last 8 MB, a redrawn progress bar kept as its last frame).
  Under `probe exec` the child's output is teed the same way, and `probe exec
  torchrun` interleaves its local ranks in that one log; every other rank that
  joins the run keeps its own `probe/run.rank<N>.log`. A notebook's output is
  not kept (a warning says so)

A close uploads files up to 64 MB and spends at most 120 s inspecting them
(`PROBE_CAPTURE_BUDGET_SEC`, or the finish timeout). When your team has big-file
capture switched on, a file between 64 MB and 5 GB goes up in parts after the
run ends instead (only its first 16 MB is checked for credentials, and one with
a credential there stays a pointer), wherever the outbox outlives the process. Any other file past a limit, or one the gate cannot
inspect, becomes a pointer - path and size, no bytes - on storage that lasts (a network drive, a Modal Volume, your own
machine). On a throwaway box's own disk (a container, a pod, CI) it cannot be
kept, so it is listed in `probe/outputs-manifest.json` with a warning: write
checkpoints there to a bucket or a mounted volume instead.

Still call `probe.log_artifact` for:

| case | why |
|---|---|
| a file OUTSIDE the run's folder | the sweep only sees that folder |
| a name or kind you choose | captured files are named by path and never filed `checkpoint`; `log_artifact` files `.ckpt`/`.safetensors`/`.pt`/`.pth` as `checkpoint` itself |
| a checkpoint written straight to a bucket | `log_artifact(name, uri="s3://...")` records where it is |

Runs that SHARE a folder at the same time (sweep workers, parallel seeds) cannot
tell whose files are whose, so capture skips them and warns. Give each run its
own: `probe.init(..., outputs=f"results/{seed}")` (`probe exec --outputs`).

Opt out with `probe.init(capture_outputs=False)`, `PROBE_CAPTURE_OUTPUTS=0` or
`probe exec --no-capture-outputs`. To keep only some paths out (datasets,
checkpoints), list them in a `.probeignore` at the git toplevel (gitignore
syntax) or `PROBE_IGNORE`: that covers code, output and read capture.
`PROBE_CAPTURE_LOG=0` keeps outputs but drops
the log - and with it capture after a hard death, which the log's helper runs.
Nothing survives a death that takes the whole container or job step with it (the
program as a container's PID 1): write to lasting storage there.
On SIGTERM (a preemption, `docker stop`, a deleted pod) the SDK spends up to
`PROBE_SIGTERM_FLUSH_SECONDS` (20 s; `0` turns it off) sending what is queued,
closes the run `failed` (`probe_finish.reason` = `preempted`), then exits by
SIGTERM. A `kill` from another process of yours on the machine (stopping your own
job) closes it `canceled` instead (`reason` = `stopped`). A SIGTERM handler
installed before `probe.init()` runs first.
`log()` queues to local disk and returns; the run's rows go out every
`PROBE_SEND_INTERVAL_SEC` (5 s; `0` sends at once), so the page trails by that
much, and `finish()` sends the rest without waiting. A power cut can lose the
last ~2 s of rows (`PROBE_OUTBOX_FSYNC=each` fsyncs each row, slower).

## UPLOAD ONLY WHAT A RERUN NEEDS:

Before the first run in a folder, write a `.probeignore` there (gitignore
syntax; `!` re-includes) so the code capture holds what a fresh sandbox needs to
rerun it: the code the run executes, its configs, and its build and dependency
files (lockfiles, `pyproject.toml`, `Cargo.toml`). Leave out what those rebuild
(build output, venvs, caches), data and checkpoints (link them instead), other
projects in the folder, and earlier runs' outputs. Update it when the run starts
using something new.

## THE ONE RULE:

**A process that emits telemetry must itself be configured to emit it.**
Registering a backend mutates ONE interpreter's memory; a parent's config never
reaches a child, and neither does `probe.init()`'s ambient binding - a
contextvar over a process default.

```
launcher process          config lives here
  └── trainer subprocess         ...but not here
        └── Ray driver                 ...nor here
              └── Ray actor  <-- logs the metrics, knows nothing
```

The subtle one, which raised nothing: a flag set on `args` in the Ray driver
ARRIVES in the actors (`args` pickles) while the registry entry does not - every
check passes, nothing initialises.

### FIXES, IN ORDER:
1. Configure inside the emitting process - the actor's `__init__`, or Ray's `worker_process_setup_hook` (once per worker, before actor code)
2. Pass config through what crosses the boundary - `runtime_env["env_vars"]`; Ray workers do NOT inherit the submitter's environment
3. Assert it landed where it is READ, not where you set it
4. `import probe` needs `probe-research` in THAT process's environment (`uv add probe-research`). The wizard's CLI install is isolated and NOT importable; `probe` and `probe-agent` on PyPI are unrelated packages owned by other people

The RL launchers (RL TRAINERS below) do 1 and 2 for you: they open the run
before Ray starts and put the setup hook, `PROBE_*` and a token for that run
only in the job's runtime env. 3 stays yours: check the capture on a short run.

## SWITCHING OFF W&B:

A script that logs to W&B (`import wandb`) moving to Probe: follow `instrument-code/from-wandb.md` (beside this
file). It holds the order (both for one run, check parity, then remove W&B) and the call map.

## RL TRAINERS AND HARBOR:

Each records the trainer's metrics and every attempt (rollout, trajectory) as
a SAMPLE: task, scenario, reward, the weights that made it, the step that
trained on it, passed / failed / error. Before launching, read your
framework's section of `instrument-code/rl-frameworks.md` (beside this file):
the lines to paste, the task and scenario fields, the worker token, resume.

| framework | the door |
|---|---|
| Miles (upstream and the July fork) | `python -m probe.connectors.miles.launch train_async.py ...` + `--custom-rollout-log-function-path probe.connectors.miles.log_samples` |
| ART | `probe.integrations.art.track(model)`, `WANDB_API_KEY` unset |
| SkyRL | `trainer.override_entrypoint=probe.integrations.skyrl.entrypoint` on `skyrl.train.entrypoints.main_base` |
| verl | `python -m probe.integrations.verl.launch -- python3 -m verl.trainer.main_ppo ...` |
| TRL | `report_to="probe"` + `probe.integrations.trl.track(trainer)` |
| Harbor | `harbor run ... --plugin probe --pk run=RUN` (`probe-research[cli,harbor]`) |

A per-sample number of an RL attempt is a sample's, never a labeled metric
point. Then prove the capture on a two-step run: `probe samples capture RUN`
says yes, or no with its reasons.

## WHAT MUST REACH THE JOB:

Three things, none of which travel on their own - the PACKAGE, the run id +
epoch, and a TOKEN. `probe exec --token-ttl <the job's expected length, plus a
margin>` exports `PROBE_RUN_ID`, `PROBE_RUN_EPOCH` and a `PROBE_TOKEN` that
records that one run and nothing else into its child, and `probe.init()`
attaches, but that export dies at the machine boundary:

| Launcher | id + epoch + token | the package |
|---|---|---|
| Modal | `secrets=[modal.Secret.from_local_environ(["PROBE_RUN_ID", "PROBE_RUN_EPOCH", "PROBE_TOKEN"])]` (local env is not forwarded; never a literal - the snapshot uploads this script) | `modal.Image.debian_slim().pip_install("probe-research")` |
| Ray | `runtime_env={"env_vars": {...}}`, the same three (Ray's dashboard shows them: only ever a run's own token) | `runtime_env={"pip": ["probe-research"]}` |
| Slurm | `sbatch --export=ALL,PROBE_TOKEN,PROBE_RUN_ID=...,PROBE_RUN_EPOCH=...` | the venv the job activates |
| Docker / k8s | `-e PROBE_TOKEN -e PROBE_RUN_ID=...` / `env:` on the pod | the image |

When they are set (the token alone is enough), `probe.init(config=args)` joins the run
the launcher opened. A `project` / `experiment` that names that run's own home (or
`project=probe.FROM_ENV`) is accepted, so a script that carries its slugs runs either way;
a different one raises, and `name` / `question` are ignored with a warning. The config
still lands on that run. If the launcher only submitted the job, `probe exec` opened the
run AWAITING ATTACH and this same call takes ownership.

The credential is `PROBE_TOKEN`, plus `PROBE_BASE_URL` if you are not on the
default server; there is NO `PROBE_API_KEY`. Give the job the run's own token,
the `--token-ttl` one above (a `--detached-launcher` hand-off gets one anyway).
If the run is still live near expiry, Probe alerts you and the researcher:
`probe run token <run> --extend 7d`. Only a job that opens runs of its own
needs the researcher's `probe token create --name <job> --scope read --scope
write`, run in their own terminal and kept in the platform's secret store.
Recording from the launcher after the job ends loses its curves, logs and data
reads - say so before offering it.

On Kubernetes the outbox sits on the container's own disk and its background
worker dies with the pod, so writes still queued when the pod is deleted are
lost. Put `PROBE_OUTBOX_DIR` on a persistent volume, set
`PROBE_FINISH_TIMEOUT_SEC=600`, and give the pod a
`terminationGracePeriodSeconds` at least that long. Deleting the pod sends
SIGTERM, which gets `PROBE_SIGTERM_FLUSH_SECONDS`: raise it with the grace period.

A run id names a row, not an attempt: without the EPOCH a stale process holding
an old id attaches to a row a newer attempt reopened, and inherits its write
authority. Several ranks on one id is fine - the first reopens, the rest join.

## WHICH RUN ARE YOU WRITING TO:

PyTorch Lightning: `Trainer(logger=ProbeLogger(experiment=..., name=...))`, from
`probe.integrations.lightning` (install `probe-research[lightning]`). It adopts a
run `probe.init()` already opened, logs from global rank 0 only, records kept
checkpoints as path references, and never finishes its run mid-script - the
process exit, or your `probe.finish()`, does. A sweep loop gets one run per
Trainer: a new ProbeLogger closes the run the previous one opened.

Hugging Face Trainer: `import probe.integrations.huggingface`, then
`TrainingArguments(report_to="probe")` - or pass
`callbacks=[ProbeCallback(experiment=..., name=...)]` to choose the run (install
`probe-research[huggingface]`; use one or the other, not both). Same rules as
the Lightning logger: world-zero process only, adopts an open run, checkpoints
as path references, one run per Trainer, never finishes its run.

Both record each checkpoint's LOCATION by default; one on a job's own throwaway
disk (a pod, a container, CI) warns, because it is gone with the job. To store
the files, pass `checkpoints="upload"` (`ProbeCallback` / `ProbeLogger`), or set
`PROBE_CHECKPOINTS=upload` with `report_to="probe"`: each save is uploaded as
its own set, folders included (rank 0's view: shards kept on other nodes' own
disks are not gathered). Get one back with `probe artifact download
--run <run> --checkpoint latest|best -o DIR` (`client.checkpoint(run_id,
"best")`, `client.download_checkpoint(row, dir)`).

Ray Tune: one run per trial, opened inside the trainable. A function
trainable's run closes itself when Tune ends the trial - after its last
iteration, or at a scheduler's early stop (ASHA, median stopping) - as
`completed`. A `tune.Trainable` class must call `probe.finish()` in `cleanup()`:
Tune waits for `cleanup()`, then SIGKILLs the trial's processes 200 ms after
its SIGTERM, too soon for the exit hook's close. Not closed there, the run
stays `running`, or reads `failed` (the SIGTERM taken for a preemption).

A trainer integration (Miles, ART, SkyRL, verl, TRL, Harbor, Ray, etc) may
open its OWN run and ignore the id you supply. Steer it with the knobs it reads
(`PROBE_PROJECT`, experiment, run name, external id), then check where it
actually wrote:

- the WRITER knows - each RL adapter keys its run so a relaunch finds it again
  (`rl-frameworks.md` names each key), and every Ray worker's runtime env
  carries the `PROBE_RUN_ID` it joined
- a default project name in a run spec quietly collects runs that belong
  elsewhere (Miles: `miles`)
- an empty run proves where you looked, not what happened

## METRIC OR SPAN:

| Your work | Use |
|---|---|
| a flat loop repeating phases (step 12 is not inside step 11) | metrics, one series per phase |
| nested work of varying structure (trial -> agent turns -> verifier) | spans |
| one value per step | `probe.log({...}, step=i)` |
| one final number (test accuracy) | `probe.log({"test/acc": acc})` once - shows as a single number, not a chart |
| a value per sample | LABELS, never dimensions - per-sample ids shatter one series into hundreds of one-point tiles. Labeled points NEVER plot, so the curve needs a second, unlabeled key |
| an RL attempt (rollout, trajectory) and its numbers | a SAMPLE: the trainer's adapter, or `run.samples.log` in your own loop (RL TRAINERS AND HARBOR) |

A score of a training run's checkpoint is a metric on that run at the
checkpoint's step (`probe log RUN key=value --step N`), not a new run.

`spans: 0` on a training run is usually correct - check the phases are not
already timing metrics.

Spans have no heartbeat and no reaper. In the two-call form (`run.span(...)`,
then a second call with `id=` to close it), one opened before the work leaks on
every path that raises: record it AFTER, with real start and end timestamps.
`with probe.span(...)` closes on every path (`failed` when an exception escapes).

`span_type="rollout"` is the only thing you emit that becomes an entity: each
materializes an addressable TRIAL keyed by that span's id. Pass a real `name=`
and `attributes`: the name is the trial's name until a generated title lands,
and that happens only once the run is terminal, written FROM `attributes`.
Sparse attributes give 500 opaque rows.

Per-task eval results are one rollout span per task: its scores in
`attributes`, its output files linked to it, so the tasks read and count across
runs. From a shell, `probe span add` prints the span id; upload each output with
`--span <span id>`.

```python
with probe.span("rollout", name=task.id, external_key=task.id) as span:
    out = solve(task)
    span.attributes.update(score=out.score, unfinished_reason=out.reason)
    probe.log_artifact(f"eval/{task.id}/output.png", path=out.image, span_id=span)
```

## NAME WHAT A CRASH WAS PROCESSING:

Wrap each batch or sample of the loop so a crash email names what it died on.
It sends nothing and changes no metric, so enter it every batch:

```python
for epoch in range(epochs):
    for i, batch in enumerate(loader):
        with probe.context(batch_id=i, epoch=epoch):   # or sample_id= / task_id=
            train_step(batch)
```

Keys: `batch_id`, `sample_id`, `task_id`, `prompt_id`, `epoch`, `split`,
`phase` - ints or strings (a whole-number float epoch is kept; `epoch=int(...)`
otherwise), plus `step` as a non-negative int; anything else is dropped. Wrap
the code that can raise, and never `yield` inside the block. Never
`run.unit(labels=/coords=)` for this: it stamps every `log()` inside, and the
curve stops plotting.

## DECLARE WHERE A METRIC MUST STAY (OPTIONAL):

When a metric has a range you are SURE of - an accuracy in [0, 1], a loss that
must never pass some ceiling - declare it once, after `probe.init()` and before
the loop. The run's creator is emailed the first time a value leaves it:

```python
probe.expect({"val/acc": (0.5, 1.0), "train/loss": (None, 20)})  # None = open end
```

Only ranges you are sure of: a wrong range emails someone about a healthy run.
Leave it out when unsure - NaN and inf are reported without any declaration. It
never raises and changes no metric. For a run already going:
`probe run expect <run> val/acc --min 0.5 --max 1`.

## HARBOR TRIALS - THE FILE DOOR:

Harbor writes each trial to a directory (`result.json`, `agent/trajectory.json`,
verifier logs). Probe captures that directory into the run, keyed to the
training step. Rollout spans above are the other door; this one needs no code in
the agent. A whole Harbor job is samples, one per attempt:
`harbor run ... --plugin probe` as it runs, or `probe trial add $RUN jobs/<job>`
after (`rl-frameworks.md`, HARBOR).

```
probe trial add $RUN <trial-dir> --step 600               # rollout span + reward metric + files + manifest
probe trial add $RUN <trial-dir> --step 601 --no-expand   # raw only
```

A recognised trajectory format (ATIF v1.x) expands into turn/tool_call spans. An
unknown one is stored raw, never rejected; `probe trial expand $RUN
<manifest-id>` expands it later, idempotent.

With Miles, the trainer writes one export descriptor per trial and nothing
uploads until something consumes it (key it by the attempt's `TrialResult.id`,
as Miles' sample is: `rl-frameworks.md`, MILES):

| step | command |
|---|---|
| copy + checksum the host trial tree to a durable volume, no network | `probe trial stage <host-trial-dir> --to /shared/probe/trial-601 --expect result.json` |
| consume one descriptor | `probe trial export <dir>/export-request.json` |
| retry every unfinished one under a root | `probe trial drain /shared/probe/captures`; `--run "$PROBE_RUN_ID"` binds descriptors written before the run existed |
| keep exporting as trials land | `probe trial watch /shared/probe/captures --interval 5` - run it somewhere that outlives the job |
| republish the completeness manifest | `probe trial reconcile "$PROBE_RUN_ID" <staged-trial-dir>` |

`stage` keeps `.probe-capture.json` beside the bytes. Collection status and
upload status are separate, so an exporter outage leaves a retryable list, not
lost paths. Stable keys make a retry update the same rollout; same-step samples
stay distinct under the `sample` and `group` labels.

## DO NOT DUPLICATE, DO NOT BLOCK:

Capture what the trainer's integration CANNOT know - your reward function,
scoring sandbox, dataset transform. Re-emitting its phase timings, throughput,
loss or config buys hot-path latency and two sources of truth that will
disagree.

- Never call HTTP synchronously inside a coroutine - in an async reward it
  serialises every other sample and stalls the timers `asyncio.wait_for` depends
  on, so timeouts overshoot silently. Use `asyncio.to_thread`, or a queue
- `run.log`/`probe.log` date each point with its call time; pass `wall_clock=`
  only for an event that happened earlier. `log_derived*` and older SDKs send
  none, and the server then stamps ARRIVAL - behind the queue below, drain time
- Prefer the SDK's durable on-disk queue to per-event network calls
- Cache handles per process, but cache only SUCCESS - the first attempt often
  races the run's creation, and a cached miss disables capture for the life of
  the worker

## WHERE THE QUEUE LIVES:

Every process that records - a rank, a Ray actor, a launcher's driver - queues
in its machine's outbox (`~/.local/state/probe/outbox`, or `PROBE_OUTBOX_DIR`;
a rank gets its own `rank-*` folder there), never in `cwd`: Ray DELETES a job's
`cwd` on completion. A detached worker sends it and outlives the process, so a
killed actor's queued rows still go. A queue of your own follows the same rules:
never under `cwd`, on a disk that outlives the job, drained explicitly at the end.

## VERIFY AT THE DESTINATION:

The exit code says the process ended, not what was stored.

```python
run = client.get_run(run_id)
assert run["counts"]["metrics"] > 0
assert len(client.run_series(run_id)) < 50   # else: a wall of one-point tiles
# on a box you are about to destroy, a file:// pointer resolves nowhere afterwards:
assert all(not a["uri"].startswith("file://") for a in client.list_run_artifacts(run_id))
```

Series count near point count means something high-cardinality became a
dimension - fix the shape before spending more compute. Print which outcome
occurred: drained, written but unpublished, nothing written.

An RL run: `probe samples capture RUN` must say yes, and
`probe samples stats RUN --by step --measure mean_reward,count` must match the
trainer's own mean reward per step (`rl-frameworks.md`, CHECK THE CAPTURE).

A `--reference` checkpoint or dataset on a box you are about to destroy resolves
nowhere afterwards. Code is exempt - always uploaded, and `--reference` on it is
refused.

## WHAT THE RUN READ:

`probe.init()` records every file the run opens for reading (path + sha256) and
sends the list when the run finishes; the server links each one to the run that
WROTE those bytes, which is how a follow-up run gets its real parent. `probe
exec` does the same for a Python child. Nothing to add - but know its edges:

- It sees `open`, `pathlib`, numpy, pandas, `torch.load`, PIL, pickle, and
  `pyarrow.parquet`, `pyarrow.memory_map`, `h5py`, `safetensors`. It does NOT
  see other readers that open files from C - a `pyarrow.dataset` read directly,
  feather, sqlite. For those, say it yourself in the script:
  `run.use_dataset("dataset:NAME@V")` or `run.use_checkpoint("checkpoint:RUN@best")`;
  for an artifact,
  `probe edge add --source run:$RUN --target artifact:$ID --relation consumes`
- `Client.run(...)` handles record only with `capture_reads=True`
- Opt out: `probe.init(capture_reads=False)` or `PROBE_CAPTURE_READS=0` (reads
  and writes). The files it WRITES are recorded too (path + final sha256); off
  alone with `capture_outputs=False`, `PROBE_CAPTURE_OUTPUTS=0` or
  `PROBE_CAPTURE_WRITES=0`
- A read is evidence, not a link. A wrong data link: `probe edge remove ID`,
  then `run.use_dataset` / `run.use_checkpoint`
- Where the Probe daemon records, `probe edge`, `dataset use` and `checkpoint
  use` are refused: ask its writer, `probe ask --writer "<the link or fix>"`

## RELAUNCHING A RUN:

Reusing an `external_id` conflicts with the incumbent; `on_conflict` says what
the relaunch MEANS:

| Intent | Use | What it does |
|---|---|---|
| continue past a crash | `"auto"` (default) | resumes a dead incumbent in place: same run, same curve, steps at or below the resume point refused |
| restart from an EARLIER checkpoint, tail discarded | `on_conflict=probe.Rewind(step=400_000)`, CLI `--rewind-to-step 400000` | hides the record from that step onward (inclusive, W&B-style) in the reopen transaction, so the relaunch REWRITES it; an undoable edit until the relaunch writes over those steps. The only policy that reopens a `completed` run |
| keep BOTH curves | `client.fork_run(source, step=400_000)`, CLI `probe run fork <run> --step 400000` | a NEW run from that checkpoint under its own identity; the source is untouched |
| repeat from scratch | `"supersede"` | a fresh `external_id-rN` retry row; the dead incumbent is tagged `superseded` |

Nothing is hidden without `Rewind`, and `Rewind` needs a server that declares
it - the SDK preflights `GET /v1/server/features` and refuses loudly on an older
deployment. Never work around that by deleting and re-uploading the run;
upgrade, or fork.

## TAKE COMMANDS WHILE IT RUNS:

Register what the script can be asked; `probe run send RUN <kind>` asks it, and the
handler runs at the next `run.log()` (a safe point on the training thread):

```python
from enum import StrEnum
from probe import CommandKind

run.on(CommandKind.CHECKPOINT, lambda: save_checkpoint())        # return the artifact id
run.on(CommandKind.CONFIG, lambda values: set_lr(values["lr"]))  # then recorded on run.config
run.on(CommandKind.STOP, lambda: trainer.request_stop())         # graceful; `stop --force` signals the process

class TrainerCommand(StrEnum):
    EVAL_NOW = "eval_now"

run.on(TrainerCommand.EVAL_NOW, lambda payload: evaluate(**payload))  # `probe run send RUN eval_now --json '{...}'`
```

A kind is an enum member, never a string. An ask the script registered nothing for is
refused at once, with why; `probe run commands RUN` lists what was asked and answered.
