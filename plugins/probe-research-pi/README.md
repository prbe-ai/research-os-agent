# probe-research-pi

A [pi](https://github.com/earendil-works/pi) extension with three independent
capabilities: it spawns the `probe-research-tap` daemon to ship sanitized pi
session transcripts to Research OS (the pi equivalent of what
`probe-research-tap`'s `hooks/session-start.sh` / `hooks/session-end.sh` do
for Claude Code and Codex), it briefs every session with the team's shared
note (see "Team note" below — this half works even on an unpaired device,
since it never talks to the tap package), and it bridges Probe Research's
read-only MCP server into pi as native tools (see "Probe MCP read tools"
below — pi has no MCP client of its own, so this extension is one; see
"pi-mcp-adapter" for when that bridge stands down in favor of a shared one).
Runtime code is loaded directly by pi's own extension loader (jiti), no build
step; capture and the team note are dependency-free, the MCP bridge is the
one part of this package with real npm dependencies
(`@modelcontextprotocol/sdk` and, for schema translation, `typebox` — see
"Development" for why `npm install` is required for that half).

## Install

pi's only install surface is a `packages` array in `<agent-dir>/settings.json`
(`~/.pi/agent/settings.json` by default) — read by pi itself at startup, and,
independently, by pi-mcp-adapter if that's also installed (see
"pi-mcp-adapter" below). One entry does the whole job: it is how pi loads
this package's extension code and its three skills (`package.json`'s `"pi":
{"extensions": [...], "skills": [...]}`), and it is the only thing that makes
this package's `mcp.json` manifest discoverable to the adapter. There is no
separate install step for any of the three.

**Keep this user-scope** (`~/.pi/agent/settings.json`), not project-scope
(`<cwd>/.pi/settings.json`). pi's project-scoped resources are gated by
project trust, and the default trust policy (`defaultProjectTrust: ask`)
only prompts in interactive UI — non-interactive invocations (`pi -p`,
`--mode json`, `--mode rpc`) never show that prompt, and with no saved trust
decision `resolveProjectTrusted()` (verified against pi 0.84.3's own source,
`dist/core/project-trust.js`) returns `false`, so an untrusted project simply
never loads its local packages. Those non-interactive modes are exactly the
ones most worth capturing (scripted/agentic pi runs), so a project-scope
install would silently not run for the sessions that matter most. Route A
below always writes user scope; if you hand-edit `<cwd>/.pi/settings.json`
instead of the file Route B shows, you've reintroduced this gate.

### Route A (recommended): `probe wizard --agent pi`

```bash
probe wizard --agent pi
```

Writes the entry for you. `agent/src/probe/cli/pi_config.py`'s
`install_package_entry()` resolves this package's directory
(`PROBE_PI_PACKAGE_ROOT` env, else a bounded checkout walk from wherever the
`probe` CLI itself is running — see that module's docstring for the full
two-step resolution order, and why neither resolving is a loud
`PackageRootError`, never a silent skip), reads `settings.json` (a missing
file is treated as `{}`), appends the resolved absolute path as a plain
string to `packages` unless an entry already identifies this package, and
writes back atomically (tmp file + `os.replace` in the same directory, so a
crash mid-write can never leave a half-written `settings.json`). Every other
key in the file, and their order, survives untouched. Re-running it is a
no-op: identity is checked the way pi itself checks it — an `npm:` source
matches by name, everything else by resolved absolute path, never a raw
string compare — so a second wizard run does not append a duplicate entry pi
would happily load twice.

The same install also retires a legacy symlink install, if one exists and
points at this package (`pi_config.migrate_legacy_symlink` — see "Legacy"
below): once the `packages` entry is doing the job, a leftover symlink at
`~/.pi/agent/extensions/probe-research-pi` would make pi load the extension
twice. `capabilities.py`'s `installed_plugins()` now reads this same
`packages` entry to answer "is pi installed", so `probe wizard`/`probe
doctor` report a pi install truthfully instead of the permanent "absent"
they used to report before this existed.

Today `probe wizard --agent pi` only offers the capture capability for pi —
see "Known limitation" below for what that means for the Probe MCP bearer
token.

### Route B: manual `packages` entry

For anyone not using the `probe` CLI (or pointing at a non-default
`PI_CODING_AGENT_DIR`), add the package directory to
`~/.pi/agent/settings.json` yourself:

```json
{
  "packages": [
    "/path/to/research-os/agent/plugins/probe-research-pi"
  ]
}
```

This is exactly the entry Route A writes for you. An entry may be a bare
string (as above) or `{"source": "..."}` — pi reads both shapes identically
(`getPackageSourceString`, pi 0.84.3's `dist/core/package-manager.js`). A
relative path resolves against the agent dir, not this process's cwd or the
package root — pi-mcp-adapter's own loader resolves relative `packages`
sources the same way, against that same `settings.json`'s directory, so a
relative entry means the same thing to both readers. pi's own `pi install
/path/to/probe-research-pi` command writes the identical kind of entry, if
you'd rather let pi's CLI do it than hand-edit JSON.

### Legacy: the `~/.pi/agent/extensions/` symlink

```bash
mkdir -p ~/.pi/agent/extensions
ln -s /path/to/research-os/agent/plugins/probe-research-pi \
      ~/.pi/agent/extensions/probe-research-pi
```

The pre-`packages`-array install. Still works — pi's auto-discovery scan of
`extensions/` (`collectAutoExtensionEntries`) finds `index.ts` by convention
regardless of anything in `settings.json` — but it is now a strictly worse
option, for two reasons. First, it never reads this package's
`package.json`, so neither the three skills nor the MCP manifest load through
it (see "Skills" below for the verified split between pi's two discovery
mechanisms); pi-mcp-adapter in particular can **never** see `mcp.json`
through a symlink install, because its loader only reads `packages` entries
and does not scan `extensions/` at all (see "pi-mcp-adapter" below). Second,
Route A's wizard install actively retires this symlink the moment it finds
one pointing at this package (`pi_config.migrate_legacy_symlink`) — leaving
both in place would make pi load the extension twice. Use this route only if
you specifically cannot run Route A or B.

`pi install github:prbe-ai/research-os-agent` writes this same kind of
`packages` entry, and is what the wizard now writes on any machine without a
research-os checkout. That repo is the PUBLIC MIRROR — the one Claude and
Codex already install their plugins from — and the `package.json` rendered to
its root carries a `"pi"` manifest pointing at
`plugins/probe-research-pi/`, so pi clones the repo, runs
`npm install --omit=dev` at the root, and loads this package from there.

There is deliberately no npm publish: the mirror push is already the release
mechanism for the other two clients, and reusing it means one channel to keep
in step instead of two. See `pi_config.resolve_install_source`.

## Skills

This package vendors three workflow-memory skills —`track-work`,
`show-research-status`, `instrument-training-runs` — copied byte-for-byte
from this monorepo's canonical `skills/` by `make sync-pi-skills` (run from
`agent/`; `tests/test_pi_skills_sync.py` fails the build if the copies
drift). Edit `skills/`, never `plugins/probe-research-pi/skills/` directly.

**Why only some install routes above bring the skills along.** pi has two
unrelated discovery mechanisms. The legacy symlink into
`~/.pi/agent/extensions/` (see "Install" → "Legacy" above) goes through pi's
*auto-discovery* scan of that directory (`collectAutoExtensionEntries` in pi
0.84.3's `core/package-manager.js`), which finds `index.ts` by convention and
never reads this package's `package.json` at all. The `"pi": {"skills":
[...]}` manifest is read only by `collectPackageResources` — the code path
that runs for a directory pi's own package manager has registered as a
source, i.e. anything in the `packages` array. So Route A and Route B in
"Install" above (both write a `packages` entry) resolve the extension AND
all three skills AND the MCP manifest, in lockstep, from the one entry; the
legacy symlink resolves the extension only. Verified live: with this package
only symlinked into `extensions/`, pi's own `DefaultResourceLoader` resolves
the extension but zero skills; with the package additionally registered via
a `packages` entry, the same loader resolves the extension AND all three
skills. See `tests/skillsManifest.test.ts`, which asserts both states
against pi 0.84.3's real, public resource-loading code (not a
re-implementation of its discovery rules).

### Skills only, no capture: the drop-in skills directory

```bash
mkdir -p ~/.pi/agent/skills
for s in track-work show-research-status instrument-training-runs; do
  ln -s /path/to/research-os/agent/skills/$s ~/.pi/agent/skills/$s
done
```

pi auto-discovers `~/.pi/agent/skills/<name>/SKILL.md` every session
(`collectAutoSkillEntries`, the same auto-discovery family as the extensions
scan above, just pointed at `skills/` instead) — independent of this
package, this monorepo, or the capture daemon entirely. Use this route to
try the skills without also taking on the tap dependency below, or on a
machine that does not have this repo checked out. It does not track updates
to the vendored copy the way a `packages` entry does (a `git pull` in this
repo does not refresh symlink targets that were copied rather than linked),
and it installs nothing for transcript capture or the MCP bridge.

### A known content gap, not a wiring gap

`track-work`'s description is 1,142 characters — over the Agent Skills
spec's 1,024-character cap that pi's own validator enforces
(`core/skills.js`, `MAX_DESCRIPTION_LENGTH`). pi does not refuse the skill
for it: `loadSkillFromFile` still loads it with the full, untruncated
description, and only appends a `warning` diagnostic
(`description exceeds 1024 characters (1142)`) — `tests/skillsManifest.test.ts`
pins this exact diagnostic so a fix or a further drift is caught rather than
silently absorbed. `track-work`'s description is shared verbatim with Claude
Code and Codex (`skills/track-work/SKILL.md` is the single canonical copy —
see the top of this section), so shortening it is a cross-harness content
decision, not something this package should do unilaterally to satisfy pi's
cap; flagged here for whoever makes that call.

## Prerequisite: `probe-research-tap` must be reachable

This extension does not ship the capture daemon itself — it spawns
`python3 -m tap watch`, from the separate `probe-research-tap` Python
package (`agent/plugins/probe-research-tap/` in this monorepo; standalone,
pip-installable, zero dependencies). It resolves an interpreter + that
package in this order:

1. `PROBE_PI_TAP_ROOT` env — point this at a `probe-research-tap` checkout
   (its `tap/` package importable via `PYTHONPATH`). **The supported
   override for any install shape outside this monorepo.**
2. The sibling `probe-research-tap/` directory next to this package. Works
   inside this monorepo checkout AND in a mirror install: the mirror renders
   `plugins/probe-research-pi/` and `plugins/probe-research-tap/` as siblings,
   so the tap package (with its `tap/__init__.py`) is right where this step
   looks. It is only absent for an install shape that ships this package
   alone — see "Known limitation."
3. A bare `python3`/`python` on `PATH`, assuming `probe-research-tap` was
   separately `pip install`-ed into the active environment.

If no step resolves an interpreter at all, the extension refuses to spawn
and says so once, on stderr and in its own log — it will not start a daemon
that immediately exits.

## Pairing

Capture needs a device token, checked in the same order
`hooks/session-start.sh` and `tap/config.py`'s `load_token()` use:

1. A paired device token at `~/.pi/agent/state/probe-research-tap/.token`
   (written by `PROBE_TAP_SOURCE=pi python3 -m tap pair <token>`, using a
   pairing token minted from the Research OS dashboard).
2. The `PROBE_PI_TAP_TOKEN` env var.
3. The probe CLI's own config (`probe login`) —
   `$XDG_CONFIG_HOME/probe/config.json` (default `~/.config/probe/config.json`).

If none resolve, the extension refuses to spawn a daemon for that session —
loudly, once, on stderr (never stdout: `--mode json` reserves stdout for
structured output) — rather than starting one that would 401 on every tick.

## Status

Run the `/probe-status` command inside an interactive or RPC pi session to
see: whether this device is paired and how, whether the Probe MCP bridge is
running directly or standing down for pi-mcp-adapter (see "pi-mcp-adapter"
below), which Python interpreter/tap checkout would be used, and whether a
daemon is currently capturing the active session. (Not verified against
print/json mode's slash-command handling — for a one-shot headless run,
check the session_start refusal message on stderr, or `PROBE_TAP_SOURCE=pi
python3 -m tap status` directly.)

For delivery/outbox detail (bytes shipped, retry state, last-401 halt),
`/probe-status`'s output points at the fuller existing tool:

```bash
PROBE_TAP_SOURCE=pi python3 -m tap status
```

This extension deliberately does not reimplement that reporting — it lives
against a SQLite file this package has no dependency to read, and
duplicating `tap/status.py`'s precedence logic a third time is exactly the
kind of drift its own docstrings warn against.

## Killswitch

Same file, same semantics as Claude Code/Codex:

```bash
touch ~/.pi/agent/state/probe-research-tap/.disabled
```

Checked first, before pairing — a session starting while this file exists
never spawns a daemon, and the daemon itself (`tap watch`) also checks it
independently if ever started another way.

## Team note

Independent of the capture daemon above — this works whether or not a device
is paired, because it never talks to the tap package at all. The team note is
the lab's shared memory: one markdown file, `~/.pi/agent/probe-team-note.md`
by default, that every agent (Claude Code, Codex, and now pi) reads from and
writes to.

**Inject, don't render.** Claude Code and Codex get the note by having a
managed block rewritten into their global instruction file
(`CLAUDE.md`/`AGENTS.md`) at sync time. pi instead appends the note straight
onto `event.systemPrompt` in a `before_agent_start` handler — it never writes
to a file the researcher owns, and it can never collide with
`AGENTS.override.md` shadowing a project's `AGENTS.md`.

**What happens, and when:**

1. `session_start` (every reason, including `reload`) reads
   `~/.pi/agent/probe-team-note.md` — or `$PI_CODING_AGENT_DIR/probe-team-note.md`
   if that env var is set — into an in-memory cache. Absent, empty, or
   unreadable all just mean "nothing to brief this session with"; nothing
   throws.
2. `before_agent_start` (every turn) appends the cached note, unchanged, to
   the system prompt. It never re-reads the file — see the module docstring
   in `src/teamNote.ts` for why a fresh install's first session has no note
   yet, and why that's fine.
3. `agent_settled` (pi's analogue of Claude Code's `Stop` — fires once an
   agent run has fully settled) fires a **detached** `probe notes sync`, with
   `PROBE_AGENT=pi` set so the CLI resolves pi's own file rather than falling
   back to Claude Code's. This is a full sync (push then pull), not
   `--push-only` — a deliberate departure from Claude Code's `Stop`, made
   because pi exposes no `SessionEnd`-shaped event to split the pull half
   onto. The refreshed copy reaches the *next* `session_start`'s cache read,
   not this session's — same as an edit in Claude Code session N first
   reaching the block session N+2 reads.

**Detached for an inverted reason from Codex's `setsid`.** `hooks/team-note-sync.sh`
detaches to survive Codex's 3-second `SessionEnd` cap. pi imposes no timeout
at all on extension handlers (verified against `dist/core/extensions/runner.js`:
every handler runs inside a bare `try`/`catch`, no `Promise.race`). Detaching
here exists so a hung `probe` CLI (dead network, stuck DNS) can never stall
the researcher's actual session — same mechanism, opposite justification.

**Fails open and silent**, matching every hook in `probe-research`'s Claude
Code/Codex plugin: no `probe` CLI on `PATH` or in the documented fallback
locations, a dead network, or a server-side conflict all leave the local file
exactly as the session left it.

**Requires a CLI new enough to have a `pi` case.** `probe notes sync` resolves
`agent_rules.memory_path()` via `PROBE_AGENT`; a `probe-research` CLI without
the `pi` branch (anything before this feature shipped) falls back to Claude
Code's file, so an old CLI on `PATH` would sync the wrong path silently. There
is currently no version floor check here the way `version_check.py` has one
for Claude Code/Codex (`TEAM_NOTE_MIN_CLI`) — flagged as a known gap, not
solved by this extension.

## Probe MCP read tools

Independent of the capture daemon and the team note above — pi has no MCP
client of its own (verified against pi 0.84.3's own source: no MCP file
anywhere in the package, no MCP reference in the extension API types), so
this extension acts as one. On `session_start`, it connects to Probe
Research's read-only MCP server (`https://mcp.research.prbe.ai/mcp` — the
same server Claude Code and Codex already use), lists its tools, and
registers each as a native pi tool via `pi.registerTool()`, translating each
tool's JSON Schema `inputSchema` into TypeBox with `Type.Unsafe()` (see
`src/mcpSchema.ts` for why that — not a hand-written Object/String walker —
is the correct translation: verified against pi's own dist that `parameters`
is only ever used as a plain JSON Schema object, never through TypeBox's
runtime type-checking). Every registered tool name is prefixed
`probe_mcp_...` so it cannot collide with a pi built-in or another
extension's tool. This is the fallback path — if pi-mcp-adapter already owns
this server, this extension stands down instead of connecting a second time;
see "pi-mcp-adapter" below.

**Two auth paths, same resolution order as `probe-mcp-headers`.** The fast
path is a Probe MCP bearer token — the same `mcp_token` Claude Code's MCP
connection already uses, minted on any device that has run `probe login` or
the setup wizard. Resolution order mirrors
`agent/plugins/probe-research/bin/probe-mcp-headers` exactly: `PROBE_MCP_TOKEN`
env first, then the probe CLI config file's `mcp_token`, read from BOTH
shapes (v2 named-contexts, v1 flat) — reading only v1 is the exact bug that
script's own comments record making the fast path silently return nothing on
every install the wizard has ever produced. **The write-scoped `ingest_token`
in the same file is never read** — this surface is read-only. A token is
re-resolved on every connect and again after a 401/403 from a tool call, so
a rotated token (or one written by `probe mcp token set` after this session
started) is picked up without restarting, matching Claude Code's behaviour.

If no bearer token exists anywhere, the fallback is the standard OAuth
authorization-code + PKCE flow via the official `@modelcontextprotocol/sdk`
client (`StreamableHTTPClientTransport`'s `authProvider` option and
`client/auth.js`'s `auth()`/`UnauthorizedError` — no protocol is hand-rolled;
this package implements only the `OAuthClientProvider` storage interface,
under `~/.pi/agent/state/probe-research-mcp/oauth.json` by default). It is
**never** attempted automatically — `session_start` only ever tries a bearer
token or previously-saved OAuth tokens, both non-interactive and bounded, so
an unpaired device degrades silently instead of stalling the session. Run
`/probe-mcp-login` to authenticate interactively: it prints an authorization
URL and prompts for the URL you land on (or just the `code` value) after
approving — the same paste-back pattern pi's own canonical OAuth example
(`examples/extensions/custom-provider-gitlab-duo`) uses, deliberately *not* a
local callback server or a browser launched on your behalf, since neither
belongs in a coding-agent extension and neither works from a headless/SSH pi
session anyway. `/probe-mcp-login` also doubles as "reconnect Probe MCP
tools now" for anyone already paired.

**Degrades, never crashes.** No token, an unreachable server, or a failed
tool list all leave a working pi session with zero Probe tools and one clear
message (stderr + the extension log + a UI toast where one exists) — the
same shape as the unpaired-tap case above. Every network step
(`connect`, `listTools`, each tool `callTool`) is bounded with its own
timeout (`src/mcpBridge.ts`'s `McpBridgeTimeouts`), because pi imposes none
of its own on extension handlers (verified against
`dist/core/extensions/runner.js`: every handler call is a bare
`try`/`catch`, no `Promise.race`) — an unreachable server would otherwise
stall `session_start` indefinitely with nothing to interrupt it.

## pi-mcp-adapter

pi-mcp-adapter is a separate, optional pi package that auto-discovers MCP
server manifests declared by *other* `packages` entries and connects to them
itself, instead of each extension running its own client the way "Probe MCP
read tools" above does. This package ships the manifest it needs to be
found: `mcp.json` plus `"pi": {"mcp": "./mcp.json"}` in `package.json`. Once
this package is registered as a `packages` entry (either Install route
above), the adapter — if also present, now or installed later — discovers
`mcp.json` with zero further configuration and surfaces the server as
`probe-research-pi__probe` in its own tool namespace, prefixed and sanitized
from this package's name (`package-mcp-loader.ts`, their repo). **It does
not scan `node_modules`, and it does not scan `~/.pi/agent/extensions/`** —
the `packages` arrays are the whole gate, which is exactly why the legacy
symlink install (see "Install" → "Legacy" above) can never be discovered
this way.

**The manifest declares OAuth, and nothing else.** `mcp.json` sets
`auth: "oauth"` with a `scope`/`clientName` under `oauth` — no `bearerToken`
and no `!command` fast path. Verified live against production on
2026-08-27: the server's own discovery metadata (S256 PKCE,
`token_endpoint_auth_methods: ["none"]`, RFC 7591 dynamic client
registration, scope `research:read`) is enough for the adapter's built-in
OAuth flow to complete on its own, with no manual client-registration step
and no separate secret to configure. A `bearerToken` entry would be more
convenient when it works, but the adapter's `!command` execution semantics
make a **failing command stop the connection** rather than fall through to
OAuth — it is a fast path, not a fallback-safe one. Shipping one in this
manifest would turn "no token minted for this device yet" into "the adapter
refuses to connect at all" the first time someone hits it, instead of
degrading to an interactive OAuth prompt the way this package's own direct
bridge does. Lazy connection lifecycle (the adapter's default) is correct
here too: nothing forces a connection before some tool is actually called.

**The stand-down rule.** With both pi-mcp-adapter and this package
registered, two things would otherwise try to serve the exact same Probe
tools: the adapter, through the manifest above, and this extension's own
direct bridge (`session_start`, see "Probe MCP read tools"). `src/adapterHandoff.ts`
decides which one runs, re-checked fresh on every `session_start` (two local
file reads, no network) rather than cached once — a `packages` entry can be
added or removed between sessions, and this has to track that without a
restart.

The rule is **both-or-neither**, not just "adapter present": the direct
bridge steps aside only when the merged user *and* project `packages`
arrays contain **both** pi-mcp-adapter and this package. Adapter present but
this package installed only through the legacy symlink is deliberately
**not** enough to stand down — the adapter's loader only reads `packages`
entries, so it can never see a symlinked package's `mcp.json` at all, and
standing down there would make Probe's tools vanish outright, which is worse
than the duplicate-tool registration this whole check exists to prevent.

Detection **fails open**: a missing, unreadable, or malformed `settings.json`
at either scope is read as "this scope contributes no packages," which can
only push the decision toward keeping the direct bridge alive, never toward
silently standing down. Run `/probe-status` to see which mode is active for
the current session — it prints either `mcp: Probe MCP served via
pi-mcp-adapter` or `mcp: bridged directly by this extension (see
/probe-mcp-login)`, so a "where did my Probe tools go" report has a trail to
follow immediately.

## Config

| Variable | Purpose |
|---|---|
| `PROBE_PI_TAP_TOKEN` | Device token override |
| `PROBE_PI_TAP_PLUGIN_DIR` | State dir override (default `~/.pi/agent/state/probe-research-tap`) |
| `PROBE_PI_TAP_ROOT` | Path to a `probe-research-tap` checkout (see above) |
| `PROBE_CONFIG_PATH` | probe CLI config path override (tests/dev) |
| `PROBE_BASE_URL` | Backend origin override (read by the daemon itself) |
| `PI_CODING_AGENT_DIR` | pi's OWN global config dir override (default `~/.pi/agent`) — not a probe-specific variable; relocates the team-note document, the MCP OAuth token cache, and, for pi itself, `AGENTS.md`, `settings.json`, and `extensions/` |
| `PROBE_MCP_TOKEN` | Probe MCP bearer token override — same variable, same precedence, as `probe-mcp-headers` |
| `PROBE_MCP_URL` | Probe MCP server URL override (default `https://mcp.research.prbe.ai/mcp`; tests/dev only) |

`PROBE_PI_PACKAGE_ROOT` is a related override, but it belongs to the `probe`
CLI's wizard (`agent/src/probe/cli/pi_config.py`), not to this extension's
own runtime — see "Install" → "Route A" above.

## What this extension actually does

On `session_start` (every reason: `startup`, `new`, `resume`, `fork`,
`reload`), independent of everything below: attempt the Probe MCP bridge
(bearer token, else previously-saved OAuth tokens, else skip with no network
call — see "Probe MCP read tools" above) and register whatever tools that
connection yields, unless pi-mcp-adapter already owns the server (see
"pi-mcp-adapter" above), in which case this step logs the hand-off and does
nothing further. Runs before, and regardless of the outcome of, every step
that follows — it needs no transcript file, no pairing, and is not gated by
the killswitch (a different credential, `mcp_token`, never the tap's
ingest/device token).

Then, still inside `session_start`:

1. Resolve the current session's id and JSONL path
   (`ctx.sessionManager.getSessionId()` / `.getSessionFile()`) — skip if the
   session has no file yet (in-memory, nothing to tail).
2. Best-effort prune `.shutdown` sentinels in `/tmp` older than two days
   (session-end.sh deliberately never deletes its own; see `daemon.ts`) —
   the same hygiene Claude Code's hook does, needed here too for a pi-only
   install with no Claude Code hook to do it for free.
3. Refuse silently if the `.disabled` killswitch is present.
4. Skip if this extension runtime already spawned a daemon for this session
   id (fast, in-process — survives `/reload` because module state persists
   across it) — and skip again if a *different* process already has a live
   watcher for it (a pidfile + liveness check, same convention Claude Code's
   hook uses: `/tmp/probe-research-tap-watcher-<session_id>.{pid,shutdown}`,
   the **same prefix Claude Code uses**, not a pi-specific one — see
   `src/paths.ts` for why that's load-bearing).
5. Refuse loudly (stderr + log + UI toast where a UI exists), once, if
   unpaired or if no Python interpreter can be found.
6. Otherwise spawn a detached crash-recovery wrapper
   (`/bin/sh`, POSIX, ported from `hooks/session-start.sh`'s bash wrapper)
   that runs `python3 -m tap watch --session-id <id> --cwd <cwd> --transcript
   <path>` with `PROBE_TAP_SOURCE=pi` set, and `.unref()`s it — this returns
   immediately, it does not wait for the daemon.

On `session_shutdown`: touches the shutdown sentinel and signals the
wrapper's process group (`stopDaemon`, the `session-end.sh` analog), and
best-effort closes any live Probe MCP connection, for every reason
**except** `reload` — a reload tears down and rebuilds the extension runtime
for the *same* session, which fires `session_start` again immediately after;
stopping the daemon (or dropping the MCP connection) there would spuriously
FINALIZE a session that isn't actually ending, or leave that session's Probe
tools gone until the next real reconnect. On `reload`, `session_start`
instead re-registers the already-fetched tool list against the fresh
registry with no new network call (pi rebuilds each extension's tool
registry from scratch on reload — verified against `loader.js`).

## Known limitation

**The `probe-research-tap` location is solved for the mirror install, not in
general.** A mirror install (`github:prbe-ai/research-os-agent`) clones the
whole repo, and the mirror renders `plugins/probe-research-tap/` as a sibling
of this package — so step 2 resolves there the same way it does inside this
monorepo, which is what closed the old "no packaging step ships a Python
package alongside a pi extension" gap. It is closed by the SHAPE of the
channel, not by a packaging step: any install that ships this package alone
still has only step 3 (a separate `pip install`) or `PROBE_PI_TAP_ROOT`,
which remains the documented override for every other install shape.

**The wizard installs the package now; it does not fully pair the MCP bridge
yet.** `probe wizard --agent pi` installs the extension, the three skills,
and the MCP manifest together through the `packages`-entry mechanism (see
"Install" above), retires a legacy symlink pointing at this package
(`pi_config.migrate_legacy_symlink`), and requests/stores a capture device
credential the same way it does for Claude Code and Codex. What it does not
yet do: today `probe wizard --agent pi` only offers the **capture**
capability for pi — `setup.py`'s `restart_notice` says so explicitly ("pi is
capture-only through this wizard") — and the **tracking** capability is what
requests the `mcp` grant that mints a Probe MCP bearer token
(`CAPABILITY_GRANTS`). So on a device that has never run `probe login`/the
wizard for Claude Code or Codex first, `/probe-mcp-login`'s interactive
OAuth flow (see "Probe MCP read tools" and "pi-mcp-adapter" above) remains
the only way to get Probe's read tools into pi.

**`/probe-mcp-login`'s OAuth fallback is interactive-only by design, not
merely by omission.** It asks the researcher to paste back the URL they land
on rather than running a local callback server or launching a browser —
both are real infrastructure this package deliberately does not take on (see
"Probe MCP read tools" above), and a browser launch would silently do
nothing useful on the headless/SSH/container pi sessions this project mostly
cares about anyway. The practical implication: on a device with no bearer
token and no interactive pi session available, there is currently no way to
authorize Probe's MCP tools for pi at all — other than installing
pi-mcp-adapter and letting *its* OAuth flow run instead (see
"pi-mcp-adapter" above, which does not have this restriction). This is
expected to be rare — any device already paired with `probe login`/the
wizard for *any* agent already has the bearer token this bridge prefers —
but it is a real, narrower gap than the tap's, flagged here rather than
papered over.

## Development

```bash
npm install
npm run typecheck
npm test
```

`tests/skillsManifest.test.ts` additionally exercises pi's own, real
`DefaultResourceLoader`/`SettingsManager` (from the
`@earendil-works/pi-coding-agent` devDependency) against a scratch
`~/.pi/agent` — it is the test that actually proves the "pi.skills" manifest
resolves, rather than asserting the manifest's shape and trusting pi to
agree with it.

`node:child_process`'s `spawn` is mocked in tests — no test ever launches a
real daemon. Pairing/killswitch tests use real temporary files via
`PROBE_PI_TAP_PLUGIN_DIR`/`PROBE_CONFIG_PATH` overrides, never the real
`~/.pi` state on the machine running the suite.

`tests/mcpBridge.test.ts` never constructs a real `StreamableHTTPClientTransport`
or reaches the network either — it exercises `connectAndRegisterTools()` and
`interactiveOAuthLogin()` entirely against a fake `McpConnector`
(`src/mcpBridge.ts`'s own injectable seam). `tests/mcpAuth.test.ts` and
`tests/extension.test.ts` isolate `PROBE_MCP_TOKEN`/`PROBE_CONFIG_PATH` for
the same reason `PROBE_PI_TAP_TOKEN` is isolated: this dev machine has a real
paired `probe` install, and an un-isolated env var would make these tests
attempt a real connection to the real MCP server.

`tests/adapterHandoff.test.ts` covers D6's stand-down decision against real
temporary `settings.json` files at both scopes — including malformed JSON, a
non-object root, and a non-list `packages` key — never the real `~/.pi`
state on the machine running the suite.
