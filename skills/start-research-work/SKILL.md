---
name: start-research-work
description: Start tracked research — create the project and experiment if new, then open a run in them. Use before any training, evaluation, sweep, docking, scoring, or simulation. Not for edits, installs, or unit tests.
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

2. **Create the project and experiment.** Always explicit from the CLI.

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

4. **Reuse before you create.** Before writing or materially changing a reusable
   script, scoring method, dataset, config, image, checkpoint or container
   definition, run `get_entity(ref="asset:<name>", view="versions")`. Its tool
   description spells out what the two empty answers mean — they are opposites, and
   confusing them is how duplicate asset identities get created, the single most
   expensive avoidable error here: two scorers with the same intent and different
   behaviour make every result that used either one unreproducible.

5. **Snapshot before launch.** `run.snapshot()` / `probe snapshot RUN_ID` captures
   code + env and pins `env_ref`. A run with no execution record cannot be reproduced
   and cannot be published. Record W&B, scheduler, pod, image and storage ids with
   `run.link(...)` / `probe link` as they appear; they land on `foreign_keys`.

Recording what the run does, and closing it, is `track-research-work`. Command syntax
for artifacts, assets, publishing and project admin is in that skill's `reference.md`.
