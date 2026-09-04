# Changelog

## Unreleased

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
