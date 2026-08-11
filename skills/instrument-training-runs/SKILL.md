---
name: instrument-training-runs
description: Wire tracking into a training or evaluation script so what it records actually lands — where the capture code must live in a distributed job, which run it writes to, and whether a thing is a metric or a span. Use when writing or modifying a script that trains, evaluates, sweeps or serves and should be tracked; when choosing between run.log, spans, artifacts and labeled points; when adding Probe to a trainer that already has its own integration (miles, trl, verl, Ray); and when a run finished green but recorded nothing, recorded less than expected, or recorded onto the wrong run. Trigger before the first paid GPU hour, not after — every failure here is silent, and a run that captures nothing looks exactly like a run that captures fine until someone reads it back.
---

# Instrument training runs

`start-research-work` decides WHEN to open a project, experiment and run.
`track-research-work` decides WHAT to record once one is open. This skill is
the part between them: WHERE the capture code has to live so the recording
survives the process boundaries a real training job is made of.

Everything here fails silently. Nothing raises. A misinstrumented run
completes, exits 0, prints correct-looking metrics to stdout, and stores
nothing — and it is indistinguishable from a healthy run until someone opens
the dashboard days later.

## The one rule

**A process that emits telemetry must itself have been configured to emit
it.** Configuring a parent proves nothing about a child.

That sounds obvious and is violated constantly, because the configuration
looks global and is not. Registering a backend, setting a flag, or building a
client mutates ONE interpreter's memory. Every fan-out is a fresh place the
configuration has to arrive:

```
launcher process          config lives here
  └── trainer subprocess         ...but not here
        └── Ray driver                 ...nor here
              └── Ray actor  <-- logs the metrics, knows nothing
                    └── DataLoader worker
```

Symptoms, all of which have happened:

* A backend registered in the launcher, training run in a subprocess: the
  trainer consults an untouched registry. Zero metrics, no error.
* A backend registered in the Ray DRIVER: actors do the logging and each has
  its own copy of the registry. Zero metrics, no error.
* A flag set on `args` in the driver: `args` pickles into actors so the flag
  ARRIVES, while the registry entry does not — so every check passes and
  nothing initialises.

Fixes, in order of preference:

1. Configure inside the emitting process — an actor's own `__init__`, or
   Ray's `worker_process_setup_hook` in `runtime_env`, which runs once per
   worker before any actor code.
2. Pass configuration through the mechanism that actually crosses the
   boundary (`runtime_env["env_vars"]`, not the shell environment — Ray
   workers do NOT inherit the submitter's env).
3. Assert it landed where it is read, not where you set it. Check the object
   the consumer consults, not the one you mutated.

## Which run are you writing to?

Do not assume you are writing to the run you created.

A trainer integration may mint its OWN run from its own config and ignore an
externally supplied run id. When that happens the telemetry is perfect and
lands somewhere you are not looking — the run you created sits empty, which
reads as data loss and is not.

Before concluding anything was lost:

* Ask the WRITER which run it chose. A durable queue writes an `intent.json`
  naming its `run_id`; read that rather than inferring.
* Check the project too, not just the run. A default project name in an
  integration's run spec will quietly collect runs that belong elsewhere.
* An empty run proves where you looked, not what happened.

If the integration builds its own run spec, steer it with the knobs it reads
(project, experiment, run name, external id) rather than creating a competing
run — two runs for one job is worse than one run in the wrong place.

## Metric or span?

Decide the SHAPE before writing capture code. They are not interchangeable
and the wrong one is work for nothing.

| Your work looks like | Use | Because |
|---|---|---|
| a flat loop repeating the same phases (step 12 is not inside step 11) | metrics, one series per phase | `perf/step_time` per step already answers "where did the time go" |
| nested work of varying structure (a trial containing agent turns containing a verifier) | spans | you want to ask what happened INSIDE what, and a tree cannot be expressed as scalars |
| one value per step | `run.log`, `step=` | plots as a curve |
| a value per sample/example | LABELS, never dimensions | per-sample ids are high-cardinality; as dimensions they shatter one series into hundreds of one-point tiles |

A trainer loop is usually metric-shaped, and an agent/sandbox harness is
usually span-shaped. `spans: 0` on a training run is often correct rather
than a gap — check whether the phases are already logged as timing metrics
before building anything.

Spans have no heartbeat and no reaper. A span opened before the work must be
closed on every path including the ones that raise, or it stays `running`
forever. Recording a span AFTER the work, with real start and end
timestamps, cannot leak one.

## Do not duplicate what the integration already does

Before adding capture to a script that uses a trainer with a Probe
integration, read what that integration already emits. Trainers commonly log
their own phase timings, throughput, loss curves and config. Re-emitting them
costs latency in the hot path and creates two sources of truth that will
disagree.

Add capture for what the integration CANNOT know: your reward function, your
scoring sandbox, your dataset transform, your domain metrics.

## Do not block the training loop

Capture code runs inside the thing being measured.

* A synchronous HTTP call inside a coroutine freezes the event loop. In an
  async reward that holds a concurrency slot, this serialises every other
  sample and stalls the timers `asyncio.wait_for` depends on, so timeouts
  silently overshoot. Use `asyncio.to_thread`, or a queue.
* Prefer the durable on-disk queue the SDK provides over per-event network
  calls. It batches, survives a crash, and keeps the loop free.
* Cache handles per process, but cache only SUCCESS. Caching a failure
  permanently disables capture for the life of the worker, and the first
  attempt often races the run's creation — retry rather than give up.

## Where the queue lives matters

A durable queue writes to disk, and an exporter uploads from there. Two
things follow:

* Its directory must be SHARED by every process that captures, and must not
  default to something per-process. A location derived from `cwd` gives each
  actor its own queue that no single exporter drains.
* It must outlive the box. Under Ray, `cwd` is a per-job directory Ray
  DELETES on completion, taking undrained points with it. Point it somewhere
  durable and drain it explicitly at the end.

## Verify at the destination

The exit code tells you the process ended. It says nothing about what was
stored.

Before reporting a run as captured, read it BACK from the server:

```python
run = client.get_run(run_id)
assert run["counts"]["metrics"] > 0
```

and check the SHAPE, not just the count:

```python
series = client.run_series(run_id)
assert len(series) < 50, f"{len(series)} series — a wall of tiles, not graphs"
```

If the series count is close to the point count, every series holds one
point: something high-cardinality became a dimension. Fix the shape before
spending more compute.

The same rule applies to artifacts. A reference artifact pointing at a path
on a machine you are about to destroy resolves nowhere afterwards. Upload
bytes, or reference storage the team can actually reach, and confirm the
object exists after teardown.

## Report capture honestly

Print what happened to the telemetry, not just to the training. A line that
states which of several outcomes occurred — captured and drained, captured
but unpublished, nothing written — turns a silent loss into a visible one,
and is often the only reason a problem is caught while the machine is still
alive to fix it.
