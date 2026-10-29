/**
 * What an inline session still owes at the end of a turn (notes parity N3),
 * pi's half of the plugin's Stop hook (`hooks/owed_hook.py`): the CLI keeps the
 * ledger and its words (`probe session owed --send-back`: a summary for a
 * project or experiment the agent created or concluded, a note on a run it
 * tagged invalid, each at most twice a session); this only looks first whether
 * the ledger holds an item of the agent's own (`session_marker.agent_owes`), so
 * a turn that owes nothing starts no CLI. Never throws.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { probeStateDir, type PathEnv } from "./core/paths.js";
import { findProbeBinary, type ProbeBinaryDeps } from "./core/teamNote.js";
import { trackingChildEnv, type TrackingExecFileFn } from "./core/trackingState.js";

/** `session_marker.OWED_DIRNAME`. */
export const OWED_DIRNAME = "owed";

/** `owed_hook.CLI_TIMEOUT_S`, in ms. */
export const OWED_CLI_TIMEOUT_MS = 8_000;

/** `session_marker.OWED_MAX_ASKS`: how many times one item is asked about. */
export const OWED_MAX_ASKS = 2;

/** `session_marker.SUMMARIES_MODE_TTL_S`, in ms: how long a seen summaries mode stands. */
export const SUMMARIES_MODE_TTL_MS = 24 * 3600 * 1000;

/** Who wrote a ledger item (`owed.BY_AGENT`) and what it owes (`owed.SUMMARY`). */
export enum OwedBy {
  Agent = "agent",
}
export enum OwedWhy {
  Summary = "summary",
}
const RECORDER_MODE = "recorder";

/** `session_marker.seen_recorder_mode`: this machine last saw the team's summaries in recorder mode. */
function seenRecorderMode(env: PathEnv): boolean {
  try {
    const seen = JSON.parse(readFileSync(join(probeStateDir(env), "summaries", "mode.json"), "utf8"));
    return seen?.mode === RECORDER_MODE && Date.now() - Number(seen?.at ?? 0) * 1000 < SUMMARIES_MODE_TTL_MS;
  } catch {
    return false;
  }
}

/**
 * Could the CLI send the agent back now (`session_marker.agent_owes`)? An item
 * of the agent's own that was asked about fewer than OWED_MAX_ASKS times, a
 * summary only while recorder mode was seen: an exhausted ledger starts no CLI.
 */
export function agentOwes(sessionId: string, env: PathEnv = process.env): boolean {
  try {
    const data = JSON.parse(readFileSync(join(probeStateDir(env), OWED_DIRNAME, `${sessionId}.json`), "utf8"));
    const items: unknown = data?.items;
    const asks: Record<string, unknown> = data?.asks && typeof data.asks === "object" ? data.asks : {};
    const recorder = seenRecorderMode(env);
    return Array.isArray(items) && items.some((item) => item && typeof item === "object" && item.by === OwedBy.Agent
      && Number(asks[`${item.why} ${item.kind} ${item.ref}`] ?? 0) < OWED_MAX_ASKS
      && (item.why !== OwedWhy.Summary || recorder));
  } catch {
    return false;
  }
}

export interface OwedDeps extends ProbeBinaryDeps {
  execFile: TrackingExecFileFn;
  timeoutMs?: number;
}

/** What the agent is sent back with (`probe session owed --send-back`), or null. */
export async function owedSendBack(sessionId: string, deps: OwedDeps): Promise<string | null> {
  const binary = findProbeBinary(deps);
  if (!binary) return null;
  const env = trackingChildEnv(deps.env);
  return new Promise((resolve) => {
    try {
      deps.execFile(
        binary,
        ["session", "owed", "--send-back", "--session", sessionId],
        { env, encoding: "utf8", timeout: deps.timeoutMs ?? OWED_CLI_TIMEOUT_MS, maxBuffer: 64 * 1024 },
        (error, stdout) => resolve(error ? null : stdout.trim() || null),
      );
    } catch {
      resolve(null);
    }
  });
}
