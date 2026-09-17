# Reference

Lookup only; judgment is in the skill. Sections mirror the skill's numbering.

## INDEX

| skill | reference |
|---|---|
| §1 the map | §1 ENTITY COMMANDS - every create/change/delete verb, per entity |
| §2 project and experiment | §2 QUESTION AND TAGS |
| §3 run | §3 RUN - snapshot inputs, capture calls, delivery (the outbox) |
| §4 files and numbers | §4 FILES AND NUMBERS - artifact commands, big files, names and versions, metric shape, derived metrics |
| §5 papers | §5 PAPERS |
| notes, reading, publishing | §6 NOTES, §7 READING BACK, §8 PUBLISHING |
| worked examples | §9 RECIPES - data processing, provisioning, trials |

## 1. ENTITY COMMANDS

```
probe project create SLUG --kind K [--parent P] [--tag T] | list | get | use | set | move | delete
probe experiment create SLUG --project P --question "..." | set | freeze --label L | delete | edges
probe exec [--project P | --experiment E] [--parent RUN --relation R] -- CMD
probe run fork SRC --step N | end --status S | check | reproduce | tag | set RUN --name
probe group create EXPERIMENT_ID --name NAME [--kind K] [--spec JSON|@FILE] | list | get | set
probe trial list RUN | get TRIAL | set TRIAL --name
probe workspace create SLUG [--name] [--use] | list | get | rename | use | delete ID --yes
probe shared add PATH | list | download | share ARTIFACT_ID [--replace] | unshare ARTIFACT_ID | delete
probe edge add --source run:A --target artifact:B --relation R | remove EDGE_ID
probe project reference add --to PROJECT | remove
probe project code attach OWNER/REPO | detach | list
```

Prefer `--project` or `PROBE_PROJECT` over `project use` (machine-global -
skill §2). `probe experiment set EXP --question "..."` is the only way to
change a question: opening a run with `question=` on an existing experiment
never rewrites it.

## 2. QUESTION AND TAGS

For a teammate, not a log. Slugs and names: ENTITY NAMING in the skill.

| field | shape |
|---|---|
| question | one testable question, ≤30 words: the change and what it might move. Never the outcome you expect |

Descriptions are server-written.

Explain acronyms. Use product or milestone names only when given - never
invent or expand a codename. Checkpoints, paths, commands and parameters go in
config or notes.

**Tags** at creation, 1-3 lowercase-kebab from a short vocabulary: `baseline`,
`ablation`, `sweep`, `debug`, `smoke-test`, `prod-candidate`, `infra`. Retag
when the meaning changes: `probe run tag RUN flaky --remove prod-candidate`.

## 3. RUN

### Snapshot inputs - the decision record

```
probe snapshot RUN --cwd PATH --include 'data/**' --include checkpoints/base.pt
probe snapshot-show RUN                  # what was captured
probe snapshot-restore RUN --verify-only # can it be rebuilt: want "0 unavailable"
```

- A glob matching nothing is an error; a path outside the snapshot root is
  refused; naming a file already in the manifest adds no duplicate.
- Size is handled: `--reference-over-mb`, default 100.
- A non-git directory is captured whole, skipping lockfile-rebuilt trees and
  credential-shaped names.
- Files git cannot supply are stored one artifact row per file
  (`snapshot-show` labels those `captured`), or as the run's `code-bytes`
  archive with `PROBE_CODE_STORAGE=archive`. Restore reads both.

The decision record is an artifact on the run — `probe artifact add RUN
inputs-decision.json --kind inputs_decision`:

```json
{
  "included": [
    {"path": "data/train.jsonl", "why": "training set; regenerating is not deterministic"},
    {"path": "checkpoints/base.pt", "why": "base weights; referenced, 4.2GB on gpu-node-7"}
  ],
  "excluded": [
    {"path": "data/cache/", "why": "regenerated from train.jsonl on first epoch"},
    {"path": ".env", "why": "credentials; run needs HF_TOKEN, value not recorded"}
  ],
  "env_vars_that_matter": ["HF_TOKEN", "CUDA_VISIBLE_DEVICES"]
}
```

### Capture calls

Use the surface the run was opened with.

| | in the script (SDK) | from a shell (CLI) |
|---|---|---|
| metrics | `run.log({"loss": l}, step=i)` | `probe log RUN loss=0.4 --step 100` |
| per-device / per-actor | `run.log_hw({"gpu_temp": 88}, device=3)` | `probe log RUN gpu_temp=88 --dim device=3` |
| nested structure | `with run.span("trial", ...) as s:`, `run.step(i)` | `probe span add RUN --type trial` |
| outputs | `run.log_artifact("ckpt", path=...)` | `probe artifact add RUN PATH --name ckpt` |
| external ids | `run.link(wandb_run_id="abc")` | `probe link RUN --set wandb_run_id=abc` |
| a run FROM a run | `run.child(relation="retry")` | `probe exec --parent RUN --relation retry -- cmd` |
| computed metrics | `run.log_derived_series("eval/auc", pts, producer="…")` | `probe log RUN eval/auc=0.9 --step 100 --derived --producer …` |
| expression views | `run.create_view("loss_ratio", spec)` | `probe views create RUN loss_ratio --spec-file spec.json` |

**Only the SDK runs inside the training loop**, so `step=` is a real curve
there and scattered points anywhere else. Omitting `step=` auto-increments per
metric kind; `step=None` puts the point on the wall-clock axis. `run.log()`
takes any type: numbers plot; strings, dicts, lists and None are stored on the
step and read back under `view="trajectory"`.

Use the `with` form of `run.span`: both timestamps off one clock, it nests
what opens inside it, and it closes `failed` if the body raises.
`instrument-code` covers metric-vs-span and the leak rule.


### Delivery - what queues

`probe log`, `probe span add` and a RUN-anchored `probe artifact add` queue
into a durable local outbox and return immediately. `--sync` forces blocking.

- `probe outbox status` exits 0 when everything is delivered; MCP reads lag
  the same way.
- **`probe run end` is the barrier.** `--async` queues the close BEHIND the
    data; `--flush-timeout N` bounds the wait.
- **Only run-anchored artifacts queue.** Project, experiment, workspace and
    Shared uploads are synchronous unless you pass `--async`.
- **Failures surface** as a stderr `outbox:` banner and in `probe outbox
    status` / `probe doctor`. `probe outbox retry` requeues dead-lettered
    items.

Relaunch, rewind and fork: `instrument-code`, RELAUNCHING.

## 4. FILES AND NUMBERS

### Artifact commands

An artifact is a name with immutable versions, each pinned from an upload.

| what | command |
|---|---|
| upload an output | `probe artifact add RUN PATH --name N --kind KIND --step N` |
| upload to a non-run anchor | `probe artifact add --project P \| --experiment E \| --workspace W \| --shared PATH --name N` |
| list a run's outputs | `probe artifact list RUN` |
| browse one folder at a time | `probe artifact tree RUN --prefix P --limit N` |
| download a pinned version | `probe artifact download ARTIFACT_ID --version N --to PATH` |
| read the version chain | `probe artifact versions ARTIFACT_ID` |
| pin a new version | `probe artifact version-add ARTIFACT_ID --from-artifact SOURCE_ID --label L` |
| who depends on this | `probe artifact pin-impact ARTIFACT_ID` |
| record producer lineage | `probe edge add --source run:RUN --target artifact:ID --relation produces` |

### Big files

`artifact add` streams, so the upload itself handles any size. Over 100 MB,
record a pointer instead:

| the bytes live | pass | what lands |
|---|---|---|
| on this box or a shared volume | `--reference` | a `file://` uri plus path and host - name the host in `--notes`. `--hash` fingerprints; `--allow-missing` for a path this host cannot see |
| already in a bucket | `--uri s3://...` | the object uri, resolvable by anyone with the bucket |

`--kind`, `--step`, `--span` and `--meta` are run-anchor only. References are
run/project/experiment only — a workspace or Shared file IS its bytes, so
`--reference` and `--uri` are errors there. Code is always stored, never
pointed at (`--kind code|script|source`, or the snapshot's `code_bytes`).
Uploads are content-addressed: bytes already held are never re-sent. From the
SDK, `run.log_artifact(name, path=…, reference=True)` records where the bytes
are without moving them.

### Artifact names and versions

**`--name` is the file's relative path; its extension is kept** (`--name ckpt`
on `ckpt-4000.pt` stores `ckpt.pt`) so the dashboard can preview it. What the
file IS goes in `--notes`, never in `--name`.

**Before creating an artifact, check `entity(refs=["artifact:<name>"],
view="versions")`:**

| resolved to | do |
|---|---|
| an exact compatible version | download it; record consumption, do not copy into a new identity |
| same purpose, content must change | produce it, upload, then `version-add` to the SAME artifact |
| nothing compatible | `artifact add` - a new artifact and its first version. Say why in the experiment's notes |

For datasets, pin provenance in the version meta: input asset versions, the
transform script version, parameters, schema, output content hash.

### Metric shape - decide before you log

A series is a key plus a dimension combination. Every distinct combination is
a separate series, and a one-point series renders as a scalar tile, not a
chart.

| shape | how to log it | renders as |
|---|---|---|
| curve (loss, lr, reward over time) | one series, many points, differing in `step=` | a line chart |
| headline scalar (final accuracy) | one series, one point, 0-2 low-cardinality dims | a stat tile |
| breakdown (accuracy by category) | one series per category value, cardinality under ~20 | N tiles, or a grouped read |

**Dimensions are the LOW-CARDINALITY axes you group by** — split, seed, rank,
category, never an identifier. Series ≈ the product of your dimension
cardinalities; past ~50 you have a wall of tiles.

**A labeled point is never plotted.** Charts read only unlabeled points;
labels make a point addressable in per-sample views. So the curve and the
per-sample record are two writes under two keys:

```python
for i, trial in enumerate(trials):
    run.log({"reward": trial.reward}, step=i)                    # THE CURVE: no labels
    run.log({"reward_sample": trial.reward}, step=i,             # the per-sample record
            labels={"instance_id": trial.id, "repo": trial.repo})
```

One key logged both ways draws a silent subset. Keep the headline ONE series
(`accuracy`, with `accuracy_per_example` under its own `kind=`) and declare
`agg="mean"` at the write.

**After the FIRST run of any new logging code, assert the shape** — a bad
shape is silent, since every call succeeds:

```python
series = client.run_series(run.id)      # one row per (key, kind, dimensions)
assert len(series) < 50, f"{len(series)} series — a wall of tiles, not graphs"
plottable = [s for s in series if s["point_count"] > 1 and not s["has_labeled_points"]]
assert plottable, "every point is labeled or single — nothing will draw"
```

Run BOTH: moving an id from `dimensions` to `labels` fixes the first and trips
the second. Do it on a 2-instance run, not after 300. For a breakdown,
`metrics(mode="grouped")` with a large `step_bucket` should return rows with
`n > 1`.

### Derived metrics and expression views

Two doors for a metric you thought of after the run finished:

- **Derived metrics** - computed in Python, pushed as points:
    `run.log_derived_series(key, points, producer=…)` for a curve,
    `run.log_derived({...}, step=…, producer=…)` for one step. `producer` and
    `step` are required; a backfill lands on existing steps.
- **Expression views** - a formula over existing series, evaluated at read
    time so it stays right as a live run advances. `probe.expr` or
    `--spec-file`. `preview` before `create`: a spec naming an unlogged series
    returns `missing_inputs` instead of an empty panel.

## 5. PAPERS

`probe paper add PROJECT "TITLE"` on a `research` project; then `list`,
`update`, `tag`, `remove`, `edges`.

| flag | what goes in it |
|---|---|
| `--source` | REQUIRED - url or local path (`--url` is an alias) |
| `--authors` | free text, "Vaswani et al." |
| `--repo` | the repository read alongside the paper |
| `--summary` | the main idea in YOUR words (`@file.md` works) |
| `--discrepancies` | what the repo does that the paper does not say: censored code, a missing ablation, a contradicted hyperparameter (`@file.md` works) |
| `--tag` | one concept, repeatable - the axis a forty-paper review gets grouped on |

`--via-provenance`, required with `--via <paper id>`:

| value | when |
|---|---|
| `observed_call` | a tool call handed it to you - `find_papers(mode="similar", expand="references"\|"citers")` already knew the source |
| `provider_citation` | you took it off a reference list |
| `human` | someone told you |
| `inferred` | you worked the link out afterwards |

Amend, never re-add:

- `paper update` leaves omitted fields alone; `""` clears any field but
  `--title` and `--source`.
- `paper tag <id> <concept>` adds, `--remove` drops, `--set` replaces.
- `paper list <project> [--tag <concept>] [--limit 50]` reads newest first;
  `--tag` requires ALL of them. `paper edges <project>` reads the chain.
- Adding counts as project activity; editing and removing do not.

## 6. NOTES

Projects, experiments, runs, groups, artifacts, plus ONE team note; a trial
has none. `probe notes checkout` -> edit the file -> `probe notes push`. The
`edit-notes` skill is the manual: caps, merging, sub-notes, compaction. The
CLI is the only writer; a script shells out to it.

## 7. READING BACK

**`metrics` has three modes**; each refuses the others' arguments, so a
refusal means re-issue in the right mode.

| mode | reads |
|---|---|
| `grouped` | reduces a key |
| `coordinates` | enumerates the axes a run logged on — call it first when guessing at `by` |
| `points` | raw points, a page at a time |

`entity`'s own description lists every view and `card` returns
`available_views` - ask the tool, never a memorised table. Take the narrowest
view that answers the question; `view_options` narrows server-side. A
`partial` read is not the whole record: name what you did not see next to the
finding it qualifies.

To trace a path, URI, artifact id or content hash: `search_knowledge` with it
as the query (exact match), then `entity(view="lineage")` on the run that owns
the hit. No hit means unknown provenance, not none - say so.

## 8. PUBLISHING

Only when the researcher explicitly asks to mark, publish or approve. What
gets published is an immutable **experiment version** (a manifest of its runs)
plus results pinned as artifact versions. There is no run-level "official"
flag; never invent one.

1. `entity(view="reproduce")` on each candidate run: check question and config, that `env_ref` resolves (`missing: ["execution_record"]` = no snapshot, not reproducible), and that `code.manifest.n_pending_upload` is zero.
2. `entity(view="versions")` on the experiment: if a version already covers this set, do not mint a second.
3. Present the exact experiment + asset versions and get explicit approval for that set. Metrics and exit status are not approval.
4. `probe artifact version-add ...` per approved asset. Then mint it:
   `probe version create EXPERIMENT_ID --label LABEL`, and report the version.

Never decide on a `partial` view - §7.

## 9. RECIPES

### Data processing steps as runs

One project-direct run per script or stage VERSION, with a deterministic
`--external-id`:

```
probe run start --project PROJ --external-id clean-structures-v2
probe artifact add $RUN clean_structures.py --kind script
probe edge add --source run:$RUN --target artifact:RAW_ID --relation consumes
probe edge add --source run:$RUN --target artifact:CLEAN_ID --relation produces
probe run end $RUN --status completed
```

A FAILED step resumes on retry with the same id. A COMPLETED one refuses it -
bump the version. Thresholds chosen and rows deleted go in the run's notes:
deletions are provenance, not housekeeping.

### Provisioning runs

```
probe run start --project swe-smith-shakedown --tag infra
probe link $RUN --set gcp_zone=us-central1-a --set gcp_machine_type=a3-highgpu-8g
probe run end $RUN --status failed
```

The error text goes in the run's notes.

`probe link` puts machine identity in `foreign_keys`, where a reader can match
it against the training run, which points back with `probe link $TRAIN --set
provisioned_by=$INFRA_RUN`. **`foreign_keys` is ONLY for what the lineage
vocabulary cannot say** (`consumes` `produces` `evaluates_on` `forked_from`
`resumed_from` `retried_from` `branched_from` `promoted_to` `derived_from`) -
a relaunch after a crash is `run child --relation retry`, not a key. Attempts
cannot share a run group (groups are experiment-anchored; a project-direct run
422s on `group_id`): use a shared key (`--set campaign=h100-hunt`) plus a
paragraph in the project's notes. The runs are the evidence; the notes are
what gets read.

### Trials

A TRIAL is one rollout, addressed by its rollout span id - the last segment of
a `/runs/<run>/trials/<id>` link. `probe trial list RUN` is the authored
inventory, not `probe span list --type rollout`: the span is what the producer
emitted, the trial is the row beside it.

**Its name resolves through a chain**, and only the first link is yours:
`trial set --name` (or a generated title) -> the rollout span's generated
description -> the span name the script passed -> `Unnamed trial`. The
generated title lands once the run goes terminal, from the span's
`attributes`, so at instrumentation time the lever is a meaningful `name=` and
populated `attributes`. A name you set is sticky: generation and the
producer's retries both leave it alone.

`trial set --name|--description` are the only authored fields; a trial has no
notes.
