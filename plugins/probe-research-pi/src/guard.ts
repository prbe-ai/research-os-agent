/**
 * The guard: what `extension.ts`'s `tool_call` handler refuses before a tool
 * runs. pi's analogue of Claude Code's PreToolUse hook, `tracking_guard.py`'s
 * `_deny` and `_deny_daemon_profile`, for the two rules a pi session needs:
 *
 *   BOTH PROFILES -- a `bash`/`powershell` command that names the Probe
 *   daemon's question folder, or a `write`/`edit` of a file inside it, is
 *   refused (`DENY_REASON_APPROVALS`). Only the researcher answers the
 *   daemon's held questions, through pi's own dialog (`approvals.ts`) or
 *   `probe approvals` in a terminal; an agent that wrote an answer file would
 *   be answering for them. `session_marker.touches_approvals`, ported.
 *
 *   DAEMON PROFILE -- the daemon records and reads, so a shell command running
 *   `probe <anything>` outside `DAEMON_PROFILE_ALLOWED` and the agent's runs'
 *   own data, and every Probe MCP tool call, are refused (`DAEMON_PROFILE_DENY`).
 *   Unless the agent took Probe over (`probe session inline`, `sessionIsInline`):
 *   then only the question folder and the researcher's switch (`switchMove`,
 *   `INLINE_SWITCH_DENY`) are refused.
 *
 * The `read-only`/`off` refusals of the Python guard are not ported: pi has
 * never carried them, and the CLI's own write gate refuses those writes on
 * every harness.
 *
 * A TRIPWIRE, NOT A BOUNDARY, exactly as the Python says: the agent runs as
 * the same user, so a script that builds the path or the command at run time
 * still gets through. The daemon's own checks are what a yes must still pass.
 * Every string and pattern here is a copy; `agent/tests/test_pi_guard_parity.py`
 * pins them to `session_marker`.
 */

import { readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { approvalsDir, probeStateDir, type PathEnv } from "./core/paths.js";
import { isProbeMcpTool } from "./profile.js";
import { daemonProfileRefusal, switchMove } from "./core/probeCommands.js";
import { ProbeState } from "./core/trackingState.js";
import { STATE_SUFFIX } from "./core/turnSignal.js";

/** `session_marker.DENY_REASON_APPROVALS`. Placeholders: `{tool}`, `{path}`. */
export const DENY_REASON_APPROVALS =
  "`{tool}` would touch {path}, where the Probe daemon keeps the questions it holds for the " +
  "researcher and their answers, so it was refused before it ran. Only the researcher answers " +
  "them: through your question tool, word for word, or `probe approvals` in their own " +
  "terminal. Do not write there, and do not route around this.";

/** `session_marker.DAEMON_PROFILE_DENY`. Placeholder: `{matched}`. */
export const DAEMON_PROFILE_DENY =
  "The Probe daemon records this session and reads the team's work for you, so `{matched}` was refused before it ran. You only instrument your runs with the SDK. To ask about the team's prior work: `probe ask \"<question>\"`. To do it yourself, run `probe session inline` first.";

/** `session_marker._APPROVALS_TEXT`'s pattern: the folder as a command's text names it. */
export const APPROVALS_TEXT_SOURCE = String.raw`probe/+approvals\b|\bapprovals/+(answers|requests)\b`;
const APPROVALS_TEXT = new RegExp(APPROVALS_TEXT_SOURCE);

/** pi's built-in tools the guard reads, by name (`isToolCallEventType`'s names). */
export const PiTool = {
  Bash: "bash",
  PowerShell: "powershell",
  Write: "write",
  Edit: "edit",
} as const;

/** Tools that write the file one input field names: `session_marker._FILE_WRITE_TOOLS`, pi's spelling. */
const FILE_WRITE_FIELD: Readonly<Record<string, string>> = { [PiTool.Write]: "path", [PiTool.Edit]: "path" };
/** Tools that run one input field as a shell command: the Python's `Bash`. */
const SHELL_FIELD: Readonly<Record<string, string>> = { [PiTool.Bash]: "command", [PiTool.PowerShell]: "command" };

/** `os.path.realpath`'s non-strict reading: symlinks resolved as far as the path exists. */
function realpathLoose(path: string): string {
  try {
    return realpathSync(path);
  } catch {
    const parent = dirname(path);
    return parent === path ? path : join(realpathLoose(parent), basename(path));
  }
}

/** pi's `UNICODE_SPACES` (dist/utils/paths.js). */
const UNICODE_SPACES = /[\u00A0\u2000-\u200A\u202F\u205F\u3000]/g;

/** pi's `normalizeWindowsShellPath`: Git Bash / MSYS / Cygwin / WSL drive paths. */
function normalizeWindowsShellPath(filePath: string): string {
  if (!filePath.startsWith("/") || filePath.startsWith("//") || filePath.includes("\\")) return filePath;
  const match = filePath.match(/^\/(?:mnt\/|cygdrive\/)?([a-z])(?:\/(.*))?$/i);
  if (!match) return filePath;
  const suffix = match[2]?.replaceAll("/", "\\");
  return `${match[1].toUpperCase()}:\\${suffix ?? ""}`;
}

/** pi's `normalizePath` (dist/utils/paths.js, pi 1.0.0), the options its tools pass. */
function normalizeLikePi(input: string, options: { unicodeSpaces?: boolean; stripAt?: boolean } = {}): string {
  let normalized = input;
  if (options.unicodeSpaces) normalized = normalized.replace(UNICODE_SPACES, " ");
  if (options.stripAt && normalized.startsWith("@")) normalized = normalized.slice(1);
  if (process.platform === "win32") normalized = normalizeWindowsShellPath(normalized);
  const home = homedir();
  if (normalized === "~") return home;
  if (normalized.startsWith("~/") || (process.platform === "win32" && normalized.startsWith("~\\"))) {
    return join(home, normalized.slice(2));
  }
  if (/^file:\/\//.test(normalized)) return fileURLToPath(normalized);
  return normalized;
}

/**
 * A tool's `path` exactly as pi's own write/edit resolve it: `resolveToCwd`
 * (dist/core/tools/path-utils.js) over `resolvePath`. Unicode spaces folded,
 * `@` stripped, `~` expanded, a `file://` URL turned into its path, relative
 * to cwd. pi does not export it, so it is ported; a resolution that differs
 * from pi's is a way past the guard (a `file://` path was one).
 */
export function resolveLikePi(target: string, cwd: string): string {
  const normalized = normalizeLikePi(target, { unicodeSpaces: true, stripAt: true });
  return isAbsolute(normalized) ? resolve(normalized) : resolve(normalizeLikePi(cwd), normalized);
}

function fill(template: string, values: Readonly<Record<string, string>>): string {
  return template.replace(/\{(\w+)\}/g, (whole, key: string) => values[key] ?? whole);
}

/** `session_marker.touches_approvals`: the approvals path a tool call aims at, or null. */
export function touchesApprovals(toolName: string, input: unknown, env: PathEnv, cwd: string): string | null {
  if (typeof input !== "object" || input === null) return null;
  const fields = input as Record<string, unknown>;
  const fileField = FILE_WRITE_FIELD[toolName];
  if (fileField !== undefined) {
    const target = fields[fileField];
    if (typeof target !== "string" || !target) return null;
    const root = realpathLoose(approvalsDir(env));
    const real = realpathLoose(resolveLikePi(target, cwd));
    return real === root || real.startsWith(root.endsWith(sep) ? root : root + sep) ? target : null;
  }
  const shellField = SHELL_FIELD[toolName];
  if (shellField !== undefined) {
    const command = fields[shellField];
    if (typeof command === "string" && (APPROVALS_TEXT.test(command) || command.includes(approvalsDir(env)))) {
      return approvalsDir(env);
    }
  }
  return null;
}

/** `session_marker.INLINE_SWITCH_DENY`. Placeholder: `{matched}`. */
export const INLINE_SWITCH_DENY =
  "`{matched}` moves the researcher's Probe switch, so it was refused before it ran: only the researcher moves it (`/probe on`, `/probe read`, `/probe off`). To hand Probe back to the daemon, run `probe session daemon`.";

/** `session_marker.INLINE_FROM`: the states the agent may take Probe over from. */
export const INLINE_FROM: ReadonlySet<string> = new Set(["daemon", "read-only"]);

/** `session_marker.INLINE_SUFFIX`: the takeover's marker beside `<sid>.state`. */
export const INLINE_SUFFIX = ".inline";

/**
 * `session_marker.is_inline`, ported: the agent took Probe over from the daemon
 * (`probe session inline`, Richard 2026-10-05). The session's state is `full`
 * AND its `<sid>.inline` marker names where it was taken over from. The caller
 * knows the profile (the marker is only ever written in the daemon profile).
 */
export function sessionIsInline(sessionId: string | undefined, env: PathEnv = process.env): boolean {
  if (!sessionId) return false;
  const sessions = join(probeStateDir(env), "sessions");
  try {
    const state = readFileSync(join(sessions, `${sessionId}${STATE_SUFFIX}`), "utf8").trim().toLowerCase();
    if (state !== ProbeState.Full) return false;
    const marker = JSON.parse(readFileSync(join(sessions, `${sessionId}${INLINE_SUFFIX}`), "utf8")) as unknown;
    if (typeof marker !== "object" || marker === null) return false;
    const { from, since } = marker as { from?: unknown; since?: unknown };
    // `session_marker.inline_marker`: a known origin and a finite `since`.
    return INLINE_FROM.has(String(from)) && typeof since === "number" && Number.isFinite(since);
  } catch {
    return false;
  }
}

export interface GuardContext {
  daemonProfile: boolean;
  /** The agent took Probe over (`sessionIsInline`): the daemon profile refuses nothing. */
  inline?: boolean;
  env: PathEnv;
  cwd: string;
}

/** Why this tool call is refused, or null to let it run. */
export function guardToolCall(toolName: string, input: unknown, context: GuardContext): string | null {
  const aimed = touchesApprovals(toolName, input, context.env, context.cwd);
  if (aimed !== null) return fill(DENY_REASON_APPROVALS, { tool: toolName, path: aimed });
  if (!context.daemonProfile) return null;
  if (context.inline) {
    // The agent took Probe over: only the researcher's switch is refused.
    const field = SHELL_FIELD[toolName];
    const line = field !== undefined && typeof input === "object" && input !== null ? (input as Record<string, unknown>)[field] : undefined;
    const moved = typeof line === "string" ? switchMove(line) : null;
    return moved === null ? null : fill(INLINE_SWITCH_DENY, { matched: moved });
  }
  if (isProbeMcpTool(toolName)) return fill(DAEMON_PROFILE_DENY, { matched: toolName });
  const shellField = SHELL_FIELD[toolName];
  if (shellField === undefined || typeof input !== "object" || input === null) return null;
  const command = (input as Record<string, unknown>)[shellField];
  if (typeof command !== "string") return null;
  const matched = daemonProfileRefusal(command);
  return matched === null ? null : fill(DAEMON_PROFILE_DENY, { matched });
}
