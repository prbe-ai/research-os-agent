/**
 * The agent finished a turn: tell the Probe daemon where it ended.
 *
 * pi's analogue of the tap's Stop hook (`probe-research-tap/hooks/turn-end.sh`,
 * which Claude Code and Codex run). Writes
 * `<state>/probe/sessions/<sid>.turn` = `{"offset": <transcript size>, "at":
 * <epoch seconds>}` when this session is in the `daemon` state. The worker
 * (`companion_worker.TURN_SUFFIX`) then reads the transcript up to that offset
 * at once instead of waiting for quiet, and runs its conclusions pass over the
 * whole session, so what the agent concluded in its last message is recorded
 * while the researcher is still reading it.
 *
 * Silent and harmless on every failure: it never throws, writes nothing
 * outside `daemon` and nothing outside the sessions directory, and replaces
 * the file atomically (the worker may read it at any moment).
 */

import { join } from "node:path";

import { probeStateDir, type PathEnv } from "./paths.js";
import { ProbeState } from "./trackingState.js";

export const TURN_SUFFIX = ".turn";
export const STATE_SUFFIX = ".state";
const SESSION_ID = /^[A-Za-z0-9._-]{1,128}$/;

export interface TurnSignalDeps {
  env: PathEnv;
  now: () => number;
  pid: number;
  readFileSync: (path: string) => string;
  sizeOf: (path: string) => number;
  writeFileSync: (path: string, content: string) => void;
  renameSync: (from: string, to: string) => void;
}

/** Write the turn signal; true when it was written. */
export function writeTurnSignal(
  sessionId: string | undefined,
  transcriptPath: string | undefined,
  deps: TurnSignalDeps,
): boolean {
  if (!sessionId || !SESSION_ID.test(sessionId) || !transcriptPath) return false;
  try {
    const dir = join(probeStateDir(deps.env), "sessions");
    const state = deps.readFileSync(join(dir, sessionId + STATE_SUFFIX)).trim().toLowerCase();
    if (state !== ProbeState.Daemon) return false;
    const offset = deps.sizeOf(transcriptPath);
    const tmp = join(dir, `.${sessionId}.${deps.pid}${TURN_SUFFIX}.tmp`);
    deps.writeFileSync(tmp, JSON.stringify({ offset, at: deps.now() / 1000 }));
    deps.renameSync(tmp, join(dir, sessionId + TURN_SUFFIX));
    return true;
  } catch {
    return false;
  }
}
