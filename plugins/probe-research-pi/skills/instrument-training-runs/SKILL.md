---
name: instrument-training-runs
description: Wire tracking into a training or evaluation script so what it records actually lands — where the capture code must live in a distributed job, which run it writes to, and whether a thing is a metric or a span. Use when writing or modifying a script that trains, evaluates, sweeps or serves and should be tracked; when choosing between run.log, spans, artifacts and labeled points; when adding Probe to a trainer that already has its own integration (miles, trl, verl, Ray); and when a run finished green but recorded nothing, recorded less than expected, or recorded onto the wrong run. Trigger before the first paid GPU hour, not after — every failure here is silent, and a run that captures nothing looks exactly like a run that captures fine until someone reads it back.
---

# Instrument training runs

`track-work` decides WHEN to open a project, experiment and run, and WHAT to
record into them. This skill is WHERE the capture
code must live so the recording survives the process boundaries a real
training job is made of.

Nothing here raises. A misinstrumented run exits 0, prints correct-looking
metrics to stdout, and stores nothing.

## The one rule

**A process that emits telemetry must itself have been configured to emit
it.** Configuring a parent proves nothing about a child.

Registering a backend or setting a flag mutates ONE interpreter's memory.
Every fan-out is a fresh place the configuration has to arrive:

```
launcher process          config lives here
  └── trainer subprocess         ...but not here
        └── Ray driver                 ...nor here
              └── Ray actor  <-- logs the metrics, knows nothing
```

All of these have happened, none raised:

* Backend registered in the launcher, training in a subprocess.
* Backend registered in the Ray DRIVER; the ACTORS log, each with its own copy
  of the registry.
* A flag set on `args` in the driver: `args` pickles into the actors so the
  flag ARRIVES while the registry entry does not — every check passes and
  nothing initialises.

Fixes, in order:

1. Configure inside the emitting process — the actor's `__init__`, or Ray's
   `worker_process_setup_hook`, which runs once per worker before actor code.
2. Pass config through what actually crosses the boundary:
   `runtime_env["env_vars"]`, since Ray workers do NOT inherit the submitter's
   environment.
3. Assert it landed where it is READ, not where you set it.

## Which run are you writing to?

A trainer integration may mint its OWN run from its own config and ignore the
run id you supply. The telemetry is then perfect and lands somewhere you are
not looking, which reads as data loss and is not.

* Ask the WRITER: a durable queue writes `intent.json` naming its `run_id`.
* Check the project too — a default project name in a run spec quietly
  collects runs that belong elsewhere.
* An empty run proves where you looked, not what happened.

Steer the integration with the knobs it reads (project, experiment, run name,
external id). Two runs for one job is worse than one run in the wrong place.

## Metric or span?

| Your work | Use | Because |
|---|---|---|
| a flat loop repeating phases (step 12 is not inside step 11) | metrics, one series per phase | `perf/step_time` already answers "where did the time go" |
| nested work of varying structure (trial → agent turns → verifier) | spans | a tree cannot be expressed as scalars |
| one value per step | `run.log(..., step=i)` | plots as a curve |
| a value per sample | LABELS, never dimensions | per-sample ids shatter one series into hundreds of one-point tiles |

A trainer loop is metric-shaped; an agent or sandbox harness is span-shaped.
`spans: 0` on a training run is usually correct — check whether the phases are
already timing metrics before building anything.

Spans have no heartbeat and no reaper, so one opened before the work leaks on
every path that raises. Record it AFTER, with real start and end timestamps.

A `rollout` span is special: each one materializes an addressable TRIAL beside
it, keyed by that span's id, with its own page a teammate can open and annotate.
So `span_type="rollout"` is the type to reach for when a nested unit is one
attempt at a task — nothing else you emit becomes an entity of its own.

**Pass a `name=` and `attributes` that mean something.** The span name you write
here is the trial's name until something better exists — a harness that passes
its own join key gives you a page titled
`HIPS__autograd.ac044f0d.lm_rewrite__probe-1cd50cf5`. A generated title does
fill the sidecar in later, but only once the run reaches a terminal status, and
it is written FROM what you logged: `attributes` is the field that says what the
rollout attempted. Sparse attributes give a vague title. Neither costs more than
one argument at the call site, and together they are the difference between a
readable trial list and 500 opaque rows.

## Don't duplicate, don't block

Read what the trainer's integration already emits — phase timings, throughput,
loss, config. Re-emitting costs hot-path latency and creates two sources of
truth that will disagree. Capture what it CANNOT know: your reward function,
scoring sandbox, dataset transform.

* A synchronous HTTP call inside a coroutine freezes the event loop. In an
  async reward holding a concurrency slot it serialises every other sample and
  stalls the timers `asyncio.wait_for` depends on, so timeouts overshoot
  silently. Use `asyncio.to_thread`, or a queue.
* Prefer the SDK's durable on-disk queue to per-event network calls.
* Cache handles per process, but cache only SUCCESS — the first attempt often
  races the run's creation, and caching that miss disables capture for the
  life of the worker.

## Where the queue lives

* SHARED by every capturing process. A path derived from `cwd` gives each
  actor its own queue that no single exporter drains.
* Outliving the box. Under Ray, `cwd` is a per-job directory Ray DELETES on
  completion, taking undrained points with it. Drain explicitly at the end.

## Verify at the destination

The exit code says the process ended, not what was stored.

```python
run = client.get_run(run_id)
assert run["counts"]["metrics"] > 0
assert len(client.run_series(run_id)) < 50   # else: a wall of one-point tiles
```

If the series count approaches the point count, something high-cardinality
became a dimension. Fix the shape before spending more compute.

Same for artifacts: a reference pointing at a path on a machine you are about
to destroy resolves nowhere afterwards. Code never has this problem — a code
artifact is always uploaded, and `--reference` on one is refused rather than
recorded — but a `--reference` checkpoint or dataset on an ephemeral box does.

Print which outcome occurred — drained, written but unpublished, nothing
written. It is often the only reason a loss is caught while the machine is
still alive to fix it.
