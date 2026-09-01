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

import { homedir } from "node:os";
import { join } from "node:path";

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
 * only this extension's private state. Not a guess: pi 0.84.3's own
 * `dist/config.js` derives it as `${APP_NAME.toUpperCase()}_CODING_AGENT_DIR`
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
 * docstring for the same sourcing note. The two must agree: this extension
 * reads `teamNoteDocumentPath()` for its OWN cache, and `probe notes sync`
 * (invoked with `PROBE_AGENT=pi`, see `teamNote.ts`) writes through the
 * Python resolution. A drift here is silent -- this extension would cache
 * one file while the CLI syncs a different one.
 */
export const PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR";

/** pi's own global agent directory: env override, else `~/.pi/agent`. See `PI_AGENT_DIR_ENV`. */
export function piAgentDir(env: PathEnv = process.env): string {
  const override = env[PI_AGENT_DIR_ENV];
  if (override && override.trim()) return override;
  return join(homedir(), ".pi", "agent");
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
 * Where the team note document lives for a pi session -- the file this
 * extension reads to brief a session, and the file `probe notes sync
 * --agent pi` (via `PROBE_AGENT=pi`) reads and writes. Mirrors
 * `team_note_file.paths(source="pi").document` in the Python CLI, which is
 * `agent_rules.memory_path("pi").parent / "probe-team-note.md"` --
 * `~/.pi/agent/probe-team-note.md` by default, right beside where pi's own
 * `AGENTS.md` lives, exactly like `~/.claude/probe-team-note.md` sits beside
 * `~/.claude/CLAUDE.md` for Claude Code.
 */
export function teamNoteDocumentPath(env: PathEnv = process.env): string {
  return join(piAgentDir(env), "probe-team-note.md");
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
 * MCP client credential cache, scoped under pi's own agent directory like
 * the team-note document is.
 */
export function mcpOAuthStateFile(env: PathEnv = process.env): string {
  return join(piAgentDir(env), "state", "probe-research-mcp", "oauth.json");
}

export function pidFile(sessionId: string): string {
  return join("/tmp", `${WATCHER_PREFIX}-watcher-${sessionId}.pid`);
}

export function shutdownSentinelFile(sessionId: string): string {
  return join("/tmp", `${WATCHER_PREFIX}-watcher-${sessionId}.shutdown`);
}
