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
 * Only while the service is switched on: `PROBE_SERVICE=on|off`, else
 * `"service": "on"` in the probe CLI's config.json (the same rule as the
 * client's `enabled()`), and never in a session whose Probe is `off` (A2).
 */

import { delimiter } from "node:path";

import { probeConfigPath, type PathEnv } from "./paths.js";
import type { SpawnFn } from "./daemon.js";
import type { TapRuntime } from "./tapRuntime.js";

/** The switch's environment variable (`tap/service/client.py::ENV_FLAG`). */
export const SERVICE_ENV = "PROBE_SERVICE";
/** Its config.json key (`client.py::CONFIG_KEY`). */
export const SERVICE_CONFIG_KEY = "service";
/** The pid the tap's registration thread names as the owner (`service_link.py::OWNER_PID_ENV`). */
export const SERVICE_OWNER_PID_ENV = "PROBE_SERVICE_OWNER_PID";

const ON = new Set(["on", "1", "true", "yes"]);
const OFF = new Set(["off", "0", "false", "no"]);

export interface ServiceSwitchDeps {
  env: PathEnv;
  readFileSync: (path: string) => string;
}

/** Is the Probe service switched on for this machine? Never throws. */
export function serviceEnabled(deps: ServiceSwitchDeps): boolean {
  const raw = (deps.env[SERVICE_ENV] ?? "").trim().toLowerCase();
  if (ON.has(raw)) return true;
  if (OFF.has(raw)) return false;
  try {
    const config = JSON.parse(deps.readFileSync(probeConfigPath(deps.env))) as Record<string, unknown>;
    const value = config?.[SERVICE_CONFIG_KEY];
    return value === true || (typeof value === "string" && ON.has(value.trim().toLowerCase()));
  } catch {
    return false;
  }
}

export interface EnsureServiceDeps {
  spawn: SpawnFn;
  env: PathEnv;
  log: (message: string) => void;
}

/**
 * Start the service if none runs: `python -m tap service ensure`, detached.
 * Returns at once (the hook-budget rule: never wait on the service).
 */
export function ensureService(runtime: TapRuntime, deps: EnsureServiceDeps): boolean {
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
