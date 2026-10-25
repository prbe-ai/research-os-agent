/**
 * Filesystem conventions this extension MUST agree with the tap daemon on.
 *
 * These are not invented here — they mirror `agent/plugins/probe-research-tap/tap/sources.py`
 * and `tap/config.py`'s `pi` row exactly, because the daemon (a separate Python process this
 * extension spawns) resolves the same paths independently and the two sides never talk to
 * negotiate them. Drift here means a spawned daemon can't find its own token, or a shutdown
 * sentinel this extension touches is never seen by the daemon watching a different path.
 *
 * Source of truth, read directly out of the daemon package (not copied from a description):
 *   - tap/sources.py `_SOURCES["pi"]`: token_env="PROBE_PI_TAP_TOKEN",
 *     plugin_dir_env="PROBE_PI_TAP_PLUGIN_DIR", default plugin_state_dir for "pi" is
 *     `~/.pi/agent/state/<plugin_name>`.
 *   - tap/config.py: `PLUGIN_NAME = "probe-research-tap"` (the same literal for every source —
 *     claude_code, codex, AND pi all use this as their state-dir leaf name).
 *   - tap/config.py `shutdown_sentinel()`: prefix is `"prbe-codex-tap"` ONLY when
 *     `capture_source() == "codex"`; every other source — including "pi" — gets the plain
 *     `PLUGIN_NAME` ("probe-research-tap") prefix. So the pid/shutdown files this extension
 *     manages for pi sessions use the SAME "probe-research-tap-watcher-<id>" prefix Claude
 *     Code's hook uses, not a pi-specific one. This is load-bearing: main.py's daemon calls
 *     `cfg.shutdown_sentinel(session_id)` internally to decide when to stop, and that call
 *     computes this exact prefix. If we invented "probe-research-pi-watcher-*" instead, the
 *     daemon would never see our shutdown signal.
 */

import { homedir, tmpdir } from "node:os";
import { join, posix, win32 } from "node:path";

export const PLUGIN_NAME = "probe-research-tap";
export const SOURCE_ID = "pi";
export const TOKEN_ENV = "PROBE_PI_TAP_TOKEN";
export const PLUGIN_DIR_ENV = "PROBE_PI_TAP_PLUGIN_DIR";
export const CONFIG_PATH_ENV = "PROBE_CONFIG_PATH";

/**
 * The Probe Research read MCP surface — same server Claude Code and Codex
 * already connect to (`agent/plugins/probe-research/.mcp.json`,
 * `.codex-plugin/plugin.json`). `MCP_URL_ENV` is a tests/dev-only override
 * (see README's Config table), present so tests (and, in a pinch, someone
 * pointed at a non-prod deployment) never have to touch the real constant.
 */
export const MCP_SERVER_URL = "https://mcp.research.prbe.ai/mcp";
export const MCP_URL_ENV = "PROBE_MCP_URL";

/** Fast-path bearer token env var — mirrors `probe-mcp-headers`' resolution order exactly. */
export const MCP_TOKEN_ENV = "PROBE_MCP_TOKEN";

/** Same watcher-file prefix Claude Code's hook uses — see file-level comment. */
export const WATCHER_PREFIX = "probe-research-tap";

export interface PathEnv {
  [key: string]: string | undefined;
}

/** Plugin-local durable state root for pi captures: env override, else `~/.pi/agent/state/<name>`. */
export function pluginDir(env: PathEnv = process.env): string {
  const override = env[PLUGIN_DIR_ENV];
  if (override && override.trim()) return override;
  return join(homedir(), ".pi", "agent", "state", PLUGIN_NAME);
}

/**
 * The env var that relocates pi's OWN global config directory
 * (`~/.pi/agent`) -- distinct from `PLUGIN_DIR_ENV` above, which relocates
 * only this extension's private state. Not a guess: pi 0.86.0's own
 * `dist/config.js` (same derivation as 0.84.3's) derives it as
 * `${APP_NAME.toUpperCase()}_CODING_AGENT_DIR`
 * (`APP_NAME` is `"pi"` absent a white-label `piConfig.name` in its own
 * package.json, which the installed package does not set), and `getAgentDir()`
 * -- read by every pi entry point, including the one that resolves the global
 * `AGENTS.md`/`AGENTS.override.md` via `resource-loader.js`'s
 * `loadContextFileFromDir` -- returns it verbatim when set. Unlike Codex's
 * `CODEX_HOME` (a parent directory Codex appends `AGENTS.md` onto), this
 * names the agent directory itself, so nothing is appended before it either.
 *
 * Mirrors `agent_rules.memory_path`'s `"pi"` branch in the Python CLI
 * (`agent/src/probe/cli/agent_rules.py`) exactly -- see that function's own
 * docstring for the same sourcing note. The two must agree about pi's own
 * `AGENTS.md`, which is what this resolves. It no longer decides where the
 * TEAM NOTE lives: that is one file per machine in the state directory
 * (`teamNoteDocumentPath` below), reached by no harness path at all.
 */
export const PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR";

/** pi's own global agent directory: env override, else `~/.pi/agent`. See `PI_AGENT_DIR_ENV`. */
export function piAgentDir(env: PathEnv = process.env): string {
  const override = env[PI_AGENT_DIR_ENV];
  if (override && override.trim()) return override;
  return join(homedir(), ".pi", "agent");
}

/**
 * pi's own global instruction file, `<piAgentDir>/AGENTS.md` -- where the
 * wizard installs Probe's managed block for pi. Mirrors
 * `agent_rules.memory_path` for this harness.
 */
export const PI_AGENTS_FILE = "AGENTS.md";

export function piAgentsFile(env: PathEnv = process.env): string {
  return join(piAgentDir(env), PI_AGENTS_FILE);
}

/**
 * pi's own USER-scope settings.json -- `<piAgentDir>/settings.json`. This is
 * the file pi's package manager reads its `packages` array from at user
 * scope (`dist/core/package-manager.js`'s `resolvePackageSources`), and the
 * SAME file pi-mcp-adapter's loader reads (`package-mcp-loader.ts`, see
 * `adapterHandoff.ts`'s module docstring) to discover MCP manifests from
 * those same package entries. Shared here rather than duplicated because
 * both this extension's D6 stand-down check and any future settings-editing
 * code must agree on exactly this path.
 */
export function piSettingsPath(env: PathEnv = process.env): string {
  return join(piAgentDir(env), "settings.json");
}

/**
 * pi's own PROJECT-scope settings.json -- `<cwd>/.pi/settings.json`. Mirrors
 * `piSettingsPath` at project scope; `cwd` is a plain parameter (not read
 * from `env`) because pi resolves this against the session's actual working
 * directory (`ctx.cwd` in this extension), not an environment variable.
 */
export function piProjectSettingsPath(cwd: string): string {
  return join(cwd, ".pi", "settings.json");
}

/**
 * Probe's state directory: `$XDG_STATE_HOME/probe`, else `~/.local/state/probe`.
 * Mirrors `probe.version_policy.state_dir` in the Python CLI. Duplicated rather
 * than shelled out for, because this runs on pi's session-start path where a
 * subprocess is a latency budget we do not have.
 */
export function probeStateDir(env: PathEnv = process.env): string {
  const xdg = env.XDG_STATE_HOME;
  const base = xdg && xdg.trim() ? xdg : join(homedir(), ".local", "state");
  return join(base, "probe");
}

/** `session_marker.APPROVALS_DIRNAME`: the Probe daemon's held questions, under the state dir. */
export const APPROVALS_DIRNAME = "approvals";

/**
 * Where the Probe daemon keeps the questions it holds for the researcher and
 * their answers: `requests/<id>.json` and `answers/<id>.json`, in
 * `APPROVALS_DIRNAME` under `probeStateDir`. Mirrors
 * `probe.daemon.approvals.approvals_dir` and `session_marker.approvals_dir`.
 * `guard.ts` keeps the agent out of it; `approvals.ts` writes the
 * researcher's answers into it.
 */
export function approvalsDir(env: PathEnv = process.env): string {
  return join(probeStateDir(env), APPROVALS_DIRNAME);
}

export const SESSIONS_DIRNAME = "sessions";

/**
 * Where Probe keeps the per-session state files (`<sid>.state`, `<sid>.inline`,
 * `<sid>.profile`, the lease): `SESSIONS_DIRNAME` under `probeStateDir`. Mirrors
 * `session_marker.sessions_dir`. `guard.ts` keeps the agent out of it -- those
 * files decide the guard's own verdict.
 */
export function sessionsDir(env: PathEnv = process.env): string {
  return join(probeStateDir(env), SESSIONS_DIRNAME);
}

/**
 * Where the team note document lives -- ONE per machine, whatever harness is
 * running. This extension reads it to brief a session, and `probe notes sync`
 * (invoked with `PROBE_AGENT=pi`, see `teamNote.ts`) reads and writes it.
 * Mirrors `team_note_file.paths().document` in the Python CLI, which is
 * `state_dir() / "team-note" / "probe-team-note.md"`.
 *
 * IT USED TO BE PER HARNESS -- `~/.pi/agent/probe-team-note.md` beside pi's own
 * `AGENTS.md`, with Claude Code and Codex each keeping their own beside theirs.
 * The sync's base copy was keyed on the credential alone, so one base served
 * three documents: each harness read the others' file as unsynced work and
 * pushed a whole document over it at every session start, and nothing ever
 * converged them. Measured on 2026-09-07: server versions v765-v770, two
 * byte-identical bodies alternating every ~20 minutes, three whole sections
 * appearing and disappearing. `PROBE_AGENT` still selects an instruction FILE;
 * it must never again select a document.
 *
 * `tests/teamNote.test.ts` pins this against the same fixture the Python
 * suite reads, so the two implementations cannot drift apart silently.
 */
export function teamNoteDocumentPath(env: PathEnv = process.env): string {
  return join(probeStateDir(env), "team-note", "probe-team-note.md");
}

export function tokenFile(env: PathEnv = process.env): string {
  return join(pluginDir(env), ".token");
}

export function disabledFile(env: PathEnv = process.env): string {
  return join(pluginDir(env), ".disabled");
}

export function logDir(env: PathEnv = process.env): string {
  return join(pluginDir(env), "logs");
}

export function extensionLogFile(env: PathEnv = process.env): string {
  return join(logDir(env), "extension.log");
}

export function sessionLogFile(sessionId: string, env: PathEnv = process.env): string {
  return join(logDir(env), `${sessionId}.log`);
}

/** Probe CLI's own config file — mirrors tap/config.py `probe_config_path()`. */
export function probeConfigPath(env: PathEnv = process.env): string {
  const override = env[CONFIG_PATH_ENV];
  if (override && override.trim()) return override;
  const xdg = env.XDG_CONFIG_HOME;
  const root = xdg && xdg.trim() ? xdg : join(homedir(), ".config");
  return join(root, "probe", "config.json");
}

/**
 * Where the OAuth fallback's client registration + tokens live when this
 * device has no Probe bearer token (see `mcpOAuth.ts`). Deliberately NOT
 * inside `pluginDir()`: that directory's name and contents are a contract
 * shared with the Python tap daemon (see the file-level comment above), and
 * this state has nothing to do with capture — it is this extension's own
 * MCP client credential cache, scoped under pi's own agent directory. (The
 * team-note document is NOT: it is one file per machine in the state dir.)
 */
export function mcpOAuthStateFile(env: PathEnv = process.env): string {
  return join(piAgentDir(env), "state", "probe-research-mcp", "oauth.json");
}

/** The platforms this package treats apart (Node's `process.platform` names). */
export const OsPlatform = { Windows: "win32", Mac: "darwin", Linux: "linux" } as const satisfies Record<string, NodeJS.Platform>;

/** The folder of a session's watcher files when it names an absolute one, on
 *  every OS (`tap/config.py::WATCHER_DIR_ENV`, tap 0.10.19): the tap, its hooks,
 *  `probe` and the status line all honour it; a test sets it to keep its files
 *  out of the shared `/tmp`. */
export const WATCHER_DIR_ENV = "PROBE_TAP_WATCHER_DIR";

/** Where a session's watcher files live: `WATCHER_DIR_ENV`, else `/tmp`, as the
 *  tap computes it (`tap/config.py::_watcher_dir`); on Windows the user's temp
 *  folder (the hooks' Git Bash `/tmp`), never `\\tmp` on the current drive. */
export function watcherDir(platform: NodeJS.Platform = process.platform, env: PathEnv = process.env): string {
  const given = env[WATCHER_DIR_ENV];
  if (given && (platform === OsPlatform.Windows ? win32 : posix).isAbsolute(given)) return given;
  return platform === OsPlatform.Windows ? tmpdir() : "/tmp";
}

export function pidFile(sessionId: string): string {
  return join(watcherDir(), `${WATCHER_PREFIX}-watcher-${sessionId}.pid`);
}

export function shutdownSentinelFile(sessionId: string): string {
  return join(watcherDir(), `${WATCHER_PREFIX}-watcher-${sessionId}.shutdown`);
}

/**
 * The session is recorded by the Probe service (plan A2, `tap record`), not by a
 * `tap watch` of its own: no `.pid` file, and this marker names the service's
 * pid instead. Mirrors `tap/config.py::hosted_file`. Read only through
 * `hosted.ts::hostedServicePid`: /tmp is shared, so its number is checked.
 */
export function hostedFile(sessionId: string): string {
  return join(watcherDir(), `${WATCHER_PREFIX}-watcher-${sessionId}.hosted`);
}

/** `probe.service.paths`: the service's folder name, the lock its running copy
 * holds (and writes `<pid> <start time>` into), and the CLI's own interpreter. */
export const SERVICE_DIRNAME = "service";
export const SERVICE_LOCK_NAME = "service.lock";
export const SERVICE_RUNTIME_NAME = "runtime.json";

/** `$HOME`, as `Path.home()` reads it on POSIX (and USERPROFILE on Windows). */
function homeOf(env: PathEnv, platform: NodeJS.Platform): string {
  const home = platform === OsPlatform.Windows ? env.USERPROFILE ?? env.HOME : env.HOME;
  return home && home.trim() ? home : homedir();
}

/**
 * `probe.service.paths.root_dir`: `$HOME/.local/state/probe/service`, NEVER
 * moved by `XDG_STATE_HOME` (unlike `probeStateDir` above: every client must
 * agree where the one service lives, plan X2); on Windows
 * `%LOCALAPPDATA%\\probe\\service`. Each machine's service is a folder below it,
 * named by `hosted.ts::machineKey`.
 */
export function serviceRootDir(env: PathEnv = process.env, platform: NodeJS.Platform = process.platform): string {
  if (platform === OsPlatform.Windows) {
    const local = env.LOCALAPPDATA;
    const base = local && local.trim() ? local : join(homeOf(env, platform), "AppData", "Local");
    return join(base, "probe", SERVICE_DIRNAME);
  }
  return join(homeOf(env, platform), ".local", "state", "probe", SERVICE_DIRNAME);
}

/** `paths.runtime_path`: `{"python": ..., "cli_version": ...}`, the Probe CLI's
 * interpreter, rewritten by the CLI at every start. */
export function serviceRuntimeFile(env: PathEnv = process.env, platform: NodeJS.Platform = process.platform): string {
  return join(serviceRootDir(env, platform), SERVICE_RUNTIME_NAME);
}

/** `paths.UV_TOOL_PYTHON`: where `uv tool install probe-research` puts the CLI's
 * interpreter, under `$HOME`. */
export const UV_TOOL_PYTHON = [".local", "share", "uv", "tools", "probe-research", "bin", "python"] as const;
/** `paths.fallback_python` on Windows, under `%APPDATA%`. */
export const UV_TOOL_PYTHON_WINDOWS = ["uv", "tools", "probe-research", "Scripts", "python.exe"] as const;

/** `paths.fallback_python`: the CLI's interpreter when runtime.json names none. */
export function uvToolPython(env: PathEnv = process.env, platform: NodeJS.Platform = process.platform): string {
  if (platform === OsPlatform.Windows) {
    const roaming = env.APPDATA;
    const base = roaming && roaming.trim() ? roaming : join(homeOf(env, platform), "AppData", "Roaming");
    return join(base, ...UV_TOOL_PYTHON_WINDOWS);
  }
  return join(homeOf(env, platform), ...UV_TOOL_PYTHON);
}

/**
 * Left in the tap's state folder when no Python 3.11+ was found to run the tap
 * on, holding what was found instead ("python3 3.9.6"); removed once one is.
 * `probe doctor` names it (`doctor.TAP_PYTHON_MISSING`). Mirrors
 * `hooks/tap-python.sh`'s TAP_PYTHON_MISSING.
 */
export const TAP_PYTHON_MISSING = ".python-missing";

export function pythonMissingFile(env: PathEnv = process.env): string {
  return join(pluginDir(env), TAP_PYTHON_MISSING);
}
