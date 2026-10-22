/**
 * The machine-wide Probe service (plan T5), from pi.
 *
 * pi cannot import the service's Python client, so it runs the tap's byte copy
 * of it (`python -m tap service ...`, `tap/service/client.py`), with the same
 * interpreter and PYTHONPATH it starts capture with (`tapRuntime.ts`):
 *
 *   session_start  ->  `tap service ensure`, detached and never waited on, so
 *                      the service is up by the time capture registers
 *   capture        ->  `tap start` gets PROBE_SERVICE_OWNER_PID = this pi
 *                      process, so the tap's registration thread (`tap/
 *                      service_link.py`) names pi as the session's owner and
 *                      the service sees the session end the moment pi exits
 *   session_shutdown -> stopping capture ends the registration (`session.end`)
 *
 * The service has no switch; it is never started from a session whose Probe is
 * `off` (A2), and never on Windows yet: it listens on a Unix socket and locks
 * with fcntl, so every session there would try, and fail, to start it (D16).
 */

import { delimiter } from "node:path";

import type { PathEnv } from "./paths.js";
import type { SpawnFn } from "./daemon.js";
import type { TapRuntime } from "./tapRuntime.js";

/** The pid the tap's registration thread names as the owner (`service_link.py::OWNER_PID_ENV`). */
export const SERVICE_OWNER_PID_ENV = "PROBE_SERVICE_OWNER_PID";

/** The platforms the service cannot run on yet (Node's `process.platform` names). */
export const ServicelessPlatform = { Windows: "win32" } as const satisfies Record<string, NodeJS.Platform>;

const SERVICELESS: ReadonlySet<string> = new Set(Object.values(ServicelessPlatform));

/** Can the service run here? Not on Windows until it has a named-pipe transport. */
export function serviceSupported(platform: NodeJS.Platform = process.platform): boolean {
  return !SERVICELESS.has(platform);
}

export interface EnsureServiceDeps {
  spawn: SpawnFn;
  env: PathEnv;
  log: (message: string) => void;
  /** Defaults to `process.platform`; tests name another. */
  platform?: NodeJS.Platform;
}

/**
 * Start the service if none runs: `python -m tap service ensure`, detached.
 * Returns at once (the hook-budget rule: never wait on the service). Where the
 * service cannot run (`serviceSupported`) it starts nothing and returns false.
 */
export function ensureService(runtime: TapRuntime, deps: EnsureServiceDeps): boolean {
  if (!serviceSupported(deps.platform)) return false;
  const pythonPath = runtime.tapRoot
    ? [runtime.tapRoot, deps.env.PYTHONPATH].filter((v): v is string => Boolean(v)).join(delimiter)
    : deps.env.PYTHONPATH;
  try {
    const child = deps.spawn(runtime.python, ["-m", "tap", "service", "ensure"], {
      detached: true,
      stdio: "ignore",
      env: { ...deps.env, ...(pythonPath ? { PYTHONPATH: pythonPath } : {}) },
    });
    child.unref();
    return true;
  } catch (err) {
    deps.log(`probe service: ensure did not start: ${err instanceof Error ? err.message : String(err)}`);
    return false;
  }
}
