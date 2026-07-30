# Changelog

## 0.24.0 (unreleased)

### Added

- **Begin-state bytes** (`probe.sandbox-state/1`): the snapshot tool's `begin`
  subcommand gains `--bytes`, teeing the manifest walk into a streamed
  `begin-bytes.tar.gz` — the byte-level "before" state of the sandbox that the
  bundle previously only described as metadata. Modified files get true
  before/after diffs; deleted files' contents become recoverable. Guarded by
  `--max-begin-bytes` (default 32 GiB, further capped at 50% of free space)
  with the same drop accounting and PSBX1 trailer integrity as the end delta.
- `SandboxStateOptions` grows `root` (plumbs the binary's existing scan-root
  flag), `begin_bytes`, `begin_bytes_ref`, and `max_begin_bytes`. The sharing
  model is per-task: the caller's ledger elects one trial per task
  (`task_checksum`) to capture; every trial of the task stamps
  `meta.json.begin_bytes = {captured, ref, budget_bytes, truncated,
  dropped_count}` so renderers can resolve the shared archive and verify
  per-file validity against the begin manifest's sha256s (design:
  `docs/2026-07-29-begin-state-bytes.md`).
- `begin_timeout_sec` now defaults to `None`, resolving to 120 s (600 s when
  `begin_bytes` is on); explicit values are honored unchanged.

### Changed

- The begin phase now downloads and sha256-verifies every file the trailer
  names (previously just the manifest), so the begin archive inherits the
  manifests' tamper-evidence.
- Experiment creation and passive ingest now require an explicit project.
  The CLI can use `--project`, an active project selection, or an exact project
  identifier; SDK and ingest callers must send the project coordinate.

### Removed

- The agent no longer creates or relies on a synthetic `Default` project, and
  default-named projects can be archived like any other project.
- The unused automatic-hypothesis helpers and placeholder experiment behavior
  have been deleted.

## 0.23.0 (unreleased)

### Added

- **`probe.connectors.harbor_capture`** — the SDK-owned capture facade for
  Harbor bridges. Any bridge/server that owns a harbor `Trial` gets Probe
  capture in ~3 lines:

  ```python
  from probe.connectors import harbor_capture

  handle = harbor_capture.attach(trial, correlation={...}, context={...},
                                 capture_mode="shadow",
                                 sandbox_state=SandboxStateOptions())
  try:
      result = await trial.run()
  finally:
      capture = await handle.finalize(trial_dir)
  ```

  `attach()` installs the correlation hooks (logical `session_id` plus a
  best-effort provider sandbox id read from stable string identifiers on the
  per-backend private handles — Daytona/E2B `_sandbox`, Modal
  `_sandbox.object_id`, Runloop `_devbox.id` — retained so they survive
  Harbor nulling the environment handle) and, when `sandbox_state=` options
  are given, the existing `probe.sandbox-state/1` recorder from
  `harbor_runner`. `finalize()` stages the trial tree through
  `stage_trial_export` and returns a `HarborCaptureResult` carrying the
  staged paths, archive hash, external key, sandbox ids, and the
  sandbox-state summary (also folded into the export's
  `context.sandbox_state`).

  Capture modes: `off` (no-op handle, harbor never imported), `shadow`
  (best-effort — staging failures come back as `status="failed"`, never
  raised), `required` (same staging, but the caller gates on
  `capture.complete` / `capture.raise_if_incomplete()` to fail its
  response). Harbor stays an optional lazy dependency behind
  `verify_harbor_contract()`.

- `SandboxStateRecorder` grew `summary()` (the JSON-safe verdict the facade
  folds into capture context, `"not_attempted"` until a hook fires),
  `attempted()`, and `record_install_failure()` for callers that install the
  hooks fail-open.

### Fixed

- The durable Harbor exporter now maps Miles `sample_id` and `group_id`
  correlation onto Probe `sample` and `group` point labels. Multiple trials at
  the same training step therefore retain distinct reward points and join
  directly to their `harbor_trial` manifests without creating per-sample metric
  series.

## 0.22.0 (unreleased)

### Breaking

**`run.log()` auto-increments `step` when you omit it.** Previously a bare
`run.log({"loss": l})` sent no `step_index` at all and the points landed on the
wall-clock axis. They now land on steps 0, 1, 2, … so the common loop draws a
curve. This silently changes the axis of any existing bare-`log()` call site.

```python
for batch in loader:
    run.log({"loss": loss})        # 0.16.0: no step   0.17.0: steps 0,1,2,…
```

Opt out with an explicit `step=None`, which still means "no step axis":

```python
run.log({"loss": loss}, step=None)   # wall-clock only, as before
```

An explicit `step=i` is unchanged, and now also moves the auto counter past `i`
so mixing the two forms cannot stack a second series on steps already used.
Counters are per metric `kind`, so `log_hw()` never shifts the training curve.

**`run.span()` returns `SpanHandle`, not `str`.** It subclasses `str`, so
comparison, formatting, dict keys and `id=` passthrough are unchanged, and
`copy`/`deepcopy`/`pickle` degrade it to a plain `str`. Only `type(x) is str`
breaks; use `isinstance(x, str)`.

**`client.run()` can now create its parents, but only via `hypothesis=`.** In
0.16.0 it always raised on an unknown slug. Passing `hypothesis=` creates the
experiment (and its project); omitting it is unchanged and creates nothing. A
slug that is a near-miss of an existing one is REFUSED rather than created.

This is SDK-only. **`probe run start` never creates**, on any path — on the CLI
the slug is hand-typed on every invocation, which is where typos come from. Use
`probe project create` / `probe experiment create` there.

### Added

- **Module-level API**: `probe.init()` / `probe.log()` / `probe.log_hw()` /
  `probe.log_artifact()` / `probe.span()` / `probe.finish()` /
  `probe.active_run()`. Logs from anywhere without threading a handle through
  call frames. The binding is a contextvar over a process default, so worker
  threads find the run while a scoped `init()` shadows rather than hijacks. A
  script that exits without `finish()` is closed as `completed` / `failed` /
  `canceled` instead of waiting for the crash reaper.
- **`run.span()` is a context manager**: `with run.span("rollout") as span:`
  stamps both timestamps from one clock, auto-nests children, and closes with a
  terminal status even when the body raises. Spans have no heartbeat and no
  reaper, so one abandoned by an exception previously stayed `running` forever.
- **`client.compare()`**: N runs read back aligned on a shared step axis,
  labelled by petname, with `None` holes rather than truncation to the shortest.
  `.to_pandas()` if pandas is installed; no new dependency.
- **`run.log()` accepts any value type.** Numbers (and bools, numpy scalars, 0-d
  tensors) become metric points; strings, dicts, lists and `None` go to that
  step's record. Previously one non-numeric key raised out of the training loop
  *and* discarded every numeric metric in the same call.

### Fixed

- A non-numeric value in `log()` no longer takes its numeric neighbours with it.
- `log()` no longer reports a spooled metric write as confirmed when the same
  call also wrote a step record.
- `log({})` no longer consumes a step index.
- Span attributes go through the same JSON-safety pass as metrics, so an
  unserialisable value warns instead of raising inside the training loop (and no
  longer displaces the body's own exception on the way out of a `with` block).
- `run.step()` forwards `strict=`; it used to swallow it.
