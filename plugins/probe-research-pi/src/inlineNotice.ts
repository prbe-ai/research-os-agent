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
 */

import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { currentInlineSince } from "./guard.js";
import { probeStateDir, type PathEnv } from "./core/paths.js";

/** `session_marker.INLINE_NOTICE`, verbatim (Richard's approved text, 2026-10-06). */
export const INLINE_NOTICE =
  "Probe is `on (inline)` for this conversation: the Probe daemon is off, and you read and write Probe yourself with the `probe` CLI, recording the work as it happens. Before recording, read the two skills you record by: {track_work} and {edit_notes}. This session has no Probe MCP tools: where a skill names one, use its `probe` CLI command (`probe --help`).";

/** `session_marker.INLINE_SHOWN_SUFFIX`. */
export const INLINE_SHOWN_SUFFIX = ".inline-shown";

/** The notice, naming this package's copies of the two skills. */
export function inlineNotice(extensionDir: string): string {
  return INLINE_NOTICE.replace("{track_work}", join(extensionDir, "skills", "track-work", "SKILL.md")).replace(
    "{edit_notes}",
    join(extensionDir, "skills", "edit-notes", "SKILL.md"),
  );
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
