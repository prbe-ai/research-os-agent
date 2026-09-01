/**
 * D6's stand-down check: does pi-mcp-adapter already own the Probe MCP
 * server, so THIS extension's direct bridge (mcpBridge.ts) would just
 * double-register the same tools?
 *
 * pi-mcp-adapter auto-discovers a package's `pi.mcp` manifest (this
 * package ships one at `../mcp.json`) purely from the `packages` arrays in
 * pi's own settings.json files — it never scans `node_modules` or
 * `~/.pi/agent/extensions/` (see the plan's Verified fact 3, read out of
 * their `package-mcp-loader.ts`). So the ONLY reliable signal for "will the
 * adapter pick up our manifest" is reading those same settings files
 * ourselves and re-deriving identity the way pi's own package manager does.
 *
 * BOTH-OR-NEITHER rationale (the actual D6 rule): stand down only when the
 * merged user+project `packages` lists contain BOTH pi-mcp-adapter AND our
 * own package.
 *   - Adapter absent -> nothing else will ever serve these tools; keep
 *     bridging directly, unconditionally.
 *   - Adapter present but WE are not in `packages` -> the classic case is a
 *     legacy install: this extension symlinked into
 *     `~/.pi/agent/extensions/` (this package's own documented install
 *     method for the extension itself) rather than registered as a
 *     `packages` entry. The adapter's loader only reads `packages`, so it
 *     cannot see our `mcp.json` in that shape at all. Standing down here
 *     would make Probe's tools vanish outright — worse than the duplicate
 *     tools this whole check exists to prevent — so the bridge stays up.
 *
 * FAIL OPEN, ALWAYS. Every read in this module is synchronous fs, wrapped
 * so that a missing file, unreadable permissions, malformed JSON, or a
 * settings root/`packages` value of the wrong shape degrades to "this scope
 * contributed no packages" rather than throwing or blocking session_start.
 * The failure direction matters: an unreadable settings file must never be
 * read as "the adapter is present" (that would silently kill Probe's
 * tools) — it can only ever push the result toward `standDown: false`,
 * i.e. toward keeping the direct bridge alive. Same posture as
 * mcpAuth.ts/pairing.ts: deciding whether to connect must never itself
 * require anything more than a synchronous local file read.
 *
 * Identity rules mirror pi 0.84.3's REAL package-source resolution
 * (`dist/core/package-manager.js`'s `parseSource`/`parseNpmSpec`/
 * `getSourceMatchKeyForSettings`, and `dist/utils/paths.js`'s
 * `isLocalPath`/`resolvePath` — read directly out of this package's own
 * `node_modules/@earendil-works/pi-coding-agent`, a devDependency of this
 * package for exactly this kind of verification, see skillsManifest.test.ts):
 *   - `npm:<name>[@version]` -> name is `parseNpmSpec`'s
 *     `/^(@?[^@]+(?:\/[^@]+)?)(?:@(.+))?$/` capture group 1, copied
 *     verbatim so a scoped name like `@scope/pi-mcp-adapter` is captured
 *     whole and never collapses to just `pi-mcp-adapter`.
 *   - A source is "local" (pi's `isLocalPath`) unless it starts with one of
 *     `npm:` / `git:` / `github:` / `http:` / `https:` / `ssh:` — those
 *     prefixes are the only literal signal pi itself uses; everything else,
 *     including bare `git@host:owner/repo` scp syntax, resolves as local.
 *   - Local paths resolve relative to the SCOPE's base dir (agent dir for
 *     user scope, `<cwd>/.pi` for project scope — `getBaseDirForScope`),
 *     absolute paths resolve as-is, and identity compares REALPATHs (pi's
 *     own match key is `local:<resolved path>`, and this module goes one
 *     step further to realpath both sides so a symlinked package root still
 *     matches).
 */

import { readFileSync, realpathSync } from "node:fs";
import { isAbsolute, join, resolve, sep } from "node:path";

import { piAgentDir, piProjectSettingsPath, piSettingsPath, type PathEnv } from "./paths.js";

/** The exact phrase D6 requires in the extension log and /probe-status when standing down. */
export const MCP_SERVED_VIA_ADAPTER_MESSAGE = "Probe MCP served via pi-mcp-adapter";

const ADAPTER_NPM_NAME = "pi-mcp-adapter";
const OUR_NPM_NAME = "probe-research-pi";

/**
 * The public mirror repo we ARE distributed from — a GIT BASENAME only.
 *
 * pi has no marketplace, so a real install is a git source pointing at this
 * repo: pi clones it and loads us from the root manifest's
 * `plugins/probe-research-pi/index.ts`. The repo is named `research-os-agent`,
 * but the package.json rendered to its root deliberately carries OUR name
 * (`probe-research-pi`) — that is what pi-mcp-adapter builds its
 * `<package>__<server>` tool prefix from, so `probe-research-pi__probe` holds
 * whether we were installed from a checkout or the mirror. Hence only the git
 * spelling needs a second identity here; the local branch matches by name.
 */
const OUR_MIRROR_REPO = "research-os-agent";

// Mirrors pi's own `isLocalPath()` (dist/utils/paths.js): a source is
// non-local ONLY if it starts with one of these literal prefixes.
const NON_LOCAL_PREFIXES = ["git:", "github:", "http:", "https:", "ssh:"];

export interface AdapterHandoffOptions {
  env: PathEnv;
  /** The session's working directory — used to locate `<cwd>/.pi/settings.json`. */
  cwd: string;
  /** This package's own root directory — the entry point's `extensionDir`. */
  packageRoot: string;
}

export interface AdapterHandoffResult {
  standDown: boolean;
  /** Human-readable explanation — always populated, including the fail-open cases. */
  reason: string;
}

type ParsedSource =
  | { kind: "npm"; name: string }
  | { kind: "git"; basename: string }
  | { kind: "local"; path: string };

/** Mirrors `parseNpmSpec` — see module docstring. */
function parseNpmName(spec: string): string {
  const match = spec.match(/^(@?[^@]+(?:\/[^@]+)?)(?:@(.+))?$/);
  return match ? match[1] : spec;
}

/**
 * `git:<url-ish>`'s identity for our purposes is just the repo's path
 * basename with any `.git` suffix stripped (e.g. `pi-mcp-adapter` out of
 * `https://github.com/foo/pi-mcp-adapter.git` or `git:github.com/foo/pi-mcp-adapter#main`).
 * This is deliberately narrower than pi's own git-URL parser (which pulls
 * in `hosted-git-info` for host/owner/ref) — no new dependency is worth
 * adding just to answer "does the last path segment say pi-mcp-adapter."
 */
function gitBasename(source: string): string {
  let s = source.startsWith("git:") ? source.slice(4) : source;
  const hashIdx = s.indexOf("#");
  if (hashIdx !== -1) s = s.slice(0, hashIdx);
  const qIdx = s.indexOf("?");
  if (qIdx !== -1) s = s.slice(0, qIdx);
  const parts = s.split(/[/:]/).filter(Boolean);
  let last = parts[parts.length - 1] ?? "";
  if (last.endsWith(".git")) last = last.slice(0, -4);
  return last;
}

function parseSource(rawSource: string): ParsedSource {
  const source = rawSource.trim();
  if (source.startsWith("npm:")) {
    return { kind: "npm", name: parseNpmName(source.slice(4).trim()) };
  }
  if (NON_LOCAL_PREFIXES.some((prefix) => source.startsWith(prefix))) {
    return { kind: "git", basename: gitBasename(source) };
  }
  return { kind: "local", path: source };
}

/** realpathSync, falling back to a plain resolve() when the target doesn't exist (yet) or isn't readable. */
function realpathOrResolved(path: string): string {
  try {
    return realpathSync(path);
  } catch {
    return path;
  }
}

function resolveLocalSource(rawPath: string, baseDir: string): string {
  const resolved = isAbsolute(rawPath) ? resolve(rawPath) : resolve(baseDir, rawPath);
  return realpathOrResolved(resolved);
}

/** Reads a local candidate's package.json "name" field. Any failure -> null, never throws. */
function readPackageJsonName(dir: string): string | null {
  try {
    const raw = readFileSync(join(dir, "package.json"), "utf-8");
    const data: unknown = JSON.parse(raw);
    if (typeof data === "object" && data !== null && !Array.isArray(data)) {
      const name = (data as Record<string, unknown>).name;
      return typeof name === "string" ? name : null;
    }
    return null;
  } catch {
    return null;
  }
}

interface ScopeEntries {
  /** Raw `packages` source strings for this scope, already flattened from string | {"source": ...}. */
  sources: string[];
  /** Base dir relative local sources in this scope resolve against. */
  baseDir: string;
  /** Set when the scope's settings.json was unreadable/malformed — folded into the overall reason. */
  note?: string;
}

/**
 * Reads one settings.json's `packages` array. Every failure mode collapses
 * to "this scope contributed zero packages" — see the module docstring's
 * FAIL OPEN section for why that direction is the only safe one: an
 * unreadable file must never be mistaken for "the adapter is configured
 * here."
 */
function readScope(settingsPath: string, baseDir: string, label: string): ScopeEntries {
  let raw: string;
  try {
    raw = readFileSync(settingsPath, "utf-8");
  } catch {
    // Missing file is the common, expected case (fact 8: this machine's
    // real settings.json has no `packages` key at all) — no note needed.
    return { sources: [], baseDir };
  }

  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return { sources: [], baseDir, note: `${label} settings.json (${settingsPath}) has malformed JSON, ignored` };
  }

  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    return { sources: [], baseDir, note: `${label} settings.json (${settingsPath}) root is not an object, ignored` };
  }

  const root = data as Record<string, unknown>;
  const packages = root.packages;
  if (packages === undefined) {
    // Absent `packages` is normal — pi itself treats this the same as `[]` (`?? []`).
    return { sources: [], baseDir };
  }
  if (!Array.isArray(packages)) {
    return { sources: [], baseDir, note: `${label} settings.json (${settingsPath}) "packages" is not a list, ignored` };
  }

  const sources: string[] = [];
  for (const entry of packages) {
    if (typeof entry === "string") {
      sources.push(entry);
    } else if (entry && typeof entry === "object" && !Array.isArray(entry) && typeof (entry as Record<string, unknown>).source === "string") {
      sources.push((entry as Record<string, unknown>).source as string);
    }
    // Any other shape is a malformed individual entry — skipped, not fatal to the scope.
  }
  return { sources, baseDir };
}

function matchesAdapter(source: string, baseDir: string): boolean {
  const parsed = parseSource(source);
  if (parsed.kind === "npm") return parsed.name === ADAPTER_NPM_NAME;
  if (parsed.kind === "git") return parsed.basename === ADAPTER_NPM_NAME;
  const dir = resolveLocalSource(parsed.path, baseDir);
  return readPackageJsonName(dir) === ADAPTER_NPM_NAME;
}

function matchesOurs(source: string, baseDir: string, ourRealRoot: string): boolean {
  const parsed = parseSource(source);
  if (parsed.kind === "npm") return parsed.name === OUR_NPM_NAME;
  // We ARE distributed via git now — the mirror repo is the whole channel, so
  // a git source naming it is us. This returned a flat `false` while the only
  // install shapes were a local checkout and an unpublished npm name; left
  // that way, every mirror-installed session would fail condition (b) of D6,
  // never stand down, and register a second copy of the Probe MCP tools
  // beside pi-mcp-adapter's.
  if (parsed.kind === "git") return parsed.basename === OUR_MIRROR_REPO;

  const dir = resolveLocalSource(parsed.path, baseDir);
  if (dir === ourRealRoot) return true;
  // Mirror layout, installed from a local clone rather than by pi's own git
  // fetch: the entry names the REPO root, and our package root is nested at
  // `<repo>/plugins/probe-research-pi`. Confirm by name so an unrelated
  // ancestor directory can never match on the prefix alone.
  return ourRealRoot.startsWith(dir + sep) && readPackageJsonName(dir) === OUR_NPM_NAME;
}

export function detectAdapterHandoff(opts: AdapterHandoffOptions): AdapterHandoffResult {
  const notes: string[] = [];

  const userScope = readScope(piSettingsPath(opts.env), piAgentDir(opts.env), "user");
  if (userScope.note) notes.push(userScope.note);

  const projectScope = readScope(piProjectSettingsPath(opts.cwd), join(opts.cwd, ".pi"), "project");
  if (projectScope.note) notes.push(projectScope.note);

  const ourRealRoot = realpathOrResolved(resolve(opts.packageRoot));

  const entries = [
    ...userScope.sources.map((source) => ({ source, baseDir: userScope.baseDir })),
    ...projectScope.sources.map((source) => ({ source, baseDir: projectScope.baseDir })),
  ];

  let adapterFound = false;
  let oursFound = false;
  for (const { source, baseDir } of entries) {
    if (!adapterFound && matchesAdapter(source, baseDir)) adapterFound = true;
    if (!oursFound && matchesOurs(source, baseDir, ourRealRoot)) oursFound = true;
    if (adapterFound && oursFound) break;
  }

  const noteSuffix = notes.length > 0 ? ` (${notes.join("; ")})` : "";

  if (adapterFound && oursFound) {
    return {
      standDown: true,
      reason: `pi-mcp-adapter and ${OUR_NPM_NAME} are both present in the merged pi packages list.${noteSuffix}`,
    };
  }
  if (adapterFound && !oursFound) {
    return {
      standDown: false,
      reason:
        `pi-mcp-adapter is present in the merged pi packages list, but ${OUR_NPM_NAME} is not — ` +
        "likely installed via the legacy ~/.pi/agent/extensions symlink rather than a packages entry, " +
        "so the adapter cannot discover our mcp.json manifest; keeping the direct MCP bridge up so Probe " +
        `tools do not vanish.${noteSuffix}`,
    };
  }
  return {
    standDown: false,
    reason: `pi-mcp-adapter is not present in the merged pi packages list — bridging Probe MCP tools directly.${noteSuffix}`,
  };
}
