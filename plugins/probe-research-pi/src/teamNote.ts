/**
 * The team note for pi: read the local file a session should be briefed
 * with, and push local edits + pull the team's latest via a DETACHED
 * `probe notes sync`.
 *
 * INJECT, DO NOT RENDER. Claude Code and Codex get the note by having a
 * managed block rewritten into their global instruction file
 * (`~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md`) at sync time, and reading
 * that file — like any instruction file — at the START of their NEXT
 * session. pi gets the same content by appending it to `event.systemPrompt`
 * in a `before_agent_start` handler instead (see `extension.ts`'s wiring).
 * This sidesteps two things the render path has to deal with: it never
 * writes to a file the researcher owns (an `AGENTS.md` a person edits by
 * hand), and it can never collide with `AGENTS.override.md` shadowing a
 * project's `AGENTS.md` (pi 0.84.3's `resource-loader.js` checks
 * `AGENTS.override.md` before `AGENTS.md` in the same directory — irrelevant
 * to text injected straight into the prompt, and a real hazard for anything
 * written to disk instead).
 *
 * CACHE ONCE PER SESSION. `before_agent_start` fires on every turn; reading
 * and re-parsing the note file that often would be pure waste for content
 * that — by design — only ever changes between sessions (see below). The
 * cache lives in `extension.ts`'s module scope, populated once at
 * `session_start` by `readTeamNote()`, and injected unchanged into every
 * `before_agent_start` of that session by `renderTeamNoteForPrompt()`.
 *
 * ONE SYNC TRIGGER, NOT TWO. Claude Code/Codex split the job across two
 * hooks: `Stop` fires every turn and only pushes (cheap — get this turn's
 * edits out, no network round trip beyond that); `SessionEnd` fires once and
 * does a full reconcile (push, THEN pull, THEN re-render). pi has no
 * `SessionEnd`-shaped event exposed to extensions, and the brief for this
 * work names exactly one hook to use: `agent_settled`, "the analogue of
 * Claude Code's Stop." Rather than only push from `agent_settled` (which
 * would mean a pi machine could send edits out but would never once pull a
 * teammate's back in — the team note's entire premise, "edits sync on their
 * own... two sessions editing at once is fine," depends on the pull half
 * running SOMEWHERE), `spawnTeamNoteSync()` below runs a FULL `probe notes
 * sync` every time. That refreshed copy does not reach the CURRENT session's
 * cache (see "cache once" above) — it reaches the NEXT `session_start`'s
 * read, exactly like a Claude Code edit at session N first reaching the
 * block session N+2 reads (team-note-sync.sh's own comment). This is a
 * deliberate departure from mirroring Stop's push-only cheapness, made
 * because pi's single-trigger design leaves no other opportunity for the
 * pull half to run at all.
 *
 * DETACHED, FOR AN INVERTED REASON. `hooks/team-note-sync.sh`'s `setsid`
 * exists to survive Codex's 3-second `SessionEnd` timeout — the hook's
 * process group would otherwise be killed mid-flight along with the timed-
 * out parent. pi imposes NO timeout at all on an extension's event handlers
 * (verified against pi 0.84.3's `dist/core/extensions/runner.js`: every
 * handler call is `await`ed inside a bare try/catch — no `Promise.race`, no
 * wrapper, no deadline). So detaching here is not about surviving a clock —
 * there isn't one. It is because an in-process, un-detached spawn that hangs
 * (a dead network, a stuck DNS lookup) would hang INSIDE the `agent_settled`
 * handler with nothing to interrupt it, stalling the researcher's actual pi
 * session indefinitely. Same mechanism as Codex's `setsid` (spawn a child,
 * do not wait on it), opposite justification — a future reader who assumes
 * this was copied for the timeout reason would be wrong; say so if this
 * comment ever moves.
 *
 * FAIL OPEN AND SILENT, matching every hook in `probe-research`'s plugin: a
 * missing `probe` CLI, a dead network, or a server-side conflict must all
 * leave the local file exactly as the session left it. Nothing here throws
 * out of `readTeamNote()` or `spawnTeamNoteSync()`.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, isAbsolute, join } from "node:path";

import { teamNoteDocumentPath, type PathEnv } from "./paths.js";

// ---------------------------------------------------------------------------
// Reading the cached note
// ---------------------------------------------------------------------------

export interface TeamNoteReadDeps {
  readFileSync: (path: string) => string;
}

const defaultReadDeps: TeamNoteReadDeps = {
  readFileSync: (path) => readFileSync(path, "utf-8"),
};

/**
 * The local team-note document's text, or `null` when it is absent, empty,
 * or unreadable. NEVER THROWS — a fresh install with no note yet synced, a
 * permissions problem, or a decode error are all just "nothing to brief this
 * session with," not a reason to fail `session_start`.
 */
export function readTeamNote(env: PathEnv = process.env, deps: TeamNoteReadDeps = defaultReadDeps): string | null {
  try {
    const text = deps.readFileSync(teamNoteDocumentPath(env));
    return text.trim().length > 0 ? text : null;
  } catch {
    return null;
  }
}

/**
 * Format the cached note for appending to `event.systemPrompt`. Named the
 * real, absolute file path — the whole point of the team note is that "you
 * edit that file like any other markdown," and an agent that cannot see
 * where the file lives cannot do that.
 */
export function renderTeamNoteForPrompt(note: string, documentPath: string): string {
  return (
    `\n\n## Probe team note\n\n` +
    `The lab's shared memory -- what this team is working on, has decided, and what not to repeat. ` +
    `Edit \`${documentPath}\` directly to change it for the team; edits sync automatically.\n\n` +
    `${note.trim()}\n`
  );
}

// ---------------------------------------------------------------------------
// Spawning the detached sync
// ---------------------------------------------------------------------------

export interface ChildLike {
  pid: number | undefined;
  unref: () => void;
}

export type SpawnFn = (
  command: string,
  args: string[],
  options: { detached: boolean; stdio: "ignore"; env: PathEnv },
) => ChildLike;

export interface ProbeBinaryDeps {
  existsSync: (path: string) => boolean;
  isExecutable: (path: string) => boolean;
  env: PathEnv;
}

/** The two documented install locations `hooks/team-note-sync.sh` also falls
 * back to, in the same order, after a bare `PATH` search comes up empty. */
function fallbackProbePaths(env: PathEnv): string[] {
  // `env.HOME`, not `os.homedir()`: `team-note-sync.sh` interpolates the
  // literal `$HOME` env var, and matching that (rather than a syscall that
  // can disagree with it) is what makes this resolution actually mirror the
  // shell script it is standing in for. Real callers get the same answer
  // either way; tests get full isolation without needing to touch the
  // process's real home directory.
  const home = env.HOME || homedir();
  return [join(home, ".local", "bin", "probe"), join(home, ".local", "share", "uv", "tools", "probe-research", "bin", "probe")];
}

function findOnPath(name: string, deps: ProbeBinaryDeps): string | null {
  const pathVar = deps.env.PATH ?? "";
  // A relative PATH entry (especially `.`) makes a repository-controlled
  // executable part of this extension's startup trust boundary. Only accept
  // absolute install directories; the documented HOME fallbacks below cover
  // the common user-local installs when PATH has no trusted answer.
  for (const dir of pathVar.split(delimiter).filter((entry) => isAbsolute(entry))) {
    const candidate = join(dir, name);
    if (deps.existsSync(candidate) && deps.isExecutable(candidate)) return candidate;
  }
  return null;
}

/**
 * Resolve the `probe` CLI with the same order as `hooks/team-note-sync.sh`:
 * absolute `PATH` entries first, then the two documented fallback install
 * locations. Relative entries are intentionally ignored here because this
 * extension invokes the result automatically during startup. A
 * SEPARATE, smaller lookup from `tapRuntime.ts`'s `findOnPath` (that one
 * resolves a Python interpreter under `PROBE_PI_TAP_ROOT`'s own override
 * chain, for a different package entirely — see its own docstring) — same
 * shape, unrelated domain, not worth sharing across the two.
 */
export function findProbeBinary(deps: ProbeBinaryDeps): string | null {
  const onPath = findOnPath("probe", deps);
  if (onPath) return onPath;
  for (const candidate of fallbackProbePaths(deps.env)) {
    if (deps.existsSync(candidate) && deps.isExecutable(candidate)) return candidate;
  }
  return null;
}

export interface TeamNoteSyncDeps {
  spawn: SpawnFn;
  existsSync: (path: string) => boolean;
  isExecutable: (path: string) => boolean;
  env: PathEnv;
  log: (message: string) => void;
}

/**
 * Fire a detached `probe notes sync` for this pi session and return
 * immediately. See the module docstring for why this is a full sync (not
 * `--push-only`), why it is detached, and why nothing here ever throws.
 *
 * `PROBE_AGENT=pi` is set in the child's environment — mirroring exactly how
 * `hooks.json` exports `PROBE_AGENT=codex` before invoking this same shell
 * script for Codex (Claude Code's own hook leaves it unset and rides the
 * CLI's `claude_code` default). Without it, `agent_rules.memory_path()`
 * inside the CLI would resolve to Claude Code's file even though this call
 * originated from pi, because that function falls back to
 * `os.environ.get("PROBE_AGENT")` whenever it is not told the caller
 * explicitly.
 */
export function spawnTeamNoteSync(deps: TeamNoteSyncDeps): void {
  const binary = findProbeBinary(deps);
  if (!binary) {
    deps.log("team-note sync skipped: no probe CLI found on PATH or in the documented fallback locations");
    return;
  }
  try {
    const child = deps.spawn(binary, ["notes", "sync"], {
      detached: true,
      stdio: "ignore",
      env: { ...deps.env, PROBE_AGENT: "pi" },
    });
    child.unref();
    deps.log(`team-note sync spawned (pid ${child.pid ?? "unknown"})`);
  } catch (err) {
    deps.log(`team-note sync spawn failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}
