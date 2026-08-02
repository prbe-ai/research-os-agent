# Changelog

## Unreleased

### Changed

- `search_knowledge`'s `search_in` and `collapse` are now typed as enums, so
  their vocabularies ship in the tool's JSON Schema (`$defs.ToolCorpus`,
  `$defs.CollapseMode`) instead of existing only as prose in the description.

  Callers get client-side validation and a rejection that names every accepted
  value: `Input should be 'files', 'documents', 'transcripts' or 'experiments'`.
  Previously a typo round-tripped to the server and came back as
  `unsupported_values`, which named the bad value but never the valid set.

  **Behaviour change:** one bad entry now rejects the whole list.
  `search_in=["documents", "bogus"]` used to search `documents` and flag
  `bogus`; it now fails. The rows that call used to return were for the value
  the caller already got right, and the error hands a caller the correct
  vocabulary for an immediate retry.

  `ResearchReadService` still takes plain strings and keeps its graceful
  unsupported-value handling — it is callable directly from Python, where
  nothing validates on its behalf.

## Unreleased

### Breaking

- `search_knowledge`'s `corpora` parameter is now **`search_in`**. Passing
  `corpora` raises; it is not honoured and not aliased.

  The old name read as the plural of the backend's `corpus` field
  (`POST /v1/search`), and it is not. Before this release, two of the five
  values mapped identity (`transcripts`, `experiments`) and three did not
  (`documents` fanned out to github + files; `assets` and `procedures` both
  collapsed to files). Whichever identity value you tried first confirmed the
  misreading. See the next entry for the value list as it stands now.

  `corpora` stays bound in the tool signature, marked `deprecated`, **purely to
  reject**. Deleting it would have been silent: FastMCP builds its argument
  model without `extra="forbid"`, so pydantic discards unknown keys — a stale
  caller would have received an unfiltered search wearing a success envelope,
  which is the failure this tool already refuses elsewhere.

  Response fields rename with it: `unsupported_corpora` -> `unsupported_values`,
  and the `kb_corpora` completeness marker -> `kb_values`.

- The `assets` and `procedures` values collapse into **`files`**. Both mapped to
  the same backend corpus, so the tool was advertising a distinction the index
  cannot make (`IndexDocType` has one bucket, `workspace.file`). Narrowing to
  `assets` never excluded a procedure, and vice versa.

### Added

- `make regen-mcp-schema` re-captures the MCP tool-schema baseline. It pins
  `PYTHONPATH` and refuses to run against a source tree other than the one you
  are in, because a bare `import probe.mcp.server` from a worktree resolves to
  the *installed* package and would snapshot the wrong schema while the pin
  test stayed green.

## 0.28.0

### Added

- Run titles and descriptions can now be edited with
  `probe run set RUN --name ... --description ...`, matching the existing
  project and experiment editing commands.
- `probe run start` and `probe run child` accept `--description`, and the
  Python SDK exposes run descriptions on creation, reads, and
  `Client.update_run()`.

## 0.27.1

### Fixed

- `probe wizard` no longer dies with `KeyError: Capability.AUTO_UPDATE` right
  after you answer the auto-update question. `plan()` read every capability's
  label out of `MENU_COPY`, which holds only the two checkbox rows — auto-update
  is asked as its own step and its copy lives in `AUTO_UPDATE_COPY`. It was the
  worst possible split: auto-update defaults ON and starts OFF, so the plan
  always changed it, so *every fresh install crashed* — after the consent menu
  and before anything was installed. `probe wizard --yes` on a fresh machine
  (CI, scripted setup) crashed the same way, since `plan()` runs on the flag
  path too. Labels now come from `PLAN_LABELS`, which is total over
  `Capability` and asserted to stay that way. Broken since the auto-update step
  was split out of the picker (#73), shipped in 0.26.0 through 0.27.0.

## 0.27.0 (unreleased)

### Breaking

- `check_run` / `probe run check` no longer answer `complete` on the cheap path.
  It counted rows — is there an `env_ref`, is there a `code_snapshot` artifact —
  and never asked whether either led anywhere, so seventeen runs whose code was
  already unrecoverable read as captured for a week. Three verdicts now:
  `incomplete` (something absent or provably unrecoverable), `unverified` (the
  default: nothing obviously absent, which is NOT "can be rebuilt"), and
  `complete`, earned only under `verify=True` / `--verify` by resolving the
  recorded commit against its remote. Callers testing `state == "complete"` must
  either pass `verify` or accept `unverified`. CLI exit 2 now means `incomplete`
  specifically, so an unverified run no longer fails a script.

### Added

- `check_run(verify=True)` and `probe run check --verify` resolve the captured
  code reference by depth-1 fetching the recorded commit from the recorded
  remote — the same thing a reproduction does. `snapshot.commit_on_remote()` is
  memoized on `(remote, commit)` and bounded by a 20s timeout, so auditing a
  project costs one fetch per distinct base commit rather than one per run
  (measured: 201 runs sharing a base = 1 network call, 2.6s; the other 200
  resolve from cache in 0.01ms total). Never called during a run, so it cannot
  affect training or upload throughput.
- `check_run` reports `pending_code_bytes` when the manifest records files whose
  bytes were never stored. Free: the summary already arrives on the artifact's
  meta, so it costs a dict lookup and no network. This is the failure mode
  per-file capture introduced in 0.26.3, and leaving it unchecked would have
  repeated the original mistake in a new place.

- Miles' existing `probe.connectors.miles.per_sample_rollout_log` hook now
  captures arbitrary numeric entries from `sample.metadata["probe_metrics"]`
  and inline `args.probe_sample_metrics` metric-name to dotted-path mappings.
  Stock launchers that cannot carry custom args can define the same mapping with
  `make_per_sample_rollout_log(...)` in an importable hook module.
  These values use the same durable metric queue and database representation as
  aggregate `tracking.log()` points, with `metric_scope=sample`, sample/group
  labels, and the existing Harbor rollout-span anchor distinguishing them.
  Missing and non-numeric values are omitted instead of becoming false zeros;
  explicit numeric zero remains a measurement. Runs reserve 1,024 configurable
  sample points per sample by default, adjustable through
  `args.probe_sample_metric_budget`.

## 0.26.4 (unreleased)

### Fixed

- `get_entity(view="reproduce")` no longer fails on token budget. The view is
  atomic (never truncated), so the per-file code manifest inside it made the whole
  call error on any real repo — 224 files was 79,809 characters, 94% of it manifest
  rows. It now carries the manifest SUMMARY plus `entries_omitted`; the rows stay
  available at `/v1/execution-records/{env_ref}`. Same run: 3,713 characters.

### Added

- `probe snapshot-show <run>` prints a run's captured code manifest, one file per
  line, with `--pending-only` for the files whose bytes are not yet stored.
  `probe snapshot` now also reports the referenced / pending-upload counts.
- `capture_manifest` and `pushed_base` are exported from `probe.snapshot`.

## 0.26.3

### Fixed

- Code capture no longer stakes reproducibility on a commit that may exist only
  on the machine that ran the job. `snapshot.capture_manifest()` classifies each
  file per-FILE as retrievable from a *pushed* remote (`source="git"`) or needing
  its bytes uploaded (`source="blob"`), proving reachability with `git ls-remote`
  rather than assuming it. `Run.snapshot()` publishes the manifest and its
  `tree_sha256` on the execution record and the code-snapshot artifact meta.
  Classification only: `n_pending_upload` counts outstanding work, and callers
  still move the bytes.
- `snapshot.capture_env()` records the resolved package LIST instead of only a
  digest and a count, reads it via `importlib.metadata` (a `uv venv` ships no
  `pip`, so the previous `pip freeze` subprocess captured nothing at all), and
  raises instead of silently returning `{"python": ...}`. Strictness follows the
  client's `fail_open` setting unless `snapshot(strict=...)` overrides it.
  **Breaking for digest consumers:** `packages_sha256` still exists but is now
  computed over sorted `name==version` lines, so its value differs for an
  unchanged environment. Do not compare across this boundary.
- Remote URLs are credential-scrubbed before being recorded. A CI remote such as
  `https://x-access-token:<TOKEN>@github.com/...` previously copied a live token
  into run metadata and artifact meta.
- `ls-remote` runs with a 10s timeout and `GIT_TERMINAL_PROMPT=0`, so an
  unreachable or credential-prompting remote can no longer hang the start of a run.

### Added

- `Run.reconcile_artifact(name, content_hash)` finds an artifact a lost response
  hid, so a retry reuses it instead of creating a duplicate. Opt-in:
  `log_artifact` does not call it yet.

- Expanded Harbor trajectories now stamp every turn, tool call, nested span,
  and truncation marker with a zero-based `attributes.trajectory_index`.
  Consumers can restore parser execution order without relying on optional
  timestamps; system and user setup turns also stop inheriting model metadata
  that ATIF did not record on those steps.
- SDK-owned Harbor captures now request recognized trajectory expansion from
  the durable watcher by default, removing the manual `probe trial expand`
  step for future captures while retaining the raw trajectory artifact.

## 0.26.2 (unreleased)

### Fixed

- Miles per-sample reward and response-length points now carry the same
  deterministic rollout `span_id` as their correlated Harbor capture whenever
  the agent response includes the capture `external_key`. This makes the
  dashboard's sample → trial → trajectory/sandbox join exact without requiring
  Miles-core changes. The optional anchor survives durable queue replay, while
  older and non-Harbor records continue draining unanchored.

## 0.26.1 (unreleased)

### Changed

- `search_knowledge` no longer discards knowledge hits. `collapse="experiment"` (the
  DEFAULT) used to drop every result row that was not an experiment or run, so every
  document, transcript, file and artifact hit the backend returned was filtered out
  before the caller saw it — the ingested Claude Code session corpus was unreachable
  through the tool entirely. Collapse now dedupes experiments and runs and passes
  everything else through in the merged ranking order. Callers on the default will
  start seeing rows with `entity_type` `document` / `file` / `project` / `artifact`;
  those rows are terminal (no `resource` to hand to `get_entity`).
- `search_knowledge` `corpora` now narrows the semantic channel to exactly the corpora
  named, instead of always unioning `experiments` in. The union made narrowing useless
  in practice: the per-channel budget is ~`top_k/2` and experiment projections outrank
  the knowledge corpora, so `corpora=["transcripts"]` came back holding only
  experiments. To restore the old behavior, name it: `["experiments", "transcripts"]`.
  A narrowing where every named corpus is unrecognized still falls back to
  experiments-only and reports `kb_corpora` in `completeness.missing`. The exact
  channel is structured-entity search and remains un-filtered by corpus.

### Fixed

- Miles now reserves three labeled metric points per planned rollout sample:
  the per-sample reward and response length plus the correlated Harbor verifier
  reward. This prevents the durable exporter from exhausting a run's
  create-time labeled-point budget during normal per-sample capture.

## 0.26.0 (unreleased)

### Added

- `HarborCaptureResult.begin_bytes_captured` (and `SandboxStateRecorder.begin_bytes_captured()`
  + a `begin_bytes_captured` field in the recorder summary): whether the trial
  archived and verified begin-state bytes. Lets a bridge's per-task election read
  capture status straight off the `finalize` result instead of re-parsing the
  authored `meta.json` from disk.

## 0.25.0 (unreleased)

### Changed

- Experiment creation and passive ingest now require an explicit project.
  The CLI can use `--project`, an active project selection, or an exact project
  identifier; SDK and ingest callers must send the project coordinate.

### Removed

- The agent no longer creates or relies on a synthetic `Default` project, and
  default-named projects can be archived like any other project.
- The unused automatic-hypothesis helpers and placeholder experiment behavior
  have been deleted.

## 0.24.0 (unreleased)

### Breaking

**Root `--token`, `--ingest-token`, and `--hmac-secret` flags are removed.**
A secret in argv leaks into shell history and `ps`, and the new background
outbox drainer could never resolve a credential that lived only in one
process's flags. Migrate to the environment variables the SDK already honors
(`PROBE_TOKEN`, `PROBE_INGEST_TOKEN`, `PROBE_HMAC_SECRET`) or a named context
via `probe login`. `probe login --token` (which STORES the credential) is
unchanged; `--base-url` and `--spool-dir` remain.

**The JSONL spool is replaced by the outbox journal.** Fail-open writes now
land in `~/.local/state/probe/outbox` (override: `PROBE_OUTBOX_DIR` or
`--spool-dir`) as one versioned operation journal (`probe.outbox/1`) with
per-op identity, run tags, context pins, and a content-addressed blob store.
A surviving legacy spool is imported automatically, in order, on first use.
`Client(spool=...)` is gone; pass `journal=` or `spool_dir=`.

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

**`--async` / `PROBE_ASYNC=1`: non-blocking writes.** `probe log`, `span add`,
`note add`, `artifact add`, and `run end` queue to the local outbox and return
immediately; a wake-on-enqueue detached drainer delivers with retries and
capped backoff until the queue is empty, then exits. Small files fingerprint
and register upload intent inline (a ~2s-capped presign ping creates the
server's pending row); large files snapshot instantly (filesystem clone where
supported) and hash in the drainer. Failure policy: permanent rejections
dead-letter and the queue keeps flowing; transient failures wait and retry;
401/403 halts delivery with items untouched.

Delivery is **at-least-once**: a crash between the server committing a write
and the journal deleting the op replays it (ops carry an `op_id`; the drain
fsyncs deletions to keep the window minimal, and 409-with-existing_id on a
retry is treated as our own earlier delivery). Scope run refs consistently —
the run-end barrier matches the literal ref you enqueued with (id vs slug).

**`probe outbox status|drain|watch|retry|pause|resume`** — one surface over
the whole queue; `probe flush` is now an alias of `outbox drain`. Every
command prints a one-line stderr banner when the outbox holds dead letters or
is auth-blocked, and `probe doctor` gained an Outbox section. `probe run end`
is a run-scoped barrier: it delivers that run's queued items first and exits
non-zero (without closing the run) while any cannot be delivered.

### Changed

- The begin phase now downloads and sha256-verifies every file the trailer
  names (previously just the manifest), so the begin archive inherits the
  manifests' tamper-evidence.

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
