# Changelog

## Unreleased

### Fixed

- **A ref that is both a project id and a project slug no longer silently resolves to
  one of them.** `_project_id` parsed the ref as a UUID and, when that worked, returned
  it as an id without ever asking whether a *slug* matched too. A project whose slug was
  UUID-shaped was therefore unreachable by slug — and worse, naming it addressed
  whichever project owned that UUID as its id. Observed 2026-08-04 with two live
  projects, where slug `6fa49e87-…` belonged to one and id `6fa49e87-…` to another:
  `probe project delete 6fa49e87-…`, meaning the first, would have permanently deleted
  the second. Exit 0, a `deleted` line naming the ref, and nothing to restore.

  Both spellings still resolve. Only the genuine collision is refused, and it names both
  candidates so the operator can pick with `--by-id` / `--by-slug` rather than being told
  "ambiguous" and left to guess. `--yes` does not skip the check: there is no answer to
  "are you sure" that says *which* project was meant, and scripts pass `--yes` by default.

  Reachable from 8 call sites including `project get / use / patch / tag / move / delete`.
  The inverse resolver (`_project_slug`) had the bug mirrored, and the two anchor
  resolvers — `_anchor_id_for` (artifact uploads) and the backfill's `_resolve_ref` —
  had it in a quieter form, where the cost is an import filed into a stranger's project
  instead of a deletion. All four now share `probe.cli.refs`.

- **Slug lookups no longer stop at 200 rows.** Resolution scanned
  `list_projects(limit=200)`, so a slug on project 201+ raised `no project with id or
  slug X` — a false absence indistinguishable from a real one, and one that gets acted
  on by creating a duplicate. It is a server-side `?slug=` on a UNIQUE column now: 0 or
  1 row, no paging, no cap.

### Changed

- **Every `delete` verb takes the same ref forms and prompts the same way.** They had
  drifted: `project delete` took an id or a slug, `experiment delete` took ids only (a
  slug 422'd against the UUID-typed route), and `run delete` took ids only even though
  `run get` had accepted a petname `short_id` all along. Learning the habit from one verb
  and using it on the next got you a 422 at best. All four now route through one path
  that resolves, confirms, then deletes by canonical id:

  | verb | accepts |
  |---|---|
  | `project delete` | id or slug (`--by-id`/`--by-slug` when both) |
  | `experiment delete` | id or slug (`--by-id`/`--by-slug` when both) |
  | `run delete` | id or petname `short_id` — no disambiguator needed, a petname cannot be UUID-shaped |
  | `artifact delete` | id only — there is no by-name index and a name is anchor-scoped, so there is no second spelling to accept |

  The confirmation prompt and the `deleted` line now name the **resolved** entity
  (name, handle and id) instead of echoing the string that was typed. Echoing the ref
  asks the operator to confirm their own typo, and in the collision above it is exactly
  the string that does not identify what is about to go. Resolution therefore happens
  *before* the prompt, which is the ordering the confirmation is worth anything under.

- **The Claude Code tap daemon died seconds after every SessionStart**
  (`probe-research-tap` 0.1.3). Transcripts silently stopped reaching
  research-os: sessions showed full artifact and experiment linkage next to
  "No transcript for this session". On one machine, **zero** tap daemons were
  alive against 120 leaked shutdown sentinels, and a live session's daemon had
  exited 34 seconds in while its transcript kept growing for another 35 minutes.

  `session-start.sh` detached the wrapper with `nohup ... & disown`. Neither
  changes the process group: `nohup` only ignores SIGHUP and `disown` only
  clears the shell's job table. So the wrapper inherited the hook's PGID and
  any SIGTERM delivered to that group took the daemon with it. Measured
  directly — wrapper PID 7006, PGID 6958, identical to the spawner's.

  The old comment concluded this was unavoidable because macOS ships no
  `setsid(1)`. That is true of the binary and irrelevant: `python3` exposes
  `os.setsid()`, and the hook already requires python3 to parse its own hook
  payload. The wrapper now launches through a shim that setsids and then execs
  in place, so it is a real session leader (PID == PGID) and nothing outside
  its own group can reach it.

  Also fixed, both found by the new tests rather than by reading:

  - `session-end.sh` used `kill -TERM "-$PID"` unconditionally. A PGID only
    exists because some process with that id led the group, so a non-leader pid
    cannot collide with a live group — but an orphaned group whose leader has
    exited *does* keep its pgid while that pid becomes free, so a stale pid file
    could signal strangers. It now verifies leadership before using the group
    form.
  - Shutdown sentinels leaked forever (`session-end.sh` never deletes one and
    only a later SessionStart *for the same session id* clears it, but session
    ids are UUIDs and never recur). Now pruned after 2 days — with a trailing
    slash on `/tmp/`, because `/tmp` is a symlink on macOS and `find` defaults
    to not following it, so the obvious spelling exits 0 having done nothing.

  The hooks had no test coverage at all, which is why a daemon that died in
  every real session shipped green. `tests/test_hook_spawn.py` drives the actual
  shell scripts and pins session leadership, survival of a spawner-group kill,
  teardown, the stale-pid group-kill guard, and sentinel pruning. Each assertion
  was verified against a deliberately reintroduced bug.

### Added

- **`show-research-timeline` skill** (plugin 0.15.0, released by dispatch). Draws the whole research arc as
  ONE horizontal track in the session — science stages and tracking stages on a
  single line, left to right in the order they have to occur, with the current
  position marked and one next action under the rule. Left to right because the
  reader's question is "how much of this is behind me", which a track answers at a
  glance and a vertical list answers by counting — and the connector answers it
  before any label is read, solid `━` behind the work and light `─` ahead. Drawn on
  a 13-column grid so labels are the real word (`hypothesis`, not `hypoth`); wraps
  to a second block past ~99 columns rather than narrowing cells or eliding stages.

  The gap it closes is the moment before a launch: the command is visible and nothing
  downstream is. Probe already holds every fact — `browse_research` has the run counts,
  `handoff` has `series` / `span_types` / `artifact_total`, `reproduce` reports
  `execution_record` in `missing`, the experiment knows whether it was ever versioned —
  and hands them back one entity at a time. The skill spends those reads once and
  renders the answer.

  Two bands were the obvious shape and are the wrong one. Snapshot-after-launch is a
  missed snapshot, and a layout that puts tracking on its own track hides precisely
  that ordering failure. Marks are evidence-gated: only the derivable stages can
  produce a completion mark, stages inferred from the researcher's brief are drawn but
  never checked off, and `?` (Probe has no signal) is kept distinct from `○` (ahead,
  not started) so nobody reads an unknown as a done.

- **`probe artifact add --notes`** — a real description field on every anchor
  (research-os 0095). Previously there was nowhere to put one: `--meta` is
  run-anchor only and `ScopedUploadRequest` forbids extras, so a project or
  experiment upload could not describe itself at all. Backfill's prompt told
  agents to use `probe note add` (not a command — it is `probe notes write`) or
  `--meta` (rejected), so they improvised and concatenated the description onto
  `--name`. That breaks more than it looks: `name` is the file's relative posix
  path, `path` is GENERATED from its dirname, and the dashboard classifies a
  file from the extension at the end of its name and never sniffs bytes — so a
  described artifact lost its preview, its tree leaf, and its folder.

  **Requires backend 0095.** The upload contract forbids unknown fields, so a
  CLI sending `notes` to an older backend gets a 422 — ship the backend first.

### Fixed

- **Backfill's reconcile never ran.** `probe backfill` finished a byte-perfect
  204-file import and reported "could not read back the project to confirm what
  landed" — the one number the feature exists to print. Three faults stacked:

  - **The summary parser could not match its own output format.** `agent_argv`
    launches both agents with `--output-format stream-json`, so the closing JSON
    summary is a *string inside* a `{"type":"result","result":"..."}` envelope,
    with its quotes escaped. `summary_projects` looked for `projects` at the top
    level of a stdout line, so it matched only when the agent was NOT streaming —
    which is never. It now scans the decoded envelope too (verified against real
    `claude -p` output: the old parser returns `[]`, the new one the slug).
    Previously masked by the pinned-anchor fallback, and exposed when the agent
    was given ownership of project naming.
  - **The count omitted experiment-anchored artifacts.** `count_landed` listed
    the project anchor only, while step 3 of the prompt *tells* the agent to
    attach artifacts to experiments. A faithful 204-file import read back as
    121 — a 40% shortfall that was entirely where the reconcile looked.
  - **Slugs were passed to a route typed for a UUID.** `summary_projects`
    returns slugs; `/v1/projects/{id}/artifacts` 422s on one, and the reconcile
    swallowed it as "could not read back". Slugs are now resolved first.

### Changed

- **The project's notes moved from an artifact to a column** (research-os 0094,
  backend 0.102.0.0). `probe notes show` / `write` are unchanged; what changed is
  underneath, and it fixes what the artifact version got wrong:

  - **Editing replaces instead of accumulating.** Artifact identity is
    `anchor+name+content_hash`, so every edit appended a *new* row — a project's
    artifact list filled with copies of one file. A column is edited in place.
  - **Reading costs nothing.** The notes come back on `GET /v1/projects/{id}`, the
    call `get_entity` already makes to resolve the project, so the excerpt on the
    project card is free. The artifact version paid three round trips (list →
    presign → R2 GET) on the cheapest, most-used read in the tool, and pushed
    ~250 bytes of markdown through the blob store to do it.

  `set_project_notes` **reads back what the server stored** and raises if it differs.
  `ProjectPatch` does not forbid extra fields, so a backend predating 0094 accepts
  `notes`, ignores it, and answers 200 — without the check the write vanishes and the
  caller is told it succeeded. Requires backend ≥ 0.102.0.0.

  `probe notes write` now prints a `{project, chars}` confirmation rather than
  echoing the whole document back on stdout.

### Fixed

- **`npx probe-research` now runs the latest CLI instead of freezing on whatever
  you already had.** The launcher handed off to any local `probe` at or above
  `MIN_CLI` and never asked whether something newer existed. A floor is satisfied
  forever, so every user who had ever installed the CLI was pinned to it — and
  `npx <tool>` is the one command whose whole contract is "run the latest".

  This is the same freeze the `--refresh` flag exists to prevent one branch below
  (uv serving whatever it resolved on day one). It was found there, fixed there,
  and left standing in the handoff branch.

  The launcher now reads `cli.latest` from `/v1/client-version` — the same
  manifest the SessionStart nudge reads, so the two cannot disagree about what
  latest means — and falls through to a fetch when the local install is behind.
  `PROBE_BASE_URL` is honoured, because a self-hosted tenant's latest is not this
  one's. Every failure falls OPEN to the local install: offline, proxied, non-200,
  malformed version, or slower than 1.5s all run what you have. A currency check
  that can strand someone offline is worse than the staleness it fixes.

  Fetching also had to change, and this was the half that nearly shipped doing
  nothing. The spec was `>=MIN_CLI`, which an already-installed stale version
  satisfies, so `uv tool run` handed back the exact version just declared out of
  date — the launcher printed "fetching the latest" and changed nothing. Measured
  end to end: 0.46.0 detected as behind 0.47.0, then 0.46.0 returned. The spec now
  resolves to the newest known version, never below the floor.

- **DEP0190 on every launcher run.** `has()` paired an args array with
  `shell: true`, which Node deprecated because the arguments are concatenated
  rather than escaped. It printed a security warning on the from-zero entry point.
  Now one shell string.

### Added

- **`probe wizard` can write a tracking pointer into your global `CLAUDE.md`.**
  A skill has to be SELECTED before its body is read; `CLAUDE.md` is in context on
  every turn. That difference decides whether tracking happens. Observed directly:
  a session whose `CLAUDE.md` mandated searching Probe before design work used the
  READ surfaces perfectly for its whole length and never registered a project, an
  experiment or a note — because the write side had no equivalent standing rule.
  Same agent, same tools, same session; the only asymmetry was which surface
  carried the instruction.

  The block names SURFACES, never procedures. Procedures rot: in eight days the
  note vocabulary was added (#144), replaced by `NOTES.md` (#150) and re-triggered
  (#149), so a block naming `probe note add --kind` would now be teaching a command
  that does not exist. This file lives in the researcher's home directory and no
  release can reach it, so anything version-specific in it is stale forever.
  Naming the two skills and letting THEM carry the commands is what makes an
  unreachable copy safe.

  It is user-global, so it also loads while fixing an unrelated CSS bug. The rule
  is therefore conditional on the work being research rather than an unconditional
  order — a block that tells an agent to register a project during frontend work
  teaches the agent that the block does not apply to it, which costs it authority
  in the sessions it was written for.

  Opt-in on the wizard menu, defaulting on for a fresh machine and preserving the
  existing choice on a re-run, matching every other row. Everything outside the
  markers is preserved byte for byte; re-running never appends a second block; the
  wording is versioned so an outdated block is rewritten in place rather than
  left to drift, and `probe doctor` reports it as outdated instead of merely
  present. Unticking removes the block and leaves the file — a file in someone's
  home directory is not ours to delete.

### Added

- **`capture-run-inputs` skill.** `probe snapshot` captures what git can see; it
  cannot know that `data/train.jsonl` is the dataset and `.venv` is not, because
  `.gitignore` was written to keep a repo clean rather than to describe an
  experiment. The plumbing for the rest shipped over 0.38.0–0.43.0 (`--include`,
  upload, `snapshot-restore`); this is the judgment that drives it.

  The skill walks the agent from `snapshot-show` (read what was missed) through
  finding real inputs (paths the entry point opens, the launch config, `.gitignore`
  read per-entry, base weights, env var NAMES never values) to `--include`, and
  ends at `snapshot-restore --verify-only` so the claim is checked rather than
  assumed. It draws the inputs/outputs line explicitly — outputs are artifacts, and
  sweeping them into the snapshot makes "what produced this result?" unanswerable.

  It also requires recording what was CONSIDERED AND REJECTED, with reasons. Once
  scope is agent-judged, absence stops being informative: a file missing from a
  snapshot could mean "not an input", "judged not an input", or "nobody looked",
  and six weeks later those are indistinguishable.

- `tests/test_skills_commands_exist.py` asserts every `probe ...` command a skill
  teaches is actually registered. `test_skills_sync.py` guards the plugin copy
  against drifting from `skills/`; it cannot catch a perfectly-synced skill that
  teaches a renamed flag. Same invisible shape: tests pass, MCP is correct, only
  the agent is wrong.

### Added

- **`probe snapshot --include GLOB`** captures inputs `.gitignore` hides. `.gitignore`
  is right about build output and wrong about a downloaded dataset, a base
  checkpoint, or a config kept out of the repo on purpose — those are INPUTS, and
  the manifest had no way to name them, so they were recorded nowhere, not even as
  a hash. Repeatable; a directory captures its files; a glob matching nothing is an
  error rather than a silent no-op, and a path escaping the snapshot root is refused.

  Size decides the outcome. Under `--reference-over-mb` (100 default) the file is
  stored in the code-bytes archive. Above it, the path, host and sha256 are
  recorded as `source: "reference"` and the bytes are left where they are — copying
  a 40 GB checkpoint into every run is duplication, not reproducibility.

  `probe snapshot-restore` reports a reference as OFF-PLATFORM with its uri and
  host rather than as a failure, since the bytes exist somewhere specific. It does
  NOT count toward `n_unavailable`, but it does keep `tree_matches` false: a reader
  has to be able to tell "rebuilt" from "rebuilt except the checkpoint".

### Changed

- **The skills now say WHEN the project is created, and that they are re-entered.**
  The trigger to fire before a run exists was added in #144 and removed again in #150
  along with the note vocabulary it was written for. The mechanism #150 put in its
  place is better and the gap it left is the same one: an agent reading these still
  built the scaffold first and created the project afterwards, which is the one order
  that discards the reasoning `NOTES.md` exists to hold.

  Step 2 states the sequencing — create the identities at the moment the work is named,
  before the repo and the deps, because `NOTES.md` anchors to a project and has nowhere
  to go until one exists. `run_count: 0` is named as the correct state for a project
  whose first run has not started, since an empty project reads as premature and
  invites exactly that deferral.

  Re-entry is the other half. `start-research-work` is named for a moment, so it fired
  once and was done; forty turns into a planning session nothing brought an agent back.
  Both the body and the description now say it is re-entered, and name the four moments
  that were uncovered: choosing or rejecting an approach, the USER overriding you, a
  tool behaving differently than documented, and the point just before context is
  compacted or the session ends. It also draws the line against session capture — the
  transcript tap ships the raw conversation, `NOTES.md` is the skimmable version.

  `track-research-work` lost `notes` from its description in #150, so a session with
  zero runs read it as inapplicable; its description covers `NOTES.md` again, and a
  session that opened no run now has a closing act instead of ending silently.

- **`test_skills_sync.py` now parses the frontmatter it guards.** It compared the three
  copies and validated tool names, but never read the YAML — so a `: ` inside a
  description (`reproduce: training, evaluation`) terminated the plain scalar, broke the
  document, and stopped the skill loading entirely while every test stayed green. Found
  by writing that bug and watching the suite pass on it. Verified by breaking it again
  after: exit 1 with the bug, exit 0 without.

- **A directory that is not a git repository is now captured instead of refused.**
  `capture_manifest` raised outside a repo, so a project like `research-workflows/`
  got zero capture — not degraded capture, an error. That was defensible only
  while no uploader existed: the one case with NOTHING retrievable anywhere was
  the one turned away. With upload shipped (0.38.0) it is now the case that needs
  storing most.

  There is no reference half without git, so every file is `source: "blob"` and
  every file is uploaded; `base_commit`, `remote` and `vcs` are null and no shadow
  ref is taken.

  The concern behind the old refusal was real and is now a filter rather than a
  refusal. `SKIP_DIRS` drops what a lockfile rebuilds (`.venv`, `node_modules`,
  `__pycache__`, caches), and credential-shaped names (`.env`, `*.pem`, `id_rsa*`,
  `credentials*`) are excluded so that auto-uploading a working directory is not
  how a secret leaves the machine. Everything excluded is REPORTED in
  `manifest["skipped"]` with a reason — once a filter exists, absence stops being
  informative on its own.

### Added

- **The folder picker leads with a path bar.** The current path is now the
  first row and it is selectable: press enter on it and type or paste. Where
  you are and where you can type are the same control, which is the shortest
  route from "the path is already on my clipboard" to done — and anyone
  arriving from a cluster shell, Slack or the dashboard has the path. It used
  to be an "Enter a path…" item at the bottom of the list, below everything you
  would have to scroll past.

- The backfill progress line is centred with the rest of the wizard. Flush at
  column 0 it read as output from a different program running underneath.

- **Backfill lets the agent decide the projects, and name them.** The anchor
  used to be pinned before launch — one project, named after the folder — which
  collapsed `/workspace` (Michael's work, Xian's work, Connor's work) into a
  single project called `workspace`. The shape of the work is the judgement the
  agent is there for, so it now decides how many projects, which existing ones
  to file into, and what to call them. `--project` still forces one destination,
  and is resolved before launch so a bad name fails in a second rather than
  after twenty minutes of reading.

  What replaces the pin is discipline plus a backstop: the prompt makes the
  agent list what exists and reuse before creating (and argues why — the
  `odyssey-infill-v3` / `odyssey_infill_v3` near-miss splits a record in half
  invisibly), names are directed at the work rather than the directory, and
  `ensure_project`'s near-miss guard still refuses a typo-shaped slug whoever
  chose it.

- **`--project` accepts a slug** on `probe artifact add` and `probe artifact
  list`, not only a project id. Additive, never a new gate: an id passes
  through and so does anything that does not resolve, since the route already
  answers a bad anchor with a 422. Uses the exact `?slug=` lookup, so it is one
  request and correct past 200 projects.

  This is what makes agent-chosen projects workable — otherwise the agent would
  have to capture a uuid at creation and thread it through several thousand
  commands, and only has to get that wrong once.

  The reconcile follows suit: the agent's summary names every project it filed
  into, and that is the only thing taken from its own account of the run. It
  says where to look; the server still says how many and the walk still says how
  many there should be, so an agent that overstates its work cannot make the two
  agree. No projects named is reported as uncounted, never as zero.

- **`probe snapshot-restore RUN_ID DEST`** rebuilds a run's captured working tree.
  Files git can supply are fetched from the recorded remote (one depth-1 fetch of
  the base commit, not one per file); the rest come from the uploaded `code-bytes`
  archive. Storing bytes without a way to reassemble them moved the gap rather
  than closing it.

  Every file is verified against the sha256 the manifest recorded, and the rebuilt
  tree against `tree_sha256`. A mismatch is reported UNAVAILABLE and **never
  written** — the `probe.sandbox-state/1` rule: degrade to "unavailable", never to
  a wrong answer. The command exits non-zero if any file could not be produced,
  and reports per file rather than all-or-nothing, so an unreachable remote still
  restores what the archive holds.

  `--verify-only` resolves and hashes everything without writing, which is how a
  fleet gets swept for "which of these can actually be rebuilt?".

### Added

- **`probe snapshot` now uploads the bytes git cannot supply.** Files classified
  `source: "blob"` — edited, untracked, unpushed, or no remote at all — are tarred
  into a single `code-bytes` artifact and stored through the ordinary presign
  flow. Previously the record kept a sha256 for them and nothing else, and a
  sha256 verifies a file you already have rather than producing one you do not:
  the run was identified precisely and unreproducible. Confirmed on `bird-sql-sft`,
  where 16 completed runs lost their code when the box was rebuilt while still
  reading as captured.

  On by default; `--no-upload` opts out. `--max-upload-mb` (256 default) refuses
  rather than truncating — a silently partial archive reporting success is the
  original defect in a new place. Files already retrievable from a pushed remote
  stay references, so nothing is uploaded twice.

  The archive is byte-deterministic (normalised mtime/uid/gid/owner/order, and
  `filename=""` so gzip does not stamp the output path into its header), which
  lets the presign `have` check collapse an N-run sweep over unchanged code to a
  single upload. Modes and symlinks survive — a restored tree whose entrypoint
  lost `+x` does not run.

  The artifact meta’s `n_pending_upload` now reports what SURVIVES the upload,
  not what was classified, so `check_run` gating `pending_code_bytes` on it means
  "these bytes are gone" rather than "an upload was attempted".
  `n_classified_pending` keeps the pre-upload count for diagnostics.
- **`probe notes` — one free-text markdown document per project.** `probe notes
  show` prints it; `probe notes write [FILE]` replaces it (stdin when no file),
  and `--append` adds to it instead, which is what you want when two agents share a
  project and a plain write is last-one-wins. Free text, no schema.

  It rides along on the project's MCP `card` as an excerpt, which is the part that
  makes it work: an agent orients with `browse_research` and a card, and a briefing
  it has to know to ask for is one it does not read. `view="notes"` returns the whole
  file. `client.get_project_notes()` / `set_project_notes()` from the SDK.

### Removed

- **`probe note` and its research-note vocabulary are gone**, replaced by the plain
  markdown file above. A note was an entry with a `kind`
  (`intent|hypothesis|decision|observation|failure|result|deviation|next_step`),
  plus `--supersedes`, `--authority` and `--confidence`, encoded into a
  `kind="note"` artifact. Nothing server-side ever validated, aggregated or grouped
  by any of it — `NOTE_KINDS` was a set in the client and `agent_summarized` appears
  nowhere in the backend — so eight kinds bought a single list filter, at the cost of
  making every writer pick one. What people actually write is prose, and the durable
  claims this was meant to hold were already going into markdown in the repo.

  Gone with it: `client.notes`, `NoteClient`, the `EventKind` enum, and the
  supersession machinery (a markdown file is edited, so "replaced" needs no model).
  Project-anchored notes shipped in 0.40.0 and 0.41.0 only. Existing `kind="note"`
  artifacts are untouched and still readable as ordinary artifacts.

### Fixed

- **Backfill imports into a project named for the folder, not the ambient
  active one.** Pointing at `anthrogen-backfill-test` put its artifacts in
  whatever `probe project use` had last been set to — a place nobody would
  think to look. `project use` sets where new *runs* go; it was never a
  standing statement about where imported folders belong. The ambient project
  (`probe project use`, `PROBE_PROJECT`) is no longer consulted; `--project`
  names a destination explicitly when you want one.
- **The test fake's experiment-artifact listing was inverted in both directions.** It
  rolled up the artifacts of the experiment's RUNS — rows
  `GET /v1/experiments/{id}/artifacts` has never returned, it filters `experiment_id`
  alone — while reading directly-filed ones from the wrong key, so it missed the only
  rows that do belong. It also dropped `meta` on project/experiment artifact writes,
  which a research note IS: a note test would have gone green against a fake that
  threw the note away.

- **Run lineage is no longer a half-answer.** `get_entity(ref="run:<id>",
  view="lineage")` walked `parent_run_id` only — fork/retry parentage — and
  never read the edge table, so a run that consumed a dataset version and
  produced three artifacts answered `ancestors: [] / descendants: []`. An agent
  reads that as "this run has no lineage", which is a confident wrong answer
  rather than a missing one. The view now returns both relations under separate
  keys: `run_ancestry` (the parent chain, unchanged) and `edges` (artifact and
  asset-version provenance). Kept separate deliberately — they are different
  relations over different endpoint kinds, and flattening them recreates the
  ambiguity that made the empty response unreadable.
- **A run hit from the exact channel is addressable.** `search_knowledge` now
  maps `entity_type: "run"` to `research://runs/<id>/handoff` and carries
  `short_id` in the card. Pasting a petname you were handed resolves to the run
  (research-os 0093 added the backend's runs branch); without the card field a
  correct hit could look unrelated to the query, since a run's `name` may be
  server-derived or since edited.

- **The reuse check works again.** The MCP instructions, `get_entity`'s
  description and `start-research-work`'s step 4 all mandated
  `get_entity(ref="asset:<name>", view="versions")` — the guard against duplicate
  identities, called the most expensive avoidable error in the system. The asset
  registry was retired into artifacts (research-os #143/#144) and the MCP asset
  views were deleted, so that call had nothing behind it for a release.

  It did not fail cleanly. `asset` was not a key in the ref resolver, so the ref
  fell into a guess-every-getter loop that caught only `NotFoundError`;
  `get_experiment()` raises a 422 `uuid_parsing` on a non-UUID name, so a
  compliant agent got a parse error naming `experiment_id` for a call that never
  mentioned an experiment. And because the description defines an error as "the
  name does not exist, a new identity is licensed", the guard **against**
  duplicate identities licensed one on every call.

  The check is now `get_entity(ref="artifact:<name>", view="versions")`,
  resolving by name against the shared, lab-wide level. An unknown ref kind is
  rejected outright instead of guessed at.

- `EnvelopeState.NO_MATCH` is real. The tool description had promised
  `state="no_match"` since the asset registry shipped and the enum never had the
  member, so "this artifact exists but no version satisfies your requirement" was
  indistinguishable from "no such artifact" — the confusion that opens a second
  identity. `highest_version` and `version_count` ride the fixed-size payload, so
  the ceiling survives token-budget truncation.

- A bare ref is checked for UUID shape locally, so a genuine backend 422 is no
  longer rewritten as "nothing matches this ref".
- **`probe snapshot` recorded the CLI's own environment as the project's.**
  `capture_env` enumerated `importlib.metadata` in the calling process. That is
  correct for `run.snapshot()`, which runs inside the training venv, and wrong for
  the CLI, which is a uv-tool install: snapshots taken from the command line
  recorded typer/rich/questionary/mcp and the tool's Python version instead of the
  project's packages. `strict=True` only refused an *empty* dependency set, so the
  wrong one was written as a confident, plausible execution record — the exact
  "unreproducible due to different venvs" failure the record exists to prevent.

  `probe snapshot` now resolves the project's virtualenv (`.venv` / `venv` / `env`,
  searching from `--cwd` up to the git toplevel, then `VIRTUAL_ENV`, then
  `CONDA_PREFIX`) and enumerates packages by running `importlib.metadata` under
  **that** interpreter — no `pip` required, which matters because `uv venv` installs
  none. New `--venv PATH` pins it explicitly. `strict` now also refuses the
  wrong environment, not just an absent one: with no project venv found and the
  running interpreter outside the tree, the snapshot fails instead of recording.

  `deps` gained `venv`, `python_executable` and `resolved_via`, so a capture that
  picked the wrong environment is visible in the record rather than
  indistinguishable from a correct one. Those paths participate in the execution
  record's content hash, so identical environments at different paths no longer
  share a record — deliberate, and already true of `hardware.gpu`.

  SDK behaviour is unchanged by default (`run.snapshot()` records its own
  interpreter). Launchers that start training as a subprocess should pass
  `run.snapshot(detect_venv=True)` or an explicit `venv=`.

  Packages are now always enumerated by running the target interpreter, including
  when that is the current one. The in-process variant was deleted rather than
  kept: two implementations of one algorithm whose output is hashed into
  `env_ref` will drift, and the drift reads as two identical environments
  comparing unequal — indistinguishable from a real dependency change. The spawn
  costs ~50ms once per run, since a snapshot is a launch-time act. A frozen
  interpreter (PyInstaller) now raises instead of enumerating the bundled app.

  `deps` carries only what the environment IS (`python`, `packages`,
  `package_count`, `packages_sha256`). The provenance — `venv`,
  `python_executable`, `resolved_via` — rides on the `code-snapshot` artifact
  meta under `env`, because the execution record's `content_hash` covers the
  whole `deps` section and an absolute path in it would make two identical
  environments at different paths produce different `env_ref`s.

### Removed

- **Archiving is gone**, following the backend (research-os 0.88.0.0). Archiving
  hid a project or experiment with no way to bring it back, and `run delete` was
  a soft-delete whose only purge path was an owner-only `run gc`. Removed from
  the SDK: `archive_project`, `restore_project`, `archive_experiment`,
  `restore_experiment`, `restore_run`, `gc_runs`, and the `include_archived` /
  `include_deleted` keyword arguments. Removed from the CLI:
  `probe project archive|restore`, `probe experiment archive|restore`,
  `probe run restore|gc`, and the `--include-archived` / `--include-deleted`
  flags.

### Added

- **Backfill shows what the agent is doing.** A bare `claude -p` prints nothing
  until it exits, so an import over a real folder sat silent for minutes and
  read as frozen. Both agents are now asked for a JSONL event stream and the
  run renders as one self-updating line — `⠹ 1:07 · 14/37 · uploading
  docq_scores.csv` — counting uploads against the census, so the number you
  watch is the denominator the reconcile checks at the end. Not the transcript:
  an agent transcript is thousands of lines nobody reads.

- **`probe backfill --agent claude|codex`.** Asked only when both are installed
  and neither was named. The two are confined differently and the picker says
  so rather than implying parity: Claude takes a tool allowlist (`Bash(probe:*)`
  — it cannot write, delete or fetch), Codex takes a filesystem+network sandbox,
  which bounds where commands act but not which ones run.

- **Paste a path in the folder picker.** "Enter a path…" accepts quotes, `~`
  and relative paths, and re-asks on a bad one rather than dropping you back
  into a browser two directories away.

- **`probe backfill`** — a top-level command, so `npx probe-research backfill`
  works from zero. Arguments are forwarded verbatim by the npm launcher, so the
  command the dashboard's last onboarding step hands you lands straight on the
  folder picker. `probe backfill <folder>` skips the picker.

  It installs a persistent `probe` first, and for a stronger reason than the
  wizard has: reached through `npx` we are running from an ephemeral uvx/pipx
  with no binary on PATH, and the agent does its work by shelling out to
  `probe artifact add`. Without that step the agent reads the whole folder and
  lands nothing.

  The npm launcher's CLI floor moves to **0.36.0** for the same reason it moved
  to 0.27.1: arguments are forwarded to whatever `probe` is already on PATH, so
  under the old floor a user on 0.35.0 would answer a command the product just
  told them to run with `No such command 'backfill'`. Nothing in the copied
  string differs — only the floor can catch it.

- **`probe wizard` → Import existing work.** Point the wizard at a folder of
  existing research and one headless Claude agent reads it, uploads what it
  finds, and describes each artifact. The wizard does the two things a program
  does better and hands the middle to the agent: it ENUMERATES the folder
  (file and byte counts, pruning build noise) so the denominator comes from a
  walk no model produced, and it RECONCILES what landed against that count
  afterwards. Silent partial coverage reading as success is the failure this
  shape exists to prevent.

  The folder picker labels every subdirectory with its file count and size, so
  nobody points an importer at a 2.9 TB `checkpoints/` without seeing it first.
  Files over 100MB are recorded as references (`--reference --allow-missing`,
  unhashed — fingerprinting a 10GB checkpoint over a shared mount costs minutes
  and buys nothing); everything else uploads.

  The project anchor is fixed before the agent starts and resolved through
  `ensure_project`, so an agent may decide what a folder MEANS but never what it
  is CALLED — a second run opening a second project for the same work is the one
  mistake here that cannot be undone. The agent runs with
  `Bash(probe:*),Read,Glob,Grep,Task` and nothing else: it sweeps folders nobody
  audited, so it can call the probe CLI and read, but not write, delete, or
  reach the network by any other route.

  `--action backfill --folder <path>` skips the picker for headless use.

- `probe project delete` and `probe experiment delete`, plus SDK
  `delete_project()` / `delete_experiment()`. All three delete verbs
  (`project`, `experiment`, `run`) are permanent, take the whole subtree, and
  prompt for confirmation unless `--yes` is passed.

### Changed

- `delete_run()` returns `None` (the backend now answers 204) instead of the
  soft-deleted run.
- Slug resolution has two outcomes again, not three. An archived slug used to be
  a dead end where lookup said "missing" and create said "already exists";
  deleting frees the slug, so `resolve_or_raise` and the create guard no longer
  carry an ARCHIVED branch.

- `search_knowledge`'s `search_in` and `collapse` are now typed as enums, so
  their vocabularies ship in the tool's JSON Schema (`$defs.ToolCorpus`,
  `$defs.CollapseMode`) instead of existing only as prose in the description.

  Callers get client-side validation and a rejection that names every accepted
  value: `Input should be 'files', 'documents', 'transcripts' or 'experiments'`.
  Previously a typo round-tripped to the server and came back as
  `unsupported_values`, which named the bad value but never the valid set.

  **Behaviour change:** one bad entry now rejects the whole list.
  `search_in=["documents", "bogus"]` used to search `documents` and flag
  `bogus`; it now fails. The rows that call used to return were for the value
  the caller already got right, and the error hands a caller the correct
  vocabulary for an immediate retry.

  `ResearchReadService` still takes plain strings and keeps its graceful
  unsupported-value handling — it is callable directly from Python, where
  nothing validates on its behalf.

## Unreleased

### Added

- **`capture-run-inputs` skill.** `probe snapshot` captures what git can see; it
  cannot know that `data/train.jsonl` is the dataset and `.venv` is not, because
  `.gitignore` was written to keep a repo clean rather than to describe an
  experiment. The plumbing for the rest shipped over 0.38.0–0.43.0 (`--include`,
  upload, `snapshot-restore`); this is the judgment that drives it.

  The skill walks the agent from `snapshot-show` (read what was missed) through
  finding real inputs (paths the entry point opens, the launch config, `.gitignore`
  read per-entry, base weights, env var NAMES never values) to `--include`, and
  ends at `snapshot-restore --verify-only` so the claim is checked rather than
  assumed. It draws the inputs/outputs line explicitly — outputs are artifacts, and
  sweeping them into the snapshot makes "what produced this result?" unanswerable.

  It also requires recording what was CONSIDERED AND REJECTED, with reasons. Once
  scope is agent-judged, absence stops being informative: a file missing from a
  snapshot could mean "not an input", "judged not an input", or "nobody looked",
  and six weeks later those are indistinguishable.

- `tests/test_skills_commands_exist.py` asserts every `probe ...` command a skill
  teaches is actually registered. `test_skills_sync.py` guards the plugin copy
  against drifting from `skills/`; it cannot catch a perfectly-synced skill that
  teaches a renamed flag. Same invisible shape: tests pass, MCP is correct, only
  the agent is wrong.

### Added

- **`probe snapshot --include GLOB`** captures inputs `.gitignore` hides. `.gitignore`
  is right about build output and wrong about a downloaded dataset, a base
  checkpoint, or a config kept out of the repo on purpose — those are INPUTS, and
  the manifest had no way to name them, so they were recorded nowhere, not even as
  a hash. Repeatable; a directory captures its files; a glob matching nothing is an
  error rather than a silent no-op, and a path escaping the snapshot root is refused.

  Size decides the outcome. Under `--reference-over-mb` (100 default) the file is
  stored in the code-bytes archive. Above it, the path, host and sha256 are
  recorded as `source: "reference"` and the bytes are left where they are — copying
  a 40 GB checkpoint into every run is duplication, not reproducibility.

  `probe snapshot-restore` reports a reference as OFF-PLATFORM with its uri and
  host rather than as a failure, since the bytes exist somewhere specific. It does
  NOT count toward `n_unavailable`, but it does keep `tree_matches` false: a reader
  has to be able to tell "rebuilt" from "rebuilt except the checkpoint".

### Changed

- **A directory that is not a git repository is now captured instead of refused.**
  `capture_manifest` raised outside a repo, so a project like `research-workflows/`
  got zero capture — not degraded capture, an error. That was defensible only
  while no uploader existed: the one case with NOTHING retrievable anywhere was
  the one turned away. With upload shipped (0.38.0) it is now the case that needs
  storing most.

  There is no reference half without git, so every file is `source: "blob"` and
  every file is uploaded; `base_commit`, `remote` and `vcs` are null and no shadow
  ref is taken.

  The concern behind the old refusal was real and is now a filter rather than a
  refusal. `SKIP_DIRS` drops what a lockfile rebuilds (`.venv`, `node_modules`,
  `__pycache__`, caches), and credential-shaped names (`.env`, `*.pem`, `id_rsa*`,
  `credentials*`) are excluded so that auto-uploading a working directory is not
  how a secret leaves the machine. Everything excluded is REPORTED in
  `manifest["skipped"]` with a reason — once a filter exists, absence stops being
  informative on its own.

### Added

- **`probe snapshot-restore RUN_ID DEST`** rebuilds a run's captured working tree.
  Files git can supply are fetched from the recorded remote (one depth-1 fetch of
  the base commit, not one per file); the rest come from the uploaded `code-bytes`
  archive. Storing bytes without a way to reassemble them moved the gap rather
  than closing it.

  Every file is verified against the sha256 the manifest recorded, and the rebuilt
  tree against `tree_sha256`. A mismatch is reported UNAVAILABLE and **never
  written** — the `probe.sandbox-state/1` rule: degrade to "unavailable", never to
  a wrong answer. The command exits non-zero if any file could not be produced,
  and reports per file rather than all-or-nothing, so an unreachable remote still
  restores what the archive holds.

  `--verify-only` resolves and hashes everything without writing, which is how a
  fleet gets swept for "which of these can actually be rebuilt?".

### Added

- **`probe snapshot` now uploads the bytes git cannot supply.** Files classified
  `source: "blob"` — edited, untracked, unpushed, or no remote at all — are tarred
  into a single `code-bytes` artifact and stored through the ordinary presign
  flow. Previously the record kept a sha256 for them and nothing else, and a
  sha256 verifies a file you already have rather than producing one you do not:
  the run was identified precisely and unreproducible. Confirmed on `bird-sql-sft`,
  where 16 completed runs lost their code when the box was rebuilt while still
  reading as captured.

  On by default; `--no-upload` opts out. `--max-upload-mb` (256 default) refuses
  rather than truncating — a silently partial archive reporting success is the
  original defect in a new place. Files already retrievable from a pushed remote
  stay references, so nothing is uploaded twice.

  The archive is byte-deterministic (normalised mtime/uid/gid/owner/order, and
  `filename=""` so gzip does not stamp the output path into its header), which
  lets the presign `have` check collapse an N-run sweep over unchanged code to a
  single upload. Modes and symlinks survive — a restored tree whose entrypoint
  lost `+x` does not run.

  The artifact meta’s `n_pending_upload` now reports what SURVIVES the upload,
  not what was classified, so `check_run` gating `pending_code_bytes` on it means
  "these bytes are gone" rather than "an upload was attempted".
  `n_classified_pending` keeps the pre-upload count for diagnostics.

### Breaking

- `search_knowledge`'s `corpora` parameter is now **`search_in`**. Passing
  `corpora` raises; it is not honoured and not aliased.

  The old name read as the plural of the backend's `corpus` field
  (`POST /v1/search`), and it is not. Before this release, two of the five
  values mapped identity (`transcripts`, `experiments`) and three did not
  (`documents` fanned out to github + files; `assets` and `procedures` both
  collapsed to files). Whichever identity value you tried first confirmed the
  misreading. See the next entry for the value list as it stands now.

  `corpora` stays bound in the tool signature, marked `deprecated`, **purely to
  reject**. Deleting it would have been silent: FastMCP builds its argument
  model without `extra="forbid"`, so pydantic discards unknown keys — a stale
  caller would have received an unfiltered search wearing a success envelope,
  which is the failure this tool already refuses elsewhere.

  Response fields rename with it: `unsupported_corpora` -> `unsupported_values`,
  and the `kb_corpora` completeness marker -> `kb_values`.

- The `assets` and `procedures` values collapse into **`files`**. Both mapped to
  the same backend corpus, so the tool was advertising a distinction the index
  cannot make (`IndexDocType` has one bucket, `workspace.file`). Narrowing to
  `assets` never excluded a procedure, and vice versa.

### Added

- `make regen-mcp-schema` re-captures the MCP tool-schema baseline. It pins
  `PYTHONPATH` and refuses to run against a source tree other than the one you
  are in, because a bare `import probe.mcp.server` from a worktree resolves to
  the *installed* package and would snapshot the wrong schema while the pin
  test stayed green.

## 0.28.0

### Added

- Run titles and descriptions can now be edited with
  `probe run set RUN --name ... --description ...`, matching the existing
  project and experiment editing commands.
- `probe run start` and `probe run child` accept `--description`, and the
  Python SDK exposes run descriptions on creation, reads, and
  `Client.update_run()`.

## 0.27.1

### Fixed

- `probe wizard` no longer dies with `KeyError: Capability.AUTO_UPDATE` right
  after you answer the auto-update question. `plan()` read every capability's
  label out of `MENU_COPY`, which holds only the two checkbox rows — auto-update
  is asked as its own step and its copy lives in `AUTO_UPDATE_COPY`. It was the
  worst possible split: auto-update defaults ON and starts OFF, so the plan
  always changed it, so *every fresh install crashed* — after the consent menu
  and before anything was installed. `probe wizard --yes` on a fresh machine
  (CI, scripted setup) crashed the same way, since `plan()` runs on the flag
  path too. Labels now come from `PLAN_LABELS`, which is total over
  `Capability` and asserted to stay that way. Broken since the auto-update step
  was split out of the picker (#73), shipped in 0.26.0 through 0.27.0.

## 0.27.0 (unreleased)

### Breaking

- `check_run` / `probe run check` no longer answer `complete` on the cheap path.
  It counted rows — is there an `env_ref`, is there a `code_snapshot` artifact —
  and never asked whether either led anywhere, so seventeen runs whose code was
  already unrecoverable read as captured for a week. Three verdicts now:
  `incomplete` (something absent or provably unrecoverable), `unverified` (the
  default: nothing obviously absent, which is NOT "can be rebuilt"), and
  `complete`, earned only under `verify=True` / `--verify` by resolving the
  recorded commit against its remote. Callers testing `state == "complete"` must
  either pass `verify` or accept `unverified`. CLI exit 2 now means `incomplete`
  specifically, so an unverified run no longer fails a script.

### Added

- `check_run(verify=True)` and `probe run check --verify` resolve the captured
  code reference by depth-1 fetching the recorded commit from the recorded
  remote — the same thing a reproduction does. `snapshot.commit_on_remote()` is
  memoized on `(remote, commit)` and bounded by a 20s timeout, so auditing a
  project costs one fetch per distinct base commit rather than one per run
  (measured: 201 runs sharing a base = 1 network call, 2.6s; the other 200
  resolve from cache in 0.01ms total). Never called during a run, so it cannot
  affect training or upload throughput.
- `check_run` reports `pending_code_bytes` when the manifest records files whose
  bytes were never stored. Free: the summary already arrives on the artifact's
  meta, so it costs a dict lookup and no network. This is the failure mode
  per-file capture introduced in 0.26.3, and leaving it unchecked would have
  repeated the original mistake in a new place.

- Miles' existing `probe.connectors.miles.per_sample_rollout_log` hook now
  captures arbitrary numeric entries from `sample.metadata["probe_metrics"]`
  and inline `args.probe_sample_metrics` metric-name to dotted-path mappings.
  Stock launchers that cannot carry custom args can define the same mapping with
  `make_per_sample_rollout_log(...)` in an importable hook module.
  These values use the same durable metric queue and database representation as
  aggregate `tracking.log()` points, with `metric_scope=sample`, sample/group
  labels, and the existing Harbor rollout-span anchor distinguishing them.
  Missing and non-numeric values are omitted instead of becoming false zeros;
  explicit numeric zero remains a measurement. Runs reserve 1,024 configurable
  sample points per sample by default, adjustable through
  `args.probe_sample_metric_budget`.

## 0.26.4 (unreleased)

### Fixed

- `get_entity(view="reproduce")` no longer fails on token budget. The view is
  atomic (never truncated), so the per-file code manifest inside it made the whole
  call error on any real repo — 224 files was 79,809 characters, 94% of it manifest
  rows. It now carries the manifest SUMMARY plus `entries_omitted`; the rows stay
  available at `/v1/execution-records/{env_ref}`. Same run: 3,713 characters.

### Added

- `probe snapshot-show <run>` prints a run's captured code manifest, one file per
  line, with `--pending-only` for the files whose bytes are not yet stored.
  `probe snapshot` now also reports the referenced / pending-upload counts.
- `capture_manifest` and `pushed_base` are exported from `probe.snapshot`.

## 0.26.3

### Fixed

- Code capture no longer stakes reproducibility on a commit that may exist only
  on the machine that ran the job. `snapshot.capture_manifest()` classifies each
  file per-FILE as retrievable from a *pushed* remote (`source="git"`) or needing
  its bytes uploaded (`source="blob"`), proving reachability with `git ls-remote`
  rather than assuming it. `Run.snapshot()` publishes the manifest and its
  `tree_sha256` on the execution record and the code-snapshot artifact meta.
  Classification only: `n_pending_upload` counts outstanding work, and callers
  still move the bytes.
- `snapshot.capture_env()` records the resolved package LIST instead of only a
  digest and a count, reads it via `importlib.metadata` (a `uv venv` ships no
  `pip`, so the previous `pip freeze` subprocess captured nothing at all), and
  raises instead of silently returning `{"python": ...}`. Strictness follows the
  client's `fail_open` setting unless `snapshot(strict=...)` overrides it.
  **Breaking for digest consumers:** `packages_sha256` still exists but is now
  computed over sorted `name==version` lines, so its value differs for an
  unchanged environment. Do not compare across this boundary.
- Remote URLs are credential-scrubbed before being recorded. A CI remote such as
  `https://x-access-token:<TOKEN>@github.com/...` previously copied a live token
  into run metadata and artifact meta.
- `ls-remote` runs with a 10s timeout and `GIT_TERMINAL_PROMPT=0`, so an
  unreachable or credential-prompting remote can no longer hang the start of a run.

### Added

- `Run.reconcile_artifact(name, content_hash)` finds an artifact a lost response
  hid, so a retry reuses it instead of creating a duplicate. Opt-in:
  `log_artifact` does not call it yet.

- Expanded Harbor trajectories now stamp every turn, tool call, nested span,
  and truncation marker with a zero-based `attributes.trajectory_index`.
  Consumers can restore parser execution order without relying on optional
  timestamps; system and user setup turns also stop inheriting model metadata
  that ATIF did not record on those steps.
- SDK-owned Harbor captures now request recognized trajectory expansion from
  the durable watcher by default, removing the manual `probe trial expand`
  step for future captures while retaining the raw trajectory artifact.

## 0.26.2 (unreleased)

### Fixed

- Miles per-sample reward and response-length points now carry the same
  deterministic rollout `span_id` as their correlated Harbor capture whenever
  the agent response includes the capture `external_key`. This makes the
  dashboard's sample → trial → trajectory/sandbox join exact without requiring
  Miles-core changes. The optional anchor survives durable queue replay, while
  older and non-Harbor records continue draining unanchored.

## 0.26.1 (unreleased)

### Changed

- `search_knowledge` no longer discards knowledge hits. `collapse="experiment"` (the
  DEFAULT) used to drop every result row that was not an experiment or run, so every
  document, transcript, file and artifact hit the backend returned was filtered out
  before the caller saw it — the ingested Claude Code session corpus was unreachable
  through the tool entirely. Collapse now dedupes experiments and runs and passes
  everything else through in the merged ranking order. Callers on the default will
  start seeing rows with `entity_type` `document` / `file` / `project` / `artifact`;
  those rows are terminal (no `resource` to hand to `get_entity`).
- `search_knowledge` `corpora` now narrows the semantic channel to exactly the corpora
  named, instead of always unioning `experiments` in. The union made narrowing useless
  in practice: the per-channel budget is ~`top_k/2` and experiment projections outrank
  the knowledge corpora, so `corpora=["transcripts"]` came back holding only
  experiments. To restore the old behavior, name it: `["experiments", "transcripts"]`.
  A narrowing where every named corpus is unrecognized still falls back to
  experiments-only and reports `kb_corpora` in `completeness.missing`. The exact
  channel is structured-entity search and remains un-filtered by corpus.

### Fixed

- Miles now reserves three labeled metric points per planned rollout sample:
  the per-sample reward and response length plus the correlated Harbor verifier
  reward. This prevents the durable exporter from exhausting a run's
  create-time labeled-point budget during normal per-sample capture.

## 0.26.0 (unreleased)

### Added

- `HarborCaptureResult.begin_bytes_captured` (and `SandboxStateRecorder.begin_bytes_captured()`
  + a `begin_bytes_captured` field in the recorder summary): whether the trial
  archived and verified begin-state bytes. Lets a bridge's per-task election read
  capture status straight off the `finalize` result instead of re-parsing the
  authored `meta.json` from disk.

## 0.25.0 (unreleased)

### Changed

- Experiment creation and passive ingest now require an explicit project.
  The CLI can use `--project`, an active project selection, or an exact project
  identifier; SDK and ingest callers must send the project coordinate.

### Removed

- The agent no longer creates or relies on a synthetic `Default` project, and
  default-named projects can be archived like any other project.
- The unused automatic-hypothesis helpers and placeholder experiment behavior
  have been deleted.

## 0.24.0 (unreleased)

### Breaking

**Root `--token`, `--ingest-token`, and `--hmac-secret` flags are removed.**
A secret in argv leaks into shell history and `ps`, and the new background
outbox drainer could never resolve a credential that lived only in one
process's flags. Migrate to the environment variables the SDK already honors
(`PROBE_TOKEN`, `PROBE_INGEST_TOKEN`, `PROBE_HMAC_SECRET`) or a named context
via `probe login`. `probe login --token` (which STORES the credential) is
unchanged; `--base-url` and `--spool-dir` remain.

**The JSONL spool is replaced by the outbox journal.** Fail-open writes now
land in `~/.local/state/probe/outbox` (override: `PROBE_OUTBOX_DIR` or
`--spool-dir`) as one versioned operation journal (`probe.outbox/1`) with
per-op identity, run tags, context pins, and a content-addressed blob store.
A surviving legacy spool is imported automatically, in order, on first use.
`Client(spool=...)` is gone; pass `journal=` or `spool_dir=`.

### Added

- **Begin-state bytes** (`probe.sandbox-state/1`): the snapshot tool's `begin`
  subcommand gains `--bytes`, teeing the manifest walk into a streamed
  `begin-bytes.tar.gz` — the byte-level "before" state of the sandbox that the
  bundle previously only described as metadata. Modified files get true
  before/after diffs; deleted files' contents become recoverable. Guarded by
  `--max-begin-bytes` (default 32 GiB, further capped at 50% of free space)
  with the same drop accounting and PSBX1 trailer integrity as the end delta.
- `SandboxStateOptions` grows `root` (plumbs the binary's existing scan-root
  flag), `begin_bytes`, `begin_bytes_ref`, and `max_begin_bytes`. The sharing
  model is per-task: the caller's ledger elects one trial per task
  (`task_checksum`) to capture; every trial of the task stamps
  `meta.json.begin_bytes = {captured, ref, budget_bytes, truncated,
  dropped_count}` so renderers can resolve the shared archive and verify
  per-file validity against the begin manifest's sha256s (design:
  `docs/2026-07-29-begin-state-bytes.md`).
- `begin_timeout_sec` now defaults to `None`, resolving to 120 s (600 s when
  `begin_bytes` is on); explicit values are honored unchanged.

**`--async` / `PROBE_ASYNC=1`: non-blocking writes.** `probe log`, `span add`,
`note add`, `artifact add`, and `run end` queue to the local outbox and return
immediately; a wake-on-enqueue detached drainer delivers with retries and
capped backoff until the queue is empty, then exits. Small files fingerprint
and register upload intent inline (a ~2s-capped presign ping creates the
server's pending row); large files snapshot instantly (filesystem clone where
supported) and hash in the drainer. Failure policy: permanent rejections
dead-letter and the queue keeps flowing; transient failures wait and retry;
401/403 halts delivery with items untouched.

Delivery is **at-least-once**: a crash between the server committing a write
and the journal deleting the op replays it (ops carry an `op_id`; the drain
fsyncs deletions to keep the window minimal, and 409-with-existing_id on a
retry is treated as our own earlier delivery). Scope run refs consistently —
the run-end barrier matches the literal ref you enqueued with (id vs slug).

**`probe outbox status|drain|watch|retry|pause|resume`** — one surface over
the whole queue; `probe flush` is now an alias of `outbox drain`. Every
command prints a one-line stderr banner when the outbox holds dead letters or
is auth-blocked, and `probe doctor` gained an Outbox section. `probe run end`
is a run-scoped barrier: it delivers that run's queued items first and exits
non-zero (without closing the run) while any cannot be delivered.

### Changed

- The begin phase now downloads and sha256-verifies every file the trailer
  names (previously just the manifest), so the begin archive inherits the
  manifests' tamper-evidence.

## 0.23.0 (unreleased)

### Added

- **`probe.connectors.harbor_capture`** — the SDK-owned capture facade for
  Harbor bridges. Any bridge/server that owns a harbor `Trial` gets Probe
  capture in ~3 lines:

  ```python
  from probe.connectors import harbor_capture

  handle = harbor_capture.attach(trial, correlation={...}, context={...},
                                 capture_mode="shadow",
                                 sandbox_state=SandboxStateOptions())
  try:
      result = await trial.run()
  finally:
      capture = await handle.finalize(trial_dir)
  ```

  `attach()` installs the correlation hooks (logical `session_id` plus a
  best-effort provider sandbox id read from stable string identifiers on the
  per-backend private handles — Daytona/E2B `_sandbox`, Modal
  `_sandbox.object_id`, Runloop `_devbox.id` — retained so they survive
  Harbor nulling the environment handle) and, when `sandbox_state=` options
  are given, the existing `probe.sandbox-state/1` recorder from
  `harbor_runner`. `finalize()` stages the trial tree through
  `stage_trial_export` and returns a `HarborCaptureResult` carrying the
  staged paths, archive hash, external key, sandbox ids, and the
  sandbox-state summary (also folded into the export's
  `context.sandbox_state`).

  Capture modes: `off` (no-op handle, harbor never imported), `shadow`
  (best-effort — staging failures come back as `status="failed"`, never
  raised), `required` (same staging, but the caller gates on
  `capture.complete` / `capture.raise_if_incomplete()` to fail its
  response). Harbor stays an optional lazy dependency behind
  `verify_harbor_contract()`.

- `SandboxStateRecorder` grew `summary()` (the JSON-safe verdict the facade
  folds into capture context, `"not_attempted"` until a hook fires),
  `attempted()`, and `record_install_failure()` for callers that install the
  hooks fail-open.

### Fixed

- The durable Harbor exporter now maps Miles `sample_id` and `group_id`
  correlation onto Probe `sample` and `group` point labels. Multiple trials at
  the same training step therefore retain distinct reward points and join
  directly to their `harbor_trial` manifests without creating per-sample metric
  series.

## 0.22.0 (unreleased)

### Breaking

**`run.log()` auto-increments `step` when you omit it.** Previously a bare
`run.log({"loss": l})` sent no `step_index` at all and the points landed on the
wall-clock axis. They now land on steps 0, 1, 2, … so the common loop draws a
curve. This silently changes the axis of any existing bare-`log()` call site.

```python
for batch in loader:
    run.log({"loss": loss})        # 0.16.0: no step   0.17.0: steps 0,1,2,…
```

Opt out with an explicit `step=None`, which still means "no step axis":

```python
run.log({"loss": loss}, step=None)   # wall-clock only, as before
```

An explicit `step=i` is unchanged, and now also moves the auto counter past `i`
so mixing the two forms cannot stack a second series on steps already used.
Counters are per metric `kind`, so `log_hw()` never shifts the training curve.

**`run.span()` returns `SpanHandle`, not `str`.** It subclasses `str`, so
comparison, formatting, dict keys and `id=` passthrough are unchanged, and
`copy`/`deepcopy`/`pickle` degrade it to a plain `str`. Only `type(x) is str`
breaks; use `isinstance(x, str)`.

**`client.run()` can now create its parents, but only via `hypothesis=`.** In
0.16.0 it always raised on an unknown slug. Passing `hypothesis=` creates the
experiment (and its project); omitting it is unchanged and creates nothing. A
slug that is a near-miss of an existing one is REFUSED rather than created.

This is SDK-only. **`probe run start` never creates**, on any path — on the CLI
the slug is hand-typed on every invocation, which is where typos come from. Use
`probe project create` / `probe experiment create` there.

### Added

- **Module-level API**: `probe.init()` / `probe.log()` / `probe.log_hw()` /
  `probe.log_artifact()` / `probe.span()` / `probe.finish()` /
  `probe.active_run()`. Logs from anywhere without threading a handle through
  call frames. The binding is a contextvar over a process default, so worker
  threads find the run while a scoped `init()` shadows rather than hijacks. A
  script that exits without `finish()` is closed as `completed` / `failed` /
  `canceled` instead of waiting for the crash reaper.
- **`run.span()` is a context manager**: `with run.span("rollout") as span:`
  stamps both timestamps from one clock, auto-nests children, and closes with a
  terminal status even when the body raises. Spans have no heartbeat and no
  reaper, so one abandoned by an exception previously stayed `running` forever.
- **`client.compare()`**: N runs read back aligned on a shared step axis,
  labelled by petname, with `None` holes rather than truncation to the shortest.
  `.to_pandas()` if pandas is installed; no new dependency.
- **`run.log()` accepts any value type.** Numbers (and bools, numpy scalars, 0-d
  tensors) become metric points; strings, dicts, lists and `None` go to that
  step's record. Previously one non-numeric key raised out of the training loop
  *and* discarded every numeric metric in the same call.

### Fixed

- A non-numeric value in `log()` no longer takes its numeric neighbours with it.
- `log()` no longer reports a spooled metric write as confirmed when the same
  call also wrote a step record.
- `log({})` no longer consumes a step index.
- Span attributes go through the same JSON-safety pass as metrics, so an
  unserialisable value warns instead of raising inside the training loop (and no
  longer displaces the body's own exception on the way out of a `with` block).
- `run.step()` forwards `strict=`; it used to swallow it.
