---
name: start-research-work
description: Start tracked research — pick or create the project and experiment, then open the run. Use before any training, evaluation, sweep, docking, scoring, or simulation. Not for edits, installs, or unit tests.
---

# Start research work

Opening a run get-or-creates the project and the experiment in the same call, so this
is the only moment those identities get decided — and the surface you open it with
decides what the run can ever record. Both are expensive to reverse afterwards.

1. **Orient first.** `browse_research` if you do not know what is in this project;
   `search_knowledge` if you have terms and want prior work on this specific thing.
   Check what is already RUNNING before you launch anything — `browse_research`
   reports `active_run_count`, and duplicate GPU-hours are the expensive mistake.

2. **Reuse the active run** when its intent matches. Otherwise open one, with the
   surface that fits where the code will execute.

   **Writing or editing the training script → the SDK, in-process.** This is the
   `wandb.init` / `wandb.log` shape, and it is the default whenever the script is
   yours to touch:

   ```python
   import probe

   client = probe.Client()          # token from env / `probe login`
   run = client.run(experiment="dockq-sweep", project="folding",
                    hypothesis="temp 0.7 beats 1.0", external_id="rp-9931")
   run.snapshot()                                    # code + env, pins env_ref
   for step, batch in enumerate(loader):
       run.log({"loss": loss, "reward": reward}, step=step)      # the curve
   run.finish()                     # or `with run:` — closes even on an exception
   ```

   The handle lives in the training process, so it heartbeats on its own (60s,
   `PROBE_HEARTBEAT_SECONDS` tunes it), it can see every step, and `finish()` flushes
   whatever spooled. Writes are fail-open: a network blip spools to disk rather than
   raising inside the loop.

   **In an agent shell, wrapping a script you are not editing → the CLI.**

   ```
   probe run start --project SLUG --experiment SLUG --hypothesis "..." --external-id ID
   ```

   A CLI-opened run is DETACHED — this process exits at once, so the run does not
   heartbeat and stays open until `probe run end`. You can `probe log` before, after
   and around the script, but never from inside its loop.

   **Step-level curves require the SDK**, so if the work needs one, make the script
   editable. When it genuinely is not, a small Python wrapper calling
   `run.execute([...])` still gets you the run, the snapshot, a process span and the
   real exit status.

3. **Four identity arguments fail silently, on either surface:**
   - Project and experiment are get-or-create **by slug**. A typo does not error, it
     mints a second identity. Read back what you got before trusting it.
   - The project takes a slug. Passing an id creates a project named after the UUID.
     Omit it entirely to use the ambient one from `probe project use`.
   - The hypothesis is required knowledge for a NEW experiment. Omit it and the
     experiment is minted with a marked `[auto]` placeholder that later runs never
     overwrite. Replace it with `probe experiment set EXP --hypothesis "..."`.
   - The external id should be deterministic. It is what makes a retried launch reuse
     the run instead of duplicating it.

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
