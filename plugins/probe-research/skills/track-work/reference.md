# Reference — capture calls, artifacts, snapshots, publishing, admin

Syntax and the rules that only matter once you are already doing the thing. The
judgment lives in `SKILL.md`; this is the lookup table.

## Capture calls

Record with the surface the run was opened with:

| | in the script (SDK) | from a shell (CLI) |
|---|---|---|
| metrics | `run.log({"loss": l}, step=i)` | `probe log RUN loss=0.4 --step 100` |
| per-device / per-actor | `run.log_hw({"gpu_temp": 88}, device=3)` | `probe log RUN gpu_temp=88 --dim device=3` |
| nested structure | `with run.span("trial", ...) as s:`, `run.step(i)` | `probe span add RUN --type trial` |
| outputs | `run.log_artifact("ckpt", path=...)` | `probe artifact add RUN PATH --name ckpt` |
| external ids | `run.link(wandb_run_id="abc")` | `probe link RUN --set wandb_run_id=abc` |
| sub-runs | `run.child("fold-2")` | `probe run child RUN --name fold-2` |
| computed metrics | `run.log_derived_series("eval/auc", pts, producer="…")` | `probe log RUN eval/auc=0.9 --step 100 --derived --producer …` |
| expression views | `run.create_view("loss_ratio", spec)` | `probe views create RUN loss_ratio --spec-file spec.json` |

Only the SDK column can be called from inside the training loop, so `step=` is
a real curve there and a scattering of points anywhere else. Omitting `step=`
auto-increments per metric kind; pass `step=None` when there genuinely is no
step and the points belong on the wall-clock axis. `run.log()` takes values of
any type — numbers plot; strings, dicts, lists and None are stored on that
step's record and come back under `view="trajectory"`.

Spans are for work that NESTS — a trial containing agent turns containing a
verifier — where the tree is the finding. A flat training loop is not that
shape: its phases belong in one metric series each (`perf/rollout`,
`perf/train`); `spans: 0` on a trainer run is usually correct. Prefer the
`with` form: both timestamps off one clock, nests what opens inside it, closes
`failed` if the body raises — spans have no heartbeat and no reaper, so one
abandoned by an exception stays `running` forever.

Do not invoke `probe hook ...`; those are reserved for deterministic
coding-agent hooks.

## Metric shape — decide before you log

A series is a key plus a dimension combination; every distinct combination is a
separate series, and a one-point series renders as a scalar tile, not a chart.

| shape | how to log it | renders as |
|---|---|---|
| curve (loss, lr, reward over time) | one series, many points, differing in `step=` | a line chart |
| headline scalar (final accuracy) | one series, one point, 0-2 low-cardinality dims | a stat tile |
| breakdown (accuracy by category) | one series per category value, cardinality under ~20 | N tiles, or a grouped read |

Dimensions are the LOW-CARDINALITY axes you intend to group by — split, seed,
rank, category. Never an identifier: `example_id`, a uuid, a filename each mint
their own one-point series — 500 examples logged with `example_id` as a
dimension is 500 tiles and zero graphs. Series ≈ the product of your dimension
cardinalities; past ~50 you have designed a wall of tiles.

**A LABELED POINT IS NEVER PLOTTED.** Charts read the unlabeled stream only
(the server filters on `labels_hash = <empty>`), so a point carrying ANY label
is excluded from every graph. Labels make a point addressable in per-sample
views; they do not annotate a plottable one. The curve and per-sample identity
are two different writes, under two different keys:

```python
for i, trial in enumerate(trials):
    run.log({"reward": trial.reward}, step=i)                    # THE CURVE: no labels
    run.log({"reward_sample": trial.reward}, step=i,             # the per-sample record
            labels={"instance_id": trial.id, "repo": trial.repo})
```

One key logged both ways gives a chart that silently shows a subset. If you
only want one, keep the unlabeled curve — per-item detail belongs in an
artifact anyway. Keep the headline exactly ONE series (`accuracy` separate
from `accuracy_per_example`, the high-cardinality one under its own `kind=`),
and declare `agg="mean"` at the write.

**After the FIRST run of any new logging code, assert the shape** — a bad
shape is silent, since every call succeeds:

```python
series = client.run_series(run.id)      # one row per (key, kind, dimensions)
assert len(series) < 50, f"{len(series)} series — a wall of tiles, not graphs"
plottable = [s for s in series if s["point_count"] > 1 and not s["has_labeled_points"]]
assert plottable, "every point is labeled or single — nothing will draw"
```

Both assertions: they fail on opposite mistakes, and the half-fix that moves an
identifier from `dimensions` to `labels` trips the second while making the
first look repaired. Do this on a 2-instance run, not after 300. When checking
a breakdown, pass an explicitly large `step_bucket` to
`read_metrics(mode="grouped")` and confirm rows come back with `n > 1`.

`read_metrics` is one tool over three grains: `grouped` reduces a key,
`coordinates` enumerates the axes a run logged on (call it first when guessing
at `by`), `points` reads raw points a page at a time. Each mode REFUSES the
other modes' arguments — a refusal means re-issuing in the right mode.

## Derived metrics and expression views

A finished run is not sealed — a metric you only thought of afterwards lands on
the run it belongs to. Two doors:

- **Derived metrics** — you compute in Python and push points.
  `run.log_derived_series(key, points, producer=…)` for a whole curve,
  `run.log_derived({...}, step=…, producer=…)` for one step. `--producer` is
  mandatory (the series carries `origin="derived"` plus provenance); `step` is
  required — a backfill lands on existing steps.
- **Expression views** — a formula over existing series, stored as an AST,
  evaluated at read time; nothing stored, stays correct as a live run advances.
  Build with `probe.expr` or pass `--spec-file`. `preview` before `create` — a
  spec naming a series the run never logged returns `missing_inputs` instead of
  becoming a panel that renders empty for everyone.

## Delivery — queue semantics

`probe log`, `probe span add` and a RUN-anchored `probe artifact add` queue
into a durable local outbox and return immediately; `--sync` forces blocking.
Four rules keep it honest:

- Queued is not delivered: `probe outbox status` (exit 0 = all delivered)
  before treating a missing recent write as absent anywhere, including MCP
  reads.
- `probe run end` is the barrier and stays synchronous: it delivers that run's
  queued items first or exits 2. `--async` queues the close BEHIND the data;
  `--flush-timeout N` bounds the wait.
- Only RUN-anchored artifacts queue. `--project`, `--experiment`,
  `--workspace` and `--shared` stay synchronous and fail loudly at the write;
  `probe outbox status` is their only gate if you opt in with an explicit
  `--async`.
- Failures surface as a stderr `outbox:` banner and in `probe outbox status` /
  `probe doctor`; `probe outbox retry` requeues dead-lettered items.

Prefer `--reference` over uploading bytes when the file lives on storage the
team can already resolve. `run.log_artifact(name, path=…, reference=True)`
records WHERE the bytes are without moving them.

## Artifacts and asset versions

The registry is a named artifact with immutable, zero-copy versions. A version
is pinned from an uploaded artifact; versions are never edited in place.

| what | command |
|---|---|
| upload an output | `probe artifact add RUN PATH --name N --kind KIND --step N` |
| upload to a non-run anchor | `probe artifact add --project P \| --experiment E \| --workspace W \| --shared PATH --name N` |
| list a run's outputs | `probe artifact list RUN` |
| download a pinned version | `probe artifact download ARTIFACT_ID --version N --to PATH` |
| read the version chain | `probe artifact versions ARTIFACT_ID` |
| pin a new version | `probe artifact version-add ARTIFACT_ID --from-artifact SOURCE_ID --label L` |
| who depends on this | `probe artifact pin-impact ARTIFACT_ID` |
| record producer lineage | `probe edge add --from run:RUN --to artifact:ID --relation produces` |

`probe artifact add` streams to storage, so multi-GB files upload without being
read into memory; `--uri` records a reference only.

What the reuse check (`get_entity(ref="artifact:<name>", view="versions")`)
resolved to decides the next command:

- **exact compatible version exists** → download it; record consumption, do
  not copy into a new identity.
- **same purpose, content must change** → produce the new content, upload,
  then `version-add` to the SAME artifact.
- **nothing compatible exists** → `probe artifact add` opens the new identity
  and its first version. Record the concrete reason in the experiment.

For datasets, pin provenance in the version meta: input asset versions, the
transform script version, parameters, schema, output content hash.

## Snapshot inputs — the decision record

```
probe snapshot RUN --cwd PATH --include 'data/**' --include checkpoints/base.pt
probe snapshot-show RUN                  # what was captured
probe snapshot-restore RUN --verify-only # can it be rebuilt: want "0 unavailable"
```

A glob matching nothing is an error; a path outside the snapshot root is
refused; naming a file already in the manifest adds no duplicate. Size is handled
(`--reference-over-mb`, default 100). A non-git directory is captured whole,
skipping lockfile-rebuilt trees and credential-shaped names.

The decision record is an artifact on the run:

```
probe artifact add RUN inputs-decision.json --kind inputs_decision
```

```json
{
  "included": [
    {"path": "data/train.jsonl", "why": "the training set; regenerating is not deterministic"},
    {"path": "checkpoints/base.pt", "why": "base weights; referenced, 4.2GB on gpu-node-7"}
  ],
  "excluded": [
    {"path": "data/cache/", "why": "regenerated from train.jsonl on first epoch"},
    {"path": ".env", "why": "credentials; run needs HF_TOKEN, value not recorded"},
    {"path": "outputs/", "why": "produced by the run, logged as artifacts"}
  ],
  "env_vars_that_matter": ["HF_TOKEN", "CUDA_VISIBLE_DEVICES"]
}
```

## Project Summary and notes — the write loops

The Summary is one rendering with two owners: the server refreshes the
server-owned AI narrative — never edit or imitate it; agents and people
replace only `summary_markdown`, the durable suffix. Whole-document,
last-write-wins — read, edit, write, verify:

```
probe project get PROJECT | jq -r '.summary_markdown // ""' > PROJECT.md
# Edit PROJECT.md; retain useful existing sections.
probe project set PROJECT --summary @PROJECT.md
probe project get PROJECT | jq -r '.summary_markdown // ""'  # verify
```

**Embedding a repository's README:** a line containing only
`[README](https://github.com/owner/repo)` renders that repo's README at that
point — live, refreshed on push, private repos included when the team's GitHub
App is installed. Two traps: the LINK TEXT is what makes it an embed (an
ordinary citation or bare URL embeds nothing), and the project's read-only
`repo` field is DERIVED from the line — writing one connects the repository,
removing it disconnects. Never write it for a repo the project is not about.
Spend your own words on what the README does NOT say.

Notes commands (projects, experiments, runs, trials, groups, artifacts, plus ONE
team note):

```
probe notes show                       # this project's operational briefing
probe notes append <<'EOF'             # add a paragraph, concurrency-safe
The 2026-08 export supersedes the 07 one; rows 400-900 were duplicated.
EOF
probe notes append --experiment EXP --run RUN --trial TRIAL --group GRP --artifact ID
probe notes edit --old "DOKS is out" --new "DOKS is out (retest 2027)"
probe notes status [--above 80]        # how full every note in the team is
probe notes team [--brief]             # the team note; it is a FILE, edit that
```

`append` adds; `edit` replaces one exact span (`--new` omitted deletes; a
non-unique `--old` is refused with the match count — copy more surrounding
text, never guess). Neither asks you to hold the document in context.
COMPACTING is editing: fold finished appends into sections rather than letting
the bottom grow. `--notes` on the older per-entity verbs REPLACES the whole
value — prefer `notes append`.

NOTES ARE CAPPED: 100,000 characters on a project, experiment or the team note;
4,000 on a run, trial, group or artifact. Through `notes append`/`notes edit` an
over-cap write is REFUSED, not truncated — the batched `/ingest/v1/runs` machine
door clamps instead, so a note pushed inside a run body can come back shorter
than you sent it.

`probe notes append`/`edit` print the room left and start advising at 60% full.
Act then, not at the wall: at the cap the write is REFUSED, so the paragraph you
just wrote is the one that does not land, and trimming it and retrying fails
again — the document is closed until it is compacted, and the refusal says so.

The CLI is the ONLY writer. Notes are not writable through the SDK: under its
async default a write queued and returned None, so an over-cap append was
dead-lettered instead of refused. From a script, shell out to `probe notes
append`.

A shrinking `edit` is accepted AT the cap, so a full document is never stuck. On
one already OVER its cap — a lowered cap, or a legacy row — the guard is on the
RESULT, so a single edit has to land under the cap: shrink further rather than
retrying the same span. What to do differs by carrier:

  * project / experiment — COMPACT in place: fold the appends at the bottom up
    into the sections above with `notes edit`.
  * the TEAM note — same 100,000 cap, but it takes no `append`/`edit` verb at
    all: compact it by editing the synced FILE, the way you write it.
  * run / group / artifact — the prose has outgrown a row annotation, so MOVE IT
    UP into a project or experiment notes document. Append there FIRST, then
    delete it here: that is two writes on two entities and nothing makes them
    atomic, so this order duplicates the prose if the second write fails and the
    other order loses it. A workspace or shared-folder artifact has no research
    parent at all — move its prose to the project that owns the work.

`notes status` is the tenant-wide view, fullest first — run it when you arrive
somewhere unfamiliar, or before dumping a long document into a note.

The project's notes are the default operational handoff because an excerpt
rides on the project's MCP card; run and group notes are read only by someone
already at that row.

## Display copy — names, descriptions, hypotheses

Written for a teammate, not the execution log. Names: 2-6 familiar words,
hyphenated when the CLI needs a ref — never a command, ticket number,
timestamp, petname or parameter pile. Descriptions: 1-2 sentences, ≤40 words —
what the work is, why it exists, which decision it supports. Hypotheses: one
plain testable sentence, ≤30 words, naming the expected outcome and the change
expected to cause it; preserve uncertainty. Explain acronyms; ground wording in
real product or milestone context only when supplied — never invent company
context or expand an unknown codename. Exact checkpoints, paths, commands and
parameter lists go in config, metadata or notes, so the simplified display
never costs reproducibility.

## Provisioning runs — the worked example

```
probe run start --project swe-smith-shakedown --tag infra \
    --name pair-training-capacity \
    --description "Request the accelerator capacity needed for the pair-training phase."
probe link  $RUN --set gcp_zone=us-central1-a --set gcp_machine_type=a3-highgpu-8g
probe run set $RUN --notes "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS. Same in -b and -c."
probe run end $RUN --status failed
```

`probe link` puts machine identity on `foreign_keys` where a reader can match
it against the training run; the training run points back with
`probe link $TRAIN --set provisioned_by=$INFRA_RUN` — the lineage vocabulary
(`consumes`/`produces`/`evaluates_on`/`forked_from`/`resumed_from`/
`promoted_to`/`derived_from`) has no "provisioned by", so `foreign_keys` is
the door. A campaign of attempts CANNOT share a run group (groups are
experiment-anchored; the backend 422s a `group_id` on a project-direct run) —
use a shared `foreign_keys` key (`--set campaign=h100-hunt`) plus one
paragraph in the project's notes. Write the CONCLUSION in notes: the runs are
the evidence, the notes are what gets read.

## Publishing

Only when the researcher explicitly asks to mark, publish, or approve. The
published record is an immutable **experiment version** — a manifest of the
experiment's runs — plus reusable results pinned as artifact versions. No
run-level "official" flag exists; never encode one in a filename or metadata.

1. `get_entity(view="reproduce")` on each candidate run: verify hypothesis and
   config, that `env_ref` resolves (`missing: ["execution_record"]` = no
   snapshot, not reproducible), and `code.manifest.n_pending_upload` is zero.
2. `get_entity(view="versions")` on the experiment: if a version already
   covers this set, do not mint a second.
3. Present the exact experiment + asset versions and obtain explicit approval
   for that set. Metrics and exit status are not approval.
4. `probe artifact version-add ...` per approved asset, then
   `probe version create EXPERIMENT_ID --label LABEL`. Report the version.

A `completeness.state` of `"partial"` on any view the decision rests on means
you have not seen the whole record — resolve it or say so before asking for
approval.

## Tracing a path, URI, or content hash

`search_knowledge` with the path, URI, artifact id or content hash as the
query — the exact channel matches artifacts directly. Then follow
`get_entity(view="lineage")` on the run that owns the hit. There is no
trace-file tool, deliberately: if you cannot establish provenance, say so —
never infer absence from an empty result.

## Choosing a read view

`get_entity` carries the full view matrix in its own description, and `card`
returns `available_views` — ask the tool, do not memorise a table that can go
stale. Ask for the narrowest view that answers the question; narrow with
`filters` (server-side); `handoff`'s `span_types` counts say whether a
`trajectory` call is worth making. Trust the envelope over your own optimism:
when you cannot resolve a partial read, name the unseen part in the same
breath as the finding it qualifies.

## Project and experiment admin

`probe project create | list | get | use | set | move | delete`
`probe experiment create | set | delete | edges`
`probe run set RUN [--name] [--description] [--notes TEXT|@FILE|-]`
`probe group create EXP --name NAME [--kind] [--spec JSON|@FILE] [--notes ...]`
`probe group set GROUP [--name] [--spec] [--notes ...]`

`probe project use` sets the ambient project MACHINE-globally — prefer
`--project` or `PROBE_PROJECT` whenever another session might be running.
`run start` uses the ambient value only to CHECK the experiment's home; it no
longer files anything. `probe experiment set EXP --hypothesis "..."` amends a
hypothesis (first-write-wins at creation; reopening never rewrites it).
`--notes ""` clears; `@file` and `-` read a file or stdin.
