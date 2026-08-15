# Changelog

## Unreleased

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
