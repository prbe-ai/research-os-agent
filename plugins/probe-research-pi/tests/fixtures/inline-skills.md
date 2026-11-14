# Skill: track-work

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
- Fields (names, descriptions, questions, conclusions, dead ends, reasons): a short ELI10 TL;DR in plain words. No results, settings, run names or formulas: the dashboard already shows them. Detail goes in notes. Digits that are part of a name stay (CIFAR-10, Llama 3 8B).
- Exact and machine-shaped: `--external-id`, `probe link` keys, `--config`, `--spec`, an artifact's `--name` (its relative path), a paper's real title.
- Slug: 3 lowercase hyphenated words naming the thing, permanent. `--name`: a plain title, always set, never the slug re-cased.
- A name or description the researcher chose for a project, experiment or run: write it verbatim with `--authored-by human`, in a command of its own (never with words you wrote).
- Read an entity's notes and Markdown before writing to it.

## Where things go

Put each fact on the lowest entity it is true of. Tracking on means everything is recorded; do not curate. Drop nothing: a file with no better home goes to the project's artifacts. Notes are the exception: only what would help the next agent or teammate, as hidden prose written by `edit-notes`.

- `document`, the researcher's block on a summary, is theirs - never write it; the rest of a summary is yours (write-summary).
- The team note is one synced file, `probe-team-note.md` (`edit-notes`).
- Automatically tracked: transcripts, launch context, lockfiles, lifecycle events.
- Entities and their commands: reference §1. `delete` is permanent. Use a deterministic `--external-id`, and `probe link` for foreign keys.

## Project and experiment

Create both first, each with `--name "<plain title>"`; `run start` never creates them, and a typo'd slug makes a second one.

| kind | for |
|---|---|
| `training` | weights MOVE: pretraining, SFT, RL |
| `evaluation` | weights don't: evals, sweeps, ablations. A sweep is an experiment, not a project. |
| `research` | lit reviews, design, theory; a review feeding training sits beside it |
| `general` | anything else |
| `experiment` | one question (`--question`) under one of the above; holds runs; its answer is `--conclusion` |

- Projects are flat. A phase of a bigger effort is its own project: `probe project create SLUG --kind K --name "..." --part-of P --reason "..."`. To link related work or an existing project: `probe project reference add --to P [--part-of] --reason "..."`. A repo: `project code attach PROJECT OWNER/REPO`.
- No question: a project-direct run. `freeze` pins an experiment's runs.
- No headline metric yet? Choose one from the code and runs, set it (`probe project set P --metric KEY --goal max|min`), and tell the researcher in one line.
- A project's description comes from its summary's first line.
- Pass `--project`; `project use` is machine-wide.

## Run

Every run needs an owner that keeps it alive:
1. `probe.init()` in the script (`instrument-code`; also for remote jobs).
2. `probe exec -- CMD` for code you can't edit; a submit-only launcher (`sbatch`, `ray job submit`, `modal deploy`) opens it awaiting attach (unknown launcher: `--detached-launcher`).
3. W&B: the project attached in the dashboard, or `probe wandb import-local|import-hosted`.

- Name and describe every run when it starts, each one when many start together: `probe run set RUN --name "<title>" --description "<what it tests, one sentence>"`. Leave out a field `probe run get RUN` shows locked (`name_customized`, `description_customized` true): the server refuses an agent's change to it (409) and writes nothing from that call. `--slug` only when you must find one by hand.
- Under a CLI `run start`, run the work with `probe exec RUN -- cmd` or nothing it reads or writes is recorded.
- Inputs: what the run read that changes its result (dataset, checkpoint, tokenizer, outside config): `probe snapshot RUN --include`, `inputs-decision.json`, `snapshot-restore --verify-only` (reference §3).
    - What it trained on: `probe dataset use REF --run R --role train`, or `run.use_dataset(REF)`. Weights it started or resumed from: `probe checkpoint use REF --run R`; a checkpoint it evaluates: `--role eval`. Name the exact version or save (`dataset:N@3`, `checkpoint:RUN@step400`). A mix: `run.use_mixture({REF: share, ...})`, shares summing to 1.
    - Each step that changes data (filter, dedup, relabel, split) is a run. Register what it wrote: `probe dataset add OUT --name N --run R --note "<what changed>"`. A shell step names its files: `probe exec --input IN --output OUT -- CMD`.
- Another attempt at a run is its child: launch it with `--parent RUN --relation retry|resume|fork|branch` (`retry` only if RUN failed or crashed; still `running`: link later), or `edge add` after.
- Run writes queue: check `probe outbox status` before calling one missing.

Close:
- `probe run check RUN` and report it (exit 2: add what's missing or say why); put what `probe run reproduce RUN` lists under `completeness.missing`.
- `probe run end RUN --status completed|failed|crashed|canceled|untracked` (the process, not the result). A result not to trust: `probe run tag RUN invalid` plus a note on what to believe instead.
- When the work answers an experiment's question (a winner, a decision, a dead end): `probe experiment set EXP --conclusion "..."`: the answer in one short, plain sentence. Change it when later work changes the answer. Set the experiment's tables and charts (`overview-visuals`): `probe visuals options`, then `probe visuals set`.
    - A dead end (its approach dropped, or shown not to pan out): also `probe experiment set EXP --dead-end "<why, in plain words>"`; `--clear-dead-end` when work on it resumes. It still ranks.
- A better experiment to use than the pick? Set it (`probe project set P --best EXP --reason "..."`) and tell the researcher in one line.
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
   - link every follow-up to what it built on (fork or `derived_from`): that draws its branch
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

# Skill: write-summary

Each project and experiment the work touches has a page, a custom Claude artifact in the dashboard's Summary pane. Write it only to its draft and `probe summary push` it; never publish it elsewhere.

The draft is checked out for you when work first lands on one (the path is printed); `probe summary checkout --project <slug>` (or `--experiment <slug>`) loads one by hand. Edit it, then push once with the same flag. Write the first page when you create one; update it on a result, a decision or a change of direction. Linking a project `--part-of` another: update that page to name the new part. When what a project is about changes, or its runs first hold a real example, rewrite its canvas preview too (`write-preview`, when shown to you). A `.md` draft (older CLI) takes Markdown, never html.

Before editing, read `overview-page`, `overview-contract`, `artifact-design`, `artifact-diagramming` and, with metrics, `overview-charts` (beside this file), unless shown to you whole.

# The page, top to bottom

1. TL;DR in plain words a 10-year-old could follow, no numbers: one sentence on what it is, then up to 5 one-line bullets:
   - where it's headed now
   - each key decision: what was picked over what, and why
   - how far it has moved from what it set out to do (the card's `intent`, or its first page in `summary_history`)
2. Next: the open decision, question or ask.
3. Evidence, and how sure we are: charts, tables, diagrams, number tiles, one sentence each. A chart for any logged curve or any comparison of more than three numbers, never a table instead.

- Bullets everywhere, one point each.
- A why comes only from what was recorded: the session, an experiment's conclusion or dead end, a note. None recorded: name the decision alone. Nothing decided or no drift yet: drop the bullet that has nothing to say.
- Each update rewrites the top around the newest decision or result; never add below it. Cut what newer work made stale or wrong.
- A project page covers all its parts, sized by the work in each, not by recency. Edit your part's line, and the TL;DR whenever it stops being true. Detail goes on the experiment's page.

Numbers: measurements to 3 significant figures, counts exact, p-values as Probe gives them. Leave `<!-- kept: ... -->` blocks alone. Who did what: `probe mcp call entity '{"refs": ["<kind>:<slug>"], "view": "sessions"}'` (or `"summary_history"`).

# Skill: edit-notes

# Notes

Notes sit on a project, experiment or run, or are the one team note. People and agents read them. Write only what the next person on this work needs, not what happened.

## Before you write

- Under `/probe read` or `off`, write no entity notes. Under `read`, ask before editing the team note.
- Read the whole note first. Never write from memory or from your last push.
- Never write a play-by-play, a changelog, secrets, a customer's private content, or text pasted from a paper, README or web page.

## Where it goes

The widest entity the fact is true of, no wider: run, experiment, project, parent project, workspace, team note. A big separate topic, like a handoff, gets its own sub-note.

## How it reads

- People read top to bottom. Start with one line on what this is, then the most recent or most important thing.
- One current document, readable in two minutes. Default sections: **What this is**, **Where it stands**, **Decisions**, **Don't repeat**, **Open**. Restructure when they stop fitting the work.
- Each write updates it in place: replace Where it stands, change or remove what moved, merge repeats. Older history moves down or goes. Never add a section per event.
- State facts plainly. Put anything unconfirmed under Open, once.
- Add a caveat only if it changes what someone should do.

## Entity notes

    probe notes checkout --run <slug>    # prints the path and how full the note is
    <edit the file>
    probe notes push --run <slug>        # merges with others' edits

- A conflict writes markers and exits 2. Resolve them and push again.
- One small change without a checkout: `probe notes edit --<entity> <slug> --old "..." --new "..."`. `probe notes append --<entity> <slug> --text "..."` adds at the end.
- Sub-notes: `--note "<title>"` on checkout, push and show; `probe notes create|rename|delete`. Titles can repeat, so `list` first and use `--note id:<uuid>`.

## Team note

    ~/.local/state/probe/team-note/probe-team-note.md   ($XDG_STATE_HOME if set)
    probe notes sync

- One file per machine, shared by every agent on it. No sub-notes. Never `sed -i` it, and never edit its copy inside `CLAUDE.md` or `AGENTS.md`.
- It is a CLAUDE.md for the whole team: conventions, where things live, decisions, what not to repeat.
- Over its budget it reaches nobody. Write in it the moment you learn something.

## Fixing what's there

- Evidence contradicts a note: correct it and tell the researcher what changed. Their word is final.
- Delete `> **SUPERSEDED**` regions, expired instructions, and anything newer work made stale. Keep a decision that was made and then reversed, with why.
- If the audit says deletion is disabled (`PROBE_NOTES_AUDIT_HORIZON_DAYS=0`), correct in place and delete nothing.

## When it's full

A push over the cap is refused; `probe notes status` lists the fullest notes. Move a small note up to its project or experiment (push there first, then delete here), or merge repeats, swap copies for pointers, and summarize.