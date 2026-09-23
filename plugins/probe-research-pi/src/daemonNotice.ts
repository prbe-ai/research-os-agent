/**
 * The `daemon` state's two messages to the model, for pi.
 *
 * On Claude Code and Codex these come from hooks: `version_check.py` adds
 * `DAEMON_CONTEXT` at session start, and `tracking_guard.py::_daemon_notice`
 * adds a one-off notice at the prompt where recording CHANGES hands -- the
 * daemon's lease lapsed ("recording is back with you") or came back. pi has no
 * hooks, so `before_agent_start` does both here.
 *
 * THE FILES, NOT THE CLI. `before_agent_start` fires on every turn and must stay
 * cheap, so this reads the lease (`<state>/sessions/<sid>.writer`) and the
 * shared once-per-change record (`<sid>.writer-notified`) directly instead of
 * spawning `probe`. Both formats are the Python side's; the notified record is
 * the SAME file the guard hook writes, so the two surfaces cannot announce one
 * change twice. Every string below is verbatim from the Python constants named
 * beside it, and `agent/tests/test_companion_pi_parity.py` checks that.
 */

import { join } from "node:path";

import { probeConfigPath, probeStateDir, type PathEnv } from "./paths.js";

/** `version_check.DAEMON_CONTEXT`. */
export const DAEMON_CONTEXT =
  "The Probe daemon is recording this session: it reads the transcript and files names, " +
  "tags, notes, artifacts, papers, lineage and run ends itself, so do not write to Probe " +
  "yourself. You still start runs yourself (`probe exec`, or the SDK in the script).";

/** `tracking_guard.FLIP_NOTICE["daemon"]`: the daemon (again) holds the writes. */
export const DAEMON_LIVE_NOTICE =
  "Probe is now in the `daemon` state for this conversation; it handles all WRITES to " +
  "Probe from here-on out except for SDK usage in run scripts.";

/** `session_marker.DAEMON_DEGRADED_NOTICE`. */
export const DAEMON_DEGRADED_NOTICE =
  "Probe is in the `daemon` state, but the daemon is {reason}, so recording is back with " +
  "you. Record the work as it happens, per `track-work`.";

/** Why the daemon is not recording; the lease's `reason`. `session_marker`'s vocabulary. */
export const DaemonReason = {
  NotStarted: "not-started",
  Expired: "expired",
  Stopped: "stopped",
  Gateway: "gateway",
  Budget: "budget",
  Unauthorized: "unauthorized",
  Error: "error",
} as const;

/** `session_marker.DAEMON_REASON_WORDS`. */
export const DAEMON_REASON_WORDS: Readonly<Record<string, string>> = {
  [DaemonReason.NotStarted]: "not running",
  [DaemonReason.Expired]: "not responding",
  [DaemonReason.Stopped]: "stopped",
  [DaemonReason.Gateway]: "unable to reach its model",
  [DaemonReason.Budget]: "out of budget",
  [DaemonReason.Unauthorized]: "not authorized",
  [DaemonReason.Error]: "hitting an error",
};

/** `session_marker.LEASE_VERSION`, `LEASE_SUFFIX`, `NOTIFIED_SUFFIX`, and the lease's writer. */
const LEASE_VERSION = 1;
export const LEASE_SUFFIX = ".writer";
export const NOTIFIED_SUFFIX = ".writer-notified";
const LEASE_WRITER = "daemon";

export const DaemonStatus = {
  Live: "live",
  Degraded: "degraded",
} as const;
export type DaemonStatusValue = (typeof DaemonStatus)[keyof typeof DaemonStatus];

export interface DaemonNoticeDeps {
  env: PathEnv;
  readFileSync: (path: string) => string;
  writeFileSync: (path: string, content: string) => void;
  mkdirSync: (path: string) => void;
  rmSync: (path: string) => void;
  now: () => number;
}

function sessionsDir(env: PathEnv): string {
  return join(probeStateDir(env), "sessions");
}

/** `session_marker.companion_key_held`: the active context carries `companion_token`. */
export function companionKeyHeld(deps: DaemonNoticeDeps): boolean {
  let data: unknown;
  try {
    data = JSON.parse(deps.readFileSync(probeConfigPath(deps.env)));
  } catch {
    return false;
  }
  if (typeof data !== "object" || data === null) return false;
  let context = data as Record<string, unknown>;
  const contexts = context.contexts;
  if (typeof contexts === "object" && contexts !== null) {
    const name = typeof context.current_context === "string" && context.current_context ? context.current_context : "default";
    const active = (contexts as Record<string, unknown>)[name];
    context = typeof active === "object" && active !== null ? (active as Record<string, unknown>) : {};
  }
  const token = context.companion_token;
  return typeof token === "string" && token.trim().length > 0;
}

/**
 * `session_marker.daemon_status` for a session already known to be in the
 * `daemon` state. A missing, malformed or unknown-version lease reads as
 * degraded: a broken file hands writing back to the agent, never locks it out.
 */
export function daemonStatus(
  sessionId: string,
  deps: DaemonNoticeDeps,
): { status: DaemonStatusValue; reason: string | null } {
  let lease: unknown;
  try {
    lease = JSON.parse(deps.readFileSync(join(sessionsDir(deps.env), `${sessionId}${LEASE_SUFFIX}`)));
  } catch {
    return { status: DaemonStatus.Degraded, reason: DaemonReason.NotStarted };
  }
  if (typeof lease !== "object" || lease === null) {
    return { status: DaemonStatus.Degraded, reason: DaemonReason.NotStarted };
  }
  const { v, writer, expires_at: expiresAt, reason } = lease as Record<string, unknown>;
  if (v !== LEASE_VERSION || writer !== LEASE_WRITER || typeof expiresAt !== "number") {
    return { status: DaemonStatus.Degraded, reason: DaemonReason.NotStarted };
  }
  if (typeof reason === "string" && reason) {
    return { status: DaemonStatus.Degraded, reason };
  }
  if (expiresAt <= deps.now() / 1000) {
    return { status: DaemonStatus.Degraded, reason: DaemonReason.Expired };
  }
  return { status: DaemonStatus.Live, reason: null };
}

/**
 * The notice for a CHANGE in who records, once per change, or null.
 * `tracking_guard._daemon_notice`, line for line: a steady live daemon says
 * nothing (session start already said it records), a lapse says recording is
 * back with the agent, a recovery says the daemon has it again, and
 * `not-started` gets one prompt of grace because the worker is spawned at
 * session start and may not have taken its first lease yet.
 */
export function daemonNotice(
  sessionId: string,
  inDaemonState: boolean,
  deps: DaemonNoticeDeps,
): string | null {
  const path = join(sessionsDir(deps.env), `${sessionId}${NOTIFIED_SUFFIX}`);
  let last: { status?: unknown; prompts?: unknown } = {};
  try {
    const parsed: unknown = JSON.parse(deps.readFileSync(path));
    if (typeof parsed === "object" && parsed !== null) last = parsed as typeof last;
  } catch {
    last = {};
  }
  if (!inDaemonState) {
    if (Object.keys(last).length > 0) {
      try {
        deps.rmSync(path);
      } catch {
        // Best-effort: a stale record only costs one missed announcement.
      }
    }
    return null;
  }
  const { status, reason } = daemonStatus(sessionId, deps);
  const prompts = (typeof last.prompts === "number" ? last.prompts : 0) + 1;
  const previous = typeof last.status === "string" ? last.status : null;
  let notice: string | null = null;
  let current: string | null = previous;
  if (status === DaemonStatus.Live) {
    if (previous === DaemonStatus.Degraded) notice = DAEMON_LIVE_NOTICE;
    current = status;
  } else if (
    reason === DaemonReason.NotStarted &&
    previous === null &&
    prompts < 2 &&
    companionKeyHeld(deps)
  ) {
    // Grace only when the worker CAN start: with no key it never will.
    current = null;
  } else {
    if (previous !== DaemonStatus.Degraded) {
      notice = DAEMON_DEGRADED_NOTICE.replace(
        "{reason}",
        DAEMON_REASON_WORDS[reason ?? ""] ?? "not running",
      );
    }
    current = DaemonStatus.Degraded;
  }
  try {
    deps.mkdirSync(sessionsDir(deps.env));
    deps.writeFileSync(path, JSON.stringify({ status: current, prompts }));
  } catch {
    // Best-effort, as on the Python side.
  }
  return notice;
}
