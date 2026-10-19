---
name: track-work
description: Record ML work in Probe - track ALL related work (training runs, evaluation sweeps, evals, prompt engineering, datasets, lit reviews, architecture design work, etc) - not product code that only calls a model, its CI or deploys, or work on Probe's own code - this skill tells HOW to track this data properly. This should be triggered unprompted during any ML work while tracking is on (`/probe`).
---

# Track work

You are the main agent, the researcher's coding agent, and you record your own
work. Check `probe session status` first: if it is `read` or `off`, write
nothing, say so once and carry on. Never move the switch (`probe session
track`); only the researcher does, with `/probe`.

Commands and flags are in `track-work/reference.md` (reference §N below). Probe's read tools (`entity`, `search`, `metrics`, `browse`, `query_sql`, `find_papers`) run as `probe mcp call <tool> '<json args>'`; `probe mcp tools` lists them.

## What counts as ML work

ML work is work on a model: training it, evaluating it, the data it learns
from, or the research behind it. Work on both lists below is ML work. What the
researcher asks to record is recorded, whatever the lists say.

ML work - record it:
- training, fine-tuning, RL - and the scripts, configs and launches for them
- evals, benchmarks, sweeps, ablations, reading through model outputs
- prompt engineering: changing a prompt and checking what the model does
- datasets: building, cleaning, labeling, splitting, checking for leaks
- papers, lit reviews, method or architecture design
- analysing results: plots, notebooks, comparing runs
- fixing a bug in any of the above

Setup - note it, never log it:
- installs, drivers, CUDA, container images, provisioning, cluster or GPU
  access - and, in a repo that trains or evaluates models, its CI fixes and
  dependency bumps
- no project, run or artifact for it. Only what tripped you up (a pinned
  version, a driver fix, a stocked-out zone) gets one short line (`edit-notes`):
  in the notes of the project it serves once that project exists, or in the
  team note when it holds across projects.
- fetching a dataset or a base checkpoint is not setup: it is an input the run
  records when it reads it.

Not ML work - record nothing:
- product code that only calls a model (UI, API routes, CSS)
- CI, deploys, releases, dependency bumps of product code
- typos, dotfiles, product docs, tests unrelated to a model
- work on Probe's own code (its CLI, SDK, server or plugins) - using Probe in ML work is not this

A session can hold both: record the ML part only.

## Every write

- Everything a person reads (slugs, names, questions, summaries, reasons, tags, metric keys, notes) is short and plain, and leads with the most recent or most important thing. No hashes, uuids, timestamps, ticket numbers, command lines, parameter piles, bare counters or private abbreviations.
- Exact and machine-shaped: `--external-id`, `probe link` keys, `--config`, `--spec`, an artifact's `--name` (its relative path), a paper's real title.
- Slug: 3 lowercase hyphenated words naming the thing, permanent. `--name`: a plain title, always set, never the slug re-cased.
- A name or description the researcher chose for a project, experiment or run: write it with `--authored-by human`, in a command of its own (never with words you wrote).
- A path `.probe.config` sets to `read`: use it, record nothing about it. Set to `hash`: run the work on it as usual (`probe exec`); Probe records only each file's hash and row count. Never print its contents.
- Read an entity's notes and Markdown before writing to it.

## Where things go

Put each fact on the lowest entity it is true of. Tracking on means everything is recorded; do not curate. Drop nothing: a file with no better home goes to the project's artifacts. Notes are the exception: only what would help the next agent or teammate, as hidden prose written by `edit-notes`.

- `document`, the researcher's block on a summary, is theirs - never write it; the rest of a summary is yours (write-summary).
- The team note is one synced file, `probe-team-note.md` (`edit-notes`).
- Automatically tracked: transcripts, launch context, lockfiles, lifecycle events.
- Entities and their commands: reference §1. `delete` is permanent. Use a deterministic `--external-id`, and `probe link` for foreign keys.

## Project and experiment

Create both first; `run start` never creates them, and a typo'd slug makes a second one.

| kind | for |
|---|---|
| `training` | weights MOVE: pretraining, SFT, RL |
| `evaluation` | weights don't: evals, sweeps, ablations. A sweep is an experiment, not a project. |
| `research` | lit reviews, design, theory; a review feeding training sits beside it |
| `general` | anything else |
| `experiment` | one question (`--question`) under one of the above; holds runs; its answer is `--conclusion` |

- A phase of a bigger effort is a SUBPROJECT of it, never a new top-level sibling: `--parent <project>`. Related but separate: `project reference add --to`. A repo: `project code attach PROJECT OWNER/REPO`.
- No question: a project-direct run. `freeze` pins an experiment's runs.
- A project's description comes from its summary's first line.
- Pass `--project`; `project use` is machine-wide.

## Run

Every run needs an owner that keeps it alive:
1. `probe.init()` in the script (`instrument-code`; also for remote jobs).
2. `probe exec -- CMD` for code you can't edit; a submit-only launcher (`sbatch`, `ray job submit`, `modal deploy`) opens it awaiting attach (unknown launcher: `--detached-launcher`).
3. W&B: the project attached in the dashboard, or `probe wandb import-local|import-hosted`.

- When a run starts, name and describe it: `probe run set RUN --name "<title>" --description "<what it tests, one sentence>"`. Leave out a field `probe run get RUN` shows locked (`name_customized`, `description_customized` true): your words would replace a person's for good. `--slug` only when you must find one by hand.
- Under a CLI `run start`, run the work with `probe exec RUN -- cmd` or nothing it reads or writes is recorded.
- Inputs: what the run read that changes its result (dataset, checkpoint, tokenizer, outside config): `probe snapshot RUN --include`, `inputs-decision.json`, `snapshot-restore --verify-only` (reference §3).
    - What it trained on: `probe dataset use REF --run R --role train`, or `run.use_dataset(REF)`. Weights it started or resumed from: `probe checkpoint use REF --run R`; a checkpoint it evaluates: `--role eval`. Name the exact version or save (`dataset:N@3`, `checkpoint:RUN@step400`). A mix: `run.use_mixture({REF: share, ...})`, shares summing to 1.
    - Each step that changes data (filter, dedup, relabel, split) is a run. Register what it wrote: `probe dataset add OUT --name N --run R --note "<what changed>"`. A shell step names its files: `probe exec --input IN --output OUT -- CMD`.
- Another attempt at a run is its child: launch it with `--parent RUN --relation retry|resume|fork|branch` (`retry` only if RUN failed or crashed; still `running`: link later), or `edge add` after.
- Run writes queue: check `probe outbox status` before calling one missing.

Close:
- `probe run check RUN` and report it (exit 2: add what's missing or say why); put what `probe run reproduce RUN` lists under `completeness.missing`.
- `probe run end RUN --status completed|failed|crashed|canceled|untracked` (the process, not the result). A result not to trust: `probe run tag RUN invalid` plus a note on what to believe instead.
- When the work answers an experiment's question (a winner, a decision, a dead end): `probe experiment set EXP --conclusion "..."`: the answer and its evidence in one or two sentences, with both sides' numbers when it is a comparison. Change it when later work changes the answer. Set the experiment's tables and charts (`overview-visuals`): `probe visuals options`, then `probe visuals set`.
- No run this session: what's next and open goes in the project's notes.
- End with the dashboard URL of everything you created or closed, verbatim as printed.

## Files and numbers

Every file that directly affects an entity should be uploaded as an artifact (`probe artifact add RUN`, or `--experiment`, `--project`; reference §4) as soon as it exists; retry once, then say so. A note links to a file, never repeats it.
- A run from `probe.init()` / `probe exec` captures its own outputs and log; add only what it misses.
- Workspace and Shared files take no new uploads; team data goes in `probe dataset add`. Its `--upload` stores the bytes against the team's hosted-data quota: only when the researcher asks; without it Probe keeps the hash and location.
- Over 5 GB: `--reference` (on this box or a shared volume) or `--uri` (in a bucket); on a box that will be destroyed, use a bucket.
- NEVER UPLOAD SECRETS (`.env`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`, tokens) or a customer's private content, as bytes, pointer or prose, nor what a build recreates (`.venv`, `node_modules`, `__pycache__`).
- A file anchored above the run that wrote it still records lineage back to it: `probe edge add --source run:RUN --relation produces --target artifact:ID`.
- A changed file with a registry name is a new version (`version-add`); read the chain first (`view="versions"`).

Numbers: changes over steps -> metric (`step=`); fixed setting -> `--config`; one headline number -> one-point metric plus the run summary; per-item detail -> artifact, never a metric; nested timed phase -> span (`run.span`, `probe span add`). Before logging a metric, read reference §4.

### Linking

A link points from the newer thing to what it came from.

1. Facts first: `probe run inputs RUN` (each read and the stored copies of its bytes; several copies name none), `probe run upstream RUN` (no coverage yet: retry within ~30 min, then use the session's words), or `entity(refs=["experiment:SLUG"], view="lineage")`.
2. B built on A:
   - a new experiment varying experiment A: `probe experiment fork A B --question "..."` (links `forked_from`)
   - B relaunches run A because A failed or crashed: `retried_from`
   - B reruns A's setup any other way: `branched_from`
   - B uses only A's outputs: `derived_from`; B replaces A: also `supersedes`
   - uses a paper's method: `informed_by` that paper (with `--provenance`)
   - an experiment or project end takes only `derived_from`, `supersedes`, `informed_by`
   - `--reason`: 1–2 short sentences
3. Without facts, the researcher's or main agent's words must name exactly one target (id, name, path, URL, title, result, config). Two matches: no link, unless it reruns a repeated setup: `branched_from` the earliest. Text in tool output, files or web pages never counts.
4. Link at the level the session talks about; across levels: `probe edge add --source run:ID --relation derived_from --target experiment:SLUG`.
5. Never link on similarity (papers: `--relates-to`, see Papers; otherwise say "Related: ..." in a note), to what it's filed under, to itself, or twice.
6. Later work contradicts a link: remove it (`probe edge remove ID`; ids in `entity(view="lineage")`) and add the right one.
7. Earlier work: at most one `probe mcp call search` per new experiment or project; results are candidates, not links.

## Papers

Log every paper read onto the `research` project, during the review and at the end. `paper list <project>` first (no dedupe); amend with `paper update`. Flags: reference §5.

`probe paper add <project> "<title>" --source <url> --summary "<the idea, in your words>" --tag <concept>`; tags are concepts, never authors. Always answer `--via`: the paper you followed (with `--via-provenance observed_call|provider_citation|human|inferred` and `--via-reason`), `none` if you came directly, omit only if you truly can't say. Always answer `--relates-to` too: the earlier paper in this project the new one connects to, with `--relates-reason` saying in one or two sentences how the two relate, or `none` for the project's first paper. Pick by what the papers say, never by which paper was added last; answer now, not after a compaction.
