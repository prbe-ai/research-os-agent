# Sandbox begin/end state capture (`probe.sandbox-state/1`)

Capture the filesystem state of a Harbor sandbox at two instants — what the
agent saw at step 0, and what the agent left behind — as ordinary trial
artifacts, with no Harbor fork, no task.toml or Dockerfile changes, and no
changes to the durable capture pipeline. A future dashboard layer parses and
renders the diff; this plan only has to guarantee the bundle it will read is
complete, self-describing, and cheap to parse.

Hard requirements (decided 2026-07-23):

- **Ephemeral**: nothing of ours exists inside the container during the
  agent phase. The agent and environment are unaffected except for two
  bounded, read-only scans that run strictly outside the agent's execution
  window.
- **Tamper-resistant**: the agent (or processes it leaves behind) must not
  be able to forge, corrupt, or silently suppress the capture without
  detection.
- **Zero image dependencies**: no `python3`, no coreutils, no tar in the
  task image. The dependency floor is `env.exec` itself (an execable shell,
  which every Harbor agent already requires de facto).

Motivating gap (see `2026-07-22-harbor-export-ledger.md`, "Completeness
boundary"): today's capture claim is `scope=host_trial_directory` — sandbox
state Harbor never materialized to the host is explicitly unknown. Public
Harbor deletes the environment before `Trial.run()` returns, so the only
correct place to observe sandbox state is from inside the container, before
teardown, at instants we control.

## Why the Dockerfile is not the begin state

The Dockerfile is a recipe, not a state: builds are non-deterministic
(mutable base tags, apt/pip resolution, network fetches), and the sandbox
the agent actually starts in diverges further after image build — compose
entrypoints and sidecar init run, Harbor uploads injected skills and
installs the agent (`Trial._prepare()`), all before the agent's first
action. The authoritative begin state is therefore a runtime observation at
the `AGENT_START` instant. The image digest is recorded as provenance only.

## Hook surface (verified against harbor-framework/harbor @ main)

| Fact | Source |
| --- | --- |
| `Trial.add_hook(event, callback)` is public | `src/harbor/trial/trial.py:321` |
| Events: `START`, `ENVIRONMENT_START`, `AGENT_START`, `AGENT_END`, `VERIFICATION_START`, `END`, `CANCEL` | `src/harbor/trial/hooks.py` |
| `AGENT_END` emits in `finally:` — fires on success, timeout, agent crash | `src/harbor/trial/trial.py:488` |
| `AGENT_END` precedes verification and all teardown in **both** verifier modes | `src/harbor/trial/single_step.py:38-61` |
| Hooks are awaited inline — `AGENT_START` hook completes before the agent's first action | `src/harbor/trial/trial.py:359,455` |
| `_emit` does NOT catch hook exceptions — fail-open is our responsibility | `src/harbor/trial/trial.py:359` |
| Host→sandbox reach: `env.upload_file`, `env.download_file`, `env.exec(command, user=...)` | `src/harbor/environments/base.py:917,937,1128` |

The hook API is source-level public but **undocumented**: Harbor's own
`job.py` depends on it, so it is load-bearing upstream, but we pin the
Harbor version and add a canary test that asserts the API shape so an
upgrade breaks loudly.

`AGENT_END` is deliberately chosen over `[[verifier.collect]]` hooks: collect
timing shifts with verifier mode (separate = pre-verifier, shared =
post-verifier), so shared-mode deltas would include `test.sh` side effects.
`AGENT_END` is mode-invariant and means exactly "the state the agent left."

Deferred by decision: no `t_env` epoch marker. Begin manifest at
`AGENT_START` fully defines t0. Setup-drift attribution and byte-level t0
reconstruction can be added later inside the snapshot tool (container start
time is derivable from `/proc/1`) without changing this design.

## The snapshot tool: a static binary, not a script

`probe-sandbox-snapshot`: a single **statically linked Go binary**
(`CGO_ENABLED=0`, stdlib only — filesystem walk, JSONL encoding, gzip,
sha256, tar are all in the Go standard library). Built for `linux/amd64`
and `linux/arm64` in research-os-agent CI, shipped as package data in the
`probe-research` wheel (`src/probe/connectors/_bin/`, ~3 MB per arch); the
bridge selects the arch and uploads it per-phase via `importlib.resources`.

This removes every image dependency at once — no interpreter, no
coreutils — and is strictly more robust than shell tooling: paths are
JSON-encoded (the sandbox filesystem is agent-authored and must be treated
as adversarial input, so hostile filenames are a first-class case), and
walking + hashing in one process is far faster than `find | xargs sha256sum`
pipelines.

Subcommands:

```text
probe-sandbox-snapshot begin --workdir <random-tmp>
probe-sandbox-snapshot end   --workdir <random-tmp> --begin-manifest <path>
```

Both print a small JSON trailer to stdout: the sha256 and size of every
file they wrote, plus scan stats and errors. Stdout is the bridge's
integrity side-channel — it never round-trips through the container
filesystem.

## Ephemerality protocol

The container is probe-free during the agent phase, and probe-free again
before the trial proceeds. Everything transient lives under one
random, per-phase workdir (`/tmp/.psbx-<uuid4>/`) that is deleted in the
same hook that created it.

```text
AGENT_START hook (runs to completion before the agent's first action):
  1. upload binary            → /tmp/.psbx-<r1>/snap
  2. exec begin (root)        → writes begin-manifest.jsonl.gz in workdir,
                                prints {file: sha256} trailer to stdout
  3. download begin-manifest  → HOST memory/disk (bridge-held)
  4. verify sha256 vs trailer
  5. exec rm -rf /tmp/.psbx-<r1>   → container is probe-free

  [ agent runs — zero probe footprint in the container ]

AGENT_END hook (agent already finished; fires even on timeout/crash):
  1. upload binary            → /tmp/.psbx-<r2>/snap        (fresh copy —
                                nothing persisted, so nothing to swap)
  2. upload begin-manifest    → /tmp/.psbx-<r2>/begin.jsonl.gz (from host)
  3. exec end (root)          → end-manifest.jsonl.gz + end-delta.tar.gz,
                                sha256 trailer to stdout
  4. download both            → HOST
  5. verify sha256s vs trailer
  6. exec rm -rf /tmp/.psbx-<r2>
  7. bridge writes the bundle + meta.json into
     <trial_dir>/artifacts/probe-sandbox-state/  ON THE HOST
```

Step 7 is a deliberate transport change from the earlier draft: the bundle
never touches `/logs/artifacts/` and does not rely on Harbor's collection
pass at all. The bridge already owns the host trial tree (it stages it via
`stage_trial_export` in the `Trial.run()` finally path — PR #38 / Miles
#4); it simply places the bundle there before staging. Benefits: no
in-container residue after AGENT_END, no dependence on verifier-mode
collection ordering, and `meta.json` is authored **host-side** — outside
agent reach by construction.

Environment impact ledger: two bounded root execs (read-only scans +
tar-write into the doomed workdir), transient CPU/IO outside the agent
window, and nothing else. No files during the agent phase, no environment
variables, no processes left running, no writes outside the workdir.

## Tamper model

| Attack | Defense |
| --- | --- |
| Agent discovers instrumentation (distribution shift / reward hacking) | Nothing exists in the container during the agent phase to discover |
| Agent replaces the snapshot tool between begin and end | Tool is not present between begin and end; a fresh copy is uploaded at AGENT_END |
| Agent pre-poisons begin outputs | Begin outputs are downloaded, verified against the stdout trailer, and deleted before the agent starts |
| Leftover agent daemon rewrites end outputs before pickup (shared verifier mode keeps the container alive) | Unpredictable workdir name; seconds-wide window; sha256 trailer via stdout (not the filesystem) verified host-side after download — tamper is detected and recorded, bundle marked accordingly |
| Leftover daemon forges files *before* the end scan reads them | Out of scope: indistinguishable from legitimate agent writes. The end state is honestly "the state of the filesystem at AGENT_END" |
| meta.json forgery | meta.json is written host-side only |

Residual risk is detection-not-prevention for the daemon race, and
"filesystem at AGENT_END includes anything daemons wrote" — both recorded
limitations, not silent ones.

## The bundle contract: `probe.sandbox-state/1`

Written by the **bridge, host-side**, to
`<trial_dir>/artifacts/probe-sandbox-state/`. From there it is ordinary
trial-tree bytes: `stage_trial_export` hashes it, the exporter ships it
content-addressed, the completeness ledger covers it. **No pipeline
changes.**

```text
probe-sandbox-state/
├── begin-manifest.jsonl.gz   # scanned at AGENT_START
├── end-manifest.jsonl.gz     # scanned at AGENT_END
├── end-delta.tar.gz          # bytes of files added or modified during the agent phase
└── meta.json                 # authored host-side; written last; its presence marks the bundle complete
```

Renderer recognition is by path (`**/probe-sandbox-state/meta.json` with
`schema == "probe.sandbox-state/1"`), mirroring how the dashboard
recognizes `probe.capture/1` — no exporter or backend schema changes.

### Manifest format

One JSON object per line, gzipped, sorted by `path` bytewise (enables
streaming merge-diff at render time). JSON encoding makes hostile paths
(newlines, tabs, invalid UTF-8) a non-issue.

The binary emits entries in walk order; **sorting happens host-side** in
the bridge before the bundle is written. This keeps the in-container
memory profile flat (streaming walk, no full-tree sort buffer) so the scan
cannot pressure small-memory containers even at the 2M-file guard.

```json
{"p": "/workspace/repo/main.py", "t": "f", "s": 4096, "m": 1753280000.12, "mode": "100644", "u": 0, "g": 0}
{"p": "/workspace/link", "t": "l", "lt": "/workspace/repo"}
```

`t` is `f|d|l` (regular/dir/symlink); other types (sockets, fifos, devices)
are inventoried with their type letter and never archived. Optional `h`
(sha256) appears when hashing is enabled. Fields are documented in
`meta.json.manifest_fields` so the parser never guesses.

### Delta computation (no mtime trust)

`end` loads the re-uploaded begin manifest and computes the changed set by
comparison, not by mtime heuristics:

- **added**: path in end, not in begin
- **modified**: size or mtime differs (or hash, when enabled)
- **deleted**: path in begin, not in end — derived, not stored; renderers
  recompute it from the two manifests

`end-delta.tar.gz` contains added + modified regular files and symlink
entries. Fast mode (default, size+mtime) can miss a timestamp-preserving
same-size edit; `hash` mode closes that at scan cost. The mode used is
recorded in `meta.json`.

Memory: the `end` phase holds the begin manifest as a compact lookup index
(path-hash → size/mtime, ~40 B/entry): ~10–20 MB for a typical 200–400k-file
image, ~100 MB at the 2M-file guard. If smoke tests show pressure on
small-memory containers, the escape hatch is host-computed diff: download
the end manifest first, compute the changed-set in the bridge, and upload a
path list for a "tar exactly these" third exec — O(1) container memory at
the cost of one extra round-trip. Not v1 default.

### meta.json (host-authored)

```json
{
  "schema": "probe.sandbox-state/1",
  "tool": {"name": "probe-sandbox-snapshot", "version": "0.1.0", "arch": "amd64"},
  "begin_at": "2026-07-23T20:11:04Z",
  "end_at": "2026-07-23T20:19:41Z",
  "scan": {"mode": "fast", "exclude": ["/proc", "/sys", "/dev"], "one_filesystem": true},
  "summary": {"begin_files": 184223, "added": 312, "modified": 41, "deleted": 6, "delta_bytes": 18744320},
  "limits": {"max_files": 2000000, "max_delta_bytes": 2147483648, "truncated": false, "dropped": []},
  "integrity": {"begin_verified": true, "end_verified": true},
  "errors": []
}
```

`summary` exists so the dashboard can show "+312 / ~41 / -6, 18 MB" without
downloading a manifest. `limits` implements no-silent-caps: anything
dropped by a guard is named in `dropped`. `integrity` records the
trailer-vs-download sha256 verification results. A bundle without a valid
`meta.json` is by definition incomplete and renderers must say "capture
failed", never invent a diff.

### Scan rules

- Root `/`, one-filesystem (skips foreign mounts), excluding `/proc`,
  `/sys`, `/dev`, and the workdir itself. `/tmp` and `/logs` are
  **included** in manifests (agent state is agent state); the delta tar
  excludes `/logs` bytes (Harbor already captures agent logs). Extra
  exclusions via `MILES_SANDBOX_STATE_EXCLUDE`.
- Runs as root (`env.exec(user="root")`) for full read access.
- Guards: `max_files` (default 2M), `max_delta_bytes` (default 2 GiB)
  **further capped at 50% of the workdir filesystem's free space** (statvfs
  at scan start) — the delta tar is written inside the container before
  download, and exhausting container disk post-agent could fail the
  verifier, which would violate "environment unaffected". Any cap hit is
  recorded in `limits.dropped`. Per-file unreadable → recorded in
  `errors`, scan continues.

## Bridge wiring

`examples/experimental/swe-agent-v2/public_harbor_server.py` (miles repo,
integration branch) — the component that already constructs the `Trial`
and owns the finally-path `stage_trial_export` call.

Decisions:

- **Opt-in flag** `MILES_SANDBOX_STATE=1` (bridge env; default off for the
  first rollout). No Harbor config schema involvement. Composes with the
  capture modes from miles PR #7: sandbox-state requires
  `MILES_HARBOR_CAPTURE_MODE != off` (the bundle rides the staged
  capture), and a contradictory config fails loudly at startup rather
  than silently capturing nothing.
- **Arch selection**: one `uname -m` exec at AGENT_START (falls back to
  amd64 on failure; wrong-arch exec fails fast and is recorded).
- **Timeouts** (`asyncio.wait_for` around each phase's full
  upload→exec→download→cleanup sequence): defaults 120 s begin / 300 s end
  via `MILES_SANDBOX_STATE_TIMEOUT_*`. Snapshot time is inline trial
  wall-clock — bounded by construction.
- **Every exception swallowed** inside the callbacks. Verified: Harbor's
  `_emit` propagates hook exceptions, and `AGENT_END` fires inside a
  `finally:` — an unhandled raise there would mask the trial's own result.
  Cleanup (`rm -rf` of the workdir) runs in its own guarded finally so a
  failed download still leaves the container clean.
- **Status flows into `capture_context`** (the existing opaque context on
  the export request) as `sandbox_state: {begin, end, integrity, ...}`
  plus the task's image reference from `TrialConfig` as provenance — so a
  failed or tampered snapshot is diagnosable from the run page without
  opening bundles.
- On `CANCEL`/dead-environment, the end sequence raises, is swallowed, and
  the bridge writes whatever it holds (possibly only the begin manifest)
  plus a meta.json with the error — a partial-but-honest bundle.
  `asyncio.CancelledError` is deliberately NOT caught (it is a
  BaseException): a second cancellation must unwind normally; the trial is
  already dead and losing the end snapshot there is correct.

Process topology, to be explicit: Harbor's `Trial` is an in-process object
inside the bridge's asyncio loop, not a service. `add_hook` is passive
registration; activation is Harbor's own `_emit(event)` awaiting our
callback inline, so Harbor is structurally unable to start the agent (or
the verifier) until the corresponding snapshot completes. The bridge never
enters the sandbox; the sandbox only ever hosts the snapshot binary,
twice, outside the agent's lifetime.

Failure domain: the host-held begin manifest is per-trial state in the
bridge process. If the bridge dies mid-trial, the trial dies with it
(already true today) and the capture dies too — same failure domain as the
existing staging path, no new risk class.

## End-to-end flow

```text
AGENT_START hook ── upload/exec/download/verify/delete ──► begin manifest held on HOST
agent works …                                              (container probe-free)
AGENT_END hook ──── upload/exec/download/verify/delete ──► end manifest + delta held on HOST
bridge writes bundle + meta.json into <trial_dir>/artifacts/probe-sandbox-state/
Trial.run() returns ► bridge finally-path stage_trial_export (unchanged, PR #38)
probe trial watch ► hashes verified, uploaded content-addressed, ledger complete (unchanged)
dashboard (future) ► recognizes probe.sandbox-state/1, renders diff from the two manifests
```

Storage note for the visualize phase: begin manifests for the same
task+agent are near-identical across trials and the artifact store is
content-addressed, so repeated captures dedupe; delta tarballs are
proportional to agent work, not image size.

## Failure modes

| Failure | Behavior |
| --- | --- |
| No execable shell in image (distroless) | begin exec fails; recorded; trial unaffected; no bundle |
| Unsupported arch | recorded at AGENT_START; no bundle; trial unaffected |
| Scan exceeds file/byte guard | truncation recorded in `meta.json.limits.dropped`; bundle still valid |
| Trailer/download sha256 mismatch (tamper race) | `integrity.*_verified=false` in meta.json; bundle kept; surfaced in capture_context |
| End sequence timeout / env dead at AGENT_END | host writes partial bundle (begin manifest + error meta.json) |
| Hook raises for any other reason | swallowed in callback; container cleanup still attempted; error in capture_context |
| Harbor upgrade changes hook API | pinned version + canary test fails in CI, loudly |

## Out of scope (v1)

- `t_env` / setup-drift attribution and byte-level t0 reconstruction
  (additive later inside the snapshot tool; contract unchanged).
- Multi-step trials (`multi_step.py`) — per-step event semantics
  unverified; SWE-Agent v2 is single-step. Bridge installs hooks only for
  single-step trials.
- In-memory-only sandbox state (processes, unwritten buffers) — no
  filesystem capture can see it.
- Preventing (vs detecting) the leftover-daemon race in shared verifier
  mode.
- The dashboard renderer itself — designed-for here (path recognition,
  `summary` stats, sorted-manifest streaming diff, host-authored
  `meta.json` as the completeness marker), built as its own research-os
  change following the #114 pattern.

## Work items

1. **research-os-agent** — `tools/sandbox-snapshot/` Go source (stdlib
   only), reproducible builds (`-trimpath`, stripped) via
   `scripts/build-sandbox-snapshot.sh`, **binaries committed** at
   `src/probe/connectors/_bin/` (~3 MB × 2 arches) so `pip install` from
   git works out of the gate with no Go toolchain in the install path;
   helper module `src/probe/connectors/sandbox_state.py` (binary path
   lookup, trailer parsing, host-side manifest sort, atomic bundle/meta
   authoring — bridge stays thin) + this contract. Tests: Go tests for
   begin/end/delta correctness on a temp tree, deletions derived, hostile
   filenames, unreadable files, guard truncation, stdout trailer accuracy;
   Python tests for the helper; a packaging test asserting the binaries
   ship in the wheel.
2. **miles** (integration branch) — bridge hooks implementing the
   ephemerality protocol (upload/exec/download/verify/delete per phase),
   host-side bundle + meta.json authoring into the trial tree, opt-in
   flag, timeouts, `capture_context.sandbox_state`, canary test asserting
   `TrialEvent`/`add_hook`/`env.exec`/`upload_file`/`download_file` shapes
   against the pinned Harbor. Tests alongside
   `test_public_harbor_server.py` with a fake environment, including
   "cleanup runs even when download fails" and "hook never raises".
3. **Smoke** — one live Harbor trial with `MILES_SANDBOX_STATE=1`: verify
   the bundle lands in the staged trial tree, survives stage → export,
   dashboard shows the files, `summary` counts match a hand-checked diff,
   and an in-container `ls /tmp` mid-agent-phase shows no probe files.
4. **Docs** — NEBIUS_E2E.md gains the flag + a "verify the bundle" step.
5. **Later** — research-os renderer (own plan); Harbor upstream proposal
   for first-class environment-state capture (would let us delete the
   exec choreography entirely).

## Open questions

- Default guards: are 2M files / 2 GiB delta the right ceilings for
  SWE-bench-class images? Revisit after first smoke numbers.
- Should `hash` mode be the default for training runs where reward hacking
  via timestamp-preserving edits is a live concern? (Cost: full-tree
  sha256 at both instants.)
- Multi-step: do `AGENT_START`/`AGENT_END` fire per step? Verify in
  `multi_step.py` before extending.
- Go toolchain in research-os-agent CI is new surface — confirm we're ok
  adding it (alternative: prebuilt binaries committed via LFS, worse).
