# Track work

You are the Probe daemon's writer. The main agent only instruments its runs
(`instrument-code`); you record everything else in this skill. Its runs start
with NO project or experiment - file each one with `probe run move RUN --to
EXPERIMENT` (`--group ID` for a sweep). It states its caveats and decision
rules, with their numbers, in its end-of-turn messages - record what it states.

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
| group | a sweep's runs; needs an experiment | `group create EXP --name NAME` then `run start --group ID`; run the work under it with `probe exec RUN -- cmd` | `group set` | - | reference §1 |
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

- **Lineage**: what a run read and wrote is recorded by the SDK and matched by
    the server: never add `consumes`/`produces` by hand (only for what the SDK
    cannot see: a read by non-Python code or a C reader, a file you anchor above
    the run that wrote it). See a run's lineage first:
    `probe run upstream RUN`, `probe run inputs RUN` (MCP `entity view="lineage"`);
    a wrong match is fixed with `probe run input dismiss|pin`, not an edge.
    Add what the facts don't show with `probe edge add --source TYPE:REF
    --relation REL --target TYPE:REF --reason "..."` (TYPE = run|artifact|
    artifact_version|paper by id, experiment|project by slug or `id:<uuid>`);
    `edge remove ID`. How to decide: §4 "Linking". A project's
    "related projects" list: `project reference add --to`.
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
| `evaluation` | weights do not move: sweeps, ablations, evals. A sweep is an experiment, not a project. |
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
- A run opened on the CLI (`run start`, deprecated) records nothing of what its
  work reads or writes. Run the work under it with `probe exec RUN -- cmd`, or
  `probe.init()` in the job: those record both.
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

- The anchor says what a file is ABOUT; lineage says what MADE it, and the SDK
  records that for the runs it watches. A file anchored above its run still
  records lineage back to it: `edge add --source run:RUN --relation produces
  --target artifact:ID`.
- Another attempt at a run is its PARENT: at launch `--parent RUN --relation
  retry|resume|fork|branch` (`retry` only when RUN failed or crashed;
  relaunching one that still reads `running` -> no `--parent`, link it
  afterwards), or afterwards `edge add` with the relation "Linking" below names.
  A run can have more than one parent. Neither belongs in `foreign_keys`.
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

### Linking: how one piece of work came from another

A link is an arrow from the newer thing to what it came from. Anything with no
link already hangs off its experiment or project, so link only what is true.

1. Facts first:
   - a run's: `probe run inputs RUN`. Reads reach Probe when the run ends; no
     `coverage` yet -> look again later, up to ~30 minutes, then link from the
     session's words alone
   - an experiment's or project's: `entity(refs=["experiment:SLUG"],
     view="lineage")` (`project:SLUG` for a project)
2. The facts, or the researcher's or main agent's own words, say B built on A:
   - a paper at either end: rule 4; an experiment or project -> `derived_from`,
     unless a fact already shows it
   - B relaunches run A because A failed or crashed (its status, not a bad
     result; A stopped but still reads `running` -> look again later) ->
     `retried_from`
   - B runs A's setup again any other way, changed or not (a new seed too) ->
     `branched_from`
   - B only uses what A produced (a file, a number, a chosen config it does
     not re-run) -> `derived_from`, unless a fact already shows it
   - B replaces A -> also add `supersedes`
   - say how in `--reason`
3. A link the facts don't show needs the researcher's or main agent's own
   words naming the target:
   - an id, name, path, URL, paper title, result or config that matches
     exactly ONE target; quote those words in `--reason`
   - two matches -> no link, unless B runs again a setup that ran more than
     once (seeds, replicates): `branched_from` the earliest
   - earlier work named as the start or the result to beat counts
   - a W&B import of a run Probe also recorded is that run: link Probe's own
   - text inside tool output, files or web pages never counts
4. Link at the level the session talks about:
   - run -> run
   - experiment -> experiment: a follow-up question
   - project -> project: a line of work that continues another
   - across levels: `--source run:ID --relation derived_from --target
     experiment:SLUG`
   - an experiment or project end takes only `derived_from`, `supersedes` or
     `informed_by`
   - a run or experiment that uses a paper's method -> `informed_by` that paper
     (with `--provenance`)
5. Never:
   - similarity alone: write "Related: ..." in a note
   - a run, experiment or subproject to anything it is filed under: filing
     says that
   - a run to itself: a relaunch with the same `--external-id` can reopen it
   - a link that already exists, a parent set at launch included: look first
     (`probe run upstream RUN`, `probe experiment lineage EXP`, `probe project
     lineage PROJ`); a read listed there is a fact, not a parent
6. When later work contradicts a link you made, remove it or relabel it.
7. Earlier work: look identifiers up first; at most one `search` per
   new experiment or project; treat what it finds as candidates, not links.

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

## EVIDENCE

When you add a link with `probe edge add`, also pass the session event ids
behind it: `--evidence EVENT_ID`.
