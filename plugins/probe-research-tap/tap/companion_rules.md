---
name: track-work
description: Record ML work in Probe - track ALL related work (training runs, inference sweeps, evals, lit reviews, architecture design work, etc) - this skill tells HOW to track this data properly. This should be triggered unprompted during any ML work while tracking is on (`/probe`).
---

# Track work

This skill outlines how to properly track ML work in Probe.

This file is the judgment; `track-work/reference.md` beside it is the lookup.
Its INDEX maps each section here to one there - pointers below say `reference
§N`.

# HARD GUIDELINES FOR ALL WRITES:

- all human facing data should be as SHORT, CONCISE, and SIMPLE as possible - it should be readable by a new member of the team who has little context
- **human facing**: every SLUG and `--name`; `--question`, `--summary`, `--discrepancies`, `--via-reason`; every `--tag`, metric key and span name; the run summary at `run end`; all notes and the team note.
    - human facing values should never have: hashes, uuids, timestamps, ticket numbers, command lines, parameter piles, bare counters, or abbreviations only you can expand.
- NOT human facing - keep exact and machine-shaped: `--external-id`, `probe link` foreign keys, `--config`, `--spec`, an artifact's `--name` (the file's relative path) and a paper's title (its real one).

## ENTITY NAMING

A SLUG is permanent. A NAME is not.

- SLUG: 3 lowercase hyphenated words saying what the thing IS - ex: `tool-calling-reliability`. It never changes, and it is the fallback heading, so it must read alone
- `--name`: a simple concise English title. Always try to set this - this session has some of the best context on what this work is about. 
    - Never just re-case the slug ("Bfcl Toolcall Sft")

## 0. STATE GATE

Check `probe session status` first: this skill is all about writing. If it is
`read` or `off`, do not write - say so once and carry on. Never move the switch
(`probe session track`) to make a write legal; only the researcher does that.

In the `daemon` state you ONLY instrument, and the daemon records. Put the SDK
in the scripts you write (`probe.init(description=..., tags=[...], config={...},
intent="what this run should show")` with NO project or experiment: the run
starts unfiled and the daemon files it), or wrap code you can't edit in `probe
exec -- ...`; log the run's metrics and spans; `run check`, `run end`. Create
no projects, experiments or groups, and write no notes, artifacts, papers, tags,
names, descriptions or lineage: the daemon does all of that from the session.
Say your caveats and decision rules, with their numbers, in your end-of-turn
message - the daemon records what you state. A write the researcher asks for
takes `--directed`, so the daemon does not repeat it.

## 1. THE MAP

Write each fact to the lowest entity it is true of - no wider (run detail lost
in a project), no narrower (a project-wide fact buried on one run). Tracking on
means everything is recorded; do not curate. Drop nothing: a file with no better
home goes to the project's artifacts, prose to its notes.

What to do, per entity:

| entity | what it is | make | change | undo / move | how-to |
|---|---|---|---|---|---|
| project | high level organization for a given effort; `--kind` required; a phase nests via `--parent` | `project create` | `project set\|tag` | `project move` (parent, top level, workspace); `delete` is PERMANENT | §2 |
| experiment | one question inside a project; IS a project, `kind=experiment` | `experiment create --question` | `experiment set --question\|--name\|--summary`, `tag`; `freeze` pins its runs (NO `--description`: the question is it) | `delete` PERMANENT | §2 |
| run | one execution, in an experiment or project-direct | `probe exec CMD` / SDK `run()` | `run set\|tag`, `run end --status` | `exec --parent RUN --relation retry\|resume\|fork\|branch`; `run fork SRC --step`, `run start --rewind-to-step` (see `instrument-code`, RELAUNCHING); `delete` PERMANENT | §3 |
| group | a sweep's runs; needs an experiment | `group create EXP --name NAME` then `run start --group ID` | `group set` | - | reference §1 |
| trial | one rollout under a run; `--name` only, no notes | `trial add DIR --step N`; the Harbor/Miles pipeline is in `instrument-code` | `trial set` | - | reference §1, §9 |
| span | a timed phase inside a run | `probe span add` / SDK `run.span` | - | - | `instrument-code` |
| metric | a value over steps, on a run | `probe log` / SDK `log()` | - | derived: `views preview`, `views create` | §3 / `instrument-code` |
| artifact | a file on a run, experiment, project, workspace or Shared folder; has notes | `artifact add` (`--reference` / `--uri` for big files) | `artifact version-add` | `artifact move` keeps the id; `delete` PERMANENT | §4 |
| paper | a paper read, on a `research` project | `paper add` | `paper update\|tag` | `paper remove` | §5 |
| notes | hidden prose on project, experiment, run, group, artifact | `notes checkout` then `notes push` | same, `--note "<title>"` for a sub-note | `notes delete` (sub-note only) | `edit-notes` |
| team note | ONE synced file per team (`probe-team-note.md`) | edit the file | same | same | `edit-notes` |
| authored Markdown | `summary_markdown`; a block IN the Overview page (run: below AI Summary) | the RESEARCHER's - never write it | - | - | - |
| workspace | yours, across projects; files only | `workspace create\|use` | `workspace rename` | `delete` (empty only) | reference §1 |
| Shared folder | the team's, across projects; files only | `shared add`; `shared share` lifts a workspace file | - | `shared unshare`; `delete` (soft) | reference §1 |

Cross-cutting:

- **Lineage**: `probe edge add --source run:A --target artifact:B --relation
    produces|consumes|evaluates_on|derived_from`; `edge remove ID`. Project to
    project: `project reference add --to`.
- **Repo**: `project code attach PROJECT OWNER/REPO` puts its commit timeline on
    the project (`view="code"`); `project code detach`.
- **Inputs**: `probe snapshot RUN --include`, `snapshot-show`, `snapshot-restore
    --verify-only`.
- **Delivery**: run-anchored writes QUEUE. `probe outbox status` (exit 0 =
    delivered); `outbox drain` or `run end` is the barrier.
- **Identity**: deterministic `--external-id`; `probe link` for foreign keys;
    `project use` is machine-global - pass `--project`.
- **Automatically tracked**: transcripts, session digests, who-worked-on-what,
    launch context (argv, seeds, container), lockfiles, lifecycle events.

### Read first

Read an entity's notes and Markdown BEFORE you write to it.

## 2. PROJECT AND EXPERIMENT

These two are how Probe organizes work. Create them before anything else:

```
probe project create antibody-folding --kind training
probe experiment create lower-sampling-temperature --project antibody-folding \
    --question "Does a lower sampling temperature improve structure accuracy on held-out complexes?"
```

`--kind` is required and specifies what the project is FOR.

| kind | meaning |
|---|---|
| `training` | weights MOVE: pretraining, SFT, RL |
| `inference` | weights do not move: sweeps, ablations, evals. A sweep is an experiment, not a project. |
| `research` | document-shaped: lit reviews, design, theory. A review feeding a training effort is a project BESIDE it, not inside it |
| `general` | everything else; also what W&B import uses |
| `experiment` | a LEAF under one of the four, answering one question. Its `--description` IS that question, which is why `experiment set` has no `--description` to set separately. Runs live in it; nothing nests under it. |

NOTES:

1. Create both before the scaffold. `run start` never creates them - a typo'd
   slug mints a second identity. The SDK's `client.run(project=, experiment=,
   question=)` does.
2. No question, no experiment - open a project-direct run.
3. A phase of a bigger effort is a SUBPROJECT of it, never a new top-level
   sibling: `--parent <project>`. Related but separate work is a reference
   (`probe project reference add`), not a child.
4. `project use` is machine-wide: it retargets every other session's next
   create, and experiments can't be moved back. Pass `--project`.
5. You control the slug, the `--question` and the tags. The server writes the
   title and description itself, but only after a RUN FINISHES underneath - a
   `research` project with no runs keeps its slug forever.

## 3. RUN

A run goes under an experiment or a project and MUST HAVE AN OWNER TO GUARANTEE
ALIVENESS. Put the SDK in scripts you write. Use `probe exec` only for code you
can't edit, or a launcher that only submits the job. Both open the run. There
are currently only three possible owners:

1. `probe.init()` inside the script - read `instrument-code` skill - use this
   for when a job runs on a remote machine also.
2. `probe exec -- python train.py` from a shell

- `probe exec` runs your command and owns the run. It keeps the heartbeat going,
  sets `PROBE_RUN_ID` and `PROBE_RUN_EPOCH` for the command, and closes the run
  with the command's exit code. Script in `instrument-code` skill.
- Some launchers only submit the job and return: `sbatch`, `ray job submit`,
  `modal deploy`. Wrapping them would track the launcher, not the job, so `probe
  exec` opens the run as AWAITING ATTACH and the job claims it when it starts.
  For a launcher it does not know, pass `--detached-launcher`.

3. a W&B project attached in the dashboard - live sync sees only runs created
   AFTER it is enabled; tick "Import existing runs" for older ones. `probe wandb
   import-local ROOT --project P` / `import-hosted` also need an existing
   project

NOTE:
- Do not name runs. The server gives each one a readable slug, and a finished run gets a title generated from its content. Use `--slug` only when you will need to find the run by hand later (a nightly job, say).

### Inputs

`probe exec` and the SDK's `run()` snapshot your code, environment and lockfiles
for you; `probe run check RUN` tells you if anything is missing. The part you
must decide is which untracked files are INPUTS.

An input is anything the run read that changes its result - a dataset, a base
checkpoint, a tokenizer, a config outside the repo. Ask: would the run come out
different if this file were different?

Reference §3 has: how to add files to the snapshot, record which files you chose
and why (`inputs-decision.json`), and check the snapshot can be rebuilt
(`snapshot-restore --verify-only`).

### Capture

- Outbox: writes to a run (`probe log`, spans, run artifacts) go into a local
  queue first. Queued does not mean delivered: run `probe outbox status` before
  deciding a write is missing. `probe run end` waits for the queue to drain.
  Details: reference §3.
- Writes to anything else are immediate and fail loudly. Upload a file as soon
  as it exists - on a machine that will be destroyed, the upload is the only
  copy. If it fails, retry once, then say so.
- Read back what you wrote before relying on it (the `metrics` tool). The checks
  for metric shape are in `instrument-code`.

### Close

- Before calling a run done: `probe run check RUN`, and report what it says.
    - Exit 2 means something needed to reproduce it is missing - add it, or say what and why.
    - `probe run reproduce RUN` lists EVERYTHING MISSING from this run (that's tracked) that you MUST include under `completeness.missing`.
- To end a run: `probe run end RUN --status completed|failed|crashed|canceled|untracked`
    - Status is about the process, not the result.
    - If the result cannot be trusted, `probe run tag RUN invalid` plus a note saying what to believe instead.
- No run this session? Write what is next and what is open into the project's notes.
- End with the dashboard URL of every project, experiment and run you created or closed - the one the tool printed, verbatim, never one you assembled.

## 4. FILES AND NUMBERS

### Files -> artifacts

Every file that directly affects an entity should be uploaded as an artifact. A
note links to it and never repeats its contents. §1 says which entity to hang it
on (its ANCHOR); commands and flags are in reference §4.

A RUN CAPTURES ITS OWN OUTPUTS. When a run opened by `probe.init()` or `probe
exec` ends, every file it created or changed in its folder (`outputs/...`), and
everything it printed (`probe/run.log`), reach the run without you. Do not
re-upload them. Add by hand only what that misses: files outside the run's
folder, a name or kind you choose, a bucket path. Details in `instrument-code`.

RULES:

- Only fully upload a copy of the file if <64MB (the most an upload carries). Else, record a pointer instead - `--reference` when the bytes are on this box or a shared volume, `--uri` when they are already in a bucket.
    - Over 64 MB an upload is refused. A pointer to a box that will be destroyed resolves nowhere: there, write big files to a bucket or a mounted volume and record where they are.

- NEVER UPLOAD SECRETS or a customer's private content, as bytes, as a pointer,
  or in prose: `.env`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`, tokens, for
  ex.
- Don't upload anything a lockfile or a build rebuilds: `.venv`, `node_modules`,
  `__pycache__`, etc.

- The anchor says what a file is ABOUT; an EDGE says what MADE it. A file
  anchored above its run still records lineage back to it.
- Another attempt at a run (retry, resume, fork) is PARENTAGE, not an edge.
  Consuming another run's output is an EDGE. Neither belongs in `foreign_keys`.
- Changing a file that already has a registry name is a new VERSION, never a
  new, duplicate artifact. Read the version chain first.

### Numbers -> metrics

Most things people log as metrics are not metrics. Route it first:

| what you have | where it goes |
|---|---|
| a value that CHANGES over steps (loss, reward, lr) | a metric |
| a setting that does NOT change (batch size, model name) | `--config`, never a metric |
| one headline number (final accuracy) | a metric with a single point, and the run summary at `run end` |
| per-item / per-sample detail | an artifact, NEVER a metric - an id in `dimensions` or `labels` wrecks every chart |
| a timed phase that NESTS (a trial containing turns) | a span; a flat training loop is metrics |

Landed on a metric? Read reference §4 before you write the call - it has the
shape rules and the checks that catch a bad one on its first run.

## 5. PAPERS

Log every paper you read onto the `research` project, DURING the review and
again at its end. `paper list <project>` FIRST - there is no dedupe, so two adds
of one url make two rows; `paper update` amends instead.

```
probe paper add <project> "<title>" --source <url-or-path> \
    --summary "<the main idea, in your words>" --tag <concept> \
    --via <paper-id|none> --via-provenance <how> --via-reason "<one sentence>"
```

`--source` is required. `--summary` is the idea in YOUR words; what it means for
our work is a note on the project, not this. `--tag` is the CONCEPTS the paper
is about, same vocabulary as project and run tags - never an author.
`--authors`, `--repo` and `--discrepancies` (what the repo does that the paper
does not say) round it out; full flags, amend and read-back in reference §5.

ALWAYS answer `--via`. Nothing can rebuild later how you got to a paper:

| how you got here | pass |
|---|---|
| you followed that paper to this one | `--via <paper id>`, `--via-provenance observed_call\|provider_citation\|human\|inferred`, `--via-reason` (one sentence) |
| you came to it directly - a search, or someone handed you the link | `--via none` |
| you genuinely cannot say | omit it - that records "unknown", and it renders differently |

NEVER pass the paper you happened to add last - read order is not derivation.
Answer while you still know: after a compaction it is a guess, and a guessed
edge is indistinguishable from an observed one once stored.
