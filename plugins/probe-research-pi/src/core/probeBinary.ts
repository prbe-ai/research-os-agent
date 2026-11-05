/**
 * Find the `probe` CLI: absolute `PATH` entries first, then the two documented install locations.
 * Read by the extension's CLI version check (`inlineNotice.ts`) and the tracking state's fallback
 * read (`trackingState.ts`).
 */

import { homedir } from "node:os";
import { delimiter, isAbsolute, join } from "node:path";

import type { PathEnv } from "./paths.js";

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
