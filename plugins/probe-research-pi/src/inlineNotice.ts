/**
 * `/probe inline` (Richard 2026-10-06), pi's half of telling the agent: the
 * researcher handed Probe to it (typed, or `probe session state inline` from a
 * terminal), so on its next prompt -- once per stretch, and again after a
 * compaction or resume -- it hears that the daemon is off and which two skill
 * files it records by. pi's own package ships the main agent's track-work and
 * edit-notes under `skills/`; the daemon profile only stops listing them.
 *
 * `<sid>.inline-shown` holds the stretch's `since` once the agent was told:
 * shared with the Python hooks (`session_marker.inline_notice_due`), which read
 * it as a number, so either side's spelling of the float matches.
 *
 * Its last sentence follows the local CLI (`version_check.inline_notice_for`):
 * the read bridge (`probe mcp tools|call`) only beside a CLI that has it, since
 * pi's package and the CLI update on their own schedules.
 */

import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { currentInlineSince } from "./guard.js";
import { probeStateDir, type PathEnv } from "./core/paths.js";
import { findProbeBinary, type ProbeBinaryDeps } from "./core/teamNote.js";
import type { TrackingExecFileFn } from "./core/trackingState.js";

/** `session_marker.INLINE_NOTICE`, verbatim (Richard's approved text, 2026-10-06). */
export const INLINE_NOTICE =
  "Probe is `on (inline)` for this conversation: the Probe daemon is off, and you read and write Probe yourself with the `probe` CLI, recording the work as it happens. Before recording, read the two skills you record by: {track_work} and {edit_notes}. {reads}";

/** `session_marker.INLINE_READS`: the last sentence beside a CLI with the read bridge. */
export const INLINE_READS =
  "This session has no Probe MCP tools: where a skill names one, run `probe mcp tools` to see them and `probe mcp call <tool> '<json args>'` to use one.";

/** `session_marker.INLINE_READS_OLD_CLI`: beside an older CLI, or one whose version is unknown. */
export const INLINE_READS_OLD_CLI =
  "This session has no Probe MCP tools, and this machine's `probe` CLI is too old to read Probe here: ask the researcher to update it (`probe wizard --action update`).";

/** `version_check.INLINE_BRIDGE_MIN_CLI`: the first CLI with `probe mcp tools|call`. */
export const INLINE_BRIDGE_MIN_CLI = "0.220.0";

/** `version_check.INLINE_CLI_TIMEOUT_S`, in ms: `probe --version` measured 0.9 s. */
export const INLINE_CLI_TIMEOUT_MS = 3_000;

/** `session_marker.INLINE_SHOWN_SUFFIX`. */
export const INLINE_SHOWN_SUFFIX = ".inline-shown";

/**
 * The notice, naming this package's copies of the two skills; `bridge`: the
 * local CLI has `probe mcp tools|call` (`cliHasInlineBridge`).
 */
export function inlineNotice(extensionDir: string, bridge: boolean): string {
  return INLINE_NOTICE.replace("{track_work}", join(extensionDir, "skills", "track-work", "SKILL.md"))
    .replace("{edit_notes}", join(extensionDir, "skills", "edit-notes", "SKILL.md"))
    .replace("{reads}", bridge ? INLINE_READS : INLINE_READS_OLD_CLI);
}

/**
 * `version_check._triplet`: (major, minor, patch) from `probe 0.220.0`, a
 * leading word and any pre-release or build suffix ignored; null if unreadable.
 */
export function versionTriplet(raw: string | null | undefined): [number, number, number] | null {
  if (!raw) return null;
  const words = raw.trim().split(/\s+/);
  const last = (words[words.length - 1] ?? "").split("+")[0]?.split("-")[0] ?? "";
  const parts = last.split(".");
  if (parts.length === 0 || parts.some((part) => !/^\d+$/.test(part))) return null;
  const nums = parts.map(Number);
  while (nums.length < 3) nums.push(0);
  return [nums[0]!, nums[1]!, nums[2]!];
}

/** `version_check.inline_bridge_ready`'s comparison: an unknown version is NO. */
export function cliHasInlineBridge(version: string | null): boolean {
  const have = versionTriplet(version);
  const need = versionTriplet(INLINE_BRIDGE_MIN_CLI);
  if (!have || !need) return false;
  for (let i = 0; i < 3; i++) {
    if (have[i]! !== need[i]!) return have[i]! > need[i]!;
  }
  return true;
}

export interface CliVersionDeps extends ProbeBinaryDeps {
  execFile: TrackingExecFileFn;
  timeoutMs?: number;
}

/** `probe --version`'s output, or null: no CLI, an error, or past the deadline. Never throws. */
export async function localCliVersion(deps: CliVersionDeps): Promise<string | null> {
  const binary = findProbeBinary(deps);
  if (!binary) return null;
  const timeoutMs = deps.timeoutMs ?? INLINE_CLI_TIMEOUT_MS;
  return new Promise((resolve) => {
    let settled = false;
    let child: { kill: (signal: "SIGKILL") => unknown } | undefined;
    const finish = (value: string | null): void => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      resolve(value);
    };
    const deadline = setTimeout(() => {
      try {
        child?.kill("SIGKILL");
      } catch {
        // Gone already: the deadline still settles.
      }
      finish(null);
    }, timeoutMs);
    try {
      child =
        deps.execFile(
          binary,
          ["--version"],
          { env: { PATH: deps.env.PATH ?? "", HOME: deps.env.HOME ?? "" }, encoding: "utf8", timeout: 0, maxBuffer: 64 * 1024 },
          (error, stdout) => finish(error ? null : stdout.trim() || null),
        ) || undefined;
    } catch {
      finish(null);
    }
  });
}

function shownPath(sessionId: string, env: PathEnv): string {
  return join(probeStateDir(env), "sessions", `${sessionId}${INLINE_SHOWN_SUFFIX}`);
}

/**
 * The stretch's `since` when the session is inline and the agent has not been
 * told about it yet, else null (`session_marker.inline_notice_due`).
 */
export function inlineNoticeDue(sessionId: string, env: PathEnv = process.env): number | null {
  const since = currentInlineSince(sessionId, env);
  if (since === null) return null;
  let shown: number | null = null;
  try {
    shown = Number.parseFloat(readFileSync(shownPath(sessionId, env), "utf8").trim());
  } catch {
    shown = null;
  }
  return shown === since ? null : since;
}

/** Best effort: the agent was told about the stretch that began at `since`. */
export function markInlineShown(sessionId: string, since: number, env: PathEnv = process.env): void {
  try {
    writeFileSync(shownPath(sessionId, env), `${since}\n`);
  } catch {
    // The worst case is one more notice.
  }
}

/** A compaction or resume took the notice out of the agent's context: tell it again. */
export function forgetInlineShown(sessionId: string, env: PathEnv = process.env): void {
  try {
    rmSync(shownPath(sessionId, env), { force: true });
  } catch {
    // Nothing to forget.
  }
}
