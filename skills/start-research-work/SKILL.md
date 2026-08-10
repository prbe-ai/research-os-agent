---
name: start-research-work
description: Start tracked research and keep its notes — create the project and experiment before the work, not after it, and open a run when code is about to execute. Use for reproducible research from design through training, evaluation, sweeps, ablations, data curation and docking. Re-enter it all session, not once at setup — when you choose or reject an approach, the user overrides you, a tool or dataset differs from its documentation, an assumption encoded in code proves false, planned hardware is unavailable and you substitute something else, or before context is compacted or the session ends. A plan-first or approval-gated brief does not defer this — registering entities is not the gated action. Trigger for one-off and exploratory work, when writing a script that will run, while provisioning or failing to provision its machines, and when the user did not ask for tracking. Not for dependency installs, unit tests, or reading that decided nothing.
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
   probe project create antibody-folding \
       --description "Improve antibody structure predictions for the biologics program. This work supports the next model-selection milestone."
   probe experiment create lower-sampling-temperature --project antibody-folding \
       --hypothesis "A lower sampling temperature will improve structure accuracy on held-out antibody complexes." \
       --description "Compare two sampling settings for the current antibody-folding model before the next model-selection decision."
   ```

   `client.create_project(...)` / `client.create_experiment(...)` from the SDK. Both
   raise if the slug is taken, so re-running a setup script is a loud no-op rather
   than a silent second identity.

   **Always pass `--description`: what the thing is.** It is the first thing anyone
   sees under the title, and a container without one reads as "Add description" to
   every visitor. The hypothesis says what you expect; the description says what the
   work is and why it matters.

   **Write dashboard text for a teammate, not for the execution log.** A teammate
   familiar with the company and research area should understand the title,
   description and hypothesis without knowing this run's implementation details:

   - Make the name a short, human-readable label: usually 2–6 familiar words. Use a
     hyphenated form when the CLI needs a ref. Do not use a command, ticket number,
     timestamp, generated petname or pile of parameters as the meaning-bearing name.
   - Keep a description to 1–2 short sentences, normally no more than 40 words.
     State what the work is, why it exists and which broader effort or decision it
     supports. Do not narrate the setup.
   - Write the hypothesis as one plain, testable sentence, normally no more than 30
     words. Name the expected outcome and the change or comparison expected to cause
     it. Preserve uncertainty; do not present an expectation as a result.
   - Prefer familiar language. Keep a technical term only when removing it would
     change the meaning, and explain an acronym that the intended reader may not
     know. Put exact checkpoint names, paths, commands, infrastructure identifiers,
     issue IDs and parameter lists in notes, tags, links, config or metadata instead.
   - When the context is known, ground the wording in the company's real product,
     customer, internal project, initiative, milestone, decision or timeline. Use
     only context supplied by the user or already present in Probe Research. Never
     invent company context, expand an unknown codename, or guess a deadline.
   - Make necessary internal references understandable in place. Prefer "the August
     model-selection milestone" to an unexplained codename; if the codename is the
     clearest known label, pair it with its purpose rather than dropping it.

   This is display copy, not a substitute for technical evidence. Keep the precise
   model, dataset, metric, parameter and environment details in the structured run
   record so simplifying the dashboard does not make the work irreproducible.

   Nothing reliably fills it in later. The server generates a description only
   when a child RUN reaches a terminal status, so anything that ends without one
   — an abandoned run, a project used purely to hold artifacts, an import — stays
   blank permanently.

   Missed one, or inherited a blank container? Add it after the fact. The verb
   is `set` for every kind:

   ```
   probe project set <project> --description "..."
   probe experiment set <experiment> --description "..."
   probe run set <run> --description "..."
   ```

   A project also has visible, durable Markdown below its live AI summary. Put
   context that should remain stable across summary refreshes in a file and write
   it naturally with `probe project set <project> --summary @PROJECT.md`. This is
   separate from hidden `probe notes`, which remains the agent briefing.

   Work with no hypothesis does not need an experiment at all: open a
   PROJECT-DIRECT run (`probe run start --project folding`, or
   `client.run(project="folding")`). That is a better home for it than an
   experiment named after whatever directory you happened to be in.

   **Provisioning infrastructure is such a run — open one.** An attempt to get
   hardware has a start, an end, a real terminal status and an environment, so it is
   an execution, not a footnote. A quota denial, a stockout, a node that came up and
   was released, a cluster you verified over SSH and then stopped: each is one
   project-direct run, tagged `infra`, closed with the status it actually reached.

   ```
   probe run start --project swe-smith-shakedown --tag infra \
       --name pair-training-capacity \
       --description "Request the accelerator capacity needed for the pair-training phase so the August training milestone can begin on schedule."
   probe link  $RUN --set gcp_zone=us-central1-a --set gcp_machine_type=a3-highgpu-8g
   probe run set $RUN --notes "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS. Same in -b and -c."
   probe run end $RUN --status failed
   ```

   This is the evidence base for "why did this train on A100s?", and without it that
   answer exists only in a chat transcript. Two things make it reachable later:
   `probe link` puts the machine identity on `foreign_keys` (zone, machine type, MIG
   name, cluster id) where a reader can match it against the training run; and a
   `provisioned_by` key on the training run points back at the attempt that explains
   its shape — `probe link $TRAIN --set provisioned_by=$INFRA_RUN`. The closed lineage
   vocabulary (`consumes`/`produces`/`evaluates_on`/`forked_from`/`resumed_from`/
   `promoted_to`/`derived_from`) has no relation meaning "provisioned by", so do not
   force one onto `probe edge`; `foreign_keys` is an open dict and is the right door.

   Write the CONCLUSION in the project's notes, not only in the runs. "Every
   us-central1 zone was out of H100s for the whole afternoon, so this trains on a
   single A100 80GB" is one line that an arriving agent reads off the project card
   without knowing any of those runs exist. The runs are the evidence; the notes are
   what gets read.

   A campaign of attempts that belong together — three zones, then a fallback, then a
   queued MIG resize — CANNOT share a run group: groups are experiment-anchored, and
   the backend 422s a `group_id` on a project-direct run. Give them a shared
   `foreign_keys` key instead (`probe link $RUN --set campaign=h100-hunt`) and one
   paragraph in the project's notes. Run groups, with their own `--notes`, are for
   sweeps and ensembles that live under an experiment.

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

   **The trigger that fires most often and gets logged least: an assumption you
   already encoded in code turns out to be false.** Not a decision, not a tool
   misbehaving — a fact about the data or the environment that invalidates something
   you have already written. It arrives mid-implementation, hours before any run
   exists, and it goes into a commit message and a docstring where nobody looking at
   the research will ever find it. These are all one shape:

   - the dataset field you built around means the opposite of its name (SWE-smith's
     `patch` is the BUG, not the fix — the images ship clean)
   - the metric you chose cannot be computed the way you assumed (scoring by pytest
     exit code hides that 40 of 113 tests were uncollectable)
   - the interpreter/toolchain is not where it is on your machine (pytest lives in
     the `testbed` conda env, not on PATH)
   - a slice of the corpus silently cannot be evaluated at all (19% of SWE-smith
     instances have empty problem statements, so an agent gets no prompt and scores
     0.0; and the set is multi-language, so a pytest verifier scores Go and JS 0.0)
   - a resource limit destroys the evidence rather than erroring (Docker Desktop at
     1 GB SIGKILLs a 113-test run and leaves an EMPTY log)

   Every one of those changes what a later reader concludes from the numbers, and
   none of them needs a run open to be worth recording. Write them to the project's
   notes when you find them. If you only realise at the end of the session, write
   them then — late and honest beats absent.

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
   print(run.url)                   # the dashboard link, in the job's own log
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
   reuse that survives, and it is opt-in because you name the id. `--project` on
   `run start` no longer decides where anything is filed — it CHECKS that the
   experiment is in the project you think it is, and errors if not.

   **Name the project on every write. `probe project use` is MACHINE-global.** It
   writes one `current_context` to `~/.config/probe/config.json` — not per shell, not
   per directory, not per process — and every later command that omits `--project`
   reads it. Two agents on one machine share it, so a `use` in this session silently
   retargets the other session's next create. That is not hypothetical: it moved three
   experiments into the wrong project mid-shakedown, and an experiment cannot be moved
   back — there is no `experiment move`, and `experiment set` takes only
   `--hypothesis`/`--name`/`--description`. The guard that catches this on `run start`
   was never added to `experiment create`, where `--project` still silently decides.

   Pass `--project` explicitly, or export `PROBE_PROJECT` once at the top of the
   session — it resolves per PROCESS, so it cannot leak sideways:

   ```
   export PROBE_PROJECT=swe-smith-shakedown     # per-process; safe with concurrent sessions
   probe project use ...                        # machine-global; avoid unless you are alone
   ```

   `probe project use` reports `active project: <slug>`, which reads as *this shell*
   and means *this machine, every process, until someone changes it*. `probe context
   use` is not an escape hatch either — named contexts share the same global selector.

4. **Reuse before you create.** Before writing or materially changing a reusable
   script, scoring method, dataset, config, image, checkpoint or container
   definition, run `get_entity(ref="artifact:<name>", view="versions")` — artifacts
   resolve by name at the shared, lab-wide level, which is where an official one
   lives. Its tool description spells out what the two empty answers mean — they are
   opposites, and confusing them is how duplicate identities get created, the single
   most expensive avoidable error here: two scorers with the same intent and
   different behaviour make every result that used either one unreproducible.

5. **Verify capture; snapshot only when you launched outside the tools.** Capture is
   now automatic: `probe exec` and the SDK `run()` snapshot code + env and pin
   `env_ref` for you, and also record the launch context (argv, seeds, env allowlist,
   container). So this is a *verify* step, not an action — run `probe run check RUN`
   and read the verdict: `unverified` (nothing obviously absent — the default, and NOT
   a promise it rebuilds), `incomplete` (a real gap — exit code 2), or `complete`
   (only under `--verify`, which resolves the recorded commit against its remote).
   Record W&B, scheduler, pod, image and storage ids with `run.link(...)` / `probe
   link` as they appear; they land on `foreign_keys` and stay mandatory — the tools
   cannot know them.

   Snapshot **explicitly** only when code executes OUTSIDE `probe exec`/`run()` (a
   bare `sbatch`, a notebook, a container you did not launch through the SDK):
   `run.snapshot()` / `probe snapshot RUN_ID`. The two spellings read the environment
   differently, because they have to. `run.snapshot()` runs INSIDE the training venv
   and records that interpreter. `probe snapshot` is a separate process — normally a
   uv-tool install with its own packages — so it detects the project's venv
   (`.venv`/`venv`/`env` up to the git root, then `VIRTUAL_ENV`, then `CONDA_PREFIX`).
   If the venv lives somewhere else, pass `--venv PATH`; the command fails loudly
   rather than recording the CLI's own packages as the project's.

6. **Describe it and tag it.** `--description` takes free text on `run start` too,
   not just the two containers. Use the same dashboard-language rules above: one
   plain line on this run's purpose or meaningful comparison ("Test whether a higher
   learning rate reaches the quality target sooner") makes the run list readable six
   weeks later. Put exact values in config or metadata, and amend display copy later
   with `probe run set RUN --description "..."`.

   Repeatable `--tag` on `run start` / `experiment create` /
   `project create` (SDK `tags=[...]`). Use 1-3 lowercase-kebab tags; prefer
   `baseline`, `ablation`, `sweep`, `debug`, `smoke-test`, `prod-candidate`,
   `infra` (a provisioning attempt, above) and `invalid` (the harness was broken,
   so the number measures nothing — see `track-research-work` step 6) — free-form
   allowed. If a run's meaning changes later:
   `probe run tag RUN_ID flaky --remove prod-candidate`. Filter with
   `--tag` on `run list` / `experiment list` / `project list`.

   `smoke-test` says what a run was FOR, not whether to believe it. A shakedown
   leaves both kinds under that one tag — the ones that proved ingestion works and
   the ones that scored 0.0 because the verifier was wrong — and a reader cannot
   tell them apart. That is what `invalid` is for.

7. **Hand back the link.** Every project, experiment and run you create gets its
   dashboard URL in your reply — nobody can open a uuid. `project create`,
   `experiment create` and `run start` print it to stderr; MCP entities carry it as
   `url`. Copy that string — an assembled one 404s with no sign it was invented.
   No `url`, no link.

Recording what the run does, and closing it, is `track-research-work`. Command syntax
for artifacts, assets, publishing and project admin is in that skill's `reference.md`.
