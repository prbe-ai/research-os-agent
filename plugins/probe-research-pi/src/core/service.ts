/**
 * The machine-wide Probe service, from pi: where it listens, and starting it.
 *
 *   start      ->  the CLI's own `ensure` (`python -I -m probe.service.client ensure`, detached,
 *                  never waited on), the one `ensure` every client calls (service-only T37)
 *   socket     ->  `tap service socket`, asked once per pi process (`lookupServiceSocket`): it is
 *                  keyed by the OS's machine id, which the Python already knows how to read
 *   capture    ->  the service's own (M9): it asks for the session's recorder at pi's session
 *                  start, naming the pi process it verified as the owner, so it sees the session
 *                  end the moment pi exits
 *
 * Every event goes to the service as `hooks.run` v2 (`serviceForward.ts`); nothing runs here.
 */

import { spawn as nodeSpawn } from "node:child_process";
import { existsSync, lstatSync } from "node:fs";
import { createConnection, type Socket } from "node:net";
import { constants as osConstants } from "node:os";
import { delimiter, dirname, join } from "node:path";

import type { PathEnv } from "./paths.js";
import type { SpawnFn } from "./daemon.js";
import type { TapRuntime } from "./tapRuntime.js";
import type { TrackingExecFileFn } from "./trackingState.js";

/** The platforms the service cannot run on (Node's `process.platform` names): none
 *  since R3, when Windows got its named-pipe transport. */
export const ServicelessPlatform = {} as const satisfies Record<string, NodeJS.Platform>;

const SERVICELESS: ReadonlySet<string> = new Set(Object.values(ServicelessPlatform));

/** Can the service run here? */
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

/** The environment a `python -m tap ...` child runs with: ours, the tap on PYTHONPATH. */
function tapEnv(runtime: TapRuntime, env: PathEnv): PathEnv {
  const pythonPath = runtime.tapRoot
    ? [runtime.tapRoot, env.PYTHONPATH].filter((v): v is string => Boolean(v)).join(delimiter)
    : env.PYTHONPATH;
  return { ...env, ...(pythonPath ? { PYTHONPATH: pythonPath } : {}) };
}

/** What `ensure` runs: the CLI's client, isolated (`-I`: no PYTHONPATH or project module in the way). */
export const ENSURE_ARGS = ["-I", "-m", "probe.service.client", "ensure"] as const;

/**
 * Start the service if none runs: the CLI's own `ensure` on the CLI's interpreter (`cliPython`:
 * runtime.json's, else uv's; null when no CLI is installed here: nothing to start). Detached and
 * returns at once (never wait on the service). Where the service cannot run (`serviceSupported`)
 * it starts nothing and returns false.
 */
export function ensureService(cliPython: string | null, deps: EnsureServiceDeps): boolean {
  if (!cliPython || !serviceSupported(deps.platform)) return false;
  try {
    const child = deps.spawn(cliPython, [...ENSURE_ARGS], {
      detached: true,
      stdio: "ignore",
      env: deps.env,
    });
    child.unref();
    return true;
  } catch (err) {
    deps.log(`probe service: ensure did not start: ${err instanceof Error ? err.message : String(err)}`);
    return false;
  }
}

// -- hooks.run (plan D38) ------------------------------------------------------

/** API v1 (`probe.service.protocol.API_VERSION`). */
export const SERVICE_API_VERSION = 1;
/** `protocol.Client.PI`: who this client is to the service. */
export const SERVICE_CLIENT_PI = "pi"; // harness-literal-ok: an API v1 client kind (the contract), not a harness lookup
/** `protocol.Method`: what this client sends. */
export const ServiceMethod = { Hello: "service.hello", HooksRun: "hooks.run" } as const;
/** `protocol.MAX_HOOK_ENV` / `MAX_HOOK_ENV_VALUE`: what `env` may carry. */
export const MAX_HOOK_ENV = 200;
export const MAX_HOOK_ENV_VALUE = 8192;
/** How long `tap service socket` may take (a Python start). */
const SOCKET_LOOKUP_TIMEOUT_MS = 10_000;

export interface SocketLookupDeps {
  execFile: TrackingExecFileFn;
  env: PathEnv;
  log: (message: string) => void;
  /** Defaults to `process.platform`; tests name another. */
  platform?: NodeJS.Platform;
}

/**
 * This machine's service socket: `python -m tap service socket` (the tap's copy
 * of `probe.service.paths.socket_path`), or null when it cannot be had (no
 * service on this platform, an older tap without the command). Never rejects.
 */
export function lookupServiceSocket(runtime: TapRuntime, deps: SocketLookupDeps): Promise<string | null> {
  if (!serviceSupported(deps.platform)) return Promise.resolve(null);
  return new Promise((resolve) => {
    try {
      deps.execFile(
        runtime.python,
        ["-m", "tap", "service", "socket"],
        { env: tapEnv(runtime, deps.env), encoding: "utf8", timeout: SOCKET_LOOKUP_TIMEOUT_MS, maxBuffer: 64 * 1024 },
        (error, stdout) => {
          if (error) {
            deps.log(`probe service: no socket path: ${error.message}`);
            resolve(null);
            return;
          }
          try {
            const last = String(stdout).trim().split("\n").pop() ?? "";
            const socket = (JSON.parse(last) as { socket?: unknown } | null)?.socket;
            resolve(typeof socket === "string" && socket.startsWith("/") ? socket : null);
          } catch {
            resolve(null);
          }
        },
      );
    } catch (err) {
      deps.log(`probe service: no socket path: ${err instanceof Error ? err.message : String(err)}`);
      resolve(null);
    }
  });
}

/** One event for `hooks.run`, as the service checks it (`protocol.need_hook_run`). */
/**
 * `paths.require_private`, as the Python client checks before it trusts what
 * answers there: a folder, ours, writable by nobody else. On the /tmp fallback
 * any local user could have made the folder first and listen in it.
 */
export function privateFolder(path: string): boolean {
  try {
    const st = lstatSync(path);
    const uid = typeof process.getuid === "function" ? process.getuid() : -1;
    return st.isDirectory() && st.uid === uid && (st.mode & 0o022) === 0;
  } catch {
    return false;
  }
}

/** The session's environment as `hooks.run` takes it: strings only, nothing the
 * service refuses, at most MAX_HOOK_ENV of them (Probe's, the XDG folders and
 * the agents' first: what the routes read). */
export function boundedHookEnv(env: PathEnv): Record<string, string> {
  const usable = Object.entries(env).filter(
    (entry): entry is [string, string] =>
      typeof entry[1] === "string" &&
      entry[1].length <= MAX_HOOK_ENV_VALUE &&
      !entry[1].includes("\0") &&
      entry[0].length > 0 &&
      entry[0].length <= 256 &&
      !entry[0].includes("=") &&
      !entry[0].includes("\0"),
  );
  const first = /^(PROBE_|XDG_|PI_|CLAUDE_|CODEX_|KIMI_|HOME$|PATH$|USER$|LOGNAME$|SHELL$|LANG$|LC_|TZ$|TMPDIR$)/;
  const ordered = [...usable.filter(([name]) => first.test(name)), ...usable.filter(([name]) => !first.test(name))];
  return Object.fromEntries(ordered.slice(0, MAX_HOOK_ENV));
}
