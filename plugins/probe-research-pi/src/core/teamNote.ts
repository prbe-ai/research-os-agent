/**
 * The team note, the half every harness shares: read the local document a session is briefed with,
 * decide whether this machine opted in (the pointer block), and find the `probe` CLI. The note's
 * sync and the prompt it goes into are the Probe service's (its `agent_settled` and prompt routes,
 * service-only task 16): nothing here syncs or spawns.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, isAbsolute, join } from "node:path";

import { piAgentsFile, teamNoteDocumentPath, type PathEnv } from "./paths.js";

// ---------------------------------------------------------------------------
// No pointer, no note
// ---------------------------------------------------------------------------

/** `agent_rules.BEGIN_MARKER` / `END_MARKER`: Probe's managed pointer block. */
export const POINTER_BEGIN_MARKER = "<!-- probe-research:begin (managed by `probe wizard`) -->";
export const POINTER_END_MARKER = "<!-- probe-research:end -->";

/**
 * Does pi's `AGENTS.md` carry Probe's pointer block? `agent_rules.has_block`:
 * either marker counts, a damaged block included; an unreadable or missing
 * file has none.
 *
 * THE ABSENT POINTER IS THE OPT-OUT (`probe wizard --no-agent-rules`, cli
 * 0.205.4): the CLI renders the team note only into an instruction file that
 * carries the pointer, and removes it where the pointer is gone. pi gets the
 * note by injection rather than by that render, so the same rule is applied
 * here: no pointer, no note in the prompt and no note sync from this session.
 * A damaged pointer is still somebody's opt-in, as on the CLI's side.
 */
export function hasProbePointer(
  env: PathEnv = process.env,
  read: (path: string) => string = (path) => readFileSync(path, "utf-8"),
): boolean {
  try {
    const text = read(piAgentsFile(env));
    return text.includes(POINTER_BEGIN_MARKER) || text.includes(POINTER_END_MARKER);
  } catch {
    return false;
  }
}

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

// ---------------------------------------------------------------------------
// Spawning the detached sync
// ---------------------------------------------------------------------------

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
