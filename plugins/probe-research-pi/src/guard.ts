/**
 * What `extension.ts` reads of a tool call and of the session's inline state. THE GUARD ITSELF IS
 * THE PROBE SERVICE'S (service-only task 16): every `tool_call` goes there, and its pre-tool routes
 * (the Python guard Claude Code runs) refuse it in their own words; pi runs no rule of its own.
 *
 *   `resolveLikePi`   a write/edit `path` exactly as pi resolves it: the service's routes read the
 *                     resolved path (`resolved_path`), since only pi knows how it resolves one
 *   `sessionIsInline` the inline marker (`session_marker.is_inline`, ported): the footer's words
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { probeStateDir, type PathEnv } from "./core/paths.js";
import { ProbeState } from "./core/trackingState.js";
import { STATE_SUFFIX } from "./core/turnSignal.js";

/** pi's built-in tools, by name (`isToolCallEventType`'s names). */
export const PiTool = {
  Bash: "bash",
  PowerShell: "powershell",
  Write: "write",
  Edit: "edit",
} as const;

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
 * from pi's is a way past the service's guard (a `file://` path was one).
 */
export function resolveLikePi(target: string, cwd: string): string {
  const normalized = normalizeLikePi(target, { unicodeSpaces: true, stripAt: true });
  return isAbsolute(normalized) ? resolve(normalized) : resolve(normalizeLikePi(cwd), normalized);
}

/** `session_marker.INLINE_FROM`: the states `/probe inline` is typed from (any). */
export const INLINE_FROM: ReadonlySet<string> = new Set(["daemon", "read-only", "off"]);

/** `session_marker.INLINE_SUFFIX`: the inline marker beside `<sid>.state`. */
export const INLINE_SUFFIX = ".inline";
export const InlineReason = { BudgetExhausted: "budget_exhausted" } as const;

/**
 * `session_marker.inline_marker`'s `since` for an OPEN marker (a known origin, a
 * finite `since`, no `until`), or null. `until` is a stretch whose log could not
 * be written: closed, so not inline.
 */
export function inlineSince(sessionId: string | undefined, env: PathEnv = process.env): number | null {
  if (!sessionId) return null;
  try {
    const marker = JSON.parse(
      readFileSync(join(probeStateDir(env), "sessions", `${sessionId}${INLINE_SUFFIX}`), "utf8"),
    ) as unknown;
    if (typeof marker !== "object" || marker === null || "until" in marker) return null;
    const { from, since, mode } = marker as { from?: unknown; since?: unknown; mode?: unknown };
    return INLINE_FROM.has(String(from)) && typeof since === "number" && Number.isFinite(since)
      && (mode === undefined || mode === ProbeState.Full || mode === ProbeState.ReadOnly) ? since : null;
  } catch {
    return null;
  }
}

/**
 * `session_marker.is_inline`, ported: manual or trusted confirmed-quota inline.
 * The session's state matches its open marker's mode (`full` for older markers,
 * or `read-only` for a quota handoff preserving read-only). The caller knows
 * the profile (the marker is only ever written in the daemon profile).
 */
export function sessionIsInline(sessionId: string | undefined, env: PathEnv = process.env): boolean {
  return currentInlineSince(sessionId, env) !== null;
}

/** `sessionIsInline`'s stretch: its `since`, or null when the session is not inline. One read of each file. */
export function currentInlineSince(sessionId: string | undefined, env: PathEnv = process.env): number | null {
  if (!sessionId) return null;
  try {
    const state = readFileSync(join(probeStateDir(env), "sessions", `${sessionId}${STATE_SUFFIX}`), "utf8")
      .trim()
      .toLowerCase();
    const marker = JSON.parse(readFileSync(join(probeStateDir(env), "sessions", `${sessionId}${INLINE_SUFFIX}`), "utf8"));
    return state === (marker?.mode ?? ProbeState.Full) ? inlineSince(sessionId, env) : null;
  } catch {
    return null;
  }
}

export function sessionInlineReadOnly(sessionId: string | undefined, env: PathEnv = process.env): boolean {
  if (!sessionId || !sessionIsInline(sessionId, env)) return false;
  try {
    return readFileSync(join(probeStateDir(env), "sessions", `${sessionId}${STATE_SUFFIX}`), "utf8").trim() === ProbeState.ReadOnly;
  } catch {
    return false;
  }
}
