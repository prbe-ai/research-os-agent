# Begin-state bytes: shared per-task before-content for probe.sandbox-state/1

Extends `docs/2026-07-23-sandbox-state-capture.md`. That design captures the
begin state as **metadata only** (`begin-manifest.jsonl.gz`) and archives bytes
only for the *end* side of the diff (`end-delta.tar.gz` = added+modified end
bytes). Consequences today: no true line-diff of modified files, deleted-file
contents are unrecoverable, and the dashboard renders "before state not
captured". This doc adds begin-state **bytes** without breaking the original
design's guarantees (ephemerality, tamper-evidence, fail-open, bounded
resources).

## Decision summary

| Decision | Choice |
| --- | --- |
| Sharing unit | Per `(run, task)` — the **first trial per task** captures bytes; every other rollout of that task references it |
| Dedupe key | `task_checksum` (Harbor's checksum of the task definition), carried as an opaque `begin_bytes_ref` |
| Byte scope | **Same as the manifest scope** (`--root` + excludes). No separate prefix knob |
| Validity | Per-file, per-trial: begin-manifest sha256 (`--hash`) vs archive member — verified, never assumed |
| Image digests | Provenance only, unchanged from the 07-23 design ("the Dockerfile is a recipe, not a state") |

## Why sharing is sound (and what "sound" means here)

Every trial gets a fresh container, but all rollouts of one task
(`instance_id` → task dir → image) boot the same recipe; within a run the
built image is reused. The begin *content* is therefore ~identical per task —
except for files Harbor/docker write per container (`/etc/hostname`,
`/etc/hosts`, `/etc/resolv.conf`, setup logs). So identity (`task_checksum`)
is only the **lookup key** deciding which archive to consult. Correctness
comes from the verification layer: with `hash_files` on, *every* trial's own
begin manifest carries a per-file sha256 taken at its own `AGENT_START`.
Serving "before" bytes for trial N out of trial M's archive is allowed iff
N's begin-manifest hash for that path equals the archive member's hash.
Mismatch or absence degrades per-file to "before content unavailable" —
never a wrong diff. mtime noise is invisible to this check by construction.

## In-sandbox tool (`tools/sandbox-snapshot`)

New behavior on the `begin` subcommand only:

```
begin --workdir DIR [--bytes] [--max-begin-bytes N] [existing flags]
        -> DIR/begin-manifest.jsonl.gz [+ DIR/begin-bytes.tar.gz]
```

- `--bytes` tees every inventoried regular file and symlink into a streamed
  `begin-bytes.tar.gz`, reusing the end phase's archive writer (gzip → tar,
  counting-hash tee, byte budget, drop accounting). One walk, no second pass.
- `--max-begin-bytes` (default 32 GiB) is the uncompressed byte budget,
  further capped at 50% of the workdir's free space exactly like the delta
  budget. Overflow drops files (recorded in `dropped`/`dropped_count`,
  `truncated=true`) — a partial archive stays honest and usable.
- The trailer gains `files["begin-bytes.tar.gz"]` (sha256+size) and
  `stats["begin_bytes_budget_bytes"]`; integrity travels by the existing
  stdout side-channel.
- The name is `begin-bytes.tar.gz`, not `begin-delta…`: it is a full archive
  of the scanned scope, not a diff.

Scope note: with the manifest root at `/` the archive is roughly the image
size (single-digit GiB compressed for SWE-class images). That is paid **once
per task per run**, not per trial. The pressure valves are all existing
flags: `--exclude` (shared with the manifest — an excluded path drops from
metadata too, an accepted trade), the byte budget, and narrowing `--root`.

## Host side (`sandbox_state.py`, `harbor_runner.py`)

- `BEGIN_BYTES = "begin-bytes.tar.gz"` joins the bundle contract. It rides
  `write_bundle` verbatim (copy, no sort) like `END_DELTA`.
- `SandboxStateOptions` gains:
  - `root: str = "/"` — plumbs the binary's existing `--root` flag (until
    now hardcoded by omission).
  - `begin_bytes: bool = False` — first-trial-per-task capture switch. The
    **caller** (bridge) owns the per-task ledger; the SDK stays policy-free.
  - `begin_bytes_ref: str | None = None` — opaque task identity
    (`task_checksum`) stamped into `meta.json` so renderers can find the
    sharing group. Set on *every* trial of the task, captured or not.
  - `max_begin_bytes: int | None = None` — plumbs `--max-begin-bytes`.
  - `begin_timeout_sec` default becomes `None` → resolved to 120 s, or
    600 s when `begin_bytes` is on (tar+gzip+download of GiBs cannot fit
    120 s; explicit values are always honored).
- `_begin` downloads and sha256-verifies **every** file in the begin
  trailer (the generic loop `_end` already uses), so the archive inherits
  the same tamper-evidence as the manifests. `begin_verified` stays the
  AND of all begin outputs.
- `meta.json` gains a `begin_bytes` block:

```json
"begin_bytes": {
  "captured": true,          // this trial produced begin-bytes.tar.gz
  "ref": "<task_checksum>",  // sharing key; null when caller passed none
  "budget_bytes": 34359738368,
  "truncated": false,
  "dropped_count": 0
}
```

Trials that skip capture still get `{"captured": false, "ref": ...}` —
the renderer resolves `ref` to the capturing trial's archive within the run.

## What stays out of this PR (landing order)

Mirrors the capture-facade sequence:

1. **This PR (SDK)**: binary + host helpers + recorder + options.
2. **miles bridge**: per-`(run, task_checksum)` claim ledger in
   `public_harbor_server.py` — first rollout of a task sets
   `begin_bytes=True`, everyone gets `begin_bytes_ref`. Claim races are
   harmless: the server's content-addressed blob store (`have/need`)
   absorbs duplicate uploads; duplicate capture work is wasted, not wrong.
3. **research-os server**: bundle resolution learns `begin_bytes.ref`
   (resolve the capturing trial's archive within the run), `?phase=begin`
   on `…/sandbox-state/file`, `has_begin_content` + per-file hash-verified
   validity on diff entries.
4. **dashboard**: true before/after line diffs for modified files,
   contents for deleted files.

## Failure modes (extends the 07-23 table)

| Failure | Behavior |
| --- | --- |
| Byte budget / free-space cap hit | Partial archive, `truncated` + `dropped_count` recorded; manifests unaffected |
| Archive sha256 mismatch vs trailer | `begin_verified=false`, file kept, error recorded — same policy as manifests |
| Begin timeout with bytes on | Begin fails as today (recorded, no bundle); next rollout of the task can claim capture |
| Caller never sets `begin_bytes` | Behavior is bit-for-bit today's: no new flag, no new file, no meta block change beyond `captured:false` when a ref is supplied |
