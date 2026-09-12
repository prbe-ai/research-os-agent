/**
 * Pi's host bridge to the canonical Python tracking resolver.
 *
 * Folder inheritance and atomic signal publication stay in one place: the
 * probe CLI. Pi asks the hidden `session initialize` command for the durable
 * state and only renders what that command returns.
 */

import { type PathEnv } from "./paths.js";
import { findProbeBinary, type ProbeBinaryDeps } from "./teamNote.js";

/**
 * Is a capture daemon live for this session, and if not, why not.
 *
 * `reason` comes from the CLI's own closed vocabulary (`probe session
 * status`'s `capture.reason`) and is rendered verbatim — this side never
 * invents or rewords one, so the footer and the CLI cannot describe the same
 * situation differently.
 */
export interface CaptureReading {
  running: boolean;
  reason: string;
}

export interface TrackingState {
  tracking: boolean;
  signal: "on" | "off";
  seeded: boolean;
  source: string;
  /**
   * Absent when the probe CLI on this machine predates the `capture` block
   * (`session initialize` grew it alongside the third state). Absent is not
   * "not capturing" — it is "this CLI cannot say" — which is why it is
   * optional rather than defaulted, and why the footer falls back to the
   * plain two-state text when it is missing.
   */
  capture?: CaptureReading;
}

export interface TrackingExecOptions {
  env: PathEnv;
  encoding: "utf8";
  timeout: number;
  maxBuffer: number;
}

export interface TrackingChild {
  kill: (signal: "SIGKILL") => unknown;
}

export type TrackingExecFileFn = (
  command: string,
  args: string[],
  options: TrackingExecOptions,
  callback: (error: Error | null, stdout: string, stderr: string) => void,
) => TrackingChild | void;

export interface TrackingStateDeps extends ProbeBinaryDeps {
  execFile: TrackingExecFileFn;
  log: (message: string) => void;
  timeoutMs?: number;
}

const CHILD_ENV_KEYS = [
  "PATH",
  "PATHEXT",
  "HOME",
  "USERPROFILE",
  "XDG_STATE_HOME",
  "XDG_CONFIG_HOME",
  "LOCALAPPDATA",
  "APPDATA",
  "SystemRoot",
  "TMPDIR",
  "TMP",
  "TEMP",
  "PROBE_CONFIG_PATH",
  "PROBE_SESSION_TRACKING",
] as const;

function trackingChildEnv(env: PathEnv): PathEnv {
  const child: PathEnv = { PROBE_AGENT: "pi" };
  for (const key of CHILD_ENV_KEYS) {
    if (env[key] !== undefined) child[key] = env[key];
  }
  return child;
}

export async function initializeTrackingState(
  sessionId: string,
  cwd: string,
  deps: TrackingStateDeps,
): Promise<TrackingState | null> {
  const binary = findProbeBinary(deps);
  if (!binary) {
    deps.log("tracking initialization skipped: no probe CLI found");
    return null;
  }

  const timeoutMs = deps.timeoutMs ?? 5_000;
  let result: { error: Error | null; stdout: string; stderr: string };
  try {
    result = await new Promise((resolve, reject) => {
      let settled = false;
      let child: TrackingChild | undefined;
      const finish = (value: typeof result): void => {
        if (settled) return;
        settled = true;
        clearTimeout(deadline);
        resolve(value);
      };
      const deadline = setTimeout(() => {
        if (settled) return;
        try {
          child?.kill("SIGKILL");
        } catch {
          // The deadline still settles startup even if the process vanished
          // between the timeout and the kill.
        }
        finish({
          error: new Error(`timed out after ${timeoutMs}ms`),
          stdout: "",
          stderr: "",
        });
      }, timeoutMs);

      try {
        child = deps.execFile(
          binary,
          ["session", "initialize", "--session", sessionId, "--cwd", cwd],
          {
            env: trackingChildEnv(deps.env),
            encoding: "utf8",
            // The promise's own deadline is authoritative. Node's execFile
            // timeout waits for pipe closure after signaling the child, which
            // can still hang when a descendant retains stdout/stderr.
            timeout: 0,
            maxBuffer: 64 * 1024,
          },
          (error, stdout, stderr) => finish({ error, stdout, stderr }),
        ) || undefined;
      } catch (err) {
        clearTimeout(deadline);
        reject(err);
      }
    });
  } catch (err) {
    deps.log(
      `tracking initialization failed: ${err instanceof Error ? err.message : String(err)}`,
    );
    return null;
  }

  if (result.error) {
    deps.log(`tracking initialization failed: ${result.error.message}`);
    return null;
  }

  try {
    const value = JSON.parse(result.stdout) as Record<string, unknown>;
    if (
      value.session_id !== sessionId ||
      typeof value.tracking !== "boolean" ||
      (value.signal !== "on" && value.signal !== "off") ||
      value.tracking !== (value.signal === "on") ||
      typeof value.seeded !== "boolean" ||
      typeof value.source !== "string"
    ) {
      throw new Error("invalid tracking state payload");
    }
    return {
      tracking: value.tracking,
      signal: value.signal,
      seeded: value.seeded,
      source: value.source,
      capture: parseCapture(value.capture),
    };
  } catch (err) {
    deps.log(
      `tracking initialization returned invalid JSON: ${err instanceof Error ? err.message : String(err)}`,
    );
    return null;
  }
}

/**
 * The optional half of the payload, read LENIENTLY on purpose.
 *
 * Every other field above is validated strictly and a failure discards the
 * whole read — those fields are the tracking signal itself, and rendering a
 * guess about that is the one thing this module must never do. `capture` is
 * different: it arrived later than the CLI contract around it, so a probe
 * that does not send it, or sends a shape this version does not recognise,
 * must still leave the tracking state renderable. Unrecognised means absent,
 * never `{running: false}` — "no capture" is a claim, and this side is not
 * entitled to make it on the strength of a field it could not read.
 */
function parseCapture(raw: unknown): CaptureReading | undefined {
  if (typeof raw !== "object" || raw === null) return undefined;
  const { running, reason } = raw as { running?: unknown; reason?: unknown };
  if (typeof running !== "boolean" || typeof reason !== "string") return undefined;
  return { running, reason };
}

/**
 * The footer, in pi's idiom. THREE states, matching `probe session status`'s
 * `effective` field exactly — a footer that said "tracking" while the CLI
 * said "tracked, not captured" would be the same lie in a smaller font.
 *
 * Capture is never mentioned when tracking is off: it may still be running,
 * and "not tracking" is the researcher's decision, not a capture report.
 */
export function trackingStatusText(tracking: boolean, capture?: CaptureReading): string {
  if (!tracking) return "○ not tracking";
  if (!capture || capture.running) return "● tracking";
  return `◐ tracking · no capture: ${capture.reason}`;
}
