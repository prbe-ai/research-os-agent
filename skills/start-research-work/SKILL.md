---
name: start-research-work
description: Start tracked research and keep its notebook — create project and experiment if new, record the decisions and findings that shape the work, and open a run when code is about to execute. Use for any research work that produces a result someone might reproduce — training, evaluation, sweeps, ablations, data curation, docking, and the like. Also use BEFORE any run exists — when choosing an approach, rejecting one, or hitting a reproducible finding about the tools, data or environment — and when a project, experiment or run is created, or when writing a script that will run. Re-enter it throughout a session, not once at setup. Trigger for one-off and exploratory work, and when the user did not ask for tracking. Not for installs, unit tests, or routine file reading that decided nothing.
---

# Start research work

Three identities: project, experiment, run. From the CLI, creation is always its own
explicit step — `probe run start` opens runs and never creates. The expensive failure
here is a typo'd slug minting a second identity: two experiments where you thought you
had one, and every later comparison silently reading them as different things. On the
CLI the slug is hand-typed on every invocation, which is exactly where that comes from.

(The SDK does have a create-on-demand path —
`client.run(project=..., experiment=..., hypothesis=...)`
— because there the slug is written once in a file and code-reviewed. It refuses a
near-miss of an existing slug rather than warning.)

This skill is the lab notebook, and it is RE-ENTERED, not run once. "Start" names the
first call, not the only one — the project is opened here, and every decision, finding
and reversal after it is filed here too, for as long as the work continues. A session
that came here once at setup and never returned has recorded the least interesting
moment of the work.

1. **Orient first.** `browse_research` if you do not know what is in this project;
   `search_knowledge` if you have terms and want prior work on this specific thing.
   Check what is already RUNNING before you launch anything — `browse_research`
   reports `active_run_count`, and duplicate GPU-hours are the expensive mistake.

2. **Create the project and experiment FIRST — before the scaffold, not after it.**
   Always explicit from the CLI.

   ```
   probe project create folding
   probe experiment create dockq-sweep --project folding --hypothesis "temp 0.7 beats 1.0"
   ```

   `client.create_project(...)` / `client.create_experiment(...)` from the SDK. Both
   raise if the slug is taken, so re-running a setup script is a loud no-op rather
   than a silent second identity.

   Work with no hypothesis does not need an experiment at all: open a
   PROJECT-DIRECT run (`probe run start --project folding`, or
   `client.run(project="folding")`). That is a better home for it than an
   experiment named after whatever directory you happened to be in.

   **The hypothesis is required to CREATE an experiment and is never synthesised.**
   That is the moment you know what you are testing, and nothing goes back to fill it
   in later — an omitted one used to become a permanent `[auto]` placeholder. An
   experiment that already exists keeps its own (first-write-wins), so reopening one
   never rewrites it; `probe experiment set EXP --hypothesis "..."` amends it.

   **Tracking named in a brief is a standing requirement, not a phase to schedule
   after setup.** Create the identities at the moment the work is named — before the
   repo, the deps, the config. Waiting until there is something to LOG gets the order
   backwards: notes anchor here (step 3), so nothing setup itself decides has anywhere
   to go until this exists, and by then those choices have been made and forgotten.
   Empty is the correct state for a project whose first run has not started;
   `run_count: 0` is not a reason to defer creating it.

3. **Record decisions and findings as they happen — do not wait for a run.** Planning,
   investigation and environment archaeology all happen before the first run, and that
   is where the most irrecoverable context is generated. A note anchors to the ACTIVE
   project, so it has somewhere to go from the moment the project exists:

   ```
   probe project use folding
   probe note add --kind decision --statement "GKE, not DOKS — Harbor has no generic-K8s backend" \
                  --evidence docs/findings.md#5
   probe note list                     # reads it back, supersession resolved
   ```

   `client.notes.add(project_id, "decision", "…", anchor="project")` from the SDK;
   `--experiment SLUG` or a RUN argument anchors it further down when that is the
   right scope. Kinds: `intent`, `hypothesis`, `decision`, `observation`, `failure`,
   `result`, `deviation`, `next_step`.

   **The trigger is a decision or a finding, not a command.** File one when you choose
   an approach or reject one; when you reverse something the brief assumed; when a
   tool, dataset or environment behaves reproducibly differently than documented; when
   you learn something the next session would otherwise re-derive. Do NOT file one for
   reading a file, listing a directory, or a step that decided nothing — a journal
   nobody can skim is the same as no journal.

   **Reversing an earlier decision does not overwrite it.** Pass
   `--supersedes <note_id>` and the old one is withheld on read, carrying
   `superseded_by`, instead of sitting beside its replacement as a contradiction.
   Filing after the fact is normal and honest: `--authority` defaults to
   `agent_summarized`, and `--confidence` is there for a claim you are unsure of.

   Read it back with `probe note list` or
   `get_entity(ref="project:<id>", view="notes")` — that view is the answer to "why
   this and not what the brief said", and nothing in the metrics can reconstruct it.

4. **Reuse the active run** when its intent matches. Otherwise open one, with the
   surface that fits where the code will execute.

   **Writing or editing the training script → the SDK, in-process.** This is the
   `wandb.init` / `wandb.log` shape, and it is the default whenever the script is
   yours to touch:

   ```python
   import probe

   client = probe.Client()          # token from env / `probe login`
   run = client.run(experiment="dockq-sweep", project="folding",
                    external_id="rp-9931")     # hypothesis= here would CREATE it
   run.snapshot()                                    # code + env, pins env_ref
   for step, batch in enumerate(loader):
       run.log({"loss": loss, "reward": reward}, step=step)      # the curve
   run.finish()                     # or `with run:` — closes even on an exception
   ```

   `import probe` needs `probe-research` in the script's own environment
   (`uv add probe-research` / `pip install probe-research`) — the wizard's CLI
   install is isolated and not importable from your venv, and `probe`/`probe-agent`
   on PyPI are unrelated packages.

   The handle lives in the training process, so it heartbeats on its own (60s,
   `PROBE_HEARTBEAT_SECONDS` tunes it), it can see every step, and `finish()` flushes
   whatever spooled. Writes are fail-open: a network blip spools to disk rather than
   raising inside the loop.

   **In an agent shell, wrapping a script you are not editing → the CLI.**

   ```
   probe run start --project SLUG --experiment SLUG --external-id ID
   ```

   A CLI-opened run is DETACHED — this process exits at once, so the run does not
   heartbeat and stays open until `probe run end`. You can `probe log` before, after
   and around the script, but never from inside its loop.

   **Step-level curves require the SDK**, so if the work needs one, make the script
   editable. When it genuinely is not, a small Python wrapper calling
   `run.execute([...])` still gets you the run, the snapshot, a process span and the
   real exit status.

   Two arguments here still need judgement. `--external-id` should be DETERMINISTIC:
   it is what makes a retried launch reuse its run instead of duplicating it, the one
   reuse that survives, and it is opt-in because you name the id. `--project` no
   longer decides where anything is filed — it now CHECKS that the experiment is in
   the project you think it is, and errors if not. Omit it to use the ambient one
   from `probe project use`.

5. **Reuse before you create.** Before writing or materially changing a reusable
   script, scoring method, dataset, config, image, checkpoint or container
   definition, run `get_entity(ref="artifact:<name>", view="versions")` — artifacts
   resolve by name at the shared, lab-wide level, which is where an official one
   lives. Its tool description spells out what the two empty answers mean — they are
   opposites, and confusing them is how duplicate identities get created, the single
   most expensive avoidable error here: two scorers with the same intent and
   different behaviour make every result that used either one unreproducible.

6. **Snapshot before launch.** `run.snapshot()` / `probe snapshot RUN_ID` captures
   code + env and pins `env_ref`. A run with no execution record cannot be reproduced
   and cannot be published. Record W&B, scheduler, pod, image and storage ids with
   `run.link(...)` / `probe link` as they appear; they land on `foreign_keys`.

   The two spellings read the environment differently, because they have to.
   `run.snapshot()` runs INSIDE the training venv and records that interpreter.
   `probe snapshot` is a separate process — normally a uv-tool install with its
   own packages — so it detects the project's venv (`.venv`/`venv`/`env` up to the
   git root, then `VIRTUAL_ENV`, then `CONDA_PREFIX`). If the venv lives somewhere
   else, pass `--venv PATH`; the command fails loudly rather than recording the
   CLI's own packages as the project's.

7. **Tag it.** Repeatable `--tag` on `run start` / `experiment create` /
   `project create` (SDK `tags=[...]`). Use 1-3 lowercase-kebab tags; prefer
   `baseline`, `ablation`, `sweep`, `debug`, `smoke-test`, `prod-candidate` —
   free-form allowed. If a run's meaning changes later:
   `probe run tag RUN_ID flaky --remove prod-candidate`. Filter with
   `--tag` on `run list` / `experiment list` / `project list`.

Recording what the run does, and closing it, is `track-research-work`. Command syntax
for artifacts, assets, publishing and project admin is in that skill's `reference.md`.
