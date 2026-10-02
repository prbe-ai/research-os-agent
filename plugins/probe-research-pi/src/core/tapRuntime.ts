/**
 * Locates a Python interpreter that can `import tap` — the probe-research-tap
 * daemon package this extension spawns.
 *
 * This is the one piece of the install surface the task description didn't
 * spell out: probe-research-tap is a SEPARATE, independently pip-installable
 * package (see its pyproject.toml — no dependencies, plain setuptools), not
 * something this npm package vendors or bundles. Claude Code and Codex don't
 * have this problem because their marketplace plugin installs ship `tap/`
 * bundled next to the hook script that spawns it (see
 * `hooks/session-start.sh`'s `PLUGIN_ROOT`). pi has no such bundling — a pi
 * extension is pure TypeScript loaded by pi's own jiti-based loader, and
 * there is currently no packaging step that would place a Python package
 * alongside it.
 *
 * Resolution order:
 *   1. `PROBE_PI_TAP_ROOT` env — an explicit path to a probe-research-tap
 *      checkout (its `tap/` package importable via PYTHONPATH). The
 *      documented, supported override for any install shape.
 *   2. The sibling `probe-research-tap/` directory next to this package —
 *      true ONLY inside this monorepo checkout (`agent/plugins/probe-research-pi`
 *      and `agent/plugins/probe-research-tap` are siblings). This is a
 *      development-time convenience, not a real distribution story: once
 *      this package is installed independently (`pi install npm:...`), that
 *      sibling directory will not exist. See README "Known limitation."
 *   3. Bare `python3`/`python` on PATH with no PYTHONPATH override, on the
 *      assumption `probe-research-tap` was `pip install`-ed into the active
 *      environment.
 *
 * Each step needs a working Python interpreter; if none is found anywhere,
 * resolution fails and the caller refuses to spawn (loudly, once) rather
 * than starting a daemon that would immediately exit -- see daemon.ts.
 */

import { delimiter, join } from "node:path";

import type { PathEnv } from "./paths.js";

export interface TapRuntime {
  /** Absolute path (or bare command name resolved via PATH) to invoke as the interpreter. */
  python: string;
  /** When set, prepended to PYTHONPATH so `python -m tap` resolves to this checkout. */
  tapRoot?: string;
}

export interface TapRuntimeDeps {
  existsSync: (path: string) => boolean;
  isExecutable: (path: string) => boolean;
  env: PathEnv;
  /** Directory this extension package lives in (its own `index.ts`'s directory). */
  extensionDir: string;
}

const ENV_TAP_ROOT = "PROBE_PI_TAP_ROOT";

function looksLikeTapCheckout(root: string, deps: TapRuntimeDeps): boolean {
  return deps.existsSync(join(root, "tap", "__init__.py"));
}

function venvPython(root: string, deps: TapRuntimeDeps): string | null {
  const candidate = join(root, ".venv", "bin", "python3");
  return deps.existsSync(candidate) && deps.isExecutable(candidate) ? candidate : null;
}

function findOnPath(names: string[], deps: TapRuntimeDeps): string | null {
  const pathVar = deps.env.PATH ?? "";
  const dirs = pathVar.split(delimiter).filter(Boolean);
  for (const name of names) {
    for (const dir of dirs) {
      const candidate = join(dir, name);
      if (deps.existsSync(candidate) && deps.isExecutable(candidate)) {
        return candidate;
      }
    }
  }
  return null;
}

/** Resolve a usable {python, tapRoot}, or null if no interpreter can be found at all. */
export function resolveTapRuntime(deps: TapRuntimeDeps): TapRuntime | null {
  const candidateRoots: string[] = [];
  const override = deps.env[ENV_TAP_ROOT];
  if (override && override.trim()) candidateRoots.push(override.trim());
  candidateRoots.push(join(deps.extensionDir, "..", "probe-research-tap"));

  for (const root of candidateRoots) {
    if (!looksLikeTapCheckout(root, deps)) continue;
    const python = venvPython(root, deps) ?? findOnPath(["python3", "python"], deps);
    if (python) return { python, tapRoot: root };
  }

  // No known checkout found — last resort: trust an already-`pip install`-ed `tap`.
  const python = findOnPath(["python3", "python"], deps);
  if (python) return { python };

  return null;
}
