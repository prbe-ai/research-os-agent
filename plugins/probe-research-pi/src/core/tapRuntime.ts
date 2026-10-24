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
 *      inside this monorepo checkout AND in a mirror install, which renders
 *      the two as siblings. See README "Known limitation."
 *   3. No checkout: an interpreter with no PYTHONPATH override, on the
 *      assumption `probe-research-tap` was `pip install`-ed into it.
 *
 * WHICH INTERPRETER: one the tap runs on, Python 3.11 or newer (enum.StrEnum;
 * `tap/start.py` MIN_PYTHON), asked in `hooks/tap-python.sh`'s order (#3338),
 * the first that answers:
 *   a. the checkout's own `.venv/bin/python3` (a developer checkout);
 *   b. `python3` on PATH (then `python`, which pi has always accepted);
 *   c. the Probe CLI's interpreter: the one the service's runtime.json names
 *      (the CLI writes it at every start), else where `uv tool install
 *      probe-research` puts it. The Probe service already runs the tap there;
 *   d. `python3.14` .. `python3.11` on PATH.
 * Taking the first `python3` on PATH, as pi did, ran the tap on Apple's 3.9 on
 * a Mac: every `-m tap` died on an ImportError and no pi session was recorded.
 * Each candidate costs one `python -S` start (~10 ms), asked once per pi
 * process (`pythonRunsTap`).
 *
 * If none answers, resolution fails and the caller refuses to spawn (loudly,
 * once, naming what it found: `pythonSeen`) rather than starting a daemon that
 * would immediately exit -- see daemon.ts.
 */

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { delimiter, isAbsolute, join } from "node:path";

import { serviceRuntimeFile, uvToolPython, type PathEnv } from "./paths.js";

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
  /** Does this interpreter run the tap (3.11+)? Defaults to asking it (`pythonRunsTap`). */
  runsTap?: (python: string) => boolean;
  /** Reads the service's runtime.json. Defaults to `fs.readFileSync`. */
  readFileSync?: (path: string) => string;
  /** Defaults to `process.platform`. */
  platform?: NodeJS.Platform;
}

const ENV_TAP_ROOT = "PROBE_PI_TAP_ROOT";

/** `tap/start.py` MIN_PYTHON: the oldest Python the tap runs on. */
export const MIN_TAP_PYTHON = [3, 11] as const;
/** On PATH, the interpreters pi has always tried first (b). */
const PATH_PYTHONS = ["python3", "python"] as const;
/** `tap-python.sh`'s last resort (d), newest first. */
export const VERSIONED_PYTHONS = ["python3.14", "python3.13", "python3.12", "python3.11"] as const;
/** How long one interpreter may take to say its version. */
const VERSION_PROBE_TIMEOUT_MS = 5_000;
/** `-S`: no site-packages, so the probe costs a bare interpreter start. */
const VERSION_PROBE = `import sys; sys.exit(0 if sys.version_info >= (${MIN_TAP_PYTHON.join(", ")}) else 1)`;
const SEEN_PROBE = 'import sys; print("%d.%d.%d" % sys.version_info[:3])';
/** `tap-python.sh::tap_python_seen`'s words for what `python3` on PATH is. */
export const PYTHON_SEEN_NONE = "no python3";
export const PYTHON_SEEN_BROKEN = "a python3 that does not run";
export const PYTHON_SEEN_PREFIX = "python3 ";

/** Verdicts, per interpreter path, for this pi process: interpreters do not change under it. */
const verdicts = new Map<string, boolean>();

/** Is `python` 3.11 or newer? It is asked (`python -S -c ...`), once per path per process. */
export function pythonRunsTap(python: string): boolean {
  const known = verdicts.get(python);
  if (known !== undefined) return known;
  let ok = false;
  try {
    execFileSync(python, ["-S", "-c", VERSION_PROBE], { stdio: "ignore", timeout: VERSION_PROBE_TIMEOUT_MS, windowsHide: true });
    ok = true;
  } catch {
    ok = false;
  }
  verdicts.set(python, ok);
  return ok;
}

function looksLikeTapCheckout(root: string, deps: TapRuntimeDeps): boolean {
  return deps.existsSync(join(root, "tap", "__init__.py"));
}

/** The first `name` on PATH that is an executable file, as `command -v` finds it. */
function firstOnPath(name: string, deps: Pick<TapRuntimeDeps, "existsSync" | "isExecutable" | "env">): string | null {
  for (const dir of (deps.env.PATH ?? "").split(delimiter).filter(Boolean)) {
    const candidate = join(dir, name);
    if (deps.existsSync(candidate) && deps.isExecutable(candidate)) return candidate;
  }
  return null;
}

/** The Probe CLI's interpreters (c): runtime.json's, then uv's. */
function cliPythons(deps: TapRuntimeDeps): string[] {
  const found: string[] = [];
  try {
    const text = (deps.readFileSync ?? ((path: string) => readFileSync(path, "utf-8")))(serviceRuntimeFile(deps.env, deps.platform));
    const python = (JSON.parse(text) as { python?: unknown } | null)?.python;
    if (typeof python === "string" && isAbsolute(python)) found.push(python);
  } catch {
    // No runtime.json (a CLI that has not run since its install), or not one we can read.
  }
  found.push(uvToolPython(deps.env, deps.platform));
  return found;
}

/** The interpreters to ask, in `tap-python.sh`'s order; `root` is the tap checkout, if any. */
function candidates(root: string | null, deps: TapRuntimeDeps): string[] {
  const onPath = (names: readonly string[]): string[] =>
    names.map((name) => firstOnPath(name, deps)).filter((p): p is string => p !== null);
  return [
    ...(root ? [join(root, ".venv", "bin", "python3")] : []),
    ...onPath(PATH_PYTHONS),
    ...cliPythons(deps),
    ...onPath(VERSIONED_PYTHONS),
  ];
}

/** The first candidate that is there, executable, and 3.11 or newer. */
function pickPython(root: string | null, deps: TapRuntimeDeps): string | null {
  const runsTap = deps.runsTap ?? pythonRunsTap;
  const asked = new Set<string>();
  for (const candidate of candidates(root, deps)) {
    if (asked.has(candidate)) continue;
    asked.add(candidate);
    if (deps.existsSync(candidate) && deps.isExecutable(candidate) && runsTap(candidate)) return candidate;
  }
  return null;
}

/** Resolve a usable {python, tapRoot}, or null if no interpreter the tap runs on can be found. */
export function resolveTapRuntime(deps: TapRuntimeDeps): TapRuntime | null {
  const candidateRoots: string[] = [];
  const override = deps.env[ENV_TAP_ROOT];
  if (override && override.trim()) candidateRoots.push(override.trim());
  candidateRoots.push(join(deps.extensionDir, "..", "probe-research-tap"));

  for (const root of candidateRoots) {
    if (!looksLikeTapCheckout(root, deps)) continue;
    const python = pickPython(root, deps);
    if (python) return { python, tapRoot: root };
  }

  // No known checkout found — last resort: trust an already-`pip install`-ed `tap`.
  const python = pickPython(null, deps);
  if (python) return { python };

  return null;
}

/**
 * What `python3` on PATH is, for the researcher when no interpreter runs the
 * tap: "python3 3.9.6", "no python3", or "a python3 that does not run".
 * Mirrors `tap-python.sh::tap_python_seen`; asked only on that failure path.
 */
export function pythonSeen(deps: Pick<TapRuntimeDeps, "existsSync" | "isExecutable" | "env">): string {
  const found = firstOnPath(PATH_PYTHONS[0], deps);
  if (!found) return PYTHON_SEEN_NONE;
  try {
    const version = execFileSync(found, ["-S", "-c", SEEN_PROBE], {
      encoding: "utf-8",
      timeout: VERSION_PROBE_TIMEOUT_MS,
      stdio: ["ignore", "pipe", "ignore"],
      windowsHide: true,
    }).trim();
    return /^[0-9.]+$/.test(version) ? `${PYTHON_SEEN_PREFIX}${version}` : PYTHON_SEEN_BROKEN;
  } catch {
    return PYTHON_SEEN_BROKEN;
  }
}
