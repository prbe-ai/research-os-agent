# Changelog

## Unreleased

- **The wizard offers past Kimi Code sessions for import.** "Import past coding sessions" asked
  about Claude Code, Codex and pi only, from a list kept by hand, so Kimi Code's saved sessions
  were never offered even though the importer reads them. The step now asks about every agent
  Probe captures (the harness registry), so the next one is offered without an edit.

- **The SDK no longer says disconnecting a W&B account keeps what it imported.**
  `Client.disconnect_wandb_account` was documented as "retaining imported records", but the server
  has deleted every run an account's sources imported on disconnect since 09-21 (#1772). Its
  docstring, and those of `detach_project_wandb_source` / `detach_experiment_wandb_source`, now
  say what happens: the imported runs and any work attached to them are permanently deleted; runs
  created in Probe and the destinations stay; pausing a source keeps its runs. Docstrings only, no
  behaviour change.

## 0.213.0

- **Nothing in the SDK, the CLI or the MCP addresses an experiment as a project any more**
  (light experiments X21b; needs a server with the X21b routes deployed). An experiment's W&B
  sources are listed, attached, paused and detached at
  `/v1/projects/{P}/experiments/{E}/wandb-sources` (`list_/attach_/patch_/detach_experiment_wandb_source`
  take an optional `project_id` that saves the one `GET /v1/scopes/{E}` placing it; `probe backfill`
  passes the reviewed project); their history imports stay at the project's address, which
  answers the same job. A run moves into an experiment by `experiment_id`:
  `Client.move_run(run_id, experiment_id=E)` (exactly one of `project_id` and `experiment_id`;
  an experiment by its uuid), and `probe run move --to <experiment>` sends it that way. Against
  a server older than X21b the move answers with the run where it was and is refused
  (`CapabilityUnavailable`), as before; an older server 404s the W&B routes (`CapabilityUnavailable`).
- **The Probe daemon records an experiment as an experiment** (tap 0.9.8). The daemon read and
  wrote an experiment at its old project address (`/v1/projects/{experiment}`), which the server
  keeps answering only until the old dashboard is retired. It now asks where each project or
  experiment it works on lives (`GET /v1/scopes/{id}`) and reads and writes an experiment on the
  experiment API: its details and main document at `/v1/projects/{project}/experiments/{id}`,
  its notes, files and uploads beneath that. A project keeps its address. Three proposals change
  for an experiment: a description or a tag is held (an experiment's description is its question,
  and its tags are read-only, so the server refused both; a name is still given while it reads as
  its slug), and a paper is recorded on the
  experiment's project (papers belong to projects; the server refused one on an experiment). A
  run that a session's sweep script printed is again recognised as the session's when it sits in
  an experiment the session worked in (a run names its project and its experiment since the
  experiment split, and only the project was compared). A write an older daemon queued at the old
  address still goes there.

## 0.212.0

- **Codex: you are told when Probe's own hooks are switched off** (tap 0.9.7). Codex runs a
  plugin's hook only after you approve it in `/hooks`, asks again whenever the hook changes, and
  says nothing in the session meanwhile. The next Probe plugin release changes Probe's hooks once
  (one fixed hook per event from then on), so Codex holds them until you approve. While it does,
  the tap prints one line ("type /hooks and approve the Probe Research hooks") on up to three
  prompts of each new session, and stops as soon as they run or you switch Probe off. A session
  started before the update is never told. `probe doctor` gains a "Codex hooks" section from
  Codex's own hook list whenever Codex is installed: how many of Probe's hooks Codex will run,
  which are waiting for approval, and any unexpected warning Codex shows for them.
- **Codex: the tap's SessionEnd wait ends inside Codex's 3s cap** (tap 0.9.7). Codex 0.159 cuts
  every SessionEnd hook off at 3s (and says so in `/hooks` as "1 issue loading hooks"), but the
  tap's wait for the daemon's last delivery ran to 15s under Codex and was killed mid-wait. It
  now ends at 2.7s under Codex. The hook keeps asking for 20s, so Codex's notice stays: Claude
  Code enforces each hook's own timeout, and a container that raises its exit budget still gets
  the full 15s wait.

- **Probe's hooks run through one fixed hook per event, so updates stop switching them off in
  Codex.** Codex runs a plugin's hook only after you approve it in `/hooks`, and asks again
  whenever the hook changes; 21 of the last 23 changes to Probe's hooks did that, silently. Probe
  Research and Probe Research (daemon) now register one hook per event, with no tool filter, each
  running `hooks/dispatch.sh`, and those never change; what runs behind them, and on which tools
  (`hooks/routes.json`), can change in any release with no approval. **Codex asks you to approve
  Probe's hooks once more after this update**: type `/hooks` and approve the Probe Research hooks
  (the session-capture plugin tells you while they wait, and `probe doctor` shows it). Every hook
  that ran before still runs, with the same tool filters, timeouts and environment; hooks for one
  event still run in parallel, a lone guard runs in the same process, and a call to a tool no
  Probe hook watches stops after a 12ms look at the tool's name. Measured on Linux: a Bash
  call's check before it +5ms, after it +33ms; any other tool call +12ms before and after. Kimi
  Code's hooks are unchanged.

## 0.211.0

- **An experiment's children are reached through the experiment API too** (light experiments
  X21; needs a server with research-os #2282 deployed: against an older one these calls raise
  `CapabilityUnavailable` naming it). Apart from its W&B sources, nothing in the SDK, the CLI,
  the MCP or the Probe daemon addresses an experiment's children at its project address
  (`/v1/projects/{E}/...`) any more, so they keep working when the server removes that address
  (R6):
  - Runs (`create_run`, `probe.init`, `run()`, forks, `run child`, offline replay), groups
    (`create_group`, `list_groups`), lineage (`experiment_edges`, and the new
    `experiment_lineage`), manifests (`experiment_version`, `list_experiment_versions`,
    `get_experiment_version`), `experiment_reproduce`, `get_experiment_code`, files
    (`list_experiment_artifacts`, the experiment anchor of `upload_file`, references and
    listings), sub-notes and notes writes go to `/v1/projects/{P}/experiments/{E}/<child>`.
    `create_run`, `create_group`, `list_groups`, `experiment_edges`, `experiment_lineage`,
    `experiment_version`, `list_experiment_versions`, `get_experiment_version`,
    `experiment_reproduce`, `get_experiment_code`, `list_experiment_artifacts` and `list_anchored`
    take an optional keyword `project_id=`; the notes, sub-note, upload and reference calls place
    the experiment by its id. Without a project the experiment's is found once per client (`GET
    /v1/scopes/{E}`, or any read that already placed it) and remembered. A 404 or 410 at a
    remembered OR supplied project (the experiment moved, or that project is in the trash) asks
    again once and, if the experiment is elsewhere, sends the call there.
  - A write that is QUEUED (async writes, `probe --async artifact add --experiment`,
    `enqueue_artifact_reference`) is journaled by the experiment alone
    (`/v1/projects/~/experiments/{E}/...`) and placed under its project when it drains: queueing
    needs no network, and a move before delivery does not dead-letter it. A drainer older than
    this release sends such an op to a 404 and dead-letters it.
  - An experiment's file rows (listings, uploads, references) now name the project in
    `project_id` beside the experiment in `experiment_id`, as the experiment API answers them
    (they used to say `project_id = E`).
  - The experiment's `document` and its notes' headroom (`notes_limit_chars`,
    `notes_remaining_chars`) come from the experiment detail: `get_experiment_document` and
    `probe notes show` on an experiment read `GET /v1/projects/{P}/experiments/{E}`.
    `get_experiment_leaf` (0.209.0) is now that same read. `update_experiment` sends the name,
    question and document in ONE `PATCH`, so the edit lands whole or not at all (no more
    `DocumentNotWritten` from an edit; a create still writes its document right after), and a
    document-only edit returns that write's answer rather than a re-read of the experiment.
  - An experiment's notes replace is `PATCH /v1/projects/{P}/experiments/{E}` with `notes` and
    `base_version` (or `force`); the experiment API's body has no `op_key`, so it is not sent.
  - `Reader` (service tokens): `experiments()`, `experiment()` and `groups()` read the experiment
    API (`experiments(tags=...)` is refused: an experiment's tags are kept read-only).
  - The MCP's experiment lineage view, its note-history pin and its summary view use the
    experiment API.
  - `probe run move --to <slug>` places a project or experiment slug with one `GET
    /v1/scopes?slug=`.
  - The Probe daemon's delete preview for an experiment is `DELETE
    /v1/projects/{P}/experiments/{E}?dry_run=true`, and `experiment create`, `experiment set` and
    `experiment move` are retried with their Idempotency-Key again: the server names those routes
    since X21.
  - Still at the project address: an experiment's W&B sources (the server has no experiment twin
    for them yet), and the tap's companion worker's context reads (they move with a tap release).
  - Regenerated models: `probe.models.CitationGraphState` no longer lists `disabled` (the server
    dropped it at C13), so `PaperCitationsOut` / `CitationGraphOut` refuse it; `probe paper
    citations` / `graph` read `state` raw and still print a line for an older server's
    `disabled`. The experiment detail's `summary` is nullable (a service token gets null), and
    `CaptureSource` gains `kimi_code` (already served; the checked-in schema lagged).

## 0.210.0

- **Kimi Code is a supported coding agent.** `probe wizard` installs Probe's plugins into Kimi Code
  (`--agent kimi`), and its sessions are captured, recorded inline or by the Probe
  daemon (Who records in Kimi Code), with the guard and held questions. Kimi Code has no plugin
  install command, so the wizard writes Kimi's own plugin list the way `/plugins install` does,
  and never touches a list in a layout it does not know (it prints the `/plugins install` step
  instead). Kimi puts no session id in its shells, so its hooks record which Kimi process runs
  which session and `probe` (and `probe.init()` in a script Kimi starts) finds its session by
  walking up to that process. Needs Kimi Code 2.1.1 or newer; `probe doctor` shows the version and
  whether Kimi's plugin list is managed. Each plugin version gets its own folder, so an update
  never swaps files under a running session. A Kimi session is never treated as bypass: a resumed
  session writes no new permission mode, so held questions are always asked. Past Kimi sessions
  can be imported like Claude Code's and Codex's. The approvals-folder guard now also reads a
  write tool's `path` argument (Kimi's spelling), relative to the session folder.

- **A transcript rewritten in place is never read again from the start (every harness).** The
  daemon used to start over when a chat log shrank, which handed it every earlier event again as
  new (one rewrite replayed all 14 events of a two-turn session), and read on inside unrelated bytes
  when a rewrite kept or grew the size. It now keeps the file's inode and a hash of the 4 KB before
  its cursor and, when two polls in a row see changed bytes (an inode change alone is not
  enough: copies and network mounts change it), stops reading that log with one notice and one line in
  `daemon-errors.log`; the store gains three nullable columns and stays format 1. The capture tap
  stops that session's upload with one clear log line instead of retrying every tick. Kimi Code
  rewrites its wire when it migrates an older one on resume.

- **Tap 0.9.6: Kimi Code capture, and a rewritten transcript is set aside.** The tap reads Kimi
  Code's session logs (main agent only) and, for every harness, logs one line for a transcript
  rewritten under its cursor instead of retrying it each tick. The tap ships from main, so this
  merge is its release; `client-version.json` `tap.latest` moves to 0.9.6 with it.

## 0.209.0

- **Citation commands describe the server as it is now (task C13).** The server removed its
  per-tenant citation switch, so `state: disabled` and the refresh route's 409 `citations_disabled`
  only come from an older server. The SDK docstrings say so; `probe paper citations` and `probe
  paper graph` still print a plain line for `state: disabled` instead of an empty table, and the
  SDK still raises a 409 as `ConflictError`. No behaviour changed.

- **The SDK, the CLI and the MCP speak the experiment API** (light experiments R4, X15).
  An experiment is read and written as an experiment, under the project it is filed in, and no
  longer through its project address:
  - `create_experiment` posts to `POST /v1/projects/{project}/experiments`; `get_experiment`,
    `update_experiment` (name, question), `delete_experiment` (a move to the trash, with
    `dry_run=` and `reason=`) and `list_experiments` use `/v1/projects/{project}/experiments[/{id}]`.
    When only the experiment's id is known, `GET /v1/scopes/{id}` finds its project; when only its
    slug is, `GET /v1/scopes?slug=` does (one request, tenant-wide).
  - New: `move_experiment` (`probe experiment move <exp> --to <project>`: its runs, files and
    groups move with it), `get_scope`, `get_scope_by_slug`, `get_project_workspace` (the T16
    read: one scope's experiments, run ids, question, summary and files),
    `get_experiment_overview`, `get_experiment_document` and `get_experiment_leaf`.
  - `list_experiments(project_id=...)` sends the project in the path. It used to send it as a
    `project_id` query parameter that `GET /v1/projects` never declared, so the filter was
    silently dropped and every project's experiments came back (so `probe backfill`'s file count
    for one project also counted other projects' experiments). Without a project it now reads
    every project's list, one request each, so naming the project is faster. Its `cursor` is now
    an offset token; a cursor from an older client is refused.
  - `resolve_experiment(slug, project_id=...)` is one request; without a project it is two
    (`GET /v1/scopes?slug=`, then the experiment). Only against a server older than that route
    does it read every project's list (8 at a time, stopping once found). A slug an experiment
    had before a rename (`legacy_slug`) now resolves too, after every live slug, as it does on
    the server. The CLI looks in the active project first when it is stored as an id (one
    request). The near-miss guard still reads the whole list, but once per `probe.init()` /
    `run()` / `ensure_experiment` call, not twice, and it skips a project trashed mid-read.
  - A create (or an edit) with `document=` is two writes, because the experiment API has no
    document field. If the second fails, `DocumentNotWritten` names the experiment that now
    exists and the command that finishes it (`probe experiment set <slug> --summary ...`).
  - Only the trash notice reads as "not there" (`RosError.in_trash`); any other 410, a
    `client_too_old` refusal included, is raised.
  - Create-or-get (`ensure_experiment`, `probe.init(question=...)`) adopts a slug's existing holder
    only when it is an experiment in the same project. A slug held by a project, or by an
    experiment in another project, is an error that says which.
  - Rows keep the old vocabulary beside the new: `project_id` and `parent_project_id`, `question`
    and `description`, `kind: "experiment"`. The experiment read's overview status is
    `overview_status` (the API calls it `summary`, which on the project address meant headline
    metrics). The experiment read carries no `document`, `tags`, `repo`, `sessions` or
    contributors: read the document with `get_experiment_document`.
  - **Retired:** an experiment's tags are read-only since experiments moved to their own record
    (the server has refused tag writes on them since the R3 switch). `probe experiment tag`,
    `experiment create --tag`, `experiment set --add-tag/--remove-tag/--set-tags` and
    `experiment list --tag` say so and exit; the SDK raises `ValueError` for `tags=`,
    `metadata=` and `summary=` on an experiment before sending anything. `probe.models.ExperimentCreate`
    is now the experiment API's create body (`ProjectExperimentCreate`: no `project_id`, the
    project is the path).
  - Still at the project address, because the experiment API has no route for them yet (the
    server keeps serving them to this release until R6): an experiment's `document` write and
    read; its runs (`POST /v1/projects/{E}/runs`), groups, lineage edges and lineage view, files
    and uploads, notes writes, notes history and sub-notes, the CLI's notes read for an
    experiment (it carries the notes' headroom, which the experiment read does not), versions,
    reproduce, code and W&B
    sources; a slug that names an experiment where a project is asked for (`run move --to`,
    `edge add`); `list_projects(parent_id=)`, which lists an experiment as a child; the daemon's
    delete preview; and the read-only `Reader` (service tokens reach only allowlisted reads, and
    the experiment API is not on that list).
  - A `client_too_old` refusal (410) now always ends in how to upgrade
    (`pip install -U probe-research`), naming the release that fixes it.
  - The Probe daemon's own HTTP calls (delete previews) and the tap's daemon worker now send the
    client pair (`X-Probe-Client`/`X-Probe-Client-Version`) like every other first-party client.
  - The MCP's experiment card, `research_context`, runs-of-an-experiment and run handoff read the
    experiment through the experiment API; its `summary` view reads the document separately.
  - **Release note for pinned jobs.** Once the server turns on its refusal (not before 7 days with
    no active older client, and at the latest 90 days after this release), an SDK or CLI older
    than 0.209.0 that addresses an experiment through its project address gets 410
    `client_too_old`. A pinned job that calls `probe.init()` on an experiment then fails at init
    with `ClientTooOldError`. A job already running keeps training and keeps logging: run-scoped
    writes are never refused. Upgrade: `pip install -U probe-research`.

- **Tap 0.9.5: the daemon worker names itself on every server call.** Its requests to the Probe
  API (the companion gateway and the entity reads it makes for the daemon) now carry
  `X-Probe-Client: tap` and `X-Probe-Client-Version: 0.9.5`, like every other first-party client.
  The server reads a key request that names no client as an older CLI, and once it turns on the
  light experiments' refusal (R4) it would answer such a request with 410 `client_too_old`
  wherever it reaches an experiment by its project address; a request that names the tap is never
  graded by that refusal. Nothing else changes. The tap ships from main, so this release is the
  merge; `client-version.json` `tap.latest` moves to 0.9.5 with it.

- **Tests prove the hosted MCP's experiment reads give the same answer in both storage shapes**
  (light experiments, task X9). Until the server's R2 job, an experiment's runs, files and groups
  are stored at the experiment's own id. After it, they are stored at the project with an
  `experiment_id` label. `test_mcp_experiment_shapes.py` stores one tenant both ways behind a fake
  server that answers by the R1 server's rules: dual-shape reads, with every row served in the leaf
  shape (an experiment's file says `project_id = experiment_id = E`) until R6. It runs 17 MCP reads
  against both stores -- the experiment card, a bare id, files, groups, the group card, lineage,
  notes, summary, a run's card and handoff (the run's question), runs lists, browse and
  `research_context` -- and every answer is byte-equal across the two. Every experiment file row
  names its experiment in `experiment_id`. No MCP code changed: no MCP read decides anything from
  where a row is stored.

## 0.208.0

- **Read a paper's citation links from the CLI and the SDK.** `probe paper citations <paper id>`
  lists every link the server read from that paper's bibliographies, each with its proof: the
  bibliography it came from, the entry number, and whether the id was printed in the entry or
  deposited by the publisher (entries with no id are shown as text and never become a link).
  `probe paper graph --project <ref>` shows a project's papers, the works they cite or are cited
  by that the project has not recorded (ranked by how many of its papers link to each), and the
  `cites` edges between them. Both print a table, or the server's answer with `--json`, and say so
  plainly when a team's citation links are not switched on yet. SDK: `Client.citation_graph`,
  `Client.paper_citations` and `Client.refresh_paper_citations` (a 202 that is refused with 429 and
  `retry_after` within an hour of the last fetch). Needs a server with the citation graph.
- **`provider_citation` says what it means.** The `--via-provenance` and `edge add --provenance`
  help now say it means you found the paper in a reference list, a record of your path through the
  literature, and point at `cites` links for the fact that one paper's bibliography names another.

## 0.207.0

- **The `inference` project kind is now `evaluation` in the CLI, SDK, agent rules and skills.**
  The server made `evaluation` canonical (frozen weights: sweeps, ablations, evals). `probe
  project create --kind` and `probe project move --kind` offer `evaluation`, and help and the
  choice list in a usage error name only the five current kinds. `--kind inference`
  is still accepted and sent as `evaluation`, because installed skills and agents' habits still
  type it. Code that builds `probe.models.ProjectCreate`/`ProjectPatch` itself must pass
  `evaluation` (the generated enum no longer has `inference`); `client.create_project` still sends
  either spelling unchanged. The generated `ProjectKind` enum says `evaluation`. The managed block in CLAUDE.md and
  AGENTS.md moves to v37 (its kind list says `evaluation`), so the next update rewrites it. The
  track-work skill's kind table and description say `evaluation` in the plugin, the pi package
  and the tap's daemon rules. A CLI older than this one refuses `--kind evaluation` locally, and
  its error lists `inference`, which the server still takes.
- **Exiting Claude Code no longer prints "SessionEnd hook [...] failed: Hook cancelled"** (tap
  0.9.4). Claude Code gives SessionEnd hooks 1.5s in all at exit unless
  `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` (or a SessionEnd hook in your own settings) says
  longer, and a plugin's own `timeout` does not count. The tap's hook waited up to 15s for its
  daemon's last delivery, so Claude Code cancelled it on most exits. Under Claude Code the wait
  now ends inside that budget, about 1.2s by default, and the hook exits cleanly. On a laptop the
  daemon finishes on its own after Claude Code exits. In a container or CI job, where the exit
  can take the daemon with it, set `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS=16000` so the
  session's last delivery gets the full 15s. Codex keeps its 15s wait.

## 0.206.4

- **The daemon is told why a long command was refused.** A refused shell command over 300 characters
  said only "a question shows the researcher at most 300", never what the check refused, so the
  daemon shortened a note write whose shape was the problem (a `probe notes push` after the
  heredoc) until the loop detector cut its run short: 89 of these in 30 days. The refusal now
  leads with the check's reason, and a long note write in another shape names the one that runs.
- **Fix (P1, #2227): a refused heredoc no longer skips the daemon's every-mode refusals.** The
  note-write check ran first, so text after a note (or a heredoc in another shape) was never
  checked against the rules no mode waives: in bypass mode a write to Probe's own config placed
  after a note's text ran. The whole command is now checked first.

## 0.206.3

- **`search_knowledge` no longer describes a top-up that is gone.** The knowledge engine stopped
  padding answers with raw-pool documents (prbe-knowledge, 2026-10-02): a search returns what its
  selector picked, however few, and the raw pool answers alone only when no selector could. So
  `curated_only` has nothing to switch off. It is still accepted, now marked deprecated with "no
  effect", and dropped from the tool's parameter list.

## 0.206.2

- **Updating Probe updates pi's Probe package too, and pi auto-updates.** The wizard's Update and
  every automatic update said "pi: not managed by this CLI" for pi and skipped it for every other
  agent, so pi stayed on the package it was installed with (a 0.2.x package under a 0.206 CLI never
  switched to the daemon). They now run `pi update` for Probe's package whenever pi has it, and say
  what moved. With Automatic updates on, pi's session start now applies an available update, as
  Claude Code's session-start hook does; every `probe` call pi makes is off a terminal, and the
  update check refused all of them. An update pi's session start asks for runs once that pi exits
  (it rewrites the folder pi loads from). Any update skips pi's step while this user has pi
  running and leaves it owed: pi's next session start retries it after pi exits, even with the CLI
  already current. pi's step runs one at a time, counts a failed one as a failed update in
  `probe doctor`, and treats pi being gone as nothing to do. Not on Windows yet.
- **The wizard's Who records row names an agent still recording itself.** On a machine on the
  daemon it read "daemon" while pi recorded itself; it now says "pi still records itself: Enter
  moves it to the daemon."

## 0.206.1

- **The npm page for `npx probe-research` names no particular coding agent.** Its description,
  keywords and README now say "your coding agents" (the wizard lists the ones it supports), so
  adding an agent no longer needs an npm release. Launcher 0.13.1; nothing it runs changed.

- **Switching a wizard setting saves it.** The main menu's "Probe in new sessions" row saved
  only on Enter, and moving off it put the old value back without a word. Each `←`/`→` press now
  saves it, like the Who records rows beside it; a save that fails puts the row back and says why.
  The Settings screen, the daemon's page and the Defaults picker used to drop what you switched
  when you left with `←` or Esc; leaving now applies it, as `→` does.
- **`npx probe-research` leaves your installed `probe` on the version it ran.** When the installed
  CLI is behind, the launcher runs the latest one in a throwaway environment, and the wizard's
  first step upgrades the installed copy. That reinstall was unpinned, and uv could satisfy it
  from its cache with the same old version: a 0.206.0 wizard left 0.205.4 installed, so the
  coding agents kept calling the old CLI (pi never saw the daemon the wizard had just set). It is
  now pinned to the running version, re-reads the package index, and checks the result: "Updated
  the installed `probe` 0.205.4 → 0.206.1", or the command to run if it is still behind.

## 0.206.0

- **`search_knowledge` stops calling complete answers incomplete.** The hosted MCP marks a search
  `truncated_by_response_budget` only when whole documents are missing from it, not when the
  server cut passage text that search cards never show. Ships with the hosted MCP's deploy.
- **pi capture uses only pi's own token.**
  - pi no longer falls back to the capture token in the probe CLI config. That token is Claude
    Code's, and the server refused it on pi's route, so every pi upload and every pi session
    import failed with a 403.
  - An unpaired pi is now reported as unpaired; pair it in `probe wizard`.
  - A sign-in no longer leaves pi's capture off, and the wizard removes the marker an older
    sign-in left behind, but only while pi holds no token of its own. A deliberate off is
    never touched.
  - Turning Claude Code's capture off now clears its token even when pi is installed.
- **pi can use the Probe daemon.**
  - "Who records" in `probe wizard` now moves pi too. Its Probe package switches to the daemon's
    lean set: only the skill for instrumenting code; the daemon records and reads for pi.
  - Before the switch, the wizard pairs pi's own session capture (one browser approval) if needed,
    and updates pi's Probe package (`pi update`) if it is older than 0.3.0. If either cannot
    happen, pi stays on agent and the wizard says what to run.
  - New pi sessions start on the daemon, and a resumed session follows the current setting.
  - On a machine where the other agents already record through the daemon, Enter on Who records
    brings pi along; nothing moves on its own.
  - `probe doctor` shows who records for each agent and pi's package version.
- **pi's extension (0.3.0) is ready for the Probe daemon.** Ships when it merges; it changes
  nothing until a CLI release reports the daemon profile for pi.
  - In the daemon profile pi gets what Claude Code's lean daemon plugin gives: only
    `instrument-code` and the daemon's `probe` skill, no Probe MCP tools, and no daemon messages
    to the agent. `probe` commands other than `ask`, `session status`, `run expect`, `doctor`
    and a run's own data are refused before they run. When the daemon stops recording, you get
    one pi notification.
  - The daemon's questions (a delete of a teammate's work, a command off the safe list) open in
    pi's own yes/no dialog, word for word. Esc asks again at your next prompt. Without a UI,
    answer with `probe approvals`. A tool call from pi's agent that would write an answer itself
    is refused.
  - The team note reaches pi's prompt, and syncs from pi, only while pi's `AGENTS.md` has
    Probe's block. Declining the rules in the wizard now keeps it out of pi too.
  - pi's footer reads `on (daemon)`, like every other status line.
  - Fixes found running real pi 1.0.0 (these affect pi with Probe on main today too):
    - A new interactive pi session is captured now. pi 1.0 writes a session's file at the first
      message, after the extension tried to start capture once and gave up, so the session was
      never uploaded and the Probe daemon never started for it. Capture now starts at the first
      tool call or the end of the first turn.
    - `pi -p` (print mode) answers again with Probe's block in pi's `AGENTS.md`. It used to exit
      with no output while the team-note sync ran.
    - A `probe ask` answer arrives while pi is idle instead of at your next prompt, and the
      footer is redrawn as each turn ends.
    - The footer shows pi's own capture state when `PI_CODING_AGENT_DIR` or
      `PROBE_PI_TAP_PLUGIN_DIR` moves pi's folders, instead of "not installed".

- **Releases and the public mirror read the harness registry.** `release.yml` bumps plugin
  versions through `agent/tools/release_bump.py`, which moves every released plugin's manifest for
  every hook-plugin harness in the registry: today the same four `plugin.json` files it named by
  hand. The mirror render takes its marketplace files and pi's package dir from the registry
  (new `plugin.marketplace` and `package_dir` fields); its output is byte-identical. The literal
  ratchet counts the ids and env markers the registry lists, and the Makefile's skill syncs read
  `agent/skills/profiles.json`. `agent/docs/adding-a-harness.md` says how to add the next harness,
  and which files still name harnesses by hand. No behaviour change.
- **The npm launcher's description and keywords name pi** beside Claude Code and Codex
  ("Setup wizard for Probe Research in Claude Code, Codex and pi"). `release.yml` publishes the
  launcher only when `agent/npm/package.json`'s version moves, which this does not do, so npm
  shows it from the next launcher version on.
- **Notes can be added to and corrected without a file.** `probe notes append --text "..."` adds
  a paragraph to the end of a project, experiment, run, group or artifact note (or a sub-note with
  `--note`). `probe notes edit --old "..." --new "..."` replaces one exact piece of text; it
  writes nothing and exits 1 when `--old` matches nowhere or more than once. Text is always
  literal, never a file or stdin. Both keep what a teammate wrote in the meantime: the note is
  re-read and the change applied again. `--team` writes the team note the same way, for a
  caller without the team-note file (a session that has the file keeps editing it and syncing).
  The Probe daemon may not pass `--team`: its team-note changes still go through the file, where
  the researcher sees each one first.
  - Nothing already in a note changes except the text you named. A note holding text the
    credential scrubber would rewrite (written before the server scrubbed, or caught by a newer
    rule) is refused, exit 1: review it with `probe notes checkout` and `push`. So is new text
    whose scrub would rewrite the rest of the note, and an `--old` the scrubber would rewrite.
  - A write whose reply is lost, or is not Probe's, is never sent again. The note is read back
    instead: the write landed (exit 0), or exit 1 with what is known. After a timeout or a
    502/504 an unchanged note is "not there yet": Probe may still be applying the write for a
    few minutes, so read the note before running it again. Only when Probe itself answered (a
    500/503, a redirect, a page that is not Probe's) does unchanged mean nothing was written. A
    note read without its version is refused rather than overwritten.
- **`probe views update VIEW` changes a view's expression**, not just its name (`--spec`,
  `--spec-file`, `--name`). With `--expected-updated-at` it refuses an edit made since you read
  the view and prints the view as it is now.
- **`probe run set --status` corrects a finished run's status and keeps its times.** `probe run
  end --status` stamps the end time now, which rewrites a finished run's duration; `run set`
  sends only the status. It takes `completed`, `failed`, `crashed` or `canceled`; `failed`
  emails the run's launcher. `running` and `created` are refused: on a finished run the reaper
  would later mark it crashed.
- **`probe project set` and `probe experiment set` change tags in the same write**
  (`--add-tag`, `--remove-tag`, `--set-tags`), so renaming and retagging land together or not at
  all. `probe project set` with nothing to change now says so instead of crashing.
- **The MCP reads what the dashboard assistant could, for every agent.** `browse` takes a `mode`:
  `runs` (every run filed under a project, newest first; `active=true` for what is running
  now), `workspaces` (names for a `workspace_id`), `notes` (the notes catalog, searchable
  with `query`) and `files` (the Shared folder, or one workspace's), beside the default
  `tree`; a listing resumes after the last row it sent, so a row that ends or is deleted
  between pages skips nothing. `entity` gains a run's saved `views`, its sandbox `diff`
  (`view_options.trial`) and its `sessions`, an artifact's `sessions`, a project's `readme`
  (saying why when there is no text), and `sub_note:<id>` for one sub-note whole (the
  `notes` view now hands out that address). `metrics` gains `mode="series"`: several runs
  and keys in one read, for comparing curves; a series too big for the budget is refused
  with the budget it needs, and only a series the downsample actually cut is labelled
  sampled. Ships with the hosted MCP's deploy.

## 0.205.4

- **Daemon recording is no longer described as paid-only.** Every plan can run the Probe daemon
  now (the server decides; a free team's daemon stops at $500 of model spend a month and hands
  recording back to the coding agent). The wizard and the daemon drop their "on paid plans"
  lines; a team the server closes the daemon to still hears why.
- **`--no-agent-rules` is obeyed everywhere.** Declining the rules now keeps every Probe block out
  of the global `CLAUDE.md` / `AGENTS.md`. Three paths ignored it. Switching Who records to the
  daemon wrote the daemon's block into files that had none. `--agent-rules/--no-agent-rules` passed
  beside `--who-records` was accepted and ignored. The team-note sync wrote its block into every
  harness's file after each session, opted out or not, and created `~/.codex/AGENTS.md` on
  machines without Codex. Opting out now removes the team-note block as well as the rules, and
  the next sync takes out a note left behind by an earlier opt-out. A machine already on the
  daemon cleans up with `probe wizard --who-records daemon --no-agent-rules --yes`.
- **The Who records switch names the agents it leaves behind.** pi has no daemon profile yet, so
  it keeps recording itself; the switch now says so instead of listing only Claude Code and Codex.

## 0.205.3

- **The wizard says less.** Every screen's text is cut to short, plain lines: the main menu,
  install, Defaults, Who records, Settings, the account screens, uninstall and sign-out, imports and
  the finish screen. The main menu's key hint fits on one line (`↑ ↓ move · enter choose · esc
  back`), and "Update to the latest version" is now "Update Probe". The facts stay: what leaves the
  device, where daemon deletes go, what read and off stop, and what sign-out revokes.

## 0.205.2

- **Switching Who records to the daemon works after an update.** When the installed `probe` is
  behind, `npx probe-research` runs the newest wizard from a temporary copy in uv's cache. The
  switch checked that copy for the daemon's AI libraries (never there) and refused to install into
  it, so it ended "The Probe daemon's AI libraries are missing, so nothing moved" until Probe was
  installed again. It now checks the installed `probe`, the copy the daemon runs from, and
  installs any missing libraries into it with that copy's own `probe daemon install`.

## 0.205.1

- **The daemon says why it was turned away when the cause is the team, not its key.** A 403 from
  the daemon's model route used to always read "the daemon's key was refused … Enter on Who records
  approves a new one", but two of its three causes refuse a new key the same way:
  `companion_paid_plan_required` now reads "daemon recording is on paid plans, and this team's plan
  does not include it", and `companion_disabled` "daemon recording is turned off for this team",
  each saying a new key will not change it. Only `companion_credential_required` (the saved key is
  not the daemon's) still points at Enter on Who records. The daemon still steps aside and the agent
  records in the meantime.

## 0.205.0

- **Setup goes through the wizard only (release 1 of 2: nothing is removed yet).** Every message,
  hint and doc that told someone to run `probe login`, `probe install`, `probe update`, `probe
  daemon install`, `probe companion authorize`, `probe statusline install`, `probe mcp token set`
  or `probe context use` now points at the Probe wizard (`npx probe-research`). What those
  commands did now lives in the wizard (the transcript tap moves to 0.9.3 for its own messages):
  - **Sign in only** (a remote GPU box, a self-hosted server): `probe wizard --action login`, with
    `--token`, `--base-url`, `--context`, `--ingest-token`, `--hmac-secret`, `--no-browser` and
    `--endpoint-only`. It signs in and installs nothing; the same flags with any other action, or
    with an install flag or a sign-in code they would silently drop, are refused.
  - **Who records, headless**: `probe wizard --yes --who-records agent|daemon`. It needs a
    signed-in machine, prints the daemon's approval link instead of opening a browser, and leaves
    an agent already on the chosen side alone.
  - **Enter on Who records repairs the daemon**: while it reads daemon, Enter approves a new key
    when the server refuses the saved one (revoked under Connected clients) and installs missing
    AI libraries, before the daemon's page. The daemon's "key refused" and "libraries missing"
    messages point there; `probe companion authorize` always approves a new key again, and exits 1
    unless it got one. A declined re-approval leaves no key, so nothing moves onto a dead one.
  - **Switching to the daemon installs what is missing and only that.** The daemon's key is
    approved only when this machine has none, and its AI libraries install, with no approval, when
    they are missing. A machine that held the key but not the libraries used to refuse the switch
    for "missing packages".
  - **Install asks for the MCP token on its own.** It rode along with the API sign-in, so a
    machine signed in with a pasted token never got one. A headless install missing only that
    token does not wait on a browser; the next interactive Install asks.
  - **Uninstall puts the status line back** to the command it wrapped, or removes it.
  - **The account screen** (`probe wizard --action account`) switches to, and removes, another
    saved account.
  - **Update reads a fresh package index** when it reinstalls, instead of uv's cached copy that
    can predate a release by minutes.
  - **The `npx probe-research` launcher (0.13.0) hands everything to `probe wizard`**: a bare
    code, flags alone (`npx probe-research --agent codex` used to reach the CLI root and exit 2),
    and `install [CODE]` as the guided install (`--action configure`). A call starting with a root
    option (`--base-url X doctor`) still reaches the CLI untouched.
  - **`/probe-research-setup`** runs the wizard's flags throughout.
  The commands themselves still work in this release; the next one removes them.

- **A new daemon session's status line says `(daemon)` from its first frame.** Claude Code draws the
  status line once while its SessionStart hook is still running and not again until the
  conversation moves, so a brand-new session on the daemon showed the agent's `read-only` (or `on
  (daemon degraded)`) until its first prompt. A session not seeded yet now resolves as the hook is
  about to seed it -- who records for Claude Code decides -- and a daemon that holds its key but has
  not taken its first lease reads as starting, not degraded (the prompt hook's grace). A daemon with
  no key still reads degraded.

- **MCP answers drop bookkeeping that only says "this is all of it".** A complete result no
  longer carries `"completeness": {"missing": [], "state": "complete"}`, a last page no longer
  carries `"next_cursor": null`, an `entity` batch whose refs all resolved no longer carries
  `"missing": []`, and an entity with no summary no longer shows `"summary": {}`. The capability
  map (static for the hosted backend; its one False flag, `portable_snapshots`, rode every read)
  leaves the compact envelope. That was ~30 o200k tokens per envelope, repeated per row of an
  `entity` batch, ahead of the content asked for. Partial, `no_match`, any `missing` marker and
  any cursor are unchanged; the server instructions now say `completeness` appears only when
  the answer is partial, `no_match` or names a missing item, and `next_cursor` only when there
  is more, and the cursor docs no longer tie paging to `state="partial"`. `verbose=true` still returns the full envelope. Ships with
  the hosted MCP deploy; a paged JSON read in flight across it restarts once
  (`source_changed`).

- **An `entity` batch row puts the answer one level down: `rows[i].data`, not `rows[i].data.data`.**
  Each row used to wrap its ref's whole response under `data`, so the answer sat in a box inside a
  box. A row is now `{ref, data}`, plus that row's own `completeness` when it is partial and its
  `next_cursor` when that entity has more (a row cut for size leaves it to the top-level cursor).
  Ships with the hosted MCP deploy.

- **A multi-ref `entity` read works at the minimum budget.** At `token_budget: 512`, reading the
  notes of two or more entities could fail before any note arrived. Each page carried its
  ~200-token cursor twice (on the row and at the top level), which left no room for text ("Request
  metadata exceeds this budget"); and when the first note fit but the second did not, the page sent
  had never been measured, came out at up to ~630 tokens, and was refused on every retry. A row
  cut for size (`truncated_by_token_budget`) now leaves the cursor to the top level, and the page
  sent is the one measured. A row that is partial for another reason, or that ends at its fetch
  window, keeps its cursor. Ships with the hosted MCP deploy.

## 0.204.4

- **The wizard's menu breathes under its headings, and Sign out asks first.** Each section heading
  on the main menu now has a blank line under it (folded sections still take one line each). Sign
  out is drawn red, and choosing it opens a confirmation page ("Sign out of Probe on this
  device", naming the account) with "Stay signed in" first; staying goes back to the menu with
  nothing changed. `probe wizard --action logout --yes` still signs out without asking.

- Development dependencies only; nothing an installed CLI, SDK or plugin runs changes.
  `uv.lock` takes cryptography 50.0.1 (GHSA-g6cj-pr64-35w5), and the pi plugin's lockfile takes
  vitest 4.1.11 (was 3.2.7; GHSA-82fw-gwwq-j7x9), qs 6.16.0, fast-uri 3.1.8 and ip-address
  10.7.2.

- The pi plugin is now typechecked and tested against pi 0.86.0 (devDependency, was 0.84.3),
  whose locked undici is 8.10.2 (was 8.9.0; GHSA-3wwx-pv8p-q78v). Development only: the
  plugin's runtime dependencies and the pi versions it works with are unchanged.

- Development dependencies only — `agent/uv.lock` is not what installed CLIs use. Its pyjwt
  entry moves 2.13.0 -> 2.14.0 (GHSA-w6j9-cwv2-h6wq); no pyproject.toml in this repo pins pyjwt,
  so an installed CLI already resolves 2.14.0 on its own without this change.

- **Switching Who records reports `wizard.recorder_changed`.** Every switch of the main menu's
  Who records row sends one event: `recorder` (`daemon` or `agent`), `outcome` (`moved`,
  `partial`, `failed`, `refused_plan` when the team's plan does not include the daemon, `no_key`
  when the daemon's browser approval did not complete), the agents that ended on the chosen side
  (read back from the saved config), `agent_count` and `duration_seconds`. Enter on the row sends
  nothing. `PROBE_TELEMETRY=off` still turns it off.

## 0.204.3

- **Switching Who records shows the spinner instead of a blank screen.** Moving the main menu's
  Who records row to the daemon checks with the server whether the daemon is open to your team,
  then moves your coding agents' plugins; both now run under the wizard's working indicator
  ("Checking the Probe daemon is open to your team", "Moving your coding agents to the daemon").
  The daemon's sign-in (its browser approval) still runs on its own screen, before the move.

## 0.204.2

- **Who records is a toggle on the main menu, next to the default for new sessions.** Defaults now
  opens with `Who records ‹ agent ›`: `←`/`→` switch it between the agent and the Probe daemon for
  every coding agent on this device at once (no Claude Code / Codex split), and the switch applies
  right away, no Enter. Switching to the daemon asks for the daemon's own sign-in when it has no key
  yet (one browser approval), moves the plugins, and opens the daemon's page, which asks whether the
  daemon sees the agents' reasoning; Enter on the row while it reads `daemon` opens that page again.
  A team the daemon is not open to is told so on the switch and nothing moves. Settings is back to
  automatic updates only.

## 0.204.1

- **The daemon's "Who records" row is in the wizard for every team the daemon is open to, with no
  flag.** `probe wizard` › Settings › Who records (the agent or the Probe daemon, per coding agent)
  used to show only under `probe wizard --experimental`, which contradicted the paid-plan rollout:
  now the server's answer is the only gate (a free team's NO hides it, an unknown answer offers
  it, and a machine already on the daemon always sees it). `--experimental` is still accepted and
  does nothing. The switch's refusal and the reasoning row's hint now point at `probe wizard` ›
  Settings.

## 0.204.0

- **The daemon can see why the agent chose what it did, in Claude Code and Codex.** Both agents write
  their reasoning to the chat log only as a summary, and only when a setting asks: Claude Code's
  `showThinkingSummaries` (else each thinking block is an empty string) and Codex's
  `model_reasoning_summary` (its default wrote none in our tests). So the daemon saw what
  the agent did but never its reasons. Moving an agent's "Who records" to the daemon now turns its
  summaries on where you never chose (`true` in `~/.claude/settings.json`; `"detailed"` at the top of
  `~/.codex/config.toml`, where `auto` counts as not chosen), and says so; moving back or
  uninstalling puts back what was there, only if Probe's value still is. A value you set yourself
  (`false`, `"none"`, `"concise"`) is never changed. One new Settings row under Who records, "Daemon
  sees the agent's reasoning", sets it for every agent set up on the device, and `probe doctor` shows
  it for each agent the daemon records. The summaries also show in your terminal and are uploaded
  with the session; new sessions only (headless `claude -p` needs `--thinking-display summarized`).
  On a replay bench whose reasons lived only in thinking, the daemon recorded them in 5 of 7 runs
  with summaries and 0 of 7 without.

## 0.203.0

- **The daemon is chosen in the wizard, and the switch is on / read / off everywhere.** `/probe
  daemon`, `probe session state daemon` and `probe session default daemon` no longer move anything:
  each answers that the daemon is set in `probe wizard --experimental` › Settings › Who records
  (the CLI exits 2). The wizard's "Probe in new sessions" row is on / read / off; the daemon's key
  is minted and revoked by the Who-records row alone (kept while a session it records is still
  open). Where the daemon records, `on` is stored as
  `daemon` as before, and the states now read `on (daemon)`, `read only (daemon)` and `off` — on the
  status line (`tracking (daemon)` is now `on (daemon)`), in `probe session state|status|toggle`
  (a new `label` field; `state` keeps its word for older readers) and in the flip notices. The
  daemon profile's lean plugin gets the switch: a `probe` skill and the guard on every prompt, so
  `/probe on|read|off` works there too. In `read only (daemon)` the daemon's reader keeps answering
  `probe ask` and sending `[Probe]` messages while its writer records nothing; turns spent there
  stay unrecorded after the switch is back on, and the writer is told which. A machine or folder
  default still reading `daemon` starts new sessions `on`, recorded by whoever Who records names;
  a session already stored as `daemon` keeps it until the switch moves. pi's `/probe daemon` moves
  nothing too. Capture (probe-research-tap) 0.9.2 keeps the daemon's worker running in `read only
  (daemon)` for its reader.

## 0.202.2

- **The wizard's first section is called `Install/update/uninstall`,** with its rows in that order:
  Install, Update, then Uninstall last, so the cursor reaches the one that removes everything only on
  purpose.

## 0.202.1

- **The setup wizard opens in about a second instead of nine.** "Checking what's installed on this
  device" used to ask the server seven things one after another on a two-agent machine: who you
  are, your unfiled runs and your capture key once PER AGENT, plus a separate manifest fetch. It now
  asks once (`POST /v1/device-state`), while it checks the local tools, and asks again only after a
  step that changed something (signing in, installing, uninstalling). A server that cannot check a
  credential no longer reads as a refusal, so a slow or flaky API never sends you back through
  sign-in. Each capture key goes only to the server its own uploader sends to, and the request
  never follows a redirect. On an older server without the route (or a proxy answering for it)
  the wizard asks the old way. Two local waits are gone too: the wizard
  no longer runs `probe --version` on itself (0.9 s) or imports the daemon's AI library just to
  print its version (1.5 s).

- **The wizard runs in a window of its own, and the arrow keys stay in it.** Every prompt was
  meant to take the whole terminal but never did (the full-screen flag was set after prompt_toolkit
  had built its renderer), so the wizard drew inline, and Warp scrolled its own blocks when ↑/↓ were
  pressed on the menu instead of moving the cursor. The wizard now switches to the terminal's
  alternate screen for its whole run, like Claude Code or vim: everything scrolls inside the window
  under the wizard's keys, the shell's screen and scrollback come back untouched on exit (clearing
  between screens no longer wipes the terminal's scrollback), and whatever the last screen said is
  printed again after the window closes. Section headings lose their leading `──`, and the key
  hints sit on the window's bottom row, spelled out on the main menu ("Use ↑ ↓ to move through the
  options · enter to choose · esc to go back").

## 0.202.0

- **The wizard's menu folds to fit, and Settings no longer turns single parts of Probe off.** The
  default for new sessions (`on`/`daemon`/`read`/`off`) is now the menu's first row, under a new
  `Defaults` heading, wearing its current state as `‹ on ›`: `←`/`→` walk it right there and Enter
  saves (nothing is written before Enter, and moving off the row puts it back). Enter on an unmoved
  row opens a picker with the settings screen's keys — Enter ticks a state, `→` saves it, `←` backs
  out, with the `‹ Back ←` / `→ Set default ›` bar — also reachable as `probe wizard --action
  defaults`. The daemon is still offered only to paid teams (asked on the save), and a held
  `PROBE_SESSION_STATE` is still named instead of overridden. Only the cursor's group is drawn open;
  every other group is its heading alone, and the arrow keys still walk every row in order, opening
  each group as the cursor enters it. The groups are renamed `Install/uninstall`, `Backfill` and
  `Exit/sign out` (Exit now sits with Sign out). The Settings screen keeps automatic updates (and
  the experimental "Who records" rows): its CLI + MCP, session capture and instruction-rules rows
  are gone, because a device running part of Probe is the broken state the complete install exists
  to prevent — Probe comes off whole, through Uninstall. The `--no-capture`-style flags are
  unchanged for scripts. Every step screen now also scrolls far enough to show the pointed row's
  description, not only its title, on a short terminal.

## 0.201.6

- **The daemon now sees what you type while the Claude Code agent is working.** Claude Code saves a
  message typed mid-turn (without Esc) as a `queued_command` attachment, never as a user line, and the
  daemon read no attachments but its own `[Probe]` messages: 221 of 228 such prompts in 300 real
  sessions reached neither the writer nor the reader. A queued message from you is now your prompt,
  placed where the agent picked it up, so a correction like "stop, use lr 1e-4" is recorded as yours
  and wakes the reader. A queued message from another agent session or an auto-continuation is shown
  to the daemon as harness text, never as you. Mid-turn background-task notices are still not read.
  A chat log read again from the start (Claude Code rewrote it) no longer pushes the daemon's turn
  count up, which aged out waiting messages early.

- **The setup screen no longer says the daemon's key "cannot delete".** It has held `delete` since
  the trash (0261), and its write access can remove papers, views, lineage edges, project
  references and repository links for good (artifact and sub-note deletes need `delete`, which
  the key holds only for the trash routes). Both "daemon" rows in `probe setup --action settings` now say
  where its deletes stop: "Deletes go to the trash or ask you first, unless prompts are off."
  (`precheck.PERMANENT_DELETES` is a question except in bypass mode.) The setup command doc and
  two code comments said the same stale thing and are corrected too.

## 0.201.5

- **Daemon reads repeat themselves less.** Found by the live end-to-end test: (1) when the
  researcher's prompt raised a topic, the reader sent an unasked message on it, the agent then asked,
  and the answer arrived in the same hook with the same facts. An answer now replaces an unasked
  message still waiting that was made before its ask was filed (the reader wrote the answer with
  that message in its own conversation); a later unasked message, and one beside a "nothing found"
  ending, stay. (2) Claude Code runs the Stop waiter's wake through the prompt hook, which started a
  new turn and handed over one more unasked message; the wake now leaves a marker and the prompt it
  causes (within 2 minutes) keeps the turn. pi's poller does the same for the turn it starts to hand
  an idle pi an answer.

- **A Ray Tune trial torn down while its outbox worker is sending the close now still closes the
  run.** Ray's shutdown SIGKILLs the trial actor's direct children, the outbox worker among them,
  and the SDK starts a new worker at exit to deliver the queued close. A SIGKILLed worker keeps its
  lease until the kernel has torn it down, and that exit kick ran a fraction of a millisecond after
  the kill: it read the held lease as a live worker delivering the queue and started none, so the
  lease release stayed queued and the run stayed `running` with its lease held (the cli 0.201.3
  release gate; 13 of 13 local runs once the teardown lands while the close is in flight, the usual
  case for a trial that ran for more than a moment). The kick now waits up to 0.5 s for the killed
  worker's lease to be let go (about 5 ms in practice) before it decides; a lease still held after
  that is a live worker's, a sibling trial's on the same queue, and delivery is left to it.

## 0.201.4

- **`probe version create` is a write.** It mints an experiment version, but `version` was missing
  from the write gate's groups, so the command ran in a `read` or `off` session, and the daemon
  profile's guard allowed it as if `probe version` printed the CLI's version (found live by the
  end-to-end test on the released 0.111.2 plugin). `version create` is now gated like every other
  recorder write and `version list` stays a read; `probe --version` is unchanged.

## 0.201.3

- **A SIGTERM to one rank of a Lightning DDP job closes the run `failed`/preempted again.** When a
  node is preempted alone (its pod deleted), only its rank gets SIGTERM, and Lightning 2.6's handler
  broadcasts to the other ranks from inside the handler: a collective nobody else joins, so rank 0's
  main thread blocked in gloo until torchrun SIGKILLed it 30 s later. The SDK's handler, which
  Lightning composes after that notifier, never ran, and the run stayed `running` until the lease
  expired (the 7-day soak's F2, confirmed on 0.201.0 with a stack dump of rank 0 during the grace).
  Now the first `log` after Lightning installs its handler moves the SDK's to the front of it, the
  grace is armed before any other handler runs, and when the main thread does not answer the
  SDK's re-signal (a C call runs no Python handler) the SDK closes the run from its own thread and
  ends the process (exit code 143) within `PROBE_SIGTERM_FLUSH_SECONDS`.
- **A SIGTERM that lands inside a `print` no longer leaves the run `running`.** CPython checks for
  signals between the writes of a buffered flush with the stream's lock held, so the SIGTERM handler
  can run inside a `print` (or a progress bar's redraw), and the main thread then holds
  `sys.stdout`'s or `sys.stderr`'s lock while it waits for the close. The close, on its own thread,
  flushed those streams before it stopped the log capture and waited on that lock for its whole
  budget: the process died of SIGTERM on time and the run was never closed (the 0.201.1 release
  gate, `test_a_sigterm_close_still_sends_what_the_run_read_and_wrote`, 2 to 3 runs in 12 on a busy
  box). The close's thread now keeps off both streams: its warnings go straight to fd 2 and the log
  capture skips that flush.

## 0.201.2

- **Daemon reads on Codex: a prompt starts a new turn, and the wizard says to approve the hooks.**
  Codex passes the session id only in the hook payload, never in the environment, so the reads
  hook's shell fast path could not write the turn token and the one-message-per-turn pacing fell
  back to 10-minute windows (live Codex 0.158.0 test, 2026-09-29). At a prompt with no session id
  in the environment the hook now starts Python whenever a reader has served a session on this
  machine; the tool-call path is unchanged. Codex also skips a hook nobody approved, silently, so
  moving Codex to the daemon profile in `probe wizard --experimental` now ends with "In the new
  Codex session: `/hooks` › approve the Probe hooks."

## 0.201.1

- **The daemon profile's guard sees `python -m probe.cli`.** It knew only `python -m probe`, which
  cannot run (`probe` has no `__main__`), so `python -m probe.cli project list` got past the lean
  plugin and returned data (found by the end-to-end test on the released 0.111.0 plugin).
  `python -m probe.cli ...` and `python -m probe.cli.main ...` are now read as the `probe` command
  they run, in both plugins.
- **A full outbox volume no longer strands the op that landed, and with it every later write.**
  On a disk full but for the few KB a delivered op freed, a write's op lands and its status count
  does not (#2090). The running drainer's exit check and the kick that starts a new one both read
  only that count, so the drainer exited with the op still queued and none was started again; every
  later write of the run was then held back behind that op and dropped (a 15-min full window in the
  7-day soak dropped 891 of 901 steps on 0.200.2). The drainer now also looks in its queue before it
  exits, the kick does the same when the count reads zero, and a write held back behind queued ops
  wakes the drainer. What remains is plan 1.5's rule: a write waits behind its run's queued ops.

## 0.201.0

- **A SIGTERM close with read/write capture on could spend its whole budget on local work and
  leave the run `running`.** `_close_finalize`'s lineage-hashing wait and the hardware collector's
  stop each correctly bounded themselves to half of what was left of the close's deadline -- but
  chained one after the other that is 75% of the WHOLE budget, and the drain's delivery reserve was
  computed from the ORIGINAL deadline, not from what was left once they were done: under real
  scheduling delay both routinely spent their full share, so the drain and the terminal status
  write were left with ~0s and the process died of SIGTERM with the close still unfinished (found
  under CPU contention in a release dry run, `test_a_sigterm_close_still_sends_what_the_run_read_and_wrote`,
  #2119 on #2124). Both are now bounded to `deadline - the drain's own reserve` (`_close_reserve`,
  `run.py`), so that reserve always reaches the drain and the terminal write regardless of how long
  the local finalizers take. New deterministic regression test (no CPU load needed):
  `test_a_sigterm_close_survives_local_finalizers_that_use_their_whole_budget`.
- **The daemon's approval question records the researcher's pick again.** Claude Code 2.1.283
  copies the pick into the question tool's `tool_input.answers`, so every real answer read as one the
  agent had filled in itself and was dropped: the daemon never acted on it (found by the end-to-end
  test). A pick now counts for a question call the pre-call check let through with nothing filled
  in (matched by its tool-use id).
- **The tracking guard sees `probe` behind a wrapper.** `command probe ...`, `env X=1 probe ...`,
  `nohup`, `timeout 30`, `nice`, `python -m probe ...` and `bash -c "probe ..."` are now read as the
  `probe` command they run, so a `read` or `off` session refuses them like a bare `probe` write
  (before, the wrapper hid them from the guard; the CLI's own write gate still applied).
- **Daemon reads: a "Who records" choice per coding agent, behind `probe wizard --experimental`
  (plan T7 + T8).** Settings › Who records in Claude Code / Codex: `the agent` (today, unchanged)
  or `the daemon`. Picking the daemon mints the Probe daemon's key if it has none (the same
  browser approval as the `daemon` tracking default), installs the capture plugin if missing and
  the new lean plugin `probe-research-daemon`, records the choice in the config
  (`defaults.recorders`), writes the daemon profile's Probe section into that agent's CLAUDE.md /
  AGENTS.md, sets new sessions to `daemon`, and only then removes `probe-research`; picking the
  agent reverses it. Each step reports its own failure, and nothing before the config write is
  left half done. The lean plugin carries only `instrument-code`, the daemon's `[Probe]` messages
  and the Stop wake, the daemon's approval questions, the guard (which refuses every `probe`
  command but `ask`, `exec`, the runs' own data and `session status`), the status line and the
  team note sync: no Probe MCP, no other skills, no setup command. Update, stuck-write and
  team-note notices go to you as a one-line message and `probe doctor`, never into the agent's
  context. With the daemon profile on, the daemon's reader runs for that agent's sessions (the
  `PROBE_DAEMON_READS=on` switch still works for development), `probe ask` answers by the calling
  agent's profile, and `probe session track` turns a session back to `daemon`. `probe update`,
  a re-run install, `probe doctor` and the status line all follow the profile's plugin. The row
  is hidden without `--experimental` unless an agent is already on the daemon; the existing
  "Probe in new sessions" row is unchanged, so a machine whose default is `daemon` keeps today's
  behaviour. A team the daemon is not open to (the paid-plan check) does not see a row still on
  `the agent`. pi has no row yet (T11).

- **The daemon's reader never reports this session's own fresh work as the team's prior work.**
  The writer records the session into Probe as it goes; the reader then found those records and
  cited them as "the team already did this" (the live bench: 4 of 4 messages). Its MCP connection
  now names the watched session and sends `X-Probe-Hide-Session-Work: 1`, so the hosted MCP leaves
  out every project, experiment and run that session created (needs the matching server release).
  The writer's `probe` commands forward the watched session (`PROBE_AGENT_SESSION`), so what it
  creates is always filed as that session's own work.
- **Daemon reads: the Probe daemon gets a reader (off unless `PROBE_DAEMON_READS=on`; the wizard's
  daemon profile turns it on).** Beside the writer that records the session, the worker now runs a
  second agent that only reads: it watches the same session, looks up the team's prior work through
  the Probe MCP (Opus 5.5 via the server's `reader` model alias; `PROBE_COMPANION_READ_MODEL`
  overrides), and sends the coding agent short `[Probe]` messages and answers. Its instructions are
  one approved skill file (`daemon/read_session.md`); its tools are `read`, `session`
  (open/outline/search) and the MCP, never a shell or a write, and `search_knowledge` always leaves
  out the watched session. A turn ends with `final_result(message)`; prose instead of the tool is
  the answer to an ask and dropped otherwise. A turn is at most 8 model rounds and 60 s (80 s for an
  ask); its history, cursor, ask and message are saved in one transaction and the message is
  published by id, so a crash neither loses nor doubles one. It compacts near 120K tokens, keeps its
  own tables (`read_*`) and never touches the writer's lease.
  - Unasked messages: one per researcher prompt, plus one more per 10 minutes of a long turn (a
    one-prompt autonomous session otherwise got the reader's first message and none after it,
    corrections included; replay bench, 2026-09-28).
  - `probe ask "<question>"` files a question and returns at once (the answer arrives as a
    `[Probe]` message); `--wait` waits for it (up to 2 h) and prints it. An ask the reader has held
    10 minutes without an answer fails with a reason; every ask ends answered, "nothing in the
    team's records", or failed.
  - The researcher sees each failure once (a hook `systemMessage`: the daemon stopped, the reader
    failing, reads unavailable, a stopped turn, back to normal) and `probe doctor` shows the
    reader's state, its turns and the asks.
  - Codex gets messages at prompts and after tool calls (no wake); pi's extension delivers at each
    prompt and steers an answer into a running agent, or starts a turn for one that lands while
    pi is idle.
- **The daemon's writer: 35 minutes to finish, a handover to a resumed session, long calls.** At
  session end the worker now gets 35 minutes (was 15), so one full 30-minute model call can finish.
  A resumed session's worker no longer waits for the old one to drain its queue: it asks for a
  handover, and the old worker pauses at its next model round (history saved, events covered) and
  exits; the new one carries the same conversation on. A model call may take 1,900 s client-side
  (the server's ladder: 1,800 < 1,830 < 1,860); the write lease is renewed every 60 s WHILE a call
  runs; the writer compacts near 950K tokens; the model connection uses TCP keepalive. The device
  token counter is kept per lane (writer / reader), counts fresh input plus output only, and waits
  at most 1 s for its lock.
- **Daemon reads, Claude Code delivery (plan T3; nothing publishes messages until the reader
  lands).** New stdlib hook `plugins/probe-research/hooks/reads_hook.py` hands the daemon's
  `[Probe]` messages to the coding agent, reading `<state>/probe/reads/` with the same rules as
  `probe.daemon.mailbox` (a parity test renders the same files through both). At each prompt and
  after each main-agent tool call it claims every answer (answer, nothing found, failed) not held
  for a waiting `probe ask --wait`, plus at most one unasked message per researcher turn, as
  `additionalContext`. A helper agent's tool call never claims: Claude Code 2.1.283 puts `agent_id`
  in the payload of a hook fired inside a subagent (never on the main thread, and `transcript_path`
  stays the main transcript). A bash fast path starts no Python unless a message waits for this
  session (`CLAUDE_CODE_SESSION_ID` / `CODEX_THREAD_ID`); at a prompt with nothing waiting it still
  writes the new turn token, for a session the reader serves. The wake: a Stop hook with
  `asyncRewake` (in `hooks/claude-reads.json`, declared only in the Claude Code manifest, since Codex
  has no such key) waits up to 150 s while an ask is open and exits 2 with the answer, which wakes
  the agent; a new prompt, or a hook or `probe ask --wait` taking the answer first, ends it quietly.
  It runs only when `CLAUDE_CODE_SESSION_ATTENDED=1`: under `claude -p` Claude Code runs an
  `asyncRewake` hook in the foreground, so a wait would hold the exit (measured: +8 s for an 8 s
  hook; with this hook and an open ask, `claude -p` exits in 2.4 s). Checked live on 2.1.283: an
  unasked message at the prompt, an answer after a tool call, and a wake after the turn ended all
  reached the agent; a 40 KB answer after a tool call arrives as Claude Code's saved-file preview
  (the first 2 KB and the file's path), while the same answer through the wake arrives whole.
  Codex delivery is T11: under Codex the hook exits 0. The adapter capability table gains
  `inject_tool` and `wake` (Claude Code both; Codex and pi neither).

- **The daemon writes a folder per session you can read: its log and each agent's full trace.**
  `~/.local/state/probe/daemon/sessions/<session id>/` holds `worker.log` (what the process did;
  the one-line-per-HTTP-request noise is gone), `writer.jsonl` and, with reads on, `reader.jsonl`:
  one JSON object per line, each standing alone (run, trace, span and parent ids) -- every run's
  start and end, the job description and tool definitions (once per process, again when they
  change), each model call as ONE line with what it ADDED to the conversation and what it answered
  (text, thinking, tool calls, tokens, latency, dollars), every tool run (arguments, duration,
  outcome, full result) and each compaction with its summary. Pydantic AI's own message JSON,
  logged as deltas so a long conversation grows the file linearly. `probe daemon status` names
  each session's folder. Files are 0600, the folder 0700; swept with the session's store after 30
  idle days; a file stops at 256 MB (`PROBE_DAEMON_TRACE_MAX_MB`); `PROBE_DAEMON_TRACE=off` turns
  it off. Tracing only observes: the model receives the same bytes with it on or off, and a trace
  that cannot be written never fails a bite.
- **The daemon uploads those traces to Probe, beside its work and never in its way.** A background
  task sends new lines to `POST /v1/companion/traces` (Probe's agent-traces store) in batches of
  at most 1 MB, moving a cursor kept beside each file only when Probe takes the batch, so a crash,
  a refusal or an outage loses nothing and a re-send is never a duplicate. A line over 900 KB is
  trimmed before it goes (the local file keeps it whole); a session that crashed ships at the next
  start; while tracing is off for the team the lines wait. A folder ships only under the server
  and key that recorded it: a session resumed under another account keeps tracing locally and is
  never uploaded. `PROBE_DAEMON_TRACE_UPLOAD=off` turns uploads off.

## 0.200.2

- **Hosted MCP: `X-Probe-Hide-Session-Work: 1` hides the caller's own session work.** With the
  header and the caller's ambient `X-Probe-Agent-Session`, `search_knowledge` and `browse` ask the
  backend to leave out the projects, experiments and runs that session created, browse's
  floating-run list is filtered server-side, and every `entity` view drops the rows, edges and
  origins naming that work (the entity asked for by address is still shown). The session is
  always the header's, never a tool argument, so the model cannot redirect it; `exclude_session`
  still drives the transcript self-exclusion. A backend that cannot confirm it is marked
  `session_work_exclusion_unsupported`. The Probe daemon's reader sends it; no tool parameter or
  description changed, and a caller without the header sends exactly the requests it sent before.
  `Client.search` and `Client.browse` gain `exclude_origin_session`, `Client.session_created`
  reads `GET /v1/sessions/{id}/created`, and the header name is
  `probe.sdk.agent_session.HIDE_SESSION_WORK_HEADER`.
- **A Ray Tune trial's run closes when Tune ends the trial, even over a slow network.** A
  scheduler's early stop (ASHA, median stopping), or a function trainable that simply returns,
  left its run `running` with its lease held on prod: Ray ends the trial's thread with
  `sys.exit(0)` from `tune.report()`, then SIGTERMs the actor's process group and SIGKILLs it
  200 ms later, and the exit hook's close was killed mid-request. That `sys.exit` on Ray's
  `RunnerThread` now closes the run the thread opened, at once and queue-only
  (`flush_timeout=0`: `completed` for Ray's stop, `failed` for a non-zero code), and the outbox
  worker is started again at exit, after Ray's core worker has SIGKILLed the actor's children. A
  `tune.Trainable` class closes in `cleanup()`, which Tune waits for (`instrument-code` skill).

## 0.200.1

- **An artifact over 64 MiB waiting on another upload of the same bytes tries again at least
  every minute.** The lane's backoff used to climb to 300 s while the server answered 409; the
  server now hands the bytes over once that upload has been idle 10 min (its uploader gone), so
  the wait is capped at 60 s (`BUSY_MAX_WAIT_SECONDS`), and a `strict=True` upload waits 12 min
  instead of 10 before giving up. An upload the server ended as `superseded_idle` (this machine
  went quiet and another run's create took it over) is started again from the staged copy -- and
  becomes a HAVE once the other one verifies -- instead of being recorded as a reference row.
## 0.200.0

- **A run now records what it WRITES, not only what it reads (lineage plan 3, F2).** The read
  recorder's audit hook notes each file the run opens for writing (and the new name of one it
  renames); at the close a noted file counts only if it still exists, is a regular file and is no
  longer what it was just before the run opened it (device, inode, size, mtime, ctime -- an empty
  append, a failed open or a read-only `r+` is not an output and is never hashed), and its FINAL
  bytes are hashed (fingerprinted over 1 GiB, the read side's scheme) and sent to
  `POST /v1/runs/{id}/outputs` -- only to a server that declares `run_outputs`. Never recorded:
  Probe's own folders, caches (`.cache/`, `$XDG_CACHE_HOME`, `$HF_HOME`...: a write there is a
  download, also through a symlink), the interpreter's files, credential-shaped paths,
  `.probeignore`d files, temp files gone at the close. `probe exec`'s Python child records writes
  too, and its own `probe.init(capture_outputs=False)` drops what it spooled before. A crashed
  run's writes, sent later by a recovery, are hashed only if the file is still what the writer last
  saw (it checks every 30 s while alive); otherwise they go unhashed (`unverified_after_exit`), never
  with another run's bytes. Unseen, and said so: `torch.save` (C++), C/Rust writers. Off with
  `capture_reads=False`, `capture_outputs=False` / `PROBE_CAPTURE_OUTPUTS=0`, or
  `PROBE_CAPTURE_WRITES=0` alone.
- **One I/O budget per run, reads first (F2c).** The run's 10 GB hashing budget now pays for every
  byte hashed: reads during the run, then at the close the reads still unhashed, then the writes,
  then output capture's references (one budget however many capture windows the run closes). The
  close looks at written files first, on a thread of its own, and waits for a busy hashing worker
  instead of skipping. A fingerprint past the budget is charged the edges it reads and
  none is started without budget or past the close's deadline (before, each cost up to 8 MiB,
  uncharged and unbounded in count). What was cut is counted in the coverage (`hash_cut`: budget /
  deadline). The hash cache now writes a close's answers in one transaction (2,000 small outputs:
  4.4 s to 0.35 s at the close). Measured with `scripts/bench_write_capture.py`.
- **Capture's redacted uploads carry the original's hash (F1).** `meta.source_sha256` is taken
  from the same read as the redacted bytes (`secret_gate.read_upload_redacted_sourced`), so a later
  run reading the original matches it. Two originals that redact to the same bytes stay ONE stored
  version with the first original's hash; the later one is not re-uploaded, and its hash rides the
  run's write list.
- **Capture references carry a hash (F3).** A file captured as a reference (64 MB to 1 GiB, or past
  the close budget) now sends `content_hash`, hashed from what the run's I/O budget has left and
  within the close's deadline; one the budget or the time did not reach is sent without.
- **Offline runs record reads and writes (F6).** `probe.init(mode="offline")` records both and
  queues them ahead of the close (within half its time, as online); `probe sync` first moves a
  crashed offline run's own leftovers into its queue, then delivers, dropping (with a warning) a
  list the sync server does not declare it takes -- checked again at delivery, so a list queued
  meanwhile is gated too. Offline runs now honour `capture_reads`, `capture_outputs` and `ignore=`
  for this. A dead offline run's leftovers go into its own queue, never to a server.
- **Write recording costs less per open, and stops cleanly (review of F2).** Past the 10,000-path
  cap a write-open is no longer judged at all (it used to resolve the path and run every exclusion
  first), and a new path is resolved from its folder's cached resolution plus one lstat instead of
  one lstat per component: per new write-open, 43 -> 25 µs past the cap and 61 -> 52 µs below it
  (no recorder: 21 µs; `scripts/bench_write_capture.py --hook`). What a recorder last saw of each
  file it wrote is kept as ONE identity per path in a `<host>.<pid>.wf.json` sidecar, rewritten
  atomically, instead of a spool line per change every 30 s (a week-long run grew it without
  bound, and the close parsed it outside its wait). A sweep under way when the run closes no longer
  re-creates the folder the close removed (a later recovery then sent an empty list for a closed
  run). A close no longer waits on a hashing worker stuck on a stalled mount: one still alive 5 s
  past its own deadline is left to it.
- **Reads (and two writes) made from C are recorded (lineage plan 3, F4).** Python's `open` audit
  event never fires for a library that opens its files from C or Rust, so the recorder now wraps
  the ones that matter, only once each is imported: `pyarrow.parquet` (`ParquetFile`,
  `read_metadata`, `read_schema`, and `ParquetDataset.read`, which `read_table`, `read_pandas` and
  pandas' `read_parquet` go through), `pyarrow.memory_map` (the Hugging Face `datasets` cache),
  `h5py.File` (`"r"` is a read; `"w"`, `"w-"`, `"x"`, `"a"`, `"r+"` are writes), `safetensors`
  (`safe_open`, every framework's `load_file`), and `torch.save`, a write from C++. A dataset's
  files are recorded when a read scans them -- after the partition filter, at most 10,000 per read
  -- never when it is opened. `probe exec`'s child and spawned workers run the same wrappers.
  `torch.load` needs none (it opens with Python's `open`). A function a library imported by name
  before `probe.init()` (transformers' `from safetensors import safe_open`) is pointed at its
  wrapper once, at init. `inputs.note_read(path)` records a read for any other reader. Still
  unseen, and named in the coverage: a `pyarrow.dataset` Dataset read directly (its types are
  immutable C++ wrappers), pyarrow's feather/ipc/csv/json readers, other C/Rust readers and
  writers, and a wrapped function held before `probe.init()` other than as a module global.
- **Spawned workers record into the run (lineage plan 3, F5).** A fork inherits the recorder; a
  Python worker started by spawn or forkserver (a DataLoader's, a multiprocessing Pool's, a
  `python` subprocess) did not. `probe.init()` now binds them, while its run is the only one
  recording in the process: new workers get the `probe exec` child hook through `PYTHONPATH`
  (`PROBE_READS_DIR`, `PROBE_READS_OWNER_PID`, `PROBE_READS_BIND`), and a binding file tells the
  ones already running which run they belong to -- a persistent worker stops recording a run that
  closed and follows its process to the next run (it looks every 0.25 s; what it spooled in between
  is cut at the close, so nothing lands in the wrong run). Each spool's first line names its run
  and the process whose close takes it; that close takes its live workers' spools too, never a
  rank's. The environment is restored at `finish()` (or when a second run opens). A worker that
  opens a run of its own, or opts out, leaves the binding, and its own workers with it; a run with
  `capture_outputs=False` binds its workers without writes. Only `probe.init()` binds: a CLI or
  service opening runs does not, and `probe exec`'s child stays a plain exec child.
- **Hashes that arrive late replace their unhashed read (lineage plan 3, F7).** Each read row now
  carries an `observation_id`, derived from the run, host, path and the file's identity when read,
  so every report of a read (the close's, a replay, a recovery) names the same observation, and the
  server (0290) replaces the unhashed row instead of adding one. What a close ran out of TIME to
  hash (not what the budget cut) is kept beside the run's `owner.json` and hashed and re-sent under
  the same ids by the next process on the host -- or the next run this process opens, since a
  sweep's runs each close within a short budget -- only if the file is still exactly what was read
  (or written), and with no coverage, which would replace the run's own. During the run the owner's
  hasher now also tails its workers' spools (forked or spawned; never a rank's), so their reads are
  hashed as they happen rather than all at the close. The id, and the late re-sends, go only to a
  server declaring `run_outputs` (shipped with 0290): an older one would keep a late hash as a
  second row.
- **`probe run input pin --writer RUN` pins a read to the run that wrote it.** A read matched only
  to a write that run RECORDED (lineage plan 3) has no stored version to pin with `--version`;
  `--writer` (an id or slug) names the run instead (`PATCH /v1/runs/{id}/inputs` with
  `writer_run_id`). Exactly one of the two; a pin replaces the one before and `reset` undoes it; a
  refusal (the reader itself or an unknown run: 422, a run in the trash: 410, no read at that path:
  404) exits 1. The SDK has `Client.correct_run_input(..., writer_run_id=)`, and
  `Client.run_outputs` / `Client.record_run_outputs` for `GET`/`POST /v1/runs/{id}/outputs`.
- **Docs: a CLI-opened run records its reads and writes only under `probe exec` (lineage plan 3,
  F8).** `track-work` (and the daemon's vendored copy) now says a run `run start` opened
  (deprecated) records nothing of what its work reads or writes, and points it at
  `probe exec RUN -- cmd` or `probe.init()` in the job; `instrument-code` says writes are recorded
  too, names their switches (`capture_outputs=False`, `PROBE_CAPTURE_OUTPUTS=0`,
  `PROBE_CAPTURE_WRITES=0`), and lists the C readers now seen. Same in the README.
- **Spawned-worker binding, C-reader wrappers and late hashes: review fixes (lineage plan 3).**
  Workers are bound only while exactly ONE run is open in the process, counting runs that opted
  out of recording, disabled and offline ones (a second run opened with `capture_reads=False`
  used to leave the first run's binding, so its workers recorded into the first run). A worker
  stops recording once its owner has died: the owner holds an exclusive lock on its binding for
  its life, and the binding's name carries a nonce, so a new process reusing the owner's pid is
  not followed. Binding keeps `PYTHONPATH` entries as given (an empty one is the working folder),
  and `finish()` restores it exactly. The close takes a live worker's spool only from a worker
  that FOLLOWS its binding (not one that merely inherited the run by a fork), and a header is
  written at every spool open, so a worker that joins the run itself is not taken for the owner's.
  The folder-resolution cache is re-checked against the folder's identity, so a retargeted link
  into a cache is a download again. Each wrapped C reader is ONE object at every name it has
  (`torch.save is torch.serialization.save`), so `Pool.map(pq.read_metadata, ...)` and
  `ProcessPoolExecutor().submit(torch.save, ...)` pickle again; `safetensors.safe_open` stays a
  class (`isinstance` works); `ParquetDataset.read()` lists nothing while nothing records. A late
  read re-send carries a full hash only (the server keeps a hash-less report as a retry), every
  read-list batch carries the coverage, and `probe sync` to a server without `run_outputs` strips
  observation ids and drops late re-sends (they would become second rows). `pyarrow`, `h5py` and
  `safetensors` join the `dev` extras, so CI runs the reader tests; `torch.save` is tested through
  a stub. On an NFS home the owner never tests its own binding lock (there flock is a POSIX lock,
  which the owner's own test would release), workers ask whether their owner lives about once a
  second (the binding itself still every 0.25 s), a late re-send is tagged in the offline queue
  (a caller's own list without coverage is no longer taken for one), a run handle collected
  without being finished stops counting as open, and a fork while the owner's lock opens never
  hands the child its fd. The background hasher marks each item done on the queue it took it
  from, and a close waits only on a live hasher (a swapped queue killed it and left a count that
  made every later close in the process wait its whole wait and send reads unhashed).
- **`pd.read_parquet(file)` no longer crashes a recording run, and an offline run's spawned
  workers record (lineage plan 3).** `ParquetDataset.read()` lists files only for a dataset made
  from local paths, judged from the constructor's arguments: on one made from a file object (what
  pandas and `pq.read_table(open(...))` build) `_dataset.filesystem` segfaults pyarrow, and the
  audit hook already saw that `open`. A run's spool folder escapes `%`, `:`, `;`, `/` and `\` in
  its id (`local:<key>` is `local%3A<key>`): the colon split the workers' hook folder on
  `PYTHONPATH`, so no spawned worker of an offline run loaded the hook.

## 0.199.1

- **A multi-node job opened by `probe exec -- sbatch` or `--detached-launcher` follows the
  per-writer lease rules.** Such a run is opened awaiting attach, and it never asked for leases,
  so it stayed on the old rules: a lost node was invisible to `writer_lost`, and ranks killed
  with no close (their output helpers' "writer gone" reports) left the run `running` until the
  15-minute reaper. The hand-off now asks for leases without holding one itself (the submitter
  exits once the scheduler accepts the job), every rank registers its lease when it attaches, and
  the run closes by the lease rules: after `scancel` each rank's SIGTERM close releases `failed`
  (preempted) and the run reads `failed` within seconds; ranks killed with no close read
  `crashed` at the second report; a clean finish reads `completed`. A requeued job (`scontrol
  requeue`) reopens the run its first attempt left behind instead of refusing `probe.init()`:
  attach now also reopens `crashed` and a `failed` that the SIGTERM close marked preempted. Any
  other `failed`, and `canceled`, is still refused. Found by the environment suite (E1).

## 0.199.0

- **The SDK works on Windows.** `import probe` failed there with `ModuleNotFoundError: No
  module named 'fcntl'`, so a training script could not log at all. The SDK path's file locks
  now go through one helper, `probe._shared.oscompat`: `fcntl.flock` on POSIX (unchanged) and
  `msvcrt.locking` on one byte past any lease content on Windows. Behind that import error sat
  more Windows traps on the same path, fixed too: liveness checks used `os.kill(pid, 0)`, which
  on Windows terminates the process, so asking whether the outbox sender was alive would have
  killed it; the sender is now spawned detached there (`start_new_session` is ignored on
  Windows); `os.open` gets `O_BINARY`, without which an artifact's queued copy would have had
  every `\n` rewritten as `\r\n`; a rename retries for up to a second past another process's
  open handle; and a lock file is deleted after its handle closes, since Windows cannot delete
  an open file. `probe.init`, `log`, `update_config`, `log_artifact` and `finish`, the outbox
  and its background sender, and offline mode with `probe sync` pass the `agent-os-matrix`
  workflow on Windows (Python 3.10 and 3.12). The plugin's vendored hook copies
  (`_session_marker.py`, `_telemetry_core.py`) carry the same change and keep using `fcntl`
  directly where `probe` is not importable. Still POSIX-only: the console-log tee
  (`probe/run.log`), the companion daemon, and the import and backfill commands.
- **A SIGTERM no longer loses a run's queue or leaves the run `running`.** Managed jobs
  (SageMaker, Vertex, Kubernetes, a spot reclaim, `docker stop`) stop with SIGTERM and destroy the
  container about 30 s later. Python died at once, so the writes still queued on the container's
  disk were lost and the run stayed `running` until the 15-minute reaper (the environment suite's
  managed job: 0 of 900 queued points, the config and the artifact). `probe.init()` now installs a
  SIGTERM handler (main thread only): it spends up to `PROBE_SIGTERM_FLUSH_SECONDS` (default 20;
  `0` turns it off) sending what is queued, closes the run `failed` with
  `probe_finish.reason = "preempted"`, `signal = "SIGTERM"` and `exit_code = 143`, then exits as
  SIGTERM would (a container's PID 1 exits 143). A handler that was there first runs first. One
  that returns has its own stop to make (Lightning's `SIGTERMException`, Hugging Face's JIT
  checkpoint): that stop goes ahead, its close on the way out is marked the same way, and ours
  closes the run only if the process is still running near the end of the budget. One that
  raises gets the same close on its way out. A handler installed after `probe.init()` that
  replaces ours is left alone. On a throwaway disk (a container's own layer, tmpfs), a close that
  could not deliver everything in time is sent directly, counting what is lost as
  `probe_finish.undelivered`, instead of queuing behind it for a worker that dies with the
  container. The signal can land while the main thread is inside the SDK holding a lock: the
  close runs on a thread started in advance, after the interrupted SDK call returns (at most 2 s),
  and the process dies on time whatever it is blocked on.

## 0.198.1

- **The code snapshot no longer takes the SDK's own queue as the run's code.** A job started in
  `$TMPDIR` (`docker run -w /tmp`) with no usable home queues in `$TMPDIR/probe-outbox-<uid>` and
  keeps state in `$TMPDIR/probe-home-<uid>`, and the snapshot swept those queue files into the
  run's manifest. So did any working folder holding the outbox or the state folder
  (`PROBE_OUTBOX_DIR` or `XDG_STATE_HOME` inside the project, an explicit `spool_dir`). Every
  folder the SDK keeps files in is now skipped by the code snapshot (reported once per folder as
  `probe_state`), by read capture and by the output sweep, whatever the working folder. Found by
  the environment suite (E3).
- **Hardware metrics include a run's last partial minute.** Hardware points are 60 s windows,
  sent only once a window closed, so a run under a minute recorded no hardware at all and every
  run lost its last partial minute. At `finish()` the collector now takes one last sample on its
  own thread and sends the open window too, within the close's time: a hung sensor is abandoned
  as before, never waited on past the deadline. The first CPU sample also measures the run now,
  not psutil's meaningless first 0 %. Found by the environment suite (E3).
- **Output and read capture start when `HOME` cannot be written.** With `HOME=/` (what Docker
  gives an unknown uid) or a read-only home, the outbox fell back to `$TMPDIR` but output and read
  capture stopped with `Permission denied: '/.local'`, so the run had no `probe/run.log` and no
  read list. Their state now falls back to the same private `$TMPDIR/probe-home-<uid>` a process
  with no home uses. Found by the environment suite (`managed/home[slash]`).
- **`PROBE_OUTBOX_DIR` is split per rank, like the default outbox.** A distributed job queues
  each rank's writes in `<PROBE_OUTBOX_DIR>/rank-<N>/`. Before, an explicit `PROBE_OUTBOX_DIR`
  (the setting the reliability plan's rollout note gives a cluster) put every rank of every node
  on one queue and one append lock. On an NFS home that lock is shared between machines, and
  the Linux NFS client retries a busy lock with a backoff of up to 30 s. Each `log()` waited
  that long: a two-node Slurm job made 10 steps in 5 minutes instead of 200 in 25 s. The
  directory itself is still the root that `probe outbox status`, `drain` and `sync` read, and
  they find every `rank-*` queue under it. The session-start hook's copy of this rule changes
  with it.
- **A > 64 MiB artifact logged just before a job ends is uploaded, not stranded.** Its upload
  queues in the outbox's own multipart queue, which `status.json` does not count. When it landed
  while the background sender was in its last seconds, the producer's kick saw the sender still
  running and did nothing, and the sender checked only `status.json` and exited. The upload then
  sat queued until some later `probe` command on a machine that could see that queue. In a Slurm
  job (`log_artifact` then `finish`, then the job ends) that was never: 2 of about 7 two-node runs
  in the new environment test lost their 70 MiB artifact that way. The sender now counts the
  multipart queue before it exits.

- **The Probe daemon records what is said about a write the agent made itself.** When the
  researcher asks for a write, the agent makes it with `--directed` and the daemon does not
  repeat it; the daemon read that as "this topic is the agent's", so what the session only
  SAID about it was never recorded (a side-by-side trial on 2026-09-28: the agent saved two
  papers with `--directed` and said which setting each informs; the daemon recorded neither).
  track-work now says `--directed` covers only that write. On the replay bench
  (`sxs-mlp-daemon-a`, Opus 5.5, 3 runs each) the paper "why" went from 2/3 to 3/3 and every
  check from 5.0/6 to 6/6.
- **Bench: `fixtures.py --also-project` freezes a sibling project** the session made or used
  (a lit review beside the work), so a replay sees it where the live session did; and
  `score.py` resolves a slug two entities share (a replay's duplicate of a frozen one) to
  both instead of one.

## 0.198.0

- **`probe.init()` works in a process with no home directory.** A container running an
  arbitrary uid (OpenShift, `docker run --user 12345`, many managed-job runners) can start with
  `HOME` unset and no passwd entry. There `Path.home()` raises, and `probe.init()` died with
  `RuntimeError: Could not determine home directory` in `config.config_path()` before it opened
  a run. The outbox already fell back to `$TMPDIR` (plan 1.5); the config, state, device-identity
  and session-marker paths did not. They all go through `probe.sdk.homedir` now: the real home,
  else a private `$TMPDIR/probe-home-<uid>` (0700, never one another user could have made). Like
  the outbox fallback, it does not outlive the machine. Found by the environment suite
  (`managed/home[unset]`).
- **jax arrays log and configure like numpy's.** `probe.log({"x": jnp.array([3.5])})` stored no
  point: the SDK read only numpy and torch values by shape, and `float()` refuses a jax array with
  `ndim > 0`, so a size-1 jax vector went to the step record as its repr, with a "not
  JSON-serialisable" warning. A jax value in `config=` or `update_config` (`jnp.float32(0.1)`) was
  stored as the string `"Array(0.1, dtype=float32)"`. jax arrays (and `ml_dtypes` scalars such as
  bfloat16) are now read like numpy's: one element becomes a number, a small array a list. 0-d
  jax arrays, which `float()` accepts, were already stored correctly, and so were plain numpy
  scalars (found by the environment matrix, E2).
- **The wizard offers daemon recording only to teams on a paid plan.** The Settings screen asks
  the server (`GET /v1/companion/availability`) and, on a no, the "Probe in new sessions" row
  walks `on → read → off` without `daemon` and says "Daemon recording is on paid plans." A row
  already on `daemon` keeps it, so leaving it alone changes nothing. Signed out, offline or on
  an older server it keeps offering the daemon; the key's grant still refuses a free team.

## 0.197.1

- **`probe session status` no longer reports a running transcript uploader as `not started`
  when `COLUMNS` is set.** The check that confirms a pid really is the uploader asked `ps` without
  `-ww`, so `ps` cut its output at `$COLUMNS`. The uploader's command line has ~550 characters of
  wrapper script before its first `tap`, so the check failed on a live daemon. The same check
  decides whether capture-off signals a pid and whether the tap's spawner and the pi extension
  count a daemon as alive; all four copies now pass `-ww`.
- **The agent test suite no longer stops every session's transcript uploader.** Tests that call
  `capture.turn_off()` reached the real `/tmp/probe-research-tap-watcher-*.pid` files and sent
  SIGTERM to every live session's daemon on the machine. On a shared dev box that made status
  lines read `not capturing session transcript: not started` until each session's next prompt
  restarted its uploader (7 times in one morning). `turn_off` now reads its pid directory from
  `PROBE_TEST_TAP_PID_DIR` (default `/tmp`), and the suite points it at a temp dir for every test.

## 0.197.0

- **The daemon's device token fuse is gone.** A device past 60M tokens in a UTC day
  (`PROBE_DAEMON_DAILY_TOKENS`) no longer stops its daemons until tomorrow; `device.json` still
  counts tokens for `probe companion` status. And a model failure whose text says "refused"
  (`Connection refused`, a provider refusal) no longer releases the lease as `budget`: the old
  `"fuse" in text` test matched it. Richard 2026-09-28: "we dont want any limit".

## 0.196.1

- **One team-note audit per machine at a time.** The audit line was asked once per session and
  silenced only when the auditor stamped the note, so every session that submitted a prompt in
  between was told to spawn its own background auditor (9 sessions on one box, up to 9 subagents
  rewriting one file). `probe notes audit-advisory` now claims a machine-wide lease
  (`<state>/team-note/audit-lease.json`, 2 hours) when it prints the line and prints nothing while
  another session holds it. The session holding it is told again if the first telling never
  arrived; a claim nobody acts on holds the audit for 2 hours, then the next prompt takes it over.
  `--peek` prints the line without claiming it. The plugin's `note_audit` hook no longer asks in a
  session whose tracking is `read` or `off`, or on a prompt that is itself a `/probe` switch
  command, since either would claim the audit and then skip it.

## 0.196.0

- **Every rank of a distributed Lightning or Hugging Face job now holds a writer lease.** Before,
  `ProbeLogger` and `ProbeCallback` gave ranks other than 0 no lease at all, so the server could not
  tell a lost node from a finished job (the 7-day soak worked around it with `PROBE_RUN_ID` +
  `probe.init()` on each rank). Now rank 0 shares its run id and epoch with every rank (Lightning at
  setup; Hugging Face in `on_train_begin`, through the process group's store, where rank 0 never
  waits and the others take `PROBE_RUN_ID` when set, else wait at most 10 s, so a callback on the
  main process only costs the other ranks their lease, never the job a hang), and each other rank
  joins that run as a lease-only
  writer: it beats its own `rank` lease and never logs a metric, captures anything or sends a
  status. It releases the lease when it ends: `completed` at a clean exit, `failed` on an error or
  Lightning's SIGTERM, `canceled` on Ctrl-C. A SIGKILLed
  rank (or a Hugging Face rank killed by a SIGTERM with no handler) releases nothing, and its
  lease expiring is how the server sees it lost. A server without writer leases gets nothing from
  these ranks, never a run-level heartbeat. Rank 0 writes what it did before (plans 2.8, (a), (b)).

## 0.195.1

- **A full outbox volume no longer drops writes the SDK had already queued.** On a disk full but
  for a few KB, a write's op file could land and its status bookkeeping then fail with
  `No space left on device`: the SDK counted a drop for a write it later delivered, queued the op a
  second time on its retry, and never woke the drainer, so that stranded op kept the run's lane
  non-empty and every later write of the run was dropped instead of sent directly (a 7-day soak
  rehearsal on a real 1 GiB tmpfs lost 118 of 119 writes over a 120 s full window, 0.195.0). The
  op now counts as queued once and the drainer is woken; a write is dropped only while the run
  has other writes queued (plan 1.5).

## 0.195.0

- **A write Probe refused now fails the command.** Every `probe` write used to swallow the server's
  answer: a refusal (a run that does not exist, an invalid value, a second parent) was queued,
  printed `null` and exited 0, and the Probe daemon filed it as recorded. Now a refusal (any 4xx)
  exits 1 with the server's message. A write Probe did not answer (no connection, a 5xx, a login
  it refused for now) is still kept in the outbox and sent later: the command exits 0, prints
  `probe: queued: <METHOD> <path> ...` on stderr (the first one at once, then at most one line per
  5 minutes with the running count) and, where it printed JSON, `{"queued": true, "count": N,
  "writes": [...]}` instead of `null`; `probe log` and a run's `artifact add` say `(queued)`, not
  `(delivered)`. A write the outbox could not keep either prints `probe: NOT queued: ...` and exits
  1. The daemon's logbook files a queued write as `queued`, under "Not recorded yet" in
  `session(op=status)`, never under "Recorded" (the outbox delivers it by itself; the daemon is
  told not to run it again). The daemon may no longer pass `--async`, which would queue a write
  before Probe answers. `probe exec` and the SDK are unchanged: a refused write never stops a
  training job, and nothing is kept per failed write.
- **`probe edge add` says when a link already exists, and can cite the session.** Adding the same
  edge twice prints `{"already_linked": true, "id": "<edge id>"}` and exits 0 (with `"applied":
  false` and a note on stderr when this call's `--reason`, `--evidence` or `--provenance` could not
  be applied to the edge that was there); any other refusal exits 1. `--evidence <event-id>`
  (repeatable) stores the transcript events a link rests on in `meta.evidence` as `{session, event}`
  pairs: the daemon's session (`PROBE_DAEMON_SESSION`), else the coding agent's. With neither it is
  a usage error (exit 2): an event id alone names nothing anyone can open.
- **What a run read, from the CLI.** `probe run inputs <run>` lists the files a run read, each with
  the run and file version that wrote the same bytes, whether a person dismissed or pinned that
  match, and what the recorder could see (`coverage`, including `truncated`). `probe run upstream
  <run> [--depth N]` walks what it built on, up to 5 hops back. `probe run input dismiss|pin|reset
  <run> <path>` corrects one read's match (`pin` takes `--version <id>`). The daemon may run the two
  reads but never the corrections, and the tracking switch lets the reads through in every state
  that allows reads.
- **The automatic retry link also needs the same launch.** With `PROBE_AUTO_RETRY_LINEAGE` on (still
  off by default), a run is linked as a retry of the run before it only when it also runs the same
  entrypoint, arguments and folder that run recorded. Before, a different script with the same
  config could match.
- **The Probe daemon sees what a tool call pointed at.** A call that runs no shell command (a page
  fetched, a file read, a search, an MCP call) reached the daemon as its tool name alone. It now
  carries a one-line summary of its key arguments (`url=...`, `file_path=...`, `query=...`), shown in
  the daemon's view of the chat and found by its session search, for Claude Code, Codex and pi.
- **The Probe daemon removes its own links without asking.** Before removing a lineage link it reads
  it (`GET /v1/edges/{id}`, which ships with the server's link stamp); a link Probe says this
  researcher's daemon made (`inferred`, via the daemon, by this researcher) is removed at once, and
  its logbook entry says `own link (L9)`. Any other link -- a person's, another researcher's
  daemon's, one it cannot read, any link on a server without that route -- still asks, as before.
- **The Probe daemon no longer writes project or run descriptions.** A description written through
  the CLI locks out the one-line description the server writes once a run finishes. The daemon's
  pre-check now refuses `--description` on `project create|set|patch` and `run set`; an
  experiment's question stays the daemon's to write.
- **`probe edge add` / `edge remove` help says a run may have more than one parent.** The first
  stays the run's parent and the others are edges; removing the first makes the next-oldest one the
  parent (server 0275). `edge add` also names the two new relations, `supersedes` (run -> run) and
  `informed_by` (run or file -> paper, with `--provenance`).
- **`probe edge add` takes `project:<id>` ends and the `supersedes` / `informed_by` relations.**
  The client checks a link against its generated model before sending it, and that model predated
  them, so `--relation supersedes` and a project or experiment end failed locally. The model is
  regenerated from the server's lineage changes only (a project end takes `derived_from`,
  `supersedes` or `informed_by`); the help lists the `project` type.
- **`probe edge add` links experiments and projects by slug.** `--source`/`--target` take
  `experiment:<slug>` and `project:<slug>` (or `id:<uuid>`, as `probe run move --to` does), so one
  experiment can be linked to another, a project to a project, or a run to an experiment. Both are
  sent as a `project` end: an experiment is a project. The help lists every end type and every
  relation, `supersedes` and `informed_by` included.
- **The MCP `entity(view="lineage")` says where things came from, on projects too.** On a project
  or an experiment it returns its own links in and out (the stored ones, and "built on" derived
  from its runs' reads, marked `derived: true`), its `origin` (a project it builds on or replaces,
  else its parent; none for a top-level project) and its children -- sub-projects, experiments,
  groups, runs -- each with its own `origin`. A run's lineage adds `origin` (its first parent, else
  what it built on, else its group, experiment or project); a file's carries the run that wrote
  it. An experiment's links among its runs and files, which the view always listed, are now under
  `run_edges`. The links come on the first page only, at most 20 per list; anything cut (links past
  that, children past the server's 200, "built on" read over only the newest runs) is flagged
  `*_truncated` and the read says `partial` (`lineage_beyond_window`). `probe experiment edges`
  lists the whole graph among an experiment's runs. Ships with the MCP deploy.
- **`track-work` teaches how to link work, and stops teaching hand-written file links.** The SDK
  records what a run reads and writes, so the skill no longer tells agents (or the Probe daemon,
  which follows the same text) to add `consumes`/`produces` by hand, except for what the SDK
  cannot see: a read by non-Python code or a C reader, or a file uploaded above the run that
  wrote it. A new "Linking" section says to read the facts first (`probe run inputs`,
  `probe run upstream`), to label a fact only when the session says what it was
  (`branched_from`, `evaluates_on`, `retried_from` plus `supersedes`), to add a link no fact shows
  only when the session's own words name its target, with `--reason` and `--evidence`, to link at
  the level the session talks about (run, experiment or project, and `informed_by` a paper), and
  never on similarity alone. The data-processing recipe runs the step under `probe exec` instead of
  writing its edges by hand.

## 0.194.0

- **`log_artifact` stores files over 64 MiB (plan item (g), 2/2 SDK).** Against a server that
  declares `artifact_multipart`, a file over the 64 MiB inspection limit is no longer only a local
  pointer: `log_artifact` STAGES it and returns at once, and the outbox uploads it in parts, 4 at a
  time, straight to object storage. Staging is a hardlink into the outbox on the same filesystem
  (a checkpoint rotated by rename keeps its bytes), else a reflink or copy, and only while free
  space above the outbox's floor covers it and every upload still waiting; otherwise the old
  reference row and warning. One upload moves at a time (the oldest), after every other write in
  each pass and in a lane of its own, so metrics never wait behind a checkpoint; the staged copy
  is hashed on a background thread; a restarted worker sends only the parts the server is missing;
  an upload the server dropped while the machine was away starts again. A staged copy rewritten in
  place aborts the upload and records a reference row, as does a server-side sha256 mismatch; the
  copy is deleted once verified, on failure, or after 7 days. The op lives in a queue of its own
  (`<outbox>/multipart/ops/`) that no earlier release reads, so an older worker sharing the outbox
  never holds or drops it. `finish()` does not wait when a detached worker on durable disk will
  carry the upload (`N artifact(s) still uploading in the background`); when nothing would (a
  client that sends its own writes, a pod or Modal disk) it sends them itself and records whatever
  did not make it as a reference row before returning. A part URL the store refuses (#2075's
  `UploadRefused`) is retried with fresh URLs, and after three refused slices the upload is recorded
  as a reference row. Only a server that ADVERTISES the feature gets a multipart upload: today's
  prod (doors off), a features probe that fails, an older server, an offline run, `strict` (sync),
  `PROBE_ARTIFACT_OPAQUE_POLICY=block` and output capture (D12) behave exactly as before. These
  bytes reach object storage before anything scans them: the part PUT is the one byte path without
  the local credential gate; the server inspects a prefix and records what it finds.
- A script stopped with Ctrl-C exits the way Python normally does again (killed by SIGINT, 130 in
  a shell) instead of exit code 1; 0.193.0 regression from the delivery report at exit.

## 0.193.0

- **Hugging Face Trainer callback: `probe.integrations.huggingface.ProbeCallback`.** After
  `import probe.integrations.huggingface`, `TrainingArguments(report_to="probe")` works; or pass
  `Trainer(callbacks=[ProbeCallback(experiment="...", name="...")])` to choose the run (install
  `probe-research[huggingface]`). On the world-zero process only, it opens the run (or adopts one
  `probe.init()` already opened), merges the model config, `TrainingArguments` (with `hub_token`
  masked) and any PEFT config into the run's config, and logs every Trainer log at
  `state.global_step`, sectioned as W&B does: `train/loss`, `eval/loss`, `test/*`. Evaluation is
  logged once, not twice. Each saved `checkpoint-<step>` folder becomes a `checkpoint` path
  reference whose size is the folder's, summed once per save; with `metric_for_best_model` its
  row carries `monitor`/`score`/`best`, and a folder `save_total_limit` removed is marked
  `deleted`. It never finishes the run. A `train()` whose exception the script caught closes the
  run `failed` (`canceled` after a Ctrl-C) instead of `completed`, and a stop by
  `enable_jit_checkpoint`'s SIGTERM closes it `failed` with its checkpoint recorded. A sweep loop
  gets one run per Trainer, and so does each `hyperparameter_search` trial (named
  `<run_name>-trial-<n>`; a trial transformers pruned closes `canceled`) and the `train()` after
  the search; a `train()` after `probe.finish()` writes to the run open now. A run opened in a
  forked multiprocessing worker (accelerate's `notebook_launcher`, a `Process` per trial) is
  closed when that worker exits, with how it ended, instead of staying `running`. An
  `evaluate()` after `train()` (the reloaded best model, a second eval set) is kept at the last
  step with the label `repeat=1`, `2`, ..., where the server used to drop it for having that
  step already. A second Trainer on a run it shares is warned that its curve is lost where steps
  overlap; with two ProbeCallbacks on one Trainer the one given arguments logs. Other ranks write
  nothing and hold no writer lease yet (a follow-up to the lease API). `import probe` still never
  loads transformers or torch.
- **PyTorch Lightning: a worker's run is closed even when a signal lands as its close starts.**
  torch's launcher forwards the SIGINT it got to its workers as it shuts down; one that arrived
  while the worker's close was setting up its signal hold skipped the close, and the run stayed
  `running` until the reaper. The hold is now set up again after such a signal.

- **A refused upload no longer stops a run's other writes (#2073).** An upload goes to a
  presigned address that carries its own signed permission and no login. When the server at that
  address refused it (401 or 403), the SDK read the refusal as a refused login and held every
  queued write of the run: on a self-hosted server that pointed uploads at the wrong address, 29%
  of a run's points arrived and the run was never closed. Such a refusal now fails only that
  upload. It is recorded on the run as a failed upload, the other writes keep going, and the
  error says to check the server's `public_base_url`. It no longer tells the Probe daemon that its
  key was refused. A network failure of an upload held in memory no longer puts the upload
  address's signature in its error.
- **Delivery trouble is said in the training process while it happens.** The background worker
  writes only to `drainer.log`, so a write the server rejected for good (a dead letter) or a
  refused credential surfaced at `finish()`, hours later, while a dropped write warned once per
  call (44 lines for 50 drops). Now each kind of trouble -- this process's dead letters, an auth
  block, dropped writes, a queue whose oldest write has waited 10 minutes -- prints one line when
  it starts and then at most one every 5 minutes with the running count, each naming its fix,
  prefixed `probe[rank-N]` on a distributed job; `finish()` prints what was held back. The
  writing process reads the outbox's `status.json` (now written compact) and a new
  `dead_letters.json` (the last 32 dead letters: run, op, redacted error, status; retry and
  discard remove theirs) at most every 15 s; a queue that is being worked through is not called
  stalled. The detached worker prints none of this.
- **A Ctrl-C that lands in a queue write, or while a Probe warning prints, stops the process.**
  Both caught it: a Ctrl-C mid-write was counted a dropped write (`probe_finish.dropped_writes=1`,
  a drop warning) and training went on, and one landing inside a warning was lost. Both let
  KeyboardInterrupt and `sys.exit` through now; the interrupted write leaves no partial file.
- **A training process reports its delivery counts to us.** Every 15 minutes while it writes,
  and at each `finish()`, it sends one `sdk.delivery.summary` telemetry event: how many writes it
  attempted, dropped, sent directly and had dead-lettered since its last report (dead letters by
  HTTP status; a floor, from the last 32 the outbox records), and how many are queued, for how
  long, and how long delivery has been auth-blocked. Counts only, never a key, body, name or path;
  `PROBE_TELEMETRY=off`, self-hosted servers and offline runs send nothing, as for every other
  client event. The close's report re-reads the outbox and is handed over before the process
  exits (with a slow PostHog, exit can take up to the telemetry sender's usual ~3.5 s bound).
- **The background delivery holds at most 2 MiB of parsed queue between passes.** Its pass cache
  (0.191.0) kept every queued write parsed for as long as the process lived: 58 MB for 20,000
  queued one-point writes on a training node. It keeps the oldest 2 MiB of op files (about 4,300
  writes; 12 MB at 20,000) and reads the rest again each pass, as before 0.191.0.
- **A queue write that never landed gives its sequence number back.** A Ctrl-C or a disk error in
  the middle of a write left `probe outbox status` showing a producer one write ahead of anything
  that could be delivered, with no gap to explain it (a dropped write also took a second number
  for its gap). Now the number is returned, and a dropped write's gap takes it.
- **`finish()` no longer waits seconds for the background worker to re-scan the queue.** The
  detached worker re-checks every write it sends for credentials, and it ran without the scrub
  cache the training process uses, so each point's constant keys went through the full scanner
  again: 7.1 s of CPU for 2,000 five-key `log()` calls, which `finish()` waited out. It uses the
  cache now (0.9 s); locally, `finish()` after 2,000 steps went from 2-3 s to 0.4 s.
- **A close that runs out of time still tells the server it is draining (2.8).** The "draining"
  lease beat went out under the close's already-spent deadline and was never sent, so the lease
  expired while the worker was still delivering. It gets its own budget of at least 1 s.
- **A revoked token shows in `probe doctor`, `probe outbox status` and the outbox banner.** Since
  each write carries its credential, a refused one is set aside by credential and the queue-wide
  auth block stays empty; these views now read both. "Delivery has been stopped for N min" counts
  from the first refusal instead of the latest retry.

## 0.192.0

- **A `PROBE_TOKEN` job's queued writes go out with its own token (#2035).** On a machine where
  someone had run `probe login`, the outbox worker sent a `PROBE_TOKEN` job's queued writes with
  that stored login instead: in the same team they were written as the other person, and across
  teams the run was not found and the writes were dead-lettered. Another job's `flush()` could do
  the same with its own token. Each queued write now records where its credential came from
  (`PROBE_TOKEN`, the stored login, or a token passed in code) and a one-way fingerprint of it,
  never the token itself. Whatever drains the outbox sends a write only with the credential that
  queued it: the worker a job starts inherits its `PROBE_TOKEN`, and the job's own
  `flush()`/`finish()` delivers the rest. A write nothing here can match stays queued. It does not
  count as a failed attempt and does not hold up anyone else's writes, and `probe outbox drain`
  says how many it kept and why. Writes queued by an older release keep the old rule.
  - A write queued under the stored login goes out with a later login of that context only when
    `GET /v1/me` says the new login is the same team and user. It used to go out with whoever was
    logged in by then. Which account a token is gets recorded by `probe login`, the setup wizard,
    a job's heartbeat, and the first write that goes out with that token. A login that never
    delivered anything before it was revoked has no recorded account: its writes wait, and
    `probe outbox discard --held` drops them. A worker from a release before this one still sends
    such a write with whoever is logged in -- as it does a `PROBE_TOKEN` job's writes when that
    token is the stored login's and someone else logs in over it -- and, draining the shared
    queue, it can close a run before writes of it queued under another credential.
  - When the API refuses a write's credential (a revoked or logged-out login), only that
    credential's writes are set aside. The rest of the queue keeps delivering, and nothing blocks
    the machine's outbox. The refused credential is tried again once every 5 minutes, or at once
    after `probe login`.
  - A `PROBE_TOKEN` or in-code credential's writes queue apart, in
    `credential-v1/<fingerprint>/` under the outbox, with their own worker. No older release reads
    that folder, so none can send them as the stored login, and a job whose token nothing else
    holds no longer waits behind another credential's busy worker. The stored login's writes stay
    in the shared queue, and so do a `PROBE_TOKEN` job's when its token IS the stored login. A
    run's close (on a run with writer leases, its lease release) waits for that run's earlier
    writes in another credential's queue, for at most 10 minutes, so it lands before the server's
    crash sweep, and never for writes of an older attempt the server would refuse anyway.
    `finish()` and `probe run end` count the run's writes in every queue, an older attempt's
    aside, and a relaunch taking over its run names the old attempt's writes it cannot send (a
    rotated `PROBE_TOKEN`). A `probe run end` from a release before this one cannot see those
    queues and may close the run first. A `PROBE_TOKEN` rotated while writes of the old token are
    queued leaves them waiting for that token
    (`PROBE_TOKEN=<old> probe outbox drain`, or `probe outbox discard --held --credential
    <fingerprint>`).
  - A worker left with only writes it may not send stays up and backs their run off, instead of
    exiting and being forked again on the next write (about one fork a second before). Credential
    queues left empty are removed after an hour.
  - `probe outbox discard --held` drops queued writes no credential on this machine will send as
    things stand: a stored login's whose account is unknown or another account's, or whose login
    was refused and is still the stored one. Another job's `PROBE_TOKEN` writes are that job's to
    send and stay, unless `--credential <fingerprint>` names that credential (`probe outbox drain`
    prints it).
  - An `/ingest` write names the ingest token it goes out with, not the personal token, so no
    drainer sends it with its own ingest token.
  - A client built with a token in code delivers its queued writes in-process (the detached worker
    cannot hold that token); before, they waited for `flush()` or `finish()`.
- **Each process writes its run under its own lease (SDK reliability 2.8).** Against a server that
  declares `run_writer_leases`, `probe.init()` (and `probe exec`, and every rank that joins through
  `PROBE_RUN_ID`) registers a lease for its process and beats that lease instead of the run-level
  heartbeat: every wait jittered by +-20% (the first one too, so hundreds of ranks started together
  never beat together; ranks of a large job also spread their attach by up to 2 s), carrying a
  progress counter (every `log()`/`step()` row, span and artifact upload; new `run.progress()` for
  loops that log rarely) with its idle time, a rolling 24 h p99 and the longest gap of the whole
  run, for the coming stall detector. `finish()` releases the lease with the run's verdict instead
  of setting the status itself, and the server closes the run once its leases say the work ended,
  with the worst verdict of every writer: a rank that failed is no longer hidden by the owner
  finishing `completed`, and the first rank to exit no longer closes the run for everyone. A job
  that joined a run no finalizing launcher will close holds an owner lease and sends no status of
  its own. A deferred close queues its verdict first, then keeps its lease draining (not beating)
  for up to an hour with one bounded beat. A release names its writer, so a lease no beat ever
  registered still counts. Every request carries `X-Probe-Leases: 1`. A run that is not on leases --
  opened before this server version or by an older client, joined by one, or with the server's kill
  switch off -- is closed exactly as before, with a status write, and the lease is then released. A
  writer whose run a wrong death report crashed heals under its own lease, and is told when its
  lease was reported gone. Behaviour change on such servers: a non-zero rank's own verdict now
  counts (worst wins) instead of being ignored. Older servers: unchanged.
- **A killed or crashed training process shows as `crashed` within seconds.** SIGKILL, the OOM
  killer, a segfault or an unhandled SIGTERM runs no Python, so the run used to read `running` until
  the server's 15-minute reaper. The output-capture helper (which outlives the process) now notices
  the moment its parent is gone, even while a child process still holds stdout, and reports it
  (about 1-2 s on the same host); the server ends the run only when this process was its sole
  writer. `probe exec` launchers never report their child. If a report (or the reaper) is ever
  wrong, the live process's next heartbeat puts the run back to `running` on the same write epoch;
  a `crashed` a person set (`probe run end --status crashed`) is left alone. A report made during a
  network partition is queued and delivered later; a script that daemonizes (forks, and its child
  keeps writing the run) is not reported dead (while that child lives: a pid the kernel reused for
  another process does not count); with `PROBE_BOX=1` on a HOME shared across machines
  or containers, the node agent judges only its own host's runs. Needs a server that declares
  `run_writer_gone`.
- **A relaunch's takeover no longer strands, retires or waits on the previous attempt's close.**
  A relaunch killed hard while it held the previous attempt's queued close aside left it there
  until another relaunch ran on the same machine -- past the reaper's 15 minutes, so the run read
  `crashed` and sent a crash email. Now the next outbox drain, or the next client started on that
  outbox, puts it back at once when its holder's process is gone, and a hold expires in under 10
  minutes anywhere. A previous attempt that FINISHED (its `completed` close still queued) is no
  longer continued: the close is delivered and the relaunch refuses like any resume of a
  completed run (`on_conflict=probe.Rewind(step=N)` still reopens it). And when the server cannot
  be reached, the relaunch stops delivering the old backlog at the first failure instead of
  looping for two minutes outside `probe.init()`'s own budget.
- **`PROBE_MODE=offline` and `probe sync` (plan 2.12).** `probe.init(mode="offline")` trains
  with NO network call: the run gets a local id (`local:<creation_key>`) and everything it
  logs -- metrics, spans, artifacts (staged, so the folder is self-contained), config updates
  (`update_config`, `run.config.x = ...`, `run.config.update(...)`) and its close, with the
  client's own start and end times -- queues in `<outbox>/offline/<key>/` (the outbox root,
  also under torchrun/SLURM), which no background worker touches and the 500k op cap does not
  cut short (the free-space floor still applies; the close is always queued, and writes the
  floor refused are counted on it as `probe_finish.dropped_writes`). `probe sync [DIR]`
  delivers it later with the current login (from any machine: a copied folder works; it also
  looks in every rank's `rank-*/offline/`); `probe sync --list` shows what is unsynced. A run
  whose create is refused says why and names the fix: `probe sync <dir> --project <slug>` (its
  recorded project is gone) or `--on-conflict supersede` (another run holds its `external_id`:
  it opens as `<id>-r2`, linked to that run, a dead one tagged `superseded`, as online). Those
  flags change only a run whose folder you name or whose create was refused. Exit 0 when
  everything found is synced and nothing was dropped while recording, 2 when not, 1 when
  nothing was found (with where it looked, on stderr). A run synced twice, or from two copies,
  is one run (its creation key). `probe sync` refuses a server that predates offline sync, a
  folder recorded against another server URL, and another team -- the team is known only if
  the recording machine had reached the API under that credential; a folder whose team is
  unknown syncs with a warning on stderr and in the JSON (`metadata.offline_owner:
  "unknown"`) and is refused under another login context. `--allow-mismatch` overrides the
  URL and context checks. `PROBE_INIT_FALLBACK=offline` records offline when an online
  `init()` runs out of its budget: a create it sent without getting a run back keeps its key,
  and `probe sync` sends the offline create first, replaying the sent request only if the
  server says that key already made a run -- so a create that landed is found, not
  duplicated, and one that did not (a gateway 503, a refused connection) becomes a real
  offline run with its real start. The fallback does not engage when the create came back
  before init failed, or for an `external_id` on the default `on_conflict` (online that
  resumes a crashed run; offline cannot): pass `on_conflict="supersede"` or `"error"`. If an
  online create had landed, the server reads that run `crashed` from about 15 minutes after
  init until the sync, and queues a crash notice (the 3 h crash-email floor holds the email
  back). Not yet offline: code snapshot, output capture, read capture and hardware metrics
  (skipped, and named on stderr and in `metadata.offline.skipped`). A recording killed before
  its close reads `running` after sync until the server's 7-day sync grace ends. Each write
  is one op file and, at sync, one request (a 1M-step run is ~1M files and ~1M POSTs): the
  coalesced drain (plan 1.2) does not merge an offline run's writes yet.
- **A second `probe.init()` closes the first run instead of abandoning it.** Before, the first run
  was left open with nothing bound to it: the reaper later called it `crashed`, and while both were
  open neither swept its output files. `probe.init(reinit=...)` takes W&B's values, and the default
  here is `"finish_previous"` (and `True`): the open run is closed `completed` with
  `probe_finish.closed_by: "reinit"` in its summary. The whole close, letting go of its client
  included, takes at most 10 s (`PROBE_FINISH_TIMEOUT_SEC` if lower); whatever is left is queued
  with the close behind it. One line says so, and a failure to close is a warning, never an
  exception. Ctrl-C during that close queues the run `canceled` behind its data (it used to be left
  `running` and reaped `crashed`); once its final status write is on the wire, that same status is
  queued instead, so a `canceled` never lands later over a `completed` the server already applied.
  Another thread logging during the swap waits for the new run instead of hitting "no active run",
  for at most 10 s after the old run is let go; past that, `probe.log()`, `probe.log_hw()` and
  `probe.log_artifact()` drop their writes with one warning until the new run opens, and never
  raise; the new run's close counts them in `probe_finish.dropped_writes`. `probe.span()` and
  `probe.update_config()` raise a `RosError` in that window, saying a reinit is still opening the
  run. A thread whose own run was closed from elsewhere no longer falls through to another
  thread's run. `probe.init(mode="disabled")` follows `reinit` too: it closes an open online run
  the same way instead of leaving it for the reaper.
  `"return_previous"` (and `False`) returns the open run and names the arguments it ignored;
  `"create_new"` opens another run without unbinding the first (`probe.log()` still reaches the
  first); it is closed at exit if its handle never closed it, and closing it releases the client
  `init()` built for it. Only a run opened by the SAME scope is replaced: an `init()` in a worker
  thread, an asyncio task, `asyncio.to_thread` or a forked child opens its own run beside the one
  it can see, as before. A run joined through `PROBE_RUN_ID` is returned, never closed. A reinit
  inside `with probe.init() as run:` closes that run once, and an exception after it is not filed
  as that run's crash.
  - **Notebooks:** a re-run cell replaces the run it opened when the kernel keeps context
    variables across cells. Checked: ipykernel 6.17.1, 6.29.5, 7.1, 7.2 and 7.3 do; 5.5.6 and
    7.0.1 do not; a top-level-`await` cell never does. There, and after an `await` cell,
    `probe.log()` still reaches the newest run, but a later `init()` opens a run beside it instead
    of closing it.
  - **Differences from W&B:** W&B's script default is `return_previous`, and it applies
    process-wide; here the default is `finish_previous` (D16) and every choice applies to the
    caller's own scope. W&B's `finish_previous` finishes every active run, `create_new` ones
    included; here it closes only the run the caller opened, and `create_new` runs close with
    their handle or at exit.
- **`finish()` after `run.execute()` keeps the verdict `execute()` wrote.** `execute()` closes its
  run from the command's exit code; a `finish()` (or the exit hook, or a reinit) after it used to
  write a second status over it (`failed` became `completed`), or, on the idempotent close, skip
  the rest of the close, leaving the crash breadcrumb armed so the next `probe.init()` on the
  machine filed a `hard_exit` on a run that had closed cleanly. Now the rest of the close runs
  (breadcrumb, read capture, hardware, output capture, delivery) and only the second status write
  is skipped. The same holds after any `set_status("completed" | "failed" | "canceled")`. Dead
  letters or dropped writes found then are warned about and returned in `finish()`'s report, but
  cannot be marked on a run whose close already went out. A verdict `set_status` could only queue
  (a 503) and the server then refused for good is still reported `close_rejected`, never "already
  sent". The close's wait for read-capture hashing is now at most half of what is left of its
  deadline (it was up to 30 s outside it).
- **`finish()` keeps its deadline with a hung GPU query or a held `log(commit=False)` row, and
  Ctrl-C while it hashes the run's reads cancels the run.** The close joined the hardware collector
  for 5 s and then sampled again on the caller's own thread, so a driver call hung on a failing GPU
  held `finish()` with no limit; it now waits at most half of what is left of the deadline, never
  samples from the caller, and drops the collector's unsent windows with a log warning when it
  does not stop in time. A `log(commit=False)` row was sent before the deadline started, so under
  `PROBE_ASYNC=0` a hung API held the close for up to 30 s past it; it is now sent inside the
  deadline, and queued if it runs out of time. A Ctrl-C while the close waited on hashing was
  swallowed and the run closed `completed`; it now propagates and the run is queued `canceled`,
  as documented in 0.190.0. `Client.close(timeout=)` caps the join of the client's exporter (5 s
  otherwise); what it could not send stays queued for the background worker.
- **A run's console log reaches Probe while the run is alive (plan item (h)).** Output capture
  uploaded what a run printed as `probe/run.log` only when it ended, so a three-day job showed
  nothing of its console until then, and a whole-pod death could leave nothing. Now the output
  helper also appends every byte to 1 MiB spool segments (at most 16 MiB unsent; past that the
  OLDEST is dropped and the server marks the gap, and nothing ever waits on it), and a thread in
  the process that owns the tee ships complete lines every 15 s (`PROBE_LOG_STREAM_SEC`, at least
  1 s) to `POST /v1/runs/{id}/log-chunks`: `\r` redraws collapsed to their last frame, a partial
  line (up to 64 KiB) and an unterminated private key block (up to 16 KiB) held back, redacted by
  the same function as the final log. A line too long to hold is never cut inside a run of
  token-shaped characters (the run waits for the next chunk; one past 20 KiB is withheld), and
  the chunk after such a cut is redacted together with the text before it. Best effort, not
  journaled: a failed POST is retried with the identical body (never sooner than a 429's
  `retry_after`; one POST, the transport's own retries included, takes at most 10 s and never
  outlives a close's or a recovery's budget), the server ignores a chunk it already has, and a recovery after a hard death
  sends the spool's last lines as the attempt the dead process recorded; a log name whose spool
  survived is never reused. At most 256 KiB of raw output per wake-up (about 1 s of CPU on dense
  output: the redaction plus the transport's own scrub, in the training process), skipping to the
  newest output past a 1 MiB backlog; the close gives the last lines at most 5 s and a quarter of
  what it has left. Rank 0 only by default (`PROBE_LOG_STREAM=rank0|all|0`; `all` names each
  stream `run.rank<N>`); a server that does not declare `run_log_stream` gets no spool and no
  request, and the answer is kept for the process. The final `probe/run.log` artifact and its
  2 MB + 8 MB budget are unchanged.
- **The credential scanner recognizes the shapes the #2000 reviews found (security).** Code
  capture now withholds, and redaction now replaces, credentials written as: camelCase and
  acronym fields (`postgresPassword`, `openaiApiKey`, `wandbApiKey`, `DBPassword`,
  `SMTPPassword`, `JWTSecret`), including generated letters-and-digits passwords
  (`postgresPassword: a8Kd93jLm2Qx`, `const dbPassword = "S3cr3tPassw0rd"`);
  `f"..."`/`r`/`b`/`u`-prefixed literals; defaults a program falls back to
  (`os.environ.setdefault("WANDB_API_KEY", "...")`, `os.getenv("X", default="...")`,
  `os.environ.get("X") or "..."`, `process.env.X || "..."` / `?? "..."`,
  `add_argument("--wandb-api-key", default="...")`, a pydantic `Field(default="...")`);
  environment-style `*_KEY` names, `encryption_key`, `signing_key`, and vendor keys
  (`azure_openai_key`, `openaiKey`, `azureOpenAIKey`) with a long value that has a digit
  (so an `.env.example` holding a real `AZURE_OPENAI_KEY` is caught by its content); a
  Kubernetes `- name: X_KEY` / `value: ...` pair; a Dockerfile `ENV X_KEY value`;
  `wandb_key:` and a 40-hex `key:` under a `wandb:` block; netrc `machine ... password <x>`
  and `default login ... password <x>`; `<password>...</password>`; a Gradle
  `password "..."` line; docker `auth` values (JSON, single-quoted, YAML) that decode to
  `user:password`, `identitytoken`, and `.npmrc` `_auth=`; and URL passwords longer than 64
  characters made of token characters (AWS CodeArtifact, GCP `oauth2accesstoken:ya29...`).
  New rule names on artifact rows: `env-default`, `cli-default`, `env-pair`,
  `dockerfile-env`, `netrc-password`, `xml-password`, `docker-auth`. Kept clean on purpose,
  each measured on 51k third-party and first-party files: a value followed by `(`, an f-string
  with `{...}` in it, a template slot or shell variable (`{password}`, `{{ x }}`, `...`,
  `$GH_TOKEN`), a UUID or a raw-string regex under a widened key, a camelCase field set from
  code or described in prose (`adminPassword = _messages.StringField(1)`,
  `postgresPassword: see 1Password`), pagination tokens (`NextToken`, `nextPageToken`),
  public keys (`LANGFUSE_PUBLIC_KEY`), names about a credential (`secret_name`, `TOKEN_URL`),
  owner-less keys (`cache_key`, `sortKey`), and publishable keys (`phc_`,
  `pk_live_`/`pk_test_`). A URL password over 64 characters no longer runs through quotes
  and commas, so a JSON row from an `https://` value to a later email's `@` is left alone.
  What is still not read is listed in agent/TODOS.md. The tap carries the same scanner;
  merging updates it.
- **Code capture's credential scan stays linear on one-line files, and its time budget holds
  for every file (security, #2034 re-review).** After a camelCase key the scanner read the rest
  of the value's line once per match, and code capture's verdict on each key-name hit searched
  and split the whole line around it, so a one-line file was quadratic: a 2 MB line of
  `dbPassword=...,` took 24-37 s, a 1 MB line of `azure_openai_key=...` 550 s, a 200 KB
  minified JSON of `"password"` rows 27 s. Each now reads a bounded stretch (1 KB after the
  hit, 256 characters before it), a dropped hit is re-read once per distinct span, and a
  file's words are counted once: those files take 0.2-1.4 s. Files up to 2 MiB now get
  `PROBE_SNAPSHOT_FILE_SCAN_BUDGET_SEC` (3 s) too, checked between hits: past it the file is
  withheld with reason `scan_budget` and the run warns, as a bigger file already was. A
  notebook's code cells are read JSON-decoded, as Python (up to 16 MiB): `.ipynb` JSON escapes
  every `"`, so `OPENAI_API_KEY = "..."` and `wandb.login(key="...")` in a notebook went up
  (on main too). `OPENAI_API_KEY = "<32 letters>"`, `DJANGO_SECRET_KEY` and `GPG_PRIVATE_KEY`
  with an all-letter value are caught again (the new `*_KEY` rule took the whole name and
  dropped it for having no digit). No longer withheld: UI labels under camelCase keys
  (`"forgotPassword": "Forgot password?"`, `newPassword: "New password"`,
  `"Passwort vergessen?"`; a value with a space needs a digit, and every camelCase value 6+
  characters, so `"n/a"` too), names of secret objects (Helm `existingSecret: pg-auth-v2`,
  `imagePullSecret: regcred-v1`, `tlsSecret`, `certSecret`), netrc words in prose
  (`machine gpu01 password reset`: without `login` the value needs a digit or a symbol), and
  lower-case variables (`password "$mavenPassword"`, `password $github_token`).
- **A job requeued onto the same run no longer fails at `init()` for 15 minutes.** When a pod is
  replaced before the server notices the old one died, the relaunch (`on_conflict="auto"`,
  `"resume"` or `probe.Rewind`) now takes the run over once it has been silent for three heartbeat
  intervals (at least 3 minutes), waiting up to `PROBE_TAKEOVER_WAIT_SEC` (300 s; 0 in a notebook)
  with one line on stderr. An incumbent that is still talking is a live duplicate and still
  conflicts. Before any resume or takeover, whatever the previous attempt left queued in the outbox
  is delivered first (up to `PROBE_TAKEOVER_DRAIN_SEC`, 120 s, which bounds every request of the
  drain), because the new attempt's write
  epoch would refuse it; before this, resuming a crashed run on a shared outbox threw that backlog
  away. The previous attempt's own queued close is held aside meanwhile and retired only once the
  takeover succeeds (the relaunch owns the verdict, and its "draining" tag is dropped); a relaunch
  that is refused puts it back, so the finished run still closes. The wait and the backlog
  delivery have their own bounds and are not cut off by `probe.init()`'s retry budget. A process
  whose run was taken over says so once and stops heartbeating. Needs a server that declares `run_reopen_takeover`; an
  older one keeps the old conflict.
- **A close that fails on its last request is queued even behind a full outbox.** When
  `finish()`'s own terminal status write failed (a blip on the last request), its copy for the
  outbox went through the queue-length cap, so a queue full of other runs' writes refused it:
  the close was lost and `finish()` reported it delivered. It is admitted now, like the deferred
  close, and a close the outbox cannot take at all is reported `close_unrecorded`. A write about
  to be refused at the cap counts the queue without holding the append lock.
- **The outbox delivers a backlog in a few requests instead of one per `log()`.** Delivery sent
  one POST per queued write, about 13 a second at a real round trip, so a 2,000-step run took
  over two minutes to drain after the loop ended. A run's consecutive metric writes now go in
  one POST (up to 5,000 points or 2 MiB), each still removed and counted on its own once it
  lands. Only writes whose every point has a step or a client timestamp merge, so writes queued
  by older releases drain one by one as before. If the server refuses a merged POST for a
  reason one write could cause (413, 422, a 500), it is split in halves until that write is
  found: it alone is charged or dead-lettered and its neighbours land. A
  refusal true of the whole batch (a fenced epoch, a deleted run) dead-letters it in one request;
  a busy server (503, 429) stops at the first write, as it would unmerged. A write that already
  failed is sent alone, and so is a batch whose answer was lost. Every metrics write (`log()`,
  `log_derived*`, the hardware rail, `probe import wandb`) now drops, with one warning per kind, a
  point whose step is outside 64 bits or whose key is over 2 KiB, which the server answered with a
  500. Step records are not merged.
- **Low disk, a bad HOME and Kubernetes no longer drop writes silently.** Below the outbox's
  free-space floor every write was dropped (live: 0 of 30 points arrived with the network fine,
  and `finish()` said `completed`). The floor now scales with the volume (5 %, between 256 MiB and
  2 GiB; `PROBE_OUTBOX_MIN_FREE_BYTES` still overrides it), and under it a write is sent straight
  to the server when its run has nothing queued (5 s at most on the training thread; after a
  failed attempt, none for a minute, doubling to ten), counted as `probe_finish.direct_sends` on
  the run. A write whose run still has queued writes is dropped with a capture gap as before, so
  a direct write never overtakes an older one of the same step. A HOME that is missing, read-only
  or on a filesystem without file locks no longer crashes `Client()` or drops every write: the
  queue falls back to `$TMPDIR/probe-outbox-<uid>` with one warning that it does not survive a pod
  or machine restart; only ever a directory this user owns with no group or other access (one
  someone else made there is never used, listed or drained). `probe outbox status` now lists every outbox on the machine (a distributed
  job's `rank-*` queues, the fallback) with totals, and `probe outbox drain --all` / `retry --all`
  act on all of them. On Kubernetes without `PROBE_OUTBOX_DIR`, `probe.init()` says once that the
  queue is lost with the pod; a new process kicks the worker for a queue it finds waiting.

## 0.191.0

- **PyTorch Lightning logger: `probe.integrations.lightning.ProbeLogger`.** Pass it as
  `Trainer(logger=ProbeLogger(experiment="...", name="..."))` (install `probe-research[lightning]`).
  It opens the run on global rank 0, or adopts one `probe.init()` already opened, and logs each
  `self.log` value at the trainer's step. Every checkpoint `ModelCheckpoint` keeps becomes a
  `checkpoint` artifact: a path reference by default (`checkpoints="upload"` stores the bytes),
  with the monitored score in `meta.score` and the callback that saved it in `meta.callback`;
  only a callback with a `monitor` marks a row `best`. A sharded checkpoint folder is recorded
  with the size of what is inside it, summed once per save. It never finishes the run itself.
  When Lightning stops on Ctrl-C the run closes `canceled`; on SIGTERM or an exception, `failed`.
  No process hook sees either, because Lightning turns both into ordinary exits. Probe errors
  never stop training (`strict=True` makes them raise). Under DDP, rank 0 shares the run id and
  epoch with every rank, and the other ranks write nothing. Their writer leases come with the
  lease API (plan 2.8). Lightning's hyperparameters are merged into the run's config
  (`run.update_config`). A sweep loop in one script gets one run per Trainer: a new
  `ProbeLogger` closes the run the previous one opened, `failed` if its error was caught, and an
  older logger still writing to that closed run is warned. A second Trainer on a run it shares
  (the script's `probe.init()` run, or `probe exec`'s) is warned that where both log a metric at
  the same step the server keeps the first point. Under `probe exec`, a trial error the script
  caught no longer closes the launcher's run `failed` when the process exits 0. Under
  `ddp_fork` / `ddp_notebook` / `ddp_spawn` the worker closes the run it opened on the way out:
  `canceled` on Ctrl-C, `failed` on SIGTERM or an error, as under torchrun. That close is capped
  at 20 s to fit torch's 30 s grace before SIGKILL, and a SIGTERM or SIGINT arriving during it
  waits for it (at most 25 s) instead of cutting it short. As top-k pruning moves on, earlier
  checkpoint rows are refreshed (`best`, `superseded`, `deleted`). `import probe` still loads
  neither Lightning nor torch.
- **A folder artifact reference's size walk is bounded** (100,000 entries or 2 s); past it the
  size is a lower bound and `meta.size_partial` says so. `hash_content=True` on a folder now warns
  and records `meta.content_hash_skipped` instead of being ignored silently. A `size_bytes` the
  caller passes is used as is: the folder is not walked again.
- **The generated models know a long run's smoothing receipts.** `MetricExactness` in
  `probe._generated.models` gains `sampled_smoothed` and `sampled_unsmoothed`, which the server's
  chart reads (SDK reliability plan 1.8) put in `read_provenance.exactness` for a long series
  whose smoothing came from sampled buckets, or was not drawn. Only those two members are added;
  the rest of the file is unchanged.
- **`.probeignore`: tell Probe which files not to capture.** Code capture stored everything git
  could not supply and output capture everything a run wrote, so a team keeping datasets, result
  dumps or checkpoints beside its code had no way to say "not these" short of gitignoring them.
  Now a `.probeignore` at the git toplevel (or the working directory outside a repo; only that
  one file, a nested one is not read), in gitignore syntax, plus `PROBE_IGNORE` (one pattern per
  line or comma-separated) and `probe.init(ignore=[...])` / `Client.run(ignore=...)`, keep
  matching paths out of code capture (the manifest's `skipped` lists up to 50 as `probeignore`,
  an excluded folder once rather than each file in it, and `n_probeignore` counts them all), the
  output sweep, and read capture (an excluded read is never recorded or hashed, matched both as
  opened and through its symlinks; `probe exec` filters its child's reads the same way, also when
  a later run recovers a Ctrl-C'd child's reads, and exports `PROBE_IGNORE_FILE` and its `ignore=`
  patterns so the child uses the same rules). Under `probe exec`, a child's own
  `probe.init(ignore=[...])` reaches the launcher's read and output capture too, but not its code
  snapshot, taken before the child started: the child warns, and such patterns belong in
  `.probeignore` or `PROBE_IGNORE`. Outside the repo, for an `outputs=` folder in `$SCRATCH`,
  unanchored `.probeignore` patterns (`*.jsonl`, `ckpt/`) and every `ignore=` / `PROBE_IGNORE`
  pattern apply relative to that folder, while an anchored `.probeignore` line (`/data`) cannot,
  and output capture says so once; for a file read from elsewhere, only unanchored patterns apply
  (from any source), matched against its whole path. A file under an excluded folder stays out
  whatever a later `!` line says, as in git, and a credential stays withheld whatever a `!` line
  says; an explicit `log_artifact` or `snapshot(include=...)` still wins. Matching is
  case-sensitive, trailing spaces are trimmed as git trims them, and it is bounded: a pattern
  with more than 3 `*` (whose regex could backtrack for minutes on one name), lines past 1,000
  and bytes past 64 KB are skipped with a warning. Only a regular file is read (a FIFO no longer
  hangs `probe.init()`), an unreadable one is warned about, and a UTF-16 one (PowerShell's `>`)
  is decoded. Nothing changes without a file, variable or argument, and then `pathspec` (a new
  dependency, `>=1.0`: 0.12 dropped a pattern's leading spaces) is not even imported.
- **Python 3.10 is supported, and every installer now asks for `probe-research[all]`.**
  `probe-research` installs on Python 3.10 (it required 3.11), for ML images that are still on
  3.10. `tiktoken` is a tested range (`>=0.8,<0.14`) instead of an exact pin that clashed with
  other packages. New extras: `cli` (typer, questionary), `mcp` (mcp, anyio, tiktoken) and `all`
  (both). This release still installs them by default. The next release drops them from the bare
  install, so a training environment can `pip install probe-research` next to stacks that pin
  mcp 2.x (vllm 0.30 does). To get ready, `probe update`, `probe daemon install`, the setup
  wizard and the persistent installer install `probe-research[all]` from now on, and `probe update`
  keeps what you added to the install (`uv tool install --with ...`, `pipx inject`, other extras).
  The `npx probe-research` launcher asks for `[all]` from its next published version. If the
  CLI's or the MCP server's dependencies are missing, `probe` and `probe-research-mcp` print the
  command to install them instead of a traceback. The SDK itself (`import probe`, `probe.init()`,
  logging, the run lock and delivery) no longer loads the CLI at all.
- **A resumed job's "start over" hint files the new run beside the old one.** The drop message
  for a job that rejoined its run through `PROBE_RUN_ID` now says
  `probe exec --project id:<project> --parent <run> --relation retry -- ...`: without `--project`,
  `probe exec` filed the retry in whichever project was active.
- **`probe.init()` rides out a short API outage, and a lost create never makes a second run
  (plan 2.5).** It retries for up to `PROBE_INIT_TIMEOUT_SEC` (default 90 s; `0` = the old
  few quick retries) with jittered backoff and the server's `Retry-After`, then raises (a
  refused API used to raise after 1.9 s). 401/403/404/409/422 still come back on the first
  try. Every run create now carries a fresh `creation_key`; when a create's answer is lost
  (read timeout, dropped connection, 502/503/504) the SDK sends the same request again only
  on a server that declares `run_creation_key`, and gets back the run it already made
  instead of a duplicate (or, with an `external_id`, a 409 against itself). On an older
  server the failure stands, as before. A 429 on a create is re-sent on any server (nothing
  was processed). A `Retry-After` is waited out whenever the budget still holds it, the same
  for a create as for a read; one longer than the budget has left (or, with no budget, than
  10 s) raises at once instead of sleeping. The first retry prints one stderr line. The code
  snapshot, output capture and the heartbeat thread are outside that budget
  (`transport.without_patience()`).
- **`PROBE_MODE=disabled` (or `probe.init(mode="disabled")`).** `probe.init()` returns a run
  that records nothing: no client, no network, no outbox directory, no exit hooks, and every
  `probe.log()` / `span()` / `finish()` is a no-op. An unknown mode (`PROBE_MODE=disable`)
  raises instead of going online; `offline` is not available yet (plan 2.12).
- **A resumed run's dropped steps are reported more precisely (#2022 follow-ups).**
  - A job that rejoined its run through `PROBE_RUN_ID` is told what works from there:
    `probe.init()` joining a run takes no `on_conflict`. To overwrite the old range, relaunch
    without `PROBE_RUN_ID` and with `probe.init(external_id=..., on_conflict=probe.Rewind(step=N))`
    (a run with no external id cannot be rewound, and the message says so); to start over,
    `probe exec --parent <run> --relation retry -- ...`; to keep both curves,
    `probe run fork <run> --step N`.
  - `finish()` writes the final drop count to the `resume_guard` span and closes it. The span was
    written only on the 1st, 10th and 100th drop, so a run that finished inside its dropped range
    left it `running`, often with a stale count.
  - That span no longer inherits the caller's open span or unit, and each rank keeps its own
    (`resume_guard/rank-N`).
  - The Harbor connector records a dropped reward as `dropped`, not `queued`.
- **`finish()` spends far less time preparing the list of files a run read.** With read capture on,
  a run that opened 10k files spent ~4 s (6.8 s on a slower box) at `finish()` scrubbing that
  list: the outbox's generic scrubber walked every field of every row several times. Each row is
  now built by type -- the path scrubbed for credentials once; hashes, sizes and timestamps
  checked against their exact shape (a malformed hash or size is sent as empty, never as text,
  and a row whose timestamp this module did not write is dropped and counted in coverage as
  `malformed_rows`, never re-dated to the collect time) -- and the outbox's own scrub passes skip
  the shape-checked fields and look the paths up in a scrubber cache (strings up to 256
  characters, 16,384 entries). 10k realistic reads (every row its own path, hash, fingerprint and
  timestamp): 47,957 full scans -> about one per path; ~3.9 s -> ~0.7 s. The stored paths are
  unchanged; a credential in a read path is still redacted. The cache is OFF unless a
  researcher's run turns it on (`Client.run`, `probe.init`): the hosted MCP server forbids it,
  the API's W&B import worker keeps it off, and the vendored server copy can never enable it, so
  no process that serves several tenants holds one tenant's strings or reveals, by timing,
  whether another sent one.
- **A script that simply ends closes its run even when its last write collides** (fixed in
  0.190.0, now pinned by a test). The exit hook runs the same `finish()` and swallowed its
  `run … not closed` error, so the run was neither closed nor queued to close: it read
  `running` until the reaper called it `crashed` and mailed a crash notice about a run that
  had succeeded. Scripts launched from a coding agent hit it most, because their run writes
  also record the agent session, which holds the run row longer. 0.190.0's `finish()` change
  is the fix; `test_exit_close_after_busy.py` pins the exit path with and without a Claude
  Code environment.
- **`log()` does a fraction of the disk and scrubbing work on the training thread.** Each call
  made 8 fsyncs (the op file, `.seq`, `status.json` and the producer record, each file and its
  directory) and ran the credential scrubber over the metrics body three times, plus once more
  over `status.json`. Now it fsyncs only the op file, before renaming it into the queue, and the
  body is scrubbed once (by `Client.write`; the journal scrubs only the op's envelope, and still
  scrubs any body it did not get from `Client.write`). The bookkeeping files are rewritten without
  an fsync: the drain rebuilds the status counts, and the first append of each process lifts the
  queue sequence above every op still queued, dead-lettered or reserved, so a power loss that
  rolled `.seq` back can never sort a new write (a run's close) ahead of older ones. A run's
  terminal status keeps every fsync. A process killed right after `log()` returns still loses
  nothing; a power loss can lose only the last few queued writes, never leave a torn one.
- **One run's stuck write no longer holds every other run on the machine.** The outbox was one
  machine-wide queue: the first write that failed transiently (a 503 for one run, a timeout)
  ended the drain pass, and the worker slept and started again from the same write, so every
  other run's data waited behind it, for up to the 24 h it takes that write to be dead-lettered.
  The drain now keeps one lane per run and visits them in turn, one write each, so a run with a
  deep backlog does not delay a new one either. A failure parks only its own run for the rest of
  the pass; that run backs off on its own (at least the server's `Retry-After`) while the others
  keep flowing, and a write another run queues meanwhile wakes the worker instead of waiting out
  that backoff. Each run's own writes still go in the order they were queued, its close last. A
  server that cannot be reached at all (a connect failure), or that stops answering twice in one
  pass, still stops the pass for everyone. A parked run's queued writes are not re-read every
  pass, and a run whose writes another process delivered is forgotten, so waiting costs no CPU.
- **An outbox shared by two SDK versions no longer dead-letters the newer one's writes.** A
  detached worker keeps delivering for as long as the queue has work, through an upgrade, and it
  dead-lettered any queued write of a kind it did not know. Each worker now advertises the kinds
  it delivers (`.worker-caps.json` beside its lease); a newer SDK checks before queueing a new
  kind, and if the live worker lacks it, asks it (`.worker-stop`) to exit after its current pass
  so the next write starts a worker of the newer version. A worker that meets a kind it does not
  know now holds it in its run's queue, unattempted, instead of dead-lettering it, so a rollback
  strands nothing either.

## 0.190.0

- **`finish()` never raises over delivery, and waits for at most one deadline.** The
  default close was a hard barrier: one delivery attempt (PR #1679, Mahit, gave it a 15 s
  retry), then `RosError: run … not closed`. Live, one 503 `concurrent telemetry write`
  from a colliding write failed 4 of 6 ten-step runs. Now every close drains the run's
  writes, retrying with backoff (a 503 like any transient error, at least as long as its
  `Retry-After`), until ONE deadline: `flush_timeout=`, else `PROBE_FINISH_TIMEOUT_SEC`,
  else **600 s**. Whatever is still undelivered stays queued on disk and the terminal
  status is queued BEHIND it (`probe_finish.deferred`), with a warning. Writes the server
  permanently rejected (dead letters) warn and the run closes marked
  `probe_finish.dead_lettered=N`. So a run with missing data never reads `completed`
  without saying so on the run. `finish(strict=True)` (or a `fail_open=False` client)
  keeps the old barrier: it raises and does not close. The deadline now bounds upload
  promotion, the wait for the drain lock (a busy detached worker could hold a close
  forever), every request's timeout, and the final status write (`finish(flush_timeout=10)`
  behind a backlog took 170 s), including a response that trickles in byte by byte. A
  refused credential (401/403) defers the close at once instead of waiting out the deadline,
  and a Ctrl-C during the close still queues the verdict behind the data and hands it to the
  background worker (a code-less `SystemExit`, Lightning's SIGTERM, too); one that lands
  before the drain starts, while the close hashes the run's reads or joins the hardware
  collector, queues `canceled` (a `SystemExit`: its exit code's verdict). `finish()` is
  idempotent: `with probe.init()` followed by the exit hook closes the run once, not twice
  (two deadlines, two terminal writes), and a second `probe.finish()` from another thread
  waits for the running close and returns its answer instead of closing the client under it.
  `PROBE_FINISH_RETRY_SEC` from #1679 never shipped and is folded into the one deadline;
  `flush_timeout=0` means "queue the close, wait for nothing".
- **One retry policy for every delivery loop.** The detached worker, the in-process exporter
  (which re-drained on every `log()` wake, with no backoff at all) and `finish()` take their
  waits from `durable.backoff_delays`, lengthened to the server's `Retry-After`. A write
  that keeps failing transiently is dead-lettered only after **24 h since its first failure
  AND at least 50 attempts** (`PROBE_OUTBOX_TRANSIENT_BUDGET_SEC`), not after 50 attempts
  alone, so a fast-polling loop can no longer throw data away in seconds (it did in ~13 s
  from `finish()` and ~7 s from the exporter), and a laptop that slept through a day does
  not dead-letter on its next blip. `probe outbox retry` restarts that clock. The transport's own in-request retries
  (a 503/429/502/504 on a GET or PUT) also wait at least the server's `Retry-After` instead of
  0.2 s; a `Retry-After` longer than 10 s goes straight back to the caller's loop, carried on
  the error as `retry_after`.
- **A long-lived process no longer drops every write after 500k `log()` calls.** The
  outbox's queue-length cap (`PROBE_OUTBOX_MAX_PENDING`, 500k) counted appends for the
  life of the process: the detached worker's deliveries happen in another process and
  never lowered the count, so once a process had logged 500k times every later write was
  refused while the queue sat empty (live, with the cap at 150: 150 of 450 arrived). The
  count now follows `status.json` (which each drain pass rewrites when it ends) on every
  append, and a write that would still be refused first counts the queue directory itself
  (at most once a second), so a long drain pass no longer refuses writes into a queue it has
  mostly emptied. `finish()` also reports writes this process dropped before they reached
  the outbox (full or unwritable): a warning, and `probe_finish.dropped_writes=N` on the
  run; the close itself is never refused by the cap. A producer record keeps a
  `gap_count` and its last 32 gaps instead of every one, and each drop is recorded once.
- **A drain pass no longer stalls `log()` behind a queue rescan.** Every pass parsed the whole
  outbox twice while holding the append lock (to quarantine corrupt files, then to recount
  `status.json`), so each `log()` in the training loop waited behind it: 3.3 s per pass at
  20k queued ops in the live audit. A pass now parses the queue once, outside the lock; under
  it, only corrupt files are re-checked and only ops appended during the pass are parsed.
  Measured at 20k ops: append lock held 2,075 ms per pass before, 30 ms after (the pass
  itself: 4.3 s to 1.5 s). A pass also removes `.tmp` files a killed writer left behind more
  than an hour ago. The blob sweep, which also reads the whole queue under that lock, now runs
  only after an upload left the queue, not after every pass that tried one: an upload stuck at
  the head during an outage froze `log()` for up to 0.8 s a pass at 20k queued ops.
- **A re-login no longer kills a running job's heartbeat.** `probe login` revokes the token a
  running process was built with; its heartbeat was then refused on every beat, the refusal was
  swallowed, and 15 minutes later the run was marked `crashed` while it was still training. When
  the API refuses the credential itself (a 401, or a 403 saying the key's team or membership is
  gone; never a 403 about a missing scope), the heartbeat, the in-process outbox exporter and
  `client.flush()` now re-read it and retry once. It is re-read only where it came from:
  `PROBE_TOKEN` from the environment, or this context's stored `probe login`. The new token is
  used only if `/v1/me` says it belongs to the same team and user the process started as. A login
  as anyone else, or a `PROBE_TOKEN` job on a box where someone else is logged in, keeps the old
  token and says so, once. The exporter waits for a new `probe login` instead of stopping, and
  does not hold the outbox's worker lock while it waits; a drain by another process still stops at
  the same refused write. The account is looked up once, when a heartbeat or the exporter starts;
  the config file is read at most once every 30 seconds after that, and the network is used again
  only when the token changed. Only clients that read their
  credential from the environment or `probe login` do this (`probe.init()`, a bare `Client()`); a
  token passed in code is never replaced. New: `Client.refresh_credentials()`.
- **Nested dicts are charted: `probe.log({"eval": {"acc": 0.9}})` writes `eval/acc`.** Before, a
  nested dict could not be a number, so the whole dict went into the step record and nothing was
  plotted. Any mapping now flattens to `/` keys (the separator the run page sections panels by),
  an OmegaConf `DictConfig` too, all at one step; non-numeric leaves land in the step record under
  their flat key (`eval/text`). Lists are not flattened and an empty dict writes nothing. If you
  log both `"a/b"` and `{"a": {"b": ...}}`, the explicit `"a/b"` wins, with one warning per key per
  run. Nesting deeper than 16 levels, or a dict that contains itself, is kept as one step value
  with a warning. Secrets stay redacted:
  - A nested field named like a credential (`db.pwd`, `headers.authorization`, `wandb.apikey`) is
    redacted exactly as it was when the whole dict went to the step record. Numbers are redacted
    too, except under ordinary-word names (`token`, `secret`, `cookie`, `credential`), where the
    scrubber keeps a number or plain word, as it always has.
  - A dict under a credential name (`{"token": {...}}`, a `bytes` key included) is still dropped
    whole.
  - A step-record key with a `/` is judged segment by segment, so `tok/pad_token` stays a model
    setting and `headers/authorization` is redacted even when written flat.
  - A Hydra/OmegaConf interpolation that would reveal a secret stays its unresolved `${...}` text:
    one reading the environment (`${oc.env:KEY}`), one naming a credential (`${wandb.api_key}`),
    and a chain to either, including an alias to a whole credential group (`alias:
    ${credentials}`, then `auth: ${alias}`) and a secret embedded in a string (`pw=${alias}`,
    for a secret of 4 characters or more). Other interpolations resolve. A `???` stays `???`.

  A dict keyed by data (`{"per_example_loss": {example_id: v}}`) would mint a series per id. So
  after 1,000 distinct nested keys in a run, or for a key longer than 256 characters, that
  top-level value is kept whole in the step record, with one warning. `dimensions` and `labels`
  are unchanged (still flat maps), and `log_derived` does not flatten. A call with no nested dict
  sends exactly the bytes it did before. Note: W&B's history uses `.` for nesting, so a run
  brought in with `probe import wandb` keeps `a.b` keys, which are a different series from `a/b`.
- **`probe.log(..., commit=False)`, as in W&B.** `commit=False` holds the values in a pending row
  instead of sending them; the next `log()` at the same step (or with `step` omitted) adds its
  values and sends the row once (the last value per key wins). A `log()` at a different step sends
  the held row first, and `probe.finish()`, the end of a `with` block and the exit hook send any row
  still held. A crash before that loses it, as in W&B. A preemption handler (SLURM, submitit) that
  interrupts a `commit=False` call and calls `log()` or `finish()` never hangs: its own values are
  sent at once, and `finish()` warns that the interrupted row was not sent. Nothing changes for
  calls that do not pass `commit=False`. One difference from W&B: a bare `log()` after
  `log(step=5)` lands at step 6 (W&B: 5).
- **Two `log()` calls with text at the same step no longer wipe each other.** The server replaces a
  step's record on every write, so `log({"phase": "eval"}, step=3)` then `log({"note": "x"},
  step=3)` kept only `note`. The SDK now sends both keys (the newer value wins per key), for the
  last 64 steps a run wrote.
- **Configs arrive as configs, and can change after `probe.init()`.** `config=` now takes an
  `argparse.Namespace` (jsonargparse's, as LightningCLI hands out, and `SimpleNamespace` too), a
  dataclass, a Hydra/OmegaConf `DictConfig`, a pydantic model or a dict holding numpy values, and
  stores the dict you meant. A field marked not to show (`dataclasses.field(repr=False)`, pydantic
  `Field(repr=False)`) is left out. Interpolations resolve, except one that would reveal a secret:
  one reading the environment (`${oc.env:KEY}`), one naming a credential (`${wandb.api_key}`) or a
  chain to either, an alias to a whole credential group included, stays its `${...}` text. That is a deliberate difference from W&B, which resolves
  everything. Before, each became one
  `repr()` string or a 422. NaN and infinity become `"NaN"` / `"Infinity"`, small arrays become lists,
  and a top level that is not a mapping is refused before any request. New
  `run.update_config({...})` / `probe.update_config({...})`, and writes to `run.config` (`run.config["lr"]
  = 3e-4`, `.update()`, `run.config.lr = ...`), merge into the stored config: shallow, new wins, as in
  W&B. Changing an existing value warns once per key (`allow_val_change=True` silences it,
  `allow_val_change=False` refuses); a value under a credential name is compared and shown only as
  it is sent, redacted. Copies of `run.config` are plain dicts, and `run.config |= {...}` sends. It needs a server that serves `run_config_merge`; an older one
  would silently drop the field, so the SDK does not send it there and warns once instead. Under
  `probe exec`, `probe.init(config=...)` now records the config on the joined run from global rank 0,
  where it used to be dropped with an "ignored" warning. `log()` reads numpy values and 1-element
  tensors through `.item()`: no numpy deprecation warning to raise under `-W error`, and a
  multi-element tensor goes to the step record instead of raising. A numpy scalar anywhere in a
  request is sent as a number, not its `repr()`.

## 0.189.0

- **A long outage no longer throws away the data queued during it.** When the server's reaper marked
  a silent run `crashed`, the heartbeat thread reopened it on reconnect with a new write epoch, and
  every point already in the outbox (stamped with the old epoch) was refused and dead-lettered;
  `probe outbox retry` could not bring it back, and other ranks of the same job stopped delivering
  for the rest of the run. Recovery now keeps the epoch (`keep_epoch`); against a server too old to
  do that it leaves the run `crashed` instead of reopening it, and the data still lands. Status
  writes (`finish()`, `set_status`, a queued `run end`) now carry the handle's epoch so a superseded
  attempt cannot close the newer one. `probe --async log` and `probe run end --async` no longer
  stamp epoch 1 on runs that were reopened, which dead-lettered them.
- **Corporate CA bundles work: `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` are honoured.** Behind a
  proxy that re-signs TLS, SDK writes failed certificate checks while telemetry got through,
  because the two used different trust: httpx read only `SSL_CERT_FILE`/`SSL_CERT_DIR` else
  certifi, urllib only the OS store. Every connection the SDK, CLI, daemon and MCP open now uses
  one context. `SSL_CERT_FILE`/`SSL_CERT_DIR` replace the defaults, as OpenSSL and httpx read
  them (setting only one keeps OpenSSL's built-in location for the other). Otherwise
  `REQUESTS_CA_BUNDLE` (else `CURL_CA_BUNDLE`) is added ON TOP of the OS store plus certifi,
  never instead of them, so a bundle exported for another tool can never stop Probe reaching
  its server. A path that is missing or holds no certificate prints one warning and is skipped;
  nothing raises, and there is no switch that turns verification off. Certificates are read once
  per process: restart after fixing a bundle. Without any of these set, trust only grows: httpx
  gains the OS store, urllib gains certifi. The plugin hooks and the tap keep urllib's default
  trust for now; their shared helpers only gained an optional `context` argument, which the CLI
  fills.
- **Hardware metrics no longer collide with the run's own writes (async writes, the default).** The hardware rail (on by
  default) wrote around the outbox: its metric batches and an env_ref `PATCH` of the run went
  straight to the API while the outbox was delivering the run's own metrics, and the server
  answers a metric write that meets another writer on the run with a 503. In the live audit
  `finish()` raised on 4 of 6 ten-step runs with hardware on, 0 of 6 with `PROBE_HW=0`. Under the
  default async writes, hardware batches and that `PATCH` now queue in the outbox like every other
  write to the run, carry the run's writer epoch, and never hold a run's close; a full outbox or
  the low-disk floor drops them quietly, without counting them as lost data. With
  `PROBE_ASYNC=0` they are still sent directly, so hardware charts stay live. `finish()` still
  raises when any queued write meets a transient error; that retry is a separate change. The
  hardware record also stops replacing the one a code snapshot pinned: it is minted only for runs
  with no snapshot, as it always meant to be (new read-only `run.env_ref`, the hash this handle
  pinned). An abandoned run's collector now stops with it, and `finish()` waits at most 0.5 s for
  the hardware inventory. `PROBE_HW=0` / `run(hw=False)` are unchanged.
- **`log_artifact` no longer raises into a training loop when the upload is refused.** A file
  over the 64 MiB inspection limit (say a 70 MB checkpoint), a credential in the file's path, or a
  source that changed while it was being read used to raise `CredentialBlocked` out of
  `log_artifact`. Now, unless you pass `strict=True` (or use a fail-closed client), the run records
  a reference to the file instead -- its path, size, `meta.upload = "failed"` and the reason in
  `meta.upload_error` (the path itself is withheld when it is the thing that looks like a
  credential) -- warns once per reason, and carries on. `strict=True` still raises.
  `CredentialBlocked` is now a `RosError`, so `except RosError` catches it; `except
  CredentialBlocked` keeps working. A mistake in the call still raises, as before: a missing path
  (`FileNotFoundError`, unless `allow_missing=True`, which records the pointer), a directory
  (`IsADirectoryError`), or a `content_hash`/`size_bytes` that does not match the file
  (`ValueError`). A failed upload that falls back to a reference now shares the same
  once-per-reason warning.
- **`probe artifact add` exits 1 when the file was not uploaded.** It used to print `(delivered)`
  and exit 0 when the upload was refused (a 70 MB file) or failed and fell back to a pointer. Now
  it prints `error: not uploaded (<reason>); a pointer was recorded` to stderr and exits 1; a
  missing path is an error and records nothing.
- **Code capture names a local refusal as one.** A code file the credential gate refuses is
  listed as `refused by the credential gate: <reason>` instead of `upload rejected`, and no longer
  counts toward a "storage rejected every upload" outage. A refused code-bytes archive warns once,
  with the gate's reason.
- **A run's status now follows how the process actually exited.** `probe.init()` also wraps
  `sys.exit` (chaining whatever was installed before, the way W&B does): a non-zero code closes the
  run `failed` -- `sys.exit(2)` used to close it `completed` -- `sys.exit()`/`0`/`False` close it
  `completed`, and an exit made while handling Ctrl-C closes it `canceled`. That covers Hydra, which
  calls `sys.exit(1)` inside its own `except` (the run is now `failed`, and the exception it
  swallowed is filed as the crash report) and Lightning's Ctrl-C, which does the same inside
  `except KeyboardInterrupt` (now `canceled`). A non-zero exit also writes a `process` span named
  `exit` carrying `exit_code`, which the crash email reads when there is no traceback. Only the
  main thread's exit counts, never a notebook's, and never a forked child's: a child that inherited
  the hooks can no longer close its parent's run. `fluent.note_exit(status, exc=None)` is the hook
  the Lightning/HF integrations will call for endings no process hook sees (Lightning turns
  SIGTERM into a code-less `SystemExit` that exits 0). The code stored is the one the OS reports
  (`sys.exit(-1)` is 255, `sys.exit(256)` is 0 and completes), so it never reads as a signal. A run
  the script already closed -- `run.finish(...)`, or a `with probe.init()` block that ended -- is
  not closed again at exit, and the `with` block reads its body's ending the same way: `sys.exit(0)`
  completes, Ctrl-C cancels, no crash report is filed for a `SystemExit`, and a `note_exit` made
  inside the block outranks a code-less `SystemExit` (Lightning's SIGTERM). Under `probe exec`, a
  job whose exit no hook saw (`sys.exit(main())` with `probe.init()` inside `main()`, which looks up
  `sys.exit` before `probe.init()` runs) now leaves the close to the launcher, which has the real
  exit code (`PROBE_EXEC_FINALIZES` names the launcher's run, so a run a sweep driver opens and
  hands to its own job is still closed by that job); without a launcher that shape still closes
  `completed`, as it does in W&B. So do `raise SystemExit(2)` and a `from sys import exit` taken
  before `probe.init()`. A close that fails at exit is no longer silent: one line, `probe: run <id>
  was not closed at exit (...); it will be reaped as crashed`, goes to stderr through the
  non-raising warning channel.
- **Interim, until per-rank liveness: only rank 0 closes a shared run.** A process that joined a
  run through `PROBE_RUN_ID` with a `RANK`, `SLURM_PROCID` or `OMPI_COMM_WORLD_RANK` above 0 (and a
  matching `WORLD_SIZE`, `SLURM_NTASKS` or `OMPI_COMM_WORLD_SIZE` above 1, when set) still
  delivers everything it logged and its crash evidence, but sends no terminal status -- the first
  rank to exit used to close the run for every rank, and a later rank's `completed` overwrote an
  earlier one's `failed`.
- **Ctrl-C on `probe exec` closes the run `canceled`.** The launcher takes the interrupt too, and it
  used to re-raise without closing the run, which then sat `running` until the reaper called it
  `crashed` and mailed the person who pressed Ctrl-C. A job that already closed itself keeps its
  own verdict. Salvaged from #1710; SIGTERM and SIGKILL stay failures. `probe exec` also exits
  128+N when its command dies of signal N (143 for SIGTERM), the way a shell reports it, instead of
  the 241 it passed on for SIGTERM.
- **Code capture no longer writes into your `.git`, and works without a git identity.** The
  run-open snapshot used to commit the whole working tree, untracked files included, into
  `refs/probe/snapshots/<run>`. That needed a git author identity (a bare pod has none, and then
  nothing was captured at all: no code, deps or argv), grew one repo's `.git` from 20 to 306 MB,
  and `git push --mirror` published those refs, an untracked `.env` among them. Git is now read,
  never written: HEAD, branch and dirty under `GIT_OPTIONAL_LOCKS=0`, so no object, ref or index
  write. No identity, an unborn HEAD or a read-only `.git` capture normally; a repo git cannot
  read or list (no git binary, "dubious ownership", a corrupt index) is captured as a plain
  directory with the reason in the code-snapshot's `meta.git_error` (a corrupt index still records
  HEAD and branch; dirty is unknown). The code-snapshot row points at what was stored
  (`probe-manifest:`/`probe-artifact:`) instead of a shadow ref, and records `meta.head`. Old refs
  are never deleted automatically. They are reported on stderr once per repo (again if more
  appear, and seen even with warnings ignored) and by `probe doctor`. `probe snapshot-prune-refs`
  removes them (loose and packed), printing `<ref> <sha>` for each so a delete can be undone. By
  default it deletes only refs whose run's code Probe says it stored in full, and keeps the rest
  with the reason; `--bundle FILE` backs every ref up first, `--force` deletes them as they are,
  and `--dry-run` changes nothing.
- **Code capture no longer uploads files that hold credentials (security, #2000).** The run-open
  code snapshot streamed every captured file's bytes without a content scan, and inside a git repo
  it did not even apply the credential-shaped NAME filter the non-git walk uses: an untracked,
  non-ignored `.env` or a scratch file with a `probe_pat_`/HF/AWS/GitHub token was stored
  byte-identical. Now:
  - Untracked and walked files pass the full name filter; TRACKED files only the exact-name,
    prefix and suffix rules (`tap_core/secrets.py` and a committed `.so` are code), and any
    tracked-file skip is announced. Files under a credential folder (`.secrets`, `secrets`, `.aws`,
    `.ssh`, `.gnupg`, `.docker`) are skipped on every path. Credential names include `*.env`,
    `.env-*`, `.envrc`, `netrc`/`_netrc`/`.netrc`, `.pgpass`/`pgpass.conf`, `.dockercfg` and
    `.git-credentials`; templates (`*.example`, `*.sample`, `*.template`) are not, and their content
    is scanned like any file's. `include=` records a symlink as a link and never follows one out of
    the project.
  - Every file that could be uploaded is content-scanned before anything is presigned: the shared
    scanner's full rules with its decoding layer (base64, `%xx`, `\u` escapes) up to 2 MiB, the
    quick check streamed in 1 MiB chunks above that. UTF-16 is read as UTF-16 with or without a
    byte-order mark, at any size. Compressed archives (`.tar.gz`/`.bz2`/`.xz`, `.gz`, a zip's
    compressed members) are opened one level deep: member names against the credential-name rules,
    member bytes through the same scan; one that cannot be read is withheld as `uninspectable`.
  - A file with a finding is WITHHELD, never redacted (snapshots are exact): it stays in the
    manifest as `source: "withheld"` with its size (and its sha256 only from 1 MiB up), is never
    uploaded, and is listed under `skipped` with reason `secret`, `found_in: content` and the rule
    names (never a value); one warning names the files. `probe snapshot-show` and restore report
    it as withheld.
  - A key-name hit counts unless its value is plainly not one: a template slot (`{user}`,
    `$2::uuid`, `${VAR}`, `<TOKEN>`), a comparison, a value on the next line, a separator that
    belongs to another key on the line (`add_argument("--token", help=...)`), or -- in source
    files -- a value whose FIRST word is code (`tokens[0]`, `getpass.getpass()`, a type, a name used
    elsewhere in the file). So `# password: Tr0ub4dor&3xQ (rotate monthly)`, `# api_key: <hex>.`
    and `password: str = "..."` are withheld; `token = tokens[0]` is not. A vendor-shaped token
    inside a dropped hit always counts.
  - Every file up to 64 MiB is scanned (larger ones are recorded where they live, as before). The
    scan has a time budget: a file whose scan runs past 3 s -- or a large non-source file reached
    after the snapshot's 15 s total is spent -- is withheld with reason `scan_budget` (never
    uploaded unscanned) and one warning says how many. `PROBE_SNAPSHOT_FILE_SCAN_BUDGET_SEC` and
    `PROBE_SNAPSHOT_SCAN_BUDGET_SEC` change them (`0` = no limit). The scan runs whether or not the
    snapshot uploads (so the tree's identity does not depend on it) and, when uploading, stops at
    the upload cap (`max_upload_bytes`). It adds ~6.5 s to `probe.init()` on a 3,600-file / 64 MB
    tree.
  - Not yet caught (scanner rules; a follow-up in `tap_core/secrets.py`): camelCase key names
    (`openaiApiKey`), `f"..."`-prefixed literals and `os.environ.setdefault(...)` defaults, `*_KEY`
    names (`AZURE_OPENAI_KEY`), netrc/Maven password lines, URL passwords over 64 characters, and
    base64/escaped tokens in files over 2 MiB.
- **Resuming from an older checkpoint no longer crashes the relaunch.** When a resumed run (on
  `on_conflict="auto"`/`"resume"`, or a job that rejoins its run through `PROBE_RUN_ID` after the
  reaper ended it) logs a step at or below the step the first attempt reached, `log()` raised a
  `ValidationError` at the first training step. Those calls are now dropped, the way W&B does it:
  nothing is sent, one warning names the resume point, the first dropped step and the two ways out
  (`on_conflict=probe.Rewind(step=...)` to overwrite that range, `client.fork_run(...)` to keep
  both curves), and one line says where logging resumed and how many calls were dropped. The
  counts are kept on the run in a `resume_guard` diagnostic span. `run.step()` records are guarded
  the same way, and `strict=True` still raises. A job rejoining through `PROBE_RUN_ID` now arms
  the guard too (the reopen's resume point used to be thrown away there).

- **A metric point is dated when your loop logged it, not when the server received it.**
  `run.log()` / `probe.log()` now send the call time as each point's `wall_clock` (all keys in one
  call share it). Before, the server dated a point on arrival, so points the outbox held through an
  outage landed up to minutes late (131 s after a 90 s outage, measured) and findings saw a gap
  followed by a burst. An explicit `wall_clock=` still wins; `log_derived` / `log_derived_series`
  are unchanged. Side effect: findings now bin by the machine's own clock, so the SDK warns once per
  process when that clock is more than 60 s off the server's (read from the HTTP `Date` header).
  Stamps strictly increase within a process, so a frozen clock (freezegun) or one stepped backwards
  cannot merge two `step=None` points into one. The stamps skip the credential scrubber (a value
  that is exactly an ISO-8601 timestamp cannot hold a secret), so `log()` costs what it did before.
- **An unsupported Probe install says so, once.** When the server answers that this
  `probe-research` is older than the oldest release it supports, the SDK and CLI print one line on
  stderr per process, naming your version, the minimum, and how to upgrade: `probe update` from
  the CLI, `pip install -U probe-research` from a script that imports `probe`.
  Nothing else changes: requests still go through. A route the server has retired now raises
  `ClientTooOldError` (with `.min_version`) instead of a bare `RosError`: `probe.init()` raises it
  as-is, and a queued write that hits one is dead-lettered instead of retried. The trash's 410 is
  unaffected.
- **`client.run_metrics()` and `client.export_metric_points()` give back NaN and infinities.**
  A logged `float("nan")`, `inf` or `-inf` used to read back as `None`, the same for all three.
  Against a server that names them (a `nonfinite` field on each raw point), these reads, and
  `Reader.metrics()`, now return the float the run logged, as `wandb.Api()` history does. The
  JSON the CLI prints (`probe run metrics`, `probe metrics export`) and the MCP `metrics` tool
  stay strict JSON: `"value": null` plus `"nonfinite": "nan" | "inf" | "-inf"`. An older server
  still reads `None`.
- **The daemon keeps one conversation per session by default (`PROBE_DAEMON_MODE=conversation`).**
  Each new stretch of the transcript is added as the next message, and nothing is re-sent at the end
  of every request, so the prompt cache holds: the companion bench measured 96-98% of input read
  from the cache on Claude and 30-50% lower cost than fresh bites, at the same checks. The state of
  the record is `session(op=status)`, on demand. `PROBE_DAEMON_MODE=bites` keeps the old mode.
- **The daemon's tools say what to fix instead of asking the researcher.** A `session(op=search)`
  with a `kind` that is not one is refused with the list (a guessed kind used to answer "no event
  matches"); a note written in any shape but `cat > FILE <<'TAG'` is told that shape; a `git log`,
  `diff` or `show` missing `--no-textconv --no-ext-diff` answers "not run" with the flags to add,
  instead of a question on the researcher's board. A line from the coding agent's harness shows as a
  300-character preview. The coding agent's memory index comes back after a compaction. Skills
  installed under the user base (`pip install --user`) are found, and missing ones are logged.
- **The daemon records by the researcher's own skills, with the prompt text he approved.** Its
  instructions now carry `track-work` and `edit-notes` whole, after a short job description that
  keeps only the daemon's role and how its harness works; `track-work/reference.md` and the other
  skill files are a `read` away. Every other line the daemon's model reads -- the tool
  descriptions, the labels around the chat in a bite, the status answer, the compaction prompt, the
  loop detector's stop, the tools' replies, the check and shell reasons, the reader's replies and
  the notices -- is the researcher's reviewed wording: shorter, and no advice on what to record.
  Also: a notice shows whole (it was cut at 300 characters) and a line from the coding agent's
  harness is labelled apart from the daemon's own; a refused read counts toward the loop detector;
  a yes that could not run says only why.
- `probe doctor`, `probe companion authorize` and the setup text say the daemon's key can delete
  only into the trash (since 0261; control/049 gave older keys the same), not "no delete".
- **The daemon has three tools of its own: `shell`, `read` and `session`.** `session(op=...)` replaces
  `session_open`, `session_search` and `record_status` (`op`: open, outline, search, logbook, status),
  and refuses an argument that belongs to another op instead of ignoring it. `read` also opens the
  Probe skills folder, read-only. Gone: the skills tools (`load_capability`, `read_skill_resource`)
  and `read_tool_result`: each tool caps its own output, and the session outline now comes in pages.
- **The daemon keeps no memory of its own.** Its per-project MEMORY.md and the memory tools
  (`write_memory`, `read_memory`, `search_memory`, `delete_memory`) are gone, and its whole state
  folder stays closed to its shell. It still reads the coding agent's own memory index, read-only.
- **The daemon no longer asks a judge model which of the agent's paragraphs look unrecorded.** The
  per-turn judge (`POST /v1/companion/judge`) and its `PROBE_DAEMON_JUDGE` switch are gone; notes
  are optional.

## 0.188.0

- **`probe.context()` names the batch a crash happened in.** Wrap each batch or sample in
  `with probe.context(batch_id=i, epoch=epoch):` (also `run.context(...)`; keys `sample_id`,
  `task_id`, `prompt_id`, `step`, `split`, `phase`) and a crash inside it reaches the crash email
  as `Batch: 17` / `Epoch: 3`. Unlike `run.unit(labels=...)`, it stamps nothing onto what is
  logged inside, so every curve plots exactly as before. It sends nothing, never raises, and is a
  no-op with no active run. `epoch` is a new crash-context key. The `instrument-code` skill now
  tells agents to wrap training loops this way.

- **`probe.expect()` and `probe run expect`: get an email when a metric leaves a range you set.**
  `probe.expect({"val/acc": (0.5, 1.0), "train/loss": (None, 20)})` after `probe.init(...)` declares
  where metrics should stay; the run's creator is emailed the first time a value crosses. For a run
  already going, `probe run expect <run> val/acc --min 0.5 --max 1` (or `--clear`). Optional: a
  script that never calls it is unchanged, and a call never breaks the script -- a malformed entry
  is dropped with a warning and delivery is fail-open like `probe.log`.

## 0.187.0

- **Tap 0.9.1: a daemon that stops because the switch moved is started again once it reads `daemon`.**
  The worker now exits 5 ("respawn later") when it leaves over the switch; the tap's supervisor
  starts a new one as soon as the switch reads `daemon` instead of recording a give-up (the
  0.186.x race where the switch flipped back while the worker was leaving). An older tap treats
  exit 5 as a crash and backs off, which is harmless.
- **What no mode runs is found in every part of a command.** The shell's checks stopped at the first
  part they would ask about, and bypass mode runs what is asked, so `env FOO=1 true; cat
  ~/.config/probe/config.json` read Probe's key, and `PROBE_CONFIG_PATH=... probe ...`, `env -i probe
  ...`, `timeout 60 probe ...` or `python3 -c "subprocess.run(['probe', ...], env={})"` reached Probe
  with a key from the daemon's key-less shell (0.186.1's rule held only for a command that started
  with `probe`). Every part, and the raw text of a command too complex to read, is now checked first
  for Probe's key or state, a Probe setting set or unset, `env -i`, and a probe started through
  another program; each is refused in every mode. A `probe` run from the daemon's shell says to run
  it as a command of its own instead of "run `probe login`".
- **Chained commands keep to the working folders.** `cd latest && cd ../..` climbed out through a
  symlink (the check read `..` from the link's target); a `cd` now lands only in a working folder
  outside bypass mode. A heredoc in a chained command with probe (its lines ran as commands) and a
  `cd` that is not a bare `cd DIR` are refused in every mode, and a probe write whose filter would
  not run is not run either (it landed while the model was told "not run").
- **More ways to print a piece of a key are asked about:** `cut -d` on a delimiter a key can hold,
  `tail +5c`, jq `reverse`, `diff -y`/`-W`, and `rg --max-columns-preview`.
- **The reader never shows a piece of a key, and reads a bounded amount.** A cut (a long cell, line
  or file) drops the word it lands in, so no key fragment escapes the scrubber; a NumPy header's
  shape must be sizes and a preview skips at most 1 MiB; a FIFO or device is not opened; a malformed
  file is a "not read", never a tool error; Parquet needs pyarrow 14.0.1+ (CVE-2023-47248).
- **A write sent again after a different one gets its own key.** `run set --status running`,
  `finished`, `running` reused the first key for the third and was answered with its stored
  receipt. A blocked command no longer reads the files it names to make a key, and a read keeps no
  count. The edge-exists rule's claim is corrected: a queued edge's reason/meta is not applied to
  the edge that was there (logged).
- `python -m probe.cli` runs the CLI (the daemon's fallback when no `probe` sits beside it).
- **The daemon can push the same note twice in one bite.** Each `probe` command the daemon runs
  sends an Idempotency-Key derived from its words, so a second `probe notes push` of one note with
  new text reused the first push's key with a different body, and Probe answered 422. The key now
  also covers every file the command sends (the checked-out note a push uploads, an `@file` value, a
  file argument, a manifest and its rows), and how often that content changed in the bite: new text
  is a new key, the same text again keeps its key (a push that landed replays), and text A, then B,
  then A again does not replay A's first receipt.
- **A queued lineage edge that already exists counts as delivered.** The outbox dead-lettered a
  background `probe edge add` whose edge Probe already had (409 "lineage edge already exists"),
  because a 409 on a first attempt is treated as a real conflict. For that answer the conflict is
  the whole edge (same source, relation and target), so the write is done and nothing is lost; the
  other 409 on that route ("this run already has a genealogy parent") still dead-letters.
- **The daemon asks before it prints a file in pieces.** Outside bypass mode, `cut -c`/`-b`,
  `head -c`/`tail -c`, `grep -o`, `rg -o`/`-r`, and jq string slicing (`.[m:n]`, `split`, `sub`,
  `ltrimstr`, `match`, ...) are a question instead of running at once: a key printed a few
  characters at a time, or without its prefix, is text the output scrubber cannot recognise. Bypass
  mode still runs them.
- **The daemon runs several commands per shell call.** A command such as `cd eval && probe artifact
  add results.csv --project p && probe run tag r1 done`, or `probe run list --json | jq ...`, is no
  longer refused: the daemon runs its parts in order itself, never through bash. Each `probe` part
  gets every check a lone probe command gets (owner, secret scan, questions, lease, logbook,
  Idempotency-Key) in the folder the `cd` parts moved to; `&&`, `||` and `;` behave as in bash (a
  part that was blocked, held or refused counts as failed); a probe command's stdout feeds the
  filter after its `|`. Every other part is checked and run, asked about, or refused as a command
  of its own. A probe command reading piped input or redirected to a file is still refused in
  every mode, and the shell's environment still has no Probe config. The lease is renewed between
  the parts, so several slow probe commands in one call do not outlive it.
- **A file reader for the daemon** (`tools.read_file`, for its `read` tool). It reads a file's lines
  by range, or previews a CSV/TSV (header, first rows, shape), a JSON document (what it holds,
  pretty-printed), a Parquet file (schema, rows, first rows) or a NumPy `.npy`/`.npz` (dtype, shape,
  first values, read from the format's header without numpy). It never loads a pickle or a format
  built on one (`.pkl`, `.joblib`, `.pt`, an object array: loading runs code) and shows no images.
  It reads what a safe `cat` may (bypass mode: anything but Probe's own key and state), scrubbed
  and capped. No dependency was added: Parquet needs pyarrow, and without it the reader says so.
- **The daemon can read the session again.** `session_search` and `session_open` were plain
  functions, so Pydantic AI ran them in a worker thread, and the session's store (one SQLite
  connection) refuses any thread but its own: on 0.186.0 and 0.186.1 every call answered
  `tool error (ProgrammingError)`, and the daemon could open no output behind a tag. Both now run
  on the event loop, and a test calls them through the real agent.
- **One long conversation per session, behind `PROBE_DAEMON_MODE=conversation`** (the default
  stays `bites` until the bench picks). The daemon keeps one message history per session, like
  Claude Code: each bite appends a message with only the new events and what is not recorded yet,
  the history is saved to the session's store after every run and resumed when the worker is
  restarted, and near `PROBE_DAEMON_CONTEXT_TOKENS` (120K) it compacts -- old tool results cleared
  first, then older turns summarized. The job description, MEMORY.md and plain code's state of the
  record (what the logbook says was filed where, what waits for the researcher, what failed) ride
  every request, so no compaction loses them. It starts fresh from disk, as a bite does, when the
  saved history cannot be read, after 8 compactions, or when the loop detector stopped a run. In
  this mode there are no per-bite caps (40 requests, 80 tool calls, 1.5M tokens); bites mode keeps
  them.
- **A run no longer starves the daemon's loop.** Between two model rounds the worker reads new
  transcript lines into the queue, settles the researcher's answers, and stops the run for a moved
  switch, a lost lease, the device fuse, or the session-end deadline (a run already going when the
  session ended included). A loop detector stops a run that gets the same refusal or failure 3
  times, or makes the same successful call (tool and arguments) 5 times: its events are retried
  over half as many, then skipped with a notice, as a bite its limits cut short.
- **MEMORY.md replaces the running note.** One notebook per project (the git repository of the
  session's folder, else the folder; never `$HOME` or above), kept in
  `<state>/probe/daemon/memory/<sha256 of the path>/MEMORY.md` and shared by every session and
  every restart on that project. The daemon writes it with `write_memory` (scrubbed for secrets
  before it lands, one writer at a time per project) and sees it at the end of every request,
  with the researcher's own agent memory index beside it; the files that index names stay one
  `cat` away. The daemon's job description now says what to keep there and when to update it.
- **The daemon's tools, like a coding agent's.** Independent tool calls run in parallel; a tool
  that writes (`shell`) runs alone, in the order the model called it. The Probe MCP tools are there
  from the start (no `load_capability` round). A `read` tool is offered when the CLI has the file
  reader. Oversized tool outputs are spilled to a file with a preview and a `read_tool_result`
  handle when they are made; a plan tool helps a long job; malformed tool arguments are repaired;
  a collapsed prompt cache is logged.
- **Every model round is counted as it happens.** Each response lands in the session's store
  (model, input, cached and output tokens, the tools it called, estimated dollars) and on the
  device's daily token counter at once, not after the bite. `probe daemon status` shows each
  session's tokens, its cache-hit share and an estimate in dollars (gemini-3.8-flash and
  claude-opus-5-5 at the gateway's prices; any other model in tokens only). There is no
  per-session spend limit: the team fuse and the device fuse stay the backstop.
- **File notes at a careful researcher's level.** The daemon no longer gives every file the runs
  produced a note: it describes the files a teammate needs to understand or reuse a result. It is
  told to batch independent reads in one turn and to chain commands in one shell call.
- **A switch file unreadable for a moment no longer strands a session with the agent.** A worker
  that leaves over the switch (unreadable for a minute, or not `daemon` while it waited for the
  session lock) exits 5, "respawn later"; the tap starts one again as soon as the switch reads
  `daemon`. Before, it exited "do not respawn", and when the switch already read `daemon` again by
  the time the tap looked, the tap recorded giving up in `daemon` and never started one.

## 0.186.1

- **The daemon writes to Probe only through its own guarded path, even in bypass mode.** A `probe`
  command inside a longer shell command (`cd x && probe ...`, `probe ... | jq`) is refused outright
  instead of being a question bypass mode approves, and the daemon's shell has no Probe config, so a
  `probe` it starts has no key. Reads or writes of Probe's own key and state files are refused
  outright too. Found by the replay bench on 0.186.0, where a bypass-mode session's daemon ran real
  `probe` writes around its pre-check. The daemon's log no longer starts with Pydantic AI's banner.

- **The transcript tap runs daemon v2** (tap 0.9.0). In the `daemon` state it starts `probe daemon
  worker` (CLI 0.186.0 or newer, with `probe daemon install`) instead of its own v1 worker, relays
  the SDK's run messages to it over a private socket, and backs off a worker that keeps crashing.
  With an older CLI the daemon does not start and the agent records, as when the daemon is off.

## 0.186.0

- **A write into the trash says so.** Against a server with the trash (0261), a run, group,
  experiment or project someone deleted answers 410 `in_trash` with a notice; the SDK's error now
  carries that sentence ("in the trash since ..., Probe can restore it until ...; contact
  support") instead of the bare token, so a training script logging to a deleted run fails with
  something a person can act on.
- **`probe run|experiment|project delete` say where the thing goes.** Against a server with the
  trash (it declares `trash` in `/v1/server/features`) the prompt reads "move ... to the trash? ...
  Probe support can restore it for 21 days" and the answer prints the date it can be restored
  until; against an older server, where the delete is permanent, the prompt still says so.
  `Client.delete_run`, `delete_experiment` and `delete_project` return the server's receipt. A delete the transport RETRIED (the first
  attempt landed, its reply was lost) that finds the thing already in the trash now returns the
  notice instead of raising.
- **The Probe daemon, version 2.** In the `daemon` state the coding agent only instruments its
  runs; the daemon records everything else. It is now an AI agent (Pydantic AI) that reads the
  session's chat log into a queue as lines land and takes a bite at your prompt, the agent's turn
  end, a size limit, or anything ~2 minutes old. Each bite is rebuilt from disk: its job
  description (`record-session`), a note of what is cut off, the recent chat (~50K tokens, whole
  turns, outputs behind tags), what is not recorded yet with its last writes, and its own running
  note. It records with the same `probe` CLI you use, each command parsed with the CLI's own
  parser and checked first: is it the daemon's to run (every CLI command now has one owner), does
  anything it sends look like a credential, does it need your yes. It reads files with a real
  shell: known-safe commands run at once; anything else is a question for you. Writes carry an
  `Idempotency-Key`, so a network retry writes once. Install its libraries with
  `probe daemon install` (the new `daemon` extra); without them capture keeps running and the
  agent records. See `probe daemon status`.
- **Questions from the daemon.** A delete that reaches another researcher's data, a shell
  command off the safe list, or a blocked check the daemon thinks is a false alarm is held; your
  coding agent asks you the daemon's exact question with its question tool (a hook refuses a
  reworded one and reads your pick). With the harness's approvals off (bypass mode) nothing is
  asked. No question tool: answer in your own terminal with `probe approvals`; `probe deny <id>`
  says no. A yes runs exactly the action you were shown (a command with a credential in it is
  refused, never asked with parts hidden), questions stay in the session that raised them, and a
  no is not asked again. Codex counts as bypass only with approvals `never` AND full access; a
  sandboxed Codex session is asked. Edits to the team note, and the sync that shares them, are a
  question too.
- **The daemon deletes only where Probe has a trash.** It checks the server first; on a server
  without the trash (restorable deletes) every delete stays yours. It stops the moment the switch
  leaves `daemon`, gives the agent its writes back when its key is refused or its budget is spent,
  and one session never runs two daemons. `probe run move` now fails loudly on a server that
  cannot file runs, instead of printing the unchanged run.
- **Runs can start without a project and be filed later.** `probe run move <run> --to
  <project|experiment> [--group G]` files or refiles a run; `probe run list --unfiled` lists runs
  not filed yet. `probe artifact set <id> --notes "..."` gives a file with no notes its one line.
- **Runs can start with no project (floating runs, daemon v2).** `probe.init()` and
  `Client.run()` with neither `experiment=` nor `project=`, and `probe run start` / `probe exec`
  with no `--project`, `--experiment` or active project, now open the run with no home through
  `POST /v1/runs` instead of refusing; the Probe daemon (or `probe run move`) files it later, and
  everything the run recorded moves with it. An active project (`probe project use`) still wins,
  and a call that names an experiment or project sends exactly what it sent before. A floating
  run's `child()`, `probe run child` and `fork_run` open floating runs too, where they used to
  raise. A backend without floating runs answers with a `CapabilityUnavailable` naming
  `--project` / `--experiment`. `probe exec -- python train.py` now wraps `python train.py`; click
  used to bind `python` to the RUN argument, so without a creation flag it was read as a run name.
- **New `intent` on a run: what it is meant to show.** `probe.init(intent=...)`,
  `Client.run(intent=...)`, and `--intent` on `probe run start` and `probe exec`. It is stored in
  the run's `metadata.intent` (the run schema has no field for it) and is what the daemon files and
  describes the run by.
- **The SDK tells the Probe daemon when a run opens and ends.** One JSON datagram on the session's
  local socket (`<state>/probe/sessions/<session>.sock`, or `/tmp/probe-<uid>/<hash>.sock` when
  that path is too long, used only when that folder is this user's and private): `run started`
  with the run id, session, name, description, tags, config (capped at 4 KB), intent, parent and
  relation, and the command for `probe exec`; `run ended` with the status. Fire-and-forget: under
  200 ms, never raises, and with no daemon listening nothing changes.
- **Remote jobs keep the session.** `probe exec` hands the job `PROBE_AGENT_SESSION=<agent>:<id>`
  next to `PROBE_RUN_ID`, and the hand-off notice for `sbatch` / Modal / Ray / kubectl lists it
  among the variables to forward. The SDK reads it only when no coding agent is detected on the
  machine, so a remote job's runs carry the session tag the daemon finds them by.
- **Unfiled runs are visible.** `probe doctor` and `probe session status` report how many runs
  wait to be filed and how old the oldest is ("unknown" on a backend that predates floating runs,
  never a wrong count); `probe doctor` also shows whether the daemon's AI libraries are installed
  and the last lines of the daemon's error log (`<state>/probe/daemon-errors.log`). `probe run list
  --unfiled` refuses a backend that ignored the filter. The MCP `browse` tool lists your unfiled
  runs at the lab root (`unfiled`: id, name, created time, tags), or marks `unfiled_runs` missing
  when the backend cannot say.
- **In the `daemon` state the agent no longer creates projects, experiments or groups.** They are
  the daemon's now: `probe project create`, `probe experiment create` and `probe group create`
  are refused like other daemon writes (and pass with `--directed`). Launching runs and the run's
  own data stay the agent's. The pointer paragraph says so (v36).

## 0.185.0

- **`log_artifact` no longer waits for the credential scan.** A queued upload is copied into the
  outbox's waiting room and the call returns (4-60 ms on 11-32 MB files that used to block the
  training loop for 205-346 s). The detached worker then runs a light, dependency-free check that
  replaces obvious credentials in text files (vendor-prefixed tokens, private keys, JWTs,
  `password = ...`, key-name + random-looking value) before the file is fingerprinted and queued;
  zips, tarballs, checkpoints and other binary files upload byte-for-byte as before, and the server
  inspects every upload in full and records what it finds. A run's uploads reach the server in the
  order they were logged, and its close lands after them: `finish()` / `probe run end` finish any
  scans for their run before the run is closed (`probe run end` exits 2 if one cannot be), and
  delivery holds a run's later writes behind an upload still being scanned. A crash at any point
  leaves each upload delivered exactly once, redacted, and an older CLI or SDK sharing the outbox
  never sees an unscanned file. An upload whose scan cannot run is refused with the reason in
  `probe outbox status` (log it again), never sent unscanned. `probe outbox status` shows uploads
  still waiting. Direct (`sync=True`) uploads scan once instead of three or four times.
  `PROBE_ARTIFACT_OPAQUE_POLICY=block` keeps the full inspection on the caller's thread, with its
  inline refusal. Note: an older `probe run end` sharing the outbox does not know to wait for the
  waiting room.
- **Credential scanning answers the same, faster, and decodes one layer as documented.** The shared
  scanner's entropy and printable-text checks run in C instead of a per-character Python loop, and a
  text longer than 64K characters is no longer decoded a second time: a doubly-escaped value was
  found or missed depending on the length of the text around it. The scanner also accepts a search
  accelerator, which the server's Hyperscan index uses; nothing on this side of the wire does. The
  plain anchored-value check now also reads `privateKey` and `accessKey` written without a
  separator, and escape decoding runs in one regex pass.

## 0.184.0

- **A run now saves what it produces, automatically.** When a run opened by `probe.init()`,
  `Client.run()` or `probe exec` ends -- `finish()`, a crash, interpreter exit, or a hard death
  (a segfault, the OOM killer, SIGTERM) -- every file it created or changed in its working folder
  reaches the run as `outputs/<path>`, and everything it printed is saved as `probe/run.log`
  (first 2 MB + last 8 MB; C-level output included). A small helper process carries the output:
  it ignores every signal it can, so a scheduler's SIGUSR1/USR2 or Ctrl-C never breaks the
  program's stdout, and it outlives the run to recover the log and files after a hard death. No
  signal handler is installed in your program. This works on any machine, including
  Modal/Ray/Slurm jobs nothing else can see. Files over 64 MB (the most an upload carries), past
  the close's 120 s inspection budget (`PROBE_CAPTURE_BUDGET_SEC`, or the finish timeout), or not
  inspectable are recorded as a pointer on storage that lasts (a network drive, a Modal Volume,
  your own disk) and listed in `probe/outputs-manifest.json` on a throwaway box's own disk.
  `.git`, `.venv`, `node_modules`, caches, credential-shaped names and, in a home folder, every
  dot-entry are skipped; a text credential is redacted (in the log too), and a file holding one
  the gate cannot redact is skipped with a warning naming it. Files already logged with
  `log_artifact` are not uploaded twice. The uploads are non-blocking outbox ops: `finish()`
  tries to deliver them first, but an outage can no longer keep a run open. Narrow the sweep with
  `probe.init(outputs="results/")` / `probe exec --outputs` (runs sharing a folder at the same
  time skip the sweep and say so; ranks of one run each sweep, skipping unread what another
  already queued, and each keep `probe/run.rank<N>.log`); opt out with `capture_outputs=False`, `PROBE_CAPTURE_OUTPUTS=0` or
  `probe exec --no-capture-outputs`, or drop just the log with `PROBE_CAPTURE_LOG=0` -- which also
  gives up capture after a hard death.
- **The artifact secret check runs once per file, not five times.** An upload went through the full
  inspection up to five times (about 1 MB/s on JSON text); verdicts are now cached per process by
  content hash. Only verdicts are cached, never refusals, and a fork never inherits the cache's
  lock.
- **An encoded credential left beside a replaced one is now reported.** When a file held a literal
  GitHub token AND the same kind of token base64-encoded, replacing the literal one counted the
  encoded one as handled too (they share a rule name). The redacted bytes are now inspected again
  and whatever is still found is recorded -- output capture then skips the file.
- **The `instrument-code` and `track-work` skills say what is captured for you** and when to still
  call `log_artifact`: a file outside the run's folder, a name or kind you choose, a bucket path.

## 0.183.0

- **A run now records the files it reads, so its parent is a fact, not a guess.** `probe.init()`
  watches every file the run opens for reading (Python's audit hook: `open`, `pathlib`, numpy,
  pandas, `torch.load`, PIL, pickle), hashes each one, and sends the list when the run finishes.
  The server matches each hash to the run that wrote those bytes and shows a `consumes` edge to
  the file and a `derived_from` edge to its writer. `probe exec` records the same for a Python
  child. Sending never holds a run's close; a run that dies leaves its list on disk, and the next
  run on the machine sends it. Datasets are hashed once per machine (a local cache); files over
  1 GiB, or past 10 GB per run, get a fingerprint instead of a full hash. Not seen: readers that
  open files from C (`pyarrow.parquet.read_table` called directly, `h5py`, `safetensors`), and
  every run says so. Off with `probe.init(capture_reads=False)` or `PROBE_CAPTURE_READS=0`
  (also in a `probe exec` child's own environment); `Client.run(...)` records only with
  `capture_reads=True`. Needs a server that declares `run_inputs`; against an older one nothing
  is hashed or kept. The close waits at most 30 s for hashing (a stalled mount costs a read its
  hash, never the run its close); a file rewritten at the same path is recorded again; each
  process sends its own reads, so ranks sharing a run and nodes sharing a home directory never
  take each other's; credential files (`~/.kube`, `~/.docker`, the Hugging Face token, ...)
  are never recorded.
- **`log_artifact` marks whether a file was written during the run** (`written_during_run`,
  `written_at` in its meta), so a copied-in input no longer counts as the run's output.
- **A retry the SDK itself made is labelled `observed_call`** (`on_conflict="supersede"` and the
  automatic retry), so a reader can tell it from a parent a person named.
- **New SDK reads:** `client.run_inputs(run)`, `client.run_upstream(run, depth=2)` (what a run
  built on, several hops back), `client.artifact_lineage(artifact)` (who wrote a file and who read
  it), `client.artifacts_by_hash(sha256)`, and `client.correct_run_input(run, path,
  dismissed=True | version_id=...)` to fix a wrong match. MCP: `entity(view="lineage")` on an
  artifact, and `view_options={"depth": N}` on a run's lineage view walks N hops up; against a
  server without read lineage both say so instead of reporting the file missing.
- **`instrument-code` has a "WHAT THE RUN READ" section**: what is recorded, what is not, and how
  to record an unseen read with `probe edge add --relation consumes`.

## 0.182.3

- **A session your team had deleted is no longer re-sent forever** (tap 0.8.4).
  When a customer asks us to delete a captured session, the server refuses it for good: 410
  `session_deleted` on upload, `state: "deleted"` on the receipts read. The tap used to file that
  under "retained for retry" and re-send it every tick for as long as its daemon ran. Now the
  transcript journal treats either answer as final: it drops what was waiting to upload for that
  session, deletes its local snapshot copy (never your own transcript file), marks the session done
  so nothing stages it again, and logs it once. The same holds for the old outbox (every queued
  batch of the session is dropped and no more are spooled), and `probe backfill`'s session import
  counts such a session as "deleted at your team's request (not uploaded)" instead of a failure.
  Its journal is the one the tap reads, so a deletion the import meets is also final for capture.
  Only the server's own statement about that session counts: a 410 for another reason, or one
  naming another session, keeps the batch for a retry as before.

## 0.182.2

- **Four more reads are no longer refused as writes.** In `read` and `daemon`, the write gate and
  the plugin's guard hook refused `probe project code list`, bare `probe project contributors`,
  `probe wandb discover` and `probe wandb key status`: they only read two words of a command, so a
  subgroup (`project code`) or a read that turns into a write only with `--add`/`--remove` looked
  like a write, and everything under `wandb` counted as an import. Now a subgroup's own verb
  decides, `project contributors` is a write only with `--add` or `--remove`, and `wandb discover`
  and `wandb key set|status` (local only; they never talk to Probe) pass in every state, `off`
  included. `probe project reference remove` joins the other removals, which no state refuses.
  Refusals now name the full command (`probe project code attach`, `probe wandb import-local`).
- **The guard hook reads shell lines more carefully.** Under `off` it no longer refuses `--help` on
  a read (`probe run list --help`); the CLI already allowed it. A redirection such as `&>/dev/null`
  or `>&file` is no longer read as the end of the command, and a leading one is skipped with its
  file, so `>/dev/null probe project create x` and `probe project code &>/dev/null attach ...` are
  now caught, and so is a write followed by a comment with an apostrophe (`# don't`), which used to
  drop the whole line. A refusal from an older CLI that names fewer words still counts as one.

## 0.182.1

- **`probe backfill` says when a team's page generation is paused.** When the server refuses an
  AI Summary refresh because the team's page generation is switched off (a 409 with `code:
  generation_paused`), reconstruction now reports "<project>: Optional AI Summary not requested:
  page updates are paused for your team. Nothing was queued." and records the summary as `paused`.
  It used to print "<project>: summary pending (ConflictError).", save the request as `unknown`,
  and on every later run offer to retry a refresh that never existed. A later run checks whether a
  refresh is already running (generation may be back on) and otherwise asks the ordinary question
  again. The SDK exports the code as `probe.sdk.errors.GENERATION_PAUSED`.

## 0.182.0

- **Team rules ("workflow memory") are removed.** The feature was never finished. `probe rule
  preview|declare|publish|list` is gone (the CLI now answers "No such command 'rule'"), along with
  the SDK's `preview_rule`, `declare_rule`, `publish_rule` and `query_rules`, the write gate's
  `rule` branch, the `read-rules` and `set-rule` skills (out of the plugin since 0.113.0, when the
  feature went behind a per-user flag), and the MCP server's `probe_procedures` tool and its
  per-user tool-list filter. The server removes the `/v1/procedures/*` routes in the same change.
  Rules already stored are not deleted.

## 0.181.0

- **Reads are no longer refused as writes.** In `read` and `daemon`, the write gate refused eight
  commands that only print what is recorded: `probe artifact tree`, `artifact pin-impact`,
  `experiment edges`, `paper edges`, `run metrics`, `run series`, `views data` and `views preview`.
  The second daemon trial hit it on `artifact tree`. A test now sorts every command inside a write
  group on purpose, so a new read cannot land as a write again.
- **Skills say which run owner to use.** `track-work` §3 and `instrument-code` now open with the rule: put
  the SDK in scripts you write; use `probe exec` only for code you can't edit or a launcher that only
  submits the job. The SDK is listed first. Before, `probe exec` came first with no rule for choosing.

## 0.180.2

- **The wizard's menu stops saying "Update needed" for a Claude Code tap that is already updated.**
  The Versions row graded the tap by the version that last RAN, a stamp only a new Claude Code
  session rewrites, so right after `probe update` said "transcript tap already at the latest
  (0.8.3)" the menu still read "Update needed — tap 0.6.1 → 0.8.3", and pressing Update again
  could never clear it. The row, `probe doctor`'s tap warning and `probe update --check` now grade
  Claude Code's install ledger whenever it is ahead of that stamp, read with `probe update`'s own
  reader so the two cannot pick different entries when the tap is installed in more than one
  scope. A Codex or pi tap is graded only by its own stamp, and without one reads as unchecked
  instead of borrowing Claude Code's number. Codex keeps the original gap for now: its stamp also
  moves only when a Codex session starts.

## 0.180.1

- **A Mac that cannot run git gets the one command that fixes it, not a wall of errors.** Until the
  Xcode license is accepted, and while the Xcode command line tools are missing or point at a deleted
  Xcode, macOS refuses to run `git`, so every `claude`/`codex` marketplace refresh fails. `probe
  update` and the wizard's Update pasted Apple's paragraph in full, again on the Codex tap line,
  then listed manual commands that fail the same way; the wizard's install run printed it raw on
  its refresh step, or on a first install only a "marketplace not found". Each now says "git on
  this Mac is blocked until the Xcode license is accepted. Run `sudo xcodebuild -license` in a
  terminal and agree to it, then try again." (or `xcode-select --install` / `sudo xcode-select
  --reset`), with no manual commands after it. The interactive `-license` is deliberate: `probe
  doctor` replays the message inside agent sessions, and accepting the license is the person's
  call. Text a git server sent (`remote:`) never triggers it. Also, a tap that failed for the
  plugin's own reason says "same failure as above", and when the plugin is already current the tap
  line now carries the failed refresh's reason instead of a bare "skipped".
- **The daemon no longer loses a conclusions pass to a long think** (tap 0.8.3). The model's
  reasoning counts against the answer's 16k-token limit. In 2 of 99 conclusions passes in the replay
  bench it ran out of room before the JSON was complete, and the daemon dropped the pass (the
  decision notes, the file notes, the lineage) until the next turn, or for good at session end: the
  replay that hit it twice ended with no file notes and no PCA lineage. An answer cut off at the
  limit is now asked again at once, with a note to keep the reasoning short.
- **Tap 0.8.2 is tap 0.8.0 again.** 0.8.1 added the daemon judge's SKIP mode and a session-end audit,
  both off by default; they are withdrawn until the replay bench shows them doing better than 0.8.0
  (on both trial fixtures 0.8.1 tied 0.8.0 at 8/8 and 7/7), and stay in an open PR until then.

## 0.180.0

- **The Probe daemon records what the session concluded, not just what it ran** (tap 0.8.0).
  Replayed against the two digits trials, it matches what the inline agent recorded: the decision
  with both numbers, the caveat, the PCA verdict, lineage, the parent's description and a note on
  every result file, where the daemon before it recorded one PCA note and no file notes. What
  changed:
  - A **turn-end signal** (a new `Stop` hook; pi's `agent_settled`) makes the daemon read a finished
    turn at once, then run a **conclusions pass** over the whole session (at most one per 10
    minutes, and always at session end): conclusions with both sides of every comparison, caveats,
    the experiment's and project's main documents while they are empty, a parent project's empty
    description, `derived_from`/`retried_from` lineage, an `abandoned` tag on a run the session
    replaced, and a one-line note on every result file. Codex runs the new `Stop` hook once you
    approve it; until then the daemon concludes at session end.
  - **Nothing is trimmed**: commands whole and outputs up to 120k characters, sized to the
    gateway's request limit; anything longer or older is one `expand`/`grep` away, and a section of
    track-work's detailed reference (now vendored as `tap/companion_reference.md`) too. Every model
    round is kept exactly, redacted: `probe companion trace` lists the cycles, `probe companion
    trace <cycle>` prints one (7 days / 50 MB per session; needs this CLI release).
  - Runs a sweep script printed are the session's when they were created during it in a project a
    `probe` command of the session touched; text inside quotes or a heredoc is never read as a
    `probe` command.
  - **Any kind of file the session produced uploads**, and every one is scanned for credentials
    first (text by content, in any encoding; other files by their readable runs). Compressed
    archives, which the scan cannot read into, and files from before the session are left for you
    to upload with `--directed`.
  - Chunks with nothing to decide (reads and listings only) skip the model call, on the record.
    With the server's new `POST /v1/companion/judge` enabled for the workspace, Jev flags the
    agent's paragraphs that no existing note records for the conclusions pass, and records per-kind
    answers beside each turn; it never replaces a model call. `PROBE_COMPANION_JUDGE=off` turns it
    off.

- **One statement of who writes what in the `daemon` state.** You launch and the daemon records.
  Yours: the project, experiment and sweep group you launch into, starting runs (`probe exec` or the
  SDK), the run's own data and `run end`. The daemon's: everything else (notes, artifacts, papers,
  tags, names, descriptions, lineage). A write the researcher asks for takes `--directed`. The
  session-start context, the flip notice, the `probe` and `track-work` skills, the gate's refusal
  and the after-the-fact notice now all say this (six texts had said it six ways, and an agent spent
  2.5 of 8.5 minutes of a trial reading the gate's source). The text lives once in
  `session_marker.DAEMON_CONTEXT`; pi keeps a pinned copy, and `test_daemon_split_texts.py` holds
  every surface to it.

- **Moving to `daemon` mid-session now tells the agent the whole split.** The flip notice carries the
  full session-start text instead of one line, so a session that never saw SessionStart in
  `daemon` learns what stays its own.

- **`probe group create` and an in-run `probe artifact add` are no longer refused in `daemon`.** A
  sweep's group has to exist before `run start --group`, so it is part of the launch. Inside a run's
  own job (`PROBE_RUN_ID` set) `probe artifact add` onto THAT run is the run attaching its file, the
  same write the SDK's `log_artifact` makes there ungated; onto another run, or filed on a project,
  experiment, workspace or Shared, it is gated as before.

- **`probe session status` lists the agent's writes in `daemon`.** The `daemon` block gains
  `agent_writes` (the gate's own list), `agent_writes_in_run` and a `directed` line; the refusal
  points there.

- **A probe command mentioned inside quotes or a heredoc is no longer refused.** The guard hook
  split the raw command on `|` and `;` before reading quotes, so a quoted regex naming a probe
  command, or a commit message about one, looked like a write. It now drops heredoc bodies (a
  `<<` the shell reads: not one inside quotes or `$((...))` arithmetic; `<<\EOF` included),
  tokenizes with quotes honoured and splits on the real operators (newlines included). A write
  piped into or chained after another command is still refused.

- **An image artifact is a `plot`.** With no kind given, `log_artifact` and `probe artifact add` on
  a run file `.png`, `.jpg`, `.jpeg`, `.svg`, `.gif` and `.webp` as `plot`, anything else as
  `file`. A PDF stays a `file` (a paper or a report far more often than a figure). An explicit kind
  always wins.

- **`probe update` reports the transcript tap and fails when it stays behind.** It already asked
  `claude` to update the tap, but read nothing back. It now prints the tap's version beside the
  CLI's and the plugin's, and when an installed tap is still behind the published one it says so
  and why (the failed command and what it printed, a timeout, or `claude` exiting 0 without moving
  it, which is the in-session no-op the plugin line already names), prints the manual commands and
  records the update as failed (Claude Code and Codex). `probe doctor` warns when the tap is behind.

## 0.179.4

- **A failed plugin update says why.** The wizard's Update printed "`claude plugin update` did not
  complete" (or the Codex equivalent) and dropped the error `claude`/`codex` had printed, so
  neither the screen nor `probe doctor` could say what went wrong. It now names the command that
  failed and what it printed about why, with credentials, home paths and terminal control
  characters removed. The Claude marketplace refresh also gets 150s instead of 90s: Claude allows
  that refresh 120s itself, so a slow clone was cut off before Claude gave up on it. When the
  refresh fails, the tap update is skipped rather than waiting out a second timeout.

- **Update from a temporary environment (`npx`, `uvx`, `pipx run`) says what it did to your
  installed copy**: already current, installing one, or, when the temporary copy is itself older
  than the latest release, how to upgrade the installed one. It used to print "upgrading your
  installed copy instead" and then nothing. A permanent install that fails is now reported as a
  failed update, not a successful one.

- **After Update upgrades the CLI, the wizard's Versions row and Diagnose page show the new
  version.** Both graded the running process's own version, which cannot change until it exits,
  so the menu kept saying "Update available" right after a successful upgrade. The number now
  comes from the upgraded install itself, not whichever `probe` is first on your PATH.

- **Transcript capture ends a session when its agent process ends, never because it went quiet**
  (tap 0.7.2). The tap used to finalize any session that had been quiet for ten minutes with no
  process holding its transcript open, and Claude Code never holds it open: one session was
  finalized 29 times in three days and mined again each time. SessionStart now records the
  `claude`/`codex` process that owns the session (pid and start time), and the daemon finalizes
  when that exact process is gone, including a hard kill that skips SessionEnd. A daemon that
  cannot name its owner (pi) ends the session after a day of quiet, the server sweep's window.

- **SessionEnd waits up to 15s for the final delivery** (tap 0.7.2). It returned at once, so
  wherever the agent's exit took the machine down with it (a container, a CI job) the daemon died
  before sending the session's finalize. `/clear` and `/resume` do not wait. The wait is a second
  SessionEnd hook, so the existing one keeps its Codex approval; Codex runs the new one once it is
  approved.

- **A batch staged under older redaction rules no longer blocks its session forever** (tap 0.7.2).
  The daemon asks the server about that batch: it adopts the receipt if the server has one, and
  otherwise re-redacts the same batch in place (same sequence and range, so a copy an older daemon
  delivers meanwhile is adopted too). Redaction markers no longer anchor the next value: the
  "secret" in `<redacted:anchored-secret>` used to redact one more value per scan, so a daemon
  could refuse a body it had just staged.

- **Ingest tokens from `probe login` are redacted from transcripts.** The rule matched only the
  48-hex tokens pairing mints; the device login's 32-hex `ros_ing_` tokens passed through.

## 0.179.3

- **The Probe daemon uploads files wherever the session worked** (tap 0.7.1). It no longer refuses a
  session started in the home folder or a file outside the starting folder: a relative path a
  command printed resolves against the folder that command `cd`'d into. Before uploading it checks
  the target AND every run the session worked on for the same bytes, so a file the agent already
  logged with the SDK is never uploaded twice. Credential-shaped names, hidden folders and the
  secret scan still apply.

- **The daemon state reads `tracking (daemon)`, not `on (daemon)`.** The status line and pi's footer
  now use the same word as the `on` state, with the switch position in brackets:
  `tracking (daemon) → <project>`, and `tracking (daemon degraded) → <project>` while the agent has
  recording back.

## 0.179.2

- **The status line names the switch position in `daemon`.** `on (daemon) → <project>` while the
  daemon records, `on (daemon degraded) → <project>` while the agent has recording back (it used to
  read `daemon`, then `tracking` when degraded). pi's footer, which said `tracking` in every recording
  state, now says the same and redraws each turn so the lease going live shows up.

- **`probe companion authorize` exits 1 when no key was minted** (an expired or declined
  approval), so a script can tell it from success.

## 0.179.1

- **`probe companion authorize` prints the approval link.** It passed no prompt to the device
  flow, so `--no-browser` (or any machine where the browser does not open) showed nothing and
  polled until the code expired. The wizard's `daemon` choice goes through the same path.

## 0.179.0

- **The Probe daemon: a fourth state of the `probe` switch.** `on` / `daemon` / `read` / `off`.
  In `daemon`, a background worker reads the session transcript and records the work in Probe
  (titled `companion:` sub-notes, empty names and descriptions, tags, papers, run lineage,
  ending runs the session opened, files the session produced), so the agent does not have to.
  The agent keeps its reads, still starts runs, and makes the writes the researcher asks for with
  `--directed`. Off by default: choose it in `probe setup --action settings` (which mints the
  daemon's own read + write, never-delete key) or per session with `/probe daemon`. When the daemon
  is down, out of budget or unauthorized, the session reads `daemon (degraded)` and the agent
  records as in `on`. The worker is a child of the capture daemon, so it needs capture on
  (probe-research-tap 0.7.0).
- **The `probe` CLI enforces the switch itself.** In a coding-agent session, a write the session's
  state does not allow is refused before it runs (exit 3), on every harness, pi included. A
  refused `probe exec` still runs its command, just unrecorded. Never gated: a shell outside an
  agent session (a person's own terminal), anything inside a run (`probe exec` and the SDK export
  `PROBE_RUN_ID`), and `--help`.
- `probe companion authorize | log | report | feedback`: approve the daemon's key, see every
  decision it made and why, who owned which part of the transcript, and correct a write.
  `probe doctor` reports the daemon.
- Session capture no longer sends Probe's own credentials: `probe_pat_`, `probe_svc_`, `ros_ing_`
  tokens and Claude OAuth tokens are redacted like any other secret.
- Prompts: the `probe` and `track-work` skills, the setup command and the pointer block
  (version 35) describe the `daemon` state; four skill descriptions are shorter.
- Docstrings in the SDK client and MCP service now cite the `/v1/projects/...` routes the calls
  actually hit, not the retired `/v1/experiments/...` addresses (which answer 410). No behaviour
  change.

## 0.178.0

- **`query_sql`, the MCP's seventh tool.** Read-only SQL over the lab's research tables for
  exact counts, joins and verifying a number, under the caller's own tenant and visibility.
  Call it with no `sql` for the table list, pass `tables` for columns, then send one SELECT.
  It answers in ONE page and never returns a cursor: re-reading would re-run the query. Its
  description is generated from research-os (`make gen-sql`), so it matches the dashboard
  assistant word for word. A plan refusal now carries the server's `hint` into the error.
- `Client.enqueue_artifact_reference(anchor="experiment", ...)` now delivers to the experiment's
  project address. It still queued `POST /v1/experiments/{id}/artifacts`, which answers 410
  since the experiments API was retired, so the reference was dropped on delivery. The CLI's own
  backfill passes a project anchor and was not affected.
- **Ctrl-C under `probe exec` no longer mails you a crash notice.** `execute` closed a wrapped run
  on "is the exit code zero", so an interrupt landed as `failed` and the crash notifier told the
  researcher their run had died -- a run they had just stopped themselves. SIGINT now maps to
  `canceled`, the call `fluent.py` already makes in-process for `KeyboardInterrupt`. Both spellings
  (`-2` from `subprocess.run`, `130` through a shell). SIGTERM and SIGKILL deliberately still fail:
  a preemption or an eviction is exactly the death a researcher does NOT already know about.
- `search` with the default `collapse="experiment"` still shows an experiment once. The server
  now names an experiment by its project address in both channels, so the collapse folds
  repeated `project` hits of one id as well.

## 0.177.0

## 0.176.0

- **Hardware metrics are on by default.** A bare `run()` now collects GPU and host metrics;
  `PROBE_HW=0` (or `false`/`off`) disables, and an explicit `run(hw=...)` still wins over the
  environment in both directions. This reverses the opt-in default of 2026-08-06. The reason is
  measured: of 209 crashed runs over 30 days of production, FIVE had any hardware series, and five
  of the fourteen crash detectors -- `gpu_thermal`, `gpu_cold`, `gpu_memory_creep`,
  `host_memory_pressure`, `disk_filling` -- read nothing else. Those are the detectors that explain
  an OOM or a SIGKILL, the deaths hardest to diagnose from inside the job, so opt-in left the most
  valuable third of the library dark for 97.6% of the runs that needed it. Expect roughly 4 series
  per GPU sampled every 15s; on a box with no GPU the system source alone is cheaper still.

## 0.175.0

- **`summary_markdown` is `document` on a project and an experiment, and gone
  on a run and a paper.** BREAKING. The field named a database column that no
  longer holds anything: a project's and an experiment's authored Markdown is
  a marked block inside its Overview page, and `document` says so. A run and a
  paper were never on that page lane, so there is nothing to rename -- the
  field is retired, and every door onto it with it.

  * SDK: `create_project`, `update_project`, `create_experiment` and
    `update_experiment` take `document`. `create_run`, `create_project_run`
    and `update_run` no longer take an authored document at all, and
    `Run.summary_markdown` is gone.
  * CLI: `--summary` stays on `project` and `experiment`; it is gone from
    `run start`, `run child` and `run set`.
  * MCP: the `summary` view is project and experiment only. `patch_run` no
    longer declares a document argument -- it would have answered 200 and
    stored nothing.

  A pinned older client keeps working against the old field until the server
  release that renames it lands; after that, `summary_markdown` is refused
  rather than ignored, so an un-upgraded write fails loudly instead of
  silently dropping a researcher's text.
- A detected credential is replaced rather than refused, so an upload is never
  lost to a false positive. Text has its spans replaced with
  `<redacted:{rule}>` before the file is hashed, so the credential never leaves
  the machine; containers and non-UTF-8 bytes are uploaded unchanged with the
  finding recorded, because rewriting inside one corrupts it. Only the certain
  detectors rewrite -- the key-name tier is recorded and never touches bytes.
  The file on disk is never modified.

## 0.174.3

- **The status line says what is missing when no transcript daemon is running.** `◐ tracking · no capture: <reason>` read, beside `tracking`, as "nothing is being recorded" — the opposite of the truth, since every run, metric and artifact still lands and only the conversation does not. Every surface now says `not capturing session transcript`: the Claude Code segment (`◐ tracking → project · not capturing session transcript: <reason>`), the pi footer, the Codex notice (`…, but not capturing session transcript: <reason>`), and `probe session status`, whose `effective` field reads `tracked, not capturing session transcript` where it said `tracked, not captured`. The segment's width ceiling is now derived from the widest line that must render whole — the bare degraded form with the longest reason — so the longer label costs the ceiling and never the project name, and a test pins that ceiling against the reason vocabulary so no reason can be elided to `interp…`.

- The credential gate no longer refuses model output. A name that is also an
  ordinary English word (`cookie`, `token`, `secret`, `credential`) now needs a
  key-shaped VALUE before it redacts, so a GSM8K answer reading `$0.10/cookie =
  $6`, a tokenizer vocabulary dump and `{"token": 50257}` upload unchanged.
  Compound names (`auth_token`, `set_cookie`, `wandb_api_key`) and URL query
  parameters are unchanged, and every real credential shape is still refused.
  A base64-shaped run of one repeated character is no longer decoded as a
  candidate: it cannot carry key material.

- **The SDK sends its headline scalars under both wire names.** `summary_metrics` is
  the field's real name and `summary` the alias the server still accepts. Sending only
  the alias means the server can never drop it without silently discarding the map from
  every client in the field; sending only the new name loses it against a server older
  than the rename. Both keys close both holes, and this is what lets the alias be
  removed server-side in a later release.

- **Metric reads work on runs mirrored from another tool.** Points for such a run are
  fetched from the source rather than copied, and the server refuses any reader that has
  not declared which coverage contract it understands -- a guard against handing back a
  re-sampled series as though it were the whole run. The client never declared one, so
  every MCP metric view on a W&B-synced run failed outright and reported no metrics,
  while the same data came back fine over HTTP. The client now declares the contract on
  each provider read, and the raw-point view follows the server's own redirect to the
  door that can serve a mirrored run instead of surfacing it as an error.

- A node agent, phase one. Nothing inside a job can report why the job died: the hardware rail
  looks like it could, but it runs as a daemon thread inside the training process and stops
  existing at the same instant as the thing it would explain. This adds the first piece that is
  not inside the job. A run beating as owner registers which process on this machine it is, and a
  separate watcher -- one per box, holding a file lease, in its own session so a Ctrl-C aimed at
  the job cannot reach it -- notices when that process stops existing and records it against the
  run. Process identity is the pid AND the kernel's creation time for it, so a recycled pid is
  never mistaken for the original. It observes within one ten-second sweep, where the server's
  heartbeat reaper takes fifteen to seventeen minutes, and it separates two facts that were
  previously indistinguishable: a stale heartbeat means nothing reported recently, which a wedged
  process produces while alive, and a vanished pid means the job is actually gone. Opt-in via
  `PROBE_BOX=1`, fail-open throughout, and read-only with respect to every process it watches.
  Later phases add the evidence only the box holds: kernel out-of-memory lines, a scheduler's
  termination reason, a card's ECC counters.

## 0.174.2

- `instrument-code` now tells a script to stamp `wall_clock=` with the event's
  own time: omitted, the server records when the point arrived, which behind the
  SDK's durable queue is drain time and a fictional axis. Its destination check
  also reads an artifact's `uri` back, because a metric count cannot see a file
  recorded but never stored.
- `track-work` says what attaching a W&B project actually does: live sync sees
  only runs created after it is enabled, so existing runs need the import.

## 0.174.1

- `--authored-by` is available on `probe exec` too, the command `run start`
  points to. `probe run fork --authored-by human` no longer sends `agent`: a
  fork that names itself only declares that name when you have not said who
  wrote one. The same now holds for a superseded run's `<name>-r2`. Creating a
  project with an empty `--name` works again instead of failing validation, and
  a backfill import declares the names it composes from folders and spec files
  as agent-written, so they stay improvable.

## 0.174.0

- The CLI and SDK now say who composed a name or a description. Pass
  `--authored-by human` when the researcher gave you the words and the text is
  locked: no model will ever rewrite it. Under a coding agent the default is
  `agent`, which means the server may improve the wording later; typed by hand
  in a terminal nothing is sent and the behaviour is exactly what it has always
  been. Available on `project create|set`, `experiment create|set`, and
  `run start|set|child|fork`, and on the matching SDK methods. Creating a run
  also declares the same author for any project or experiment created alongside
  it. A fork that names itself `<source>-fork` says so, so that fabricated name
  stays improvable.

- The `track-work` skill now asks a session to name a project or experiment
  instead of leaving it unnamed. A slug is permanent; a name is not, so the
  session writes the best title it can from the context it has and the server
  refines it later. The list of things a human-facing value must never contain
  (hashes, uuids, timestamps, ticket numbers, bare counters) moved up to the
  hard guidelines, where it now covers tags, metric keys and span names too.

## 0.173.1

- Escape NUL characters as visible `\0` in recognized descriptive span attributes
  before upload, including queued span replay and ingest batches. Record per-span
  counts under `probe.nul_escaped`; this display escape is not reversible.
  Reject NUL-bearing keys, identities and unsupported fields locally as permanent
  validation errors, retaining the rejected outbox operation while later writes
  continue. Rejection survives retries and older SDK drainers. Cyclic/deep JSON
  fails promptly; warnings contain no user keys or caller source lines. Inspect
  NUL-obfuscated credentials, including serialized text, before partial redaction.
  Materialize opaque values before validation. Malformed legacy records retain
  a rejection marker with the unsafe payload explicitly omitted.
  Artifact bytes are not changed by this safeguard. Existing credential scrubbing
  and artifact inspection still apply. This does not change the finish barrier.

## 0.173.0

- Transcript and SDK content now receives mandatory credential scrubbing before
  journaling and upload, including nested metadata, encoded text and diagnostic
  output. Artifact bytes are inspected before staging and replay; detected
  credentials are refused. Opaque formats and objects above 64 MiB are refused
  by default. See `docs/artifact-credential-gate.md` for limits and the separate
  server enforcement policy. Existing upload receipts remain replayable when
  their original source file has moved or changed.

- **One run's stuck outbox op can no longer fail another run's close.** The
  journal is shared per directory across runs and `drain` is strict FIFO, so a
  single undeliverable op parked every op behind it — and then the NEXT run's
  `finish()` raised `run <id> not closed` over a queue it had no stake in.
  `Run.finish()` now passes `run_ref=self.id` to the drain it already ran, the
  barrier scoping `drain(run_ref=...)`, `probe run end` and
  `Run._flush_for_span` have used all along. `Client.flush()` gains a
  `run_ref=` keyword for it; the default stays MACHINE-WIDE, so `probe outbox
  drain`, the detached worker and post-outage recovery are unchanged. FIFO is
  not weakened — a run's writes are still attempted in enqueue order, and they
  only ever needed ordering against each other.

- `Client.session_artifacts()` is removed, with the backend route it called
  (`GET /v1/sessions/{id}/artifacts`). The conversation-artifact lane behind it
  is deleted server-side: the session page's transcript already rendered
  everything the extractor re-rendered beside it. Nothing called this method --
  its only caller was an MCP source wrapper that no tool reached -- so no
  command or tool changes. The generated models lose `SessionArtifactsOut`,
  `SessionVisualsOut`, `SessionVisualCandidate`, `SessionVisualKind` and
  `SessionVisualSourceRole`.

## 0.172.0

- **Every prompt surface rewritten shorter, in one register.** The CLAUDE.md /
  AGENTS.md pointer block (POINTER_VERSION 33) is 2,885 characters instead of
  6,507: what Probe is, the three doors and their skills, when to read prior
  work, and what NOT to call Probe for. The write doctrine lives in the
  `track-work` skill it always belonged to. MCP instructions and the six tool
  sheets are rewritten under the 2,048-character client cap, with one view
  vocabulary delivered on every arrival path (`available_views` on
  `search_knowledge` too, and a 422 that names each supported view). Hook
  messages use the switch's own words (on / read / off). Always-on context
  drops ~27% (6,434 -> 4,716 o200k tokens plus 798 -> 396 for the skill
  listing); hook strings ~16%; skill bodies ~35%.
- **`entity(filters=...)` is `entity(view_options=...)`.** The old name is
  accepted as an alias for one release (a hosted session's tool list predates
  the deploy) and refused when both are given; it is never silently ignored.
- **Skills renamed for consistency:** `notes-audit` -> `audit-team-note`,
  `pull-rules` -> `read-rules`; new `edit-notes` skill carries the notes method
  (checkout / edit / push, the team note as a file, compaction). A one-line
  `notes-audit` stub ships for this release only, so a CLI whose audit dispatch
  still names it resolves; drop it in 0.91. Old slugs stay in telemetry's
  legacy list for resumed transcripts.
- **Continuation cursors issued before this deploy do not resume** (the
  `entity` request binding gained `view_options`); re-issue the read.
- **Prose audited against CLI 0.171.0.** Dead flags removed from the skills
  (`--description` on create/set, `--summary` outside `paper`, `--hypothesis`
  -> `--question`, `notes append`/`edit` -> `checkout`/`push`); the MCP
  `entity` sheet no longer teaches `probe <kind> set --summary`: the authored
  Markdown below AI Summary is the researcher's by policy now (the flag still
  exists). `track-work` cites its reference by numbered section, guarded by
  `test_track_work_cross_refs.py`.
- **A retry gets a server-minted name like every other run.** `probe exec`
  takes `--parent RUN --relation retry|resume|fork|branch`, so the honest
  wrapper can say "this run re-attempts that one" without the retiring
  `run child`. `run child --name` and the SDK's `run.child(name)` are now
  optional: omitted, the request carries no name, the server mints a petname
  and titles the run from its content once it finishes.

  Why: `--name` stayed required on `run child` long after #1276 stopped
  `run start` fabricating names, so every retry was hand-named (`attempt-2`)
  and stamped `name_customized` -- the one kind of run permanently locked out
  of the title generation every other run gets.

## 0.171.0

- **The bare switch no longer lands on `off`.** `/probe` typed with no argument
  used to advance `on → read → off → on`. It now TOGGLES `on` <-> `read`, and
  `off` is reached only by typing `/probe off` (or `probe session state off`).
  A press while the switch is `off` leaves for `read`.

  Why: the bare switch is thrown without reading anything, often mid-thought to
  quiet a session. `off` is the one state that costs something invisible — no
  Probe calls at all, so an agent under it cannot find prior work AND cannot
  know what it missed, and every later answer is quietly poorer with nothing on
  screen to say so. Nobody should arrive there by one press too many. `on` and
  `read` both keep searching alive, so pressing between them costs only what
  you can see.

  `off` still answers a press rather than sticking: someone pressing a switch
  they turned off is asking for something to change, and `read` is the smallest
  change that gives back what `off` took away. Getting back to `off` means
  typing it again.

  The wizard's `Probe in new sessions` row still cycles all three — it is a
  settings screen you are looking at, with a separate commit step, so every
  default it can set has to be reachable there.

## 0.170.0

## 0.169.0

- **`show-research-status` is now `visualize-progress`.** "Status" collided with
  `probe session status` — a CLI read the skill itself calls — and undersold
  what the skill produces, which is a drawn timeline rather than a status line.
  The description now names session MOMENTS rather than only user phrasings, so
  the skill can fire unprompted: arriving in an unfamiliar project, before a run
  or sweep starts, when a run ends, at handoff, on a broad question about the
  work, and before proposing what to do next.

  The body is 8% shorter with nothing removed but justification, and two calls
  it taught were wrong against the code: `browse(scope=)` is `ref=` (`scope` is
  the service-side name), and `entity(ref=)` is `refs=[...]`, a list — FastMCP
  drops the unknown key and the call then fails on a missing required argument,
  so the old spelling could never have worked.

  The removed-skill guard now rejects `show-research-status`, so a stale
  cross-reference fails the suite instead of teaching an agent to invoke
  nothing. An installed plugin keeps serving the old name until it updates.

- **The status line spells `read-only` out, and `off` is now RED.** The segment
  showed `● read`, which is the switch's own word — and alone on one line, with
  no neighbouring word to lean on, `read` reads as an activity in progress
  rather than as a restriction. It now says `● read-only`. Nothing else moves:
  `read-only` is already the state's stored name and an accepted spelling
  everywhere a state is typed, so `/probe read` still works and
  `probe session status` still prints `read`.

  The dot's colour now separates the two non-recording states instead of
  painting both yellow. Red is reserved for `off` — the one position of the
  switch under which an agent cannot find prior work AND cannot know what it
  missed. `read-only` still answers questions, so it keeps yellow. A caller
  that resolved the switch to a boolean and passed no state is still yellow
  `not tracking`, never guessed into red.

## 0.168.0

- **`probe overview write` and the `write-overview` skill are removed.** The backend door they wrote through (`PUT .../overview`) is gone: the dashboard's own lane reads the same session transcripts and writes the first version of a project's or experiment's page itself. `Client.write_overview()` is removed with them, the skill no longer ships in the plugin or the pi package, and the folder importer's shared vocabulary is `track-work` alone.

## 0.167.0

- **The team note's audit reminder now arrives when you type, not from
  `CLAUDE.md`.** It used to be rendered into the managed team-note block, which
  every session of a harness reads — including `claude -p`, `codex exec`, a cron
  job and subagents. Those runs were being asked to spawn a background cleanup
  they cannot spawn, for a researcher who is not there when it finishes. The
  line now travels on the `UserPromptSubmit` hook, which fires only when a
  prompt is submitted, and stays silent in an automated session (`CODEX_CI=1`
  from `codex exec`, `CLAUDE_CODE_ENTRYPOINT=sdk-cli` from `claude -p`; an
  unrecognised harness is treated as a person, because the hook already only
  fires on a submitted prompt). It asks once per session.

  Two things fall out of the move. The rendered block no longer changes size
  when an audit is due, so a reminder can no longer be what tips a note into
  its pointer form. And a harness with no working hook — pi today, or a Codex
  install that has not trusted ours — simply never dispatches an audit, which
  is the honest answer where presence cannot be known.

- **A note nobody has audited in a week is re-checked for TRUTH, whatever its
  size.** Size still fires the tightening half and nothing else does: a date
  says nothing about whether a document is too long. What the weekly pass buys
  is the half that was closed by nothing — a claim only got corrected when a
  reader happened to hold the evidence against it, so the quiet claims went
  stale unopposed. The overdue dispatch says explicitly NOT to tighten.
  `PROBE_NOTES_AUDIT_INTERVAL_DAYS=0` turns the calendar off; the 24-hour floor
  and the one-audit-per-day stamp are unchanged.

- **New: `probe notes audit-advisory`.** Prints that line, or nothing. It is
  what the hook calls — local state only, no network, and silent in an
  automated session unless `--force`.

## 0.166.0

- **The switch's three states are called `on`, `read` and `off`.** They were
  `full`, `read-only` and `off`. `/probe read` is the new spelling of
  `/probe read-only`, and every old spelling still works — `full`, `read-only`,
  `readonly`, `read_only`, `ro` — so a resumed transcript, a script you wrote
  last month, or muscle memory keeps moving the switch exactly as it did.

  What is stored in your config file and in the session marker does NOT change:
  those still say `full` and `read-only`. That is deliberate rather than
  half-finished. Those files are read by every copy of the client on the
  machine, including an older plugin or a pi extension that has not updated
  yet, and a word this version invented reads to them as unrecognised — which
  resolves to recording. Renaming the words costs nothing; renaming the bytes
  would have turned somebody's opt-out into consent on exactly the machines
  that are half-upgraded.

- **The wizard can set all three states as the machine default.** The Settings
  screen's `Track sessions by default` tick box is now a `Probe in new sessions`
  row that CYCLES: the same key that ticks every other box walks it round
  `on → read → off`, the same order and the same direction as a bare `/probe`.
  Before this, the box could only say `on` or `read` — the empty box meant
  `read`, and a default of `off` was reachable only from the command line.

## 0.165.0

- **The Probe switch has three positions now, and `/probe` is where it lives.**
  `full` records your work as it happens, the way tracking always has.
  `read-only` stops the recording and leaves searching alone. `off` stops Probe
  entirely — no writes, no lookups, and nothing injected into your session at
  start. Type `/probe` on its own and it advances one step: full → read-only →
  off → back to full. Each press takes away exactly one thing, so you can learn
  it in one lap, and the switch now tells your agent where it landed instead of
  leaving it to guess.

  `off` is the genuinely new state. Until now the switch never gated reads, on
  purpose: a session that cannot record is still better off knowing what the
  team already tried. That reasoning still holds, and it is what `read-only`
  is. `off` is for the conversations where you want Probe to have no presence
  at all — and because it costs you searches you will not know you missed, an
  agent in `off` is told to say it could not look rather than report that
  nothing exists.

  Nothing you already recorded is ever deleted by moving the switch, and moving
  back does not backfill the gap. Cleanup keeps working in every state:
  "record nothing" was never "prevent cleanup".

- **`/track-work` keeps its name and loses the switch.** It is the manual for
  recording work from a shell; `/probe` decides whether it may. Everything you
  have typed before still works — `/track-work off` still moves the switch, and
  still means read-only, which is what it has always done.
  `/instrument-training-runs` is now `/instrument-code`, the same job from
  inside a script.

- **Set a default for new sessions in any of the three states**, per machine or
  per folder: `probe session default read-only`, or
  `probe session default off --folder ~/work/clientrepo`. New sessions still
  start at `full` unless you say otherwise. `probe session status` now reports
  `state`, `reads_allowed` and `writes_allowed`, and the status line reads
  `tracking → project`, `read-only`, or `off`.

### For contributors

- The per-session state is canonical in `sessions/<id>.state`;
  `sessions/<id>.tracking` is still written as the two-valued projection so no
  older client can read an opt-out as consent. Machine and folder defaults gain
  `defaults.session_state` beside `defaults.session_tracking`, on the same
  rule. Anything written in the old vocabulary keeps the meaning it was written
  with: `off` there is `read-only`, never the new `off`.
- `probe session initialize` and `session status` keep `tracking` and `signal`
  two-valued forever — pi's extension pins `signal: "on" | "off"` and ships on
  its own release train — and carry the third value in a new `state` field.
- The `PreToolUse` hook matcher widened to `^Bash$|probe[-_]research`, so `off`
  can refuse an MCP call. `full` returns from the guard after one file read,
  before any parsing.

## 0.164.0

- **The menu now says whether this device is up to date.** "On this device"
  listed what was switched on and nothing about whether it was the version we
  publish, so a machine three releases behind looked identical to a current one.
  A new Versions row carries the verdict — up to date, update available, update
  needed, update required — and the numbers behind it (`plugin 0.80.0 → 0.81.0`).
  It is the same per-component grading `probe doctor` prints, so the two can
  differ in wording and never in verdict, and an unreadable or missing manifest
  reads as "Not checked" rather than as good news. The wizard now also refreshes
  the cached version manifest itself: the only other refresher is gated on
  auto-update being ON, so the box nothing was keeping current was the one box
  that could never be told so.

## 0.163.0

- Show how many sessions a running session import has processed next to its status, in import details and on the import status page. A resumed import re-checks every session from the start while its bar counts only confirmed sessions, so the bar could sit at 3855/3857 for many minutes and look stuck.

- **Claude Code printed a warning about our hooks at every session start.**
  `hooks.json` declared `additionalContextLimit: 9000` on `SessionStart`. That
  key is Codex's; Claude Code has never had it, and once Claude Code began
  validating hook config it announced `unknown key "additionalContextLimit" in
  hooks.SessionStart[0] ignored` on every startup. The key is gone, and nothing
  it guarded is left: it sized this channel against the team-note brief, and the
  note stopped travelling here in 0.114.0 -- it renders into `CLAUDE.md` /
  `AGENTS.md` now. What remains is a few hundred characters of nudges (257 on a
  measured start), against a Codex default that is thousands. Splitting the file
  per harness is not the alternative: Codex supplements a manifest-declared
  hooks file on top of its own discovery of `hooks/hooks.json` rather than
  replacing it, so a second file runs every hook twice. A test now holds both
  plugins' hooks to keys both harnesses read, so the next one-sided key fails in
  CI instead of on a person's screen.

## 0.162.0

## 0.161.0

## 0.160.3

- Show Resume in import details only for stopped imports that can be resumed. Update actions live as the worker starts, stops, or completes; active scans, queued work, and automatic connection retries no longer show a Resume action.

## 0.160.2

## 0.160.1
- `wizard.uninstall_completed` resolves its identity before `finish_removal` revokes and clears the device credential. The event fires after that release, so the sender's lazy resolution found no token and fell back to `machine:<id>`; a destination filtering on a known person dropped it entirely. Fail-soft — an unresolvable identity leaves the event exactly as it was.

- Fix the `UserPromptSubmit` self-heal hook dying on Linux instead of reviving a dead capture daemon. It read the heal marker's mtime with `stat -f %m`, which is BSD-only; on GNU coreutils that prints a filesystem block rather than a timestamp, and the arithmetic that followed aborted the hook under `set -u`. Every prompt after the first in a session failed with `File: unbound variable` and no respawn was ever attempted, so a daemon that died mid-session stayed dead until a new session started.
- Automatically reconcile a stale transcript finalization when Probe already accepted the exact same source boundary, allowing newly approved messages to import without replaying old data. Recheck receipts once after a conflict and count matching, already-finalized approved content as complete; preserve unverified content and pending capture data.

## 0.160.0

- **"Tracking" now means a transcript daemon is running, or says why not.** The two were never connected: a session could report tracking on while no daemon had ever started, and nothing anywhere said so. `probe session status` and `session initialize` now carry a `capture` object and a third state, `tracked, not captured`, with one reason from a closed vocabulary (`not started`, `not installed`, `not paired`, `killswitch`, `disabled path`, `no session file`, `interpreter too old`, `halted`). The Claude Code status segment renders `◐ tracking → project · no capture: <reason>`; the pi footer and the Codex notice say the same in their own idiom.
- **On pi, the CLI starts the daemon itself when tracking says one should exist** -- from `session status`, from `track`/`toggle`, and from the root callback, at most once per session per ten minutes, printing a line when it does. pi's package filter (`"extensions": []`) loads our skills and MCP tools but not the extension that spawns capture, so there was no in-process code left to do it. Explicit opt-outs are never reversed: a killswitch, a disabled path and a capture that was never installed each keep their own reason.
- **A dead capture daemon is revived at the next prompt, not the next session.** A `UserPromptSubmit` hook costs one file read and one liveness check on the healthy path and respawns on the unhealthy one, bounded to once every ten minutes.
- **One spawner instead of two copies of a process-lifecycle contract.** `python -m tap start` is now the only place the detached crash-recovery wrapper lives; it re-runs the daemon's own gates and reports them as exit codes, and refuses outright on a Python below 3.11 instead of crash-looping.
- **A pi package installed at project scope counts as installed.** The `packages` read looked only at global settings, so a project-scope install reported "not installed" -- exactly where the capture bug also occurs. Both scopes now merge the way pi does.
- `probe doctor` names a pi package filter that excludes the capture extension, which is the one condition that made all of this invisible.

## 0.159.1

- Fix a race where refreshing import progress during worker startup could leave the import interrupted before its first attempt.
- Fix folder imports blocked by the retired personal-workspace lookup. Use the only available workspace or the newest created by the current user, preserving explicit and already-approved destinations.
- Restore automatic folder scanning and import with W&B selected. Choose or create one Probe project for the files and W&B history before starting; no later file-plan review is required. Workspace selection errors no longer ask the user to log in again.

## 0.159.0

## 0.158.10

- Offer connected W&B projects during the folder import flow, then review the chosen source's Probe destination before starting imports. Remove W&B linking from post-import status and details; file-only imports still support automatic background scanning.

## 0.158.9

- The install funnel now covers what the guided setup does after the plugins land: the installation settling, the import offer's answer (including skipping it), the dashboard handoff, and Uninstall -- both the removal and a confirmation someone backed out of. The past-sessions lane reports a verdict for each of its exit paths, as the folder lane already did.
- Durable background imports report their own outcome -- finished, failed, interrupted, stalled waiting for a connection, or a worker that vanished -- under the session that approved them, replayed off the job record. Until now nothing said whether an approved import ever completed. Metadata only; telemetry can be absent from a worker's pinned source tree without affecting the import.
- Show live import progress on the last onboarding page, with Open dashboard, Return to main menu, and Exit last. Each action preserves background imports.
- Explain folder-start failures and offer Retry, Skip, and Back before advancing onboarding.
- Link W&B projects from saved folder import status and details, including automatic imports, without scanning or uploading the folder again.

## 0.158.8

- Cancel an individual session or folder import from its detail menu. Other imports keep running; canceled jobs retain their progress for an explicit resume and leave the active progress bars. Uploads already submitted may still finish.
- Exit, Escape, and Ctrl-C leave background imports and their history intact. Exit closes the wizard; use Cancel import in the details to stop a selected job.

## 0.158.7

- Back returns to the previous import form or review with selections preserved. Revisiting optional imports never repeats installation or automatically starts the same import again; unchanged reviews reuse their scan. Import selection labels its Main menu boundary, and already-started import status labels Escape as Continue.
## 0.158.6

- Folder-picker actions use `b` for Back, `s` for Skip, and `i` for Import, with directional arrows only on Back and Import. These shortcuts do not activate while editing the path with Ctrl+L.
- Completed imports leave the progress overview after 24 hours. Repeating a completed folder import starts a fresh job and rechecks current files without duplicating completed uploads.
- Explicit Exit, sign out, and uninstall stop workers and clear local import jobs. The final **Onboarding complete** page opens the dashboard with Enter or → while approved imports continue in the background.
## 0.158.5

- Multi-agent installation reports completion only after all selected agents finish, so browser onboarding waits until the **Import research work** step. Import status pages now label their continuation **Continue setup (import continues)**.
## 0.158.4

## 0.158.3

- Session-import retries recognize hash-verified history already finalized in Probe, including when live capture advanced beyond an older approved copy. They preserve later pending capture and never upload beyond the approved history.
- Raw transcript snapshots no longer share the 100 MiB sanitized upload-queue limit. Large sessions use available disk space with a safety reserve; completed copies are reclaimed, incomplete snapshots and pending uploads are retained, and retries do not recreate completed snapshots. Missing incomplete copies retain their original history boundary. Tap 0.4.7 carries the same journal fixes.

## 0.158.2

## 0.158.1

- The main menu's **Import research work** opens the same session/folder multi-select as onboarding, with both selected by default. **Existing imports** remains the monitor. Its actions show details first and Return to main menu last, with no separate Exit action; press `c` on an import's details to copy its complete log path.
- Failed session imports now name each failed session, its stage and a safe error reason in the log, with counts and the first failure in job details. HTTP failures preserve their status and retryability even when the error body is malformed or times out; credentials and response bodies stay out of these diagnostics.

## 0.158.0

## 0.157.0

### Added

- `probe exec` can open the run itself (`--project`, `--experiment`, `--slug`,
  `--name`, `--description`, `--tag`, `--external-id`, `--config`), holds it with
  a heartbeat for as long as the child lives, hands the child `PROBE_RUN_ID` and
  `PROBE_RUN_EPOCH`, and closes the run from the child's real exit code. It
  detects submit-and-return launchers (`sbatch`, `ray`, `modal deploy`,
  `kubectl` — through `python -m`, `uv run` and `uvx` wrappers) and opens the run
  awaiting attach instead of pretending to own it; `--launcher` and
  `--detached-launcher` override the detection. The exact forwarding line for the
  launcher it sees is printed, because the id does not cross a machine boundary
  on its own.
- `probe.init()` reads `PROBE_RUN_ID` and joins that run instead of creating a
  new one. Passing `experiment=`, `project=`, `name=` or `slug=` alongside it
  raises rather than guessing which side wins. `Client.attach_run(...,
  attached=True, reopen_if_dead=True)` is the same path for callers driving the
  SDK directly: it reopens a run that was swept while a job was queued, joins one
  another rank already reopened, and refuses a `failed` or `canceled` run, or one
  that ended over 24h ago, so a stale id in a `.env` cannot resurrect last week's
  work.
- A run whose beats failed through an API outage long enough for the server to
  declare it crashed puts itself back on the first beat that lands again —
  same process, same writer epoch, never a newer attempt's run.

## 0.156.2

- A byte-limited import that ends with unfinished items shows Partially complete and its verified count, rather than a full completion bar.

- Import progress now has exactly two completion bars: Session imports and File imports. Queued jobs share their category, overlapping session snapshots are counted once, and failed or completed file imports stay visible. Remaining-time estimates use observed delivery progress; scanning and preparation do not fill the completion bars.

## 0.156.1

## 0.156.0

### Changed

- `find_papers` no longer inherits `search_knowledge`'s keyword-bag query
  guidance: it asks for a natural-language description naming the specific
  model, method, dataset or benchmark and the relation being asked about. The
  two tools run on different engines — an exact channel that matches names
  literally, and a dense abstract ranker — and a measured A/B put the bag last
  on every question-shaped paper search. `_PAPER_QUERY_DOC` is shared and
  parity-pinned like its siblings; the filter guidance is unchanged.
- Folder imports default to scanning and plan review in the installer. An
  explicit Scan and import in background option automatically approves the
  proposal and runs scanning and delivery as one durable job. Progress
  distinguishes analysis phases and completed steps. Two independent survey slices can run concurrently, and
  completed reads are cached for retry. Approved background imports continue to
  share regular backfill's coverage and delivery receipts.
- Import status shows colored live progress above the continue/wait and return
  guidance. Session and folder pickers include Skip actions, and the folder
  picker provides Ctrl+B Back, Ctrl+N Import, and Ctrl+S Skip shortcuts.
- The import monitor shows session bars above folder backfill and keeps verbose
  plans and queue messages behind Details. The main menu shows the same active
  progress bars directly below the device status, updating without a keypress.
- Picker sections have more space when the terminal allows it. Session choices
  adapt on resize while keeping their selection borders and navigation visible.

## 0.155.4

### Changed

- Every `find_papers` filter — `categories`, `authors`, `published_from`,
  `published_to` — and `search_knowledge`'s `search_in` now state that they are
  ANDed and strictly narrow, from one shared string. The bag guidance added in
  0.155.2 applies to `query` alone; applied to the filters it is what produced
  empty searches. `categories` also drops the claim that a mixed set always
  returns nothing: it is an intersection with the QUERY, so the same set can
  return papers on one search and none on another.
- The generated client schema now carries `DELETE /v1/integrations/wandb/accounts/{connection_id}/record`,
  a route the server has had since the W&B mirror lane (#1506) that the last
  regeneration predated. It is recorded as pending in the parity ledger; no
  command reaches it yet.

### Removed

- Importing this machine's conversations no longer writes summaries locally with
  your own coding agent, and `--no-digest` is gone from `probe backfill` and
  `probe wizard`. The lane that received those summaries was retired on the
  server. Conversations still upload, and the import report still says what
  landed. `--agent` now offers claude and codex only; pi was listed for
  summaries and no lane accepts it.

## 0.155.3

### Fixed

- Installer pages with no explanatory content keep the heading, choices and
  navigation together instead of expanding an empty pane. Empty panes no longer
  capture keyboard focus or advertise reading shortcuts.
- After starting a session import, a live status page lets you continue setup
  immediately or stay to watch it finish, and reports the latest saved state.
- The folder picker explains what will be imported and reviewed. Enter opens
  folders, including the `../` parent row; Left and Right leave the folder list
  unchanged, Escape consistently leaves the picker, and Ctrl-C exits the installer.

## 0.155.2

## 0.155.1

### Fixed

- The install confirmation uses the same navigation row as the earlier step:
  Back on the left and a matching-width Install button on the right. The browser
  approval explanation now sits in the content above the buttons.
- Device checks and other between-screen waits show one centered, animated
  status message, keep it centered on resize, and hide the terminal cursor.

## 0.155.0

### Changed

- `find_papers` and `search_knowledge` now state the SAME thing about what a
  query should look like, from one shared string: a bag of keywords and
  identifiers, never a sentence. They used to contradict each other in writing
  — find_papers asked for a sentence — and the guidance moves from the tool
  docstring, which a client slices at 2,048 chars, onto the `query` argument,
  which is passed verbatim. `categories` now says it is an AND across a paper's
  cross-listings: name one, because a mixed set returns nothing.
- `track-work` now chooses the run surface by whether the process doing the
  work reports back, not by whether the agent happens to be editing the script.
  Work launched on another machine (Modal, Slurm, Ray, a container) routes to
  the SDK INSIDE that job; a run opened from the machine you launched FROM owns
  nothing and captures nothing. The CLI branch now states its cost — a detached
  run silent for 15 minutes is reaped to `untracked` — and points at
  `instrument-training-runs`, which it had never referenced.
- `instrument-training-runs` also triggers when a training or evaluation job is
  launched on another machine, and states that `probe-research` must be
  installed in the emitting process's own environment (the CLI install is
  isolated and not importable) — guidance lost when the skills were
  consolidated, and load-bearing now that agents are routed to instrument
  remote jobs.

## 0.154.4

### Fixed

- The main menu is one continuous page, with room for the complete menu before
  scrolling. It preserves top clearance and uses spare bottom space, tightening
  empty gaps on smaller screens. Separate content and options panes remain in
  installation steps; on short terminals, the main menu moves as a whole.

## 0.154.3

### Fixed

- Increased shared top and bottom padding to seven rows on tall terminals and
  eight on larger screens, leaving visible space below Warp's command bar.
  Compact terminal layouts keep their existing padding.

## 0.154.2

### Fixed

- Added shared top and bottom padding around the entire installer, including
  progress headers, the main menu, folder selection, sign-in, and live status.
  Padding adapts to terminal height, and long status output stays inside it.

## 0.154.1

### Fixed

- Installer sections now fit their content: short status summaries leave room
  for the menu, and actions and keyboard help follow the content without large
  empty panels. Folder lists also shrink to the number of available folders.
- Removed visible scrollbars and the terminal text cursor from menu selections
  and read-only content. Keyboard scrolling and cursors in editable fields remain.

## 0.154.0

### Changed

- Installer screens and the main menu share a thin progress bar, fixed content
  and action areas, spaced choices, and keyboard hints below the actions.
- Conversation imports detect available agent histories and let you choose any
  combination, with all detected sources selected by default.
- Reviewed conversation and data imports run in the background with durable
  checkpoints and automatic connection retries. Monitor both at the end of
  onboarding or reopen Existing imports from the main menu.
- Tap 0.4.6 shares the import journal's delivery status reporting.

## 0.153.0

## 0.152.0

## 0.151.2

### Changed

- Guided installation offers imports after setup finishes, with both import
  options selected by default and no introductory sentence above the choices.

## 0.151.1

## 0.151.0

### Changed

- Backfill reviews now use the wizard's shared menus, with scrollable plan details,
  explicit import and revision actions, and consistent keyboard navigation. Source
  recovery, changed files, W&B history, and conversation imports use the same controls.
  Agent and upload progress share one display, including on narrow terminals.

## 0.150.3

## 0.150.2

- The wizard's main menu adds one blank line between options within each section,
  keeping titles and descriptions together and preserving section spacing.

## 0.150.1

### Fixed

- Installing from the wizard reuses the device's saved authorization instead of
  opening another browser approval. The initial wizard sign-in prepares capture
  credentials with capture disabled until installation is confirmed. Missing or
  rejected credentials still require authorization.

## 0.150.0

## 0.149.0

## 0.148.2

### Changed

- Uninstall signs this device out. It used to remove the plugins and leave the
  account: both tokens stayed live on the server, the config kept its copy, and
  the next `probe wizard` skipped the browser entirely because a stored token
  reads as "already signed in" — so a machine somebody had deliberately removed
  Probe from was still signed into it, and could not be reinstalled under
  anyone else without a detour through `probe logout`. The removal now releases
  the API token and the read-only MCP token, drops the Codex MCP entry holding
  a copy of the latter, and clears the active account only — signing out of
  staging still leaves prod alone. It runs once for the device, after the last
  selected coding agent has published its emptied state, because that
  registration authenticates with the token being released. Declining at the
  confirmation signs nobody out.
- A removed device reads as fresh again, so reinstalling offers the defaults
  rather than "everything currently off". The killswitch marker the teardown
  writes is cleared once the plugin is gone and no credential resolves anywhere
  (an exported `PROBE_INGEST_TOKEN` included — where one survives, the marker
  stays), and the auto-update record is dropped rather than merely switched off.

## 0.148.1

### Changed

- Center the install progress bar horizontally and add space between the title and bar.

## 0.148.0

### Added

- The "update needed" tier says what the publisher wrote. Each
  `client-version.json` pair takes an optional `message` — the sentence shown
  when that component is urgent — because the tier is always the same shape and
  what a breaking change costs the reader is different every time. It appears in
  the session-start headline and in the `probe doctor` row, replacing the built-in
  "some features may not work correctly", which is now only the fallback. A
  message is read only for a component that is actually urgent, so one left in
  the manifest after a fix cannot resurface on a healthy install. Two urgent
  components with different messages get a line each; the same message on both is
  printed once.

### Changed

- The wizard shows only the timestamp and success/fail status for the last update attempt.
- Sign out appears immediately above Exit in the main menu.

## 0.147.2

### Changed

- Install progress uses a shorter bar with a solid fill, a lightly shaded remaining
  track, and more space before the page content.
- The session-start update notice is three short lines -- what is stale, the
  current→latest pairs, and the command -- instead of one paragraph the
  terminal soft-wrapped wherever the window ended.

### Added

- A third update tier, "update needed", driven by a new optional `recommended`
  version in each `client-version.json` pair. Below it the notice says *some
  features may not work correctly until you update* and carries the advisory
  and the restart note; at or above it, the routine one-liner. It is a version
  rather than a flag so a machine stops being warned the moment it passes the
  threshold, with no second publish, and so the advisory reaches the installs
  it actually describes -- ungated it went to everyone, including machines
  many releases past the breakage it warned about. A manifest without the
  field behaves exactly as before.

## 0.147.1

### Changed

- Sign out is the last option in the wizard's main menu, below Exit.

## 0.147.0

### Added

- Local project backfill can attach an existing W&B source and start historical
  run discovery after the destination is reviewed. It reuses the project's
  source identities and saves admission keys before requests, so restarts and
  lost responses do not create duplicate jobs or runs. History status remains
  separate from verified file delivery; metrics continue to be read from W&B.
- SDK methods for project W&B attachments, live-sync settings, and history jobs.

### Changed

- Browser sign-in routes new accounts through website onboarding before CLI setup.
  The final website step supplies a short-lived, single-use 10-character sign-in
  code that the CLI exchanges for saved credentials.
- `npx probe-research [CODE]` opens the main menu after sign-in;
  `npx probe-research install [CODE]` starts guided installation. The main menu
  offers sign out in place of switch account.

## 0.146.0

### Added

- Guided and regular installs show a progress bar above the content. Step counts
  omit skipped agent selection, and progress continues across all selected coding agents.

## 0.145.0

### Fixed

- Local backfill now verifies each reviewed file against durable delivery receipts,
  retains interrupted work, and reconciles earlier imports by downloaded bytes.
  References remain identified separately from stored file contents.
- Backfill agent helpers use the importer's own CLI package even when an older
  `probe` is installed elsewhere on PATH, preserving standalone attribution.
- Interrupted batches recover when their saved Claude conversation was never
  created. Other launch failures retain their original error alongside file
  coverage errors.
- Reconstruction drafts prioritize project overviews and current status, retain
  bounded sections from long documents, and distinguish upstream history from
  the local work. Invalid drafts retain their diagnostics and original output
  with one bounded repair attempt. Claude project reconstruction uses Sonnet
  to reconcile documents; transcript digests keep their existing model.
- Backfill refresh status follows the active Overview lane, preserves published
  draft identity on restart, and avoids retries based on an unrelated legacy queue.
  New optional refreshes require successful reviewed publication. Finished
  generation is labeled for separate review, independently of verified files.
- Historical transcripts import as standalone sessions, with native session
  identity checks and immutable retries shared with tap 0.4.5. Backfill creates
  no transcript-to-project or other entity associations.
- Tap 0.4.5 respects each source folder's capture settings during shared recovery
  and recovers completed prefixes after a daemon crash. It requires a server
  supporting protocol-2 receipts before staging new batches.

### Added

- Detect local GitHub repositories and use authorized history from the existing
  integration during backfill; reuse or attach Code sources after placement review.
- Save file/Git reconstruction drafts for review, append approved drafts without
  overwriting Overview prose, and report optional AI generation separately.
- `write-overview` skill: write the first version of a project's or
  experiment's Overview page through the agent door (`probe overview write`)
  -- an artifact someone else reads to understand what is going on, opening
  with the explanation (what this is, why, what was done, what came of it,
  where it stands) under the same contract the dashboard's own lane obeys and
  keeps current afterwards. Registered in both plugin mirrors and the pi
  manifest.

## 0.144.0

### Added

- `probe overview write <file> --project|--experiment <ref> --blurb "..." [--plan] [--series]`:
  write the first version of a project's or experiment's overview page -- the
  self-contained HTML page the dashboard shows as that entity's summary on
  teams admitted to the overview lane. The server applies the same checks to an
  agent's page as to its own and answers 422 naming the fault; every later
  automatic refresh edits the agent's page rather than replacing it. SDK:
  `Client.write_overview(kind, entity_id, html=, blurb=, plan=, series=)`.

## 0.144.0

### Fixed

- **The team note stops rewriting itself.** On a machine running more than one
  coding agent, the note's history could flip between two byte-identical bodies
  every few minutes with nobody editing anything -- whole sections appearing and
  disappearing depending on which agent synced last. Each agent kept its own copy
  of the document (`~/.claude/`, `~/.codex/`, `~/.pi/agent/`) while sharing one
  record of what the server had, so each one read the others' copy as unsent work
  and pushed a whole document over it. There is now ONE document per machine, at
  `~/.local/state/probe/team-note/probe-team-note.md` (under `$XDG_STATE_HOME` if
  you set one), and every agent on the machine edits that same file. The managed
  block in `CLAUDE.md` / `AGENTS.md` re-points itself on the first sync after
  upgrade.
- **A teammate's paragraph can no longer be deleted by your next sync.** When the
  server merged your edit with someone else's while you kept typing, the merged
  result never reached your file -- and your following sync sent your copy, which
  the server accepted as a deletion of their text. The sync now merges locally
  (the same three-way merge the server runs) and rebases in that case, so both
  sides survive.
- **A sync that could not send no longer leaves you reading a stale note.** A
  file with unsent edits used to refuse to refresh at all. It now merges the
  server's version in, with conflict markers only where the two genuinely
  disagree.

### Changed

- Documents that are not yours to send are PARKED, never deleted and never
  uploaded: a leftover per-agent copy from the old layout, and a copy written
  under a different login. They are renamed to `probe-team-note.md.unsynced-<when>`
  beside where they were found, listed by `probe doctor` with whose they are, and
  named at session start. Your own parked copy is picked back up automatically the
  next time you are signed in as that user, and syncs from there.
- `probe notes sync` reconciles in git's order: fetch, merge locally, push what
  came out.

### Fixed (found in review)

- A local merge that could not be written no longer advances the record of what
  the server holds. It did, which meant the next sync sent the file as if it
  already contained the server's text and the server accepted the difference as
  a deletion.
- The sync lock is keyed on the document, not on the login. One document per
  machine behind a per-login lock meant two logins' sessions could interleave a
  read, a park and a write over the same file.
- The check for "is this file mine" is re-taken at the moment of writing, not
  only before the network call it is separated from.
- Parked copies no longer accumulate: an identical copy is not parked twice, and
  a session picks its own work back up instead of parking the other login's
  copy on every sync forever.
- The conflict file the server hands back is written private (0600) like every
  other write of the note.
- `probe doctor` no longer calls an unstamped copy another team's; it says the
  owner is unknown. The session-start notice lists the newest copies rather than
  the oldest, and never names one written under a different login.

### For contributors

- `merge3` now lives in the agent package (`probe.sdk.merge3`) so the client can
  run the same algorithm as the server; `app/team_notes/merge.py` is a
  byte-identical copy until the backend's pinned `probe-research` moves, guarded
  by `tests/unit/test_merge3_parity.py`.
- The document path is resolved in four places that cannot import each other
  (CLI, two plugin hooks, the pi extension). They are pinned to one another by
  `agent/tests/fixtures/team-note-document-path.json`.
- A base copy is now fetched with `GET /v1/team-note` whenever it will be merged
  against, rather than taken from the brief: the brief is `body.strip()`, and a
  base off by one trailing newline turned two non-overlapping edits into a
  conflict.

## 0.143.0

### Changed

- Remove `search_web` and `read_page` from the shipped MCP tool catalog. Research
  paper search, reading, and related-paper expansion remain available through
  `find_papers`. General browsing uses the host agent's web tools; the backend,
  assistant, and Python SDK retain their web APIs.

## 0.142.0

### Changed

- **Per-file code capture is now the default.** `run.snapshot()` / `probe
  snapshot` store every file git cannot supply as its own artifact row (the
  opt-in `PROBE_CODE_STORAGE=artifacts` path shipped in 0.140.0), so a run's
  code shows in the artifact explorer and unchanged files are stored once
  across runs. Set `PROBE_CODE_STORAGE=archive` to keep the one-`code-bytes`
  archive-per-run behaviour; a server without the batch doors still gets the
  archive automatically, with one warning.

## 0.141.1

### Fixed

- **Complete run artifact inventories across backend pages.** SDK run-artifact
  reads now follow server-advertised pagination, so SDK listings and restore
  inventories retain files beyond the first 1,000 artifacts. Legacy backends
  without pagination support keep their existing unpaginated read behavior.

## 0.141.0

### Added

- **Bounded MCP reads with complete continuation.** All eight read tools share
  one response-wide budget, including batches, errors, verbose output, and full
  documents: 2,000 reference tokens by default, configurable from 512 to 8,000,
  with a UTF-8 byte ceiling of eight times the token budget. The frozen tokenizer
  ships in the package for offline use, and responses use one compact text channel.
  Cards retain useful facts and authored caveats; `entity(view="record")` exposes
  full source records and selected fields. Large documents and browse trees remain
  reachable through opaque cursors, with explicit restart errors when source content
  changes. Browse defaults to `limit=10` per source list. Ordinary SDK/CLI read
  responses remain unchanged. See [the MCP reference](https://pypi.org/project/probe-research/0.141.0/)
  for fragment reconstruction, document consistency, and field-selection examples.

### Fixed

- **A first-time install left no `probe` on the machine.** `npx probe-research
  install` runs the CLI through `uv tool run`, which unpacks it into uv's cache
  and puts that cache's `bin` on PATH -- so the bootstrap's `shutil.which
  ("probe")` found a `probe` on a machine with nothing installed: THIS process.
  The version matched, the check concluded a real install was already present,
  and the persistent install it exists to perform was skipped silently. The
  wizard then reported success while leaving nothing behind, and every
  instruction that follows (`probe doctor`, the plugin's SessionStart hook
  resolving `PROBE_BIN`, the MCP headers helper) had no binary to run.
  Measured on a fresh container: `which("probe")` returned
  `~/.cache/uv/archive-v0/<hash>/bin/probe`, `~/.local/share/uv/tools` was
  empty afterwards, and nothing was printed about it.

  A launcher's cache is now recognised and skipped, so a from-zero run finds
  nothing and installs for real. Tested on the LOCATION rather than `realpath`
  or `sys.prefix`, because both look identical for a healthy install --
  `~/.local/bin/probe` is a symlink into the uv tools directory, which is also
  that process's own prefix -- and judging by either would reinstall on every
  run. Only from-zero was affected: a machine that already had `probe` answered
  correctly, which is why no laptop and no test showed it. The bootstrap tests
  all stubbed the predicate and asserted what happened GIVEN its answer, so the
  one broken part was never executed; it is now driven directly.

## 0.140.0

### Fixed

- **An unnamed run is named by the server again, instead of a fabricated
  timestamp.** `probe run start` without `--name` had the SDK invent
  `run-<YYYYMMDD-HHMMSS>` and send it. The server reads any supplied name as
  human-chosen, so every such run was stamped `name_customized` and frozen out of
  generated titles permanently — measured fleet-wide, that was 620 of 620 runs,
  meaning the title generator had never once fired since it shipped. The SDK now
  omits the field, which is what lets the server name the run after your `--slug`
  (or a petname when you gave none) and leave the flag False so a generated title
  may replace it later. Passing `--name` still marks the run yours and is still
  never overwritten.

### Added

- **Per-file code capture, opt-in: `PROBE_CODE_STORAGE=artifacts`.** A run's
  captured code is stored as one artifact row per file instead of one
  `code-bytes` archive: every captured file shows in the run's artifact
  explorer, and a file that did not change between runs, users or artifact
  kinds is uploaded and stored once. Files go up in windows of 256 (presign,
  16 parallel checksum-pinned PUTs, confirm, one retry with fresh URLs); every
  byte is verified against the manifest on the open descriptor before it
  leaves the machine, so a file that drifted since the manifest is named as
  unstored, never sent. The snapshot records `storage`, `n_uploaded`,
  `n_deduped` and every unstored path with its reason; `--max-upload-mb`
  applies exactly as it does to the archive. A server without the batch doors
  (a lagging deploy) gets the archive as before, with one warning. The default
  stays `archive` this release.
- **`probe artifact tree RUN [--prefix P] [--limit N]`**: one folder level of a
  run's artifacts, with `truncated` when a level was cut -- what the explorer
  fetches on expand.
- **`probe snapshot-restore` and `probe snapshot-show` understand capture
  rows.** Restore takes each file from the run's complete capture rows first
  and the `code-bytes` archive second, fetching lazily in batches of 256 so a
  4,600-file tree costs one batch of memory at a time, and names the server's
  reason (`download refused: ...`, `download failed`) per file it could not
  get. `snapshot-show` labels files stored as rows `captured` and summarises
  `N stored as files` / `N as files, M in code-bytes`.

### Fixed

- **A code snapshot no longer claims files its archive does not hold.** The
  `code-bytes` archive is the copy of every file git cannot supply, and its
  record said how many files it held before now the count came from the plan,
  not the archive: a file that vanished or changed between the manifest walk
  and the archive pass was skipped in silence and still counted. One run
  recorded 7,689 files and held 316. Every member is now verified as it enters
  the archive (hashed before and while it streams, size checked on the open
  descriptor); what no longer matches is left out **and named** -- in
  `n_pending_upload`, on the artifact's `drifted` list, in the capture-time
  warning and in `probe snapshot`'s report. A file that grew keeps its recorded
  prefix; one that shrank or changed under the stream is rebuilt once, then
  reported. `probe snapshot-show` reads the archive's own list, so it stops
  reporting every file as stored the moment any archive exists, and a storage
  failure at capture time no longer reads as a complete capture.
- **Captures and restores are hardened against a moving or hostile tree.**
  Recorded files are opened `O_NOFOLLOW|O_NONBLOCK` and re-checked on the
  descriptor, so a symlink or FIFO swapped in mid-capture is refused, never
  followed or blocked on. Git path listings are NUL-delimited: a name with a
  quote, a backslash or an accent is captured instead of quoted out of the
  manifest, a directory named `x<U+2028>..` can no longer become a `../` path
  out of the tree, and a non-UTF-8 name is listed under `manifest.skipped` with
  a reason. `probe snapshot-restore` validates the manifest before touching
  disk, refuses any path outside the destination, never writes through a
  symlinked directory, and bounds each member read to its recorded size.
- **Archives build 2.5x faster** (gzip level 6 instead of 9, measured on a
  7,902-file tree; 0.3% larger output).

## 0.139.0

### Fixed

- **Signing in now re-points Codex at the read token it just minted.** Claude
  Code builds its `Authorization` header at connect time and always reads the
  current token; Codex has no credential-helper hook, so its token is a static
  copy in `~/.codex/config.toml`. Signing in wrote a new token and RELEASED the
  old one, and nothing updated that copy -- so the credential Codex kept was
  not stale, it was dead, and every Codex call 401'd. The repair existed but was
  wired only into `probe mcp token set`, which is not the command anyone runs.
  It now runs wherever credentials are minted, so the wizard's sign-in and the
  guided install both fix it. Still a repair and never an install: a machine
  with no Codex entry is left exactly as it was.
- **`probe doctor` can now see that drift.** The check sat behind "is Codex the
  selected agent", and a machine with both agents installed reports only one --
  so on exactly the machines most likely to drift, the one local signal was
  unreachable and doctor printed `CLI + MCP: ok` over a dead token. Codex's own
  `mcp list` is no substitute: it reports `bearer_token` for any header at all,
  valid or not.
- **And `probe wizard` can now fix what doctor reports.** Doctor's remedy named
  the wizard while every gate on that screen closed in exactly the state being
  reported: a signed-in device never re-enters the authorization path (`mcp` is
  folded into "already held" once `api` is), and the Codex step waits on an
  "unauthenticated" answer that a dead header never gives. The repair is now
  scheduled on its own evidence -- the token Codex holds versus the token this
  device holds -- so the command doctor points at is the command that fixes it.
- **Signing out no longer leaves a live read token on the machine.** Only the
  API token was released; `mcp_token` was wiped locally while staying VALID
  server-side, and Codex kept its own copy in `~/.codex/config.toml`. A machine
  someone had signed out of therefore kept working read access to the team's
  research. Sign-out now releases the read token the way it always released the
  API one, and drops the Codex entry holding it.

### Security

- **The read token is no longer written across deployments.** The sync pairs the
  token this device holds with a URL read from the installed plugin's manifest,
  and nothing checked that the two described the same deployment -- so a machine
  signed in to one Probe with a Codex entry pointing at another would hand a
  live read credential to the wrong server on every connect. Now checked, and
  the write is skipped with a message when they disagree. The check is pinned to
  the shipped defaults rather than to matching hostnames, because production
  deliberately splits the API and the MCP across `api.` and `mcp.`.

## 0.138.0

### Added

- **The wizard now reports whether session capture is on.** The capability
  snapshot carried auto-update and the tracking plugin and nothing else, so the
  setting the consent story rests on was invisible to the server -- nobody could
  tell whether a machine turned session tracking on at install, off later, or
  back on. Schema v2 adds `capture`, and reports what actually RUNS
  (`capture_on`): a killswitched plugin, or one with no credential, ships
  nothing and is reported `absent` rather than `installed`.
- **`mcp` and `skills` collapse into one `tracking` field.** They were always one
  fact wearing two names -- both were written from a single boolean and could
  never disagree. Older clients keep sending v1 and the server keeps accepting
  it; their silence about capture reads as `unknown`, never `absent`.

## 0.137.0

### Changed

- **The settings screen's tracking row now says what turning it OFF actually
  does.** It read "Applies to sessions on creation" whether the box was ticked
  or not, leaving anyone emptying it to guess which half of Probe stopped --
  and the guess that costs them is the one where they believe reads stopped
  too. Ticked, it says sessions are tracked through your coding agent.
  Unticked, it says write commands will be blocked and reads through MCP still
  work, with a footnote pointing at the MCP server, since that is where reads
  are turned off and it is not this screen.

### Fixed

- **Turning capture off in the wizard now tells the server, not just your
  laptop.** `probe setup` cleared local credentials, stopped the daemon and
  removed the plugin, and sent nothing -- so the device stayed live in your
  dashboard's Devices list and its capture credential stayed valid on a token
  nobody held any more. It could not be repaired afterwards either: the teardown
  deleted the very `.token` a revoke authenticates with, so a later
  `python -m tap revoke` found nothing and skipped the server too. The revoke now
  runs FIRST, while that credential exists, and says it is an uninstall rather
  than a re-pair. Off is still a promise about your machine: an unreachable
  server does not block the teardown, it just warns that the device may linger.

- **A setup driven from a coding agent could not see the approval it was waiting
  for.** The wizard's piped-output path printed unflushed, and the browser
  approval URL and code are printed immediately before the run blocks on a human
  -- so the one instruction the caller had to act on was the one guaranteed to
  sit in the buffer. Measured: six seconds after the write, a piped reader had
  received nothing. `probe login`, `probe token` and `probe mcp` shared the bug
  through `_show_device_prompt`. Both flush now.
- **A headless install configured Claude Code whatever was on the machine.** A
  run with a terminal selected every coding agent it detected; a run without one
  was pinned to `claude_code`, so the same command on the same machine did
  different things depending on whether stdout was a pipe -- and a Codex user
  driving the installer from a tool call got a green report for an agent they do
  not use. Both paths now configure what is actually installed, and a headless
  run that chooses implicitly says which agents it chose. Scripts wanting one
  agent name it with `--agent`.
- **Bare `npx probe-research` with no terminal installed everything unasked.**
  The action menu and the confirmation screen -- which is where the session
  capture disclosure is drawn -- are both gated on an interactive terminal, and
  the action defaults to `configure`, so a bare launch from an agent's shell tool
  ran a complete install with capture enabled, showed nobody the disclosure, and
  reported success. It now exits 2 and changes nothing. `--yes`, `--action`,
  `--agent` and any capability flag are all stated intent and are unaffected, so
  `probe install` and auto-update's detached re-run keep working.

## 0.136.0

### Added

- **A struck claim stops costing context the moment it is struck.** The team
  note rendered into `CLAUDE.md` / `AGENTS.md` is now the COLLAPSED form:
  a `> **SUPERSEDED**` region keeps its marker line (that a claim fell, when
  and why) and drops the retracted text. The editable `probe-team-note.md`
  file keeps its verbatim bytes — edits round-trip against the file, never
  the block.
- **The team note now asks to be audited.** Session start reports when the
  note is overdue (`<!-- audited YYYY-MM-DD -->` stamp older than 7 days, or
  the rendered block measured at 80%+ of its instruction-file budget), and
  the new `notes-audit` skill runs the cleanup out of the user's way (a
  background subagent on Claude Code; inline-first on Codex, whose sandbox
  reaps detached processes): strike
  claims the evidence contradicts, remove only expired strikes and lapsed
  expiries, shrink only under size pressure. A 24-hour floor caps the cadence;
  every edit stays recoverable through note version history. Tune with
  `PROBE_NOTES_AUDIT_INTERVAL_DAYS` and `PROBE_NOTES_AUDIT_HORIZON_DAYS`
  (horizon `0` = strike-only, delete nothing).
- **Agents are told to keep notes true in the moment, not just append.**
  track-work now says: when what you are reading is contradicted by evidence
  in front of you, fix or strike it there and then; when the researcher says
  something is deprecated, strike it in the Probe note rather than only
  dropping it from your own context; prefer correcting a team-note line over
  adding one, and record shipped work as one line plus its PR number.

## 0.135.0

## 0.134.0

### Fixed

- **A laptop no longer becomes a new machine when it joins a new network.** The
  witness that decides whether this machine may claim its own device identity
  was `<hostname>:<$HOME>`, and a hostname is not a property of the machine --
  it follows the DHCP lease. Joining a captive WiFi renamed a MacBook to
  `visitor-10-59-125-182`, it stopped recognising its own device file, and it
  minted a second identity and a second row under Connected Clients. The witness
  is now a stable machine id (`/etc/machine-id` on Linux, the hardware UUID on
  macOS), so a rename is a rename. Existing machines are adopted on the old
  comparison and stamped with the new one, so nothing splits on upgrade; a
  machine that has ALREADY forked settles on one identity and stops
  accumulating, leaving the stale row to be retired by hand.
- **A machine is named what it is called, not what the network called it
  today.** `probe login` labelled the client from the transient hostname, so the
  dashboard showed `visitor-10-59-125-182` where a computer's name belongs.
  `scutil --get LocalHostName` and `/etc/hostname` are preferred; the transient
  name stays as the fallback.

## 0.133.0

### Added

- **New sessions can inherit their tracking default from a folder.** Use
  `probe session default on|off --folder PATH` to set an override,
  `probe session default --folder PATH` to inspect its effective inherited
  value and source, or `inherit` to remove the exact folder's override. Existing
  sessions keep their already-seeded tracking state. Repository folder configs
  must be regular files no larger than 64 KiB; unsafe entries are ignored during
  inheritance and refused unchanged by the setter. Pi now seeds that default on
  `session_start`, shows it in a persistent footer, and refreshes the footer after
  an interactive tracking switch.

## 0.132.0

## 0.131.1

## 0.131.0

### Fixed

- **The wizard stops offering to install things while it removes them.** On a
  machine with more than one coding agent, every agent-scoped action opens the
  same picker first -- and it was written as if only Install could reach it. So
  picking **Uninstall Probe** opened a screen headed `Install Probe — step 1 of
  2`, asking "Which coding agents should Probe connect?" over rows reading
  "Install plugins and pair source-bound capture.", one keypress before the only
  destructive action in the product. The picker now says what the action it was
  opened for actually does -- uninstall, update, diagnose and the manual
  instructions each get their own title, lede and per-agent rows -- and only
  Install carries a step number, because only Install has a second step to
  count towards.

## 0.130.0

## 0.129.0

### Fixed

- **A search no longer hands you your own conversation.** An agent that searched
  the lab while it was working could get its own live transcript as the top
  result -- re-indexed seconds earlier, matching its own wording -- and read its
  own answer from minutes ago as evidence from the team. `search_knowledge` now
  takes `exclude_session`; most agents never need it, because the id arrives on
  a header. Pass it where the header cannot reach: under Codex, read
  `CODEX_THREAD_ID` from your shell.
- **One conversation counts once.** A transcript and the digest written from it
  are the same session rendered twice, and both were taking a result slot. The
  transcript wins, so the result is one you can actually open.
- **An answer emptied by your own exclusion says so.** New
  `all_results_were_own_session` marker: a stop, not a degradation, because
  rewording cannot produce a different corpus. And if the server is too old to
  apply the exclusion at all, `self_exclusion_unsupported` says that rather than
  quietly returning your own session anyway.
- **The managed `AGENTS.md` / `CLAUDE.md` block names no tool or skill.** Blocks
  written months ago name skills that no longer exist, and no release can reach
  the file to correct it -- it lives in your home directory. An agent handed a
  name it cannot invoke goes hunting through files for it instead. The block now
  describes the work and lets whatever you have installed introduce itself. Run
  `probe wizard` to regenerate (v27).
- **Turning tracking off stops writing, not reading.** The block said "this
  whole block is off" and then gave two examples that were both about writing,
  so agents read the search mandate as still standing. It now says which half
  the switch governs.
- **Codex identifies which conversation an MCP call belongs to.** The Codex MCP
  table gains the agent-session headers, so self-exclusion works there. Installs
  written before this update are migrated even when their token has not rotated.

## 0.128.0

### Fixed

- **An uploaded file keeps its extension.** `--name` replaces the file's
  basename, and an artifact's name IS its relative path server-side — so
  `probe artifact add spec.md --name "atomworks-study-README"` stored a file the
  dashboard could not identify and refused to preview, on 91% of one tenant's
  artifacts, all of a 25-file sample plain text. The extension is now restored
  from the path the bytes came from, at every door that names an upload
  (`artifact add` sync, async and `--from-manifest`, `shared add`,
  `Client.upload_file`, `Run.log_artifact`). Additive only: a name that already
  carries an extension is untouched, so `--name report.txt` still means that.

## 0.127.0

## 0.126.0

## 0.125.0

### Added

- **Transcript discovery finds relocated Claude Code and Codex directories.**
  `probe backfill` works down a ladder — explicit override, directories
  capture has actually seen transcripts in, `CLAUDE_CONFIG_DIR` / `CODEX_HOME`,
  then the default — walks every one that exists rather than only the first,
  and names the directories it searched when it finds nothing.
- **The import says how many sessions left no transcript**, cross-checked
  against Claude Code's prompt history and bounded to recent sessions so
  retention-deleted transcripts are not reported as never written.

### Changed

- **The track-work skill names the subproject READ.** `probe project list
  --parent` shipped with no prompt phrase anywhere, so no agent would have
  typed it -- the same fence-shape failure that left `--parent` unused for
  days. Added to the line that already teaches the write, not as a new bullet.

## 0.124.0

### Added

- **`probe project list --parent <project>`** lists a project's DIRECT
  subprojects. The CLI could build a tree (`create --parent`, `move --parent`,
  `delete --recursive`) and could see that a project *had* a parent
  (`project get` returns `parent_project_id`, `ancestors`, `subproject_count`),
  but had no way to ask "what is under this project?" — the read side of 0148
  was reachable from the dashboard and the MCP and not from the CLI. The SDK's
  `list_projects` gains a matching `parent_id`, guarded like the tags filter: a
  pre-0148 backend ignores `parent_id=` and returns every project in the tenant,
  so the client refuses that page rather than presenting it as one project's
  children.

## 0.123.0

### Fixed

- **A failed install no longer reports itself as finished.** The wizard
  registers what it set up, and the dashboard waits on that registration to
  decide an install completed — it is what advances the onboarding install step
  and what turns the browser approval page into "Installed". A guided install
  that FAILED registered too, on its way out, so a terminal that had just
  printed `Not finished — these plugins did not install` still moved onboarding
  to the next step and still told the approval page the install was done. An
  unfinished run now reports `unknown` for the plugin fields, and both surfaces
  wait for a settled answer.

  Deliberately not a check that the plugins are present: installing with
  tracking off is a real, successful outcome that leaves them `absent`, and
  waiting for `installed` would strand those users on a page that promised to
  move on by itself. The registration itself still happens on the failure path
  — it is also what adopts a legacy MCP credential.

## 0.122.0

### Changed

- **Agents are told that projects nest.** The always-loaded anchoring rule
  read `run -> experiment -> project -> your workspace`, stopping one level
  too high, and the registration trigger said "new line of work -> register a
  project" with no prompt to ask whether the work is a phase of something
  already registered. Measured cost: four Odyssey-3 phase projects were
  registered as flat top-level siblings by an agent whose CLI already had
  `--parent`, on the day the feature shipped -- `--parent` lived only in the
  track-work skill body, which is read after a skill is SELECTED, while the
  new-project-or-phase decision is made from the always-loaded block. The
  chain is now `project -> parent project -> your workspace`, registration
  names the subproject case explicitly, and the closed kind vocabulary is
  spelled out where the project is created. Both the `CLAUDE.md` / `AGENTS.md`
  block and the MCP instruction sheet compose these fragments, so neither can
  drift from the other. `POINTER_VERSION` 19.

## 0.121.0

### Changed

- **The wizard's steps continue on Enter.** `‹ Back` / `Next ›` was a printed
  label driven only by `←` / `→`; it is a row the cursor can hold now, and every
  step opens on it with `Next ›` boxed, so accepting a screen is one keystroke
  and reaching the options is a deliberate `↑`. `↓` crosses to `‹ Back` on the
  same line. Space and Enter both toggle an option; `←` / `→` and Escape are
  unchanged. The row under the cursor is drawn inside a rectangle — on the band
  that is what says which end Enter will fire.

## 0.120.0

### Fixed

- **The research-tracking block in `CLAUDE.md` / `AGENTS.md` now updates
  itself.** It is written once by `probe wizard` into a file no release can
  reach, and `agent-rules refresh` — whose docstring claimed the session-start
  hook called it — was never wired to any hook in any shipped plugin. So
  bumping `POINTER_VERSION` corrected nothing: every machine kept instructing
  its agent with whatever the wizard installed, and guidance added in one
  release was still missing from live sessions days later. The refresh now
  rides `notes sync`, which already fires on every `Stop`, already resolves
  every configured harness, and already holds each instruction file's lock —
  so it lands with the CLI rather than needing a plugin release and a session
  restart. It refreshes an existing block only: a file without one opted out,
  and a background sync must not opt it back in. A block from a newer CLI is
  left alone so two installs cannot rewrite each other every turn, and a
  damaged block is now reported as the research-tracking block rather than
  mislabelled as the team-note one.
- **`agent-rules refresh` takes the same per-file lock as the note sync.** The
  two writers of one file were serialising against different locks, which is no
  lock at all for a whole-file read-modify-write.

### Added

- **`probe wizard` › `Import past coding sessions`.** The transcript import now
  has its own menu row under `Your research`, beside `Import existing work`.
  It was previously reachable only via `probe backfill --transcripts-only` or
  the offer shown once at the end of a guided install. Also
  `probe wizard --action transcripts`.

### Changed

- **The track-work skill's sub-note paragraph names every verb.** It advised
  renaming a duplicate title without naming `probe notes rename`, and never
  mentioned `--note id:<uuid>` — the escape hatch the ambiguity refusal points
  at, which is the one way out when the advised repair is itself refused as
  ambiguous. `delete` was missing too. Now covered in 12 words FEWER than
  before, by dropping dashboard detail a CLI writer cannot act on.
  `reference.md` gains the sub-note cap rule (a sub-note gets its carrier's
  cap as its own budget, 20 per entity), which the skill already pointed there
  for and it did not carry.

### Fixed

- **Importing a research folder no longer crashes on the classification step.**
  Anything past roughly 154 sampled files failed with "argument list too long",
  including on the chunked route that exists for folders too big for one
  prompt: the prompt was a command-line argument, and Linux caps one argument
  at 128KB while the prompt was sized against the model's context window. Both
  agents read it from a pipe now. Verified at 290KB.
- **Credential files are never imported.** `.env`, `.ssh/id_rsa`,
  `.aws/credentials`, `.netrc`, `.git-credentials` and private-key suffixes are
  refused by name and by suffix, and API keys are stripped from every file
  excerpt a model is shown. `<vendor>_api_key` values are recognised by key
  name, which is the only signal available: a Weights & Biases key is 40 hex
  characters, the same shape as an ordinary content hash.
- **A folder that gained one file and lost another is no longer reported as
  unchanged.** The already-imported check compared file counts and total bytes,
  so a net-zero change announced "nothing has changed on disk" and the new file
  was never imported.

### Added

- **Dotfiles are imported.** `.hydra/config.yaml` holds the config that names a
  Hydra experiment and was being dropped before anything could read it.
  Machine-written dot-directories are still skipped, now by name.
- **The wizard says what an import will cost before it starts**, in agent
  turns, counting both limits that bound a unit rather than only the file
  count.
- **Re-importing a folder imports only the difference.** The walk is
  remembered between runs, so an unchanged folder does nothing and a changed
  one reports what is new, what changed and what is gone. Nothing is retired
  for being absent — an unmounted drive is the likelier explanation.
- **When files go missing, the run writes the list.** It used to print a count.

### Changed

- **Checkpoints, shards and weights no longer cost an agent turn.** They carry
  nothing that identifies a project and their project was already resolved
  without a model, but every 400 of them still took one turn. A 200k-file
  checkpoint folder drops from around 500 turns to tens.

## 0.119.0

### Changed

- **The interactive install always confirms in the browser.** `probe install`
  (and the menu's Install row) now runs the browser approval on every
  interactive run — the full grant set, whatever the device already holds. The
  approval page shows which account the device is being set up for, warns when
  that would SWITCH it from a previous account, and offers "use a different
  account" right there. The token the new approval replaces is revoked after
  the mint, so re-runs stop leaving live credentials behind. Scripted paths
  (`--yes`, flags, non-TTY) keep the credential skip — CI has no browser.
- **The install closes by offering to import what already exists.** One
  checkbox screen after the apply: import this machine's past coding sessions
  (the transcript import lane — it shows what it found and asks before
  anything uploads, resumes if interrupted, and skips sessions capture
  already ships) and/or a folder of project work (the existing folder
  import). Skipping is a real answer, and `probe wizard` offers both again
  later.

### Fixed

- **A tracked capture gap older than the reconcile horizon uploads again.**
  The 48h window was gating files the tap already held a cursor for, so a
  machine that sat off for a week never recovered its gap. The horizon now
  bounds only what a sweep newly adopts — with one carve-out kept from the
  old behavior: a tracked path whose file changed identity (inode) beyond
  the window is skipped, not misread from the stale offset.
- **A re-mint also revokes the replaced read-only MCP token**, not just the
  API token, and a credential mint that cannot be saved to disk reports the
  failure (naming the cleanup) instead of dying in a traceback with a live
  unstored token orphaned server-side.

Self-hosted note: the browser-confirm install requires a backend with
device-authorization `client_context` (research-os ≥ 0.242.0.0) for the
account warning and completion handoff; older backends still work but show
the plain approval page. A two-agent install needs a backend new enough to
mint per-agent capture credentials (`capture_sources`).

## 0.118.0

### Added

- **Titled sub-notes, addressable from the CLI.** Every note-bearing entity
  can carry up to 20 titled sub-note documents beside its main note
  (research-os 0.231.0.0+). `probe notes list` shows an entity's sub-notes;
  `append`/`edit`/`show` take `--note "<title>"` to address one; `create`,
  `rename` and `delete` manage them. Title-addressed writes travel WITH the
  title and resolve on the server at apply time — exactly once or refused
  with the count — so they queue through the outbox like every other notes
  write. The MCP's `view="notes"` now surfaces each sub-note as a bounded
  excerpt, and the dashboard assistant can list and open them
  (`read_entity(kind=sub_note)`).

  Duplicate titles are legal (tabs key on id), so every `--note` also takes
  `id:<uuid>` — the escape hatch the ambiguity refusal names, since the
  advised repair (`probe notes rename`) would otherwise be refused by the
  same ambiguity. `create` is strict (a replayed create is a second tab, not
  a retry, so it never queues), and an interactive `delete` re-reads the
  sub-note after the prompt so a confirm given for one title cannot destroy
  a document renamed while the prompt sat open.

- **`probe backfill` imports the agent conversations already on this
  machine.** Claude Code transcripts and Codex rollouts, discovered
  device-wide, behind their own approval gate that names the session count,
  the byte total, the date range and how many will link to a project.
  Sanitized locally before upload; summarized by your own agent rather than
  queued behind a server model call; resumable per session, and it asks the
  server before re-uploading anything it might already hold. New flags:
  `--transcripts/--no-transcripts`, `--transcripts-only`,
  `--transcripts-budget-mb`.

### Changed

- The tap plugin's sanitizers and transcript mechanics are now canonical in
  `probe.tap_core` and vendored into the plugin by `make sync-tap-core`, so
  the importer and the live tap cannot drift into producing different wire
  shapes for the same session. Tap plugin 0.4.2.

## 0.117.0

## 0.116.0

### Changed

- **`probe install` is now a guided install, and the install is always
  complete.** `npx probe-research install` (and the menu's "Install Probe" row)
  walks straight through: pick the coding agents only when both are on the
  machine, review one screen naming everything the install turns on — the
  Session capture disclosure included — then one browser approval and the
  apply. The interactive capability picker and the separate auto-update step
  are gone: an interactive Install enables everything, a killswitched capture
  included, with the disclosure on screen before the confirming keystroke.
  Scripted paths are untouched — an omitted flag on a re-run still preserves,
  so `probe wizard --yes` in CI can never re-enable something you turned off,
  and `--no-capture` still declines capture headlessly.
- **Settings now owns the capability switches.** `probe wizard` › Settings
  gained a "What Probe does here" group — CLI + MCP, Session capture, the
  global rules, automatic updates — with the boxes reading what the device
  does across both coding agents (a mixed state names the split per agent).
  Turning a capability on runs the same authorization and verification as an
  install; turning capture off goes through the same verified shutdown it
  always had. Per-agent control stays on the flags
  (`probe wizard --agent codex --no-capture`).

### Fixed

- **The team-note block is now re-rendered at session end**, so an edit reaches
  the next session rather than the one after it. `team-note-sync.sh` is
  registered on Stop and SessionEnd and ran `--push-only` on both; the render
  only runs on text it just fetched, and `--push-only` fetches nothing, so the
  render never fired from either hook. Stop stays `--push-only` because it fires
  every turn and must not carry a network pull.
- Both hook registrations state their mode explicitly. `PROBE_HOOK_EVENT` is
  inherited, so marking only SessionEnd would have let an ambient `sessionend`
  put a network pull and an instruction-file rewrite on every turn.

## 0.115.0

### Added

- **A project's attached GitHub repositories are now first-class from the
  agent.** New SDK methods attach/detach/confirm code sources, page a project's
  commit timeline (`list_project_commits`), open one commit
  (`get_project_commit`), and read a run's resolved code (`get_run_code`); the
  matching CLI lives under `probe project code` (`attach` / `list` / `detach` /
  `confirm`) plus `probe commits <project>` for the timeline. Attribution is
  server-stamped, so an agent attach is recorded as an agent action.
- **`get_entity(project, view="code")`** (MCP) reads the commit timeline of a
  project's attached repo, with `filters={"commit": sha}` to open one commit and
  `filters={"source": id}` to pick a repo when several are attached; the project
  card gains a `code` block naming the connected repositories.
- **The wizard import hands the agent a `git-history.md` digest** so it can order
  the work it is reconstructing in time — and read a captured run's base commit
  as what the run was *based on* (the nearest pushed commit), never as the exact
  tree it ran.

## 0.114.1

### Fixed

- **The instruction-file lock is keyed on the file, not the credential.**
  `Paths.lock` is keyed on origin+identity, which is right for the sync's
  per-credential base copy and wrong for `~/.claude/CLAUDE.md`: that path is the
  same whatever credential resolves, so two contexts on one machine took two
  different locks and wrote the same file.
- **The 32 KiB budget is measured in bytes.** `project_doc_max_bytes` is a byte
  budget and the team note is full of em-dashes and middots, so measuring
  `len(str)` under-counted every non-ASCII character -- in the direction that
  overruns the cap.
- **A pointer block upgrades back to the full note when space frees up.** The
  rendered form is now part of the block's identity, so a pointer no longer
  reports itself current against the full note's hash.
- **An undecodable instruction file is recorded, not raised.**
  `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so one latin-1 byte
  in a researcher's own `CLAUDE.md` escaped the handler and left the other
  harness unrendered.
- **A status file that is valid JSON but not an object fails open.** A bare list
  made `.get` raise `AttributeError` in the one path whose job is to fail open.
- The "already current" check moved inside the lock; it was a time-of-check race
  against another process replacing the block.

## 0.114.0

### Changed

- **The team note is rendered into `CLAUDE.md` and `AGENTS.md`, not injected by
  the session-start hook.** The hook declared a 9,000-CHARACTER
  `additionalContextLimit` while the backend built a brief up to 32,000; 32k
  characters is ~8k tokens and 9,000 reads as tokens-plus-headroom, but the field
  counts characters. The backend answered `truncated: false` — correct by its own
  budget — and the harness spilled the injection to a temp file and showed the
  model a 2 KB preview. `probe notes sync` now renders the note into a managed
  block in the instruction file, which the harness reads whole and before any
  hook runs.
- **The note block has its own marker pair.** `probe-research:begin/end` already
  delimits the operational pointer block; rendering the note there would delete
  it. `BlockSpec` lets both live in one file with separate lifecycles.
- **One sync renders both harnesses.** Each harness's local copy previously
  refreshed only when that harness ran, so the two drifted apart with neither
  agent able to tell.
- **`probe wizard` seeds the block at install**, which is what covers a machine
  that has never synced.
- The hook no longer carries the note. It reports when a render failed — the one
  thing a background job cannot say for itself.

### Fixed

- A stray `@path` in the team note no longer becomes a live import once the note
  is inside `CLAUDE.md`. Email addresses, @mentions and backticked text are left
  alone.
- A note containing our own markers is refused rather than mangled, and a damaged
  or unwritable block is left untouched rather than auto-repaired.
- Over budget writes a pointer to the real file instead of a truncated note:
  Codex's 32 KiB `project_doc_max_bytes` covers its whole instruction chain and
  it stops adding files at the cap.
- `skills/track-work/SKILL.md` was left behind by #865, which edited only the
  plugin copy.

## 0.113.0

### Changed

- **`probe_procedures` is advertised only to callers the workflow-memory flag
  allows.** The hosted MCP now computes its tool list PER REQUEST rather than
  once at startup: `/v1/me` reports the per-user verdict as
  `features.procedures`, and a caller the flag denies is not shown the tool.
  Tools are registered once per process and this server is multi-tenant and
  `stateless_http`, so registration-time gating was not available; the filter
  reads the request's own token through the contextvar `with_auth_and_health`
  already sets, and the identity it looks up is the `/v1/me` body the server was
  fetching and caching anyway — so the gate costs no extra round trip and the
  MCP needs no PostHog client. Fail-closed on every unknown, including an API
  too old to publish `features`: the skew window hides the tool rather than
  revealing it. STDIO IS UNGATED — there the server is a child of one person's
  agent on their own machine and the API still enforces the flag on every call.
- **Two passages naming the rule store were removed from `MCP_INSTRUCTIONS`,
  and one from the always-loaded agent block** (`POINTER_VERSION` 17 -> 18, so
  installed blocks refresh). Those strings ship to every tenant and cannot vary
  per user, so they could not keep instructing agents to call a tool most of
  them no longer have. Internal users keep the tool and its full docstring.
- **`set-rule` and `pull-rules` are no longer shipped in the plugin.** Plugin
  content is not per-user either, and two skill descriptions in every customer's
  context buy them nothing the flag will let them use. The canonical copies stay
  under `skills/`; `tests/test_skills_sync.py` asserts they stay unshipped so
  `make sync-plugin-skills` cannot quietly re-add them.
- **`probe rule` maps a gated 404 onto the "not available here" path** instead
  of a traceback, reusing the shape `_rule_unavailable` already understood. The
  command group stays registered on purpose: hiding it would take a network
  round trip at CLI startup, and this CLI runs inside training loops.

### Added

- **A session start with dead letters tells the agent to repair them.** An
  async write that dead-letters does so after the session that made it has
  moved on, and until now the failed op sat in `failed/` until a human
  happened to run `probe outbox status` — observed in production: a note
  append dead-lettered on 19 Aug was found by hand days later. Now the
  plugin's session-start hook counts the outbox's `failed/` (resolved exactly
  as the CLI resolves the journal, rank suffix included) and, when it is
  non-empty, injects a repair prompt into the session's context: read
  `status --verbose` first — and leave a paused or auth-blocked outbox alone;
  requeue a transient failure per-op; re-home a deterministic rejection's
  payload VERBATIM to an artifact plus a pointer note, then discard — never
  trim; and hand an op targeting another researcher's work to the user. An
  untracked session gets a report-only variant: told what is stuck, directed
  to a read and a report, never to Probe writes.

  Deliberately PROMPT-ONLY — adversarial review rejected the draft that also
  auto-ran `outbox retry; outbox drain` from the maintenance spawn: `retry`
  clears the auth block (designed as a human's assertion that credentials
  were fixed) and re-queues dead letters at the FIFO head where one
  unroutable op blocks fresh work; `drain` holds the drain lock across
  network I/O with none of `maybe_spawn`'s guards; both ignore
  `probe outbox pause`. Queued-but-undelivered ops need none of this: every
  CLI invocation — including the maintenance spawn's own — already kicks the
  guarded background drainer. Complements 0.112.0's removal of the SDK notes
  door: that closed the largest source of new dead letters; this gives the
  ones that still occur an agent's attention within one session.

## 0.112.0

### Removed

- **Notes are no longer writable through the SDK.** `Client.append_notes`,
  `edit_notes`, `append_project_notes` and `set_project_notes` are gone from the
  public surface. **Breaking** for any caller using them; `probe notes append` /
  `probe notes edit` are the supported writers and always were the intended ones.

  The door could not deliver on its own contract. `Client.write` enqueues under
  the SDK's async DEFAULT and returns None before any request, so `raise_permanent`
  never fired: an over-cap append was queued and dead-lettered rather than refused
  — the exact silent-success failure 0.105.2 was released to end, reachable again
  through the other door. The 0.111.0 warning could not fire there either, for the
  same reason. Warning from behind a door that swallows writes is worse than
  closing it, and `append_project_notes` had already been forcing `sync=True` for
  years to work around the same thing.

  Reads are untouched: `get_project_notes`, `list_notes`, and `notes` on every
  entity read. Knowing what a note says was never the problem. `notes=` on
  create/update calls is also untouched — that is entity metadata at creation,
  not the notes-writing workflow.

### Changed

- **The advice starts at 60% full, not 80%.** The warning is the ONLY place the
  per-carrier remedy appears on a succeeding write, so it has to start early
  enough to leave room to act in: 60% of a 4,000-character run note leaves 1,600
  characters — several more appends of runway rather than one.

- **A document AT its cap now says it is closed, not that it is 99% full.** Two
  changes, because the state is different in kind rather than degree:

  - the success path (an `edit` can land exactly on the cap) says
    `is FULL (4,000 of 4,000 characters). Nothing further will be stored here
    until it is compacted — <per-carrier remedy>`;
  - the REFUSAL path says it at all, which it did not before. The 422 arrives by
    exception, so it skipped the success-path advice entirely — the wall was the
    least informative state in the feature. `probe notes append` now prints the
    remedy under the error.

  The server's 422 also stopped naming only the limit: `appending would exceed
  the N-character notes limit; nothing further can be stored until this document
  is compacted`. A message naming only a limit reads like "send less", which is
  the one remedy that does not work — a caller that trims its paragraph and
  retries fails identically.

### Fixed

- **The "move this prose up" warning now names the ORDER, which is the part that
  can lose the prose.** A run, group or artifact note approaching its 4,000-character
  cap is told to move up to the experiment or project, and that is two writes
  against two entities with nothing making them atomic. Appending to the parent
  first means a failed second write leaves the prose DUPLICATED; the other order
  leaves it GONE. The warning named the action and not the order, so a reader
  following it had even odds of picking the destructive one — on a document
  already at its limit, which is exactly when there is no slack to recover. It
  now says "append there FIRST, then delete it here", and a test pins the
  ordering because it is one clause in a string and a later tightening would
  drop it without noticing what it carried.

  Document carriers (project, experiment) are unchanged and get no ordering
  caveat: compaction is one `edit` against one row, so the clause would be
  noise, and noise in a warning is what teaches a reader to skim the next one.
  The team note is a document carrier too but takes no `append`/`edit` verb at
  all — it is a synced file — so it never reaches this warning.

  The destination is "**a** project or experiment notes document", not "**the**
  experiment or project": an artifact has five anchors and two of them
  (workspace, shared folder) have neither, which the notes catalog has an
  explicit parentless branch for. `headroom_warning` is given only the carrier
  KIND, so it cannot name the right parent — the definite article was advice
  pointing at a row that need not exist.

  Both action clauses are now named constants pinned by EQUALITY in the tests.
  Two structural assertions were tried first and both passed on delete-first
  prose — `"FIRST" in m` with `m.index("append") < m.index("delete")`, and the
  regex `append.*FIRST.*then delete`, which matches "stop **append**ing here —
  FIRST … then delete it here, and append it to the project afterwards".
  Substring order is not operation order. The clause is prose whose wording IS
  the safety property, so a reword has to fail a test and be re-read.

### Added

- **The notes skills say that notes are capped, and name `probe notes status`.**
  The warning has fired from 80% full since 0.111.0 and the sweep has existed
  just as long, but no skill mentioned either, so nothing ever told an agent the
  command was there or what to do when the warning appeared. `track-work` now
  carries the two caps, that `notes append`/`edit` REFUSE an over-cap write
  rather than truncating it (while the batched ingest door clamps), that the
  refusal at the cap names the limit and nothing else so the warning is the
  thing to act on, that a shrinking `edit` is accepted AT the cap but must land
  under it when the document is already OVER, that the SDK's async default
  warns nothing, and the per-carrier action — compact in place for a document,
  move up (parent first) for a row annotation, and neither for a workspace or
  shared-folder artifact, which has no research parent.
### Added

- **The MCP can read the open web: `search_web`, `find_papers`, `read_page`.**
  A proxy onto `POST /v1/web/*`, which the backend has served from one Firecrawl
  key since the assistant got these three tools. The assistant could look
  something up; the coding agent actually doing the research could not, and the
  gap showed as the same failure every time — a direction proposed from memory,
  confident, with no citation and no idea whether the field had already reported
  the failure mode it was about to rediscover.

  The lab's own record and the literature are two halves of one question, and
  `search_knowledge` only ever held the first. `find_papers` searches 40M+
  abstracts across arXiv, PubMed, bioRxiv and medRxiv and reads what it finds —
  `mode="read"` with a query returns the PASSAGES answering it, which is how a
  claim gets checked against its source instead of paraphrased from an abstract.
  `search_web` is for documentation, error messages, and model or dataset cards;
  `read_page` opens one URL, because a search snippet is a fragment a search
  engine chose and never enough to quote from.

  NO NEW SECRET AND NO SECOND PROVIDER PATH. The MCP calls the backend with the
  caller's own token and the backend calls Firecrawl, so the narrowing that was
  already there — raw HTML, screenshots and link graphs dropped, page text
  capped, `recency` spelled server-side rather than passed through from a model
  — applies to this surface unchanged. The MCP pod holds no Firecrawl
  credential.

  The ways to get no results are told apart, which is most of the work here. A
  provider that ran the query and matched nothing answers
  `completeness.state="no_match"`; a door that did not answer at all answers
  `"partial"` with a `web_search` marker, the backend's own sentence in
  `data.reason` (the only thing separating "no key on this deployment" from
  "over quota, try later"), and NO `results` key — an outage must not be
  readable as an empty result set. A query the provider refused raises, because
  that one the caller can fix. A page cut at a ceiling is `partial`, and says
  which ceiling: `text_truncated` is the deployment's per-page cap,
  `truncated` is ours across the whole response.

  "The door did not answer" covers more than a status. A `TransportError`
  carries none at all, and it is the likeliest web failure there is — the SDK
  allows 30s for the round trip while the backend allows Firecrawl 25s of it,
  so a rollout, a reset connection, or an operator raising
  `FIRECRAWL_TIMEOUT_SECONDS` past ~29 all land there. A 404 on the two SEARCH
  routes is in scope too, because on those it can only mean this backend does
  not serve `/v1/web/*`; on `read_page` a 404 stays what it looks like, a dead
  link. An unrecognised `state` is never reported complete: a malformed body
  read as "searched fine, found nothing" is the exact wrong answer this family
  exists to prevent.

  TWO CEILINGS THE BACKEND CANNOT SEE, because both are properties of a
  response rather than of a page. Prose across all rows is trimmed to 100k
  characters, matching the `MAX_TOOL_RESULT_CHARS` the assistant enforces on
  the same routes — without it, `firecrawl_max_results` x
  `firecrawl_max_page_chars` is ~200k in one result, i.e. the MCP door twice as
  wide as the assistant's onto the same provider. And `data.reason` is capped
  at 300 characters: it is documented as the backend's sentence, but the SDK
  falls back to the whole response body on a non-JSON 5xx, so an ingress error
  page would otherwise ship its entire HTML into an agent's context.

  THE WEB TOOLS HOLD A BOUNDED SHARE OF THE WORKER POOL — a quarter by default,
  `PROBE_MCP_WEB_CAPACITY` to override. They are the first tools whose expected
  duration exceeds the pool's 20s shed timeout, so without a sub-quota one
  agent told "read these 16 links" takes the whole pool for half a minute and
  every other tenant's `get_entity` sheds with "server overloaded" instead of
  queueing. Admission has no per-tenant fairness; this bounds the blast radius
  until there is a measurement to size it properly.

  The three POSTs are deliberately NOT marked idempotent, unlike every other
  POST-for-read on this client. `transport._RETRYABLE` is {502, 503, 504},
  which is exactly what the router maps a provider failure onto — so the retry
  fired on precisely the wrong cases: a 503 IS "over quota, try later", and a
  502 or 504 can follow a Firecrawl call that already completed and was already
  billed. Connect errors still retry; those never reached the server.

  Every payload carries `provenance: "open-web"`. The assistant answers the
  untrusted-text problem behaviourally — a turn that has read the web loses
  unprompted writes — and this surface has no turn to spend, so it says so
  instead, in the tool descriptions and again on each result where a compacted
  session can still see it. `credits_used` rides the search response: an agent
  loop is where a browsing habit gets expensive, and this is the only place that
  cost is visible.

### Fixed

- **`/probe-research-setup` no longer installs the plugin before there is a
  credential to serve.** #165 closed this window for `probe wizard` and could not
  see the slash command, which kept installing the plugin two steps ahead of
  `probe mcp token set` — so the path a Claude Code user actually takes still had
  the bug the CLI path was fixed for. A fresh install finished, said "done", and
  sent the user to `/mcp` to authenticate a device the wizard had just authorized.

  The plugin ships an `.mcp.json` for the hosted MCP. Installed with no
  `mcp_token` stored, it connects with no `Authorization` header; the edge answers
  401 with a `WWW-Authenticate` challenge, and Claude Code discovers an
  authorization server from it and **pins the connection to OAuth**. Minting the
  token afterwards does not undo the pin, which is why "re-run the wizard" was
  never the repair. The plugin install is now its own step after the token exists,
  and it carries the manual repair for anyone already pinned (`/mcp` →
  `probe-research` → Clear authentication, then restart) plus the warning that an
  OAuth sign-in can have pinned a different account than the token that was pasted.

  Guarded by `tests/test_skills_sync.py`, on the order of the two commands inside
  the document's fenced blocks rather than on step numbers — so renumbering cannot
  satisfy it, and the prose stays free to name the command it is warning about. The
  CLI's equivalent assertion lives in `tests/test_setup_wizard.py` and cannot see
  this file, which is exactly how the two drifted apart.

## 0.111.0

### Added

- **`probe notes status`: how full every notes document in the team is.** The
  fullness sweep, and the reason the catalog row grew a length. Answering "which
  documents are close to refusing writes?" used to mean one API fetch per entity,
  which is why nobody asked — and why a project sat at 99,992 of 100,000
  characters, refusing every append, for a day. One page of `GET /v1/notes` now
  carries `chars` and `limit_chars` per row, so the sweep is one request.

  Deliberately TENANT-WIDE, with none of the `--project`/`--run`/`--artifact`
  flags the other `notes` verbs take: a single entity's headroom already rides on
  that entity's own read, so the question only this command can answer is the
  cross-entity one. Documents are listed fullest first, and a sweep that stops at
  its page bound says so rather than letting "nothing is near full" stand for
  pages it never opened.

- **`notes append` and `notes edit` warn as a document fills up.** At or above 80%
  of the cap, both print how full the document is and what to do about it. 80% is
  not a fresh number: it is the figure the notes-first-class design already
  settled on for the team note ("compact when `remaining_chars` drops below
  20,000"), reused so every carrier says "getting full" at the same point on the
  same scale. A FRACTION rather than an absolute, because the caps differ 25x and
  so does the right response — at 100,000 the answer is compaction, which needs
  runway to keep working while it happens; at 4,000 it is that the prose belongs
  on the experiment or project, which is a one-time move.

  The advice follows the CARRIER, not the size of its cap. Keying it on the cap
  would put the caps back in a client, which is what `notes_limit_chars` exists to
  remove.

### Changed

- **`Client.append_notes` and `Client.edit_notes` return the write response, and
  warn when the room is nearly gone.** Both returned `None`. They now hand back
  the PATCH response — `notes_remaining_chars` and `notes_limit_chars` — or `None`
  when the write was journaled rather than sent, which is the same `None`
  `Client.write` already returns and means "no server has seen this yet", never
  "the document is fine". The warning lives in the SDK rather than the CLI
  because the CLI is not the only writer: a training script appending in a loop is
  exactly the caller that fills a document without ever reading it back. It goes
  through `safe_warn`, so `filterwarnings("error")` cannot turn a note about
  headroom into the thing that ends a run. `warn=False` suppresses it for a caller
  that renders the same fact better — the CLI, which knows the project slug or run
  petname the SDK does not — and suppresses the message, never the check.

- **A backend too old to publish the fields produces NO warning**, rather than one
  computed against a cap the client made up. A fullness the server never asserted
  is the same class of confident wrong answer as "appended" for a write that was
  refused.

- **`client-version.json` floors the CLI at 0.105.2.** Below that, a notes write
  the server REFUSED was reported as written: the fail-open path swallowed the
  422, the CLI printed "appended to <kind> notes" and exited 0, and the paragraph
  went to the outbox to be dead-lettered. No server change reaches such an
  install — the swallowing happens on the client — so the manifest floor is the
  only lever, and `min` is what it is for. It is a nudge, not a gate: the
  SessionStart hook says "below the minimum supported version" and continues, and
  the dashboard shows a REQUIRED banner. `advisory` now describes that data loss;
  it previously described the pre-0.18.0 plugin's session gaps, which the plugin's
  own unchanged `min` still nudges for.

- **Fewer round trips per tool call.** Every answer the MCP returns carries who
  you are, and it used to re-ask the server that on every single call. It now
  remembers for a minute, so a long session makes one identity request instead
  of dozens.

- **A search that cannot be answered now says so instead of quietly answering
  something else.** There used to be a keyword fallback for servers too old to
  have the search endpoint, and the MCP never talks to one of those. Its real
  effect was that a search scoped to a project you do not have came back empty
  rather than telling you the project was not there, which reads as "this
  project has nothing in it." You now get a clear error in both cases.

- **An identity blip no longer takes every tool down with it.** If the server
  briefly cannot say who you are, reads carry on with the answer it gave a
  moment ago rather than failing outright.

### Fixed

- **Starting an MCP session no longer spends one of your memory actions.**
  Before doing anything you asked for, the MCP used to run a real search for the
  phrase "capability probe" just to find out whether the server it was talking
  to supported search at all. That search was billed like any other: one recall
  action off your plan, every session, plus a hit on the retrieval engine. It
  also showed up in your own analytics as a search you never ran, which is why a
  teammate who had only opened their editor could appear in an activity feed as
  having searched. Both are gone. Capability answers cost nothing now, and if a
  feature genuinely is not available you find out when you use it, with a clear
  error, instead of being told up front.

- **`project_scoped_search` is reported.** It had been declared since
  server-side project scoping shipped and never actually sent, so anything
  reading the capability list saw a feature you have as missing.

## 0.110.0

## 0.109.0

### Changed

- **A bare `track-work` the researcher TYPED is a toggle again.** 0.106.0 made
  bare inert on this slug to protect the merge — the skill is the how-to
  manual, and an agent loading a manual bare must not stop the recording. That
  protection was attached to the slug when it belongs to the SHAPE, so
  `/track-work` typed with no argument flips to the opposite of the current
  state (as `probe session toggle` and the legacy slugs always have), while a
  bare invocation the AGENT made still writes nothing. The line is drawn at
  shapes that are PROOF OF A PERSON — a raw typed line, or Claude Code's
  `<command-name>` expansion of one, which the harness builds only from a typed
  command. Withheld from the tool call AND from Codex's `<skill>` activation
  block, which the model sends too; a Codex researcher loses nothing, since
  their typed `$slug` line arrives as the raw shape first. `toggle`/`flip` ride
  the same permission as bare — the same request, spelled out. Explicit
  `off`/`on` are unchanged: absolute, idempotent, and honoured on every
  surface.
- **A flip claim now records its slug.** One invocation is (slug, shape), not
  shape alone: a typed bare `/track-work` followed inside the 300s window by a
  bare legacy toggle used to converge on the first one's target, so the second
  switch silently did nothing — the symptom the claim was added to fix. A claim
  with no slug (written by an older plugin) still converges, so an upgrade
  landing mid-window cannot flip twice.

### Fixed

- **The team note syncs on every occasion a session offers, and the tracking
  toggle no longer withholds it.** Two gaps, one cause: the toggle was being
  read as if the shared document were a record of the session.

  The session-start reconcile used to send `--pull-only` when tracking was off,
  on the reasoning that the toggle stops recording and a push records. It was
  half a gate and the wrong half — the `Stop` hook has never consulted the
  toggle and pushes at the end of every turn regardless — so the only thing it
  achieved was to leave an untracked session's edits unsent until some later
  session pushed them, while the instruction block told the agent the file
  "syncs on its own". The toggle governs what Probe records ABOUT THE WORK:
  projects, runs, experiments, entity notes. The team note is the lab's shared
  document, not a record of this session, and an agent has no business writing
  to it unless what it wrote belongs to the team either way. So the reconcile
  now runs in full, always.

- **PreCompact reconciles the team note.** It was already wired up and already
  ran the updater, but `_spawn_session_maintenance` returned early on that
  event, so the note was never pushed there. That matters for exactly one
  population and it is the one this hook exists for: `Stop` covers a session
  that ends, but a session alive for weeks may not reach `SessionEnd` for weeks
  and cannot re-run the start hook it already ran. Compaction is the only
  recurring occasion such a session offers. The hook stays SILENT there — the
  spawn moved above the silence check, not the message.

  The CLI version floor still blocks the spawn: a CLI without `notes sync`
  fails invisibly, which is what the floor is for. Its warning is still
  discarded mid-compaction.

## 0.108.0

## 0.107.0

### Changed

- **Code artifact references are retired: a snapshot now uploads the bytes.**
  `capture_manifest` used to classify a tracked file byte-identical to a PUSHED
  commit as `source="git"` — record the blob id, skip the upload — and let the
  remote stand in for the bytes. That pointer resolves only while the remote
  does. A force-push, a deleted fork, a private repo the person rebuilding
  cannot read, or simply the box being rebuilt all break it silently, and
  nothing downstream can tell a dead reference from a live one without going
  and looking. `Client.check_run` had grown a network probe
  (`unresolvable_code_reference`) purely to tell them apart.

  Every file in the manifest is now `source="blob"` and travels in the run's
  `code-bytes` archive. `n_git_referenced` is structurally `0` and stays in the
  manifest shape, because `restore`, `snapshot-show` and every already-captured
  run's `code_snapshot` meta read the key.

  - The one exclusion left is SIZE. Over `--reference-over-mb` (100 by default)
    a file is recorded as a FILE-PATH reference — path, host, sha256 — never a
    git one. That rule now applies to a repo's tracked files as well as to
    `--include`d ones and to the non-git directory walk, so one big tracked
    binary records where it lives instead of pushing the archive past the upload
    ceiling and losing every other file with it.
  - `base_commit` and `remote` are still captured, as PROVENANCE that no byte
    depends on.
  - `check_run(verify=True)` returns `complete` for a self-contained capture
    (`n_git_referenced == 0`, nothing pending) without any network call, and
    never reports `unresolvable_code_reference` for one. A stale commit no
    longer fails a run whose bytes are in R2. The probe remains for runs
    captured while the old classifier was live.
  - `snapshot-restore` keeps its git path for exactly those legacy manifests.
  - `snapshot-show` no longer labels a size reference `code-bytes`, which
    claimed Probe held bytes it has never held.

- **`probe artifact add --kind code` (also `script`/`source`/`code_bytes`/
  `code_snapshot`) always uploads.** Passing `--reference` with one of those
  uploads the bytes and says so on stderr; when they cannot be read from this
  host — `--allow-missing`, or a path that is not there — it is a usage error
  rather than a pointer. Enforced per manifest row as well as on the command
  line, and `--from-manifest`'s size promotion no longer hands a large code row
  straight back to a reference through the other door. `--uri file://...` is
  refused for a code kind for the same reason — it is the second door to the same
  machine-local pointer. Bucket URIs (`s3://`, `r2://`, `https://`) are untouched
  for every kind, and `--reference` is unchanged for every other kind: a 16GB
  checkpoint on a shared volume is still recorded, not copied.

- **An offsite reference does not disqualify a capture from `complete`.** A file
  over `--reference-over-mb` has its bytes off-platform, so `complete` is not a
  claim that the run rebuilds from Probe alone — it is the judgment
  `capture-run-inputs` already states to agents: a deliberate size reference is
  part of a complete capture, not a gap in one. Recorded here because the two
  branches of `check_run` used to disagree about it, granting `complete` to a run
  with a resolvable git commit and withholding it from an otherwise identical run
  without one.

## 0.106.0

### Changed

- **The tracking doctrine now records everything, and routes files.** The
  measured failure: a tenant's agents wrote 140k characters of notes and
  uploaded zero files, because every destination the prompts enumerated was
  prose. Pointer block v15 (with the skip list REMOVED — recording is consent,
  not curation; the per-conversation toggle is the researcher's only opt-out)
  routes by what is in your hand: files -> artifacts on the lowest entity they
  apply to (run -> experiment -> project -> workspace -> Shared folder), with
  a mechanical safety boundary (never secrets; multi-GB by `--reference`;
  temp/cache excluded), mandatory producer-lineage on cross-anchor files, a
  registry-version rule, and a catch-all so nothing is dropped for want of a
  matching row.
- **The doctrine is written once.** `probe/doctrine.py` composes the shared
  sentences into both Python prompt surfaces (`POINTER_BODY`,
  `MCP_INSTRUCTIONS`), so those copies can no longer drift; the skill markdown
  is held to the same vocabulary by `tests/test_doctrine_sync.py`, whose
  feature census also fails when any platform write surface loses its prompt
  phrase (the fence-shape rule: an unprompted feature is unreachable).
- **Five skills instead of eight.** `track-work` merges
  `start-research-work` + `track-research-work` + `toggle-research-tracking`
  and absorbs `capture-run-inputs`; `show-research-status` replaces
  `show-research-timeline`, adding a state summary (what is tracked, what is
  missing, the sharpest caveat) above the arc. Hard cutover, no stubs — a v14
  block naming a deleted skill fails loudly and the skills listing carries the
  recovery; `client-version.json` bumps close the skew window via the
  existing version nag.
- **Bare `track-work` never flips tracking.** The switch rides the merged
  skill: explicit `off`/`on` flip (hook-recorded, all three sighting shapes,
  both harness spellings), while bare invocation only loads guidance — an
  agent reading its own manual cannot silently stop recording. The legacy
  toggle slugs keep bare-flip forever for resumed transcripts, and stay in
  telemetry's skill set so their invocations keep counting.
- **Eval specs follow the doctrine.** Triggering cases retarget to the merged
  skill and gain the incident shapes (generated documents must upload; an
  untracked dataset must reach the snapshot; a status question routes to
  show-research-status); the two frontier cases that encoded the deleted skip
  list (`torch-bump`, `ci-flake-fix`) flip to expect tracking. The
  instructions runner now verifies the DIRECTION word on an expected switch
  invocation, since a bare invocation deliberately writes nothing.

### Added

- **`misc`: somewhere for a rule nothing could classify.** A declaration the
  classifier could not place used to be stored with no situation at all -- a
  clean exit, a real clause id, visible in an unfiltered `probe rule list`, and
  invisible to every situation-scoped read, which is the only read the store
  exists to serve. Those rules now land in a `misc` bucket (engine side:
  prbe-knowledge 0118), and the surfaces say so rather than pretending nothing
  happened.

  `probe rule declare` prints a note when a rule lands in the bucket, distinct
  from the older warning for a rule that could not be filed at all -- one is
  "reachable but nobody chose", the other is "unreachable". Both warn, neither
  gates.

  `probe_procedures` marks bucket cards `from_fallback` AND rewrites their
  `weight` line to say the rule is probably not about what you are doing. The
  boolean alone was not enough: `weight` is the field the tool contract tells an
  agent to obey, and a card reading "Follow this. Someone on this team stated it
  as a rule." is a lie about relevance even though every word is true about the
  rule. The status gloss survives in parentheses, because how much law a rule is
  stays true; only its relevance is in question.

## 0.105.2

### Fixed

- **A notes write the server refused is no longer reported as written.** A notes
  document at `MAX_DOCUMENT_NOTES` (100,000 characters) refuses every further
  append with a 422 naming the cap — permanently, since no replay can make a full
  document accept anything. `Client.write`'s fail-open path treated it like a
  network blip: it swallowed the refusal, queued the op, and returned, so
  `probe notes append` printed `appended to <kind> notes (…)`, exited 0, and the
  paragraph was never stored. The drainer then dead-lettered it. An agent kept
  appending to a document that had stopped accepting anything, one silent success
  at a time, and read its own dead letters afterwards as a broken outbox — the
  outbox was the only part working as designed.

  `write(raise_permanent=True)` now re-raises a failure the drainer would classify
  `permanent` instead of queueing it, and every notes door passes it. Transient
  failures and auth blocks still journal, which is what fail-open is for. The
  metric rail is unchanged and still queues everything: it must not raise into a
  training loop over one refused point.

## 0.105.1

## 0.105.0

### Added

- **`mcp.tool_served` now records which `get_entity` view was served.** One tool name
  covered two unrelated cost shapes: ROW views (`trajectory`, `metrics`, `artifacts`,
  `events`) are bounded by `token_budget` and page with a cursor, while ATOMIC views
  (`card`, `reproduce`, `handoff`) are deliberately unbounded — `service._VIEWS`
  refuses to truncate a reproduction manifest, because one with fields dropped
  reproduces nothing, and a team note's card IS the document. Both are right; they are
  not the same spend, and a single `tool: get_entity` row mixing them could be read but
  not acted on.

  Prompted by the first day of real data: `get_entity` was 59% of calls and 77% of all
  bytes served, including a single 106KB response.

  `view` is validated against the closed 14-member `contract.View` enum, so it stays a
  dimension rather than caller-controlled content, and the tool's default is resolved
  once at decoration time — a caller that omits `view` still gets `card` served, and
  reading only the passed kwargs would have filed the majority of traffic under no view
  at all. No other argument is recorded: `search_knowledge(query=...)` is literally the
  user's text, and this surface counts, never content.

### Added

- **`probe_procedures`: an agent can now read the team's rules without being asked
  to.** Workflow memory shipped with a CLI and two skills, which means it reached a
  coding agent only when a person drove it. This registers the read half as an MCP
  tool, so an agent about to deploy, migrate, or work somewhere unfamiliar can ask
  what this team has already decided — and get the rule bodies back, not a pointer to
  them. Writing stays off this surface: the MCP server is read-only, and `probe rule
  declare` / `/set-rule` is still where a rule is captured.

  **The empty answer is the part that took the work.** Four unrelated conditions
  return zero rules — no knowledge engine on the deployment, the workspace never
  opted in, nobody seeded the situation vocabulary, and the honest "the team has not
  written one down yet" — and on the wire they were identical. Three of those are
  somebody's bug; one is an answer. An agent that reads a misconfiguration as "this
  team has no rules" stops asking, and nothing downstream ever notices it happened.
  Each now names itself in `completeness.missing`, and only the honest one is
  `state: "complete"`. A classifier that declined to guess the situation is
  `no_match`, which is correct behaviour rather than a failure — serving a
  workspace-wide rule into a situation nobody could identify is exactly what that
  refusal exists to prevent.

  Every card carries a `weight` sentence saying how much law it is, because `status`
  is the store's vocabulary and not the reader's. Only the four statuses a human
  personally stood behind phrase it as an instruction; an `observed_convention` says
  "consider", and an unrecognised status from a newer store says "verify" rather than
  defaulting to something that sounds like law. A rule one person published on their
  own authority carries `shared_by`, `human_backers: 1` and a caveat saying so —
  otherwise the force-publish escape hatch would quietly defeat the two-human guard
  it was built beside.

  It deliberately does NOT take a session id. Every response that returns clauses
  writes an append-only serve-ledger row, that ledger is what taint-exclusion joins
  against forever, and an agent inside a tool call does not know its own session. A
  guessed value would be permanently wrong; a missing one is merely less precise. Use
  `probe rule list --session` when you have the id in hand.

### Fixed

- **`probe rule declare` no longer files a classified rule under nothing.** `preview`
  classifies the prose and prints the situation; `declare` had no way to receive it
  short of a human hand-copying the UUID across. When nobody did, the write
  SUCCEEDED — a real clause id, a clean exit, and a rule attached to no situation.
  It shows up in an unfiltered `probe rule list`, so the store looks populated, and
  it is invisible to every situation-scoped read, which is the only read the feature
  exists to serve. Found on the first rule ever declared in production, by asking for
  the situation it was obviously about and getting nothing back.

  `declare` already accepted a whole `preview` response so the two could be piped
  together; it now reads the classification out of it. An explicit `--situation-id`
  still wins, since that flag is the correction for a classification the human
  disagreed with. An `unknown` outcome still files the rule under nothing, on
  purpose: a rule that arrives in the wrong situation is worse than an unfiled one.

  And when a clause does land without a situation, the command SAYS SO on stderr.
  Warn, never gate — the write is not wrong, it is unreachable, and nothing else in
  the output distinguished those.

### Changed

- **The always-loaded instruction block names the rule store (POINTER_VERSION 14).**
  Both directions, because a read-only mention would leave the store permanently
  one-sided: pull what applies BEFORE an irreversible step, and capture what the
  researcher declares mid-session instead of only obeying it for one conversation.
  Rules are named as surfaces (`pull-rules`, `set-rule`, `probe_procedures`) and
  never as commands, for the reason that file's docstring has always given — it
  cannot be reached by a release, so anything version-specific in it is stale
  forever. The block also states the precedence explicitly: a stored rule never
  outranks the researcher in the room.

## 0.104.0

### Added

- **The hosted MCP measures what it hands an agent.** Every tool call served by
  `mcp.research.prbe.ai` now emits one `mcp.tool_served` event carrying
  `response_bytes` — the size of the response body as it leaves the server — plus the
  tool, the calling agent, and that agent's session id. This is the first answer to
  "how much of a customer's context is Probe?"

  Counted at the ASGI boundary rather than at the API, because `_fit`/`_fit_sections`
  trim payloads inside the MCP process: the API's bytes are pre-trim and always an
  over-estimate. Hosted only; a locally-run stdio MCP is not instrumented.

  BYTES, not characters, and the property name says so — ASGI hands us encoded bytes
  and a character is 1-4 of them. No token estimate is stored anywhere: tokens depend
  on the model doing the tokenizing, which is why `agent` rides along, so the division
  happens downstream where that is known.

  Only tool responses are counted. `initialize` and `tools/list` traffic is excluded
  deliberately (see TODOS.md).

  Emission is an explicit opt-in (`PROBE_MCP_ANALYTICS=1`), set only in our own
  Deployment. `probe-research-mcp-http` is a published console script, so a self-hosted
  copy stays silent unless its operator turns it on — the emitter fails closed. It
  deliberately does NOT reuse the client-side `hosted_base_url` gate: this pod reaches
  the API over an in-cluster Service to avoid a load-balancer hairpin, which that gate
  correctly reads as "not the vendor", and keying off it would have silenced the whole
  feature in production with every unit test still green.

- **The plugin tells the hosted MCP which conversation it is serving.**
  `probe-mcp-headers` now sends `X-Probe-Agent` and `X-Probe-Agent-Session` alongside
  the credential, so a tool call can be joined back to its captured transcript
  (`agent_session:{agent}:{session_id}` — the pair is the key; a lone session id
  resolves to nothing). Claude Code and a paired Codex only; Cursor is detectable but
  uncaptured, so it reports neither rather than a link nobody can follow.

  The session id is charset- and length-bounded before it reaches the JSON on stdout.
  A broken document there is not degraded telemetry, it is an unauthenticated request.

### Changed

- **Client telemetry stops calling everyone Claude Code.** `agent` came from
  `PROBE_AGENT` or a hardcoded `claude_code` fallback, so every Cursor and Codex user
  who had not set the variable landed in the Claude Code bucket — poisoning the exact
  breakdown the property exists for. It is now detected from the environment, and
  absent when nothing is detectable rather than guessed. `client_kind` is likewise
  passed through instead of hardcoded to `cli`.

- **`_telemetry_core` moved from `cli/` to `sdk/`.** The hosted MCP needs it, and
  `deploy-mcp.yml` deliberately excludes `cli/` from the MCP's rebuild filter: a
  module-level import would fail `test_deploy_scope.py`, and a lazy one would pass
  while leaving the deployed service running stale telemetry code. `sdk/` is already
  covered, and its lazy `__init__` means the import no longer drags in `httpx`.

- **The hosted MCP stops re-verifying a healthy token on every call.** `/v1/me` ran in
  front of every tool call on a fresh connection each time — a TCP+TLS handshake plus
  an API round trip before any work. The client is now shared per event loop, and
  acceptances are cached for 15s (rejections keep their 60s). The bound is deliberate:
  a revoked token keeps working for up to that window, and the 401 that prompts a
  client to re-run its headers helper is delayed by the same amount. It is not data
  access — the API authenticates every backend call behind this check.

  That call's response body was already being discarded; it now supplies the caller
  identity the accounting event needs — read once at verification time rather than at
  emit time, so a tool call slower than the cache TTL is still attributed.

  Only a 2xx is cached. A 404/429/5xx still fails open (a blip must not disconnect
  every client) but is NOT remembered, so an upstream fault cannot become fifteen
  seconds of "everyone is authenticated". Overflow evicts the oldest entry rather than
  clearing the map, and the client carries explicit pool limits with a short pool
  timeout so saturation sheds instead of stalling into the fail-open path.

- **An empty bearer now takes the 401 path.** `Authorization: Bearer ` (no value)
  parsed to `""`, which is falsy, so it skipped upstream verification entirely and fell
  through to whatever server-side credential the process had.

- **The outbox reports whether it is draining.** Two client-telemetry events,
  `outbox.drained` and `outbox.stuck`, so a queue that stops delivering is visible
  to the fleet instead of only to whoever reads the banner on their own terminal.

  They come from two processes because neither one sees both halves. The detached
  drainer reports every episode as it exits — `drained` / `auth_blocked` / `paused`
  / `stalled`, with what it delivered and dead-lettered — including the healthy
  case, since without that baseline a quiet fleet and a fleet whose workers all
  died look identical. But the states someone has to act on are exactly the ones
  where no drainer is running: `maybe_spawn` refuses to fork while a journal is
  paused or inside its auth-block cooldown, so a credential that expired mid-run
  produced a growing queue and total silence. The every-command banner reports
  that one, rate-limited to once per six hours so a training loop shelling out
  thousands of times still costs a single event.

  Metadata only, matching the plugin hook's contract: counts, booleans, ages and a
  bounded outcome vocabulary. The journal's `last_error` is a formatted exception
  message and is deliberately not sent — only its exception type, validated to be a
  bare identifier. Both events honor `PROBE_TELEMETRY=off` and the self-host egress
  gate, so a self-hosted install still never calls the vendor.

  Both gates read the backend the CALLER is using, not the CLI config file: the
  banner runs under a possible `probe --base-url ...` that never reaches the
  config, and the drainer delivers each op to the base_url pinned on that op. An
  unprovable backend reports nothing rather than guessing hosted.

- **Swallowed exceptions stop deleting the evidence.**
  `diagnostics.capture_swallowed` reports an exception that was caught and
  deliberately not re-raised, wired at the three delivery-path sites where the
  swallow costs data: a dropped write, a dropped upload, and lease renewal, which
  is how a live run silently becomes `untracked`. `report_crash` gains `handled=`,
  so a recovery lands at warning level and never mixes with a crash in triage.

  Throttled by a stamp file on the journal dir rather than an in-memory counter,
  because the workload it has to survive is a training loop shelling out to
  `probe log` per step -- every process-local budget resets on each one, so N
  commands would have meant N reports.

## 0.103.1

## 0.103.0

### Changed

- **Writes queue by default; `--sync` blocks.** `probe log`, `probe span add` and a
  RUN-anchored `probe artifact add` now return as soon as the write is durable on
  local disk, and a background drainer delivers it. A training loop calling
  `probe log` a few thousand times no longer puts the network on its critical path,
  and it drops a request per call besides: the synchronous path read the run back
  before every write, which queueing skips entirely.

  `--sync` restores blocking, and unlike the old `--async` it works on either side
  of the subcommand — `probe --sync log ...` and `probe log ... --sync` both do what
  they look like, with the one nearer the verb winning. `--async` keeps working
  everywhere it worked before, so existing scripts, skills and manifests are
  untouched. `PROBE_ASYNC=0` is the environment switch; there is deliberately no
  second variable for it.

  **Two things stay synchronous, on purpose.** `probe run end` is the only command
  that verifies delivery — it drains the run's queued writes, refuses to close while
  any of them cannot land, and exits 2 — so ending a job with it means the job
  cannot report success with data stranded on a machine that is about to disappear.
  And only RUN-anchored artifacts queue: `--project`, `--experiment`, `--workspace`
  and `--shared` uploads keep failing loudly at the moment of the write, because
  `run end` gates by run and would never gate them. An explicit `--async` still opts
  any of them in.

  Queued writes exit 0, since the op reached the disk rather than the server. Refs
  are shape-checked locally so a fat-fingered one still fails immediately, and `log`
  and `artifact add` print a trailing `(queued)` or `(delivered)` so a script can
  tell the two apart without guessing. `span add` still prints its span id alone —
  the id is minted locally and is the same in both modes, and callers parse it.
  A write the outbox could not accept exits 2 instead of claiming it was queued.
  `probe outbox status` (exit 0 = everything delivered) remains the general gate.

### Fixed

- **A full or read-only outbox can no longer crash an artifact write.** Queueing an
  upload went straight to the journal, bypassing the guard that already protected
  every other queued write, so ENOSPC, a read-only `XDG_STATE_HOME`, or an `flock`
  that returns ENOSYS (Lustre without `-o flock`, some container overlays, several
  FUSE mounts) surfaced as a raw error out of the command. Telemetry now fails quiet
  and the write falls back to a direct upload. Ctrl-C still interrupts, which matters
  because queueing a large checkpoint copies it first.

- **A queued checkpoint can no longer upload the wrong bytes.** When there was not
  enough room to snapshot a file, the write was queued anyway with a pointer to the
  original path, and the drainer read that path minutes later — so a rotating
  training loop could upload step 1100's bytes under step 1000's name, or fail on a
  file already deleted. That write is now performed directly instead, and nothing is
  left queued behind it to upload a second time.

- **`--meta` and `--notes` are redacted before they are written to disk.** They were
  stored verbatim in the outbox and kept indefinitely for a write that failed.

## 0.102.0

### Added

- **Artifact byte uploads can be asynchronous.** `run.log_artifact(path=...)`
  ran presign → PUT → confirm on the caller's thread, so a checkpoint upload
  stalled a training loop on exactly the network the rest of this work moved
  off that path. The journal has carried an upload op kind all along — the
  CLI's `--async` path uses it — and the SDK now takes it.

  **Staged-or-synchronous**, and the distinction is the whole design. The queue
  is used only when the outbox actually snapshots the bytes; when there is no
  disk headroom the upload happens now instead. `append_upload` would otherwise
  degrade to an op that merely REFERENCES the live file, which is right at a
  command line and wrong beside a training loop: checkpoint rotation — write
  `ckpt-1000`, delete `ckpt-900` — is the normal shape of that workload, and an
  unstaged op whose source rotated either dead-letters or, above the
  inline-hash threshold, uploads different bytes under the caller's name.

  Synchronous, always, for `strict=True` (it must raise and return a row),
  `sync=True`, a non-async client, a non-regular file, and `PROBE_ASYNC_UPLOADS=0`.
  Harbor's `capture_trial` and the code-snapshot archive are pinned synchronous
  too: the first confirms bytes landed for its ledger, the second deletes its
  own tmp archive in a `finally`.

- **A permanently rejected upload records a reference artifact before it
  dead-letters.** The synchronous path already degraded to an `is_reference`
  row carrying `meta.upload = "failed"`, which `check_run` counts as a capture
  gap. The drainer had no equivalent, so a queued upload that was rejected left
  no artifact row anywhere — a capability regression that would have shipped
  inside the feature above, so it is closed first.

- **`gc_blobs` collects crash-orphaned staging temporaries.** `snapshot_file`
  names its temp `.{dst}.{uuid}.tmp`, so staging `.staging-<op>` produced
  `..staging-<op>.<uuid>.tmp` — a doubled dot that the prefix test missed and
  the dotfile skip then swallowed. A SIGKILL mid-copy leaked a
  checkpoint-sized file nothing would ever reclaim, which is precisely the case
  the grace sweep exists for.

### Fixed

- **A deferred close no longer reverts queued tags.** `set_tags` replaces the
  whole list, so a queued one and a server read disagree by construction. A
  bounded `finish()` read the run over the network for its "draining" beacon,
  captured that stale list, and stamped it into the terminal PATCH — and FIFO
  replays the caller's queued tag write FIRST, so the close silently reverted
  it. The beacon is now skipped while a tag write is in flight; a dashboard
  loses a hint for the length of the drain, which beats losing the tags.
- **Miles' terminal record is confirmed before it is deleted.** The `finish`
  branch of the queue drain called `set_status` with neither `strict` nor
  `sync`, then `queue.acknowledge()` unlinked the durable record on the
  strength of not seeing an exception. Safe only by accident — both exporter
  clients happen to be built `fail_open=False`, which forces sync. It says so
  at the call site now, like the metrics branch beside it.
- **Verification that only runs on a returned row is no longer silently
  dormant.** `set_tags` is synchronous so its pre-0066 backend guard actually
  fires and `run.tags` reflects the write. `reconcile_artifact` also scans the
  outbox, because a queued `log_artifact` was invisible to it — the caller
  re-logged and both landed on drain, producing exactly the duplicate that
  method exists to prevent. `snapshot`'s `env_ref` probe stays async (finish()
  deliberately orders its completeness check after the drain) but now says
  when it could not verify, rather than skipping in silence.
- **Counters and status fields stop reporting queued as landed.** `None` means
  both "journaled, will deliver" and "fail-open spooled after a failure";
  collapsing them made every healthy async Harbor trial record permanent
  partial capture, and that verdict rides into the published manifest. Harbor
  now distinguishes `queued` from `spooled`, and its retry gate tests the
  recorded state rather than the presence of a dict — an unconfirmed reward
  used to permanently suppress the strict retry built to heal it. The W&B
  import and `probe wandb import-*` are strict, so their printed counts
  describe writes that landed. `_supersede_run` is synchronous: its whole-list
  tag replace would otherwise replay late against the OLD run's ref, which the
  new run's barrier never covers.
- **Five CLI modules stopped bypassing the CLI's sync pin.** `doctor.py`,
  `setup.py` and `client_installation.py` construct `Client` directly rather
  than through `_new_client`, so they defaulted to async, minted an outbox
  producer per invocation and tagged CLI traffic as `sdk`.

- **Every MCP tool answers compactly now, not just two of them.** `_compact` has always
  existed and has always stripped the envelope bookkeeping an agent cannot act on — but
  `_envelope`'s `verbose` default was opt-IN, so a tool got the lean shape only if it
  remembered to ask. `search_knowledge` and `get_entity` asked. `browse_research`,
  `read_metrics` and the three metric aliases did not, and shipped `schema_version`,
  `as_of`, `scope` and all seven capability flags (six of them True) ahead of their
  answer on EVERY call — about 350 characters before the first byte of data, which is
  what a person watching the tool scroll past actually reads.

  The default is now opt-OUT: the compact shape is the contract and `verbose=true` is
  the debugging affordance. A tool added later is quiet by default instead of noisy
  until someone notices. Nothing that carries signal was dropped — `data`,
  `completeness`, `next_cursor` and any FALSE capability all survive, and the two tools
  whose schema advertises `verbose` still return the full envelope on request.

- **`next_cursor` is always present in a compact response, null included.** It used to
  be emitted only when set, which made absence mean both "that was the whole answer"
  and "this read does not paginate" — so the obvious walk (`cursor =
  page["next_cursor"]`) ran fine while there was more data and raised `KeyError` on the
  LAST page. It failed at completion and succeeded mid-walk, the inverse of a useful
  failure mode, and it is the same reason `capabilities` is always emitted.

## 0.101.0

### Changed

- **The MCP metric tools are one tool.** `get_metrics_grouped`, `get_run_coordinates`
  and `export_metric_points` were one question about GRAIN asked three ways, so the
  question is now the tool and the grain is an argument:
  `read_metrics(run_id, mode="grouped"|"coordinates"|"points", ...)`. Six tools, four.
  `browse_research`, `search_knowledge` and `get_entity` are untouched.

  Each call is validated against THE MODE IT CHOSE, not against the union of the
  three. A merged tool declares every branch's arguments together, so validating
  against that union only checks that an argument exists somewhere in the tool — an
  argument meant for another mode then passes, reaches the endpoint, and is dropped
  without a word, and the 200 that comes back answered a different question.
  `mode="points"` with `by=[...]` is now an error naming the mode that does read
  `by`; before the merge it could not be expressed, and in the equivalent backend
  collapse it returned ungrouped points. `mode` is required for the same reason a
  wrong-mode argument is refused: a default picks the grain for a caller who did not
  state one.

- **The three old tool names still answer, for one release.** An MCP tool name is a
  distributed contract: the tools are served by the server, but the instructions for
  calling them ship in the installed plugin, and `.mcp.json` pins one url for every
  plugin version — so a clean rename breaks every installed client the instant the
  image rolls. Each old name is a fixed-mode delegation to the same dispatch (so the
  two spellings cannot diverge) and logs at WARNING when called. They are removed in
  the change that raises plugin `min` past the first version whose skills teach
  `read_metrics`; plugin `min` is deliberately NOT bumped here.

- The `track-research-work` and `show-research-timeline` skills teach `read_metrics`,
  including that a wrong-mode argument is refused rather than ignored.

- **`auto_drain=False` no longer means "lose your writes".** It disables the
  detached worker subprocess, which is all its name ever implied — but once
  async became the default it also meant every write went to disk with nothing
  to collect it. A default-transport client now uses the in-process exporter
  instead, so async is preserved and delivery is guaranteed. It does NOT fall
  back to synchronous: that would put the network back on the training loop's
  critical path, which is the failure this whole line of work exists to remove.
- **Async writes now require a delivery mechanism.** Three configurations used
  to queue with no drainer at all, silently, because a queued write returns
  `None` exactly like a delivered one. Where the caller explicitly asked for
  async with no background drainer — a custom transport, or `auto_drain=False`
  — it still works and now says so, because `flush()`/`finish()` is a real
  delivery path and draining by hand is deliberate in the CLI barrier and in
  tests. Only the silence was ever the defect.
- **Distributed jobs get one journal per rank.** The outbox defaults under
  `$HOME`, which on SLURM is shared, so every rank on every node contended on a
  single `.append.lock` — a cluster-wide mutex on the metric-logging path.
  Detected from `SLURM_PROCID` / `RANK` / `OMPI_COMM_WORLD_RANK`, or
  `LOCAL_RANK` qualified by hostname. A single-process run is unchanged, so
  nothing needs migrating. Deliberately not node-local scratch: `$SLURM_TMPDIR`
  is reaped at job end, which would destroy an undrained queue.
- **The outbox has a ceiling.** A byte floor (`PROBE_OUTBOX_MIN_FREE_BYTES`,
  sampled rather than checked per write) and an op-count backstop
  (`PROBE_OUTBOX_MAX_PENDING`, default 500k) now apply to every queued write,
  not just blob staging. A refusal drops the NEW write — evicting an old one
  races the drain and discards what a barrier is waiting on — and records a
  numbered capture gap, so the loss reads as a hole in the record.
- **An auth block expires.** A 401/403 used to suppress every future worker
  permanently, so a token rotating mid-run left the queue undelivered with no
  retry and no signal until someone ran `probe outbox retry`. It is a
  five-minute cooldown now: one re-probe, so a re-issued credential resumes
  delivery on its own.

### Fixed

- **Stale-root recovery anchors on the install manifest, not on the hook script.**
  0.42.0 taught every hook to re-resolve when the version it was bound to is
  pruned, by globbing the hook script inside each version-shaped sibling and
  taking the most recent match. Within the hour that shipped, the devbox proved
  the anchor too weak: when 0.42.0 pruned 0.41.0, another session hand-wrote
  three compatibility shims INTO the dead directory — `tracking_guard.py`,
  `telemetry.py`, `statusline_refresh.py`, and nothing else. That directory then
  carried the exact filenames the resolver globs for and, having been written
  afterwards, a newer mtime. So it won, and those two hooks resolved to a
  scratch dir with no `_session_marker` and no `plugin.json`.

  The shims happened to forward to the real install, so nothing broke once they
  were repaired — but nothing about that was guaranteed. An ImportError there is
  exit 1, and on `PreToolUse` / `UserPromptSubmit` a non-zero hook is a VETO, so
  the failure mode is blocked tool calls, which is exactly what the shims caused
  for three sessions before they were fixed.

  Resolution now globs `.codex-plugin/plugin.json` — the file that makes a
  directory an INSTALL rather than a pile of hook scripts — and only then checks
  that the chosen install carries the target. A version-shaped directory without
  a manifest is not a candidate however new it is. Verified against the real
  cache: the 0.42.0 resolver picks the scratch `0.41.0` for two of three
  targets, this one picks the real install for all three.

- **A plugin release no longer breaks every Codex session already running.**
  Codex installs to a version-qualified path
  (`~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`) and binds
  `$PLUGIN_ROOT` once, at session start — but installing a new version REPLACES
  the directory, and the skills root is re-resolved every turn while the hook's
  is not. So the moment a release landed, every live session's hooks exec'd a
  path that no longer existed. With the mirror publishing a version bump on
  nearly every merge, "a session older than the last release" is the normal
  case, which is why it presented as sessions rotting after a period of
  inactivity.

  NOT COSMETIC: `PreToolUse` and `UserPromptSubmit` treat a non-zero hook as a
  VETO, so a pruned directory blocked tool calls and prompts outright
  (`PreToolUse hook (blocked)`, `tracking_guard.py`, exit 2). `SessionStart` and
  `PreCompact` merely failed loudly (exit 127). One cause, two error strings:
  `.sh` targets die in bash, `.py` targets in the interpreter.

  Every hook in both plugins now re-resolves to the installed version when the
  one it was bound to is gone, and re-exports `$PLUGIN_ROOT` so the scripts
  downstream (`PROBE_PLUGIN_JSON`, `version_check.py`, and the tap's
  `$PLUGIN_ROOT/.venv/bin/python3`) follow it rather than the pruned path.
  Selection globs the exact hook script inside each candidate version, so a
  half-extracted install cannot be chosen, and takes the most recently installed
  match — by mtime, NOT `sort -V`, which is a GNU extension whose absence on
  stock BSD `sort` would have made recovery a silent no-op on every Mac while
  every Linux test stayed green.

  When nothing runnable resolves the hook is silent and exits 0.
  `session-start.sh` has always documented itself as fail-open; that contract
  cannot be honoured from inside a file that is gone, so it now lives in the
  wrapper that finds the file. One window remains by construction: a prune
  landing between the check and the `exec` still emits a line, and self-heals on
  the next event.

  The resolver is inlined per command because it must run before any file in the
  plugin can be read; there is no shared script to factor it into that would not
  itself be the missing file. Duplication is the design, and
  `tests/test_codex_stale_plugin_root.py` is what keeps the copies honest.

  Confined to Codex's versioned cache: gated on `$PLUGIN_ROOT` (which only Codex
  sets) and only globbing version-shaped siblings. Claude Code installs to an
  unversioned path and updates it in place, so it never hit this — the one
  behaviour change there is that a Claude hook whose own plugin directory is
  missing now exits 0 instead of failing, which is the same fail-open posture.

- **Telemetry can no longer raise into a training loop.** The SDK had no single
  place converting "telemetry failed" into "telemetry stayed quiet", which is
  why the same class of bug reappeared three times. `sdk/diagnostics.py` is
  that place: a `warn()` that cannot raise whatever the warning filters say.
  Under `-W error` every `warnings.warn` inside an `except` block was a live
  exception — one killed a *successful* run at its closing brace, and one sat
  ahead of the artifact fail-open so the recovery never ran and the run lost
  the only record of the file.
- **`SpanHandle.__exit__` had no exception guard at all**, and `with
  run.span(...)` is the advertised rollout API — the most-travelled unwinding
  path in the SDK. It also called `str(exc)` on the caller's live exception,
  which framework exceptions that format lazily can turn into a crash. Both it
  and `Run.__exit__` now catch `BaseException`: `finish()` sleeps and blocks on
  I/O, so a Ctrl-C landing in it is the likely case, not an exotic one.
- **A broken outbox no longer kills the run.** The journal enqueue was
  unguarded on the default path, so ENOSPC, a read-only `XDG_STATE_HOME`, or an
  `flock` returning ENOSYS raised a raw `OSError` out of `run.log()`. Fail-open
  also covered only `RosError`, so a non-JSON 2xx (a CDN interstitial) took the
  loop down where `Transport.delete` already defended against the same thing.
- **The enqueue is O(1) again.** `status.json` recounted the queue with a
  `listdir` on every append, making N writes O(N² log N) — the queue got slower
  to write exactly as the outage it exists to survive got longer. Measured
  1.19ms/append at depth 1 rising to 6.50ms at 8k; now flat at ~1.04ms.
  `_ensure()` also ran 4 mkdir + 4 chmod per write, and `chmod` is a SETATTR
  round trip on NFS.
- **Nothing is stranded at close.** `OutboxExporter` returned on its stop flag
  *before* the final drain, so a clean close discarded up to a whole interval of
  metrics, and `Client.close()` joined a dead thread and walked away. Both now
  deliver what they can and hand the rest to the detached worker. The handoff is
  one method, `Client.hand_off_delivery` — it was copy-pasted into two
  near-duplicate finish paths, one got fixed, and the other carried the bug
  through three reviews.
- **A dead worker is no longer reported as spawned.** `maybe_spawn` returned
  True when `Popen` succeeded, which says the fork worked, not that a worker
  runs — a child that cannot `import probe` exited instantly and still armed the
  caller's kick throttle, so the queue grew with nothing draining it.
## 0.100.0

## 0.99.1

### Fixed

- **A failed close can no longer replace the error that caused it.** `Run.__exit__`
  runs `finish()` inside an exception handler, so anything it raises displaces the
  traceback the researcher needs — the mechanism that made 0.98.0 kill training
  processes. 0.99.0 fixed the transport case and reopened it from another
  direction: a permanently rejected terminal PATCH (a 422, not a blip) dead-letters,
  and that raise propagated out of the with-block. A close failure is now a warning
  when the body already failed; an explicit `run.finish()` still raises.
- **An artifact upload no longer drains other runs' queues.** The span-ordering
  barrier added in 0.99.0 called `client.flush()`, a machine-wide drain of every
  run's queued ops behind the exclusive drain lock — on a path Harbor walks once
  per file per trial. It is now scoped with `run_ref` to the uploading run, and
  triggers only when that run has a queued *span*, since a pending metric point
  says nothing about whether the cited span has landed.
- **A settled finish no longer strands later writes on an F2 client.** The handoff
  closed the in-process exporter without clearing it, and `_after_enqueue` never
  respawns a closed one; on a client with an injected transport the drainer kick is
  a no-op, so nothing was left to deliver. The handoff now runs only where a
  detached worker can actually be spawned, and clears the exporter so the next
  write builds a fresh one.

## 0.99.0

### Changed

- **SDK data writes are asynchronous by default.** `Client(async_writes=...)`
  now defaults to on, so `run.log()` and every other write through the `write()`
  funnel — steps, spans, notes, tags, artifact registration, run PATCHes — is
  journaled to the local outbox and delivered out of band. A training loop can
  no longer be blocked, or killed, by the network. Reads, creates and
  `finish()` are unchanged and still synchronous: creates never travelled
  through `write()`, and `finish()` remains the delivery barrier.

  Opt back out with `Client(async_writes=False)`, or without touching code via
  `PROBE_ASYNC=0` — the SDK reads that variable now, not only the CLI. Clients
  built with an injected transport (the hosted MCP, tests) stay synchronous by
  default, because the detached outbox worker cannot replay one. The CLI's
  default is unchanged: `probe log` still writes through, and `--async` /
  `PROBE_ASYNC=1` is still its opt-in.

  **`strict=True` now implies synchronous.** It means "fail loudly, never
  journal", which is a demand for the network, so async mode no longer overrules
  it. This matters most to `probe.integrations.miles`, which passes `strict=True`
  and then deletes its own durable queue record once the write returns without
  raising — a queued write there would have erased the only copy of the data.
  Writes carrying a server-response check (`set_project_notes`,
  `append_project_notes`) are synchronous for the same reason: a queued write
  skips the read-back that catches a backend silently ignoring the field.

  Under async, `run.span()` is journaled while an artifact UPLOAD posts directly,
  so `log_artifact(path=..., span_id=...)` now delivers the queue first — the
  server enforces the span foreign key, and the reference must not outrun its
  referent.

### Fixed

- **A metrics POST is retried when the request never reached the server.**
  Retries were gated on `GET`/`PUT`, so `POST /v1/runs/{id}/metrics` got exactly
  one attempt and a single transient blip was immediately terminal. Connect-class
  failures (`ConnectError`, `ConnectTimeout`, `PoolTimeout`) now retry for any
  method. A `ReadTimeout` still does not retry a write: the request was already
  on the wire, and replaying it would append the batch twice.
- **The connect phase is bounded at 5s instead of inheriting the 30s timeout.**
  An unreachable or black-holing endpoint used to park a caller for the full
  timeout on every request — in distributed training, long enough for one rank's
  stalled write to trip a collective. Reads keep the full budget.
- **`Run.set_status` no longer raises on a transport failure.** It was the one
  write in the SDK that propagated, and `Run.__exit__` calls it from inside an
  exception handler — so a network blip while a run was already failing replaced
  the body's real traceback with a transport one and took the process down. It
  now fail-opens to the journal like every other write. `finish()` reports
  `{finish_queued, delivered, remaining}` when the terminal flip is journaled
  rather than claiming a close it did not make.

## 0.98.0

### Changed

- **MCP browse/search responses got token-lean, and the shapes changed.**
  Browse nodes no longer carry the bare `id` (byte-identical to `uuid`'s
  tail — `slug`/`uuid` are the two addresses), null-valued keys are omitted
  (absent means "nothing here"), and `available_views` rides once on the
  envelope keyed by kind instead of on every node. Nested depth-2 children
  are annotated the same as top-level nodes. Project and experiment nodes
  now carry `description` (a 280-char excerpt with `description_truncated`
  when clipped; backend ≥ 0.202). A semantic search document hit no longer
  duplicates its address as `id` — `card.doc_id` is the address; `id`
  appears only on entity-resolved hits.
- **The CLI grew an output policy.** `_print_json` prints compact JSON on a
  pipe and pretty on a terminal (whitespace is the only difference), with
  `ensure_ascii=False` plus re-escaped C1/bidi display controls in both
  modes. `probe get` / `run get` / `project get` / `project list` /
  `run list` accept `--fields slug,name,...` — a top-level projection that
  errors loudly on unknown or empty selections and always preserves
  `next_cursor`.

- **The tracking-off contract is one sentence.** It is injected at every
  session start now, not just at a context boundary, which makes it the
  most-repeated string the plugin owns. The long form spent half its length on
  things the model does not need at that moment: which of two origins turned
  tracking off, and how to turn it back on — the latter sitting beside a clause
  telling it not to raise tracking at all. State, prohibition, and the two
  reassurances that stop an agent over-reading it (reads are fine, keep
  working) survive; the rest is gone.

### Added

- **An off session's probe write is now refused, not narrated.** A new
  PreToolUse gate (Bash only) denies `probe <write>` when tracking is off,
  with the escape named in the refusal itself: `/toggle-research-tracking on`.
  The warn layer stays for everything that reaches Probe another way -- the
  SDK in a training script, the hosted MCP, a job on another machine -- but
  every project this has actually leaked came from an agent typing `probe`
  into Bash, which is the one path a hook can stop.

  **Cleanup is never gated.** `delete` / `remove` / `rm` / `prune` / `purge`
  are exempt from both the deny and the warn: "record nothing" is not "prevent
  cleanup", and blocking the command a researcher reaches for on finding
  untracked leftovers would make the mess permanent.

### Fixed

- **A fresh session is now told that tracking is off.** The off contract was
  injected only on compact and resume -- the boundaries where a declaration
  gets lost -- so a NEW session carried nothing about tracking at all. Its only
  information was the global instruction telling it to register work in Probe,
  and the state lived on the status line, which the model cannot read. Every
  session start carries the contract now.

- **`start-research-work` step 0 no longer turns tracking on by itself.** It
  told the agent that an undecided session on a default-off machine should
  `invoke toggle-research-tracking with on before the first write` -- the prose
  twin of the auto-mark bug, automation making the researcher's declaration for
  them. Two states, not three: tracking false means stop and ASK.

## 0.97.0

### Fixed

- **A machine whose default is `off` now actually records nothing.** The
  default was honoured by exactly one surface. The status line resolved an
  absent per-session marker against `defaults.session_tracking` and rendered
  `untracked`; the SessionStart off-contract and the write-warning hook looked
  only for an EXPLICIT marker, found none, and treated the session as tracking.
  So a session on a default-off box was told to register its work in Probe,
  created projects and notes with no warning at any point, and displayed
  `untracked` the whole time.

  One setting, one file, present from the session's first moment. SessionStart
  seeds `<sid>.tracking` from `default_tracking()`, so "undecided" is no longer
  a state each reader resolves for itself, and both readers now resolve through
  `is_tracking` so a seed that could not be written changes nothing.

  `Transport._auto_mark_tracking` settles the signal at the DEFAULT rather than
  at `on`. A write is the agent's act, not the researcher's declaration: it may
  record which value a session started at, never change it. The default is the
  researcher choosing what a new session starts at — the same setting the
  toggle flips, not a weaker kind of preference.

### Added

- **A session that records research is marked tracked, automatically.** The
  first successful research write from a coding-agent session (creating a
  project or experiment, opening a run, logging metrics, appending notes,
  registering an artifact) now turns the session's tracking signal on — so the
  status line and `probe session status` agree with what the dashboard already
  shows, without the model having to remember the toggle. A decision you made
  stays yours: an explicit `off` (or `on`) is never touched, the flip publishes
  exclusively (a concurrent `/toggle-research-tracking off` always wins), and
  nothing is written while `PROBE_SESSION_TRACKING` holds the setting down.
  Reads and account plumbing never mark anything.
- **"Track this session" now flips the switch.** The toggle skill's triggers
  were off-biased — ON existed only as "resume" — so a researcher saying "make
  sure this is tracked" could get a fully-recorded session whose every local
  surface said untracked. The skill now names both directions, and
  `start-research-work` checks `probe session status` before its first write:
  an explicitly-off session stops and surfaces the conflict instead of
  recording into it.

### Changed

- **Settings is a picker now, not a toggle chain.** The screen takes the
  install panels' shape: checkbox rows grouped under headings (`── Defaults`),
  ticked means on, and the band's forward half reads `Set settings ›` — the
  commit. Nothing is written while you toggle; `→` applies the DIFF (unchanged
  boxes are not rewritten), `←`/Escape leaves with nothing changed, and what
  changed is reported on the way back to the menu. Extensible on purpose: a
  new setting is one entry each in `Setting`, `SETTINGS_GROUPS`,
  `SETTINGS_COPY`, and `read_settings`/`apply_settings` — the screen itself
  never changes. Under a recognized `PROBE_SESSION_TRACKING` override the
  tracking row still does not render, and with every row locked away the
  screen degrades to Back alone rather than offering a commit over an empty
  picker.

- **`‹ Back` returns to the menu instantly.** Backing out of Settings, the
  account screen, or the import folder picker used to stop on an empty
  "Press enter to return to the menu…" page and then silently re-collect per-agent state — about a second
  per detected agent — before the menu came back. A pure back-out now says
  "nothing happened" and the wizard loop takes it at its word: no result
  page, no pause, no re-collection. Actions that do change state still
  re-read it, behind the spinner. Measured on a pty: `←` to menu in 0.06s.

## 0.96.0

### Added

- **A Settings screen on the wizard's main menu**, between Account and Help:
  general per-device options, which are not capabilities — the capability menu
  is about what Probe is *allowed* to do here, and these are about how it
  behaves once allowed. One option so far: whether sessions are tracked by
  default, the same setting `probe session default on|off` writes, now with a
  screen that shows the current state before offering to flip it — and that
  discloses when a `PROBE_SESSION_TRACKING` env override means flipping it
  changes nothing visible. Choosing Settings skips the coding-agent question,
  like Account does: one config file, so the answer has no bearing.

### Changed

- **The wizard's secondary text is warm tan, not grey.** Headings, hints and
  the `Next ›` half of the nav band now render in `#b56f28` — the amber the
  dashboard's status dots use, so the two surfaces share a palette — instead
  of a `#6c6c6c` grey that read as disabled. `‹ Back` keeps the grey on
  purpose: the way forward should be the warm end of the band, the way
  backward the quiet one.

- **The nav band sits under the options, not above them.** The way forward
  used to be on screen before any of the choices it would confirm — on a short
  terminal you could advance past the capability screen before the capture row
  had ever been drawn. The heading still leads the step (with 0.95.1's blank
  line above it, which now separates the question from the heading rather
  than from the band); the band sits where the reading ends, after the rows
  it acts on. `←`/`→` work from anywhere, so nothing became harder to reach.

- **The wizard says when it is working.** Collecting device state costs about
  a second of subprocess calls per detected coding agent, and an update blocks
  on `uv` and the plugin marketplace — both used to run in silence right after
  a keypress, which reads as a hang. Those waits now show an animated one-line
  spinner (`tui.working`), interactive terminals only: pipes and CI get no
  escape codes, same split the install-phase progress screen already draws.
  And picking Settings no longer pays for a per-agent collection it never
  reads — it reuses the snapshot the menu already took, so the screen opens
  immediately.

### Fixed

- **Writing the tracking default can no longer eat the rest of the config.**
  `probe session default` (and now the wizard's Settings toggle, which shares
  the writer) used to read the raw file and save it back outside the config
  lock. Three losses hid in that: racing a `probe login` could restore a stale
  snapshot over the fresh token while reporting success; on a v1 flat config
  the preference sat beside the old keys until the next canonical write
  migrated it into the context — where nothing reads it, so capture silently
  came back on; and a file that would not parse was replaced by just this
  preference. The write now goes through the config lock and the strict
  migrating loader: concurrent writes serialize, v1 files land in v2 shape
  with the preference where readers look, and an unreadable file is refused
  loudly with nothing touched.

## 0.95.1

### Changed

- **The wizard's screens have air between the question and the first thing you
  can press.** questionary renders its rows flush against the question, so the
  nav band and the first menu heading were sitting one line under the last line
  of the question — the boundary between what you are being asked and what you
  can act on had disappeared. Both surfaces get a blank line back. It was
  trimmed to buy rows on a short terminal, which was the wrong row to buy them
  with.

## 0.95.0

### Added

- **`npx probe-research install` goes straight to the install steps.** The
  launcher forwards whatever you type after it, and `install` was not a verb
  the CLI knew, so it reached the argument parser as an unknown command and
  exited. It is a real command now, and `probe install` works the same way.
  Plain `npx probe-research` still opens on the action menu, which is the right
  screen when you do not know what you want yet and the wrong one when you have
  already decided. Takes the same flags as the wizard, so
  `npx probe-research install --yes --no-capture` scripts cleanly.

### Changed

- **Back and Next share one row.** They were stacked, both left-aligned, which
  spent two lines saying what one line says better and hid the one thing the
  band exists to show: they are a pair, opposite ends of the same axis. Now
  `‹ Back  ←` sits at the left edge and `→  Next ›` at the right, and every
  install step gets a row of its own content back.

  Enter also means exactly one thing again. The nav rows used to be selectable,
  so the key toggled a capability on one row, went back on another and
  submitted on a third; the band is a label now, the arrows move between steps,
  and Enter only ever acts on the row you are on. Escape goes back exactly like
  `←` does, including keeping the boxes you had just ticked — it used to exit
  without them.

## 0.94.2

### Changed

- **Section headings are back on the wizard's main menu, above the spacing
  rather than instead of it.** Two jobs, two things doing them: a blank line
  separates the groups, and a short `── Set up this device` names the one
  below it. The heading sits AFTER the gap, which is the whole fix — when it
  was the separator it landed welded to the description above, so the break
  showed up a line late and read as a footer for the previous row. Headings no
  longer rule out to full width either; two dashes say "this names what
  follows" without four grey bands competing with the text.

- **Back and Next are down to a label and a key.** The band used to spend two
  lines of prose above every screen — `‹ Back  the previous step  (esc, ←)`
  and `Next ›  continue with these settings  (→)` — explaining in eleven words
  what the label already said in one. They now read `‹ Back  ←` and
  `Next ›  →`. The arrow is the explanation, the symmetry says how the flow
  moves, and Escape still goes back without needing a line to announce it next
  to a key that does the same thing.

## 0.94.1

## 0.94.0

## 0.93.0

### Added

- **`probe session toggle`** — flip this conversation's tracking to the
  opposite of its current state, resolving "current" exactly as `probe session
  status` does (explicit per-session signal first, machine default otherwise),
  so toggling never disagrees with what the status line was showing. It is the
  same write the toggle-research-tracking skill's activation hook makes on a
  bare invocation, exposed for shells and for reconciling a machine where that
  hook is absent — the skill's reconcile path now names ONE command for the
  bare ask instead of making the model choose between `track` and `untrack`
  from its own reading of prior state.

## 0.92.0

### Changed

- **The tracking switch now flips deterministically on skill activation, and
  the skill is renamed `toggle-research-tracking`** (was `research-tracking`).
  Bare `/toggle-research-tracking` is a true toggle: the PostToolUse hook
  writes the session's tracking signal to the OPPOSITE of the current state —
  the explicit signal when one exists, else the machine's default posture,
  resolved by `is_tracking` so the toggle and the statusline cannot disagree
  about what "current" means. Explicit `off`/`on` (and the skill's synonyms)
  set that state idempotently; `status` and unrecognised prose write nothing,
  because a question must never flip the switch. The write is the same one
  `probe session untrack`/`track` makes, so the researcher's declaration lands
  even when the model fumbles the CLI step — before this, the flip depended
  entirely on the model obeying prose, and an activation it dropped left the
  flag unflipped while the statusline, the compact-contract injection, and the
  write warning all confidently reported the wrong state. The skill now reads
  the result back with `probe session status` and reconciles if the hook was
  absent. The wizard's CLAUDE.md/AGENTS.md pointer block bumps to v10 for the
  rename.

### Added

- **The tracking-off declaration now survives compaction.** `probe session
  untrack` wrote a durable signal, but the only thing carrying "record
  nothing" in the model's context was skill text the summarizer could drop —
  and the plugin then injected its reconcile-Probe nudge into the rebuilt
  context without consulting the signal, breaking the research-tracking
  skill's "no more tracking nudges" promise at the exact moment the model was
  most suggestible. SessionStart now reads the session's tracking signal
  (session-start.sh parses `session_id` alongside `source`): on a
  post-compaction or resumed start of an explicitly untracked session, the
  nudge is replaced with one line restating the off contract. A normally
  tracking session compacts exactly as before, and a tracking resume stays
  silent. Every doubt — no id, invalid id, unreadable signal — degrades to
  the old behaviour, never to honouring a declaration nobody made.
- **A probe write in an untracked session now draws a warning — never a
  deny.** New `hooks/tracking_guard.py` (PostToolUse on Bash): when the
  researcher has declared the session untracked and a command still writes
  research content through the probe CLI, the model gets one line of
  additionalContext restating the contract and pointing at
  `/research-tracking on`. It cannot block anything and exits 0 on every
  path — the SDK, the hosted MCP and remote jobs are out of any hook's reach,
  so a deny here would cover one path of several and teach the agent the gate
  is advisory. The command parse leans silent on every ambiguity: reads,
  `probe session *` (the switch itself), `probe update`/`flush`, unparseable
  quoting and non-Bash tools all stay quiet.

## 0.91.0

### Fixed

- **`sdk.config.config_path` now honours `PROBE_CONFIG_PATH`**, which four other
  readers already did (`version_policy`, `capabilities`, `_telemetry_core`,
  `sdk.session_marker`). The module that WRITES the config was the lone holdout,
  so the two sets agreed only in production: a value written here landed in
  `~/.config` while every reader honouring the override looked elsewhere. Things
  worked in the field and silently vanished under any test or dev environment
  that set it. `version_policy.base_url` documented that divergence rather than
  fixing it; this is the fix, and it had to come first because the tracking
  default depends on writer and reader addressing the same file.

### Added

- **A machine-wide tracking default**, top-level in the probe config:

      {"defaults": {"session_tracking": "off"}}

  `probe session default [on|off]` reads and writes it; `PROBE_SESSION_TRACKING`
  overrides it. Ships ON — tracking is the posture, and a per-session
  `probe session track|untrack` always outranks the default in both directions.

  TOP-LEVEL, never inside a context: `clear_context` replaces a context wholesale
  (a deliberate fail-closed wipe), so a preference kept there would be erased by
  `probe logout` and tracking would silently come back on. It would also make the
  default follow whichever tenant is selected, which is not what "all my
  sessions" means. A test pins that it survives logout.

  Free on the render path: `session_marker.configured()` already opened and
  parsed that exact file and discarded the result, so one parse now feeds both
  answers. The env var is an override and never the home — a dock-launched agent
  sources no shell profile, so a default exported from a shell rc would answer
  one way in a terminal session and another in a dock-launched one.

  An unrecognised value reads as the shipped default rather than as off: a typo
  in a config file must not silently stop recording someone's research.

### Changed

- **The wizard's install steps have a visible Back and Next, and Back now
  actually goes back.** Install was three screens that each knew only about
  themselves: no step number, no way forward except guessing which row ended
  the list, and no way back except Escape — which was invisible, and which on
  the capability screen did not go back at all. It `return`ed out of the
  command, so the key you reach for after ticking the wrong box quit the
  installer, and correcting a mistake meant starting over.

  Every step now titles itself "Install Probe — step 2 of 3" and opens with a
  `‹ Back` / `Next ›` band above a labelled rule. `←` and `esc` go back, `→`
  goes forward, and Back walks the flow properly: updates → capabilities →
  agents → main menu, carrying your choices with it, so revisiting an earlier
  screen never silently re-ticks something you turned off. Ctrl-C still
  abandons, because "I chose wrong" and "get me out" are different intentions.

  Enter now means the same thing on every step — it activates the row under the
  cursor. The two checkbox screens used to run back to back with opposite
  meanings for it, so whichever you learned first was wrong on the next screen.
  Bulk-select keys (`a`, `i`) repaint the boxes they change, so the screen can
  no longer disagree with what gets applied. The agent step's "choose at least
  one" is enforced on the Next row itself, which says so. The auto-update step
  is a two-row pick instead of the one bare `(Y/n)` confirm in the flow.

  Capability descriptions wrap to the terminal instead of being clipped, so on
  a narrow window the capture row still finishes the sentence naming where the
  data goes — and the band is kept tight so that on 80x24 every capability, the
  band and the question are all on screen at once.

  No row is labelled "(recommended)" any more. Everything ships on, so a marker
  on some rows only implied the unmarked ones were the lesser choice.

- **The wizard's main menu is grouped.** Seven equal rows in one column is a
  list you read end to end every time, because nothing in it said which rows
  belonged together. Install / Uninstall / Update now sit together, then
  importing work, then the account, then help, with Exit last.

  The grouping is spacing, not labelled rules: rows inside a group are flush
  and a blank line separates the groups. Rules were tried first and read worse
  — a full-width grey line between every group competes with the seven lines
  of text that are the actual menu, and each one landed welded to the
  description above it, so the break showed up a line late and looked like a
  footer for the row above rather than a header for the rows below. Proximity
  does the same job with no ink, and gives five rows back on a screen that
  overflows 80x24. The last-update row also lines up with the rest of the
  status block now instead of sitting a space short.

- **`is_tracking` collapses to signal-then-default.** The evidence-derived
  fallback is gone: it was the right answer while the product had no default, and
  the product has one now. The refresh hook and the notice ask the same resolver
  the segment renders from, so a machine defaulting to off stops paying three API
  calls per refresh for a segment that can never read as tracking.

## 0.90.0

### Fixed

- **A run launched under a process whose name contains a space no longer reads
  `incomplete`.** Capturing the parent chain read the parent pid from
  `/proc/<pid>/stat` by splitting on whitespace, but the `comm` field is
  unescaped — under `tmux: server` (or any space-bearing parent) the fields
  shift and the parse raised. The error escaped the chain walk and deleted the
  whole `process` slot, so `probe run check` reported
  `missing: [launch_process]` and exited 2 for runs whose argv, cwd, hostname
  and user had all been captured. Affected Linux only; macOS took a different
  branch that already handled it. Such runs now read `unverified` with the
  parent chain recorded.
- A launch directory unlinked mid-run (`os.getcwd()` failing) no longer costs
  the whole `process` slot either — it is reported as one field via the
  non-blocking `launch_errors` advisory, like hostname and user already were.

## 0.89.0

### Changed

- **Two tracking states, not three, and a signal decides which.** The status line
  now shows `tracking` (with the project once one exists) or `not tracking` —
  the third state, `tracking off`, is gone. A reader does not care WHY nothing is
  being recorded, only whether anything is, and the third state made them decode a
  distinction that changed nothing they would do.

  `probe session track|untrack` writes the signal; `is_tracking` resolves it.
  An explicit decision wins in BOTH directions, which is what makes it a toggle.
  With no decision yet it derives from what the session has actually recorded,
  because both fixed defaults are wrong: defaulting ON claims a shell-debugging
  session is recorded when nothing is, and defaulting OFF calls a session
  untracked while its runs are landing.

  The 0.27–0.29 `<sid>.off` file is still honoured on read, so a researcher who
  turned tracking off before upgrading does not silently come back on.

- **The wizard now registers the status line**, alongside writing the standing
  rules — same concern, no extra checkbox. `statusLine` is a key in the
  researcher's own settings, so no release can put the segment there; left to a
  documented `probe statusline install`, it is a feature only changelog readers
  end up with. Chains rather than claims, and reports what it did.
  `tests/test_statusline_reaches_other_people.py` simulates a fresh machine and
  asserts on the settings file that results — including executing the registered
  command to prove it renders.

### Added

- `docs/2026-08-15-tracking-decision-consolidation.md` — the plan to retire
  `start-research-work` as the place the tracking DECISION lives, moving it to the
  signal both agents and humans flip, while keeping the how-to content intact.
## 0.88.0

### Added

- **`probe wizard` → Sign in or switch account.** The wizard managed everything
  about a device except whose data it writes to. Install could only ever ADD a
  credential — and skipped the browser entirely once one existed, because
  `needs_authorization` reads a stored token as "already signed in" — so an
  install made under the wrong account had no path forward inside the wizard at
  all. The only thing that CLEARED a credential was Uninstall, which takes the
  plugins with it. The advice in between was to leave the wizard and run
  `probe login` / `probe logout`, two commands the wizard names exactly once, in
  a message printed after removing a plugin.

  The new screen owns all three: sign in (again, if need be), switch to an
  account already saved on the machine, or sign out. It is a screen rather than
  a fourth checkbox because the capability menu is about what Probe DOES here,
  and every row of it is the same answer under a different account.

  Sign-out is the half with the failure modes, so it is defined as a
  postcondition like the capture off switch it borrows from:

  - it stops SESSION CAPTURE for every selected agent. The capture credential
    belongs to the account being left, so clearing the CLI token and leaving the
    uploader running would keep shipping this device's transcripts there —
    signed out everywhere except where it counts;
  - it revokes the token server-side. Once the local copy is gone the user has
    nothing left to revoke it WITH, and a stranded device token stays valid until
    it expires;
  - it clears the ACTIVE context only, so signing out of staging does not sign
    you out of prod;
  - it NAMES any `PROBE_TOKEN` / `PROBE_MCP_TOKEN` / `PROBE_INGEST_TOKEN` /
    `PROBE_SERVICE_TOKEN` still exported in the shell. Those outrank the file
    that was just cleared and no process can unset them for the parent shell, so
    reporting "signed out" without saying so would be the same lie about the API
    that `capture.py` exists to prevent about transcripts;
  - and it leaves the plugins installed. Signing back in is one screen away.

  Signing in re-pairs capture when — and only when — this device already captures
  from a credential it STORES (an env-var one would shadow whatever we minted),
  clears the killswitch so a re-pair actually sends, and revokes the credential it
  replaced, after the mint rather than before it: revoking first would leave a
  refused approval with no credentials at all.

  `probe wizard --action login` and `--action logout` are the non-interactive
  spellings; a screen cannot be the contract for CI. `--action account` on a dumb
  terminal REPORTS the account and changes nothing — minting or clearing a
  credential unasked is the one thing this action may never do.

### Changed

- **`probe doctor` names the active saved account**, and the wizard's state
  summary shows the account row even when nobody is signed in. "not logged in" on
  a machine holding three contexts sent people hunting for a lost credential when
  the answer was that a different one was active.
- **`probe wizard --action` help lists actions that exist.** It advertised
  `remove`, which is not an `Action` — so the flag the help text recommended for
  an unattended uninstall exited 2.

## 0.87.0

### Changed

- **`end-research-tracking` is now `research-tracking`, and turns tracking back ON
  as well as off** (`/research-tracking on|off`, or bare to report the state).
  The off switch had no user-typed counterpart — resuming meant knowing
  `probe session track` existed.

  `start-research-work` is deliberately NOT merged into it. The two are not peers:
  one is a 364-line how-to for creating projects, experiments and runs, the other
  is a 60-line switch. More importantly they have different owners — STARTING is
  the agent's call, unprompted (`start-research-work` triggers "when the user did
  not ask for tracking"), and STOPPING is the researcher's. Merging them would
  force one description to say both "fire unprompted" and "fire when asked", and a
  contradictory trigger is how a skill stops firing; it would also make tracking
  wait to be asked for, which is the exact failure the standing rule was written to
  fix. `tests/test_prose_anchors.py` now pins that split so the merge cannot happen
  quietly.

  The standing block names the renamed skill (POINTER_VERSION 9).

### Fixed

- **A session with tracking off never picked up a new status-line renderer.** The
  tracking-off gate sat ahead of the SessionStart maintenance, so `sync_renderer()`
  and `prune()` were skipped for exactly the sessions that most needed them: one
  that turned tracking off could not learn to display `tracking off`, and kept
  showing a stale, wrong state indefinitely. Maintenance now runs first — it is
  housekeeping, not tracking — and the gate still stops the network requests,
  which is all it was ever meant to stop.

  Found by verifying a real 0.25.0 → 0.27.0 plugin upgrade end to end rather than
  trusting the unit tests, which all passed.

## 0.86.0

### Changed

- **The always-loaded tracking block names the off switch (POINTER_VERSION 8).**
  The block read as unconditional, so an agent following it had no stated way to
  honour "stop tracking this" and would argue with the researcher every turn. It
  now names `probe-research:end-research-tracking` and says the whole block is off
  for a session once that fires. Naming the SKILL and not the command is the
  block's own rule — it lives in a home directory no release can reach, so a
  command written into it is stale the moment the CLI changes.

  Existing installs pick this up on the next `probe wizard`, which is what the
  version bump is for: an unbumped edit leaves every installed block
  stale-but-current forever.

## 0.85.0

### Added

- **`/end-research-tracking` — an off switch for one conversation.** The
  researcher types it and this session stops being tracked: no further projects,
  experiments, runs, notes or artifacts, no more tracking nudges, the background
  refresh stops, and the status line reads `● tracking off` (muted, not yellow —
  it is a state they chose, and nagging about a decision already made is what the
  switch exists to end).

  `probe session untrack` / `track` / `status` are the commands under it.
  Per SESSION, never machine-wide: a mute button that silenced the next
  conversation too is exactly the surprise nobody wants from one.

  Two things it deliberately does NOT do, both stated in the skill so nobody
  assumes otherwise. It does not DELETE what was already recorded — that work
  happened, and removing it would rewrite the research record to match a later
  mood. And it does not stop TRANSCRIPT CAPTURE: the tap has no per-session off
  switch, only the machine-wide one in `probe wizard`.

  The skill also explicitly overrides the standing CLAUDE.md/AGENTS.md tracking
  instructions for that conversation, which is the point — those rules are
  deliberately broad, and a researcher needs a way to say "not this one" without
  arguing with them.

## 0.84.0

### Added

- **An on-change tracking notice, for agents with no status line to render into.**
  Codex has a status line, but it is a picker over BUILT-IN items (`/statusline` —
  "Select which items to display"; `tui.status_line` is a sequence and an
  unrecognised entry is ignored rather than executed), so a computed segment has
  nowhere to go. The same information is delivered as a message when the state
  CHANGES — untracked → tracked, a different project, a run starting or finishing:

      Probe: tracked → bird-sql-sft
      Probe: tracked → bird-sql-sft · running
      Probe: this session is not tracked yet.

  On change and not on a cadence, because a line every turn saying the same thing
  is one a reader learns to skip — roughly four lines across a whole session
  rather than one per turn. `hooks/statusline_notify.py`, wired to `Stop` (the end
  of an agent turn, and an event both agents support).

  Opt-in like the segment: `probe statusline install` enables it when run under
  Codex, and also configures the Claude Code segment, so one command does the
  right thing per agent. `uninstall` clears both; `status` reports both.
  `PROBE_STATUSLINE=off` silences it.

  Wording comes from the same labels the segment uses, so the two surfaces cannot
  drift into describing one state differently.

### Fixed

- **The status-line refresh hook no longer runs for people who never installed
  it.** It is wired into the shared `hooks/hooks.json`, so it fired for every
  plugin user on SessionStart and every matching PostToolUse — three API calls
  per refresh for a segment they may not have opted into. It now gates on the
  install directory, so it costs one `stat` and a return otherwise.

  This matters most under **Codex**, where the spend could never buy anything.
  Codex has a status line, but it is a picker over BUILT-IN items (`/statusline`
  — "Select which items to display"; `tui.status_line` is a sequence and an
  unrecognised entry is ignored rather than executed), so there is no command
  hook for a plugin to render into. `probe statusline install` now says so
  plainly when run under Codex — as a note, not a refusal, since configuring
  Claude Code from a Codex shell is legitimate.

## 0.83.0

### Changed

- **The status line's untracked dot is filled, not hollow.** `○` is faint at
  terminal font sizes and reads as a rendering artefact rather than a mark, so
  both states now use `●` and are told apart by colour (yellow untracked, green
  tracked). Nothing is lost: the state was already carried by the WORD, which is
  what freed the glyph from the job — and a new test pins that the two states stay
  distinguishable with colour off, so colour can never quietly become the only
  channel.

## 0.82.0

### Added

- **Tracked/untracked in Claude Code's status line.** A one-line segment under
  the input box saying whether this session's work is landing in Probe —
  `○ untracked`, `● <project>`, or `● <project> ▸ running` when a run this
  session opened is executing on this box. Opt-in: `probe statusline install`
  (plus `uninstall`, `status`, and `show` for debugging).

  `· running` is answered by TWO sources OR'd together. The server's
  `GET /v1/runs?foreign_key=<agent>_session_id:<id>&active=true` is the source of
  truth — it is the only one that can see a run executing on a cluster, since
  that run holds its lock on the machine running it. The local run locks are a
  fast path: ground truth for a local process (the kernel releases an flock on
  SIGKILL and OOM, which no heartbeat can promise) and current between refreshes.

  Three pieces. `probe.sdk.session_marker` is the local cache and the renderer's
  formatting rules, vendored into the plugin's hooks (`make sync-session-marker`,
  guarded by `tests/test_session_marker_parity.py`) because the renderer runs
  under the system python3. `hooks/statusline_refresh.py` keeps that cache warm
  from `GET /v1/sessions/{id}/work` — the authoritative answer, which covers work
  created through the SDK, the CLI, the hosted MCP or a training script three
  processes deep; instrumenting the SDK's create paths instead would have missed
  whichever path was not ours to hook. `hooks/statusline.py` renders in ~26ms
  with no network and no credential.

  `probe statusline install` CHAINS rather than claims: the slot is a single
  global `statusLine` key in the user's settings with no plugin manifest field
  for it, so the installer keeps whatever was already configured, tees stdin to
  both sides, backs the file up, and restores the predecessor exactly on
  uninstall. Two traps are guarded by tests that execute the composed command:
  a predecessor ending in a shell comment (imsg-device's marker does) silently
  comments the rest of a `a; b` chain out, and a predecessor doing `input=$(cat)`
  drains the pipe before we can read `session_id`.

  Off with `PROBE_STATUSLINE=off`. Deliberately NOT gated on `PROBE_TELEMETRY`:
  that killswitch turns off analytics about the user, and this is a feature the
  user opted into.

## 0.81.0

### Changed

- **Tracking prose v7 — the gate is the domain, not the activity.** The
  CLAUDE.md/AGENTS.md pointer (v7), both tracking skill descriptions and the
  MCP server instructions now cover anything that is part of the team's ML
  work, whatever its shape — literature and model surveys, design decisions on
  model or pipeline code, dataset processing, provisioning — with a short
  decidable exclusion list (dependency installs, mechanical edits with no
  rejected alternative, reading that produced nothing durable), an
  at-the-moment cadence rule, and a data-provenance recipe (one project-direct
  run per script version via deterministic `--external-id`). Third widening of
  the old noun list proved the list structural, so the list is gone; the new
  `tests/test_prose_anchors.py` pins the criterion across all four rule
  surfaces so the paraphrases cannot drift apart.

### Added

- **Post-compaction reconcile nudge.** SessionStart with `source: "compact"`
  now injects additionalContext telling the agent to reconcile Probe notes
  with what survived compaction (`version_check.py`); PreCompact stays silent
  by contract — it has no context channel. Codex has no equivalent event; the
  gap is recorded in TODOS.md.
- **`evals/triggering/`** — a small-model trigger-classifier judge (21
  scenarios including negative controls and deliberate frontier cases) that
  measures trigger recall AND negative restraint per prose change, cheap
  enough to run on every wording tweak. Manual, outside pytest.

## 0.80.0

## 0.79.0

## 0.78.0

## 0.77.0

### Added

- **`probe import wandb`** — the deterministic W&B mirror whose absence got
  improvised badly once. `probe import wandb entity/project/run_id --run
  <probe-run>` writes one W&B run's metric history into an existing probe run
  with wall clocks backdated to W&B's own timestamps, resumes incrementally
  above the run's existing max step (a cron re-mirror converges instead of
  duplicating), merges `wandb_*` foreign keys, and lands an honest status:
  finished→`completed`, crashed→`crashed`, still-running→`untracked` — never
  `running`, and never overruling a probe run whose live owner is beating.
  Requires the `wandb` package (deliberately not a probe-research dependency).

- **`untracked` run status + observer heartbeats (server 0106, release 1 of
  2).** New vocabulary for "this run went silent without a live client ever
  attached" — the state Anthrogen's mirrored W&B runs were mislabeled
  `crashed` with. THIS release teaches every reader the word and adds the
  liveness plumbing; the reaper still writes `crashed` for all stale rows
  until the next release flips its verdict (owner-heartbeat-lost → `crashed`,
  never-owned → `untracked`). SDK: generated models know the new status,
  `_DEAD_RUN_STATUSES`/`_TERMINAL_STATUSES`/resume treat it like the other
  reopenable terminals, and `heartbeat_run`/`Run.start_heartbeat` accept
  `role="observer"` — a beat that asserts "something is watching" without
  claiming ownership, failing closed against servers too old to know the role
  (a capability probe checks for the `observer_heartbeat_at` field before the
  first beat, because an old server would silently record the beat in the
  ownership column). The miles exporter observer-beats while attached, keeping
  watched runs out of the reaper's scan. `probe run end --status untracked`
  closes a run you registered but never attached a logger to. Old CLIs/plugins
  are served `crashed` for untracked runs via a server-side version gate until
  they upgrade past this release.

### Fixed

- **Codex no longer warns on every session start (plugin 0.21.1).** The
  probe-research plugin asked for a 5s `SessionEnd` hook timeout; Codex caps
  that one event at 3s and prints `⚠ clamping SessionEnd hook timeout to 3s` on
  every session start. The declared timeout is now 3, which is what Codex was
  enforcing anyway — the hook only parses stdin, updates the funnel state file
  and spawns the DETACHED sender (measured at 50ms against a 3000ms budget), so
  no telemetry was ever using the extra 2s. `SessionEnd` is the only event Codex
  clamps; `PreCompact: 10` and `PostToolUse: 5` pass through untouched.
  probe-research-tap already shipped 3 for this reason; the two plugins now
  agree.

## 0.76.0

### Added

- **Install + backfill funnel telemetry (CLI; plugin at its next release).** `probe wizard`
  and backfill now emit the missing front half of the session funnel to
  PostHog: `wizard.invoked` (pre-bootstrap, so a broken npx→persistent install
  still enters the funnel) → `wizard.started` → `wizard.action_chosen` →
  `wizard.configure_started` → `wizard.signed_in` → `wizard.configure_completed`,
  and `backfill.started/.scanned/.plan_ready/.approved/.summary` with an
  outcome on every exit path. In-process async emit: one queue + daemon sender
  thread with a bounded (~1s) exit flush — no subprocesses, nothing ever
  printed, fail-silent throughout. Events are stamped at emit time
  (millisecond timestamps, `identity_mode`, funnel facts) so asynchronous
  delivery can never reorder or misattribute them; `invoked_by` separates
  humans from the auto-update robot spawns; `machine_id` rides every event as
  the cross-surface join key. `PROBE_TELEMETRY=off` disables everything, and
  so does any non-hosted base_url — self-host machines never call the vendor
  (the client-side extension of the egress contract). The shared contract now
  lives in `src/probe/cli/_telemetry_core.py`, vendored beside the plugin hook
  (`make sync-telemetry-core`, byte-parity-tested); the plugin hook imports it
  and gains the same hosted-only gate at this release.

### Fixed

- **Transcript capture reconciler (tap 0.3.0).** Capture no longer depends on a
  hook firing at exactly the right moment. Every live daemon now periodically
  sweeps all local transcripts against their stored cursors and drains every due
  outbox row, so three evidenced losses become delays instead of holes: a
  resumed session whose SessionStart left no daemon (~4h/1MB lost, root cause
  never proven — the reconciler makes it not matter), a resume/compaction leg
  whose transcript materialised after the daemon gave up (2.3MB never captured),
  and outbox batches stranded nine days because `drain_once` only ever looked at
  its own `session_id`. Backfill is in-process rather than a re-spawn, so nothing
  new touches the daemon pid namespace. Eligibility is gated to files the tap
  already tracks or has a session log for — a naive diff would have uploaded
  672MB of pre-install history — and bounded by a 48h horizon plus a per-sweep
  byte budget. Gaps are chunked under the gateway's 2MB body cap (an unchunked
  backfill would have been 413'd and POISON-dropped) and prioritised by recency,
  so an active session is not starved behind historical backlog. Design notes and
  the deliberate no-dedupe decision: `agent/docs/2026-08-12-transcript-capture-reconciler.md`.

### Added

- **Plugin session funnel telemetry (plugin 0.19.0).** The probe-research
  plugin's hooks now emit an anonymous-metadata funnel to PostHog so we can see
  where research tracking breaks per session: `plugin.session_started` →
  `plugin.mcp_used` → `plugin.skill_invoked` → `plugin.probe_write`, plus a
  `plugin.session_summary` with the whole funnel as booleans at SessionEnd.
  Observability only: hooks never gate, the sender is a detached process with a
  3s timeout (a PostHog outage costs a session nothing), and properties are
  ids, versions and whitelisted names — no prompts, commands, paths or file
  contents. `PROBE_TELEMETRY=off` disables it entirely. Identity is the Probe
  user UUID via a cached `/v1/me` (merging with dashboard and backend events);
  unauthenticated installs fall back to a stable machine id that never mints a
  person profile. The plugin release dispatch now bumps BOTH plugin manifests —
  `.codex-plugin/plugin.json` was hand-maintained and would have tripped the
  flavor-parity gate on the next release.

### Fixed

- **The MCP reports which client version it is.** Every backend request from the
  CLI has carried `X-Probe-Client` / `X-Probe-Client-Version` since the update
  banner shipped, but the MCP built its transport without them, so `surface=mcp`
  traffic reached the server anonymous. A user who worked through the MCP and
  never touched the CLI reported no version at all — invisible to both the
  update banner and (now that the backend stamps it onto analytics) to
  client-version adoption tracking.

  The local server sends its own package version, which is the truth under
  stdio: it ships in the same distribution as the CLI, so its version is what
  the user installed. The HOSTED server cannot do that — one transport is
  memoized per token and serves many callers, so a version fixed at
  construction would report OUR deployed version as every caller's and make the
  fleet look evenly upgraded. It instead binds the caller's forwarded pair as a
  per-request override (`client_headers_scope`), reading it the same way the
  transport already reads `current_tool()` and the agent-session headers.
  Binding an EMPTY pair is meaningful and distinct from leaving it unset: a
  caller that reported nothing must reach the backend with nothing, never with
  ours.

## 0.75.0

### Added

- **`probe metrics plot` draws a run's curves in the terminal.** The coordinate
  read surface could already return the step x metric table; nothing could show
  it, so "is the loss actually moving?" meant piping JSON into a plotting script
  or leaving for the dashboard. Bare, the verb prints a BOARD — every series the
  run logged, one sparkline each with last/min/max beside it. `--key` promotes
  those series to full PANELS drawn in braille (a 2x4 subpixel grid per cell, so
  an 80x12 block of terminal holds a 160x48 curve), with axis ticks placed on
  round values rather than on whatever the canvas height divided into.
  `--overlay` puts several keys on one canvas and therefore ONE y-axis — a
  second scale would make any two curves cross wherever the author chose — and
  the footer says when the scales are far enough apart that the smaller curve
  has flattened against the floor. A second series switches the renderer from
  braille to per-series glyphs, because color alone does not survive a pipe, a
  monochrome terminal, or a reader with a color vision deficiency; color rides
  along as a second encoding and turns itself off when stdout is not a TTY or
  `NO_COLOR` is set, and braille/box-drawing degrade to ASCII when the stream's
  encoding cannot carry them.

  A chart is read as the whole truth about a run, so everything the picture
  cannot show is said in words on stderr before it is drawn: a window the read
  cut short (`truncated`/`next_step`, which `--max-rows` and the SDK's page cap
  both produce), a `--key` that matched no series while its siblings drew, a
  seventh series dropped from an overlay, and any non-finite point that no
  scale can hold. On the canvas itself a cell more than one curve reached gets
  its own `%` glyph and a legend entry — coincident curves used to draw as one
  while the legend went on naming both. A key logged under two `kind`s stays
  two named series rather than one name printed twice.

  No new dependency. Unlike its siblings in `probe metrics`, it resolves a run
  ref, so a petname `short_id` works.

## 0.74.0

## 0.73.1

### Fixed

- **A gitignored `.env` is now REPORTED as excluded instead of silently
  vanishing inside a git repo.** The non-git walk has always listed a dropped
  credential under `skipped`, so a reader could tell "not an input" from
  "excluded by policy". Inside a repo the same file was excluded more quietly:
  `ls-files --exclude-standard` never offers it and only lockfiles get a
  force-add, so absence carried no information at all — a run that depended on a
  gitignored `.env` looked identical to one that needed nothing. The git path is
  the common path, which made it the more damaging half of the asymmetry. Both
  paths now classify with the same `_skip_reason`, and the listing is bounded
  (`--directory` collapses ignored trees; collapsed directories are dropped
  rather than guessed at). Exclusion itself is unchanged — auto-uploading a
  working directory must still not be how a credential leaves the machine.

- **`probe snapshot` no longer records its own packages as the project's.**
  When no virtualenv could be resolved for `--cwd` and the CLI's interpreter
  lived outside it, `strict` raised — but the non-strict path (the CLI's
  default) went on to enumerate that interpreter anyway, filing ~40 packages of
  typer/rich/questionary as the experiment's dependencies. The result was a
  full, plausible, entirely wrong dependency list, indistinguishable downstream
  from a correct capture; only the `resolved_via: "unresolved-fallback"` tag
  buried in artifact meta said otherwise. `capture_env` now records the
  provenance and nothing else in that case — no `packages`, no `python` — and
  says so: `Run.snapshot` warns, so `probe exec` (which takes the same
  `detect_venv=True` path and previously only warned when capture RAISED) is no
  longer silent, and `probe snapshot` prints a fuller "env: NOT captured" naming
  the fix (`--venv PATH`, or activate the environment). A missing dependency set
  is recoverable; a confident wrong one is not. Code capture is unaffected, and
  a resolvable venv (`project-venv` / `explicit` / `interpreter`) still captures
  exactly as before.

## 0.73.0

## 0.72.1

### Fixed

- **A dying writer's last operation can no longer strand in the outbox.** If
  the process was killed between enqueue and drain, the final op sat pending
  until the next run happened to drain it; the worker now hands off cleanly on
  teardown. Found by prod smoke. (research-os#476, originally agent#193 by
  @mahitoburrito)

## 0.72.0

## 0.71.0

### Fixed

- **`probe run start` no longer stalls for a minute in a directory that is not
  a git repository.** It auto-snapshots in-process, and outside a repo the
  classifier walked and hashed the WHOLE working directory with no bound —
  measured at 276,507 files and 54.6s in a folder holding ~245 checkouts, with
  every one of them classified pending upload and a 256MB upload cap waiting to
  refuse them at the end. The run itself was created in under a second; the
  whole wait was capture. The non-git walk now stops at 20,000 files and says
  so, and because capture is never a gate the run continues uncaptured rather
  than the command hanging.
- **A run petname now works on every verb that takes a run.** `span list`,
  `artifact list` and the async/`--from-manifest` write paths forwarded the ref
  untouched to routes that type their path param as a UUID, so the exact
  spelling `run start` prints came back as a 422 — silently, in the async case,
  as a dead letter minutes later in a process nobody was watching. The reads
  resolve the ref; the outbox resolves it after a 422 and retries once, so the
  happy path still costs nothing and a queued write no longer needs the network
  to be spelled correctly.
- **`probe experiment edges` takes the experiment slug**, like every other
  experiment verb. It was the one that still demanded a raw UUID.
- **`probe span add --external-key` upserts instead of conflicting.** A span's
  server-side identity is `(run, type, external_key)`, but the upsert is on the
  id and the client minted a fresh one per call — so repeating a span meant
  sending a new id carrying a key the first call had already taken, which the
  uniqueness constraint refused. The id is now derived from the identity when
  there is one.
- **`probe snapshot-show` no longer reports stored files as pending.**
  `n_pending_upload` in the execution record is a classification count ("git
  cannot supply this") frozen at capture, before the upload it counts. It is now
  reconciled against the run's `code-bytes` archive, so `--pending-only` means
  genuinely unavailable — the state it names on a run whose upload really did
  fail, and nothing on a run whose bytes landed.
- **Run recovery picks the incumbent on the whole key.** Run identity is
  `(customer, source, external_id)`, but `on_conflict` resolution scanned one
  page of runs for the external_id alone, so a run under a different source
  sharing that id could be resumed or superseded in place of the real one. The
  409 already names the right row; it is used.
- **A finished low-budget `get_entity` walk reports `complete`.** The last page
  carried every remaining row and still said `partial` with no `next_cursor` —
  telling a caller following the documented contract that data was missing and
  offering no way to fetch it. Over-budget is still reported; it no longer
  decides the verdict on a walk that reached its end. Atomic views (`reproduce`)
  are unchanged.
- **`capture_manifest(include=...)` reaches the non-git path.** It delegated
  with the working directory alone, so an explicitly included file was absent
  from the manifest of any tree without a repo.

- **The test suite no longer spawns a real coding-agent CLI or touches the macOS
  Keychain.** Three tests shelled out to the real binary — `test_backfill_session_id.py`'s
  two `claude -p` checks and `test_codex_config.py`'s `codex mcp list` acceptance
  test. Under the autouse `_isolate_config_home` fixture, which repoints `HOME` at a
  throwaway dir, the spawned agent reached for a login keychain that isn't there and
  macOS raised a `SecurityAgent` "keychain cannot be found to store" prompt on every
  attempt; a full local `pytest tests` turned that into a storm of prompts (bad enough
  once to wedge the keychain into a reboot). The `codex` acceptance test is also the one
  that failed rather than skipped under `CI` and killed the v0.70.2 release. The three
  live-binary tests are removed — the format contracts they checked are still pinned by
  the pure tests beside them — and `conftest.py` now prepends a shim dir of no-op
  `claude`/`codex` stubs to `PATH` for every test, so any future real-agent spawn fails
  fast (exit 97) instead of authenticating. `git` and other tools are untouched.

## 0.70.3

### Fixed

- **The release gate can run the Codex acceptance test.** `release.yml` runs
  the same suite as CI as a publish gate, but only CI installed the Codex CLI —
  and that test fails rather than skips when `CI` is set, by design. v0.70.2
  died on it after the bump commit and tag had already pushed, leaving the
  version manifest advertising a release PyPI did not have.

## 0.70.2

### Fixed

- **Rotating the read token now re-points Codex at it.** `probe login` and
  `probe mcp token set` wrote a new `mcp_token` and left Codex holding the old
  one, which 401s on every call — and nothing said so, because `codex mcp list`
  reports `bearer_token` for any header at all, so the health check stayed
  green. Rotation updates an existing Codex entry (never creates one), a wizard
  re-run repairs a drifted token instead of stopping at "already authenticated",
  and `probe doctor` compares the configured header against the token this
  device holds and says when they differ.

### Changed

- **CI installs the Codex CLI**, so the test that asks Codex whether it accepts
  the config we write actually runs there. It was gated on `codex` being on
  PATH, which meant the only coverage of the one failure that stops Codex from
  starting was a developer's laptop. The test now fails rather than skips when
  `CI` is set and Codex is missing, so the coverage cannot silently vanish
  again.
- **A release stamps the CHANGELOG.** The bump commit touched `pyproject.toml`
  and `client-version.json` only, so every release shipped with its entries
  still under `## Unreleased` and the published history had no version headings
  at all. 0.70.0 and 0.70.1 are stamped retroactively here.

## 0.70.1

### Changed

- **The wizard's finished screen is two labelled lists, not a paragraph.**
  It ends by answering two different questions — what changed, and what you
  still have to do — and used to answer both in one undifferentiated run of
  prose. The action people missed sat at the end of it: approve the Codex hook,
  or capture is installed and sends nothing. Outcomes now appear under
  `What changed:` and actions under `What's next:`, one short bullet each, and
  paths are shown as `~/.codex/AGENTS.md` rather than home-prefixed in full.

## 0.70.0

### Fixed

- **Adding Codex to a machine that already runs Claude Code no longer arrives
  with every box unticked.** The dual-agent menu derived its preselection from
  the intersection of the two agents' state, so a configured Claude Code plus a
  fresh Codex read as "nothing is on" — and an unticked box is not neutral on
  the apply path, which turns it into "remove the CLI + MCP plugin" and "turn
  Session capture off" against the agent that has them. Accepting the defaults
  tore down a working install while adding the second agent. Preselection now
  comes from the union: what the device already does carries over, and the
  lagging agent is brought up to it.
- **`probe wizard --action uninstall` no longer crashes partway through.** It
  raised `TypeError: cannot unpack non-iterable Result object` on the first
  line of removal that touches a plugin, which left the machine half-removed:
  plugins gone, but the managed instruction block, the auto-update flag and the
  Codex MCP entry all untouched, and a traceback instead of a summary. The
  test covering removal stubbed that call as a 2-tuple, matching the broken
  unpacking rather than the real signature, so it passed for as long as the
  command was broken.

### Changed

- **Setting up Codex is one browser approval, the same as Claude Code.** The
  approval the wizard already runs mints the read token (`api` and `mcp` are
  requested together), so a second page to mint another one bought nothing —
  and it was the step that failed, on a three-minute timeout. Codex now gets
  that token through a user-level `[mcp_servers.probe-research]` entry, which
  overrides the plugin's OAuth declaration and reports `bearer_token`. One
  agent or both, it is one approval. The MCP is still hosted; nothing moves on
  to the user's machine. `codex mcp login` remains the fallback for anyone
  whose config cannot be read or written, and `probe wizard`'s removal path
  takes the entry back out so an uninstall cannot leave an orphaned credential
  pointing at the hosted server.

  The write is confirmed with Codex itself and reverted byte-for-byte if Codex
  does not accept it. Valid TOML is not the same as an acceptable config —
  a `bearer_token` key parses fine and then stops Codex from starting at all —
  so a status we cannot read is treated as a config we may have broken, and put
  back before the fallback runs.

- **Global Codex guidance now explicitly searches Research OS before research
  design.** The wizard-managed block in `~/.codex/AGENTS.md` tells Codex to look
  for relevant experiments, decisions, documents, artifacts and captured agent
  sessions through the `probe-research` MCP instead of treating the checked-out
  repository as the team's complete history. The same shared guidance body is
  used for Claude Code to avoid maintaining two divergent policies.
- **Dashboard names, descriptions and hypotheses now default to plain, contextual
  language.** The `start-research-work` skill asks agents to keep display copy short
  and understandable, preserve uncertainty, move execution-level jargon into the
  structured run record, and use known company projects, milestones and decisions
  without inventing internal context.
- **The reproduce view now delegates to the server.** `get_entity(view="reproduce")`
  on a run no longer re-assembles a manifest client-side — it reads
  research-os `GET /v1/runs/{id}/reproduce`, the one place that reads execution
  record, launch context, code snapshot, inputs, lockfiles, lineage and per-span
  environments together. The envelope surfaces the server's
  `completeness.missing` verbatim, so an incomplete run still reads `partial`.
- **The research skills teach capture as automatic, not manual.**
  `start-research-work`'s snapshot step became a *verify* step (`probe exec`/`run()`
  auto-capture; confirm with `probe run check`); `track-research-work` gained a
  machine-checkable claim gate (`probe run check`, exit 2) and a `probe experiment
  freeze` at completion; `capture-run-inputs` drops lockfiles from the manual
  checklist (they are captured automatically now).

### Added

- **`probe run reproduce RUN`** pulls the server-assembled reproduction record for a
  run; `--export FILE` writes it as a portable JSON bundle, and `--materialize DIR`
  reconstructs a runnable directory (restores the captured code tree, writes the
  inputs-decision artifacts, and drops the full record as `reproduce-manifest.json`).
- **`probe experiment reproduce EXP`** pulls per-run reproduction summaries across an
  experiment — a map, each row carrying a `reproduce_url` drill-down — with
  `--version N` to pin against a frozen manifest. **`probe experiment freeze EXP`**
  is an ergonomic alias for minting that immutable `experiment_versions` manifest.
- **An experiment-level `reproduce` MCP view.** `get_entity(view="reproduce")` now
  works on an experiment, not just a run, returning the same per-run summary map with
  `filters={"version": N}` for version pinning.

- **One `npx probe-research` onboarding flow configures Claude Code, Codex, or
  both.** The interactive wizard presents both agents as explicit choices;
  headless installs use `--agent claude`, `--agent codex`, or `--agent both`.
  A dual-agent setup uses one browser approval but persists two independently
  source-bound capture credentials, so neither agent can write through the
  other's transcript route.
- **The Probe Research and Session Capture plugins are native Codex packages.**
  They install from the same marketplace and source trees as their Claude Code
  counterparts. Tracking skills, MCP wiring, pairing, lifecycle hooks, durable
  delivery, and storage stay shared; only the agent command adapter and Codex
  rollout normalization are source-specific.

- **Opt-in automatic hardware metrics (`probe.hw`).** `run(hw=True)` — or
  `PROBE_HW=1` — starts one collector per node (rank-aware election) that
  samples GPU/CPU/memory/disk/network and logs them as `kind=hardware`
  points on an epoch-derived 60s step grid, so redelivery, restarts, and
  future backfill dedup by construction. Sources are tiered: a
  Prometheus-exposition scraper with non-blocking discovery of DCGM-exporter
  and node_exporter (kubelet/cAdvisor is separately opt-in via
  `PROBE_HW_KUBELET=1`), over a psutil + NVML floor with cgroup-v2 quota
  awareness and CUDA_VISIBLE_DEVICES (int/UUID/MIG) → physical-index
  attribution. Fail-open everywhere: circuit breakers per source, a per-node
  series governor, and a bounded drop-oldest buffer — hardware never spools
  and never competes with training metrics (`Client.write(durable=False)`).
  GPU inventory lands on a minimal execution record when no snapshot has
  pinned `env_ref`. With `hw` off (the default) `run()` behaves exactly as
  before, except that `kind="hardware"` metrics are now exempt from the
  resume-step guard (they live on a different clock) and an implausible
  resume receipt (`last_step` in the hardware epoch range) warns and skips
  arming instead of poisoning training resume. Design + review record:
  `docs/2026-08-05-hw-metrics-design.md`. New deps: `psutil`,
  `nvidia-ml-py` (both lazy-imported behind availability probes).

- **Every `probe exec` and `client.run()` call snapshots by default.** Capture used
  to be a step someone remembered to run afterward, which meant the runs that most
  needed reproducing — the ones that broke — were the ones most likely to be missing
  it. `PROBE_AUTO_SNAPSHOT=0` opts out for the rare case where a call site wants to
  snapshot explicitly on its own schedule.

- **A launch block (`metadata.launch`, schema `probe.launch/1`) records how a run
  was actually invoked.** Scrubbed argv, host, the launcher chain (shell → python →
  entry point), env-var NAMES with allowlisted values (never arbitrary values),
  container context, and seed evidence with its provenance (explicit flag vs.
  library default vs. unset) all land on the run at snapshot time. This is the
  difference between knowing a run happened and knowing what would need to be typed
  to make it happen again.

- **OS, CPU and CUDA identity join `execution_records.hardware`, and root lockfiles
  are captured as files with their hashes joined to `deps.lockfiles`.** A
  reproduction attempt on the wrong hardware or the wrong dependency graph fails
  silently otherwise — the run "worked" and the rebuild just produces different
  numbers.

- **`probe run check` learns launch slots and a non-blocking `advisories` list.**
  Gaps in launch capture (missing process/runtime/determinism) flip verdict the way
  any other incomplete-claim gap does; judgment slots and historical runs that
  predate capture-core surface as advisories instead, so the exit-2 gate does not
  turn into migration noise. `probe run check` remains the scriptable audit
  (exit 2 on incomplete). Separately, `finish("completed")` now emits a
  non-blocking completion warning when its own capture is incomplete — nothing,
  opt-in or otherwise, blocks a run; the warning is silent when
  `PROBE_AUTO_SNAPSHOT=0` (capture was declined, not merely gappy).

### Changed

- **`pushed_base` batches to two git invocations total instead of roughly three
  per remote branch.** Same result, computed with a fraction of the process
  spawns on repos with more than a couple of remote branches.

### Fixed

- **The setup wizard now names the agents it is actually configuring.** Claude-only,
  Codex-only and dual-agent runs use matching session-capture, update, uninstall,
  troubleshooting and manual-install language. Codex guidance is written to its
  global `AGENTS.md`; Claude Code guidance remains in global `CLAUDE.md`.
- **Interactive onboarding now starts with intent, then asks for the target.** The
  action menu is first on fresh and configured devices; after choosing an action,
  the wizard asks for Claude Code, Codex or both, then presents that action's
  feature choices. Returning to the main menu prompts for agents again, so update,
  diagnose and uninstall never inherit a stale target from an earlier action.
- **The live Codex canary now defaults to the configured read credential.** It
  prefers the MCP/read token over a write token, preventing a correctly captured
  session from looking absent when the two credentials belong to different teams.
- **Codex setup now verifies the two credentials it actually uses.** A rejected
  capture token triggers re-pairing instead of reading as live, and the wizard
  completes Codex's native OAuth flow for the production `probe-research` MCP
  instead of assuming Claude's headers-helper token authenticated Codex.
- **Short Codex sessions no longer lose their final response.** SessionEnd tails
  and durably enqueues the last rollout bytes before shutdown; the live canary
  now searches only captured source content, so a relevance explanation that
  echoes the marker cannot produce a false pass.
- **Upgrades retire the standalone Codex tap.** The wizard removes
  `prbe-codex-tap-plugin@prbe-ai` before enabling the unified capture plugin,
  preventing two lifecycle hooks from racing over the same session.
- **Re-running the wizard reinstalls a manually removed capture plugin even
  when its pairing token remains valid.** Plugin presence and credential health
  are checked independently, so an uninstall/reinstall test cannot leave
  capture reported on with no lifecycle hook installed.

- **`probe run set <petname>` 422'd instead of amending the run.** `PATCH
  /v1/runs/{run_id}` is UUID-typed, and this verb passed the ref through raw, so
  the petname the CLI calls a run's name came back as a raw pydantic
  `uuid_parsing` dump. `run delete` was fixed for exactly this and the resolver's
  docstring says so; `run set`, `run metrics`, `run series` and `run check` were
  missed by that pass and now resolve too. This is the verb the skill tells an
  agent to use to add a description it forgot at create, so the recovery path for
  runs did not work.

- **`probe run child <petname>` filed the child under a parent that could not
  resolve.** It fetched the parent correctly, then sent the caller's raw ref as
  `parent_run_id` — a UUID field — with the fetched row's real `id` sitting in
  the same scope.

### Added

- **A dashboard `url` on every project, experiment and run, and skills that hand it
  back.** Nothing in the agent surface emitted a link before this: the CLI printed
  uuids, MCP entities carried `id` and `ref`, and an agent reporting "the run
  finished, it is tracked in Probe" left the researcher to go and find it. Which
  they mostly did not — the friction is small and it lands exactly when their
  attention has moved on.

  So the link is now DATA, not something a model reconstructs. `run start`,
  `run end`, `project create` and `experiment create` print it; every MCP browse
  node and `card` carries it as `url`; in-script it is `run.url`. The skills say to
  echo what they were given, and specifically not to assemble one — an invented URL
  is indistinguishable from a real one until it 404s in someone's browser.

  The origin is derived from the API host (`api.research.prbe.ai` →
  `research.prbe.ai`) and nothing else is inferred. Where that implies no dashboard
  — a self-hosted API, a dev box, and above all the hosted MCP's own in-cluster
  Service URL — deployment sets `PROBE_DASHBOARD_URL` (now set for the hosted MCP in
  `deploy/mcp/k8s.yaml`), and absent that the key is omitted entirely. Declining is
  deliberate: falling back to the public host would hand a self-hosted install links
  into somebody else's tenant.

  CLI links go to **stderr**. `RUN=$(probe run start ...)` still captures a bare id,
  and `probe project create | jq` still reads one JSON document.

- **`--notes` on `run set`, `group set` and `group create`.** The column has existed
  server-side since research-os 0096 and the SDK has written it since — `update_run`,
  `update_group` and `create_group` all take `notes`, complete with the pre-0096
  silent-drop warning. The CLI had no door to it, so from a shell the only place to
  put a caveat was `--description`, which meant destroying the description to keep it.

  That is the difference the two fields exist for: a description says what a run IS
  and is written before it runs; notes say what a later reader should DISTRUST about
  it, and are nearly always learned afterwards. The case that motivated this is a run
  that scored 0.0 because its verifier was broken rather than because the thing under
  test failed — `probe run tag RUN invalid` warns the reader, `--notes` tells them why.

  Takes literal text, `@file`, or `-` for stdin, since a caveat is usually a
  paragraph; `""` clears, matching the SDK. Notes exist on projects, runs and run
  groups — **not** on experiments.

  No schema change, no backend change, no new entity: two flags over methods that
  were already there.

### Changed

- **The skills now say that a LABELED POINT IS NEVER PLOTTED.** They previously
  pointed agents straight into this: per-sample ids "go in `labels=` instead", with
  nothing anywhere saying that charts read the unlabeled stream only
  (`labels_hash = <empty>`, `app/telemetry/store.py`). So an agent that correctly
  moved a high-cardinality identifier out of `dimensions` and into `labels` went from
  128 blank charts to one blank chart and read that as a fix.

  Observed on `rollouts-300` in `swe-smith-shakedown`: 300 trials, 129 series, **zero**
  plottable points. The data was complete and correct the whole time — every point
  carried `instance_id`, so the dashboard drew nothing and said "No unlabeled points
  to plot", which reads like the run logged nothing.

  A curve and per-sample identity are now documented as two different writes, with
  separate keys, and the rule is generalised: anything that makes a point unique is
  fatal to a chart in BOTH fields — it shatters the series in `dimensions` and
  removes the point from plotting in `labels`. Per-item identity belongs in an
  artifact.

  The suggested post-run self-check grew a second assertion, because the existing one
  passes on exactly this bug: it counts series, so an all-labeled run with one series
  sails through. Both are needed — they fail on opposite mistakes, and the half-fix
  that moves an identifier from one field to the other trips only the new one.
  Verified against production: the run that "fixed" the shape passes the old
  assertion and fails the new one.

- **The skills route to the whole capture surface, not just the parts that need a
  run.** Findings during implementation, infrastructure that could not be
  provisioned, and runs whose numbers measure nothing all had working doors and
  nothing pointing at them, so they landed in commit messages and chat transcripts.

  - `start-research-work` now names a project-direct run tagged `infra` as the home
    for a provisioning attempt (a stockout, a quota denial, a node acquired and
    released), with `probe link` carrying zone/machine-type onto `foreign_keys` and a
    `provisioned_by` key pointing from the training run back at what shaped it. The
    closed lineage vocabulary has no "provisioned by" relation, so it says not to
    force one onto `probe edge`.
  - A new trigger fires on **an assumption already written into code turning out to
    be false** — a field that means the opposite of its name, a metric that cannot be
    computed the way you assumed, a slice of the corpus that cannot be evaluated at
    all. These arrive mid-implementation with no run open, which is exactly why they
    were never logged.
  - `track-research-work` step 1 now says which of the three notes fields a given
    claim belongs in, and why the project's is the default: its excerpt rides the MCP
    `card`, so it is the only one read by someone who did not already know to look.
  - Step 6 documents `invalid` as the retro-tag for a broken harness, and says
    plainly that run `status` must NOT grow a value for it — those four values are
    lifecycle, and the reaper and every liveness check branch on them.
  - The guidance to fall back on `probe project use` is gone. It writes a
    MACHINE-global anchor that concurrent sessions share; it silently retargeted
    three experiment creates into the wrong project, and there is no `experiment
    move` to undo that. `--project` or `PROBE_PROJECT` (per-process) instead.

### Changed

- **One vocabulary across the nouns.** Learning a verb from one kind and using it
  on the next now works, which was the whole complaint:

  | | project | experiment | run | group |
  |---|---|---|---|---|
  | read | `get` | `get` **(new)** | `get` **(new)** | `get` |
  | amend | `set` **(was `patch`)** | `set` | `set` | `set` |

  `project patch` stays reachable as a hidden alias — it is in scripts — but the
  discoverable spelling is `set` everywhere. Experiments had **no read verb at
  all**, and a run's was only the top-level `probe get`, a bare verb that
  silently meant "a run"; that spelling also still works.

- **The ref and `--description` help text is generated from one place.** It was
  written per-command, so the same concept had several spellings and some were
  wrong: `run set` and `run tag` still said `"run id"` after #173 made a bare ref
  the *petname*, and only one of the six `--description` flags carried any help
  at all. `experiment create`'s slug argument had none while `project create`'s
  did.

- **Workspaces take a slug, like everything else.** `WorkspaceOut` has carried one
  all along — `UNIQUE (customer_id, slug)` — and the CLI simply refused to accept it,
  so `workspace get / rename / use`, `--workspace` on `project create / list / move`,
  and the workspace artifact anchor all demanded a UUID. They take the slug now
  (`probe workspace use mine`), with `id:<uuid>` and `name:<text>` as elsewhere.

  That closes the last gap in the ref grammar for kinds that have a slug. Run groups,
  views and shared files still have none server-side; artifacts are excluded by design.

  Resolution reads the whole workspace listing rather than a server-side filter, which
  is correct **here and nowhere else**: `GET /v1/workspaces` is deliberately unpaginated
  (one per team member, no cursor), so the list is complete and a miss is a real
  absence. The same scan over *projects* is what capped resolution at 200 rows and
  reported live projects as missing — the code says so, so nobody copies it to a
  paginated endpoint.

  BREAKING in the same shape as the rest: a bare workspace UUID no longer resolves, and
  the error names the edit. The ambient workspace (`workspace use`, `PROBE_WORKSPACE`)
  is unaffected — it was written by the tool, not typed by a person.

### Changed

- **A bare ref is now always the SLUG. An id is written `id:<uuid>`, a name
  `name:<text>`.** BREAKING for anyone passing a bare UUID.

  ```
  probe project delete folding              # slug (the normal case)
  probe project delete id:6fa49e87-...      # id
  probe project delete name:"Parity smoke"  # human name
  ```

  The previous rule accepted either spelling bare and worked out which was meant.
  That is the shape Git has, and `fatal: ambiguous argument` is the same error class.
  Here it failed in the worst available way: a UUID-shaped *slug* addressed whichever
  project owned that UUID as its *id*, and `probe project delete` took the wrong one
  with a success exit. The release before this one detected the collision and refused;
  this removes it — a collision can no longer be **expressed**, so there is no case to
  detect, no ranking rule, and no error to read.

  **Why a prefix and not a `--uuid` flag:** one command line takes more than one ref
  (`probe run start --project folding --experiment dockq-sweep`), and a flag cannot say
  which of them it applies to. `--project-uuid` / `--experiment-uuid` multiplies per ref
  per verb. A prefix rides on the ref itself, so one spelling covers every position.

  **`--by-id` / `--by-slug` are gone.** They existed only for a collision that can no
  longer be expressed, and two spellings for one decision was the wart.

  **Migration is loud and safe.** A bare UUID no longer resolves, and the error names
  the exact edit:

  ```
  '6fa49e87-...' is not a project slug -- it is a project ID.
  A bare ref is always the slug, so write it as id:6fa49e87-...
  ```

  Nothing can resolve to the *wrong* entity while you migrate, because the old reading
  no longer exists. `probe project use` now records the explicit `id:` form, and a bare
  UUID already in a context file or `PROBE_PROJECT` is still read as an id — that value
  was written by the tool, not typed by a person.

- **`name:<text>` resolves by human name**, backed by the new `?name=` exact filter
  (research-os 0.110.0.0). Names are not unique, so it resolves only on exactly one
  match; two or more lists the candidates **with their slugs** and refuses. A ref may
  be about to feed a `delete`, so it never resolves through a relevance score — fuzzy
  discovery stays `probe search`.

  A backend that never declared `?name=` DROPS it and answers an unfiltered page. That
  is detected exactly — a genuine name response cannot contain a row named something
  else — and refused rather than acted on.

### Added

- **`notes` on runs and run groups** (research-os 0096) — reachable from
  `create_run`, `create_project_run`, `update_run`, `create_group` and
  `update_group`.

  The schema regen alone did not deliver this. The SDK builds its request bodies
  as hand-written dicts rather than from the generated models, so a widened
  backend contract lands in `probe._generated.models` and nowhere a caller can
  touch — the field was present in the types and unreachable in practice.

  `notes` is not a second `description`. A description says what the run is;
  notes is what a later reader should distrust about it ("suspect, the dataloader
  was stale"). With one field the two compete, which is why the server carries
  both. On a group it matters more: `name` is part of the group's uniqueness key
  within its experiment, so prose appended there changes the row's identity and
  mints a second group instead of describing the one that exists.

  Omitting the field leaves any existing note alone; passing `""` clears it.

  A backend predating 0096 accepts the unknown field, drops it, and answers 2xx —
  the caveat vanishes and the caller is told it succeeded. The SDK now **warns**
  on that, keying off the row these calls already return (no extra request). It
  warns rather than raising, unlike `set_project_notes`: create has already made
  the entity by the time the response is in hand, so raising would leave a run on
  the server and an exception in the caller's lap.

### Changed

- Regenerated `schema/openapi.json` + `src/probe/_generated/models.py` against
  research-os v0.107.0.0. Beyond `notes`, this picks up `name` becoming OPTIONAL
  on `ExperimentCreate` and `ProjectCreate` — a widening the client had not
  reflected, matching what 0080 already did for runs.

### Fixed

- **`start-research-work` now asks for a description on projects, experiments and
  runs.** The skill showed `probe project create folding` with no `--description`
  and told the agent to tag but never to describe, so containers created through
  the normal tracking path landed blank — 7 of 17 projects and 32 of 42
  experiments in the reference lab have no description, every one created by an
  agent that was never asked for one. Nothing fills it in later: generation runs
  only when a child RUN reaches a terminal status, so anything ending without one
  stays blank permanently.

- **A description has a ceiling, not a target: up to 3 sentences, and a few words
  is fine.** Asking for one without bounding it produced a 566-char, five-sentence
  description on the `odyssey` project, which the overview then clamped to two
  lines — the back half written somewhere nobody reads. The point is that the
  field is WRITTEN; length is not the interesting part. The ask is also just a
  description of the thing now, rather than a field carrying other freight: the
  backfill's experiment line had asked for the provenance reasoning that
  justified creating the experiment, and that goes to `probe notes write`
  instead — worth keeping, just not here.

- **The prompt and skill now name the amend verb for each kind.** A description
  missed at create is recoverable, but nothing said how, and the verbs disagree:
  projects amend with `probe project patch`, experiments and runs with `set`.
  **There is no `probe project set`** — an agent that learned `experiment set`
  and guessed got `No such command`, which is how `probe note add` shipped once
  before. A test now asserts the prompt names `project patch` and never
  `project set`.

- **Backfill now writes a description for every project and experiment it
  creates.** The prompt showed `project create` with only `--name`, so whether a
  description appeared was luck — one import wrote one unprompted, the next left
  it empty, and the project read "Add description" under its title. Nothing else
  fills that in: the server generates a description only when a child RUN reaches
  a terminal status, and importing a folder creates no runs, so an undescribed
  backfill stays undescribed permanently. `--description` (plus `--tag`) is now
  explicit on both `project create` and `experiment create`, with the reason
  stated so a later trim of the prompt does not quietly drop it again.

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

  An `id:` / `slug:` prefix on the ref says the same thing as the flags and works on
  **every** command that takes a project or experiment, which the flags do not: they are
  declared on the project and experiment verbs, while a ref is accepted by around a dozen
  commands. Without the prefix, an ambiguous `--project` on `experiment create` raised an
  error naming two flags that command has no way to accept — and the project whose id *is*
  the colliding string could not be addressed there at all, since naming it is the collision.

- **Contradicting the flag with the prefix is refused**, not ranked:
  `project delete slug:X --by-id` used to run the slug and leave the operator reading
  the word "id" in their own command. A disambiguator that picks a winner is the thing
  it exists to remove.

- **A queued (`--async`) artifact resolves its anchor before it is queued.** The async
  branch returned before the resolve on the sync path, so a raw ref went into the journal
  and the drainer POSTed it minutes later — an unresolved slug became a 422 nobody is
  watching, and an id/slug collision filed the upload against the wrong project with no
  operator present. Offline it still costs nothing: an unresolvable ref passes through
  rather than gating the enqueue.

- **A backend that ignores `?slug=` is refused instead of trusted.** FastAPI silently
  DROPS a query parameter a route does not declare, so an engine predating the filter
  (a rolled-back data plane, an older self-hosted install) answers an unfiltered page.
  Reading that as "no slug matched" is the premise a UUID-shaped ref is treated as an id
  on — the original misresolution, resurrected wherever the filter is missing. Detected
  by row count, the one signal that survives the drop: an exact match on a UNIQUE column
  returns 0 or 1 row, so 2+ means nobody filtered.

- **`experiment set` and `experiment tag` resolve a slug.** `experiment delete <slug>`
  worked while `experiment set <slug>` 422'd against the same UUID-typed route.

- **`run list --experiment` and the artifact anchors take a slug.** `--experiment` shipped
  its value straight into a UUID-typed query param, so a slug came back as a raw pydantic
  `uuid_parsing` dump rather than a listing; the artifact anchors resolved `--project` by
  slug but not `--experiment`, so the two flags behaved differently on the same command line.

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

- **A refused browser approval no longer installs the plugins anyway.** Closing the
  approval tab left the run with no credential and it installed both plugins
  regardless, then reported "Not finished". That is the same trap the ordering fix
  closed, on the failure path: the tracking plugin publishes an MCP server whose
  bearer comes from the credential the run just failed to mint, so the first
  unauthenticated connect draws a `WWW-Authenticate` challenge and pins Claude Code
  to OAuth — sending the user to `/mcp` to authenticate a device that was never
  authorized at all.

  Each install is now gated on the grants its capability actually needs, and the
  gate reads the same `CAPABILITY_GRANTS` table that decides what to request, so the
  request and the check cannot drift. A partial grant still installs what it can
  authenticate: an `api`+`mcp` approval that succeeded while `capture` was declined
  installs tracking and skips capture, naming the missing credential rather than
  reporting a failed install that was never attempted. Turning a capability OFF is
  deliberately ungated — refusing to uninstall because a token could not be minted
  would trap someone on the plugin they just asked to remove.

- **A fresh install no longer sends you to `/mcp` to authenticate a device it had
  just authorized.** Two causes, one symptom. The plugin's headers helper looked up
  a top-level `mcp_token` — the v1 config shape — while the wizard has written v2,
  with the credential under `contexts.<current_context>`, since named contexts
  landed. So the fast path returned nothing on every install this product has ever
  produced, and the surface silently rode on its last-resort CLI fallback: fine on a
  machine with `probe` reachable from Claude Code's launch environment, a hard
  failure anywhere else. And the wizard installed the plugin *before* minting the
  credential it serves, leaving an `.mcp.json` on disk with nothing behind it for as
  long as a human takes to approve a browser prompt.

  Either way the request goes out unauthenticated, and the edge answers 401 with a
  `WWW-Authenticate` challenge — which is exactly what makes Claude Code discover an
  authorization server and pin the connection to OAuth. The helper now reads both
  shapes (an unknown `current_context` falls back to `default`, never to a sibling:
  another context's credential would point the MCP at an endpoint the user is not
  on), and the browser approval runs first, ahead of the marketplace refresh and both
  installs. The phase budget starts after the approval rather than before it, so a
  slow reader can no longer consume the whole 300s and produce a run that signed in
  and installed nothing.

  The config read had no test at all — every existing one injected
  `PROBE_MCP_TOKEN` or exercised the CLI fallback against an empty config dir, so a
  read that never once matched production stayed green. The new ones run with a
  `PATH` holding a python3 and no `probe`, so the read under test is the only thing
  that can answer.

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

## Released between 0.28.0 and 0.44.0 (research-os-agent, pre-monorepo)

<!--
This block was titled "## Unreleased" until 2026-08-12, which made it the SECOND
heading by that name in this file — and the one `grep -n '^## Unreleased'` finds
last, `awk` ranges run past, and a human scrolling from the bottom reaches first.
Every entry under it had shipped years of releases ago. Reading it as the pending
section says the next release contains `### Breaking` and `### Added` work when
it may contain one bug fix, which is a version-number error, not a cosmetic one.
It caused exactly that misread during the 0.73.1 cut.

Why a range and not per-version headings: these are ~15 releases' worth of
entries written as they landed in the old research-os-agent repo, whose release
process never split them, and the fold-in carried the section over verbatim. The
top entry (`capture-run-inputs`, #151) shipped in 0.44.0; the heading below this
block is 0.28.0. Splitting the rest would mean attributing each entry to a
release from commit archaeology, and a wrong attribution here is worse than an
honest range. Left as one bounded block on purpose — do not retitle it
"Unreleased".
-->

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
