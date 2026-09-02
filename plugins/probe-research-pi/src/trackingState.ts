/**
 * Pi's host bridge to the canonical Python tracking resolver.
 *
 * Folder inheritance and atomic signal publication stay in one place: the
 * probe CLI. Pi asks the hidden `session initialize` command for the durable
 * state and only renders what that command returns.
 */

import { type PathEnv } from "./paths.js";
import { findProbeBinary, type ProbeBinaryDeps } from "./teamNote.js";

export interface TrackingState {
  tracking: boolean;
  signal: "on" | "off";
  seeded: boolean;
  source: string;
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
    };
  } catch (err) {
    deps.log(
      `tracking initialization returned invalid JSON: ${err instanceof Error ? err.message : String(err)}`,
    );
    return null;
  }
}

export function trackingStatusText(tracking: boolean): string {
  return tracking ? "● tracking" : "○ not tracking";
}
