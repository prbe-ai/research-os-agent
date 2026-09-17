---
name: instrument-code
description: Wires the Probe Logging SDK (`probe.init`, `probe.log`) into a script that trains, evaluates, sweeps or serves, so that it records run-level metrics. Use when writing or changing any such script, launching a training script on another machine, when a trainer already has its own integration (miles, trl, verl, Ray), and when a run finished green but recorded nothing, less than expected, or onto the wrong run. HARD TRIGGER - ENSURE YOU READ BEFORE LAUNCHING ANY PAID GPU HOURS.
---
# Instrument code

Example:

```python
import probe
probe.init(project="folding", experiment="dockq-sweep", question="temp 0.7 wins")
probe.log({"loss": 0.42}, step=42)   # ambient: no handle to thread through call frames
probe.finish()
```

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

## WHAT MUST REACH THE JOB:

Three things, none of which travel on their own - the PACKAGE, the run id +
epoch, and a TOKEN. `probe exec` exports `PROBE_RUN_ID` and `PROBE_RUN_EPOCH`
into its child and `probe.init()` attaches, but that export dies at the machine
boundary:

| Launcher | id + epoch | the package |
|---|---|---|
| Modal | `secrets=[modal.Secret.from_dict({"PROBE_RUN_ID": ..., "PROBE_RUN_EPOCH": ..., "PROBE_TOKEN": os.environ["PROBE_TOKEN"]})]` (local env is not forwarded; never a literal - the snapshot uploads this script) | `modal.Image.debian_slim().pip_install("probe-research")` |
| Ray | `runtime_env={"env_vars": {...}}` | `runtime_env={"pip": ["probe-research"]}` |
| Slurm | `sbatch --export=ALL,PROBE_RUN_ID=...,PROBE_RUN_EPOCH=...` | the venv the job activates |
| Docker / k8s | `-e PROBE_RUN_ID=...` / `env:` on the pod | the image |

When those two variables are set, call `probe.init()` with no arguments. Naming
a project or experiment there contradicts the run the launcher opened, and it
raises. If the launcher only submitted the job, `probe exec` opened the run
AWAITING ATTACH and this same call takes ownership.

The credential is `PROBE_TOKEN`, plus `PROBE_BASE_URL` if you are not on the
default server. There is NO `PROBE_API_KEY` - nothing reads it, though `probe
exec` prints it in the Modal hint.

A run id names a row, not an attempt: without the EPOCH a stale process holding
an old id attaches to a row a newer attempt reopened, and inherits its write
authority. Several ranks on one id is fine - the first reopens, the rest join.

## WHICH RUN ARE YOU WRITING TO:

A trainer integration (miles, trl, verl, Ray, etc) may mint its OWN run and
ignore the id you supply. Steer it with the knobs it reads (project, experiment,
run name, external id), then check where it actually wrote:

- the WRITER knows - a durable queue writes `intent.json` naming its `run_id`
- a default project name in a run spec quietly collects runs that belong
  elsewhere
- an empty run proves where you looked, not what happened

## METRIC OR SPAN:

| Your work | Use |
|---|---|
| a flat loop repeating phases (step 12 is not inside step 11) | metrics, one series per phase |
| nested work of varying structure (trial -> agent turns -> verifier) | spans |
| one value per step | `probe.log({...}, step=i)` |
| a value per sample | LABELS, never dimensions - per-sample ids shatter one series into hundreds of one-point tiles. Labeled points NEVER plot, so the curve needs a second, unlabeled key |

`spans: 0` on a training run is usually correct - check the phases are not
already timing metrics.

Spans have no heartbeat and no reaper, so one opened before the work leaks on
every path that raises. Record it AFTER, with real start and end timestamps.

`span_type="rollout"` is the only thing you emit that becomes an entity: each
materializes an addressable TRIAL keyed by that span's id. Pass a real `name=`
and `attributes`: the name is the trial's name until a generated title lands,
and that happens only once the run is terminal, written FROM `attributes`.
Sparse attributes give 500 opaque rows.

## HARBOR TRIALS - THE FILE DOOR:

Harbor writes each trial to a directory (`result.json`, `agent/trajectory.json`,
verifier logs). Probe captures that directory into the run, keyed to the
training step. Rollout spans above are the other door; this one needs no code in
the agent.

```
probe trial add $RUN <trial-dir> --step 600               # rollout span + reward metric + files + manifest
probe trial add $RUN <trial-dir> --step 601 --no-expand   # raw only
```

A recognised trajectory format (ATIF v1.x) expands into turn/tool_call spans. An
unknown one is stored raw, never rejected; `probe trial expand $RUN
<manifest-id>` expands it later, idempotent.

With Miles, the trainer writes one export descriptor per trial and nothing
uploads until something consumes it:

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
- Prefer the SDK's durable on-disk queue to per-event network calls
- Cache handles per process, but cache only SUCCESS - the first attempt often
  races the run's creation, and a cached miss disables capture for the life of
  the worker

## WHERE THE QUEUE LIVES:

ONE queue, SHARED by every capturing process - a path derived from `cwd` gives
each actor its own queue that no exporter drains. It must outlive the box: Ray
DELETES its per-job `cwd` on completion, undrained points with it. Drain
explicitly at the end.

## VERIFY AT THE DESTINATION:

The exit code says the process ended, not what was stored.

```python
run = client.get_run(run_id)
assert run["counts"]["metrics"] > 0
assert len(client.run_series(run_id)) < 50   # else: a wall of one-point tiles
```

Series count near point count means something high-cardinality became a
dimension - fix the shape before spending more compute. Print which outcome
occurred: drained, written but unpublished, nothing written.

A `--reference` checkpoint or dataset on a box you are about to destroy resolves
nowhere afterwards. Code is exempt - always uploaded, and `--reference` on it is
refused.

## RELAUNCHING A RUN:

Reusing an `external_id` conflicts with the incumbent; `on_conflict` says what
the relaunch MEANS:

| Intent | Use | What it does |
|---|---|---|
| continue past a crash | `"auto"` (default) | resumes a dead incumbent in place: same run, same curve, steps at or below the resume point refused |
| restart from an EARLIER checkpoint, tail discarded | `on_conflict=probe.Rewind(step=400_000)`, CLI `--rewind-to-step 400000` | deletes the record from that step onward (inclusive, W&B-style) in the reopen transaction, so the relaunch REWRITES it. Destructive on purpose, and the only policy that reopens a `completed` run |
| keep BOTH curves | `client.fork_run(source, step=400_000)`, CLI `probe run fork <run> --step 400000` | a NEW run from that checkpoint under its own identity; the source is untouched |
| repeat from scratch | `"supersede"` | a fresh `external_id-rN` retry row; the dead incumbent is tagged `superseded` |

Nothing is deleted without `Rewind`, and `Rewind` needs a server that declares
it - the SDK preflights `GET /v1/server/features` and refuses loudly on an older
deployment. Never work around that by deleting and re-uploading the run;
upgrade, or fork.
