---
name: start-research-work
description: Start tracked research and keep its notes — create the project and experiment before the work, not after it, and open a run when code is about to execute. Use for anything someone might need to reproduce, including DESIGNING it before anything runs — training, evaluation, sweeps, ablations, data curation, docking. Re-enter it all session, not once at setup — when you choose an approach or reject one, when the user overrides you, when a tool or dataset behaves differently than documented, and before context is compacted or the session ends. A plan-first or approval-gated brief does not defer this — registering entities is not the gated action. Trigger for one-off and exploratory work, when writing a script that will run, and when the user did not ask for tracking. Not for dependency installs, unit tests, or reading that decided nothing.
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

1. **Orient first.** `browse_research` if you do not know what is in this project;
   `search_knowledge` if you have terms and want prior work on this specific thing.
   Check what is already RUNNING before you launch anything — `browse_research`
   reports `active_run_count`, and duplicate GPU-hours are the expensive mistake.

   Read the project's notes — `probe notes show`, or the `notes` on its MCP `card`. It is free-text markdown the last session left for you: what was tried,
   what was ruled out, what not to repeat. Add to it as you learn things
   (`probe notes write --append`); see `track-research-work`.

   Those reads already carry the whole arc — draw it with `show-research-timeline`
   before the first launch rather than describing it. What comes AFTER the run is
   the part a researcher at a terminal cannot see, and it is what decides whether
   the snapshot gets taken while it still can be.

2. **Create the project and experiment FIRST — before the scaffold, not after it.**
   Always explicit from the CLI.

   ```
   probe project create folding \
       --description "Antibody-antigen structure prediction; DockQ on the SAbDab split."
   probe experiment create dockq-sweep --project folding \
       --hypothesis "temp 0.7 beats 1.0" \
       --description "Sampling-temperature sweep over the 1.2B checkpoint, DockQ on 200 held-out complexes."
   ```

   `client.create_project(...)` / `client.create_experiment(...)` from the SDK. Both
   raise if the slug is taken, so re-running a setup script is a loud no-op rather
   than a silent second identity.

   **Always pass `--description`: what the thing is.** Length is not the point —
   a few words beats a blank field, and three sentences is the ceiling rather than
   the target. It is the first thing anyone sees under the title, and a container
   without one reads as "Add description" to every visitor. The hypothesis says
   what you expect; the description says what the work IS — the model, the data,
   the metric.

   Nothing reliably fills it in later. The server generates a description only
   when a child RUN reaches a terminal status, so anything that ends without one
   — an abandoned run, a project used purely to hold artifacts, an import — stays
   blank permanently.

   Missed one, or inherited a blank container? Add it after the fact. The verb
   differs per kind, and there is no `project set`:

   ```
   probe project patch <project> --description "..."
   probe experiment set <experiment> --description "..."
   probe run set <run> --description "..."
   ```

   Work with no hypothesis does not need an experiment at all: open a
   PROJECT-DIRECT run (`probe run start --project folding`, or
   `client.run(project="folding")`). That is a better home for it than an
   experiment named after whatever directory you happened to be in.

   **The hypothesis is required to CREATE an experiment and is never synthesised.**
   That is the moment you know what you are testing, and nothing goes back to fill it
   in later — an omitted one used to become a permanent `[auto]` placeholder. An
   experiment that already exists keeps its own (first-write-wins), so reopening one
   never rewrites it; `probe experiment set EXP --hypothesis "..."` amends it.

   **Do this first.** Tracking named in a brief is a standing requirement, not a phase
   after setup. The notes hang off a project, so until one exists there is nowhere
   to write what setup itself decides. `run_count: 0` is the correct state for a
   project whose first run has not started, not a reason to defer creating it.

   This skill is RE-ENTERED, not run once. "Start" names the first call, not the only
   one: append to the notes when you choose an approach or reject one, when the USER
   overrides you — the highest-value entry and the one nobody thinks to write — when a
   tool or dataset behaves differently than documented, and before context is compacted
   or the session ends, which is the last moment the reasoning still exists.

   Session capture does not cover this. The transcript tap ships the raw conversation;
   the notes are the skimmable version someone can read without replaying a session.

3. **Reuse the active run** when its intent matches. Otherwise open one, with the
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
   run.log({"accuracy": acc}, step=None, agg="mean")             # the headline: ONE series
   run.log_artifact("predictions", path="preds.jsonl")           # per-example detail
   run.finish()                     # or `with run:` — closes even on an exception
   ```

   Those three lines are the three shapes, and the split between them is a
   decision you make BEFORE writing the call, not a formatting detail: dimensions
   are low-cardinality grouping axes, per-example identity belongs in the
   artifact, and only `step=` produces a curve. Putting an `example_id` in
   dimensions mints one single-point series per example and turns the run page
   into a wall of scalar tiles, silently. `track-research-work` step 1 has the
   shape table and the assertion that catches it.

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

4. **Reuse before you create.** Before writing or materially changing a reusable
   script, scoring method, dataset, config, image, checkpoint or container
   definition, run `get_entity(ref="artifact:<name>", view="versions")` — artifacts
   resolve by name at the shared, lab-wide level, which is where an official one
   lives. Its tool description spells out what the two empty answers mean — they are
   opposites, and confusing them is how duplicate identities get created, the single
   most expensive avoidable error here: two scorers with the same intent and
   different behaviour make every result that used either one unreproducible.

5. **Snapshot before launch.** `run.snapshot()` / `probe snapshot RUN_ID` captures
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

6. **Describe it and tag it.** `--description` takes free text on `run start` too,
   not just the two containers — one line on what THIS run changes relative to the
   last one ("same config, lr 3e-4 instead of 1e-4") is what makes a run list
   readable six weeks later, when every run is named after a timestamp and a
   petname. Amend later with `probe run set RUN --description "..."`.

   Repeatable `--tag` on `run start` / `experiment create` /
   `project create` (SDK `tags=[...]`). Use 1-3 lowercase-kebab tags; prefer
   `baseline`, `ablation`, `sweep`, `debug`, `smoke-test`, `prod-candidate` —
   free-form allowed. If a run's meaning changes later:
   `probe run tag RUN_ID flaky --remove prod-candidate`. Filter with
   `--tag` on `run list` / `experiment list` / `project list`.

Recording what the run does, and closing it, is `track-research-work`. Command syntax
for artifacts, assets, publishing and project admin is in that skill's `reference.md`.
