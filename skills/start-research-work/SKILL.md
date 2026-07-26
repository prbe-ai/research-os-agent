---
name: start-research-work
description: Start tracked research — pick or create the project and experiment, then open the run. Use before launching any training, evaluation, sweep, docking, scoring, or simulation. Not for ordinary edits, installs, or unit tests.
---

# Start research work

`probe run start` creates the project, the experiment and the run in one call, so
this is the only moment those identities get decided. What follows is the judgement
that is expensive to reverse afterwards.

1. **Orient first.** `browse_research` if you do not know what is in this project;
   `search_knowledge` if you have terms and want prior work on this specific thing.
   Check what is already RUNNING before you launch anything — `browse_research`
   reports `active_run_count`, and duplicate GPU-hours are the expensive mistake.

2. **Reuse the active run** when its intent matches. Otherwise open one:

   ```
   probe run start --project SLUG --experiment SLUG --hypothesis "..." --external-id ID
   ```

   Four things in that line fail silently:
   - Project and experiment are get-or-create **by slug**. A typo does not error, it
     mints a second identity. Read back what you got before trusting it.
   - `--project` takes a slug. Passing an id creates a project named after the UUID.
     Omit it entirely to use the ambient one from `probe project use`.
   - `--hypothesis` is required knowledge for a NEW experiment. Omit it and the
     experiment is minted with a marked `[auto]` placeholder that later runs never
     overwrite. Replace it with `probe experiment set EXP --hypothesis "..."`.
   - `--external-id` should be deterministic. It is what makes a retried launch reuse
     the run instead of duplicating it.

3. **Reuse before you create.** Before writing or materially changing a reusable
   script, scoring method, dataset, config, image, checkpoint or container
   definition, call `get_entity(ref="asset:<name>", view="versions")`. The two empty
   answers mean opposite things:
   - **the name does not exist** → an error, like any bad ref. A new identity is
     licensed.
   - **`state="no_match"`** → the asset EXISTS and no version satisfies your
     `filters={"requirement": ">=N"}`. The response carries the versions that DO
     exist, so this is a real version ceiling, not an absent asset. Pin a new version
     of the SAME asset; do not open a second identity.

   Confusing those is how duplicate asset identities get created, the single most
   expensive avoidable error here: two scorers with the same intent and different
   behaviour make every result that used either one unreproducible. Requirements
   match monotonic integers and labels (`>=3`, `v1.4-final`), not semver ranges.
   Never edit a published version in place.

4. **Snapshot before launch.** `probe snapshot RUN_ID` captures code + env and pins
   `env_ref`. A run with no execution record cannot be reproduced and cannot be
   published. Record W&B, scheduler, pod, image and storage ids with `probe link` as
   they appear; they land on the run's `foreign_keys`.

Recording what the run does, and closing it, is `track-research-work`. Command syntax
for artifacts, assets, publishing and project admin is in that skill's `reference.md`.
