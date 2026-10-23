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
 * And one call it makes itself, over the service's socket (API v1: one JSON
 * object per line, `service.hello` first), because a tool call cannot wait for
 * a Python start (plan D38):
 *
 *   tool_call      ->  `hooks.run`: the call as Claude Code's PreToolUse
 *                      payload, run through a Probe plugin folder's own
 *                      pre-tool routes (`runHook`); its answer read as Claude
 *                      Code reads a PreToolUse hook's (`preToolVerdict`). The
 *                      socket's path comes from `tap service socket`, asked
 *                      once per pi process (`lookupServiceSocket`): it is keyed
 *                      by the OS's machine id, which the Python already knows
 *                      how to read on every platform. Nothing here waits past
 *                      SERVICE_HOOK_WAIT_MS (D28), and anything but a clear
 *                      answer is null: the caller's own check runs instead.
 *
 * The service has no switch; it is never started from a session whose Probe is
 * `off` (A2), and never on Windows yet: it listens on a Unix socket and locks
 * with fcntl, so every session there would try, and fail, to start it (D16).
 */

import { lstatSync } from "node:fs";
import { createConnection, type Socket } from "node:net";
import { delimiter, dirname } from "node:path";

import type { PathEnv } from "./paths.js";
import type { SpawnFn } from "./daemon.js";
import type { TapRuntime } from "./tapRuntime.js";
import type { TrackingExecFileFn } from "./trackingState.js";

/** The pid the tap's registration thread names as the owner (`service_link.py::OWNER_PID_ENV`). */
export const SERVICE_OWNER_PID_ENV = "PROBE_SERVICE_OWNER_PID";

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

/**
 * Start the service if none runs: `python -m tap service ensure`, detached.
 * Returns at once (the hook-budget rule: never wait on the service). Where the
 * service cannot run (`serviceSupported`) it starts nothing and returns false.
 */
export function ensureService(runtime: TapRuntime, deps: EnsureServiceDeps): boolean {
  if (!serviceSupported(deps.platform)) return false;
  try {
    const child = deps.spawn(runtime.python, ["-m", "tap", "service", "ensure"], {
      detached: true,
      stdio: "ignore",
      env: tapEnv(runtime, deps.env),
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
/** `protocol.HOOK_LANES`: the lanes this package runs (a hooks.json entry's argument). */
export const HookLane = { PreTool: "pre-tool" } as const;
export type HookLaneValue = (typeof HookLane)[keyof typeof HookLane];
/** D28 (`protocol.HOOK_WAIT_MS`): the most a tool call waits on the service. */
export const SERVICE_HOOK_WAIT_MS = 300;
/** What the service may spend running the event, of that wait (`timeout_ms`): the rest is the round trip. */
export const SERVICE_HOOK_RUN_MS = 250;
/** `protocol.MAX_HOOK_ENV` / `MAX_HOOK_ENV_VALUE`: what `env` may carry. */
export const MAX_HOOK_ENV = 200;
export const MAX_HOOK_ENV_VALUE = 8192;
/** `protocol.MAX_LINE_BYTES`: an answer longer than this is not read. */
const MAX_LINE_BYTES = 1024 * 1024;
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
export interface HookRun {
  /** The Probe plugin folder whose routes run (absolute). */
  pluginRoot: string;
  lane: HookLaneValue;
  /** The event as a Claude Code hook reads it on stdin. */
  payload: Record<string, unknown>;
  /** The session's environment, at most MAX_HOOK_ENV variables (`boundedHookEnv`). */
  env: Record<string, string>;
  /** The session's folder (absolute). */
  cwd: string;
  /** What the service may spend on it. */
  timeoutMs: number;
}

/** `hooks.run`'s answer (`probe.service.hookhosts.run_event`). */
export interface HookRunResult {
  /** The dispatcher's exit code; null when the service cannot say how the event ended. */
  exitCode: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
  /** Either stream was cut at the service's MAX_HOOK_OUTPUT. */
  truncated: boolean;
  /** stdout was cut: its verdict cannot be read. */
  stdoutTruncated: boolean;
  /** stderr was cut: nothing a verdict on stdout, or an exit-2 veto, depends on. */
  stderrTruncated: boolean;
  /** Where it ran: `host` (the folder's warm host) or `process`; null for nowhere. */
  via: string | null;
}

export interface HookClientDeps {
  /** This package's version, for `service.hello`. */
  clientVersion: string;
  /** Defaults to SERVICE_HOOK_WAIT_MS. */
  waitMs?: number;
  /** Defaults to a Unix-socket `net.createConnection`. */
  connect?: (path: string) => Socket;
  /** Is the socket's folder this user's alone? Defaults to `privateFolder`. */
  privateFolder?: (path: string) => boolean;
}

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

function readResult(value: unknown): HookRunResult | null {
  if (typeof value !== "object" || value === null || "refused" in value) return null;
  const v = value as Record<string, unknown>;
  const exitCode = v.exit_code;
  if (exitCode !== null && !(typeof exitCode === "number" && Number.isInteger(exitCode))) return null;
  if (typeof v.stdout !== "string" || typeof v.stderr !== "string") return null;
  return {
    exitCode: exitCode as number | null,
    stdout: v.stdout,
    stderr: v.stderr,
    timedOut: v.timed_out === true,
    truncated: v.truncated === true,
    // An answer that does not say which stream was cut: count both as cut.
    stdoutTruncated: v.stdout_truncated === true || (v.stdout_truncated === undefined && v.truncated === true),
    stderrTruncated: v.stderr_truncated === true || (v.stderr_truncated === undefined && v.truncated === true),
    via: typeof v.via === "string" ? v.via : null,
  };
}

/**
 * `hooks.run` over the service's socket, within `waitMs`: the answer, or null
 * for anything else (no socket, a folder that is not this user's alone, a
 * refusal, an error answer, an older service without the method, a timeout).
 * One connection per event; never rejects.
 */
export function runHook(socketPath: string, run: HookRun, deps: HookClientDeps): Promise<HookRunResult | null> {
  return new Promise((resolve) => {
    if (!(deps.privateFolder ?? privateFolder)(dirname(socketPath))) {
      resolve(null);
      return;
    }
    let sock: Socket | undefined;
    let settled = false;
    let buffer = "";
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (value: HookRunResult | null): void => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) clearTimeout(timer);
      sock?.destroy();
      resolve(value);
    };
    try {
      sock = (deps.connect ?? ((path: string) => createConnection({ path })))(socketPath);
    } catch {
      finish(null);
      return;
    }
    timer = setTimeout(() => finish(null), deps.waitMs ?? SERVICE_HOOK_WAIT_MS);
    const line = (id: number, method: string, params: Record<string, unknown>): string =>
      JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n";
    sock.setEncoding("utf8");
    sock.on("connect", () => {
      sock!.write(
        line(1, ServiceMethod.Hello, {
          client: SERVICE_CLIENT_PI,
          client_version: deps.clientVersion || "unknown",
          api: SERVICE_API_VERSION,
        }) +
          line(2, ServiceMethod.HooksRun, {
            plugin_root: run.pluginRoot,
            lane: run.lane,
            payload: run.payload,
            env: run.env,
            cwd: run.cwd,
            timeout_ms: run.timeoutMs,
          }),
      );
    });
    sock.on("data", (chunk: string) => {
      buffer += chunk;
      let at: number;
      while ((at = buffer.indexOf("\n")) >= 0) {
        const text = buffer.slice(0, at);
        buffer = buffer.slice(at + 1);
        if (!text.trim()) continue;
        let message: Record<string, unknown>;
        try {
          message = JSON.parse(text) as Record<string, unknown>;
        } catch {
          finish(null);
          return;
        }
        if (typeof message !== "object" || message === null || message.method === NOTIFICATION_METHOD) continue;
        if ("error" in message) {
          finish(null); // hello refused, an older service (-32601), bad params
          return;
        }
        if (message.id === 2) {
          finish(readResult(message.result));
          return;
        }
      }
      if (buffer.length > MAX_LINE_BYTES) finish(null);
    });
    sock.on("error", () => finish(null));
    sock.on("close", () => finish(null));
  });
}

/** A PreToolUse hook's `hookSpecificOutput.permissionDecision` (Claude Code's words). */
export const PermissionDecision = { Allow: "allow", Deny: "deny", Ask: "ask" } as const;
/** A hook's top-level `decision` that refuses (Claude Code's older form). */
export const HOOK_DECISION_BLOCK = "block";
/** API v1's notification method: an event, never an answer. */
const NOTIFICATION_METHOD = "event";

/** What a PreToolUse hook's answer tells the harness to do with the call. */
export const ToolVerdict = { Allow: "allow", Block: "block" } as const;
export type PreToolVerdict = { verdict: typeof ToolVerdict.Allow } | { verdict: typeof ToolVerdict.Block; reason: string };

/** A veto that gave no words of its own. */
export const VETO_WITHOUT_REASON = "A Probe hook refused this tool call before it ran.";

/**
 * A PreToolUse answer read as Claude Code reads it: exit 2 is a veto (its
 * stderr the reason), whatever else was cut; exit 0 with
 * `permissionDecision: "deny"`, `decision: "block"` or `continue: false`
 * refuses with the reason given; exit 0 with nothing (or `allow`) lets the
 * call run. A stderr cut short costs a verdict on stdout nothing. Null for
 * anything that is not a clear verdict -- out of time, an end the service
 * could not see, a cut stdout, a route that failed (any other exit), output
 * that is not a JSON object, `ask` (pi has no permission prompt here) -- and
 * the caller's own check decides. A refusal never reads as an allow.
 */
export function preToolVerdict(result: HookRunResult | null): PreToolVerdict | null {
  if (result === null || result.timedOut || result.exitCode === null) return null;
  let output: Record<string, unknown> | null = null;
  if (result.stdout.trim() && !result.stdoutTruncated) {
    try {
      const parsed: unknown = JSON.parse(result.stdout.trim());
      output = typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
    } catch {
      output = null;
    }
  }
  const specific =
    output && typeof output.hookSpecificOutput === "object" && output.hookSpecificOutput !== null
      ? (output.hookSpecificOutput as Record<string, unknown>)
      : null;
  const said = (value: unknown): string | null => (typeof value === "string" && value.trim() ? value : null);
  if (result.exitCode === 2) {
    return {
      verdict: ToolVerdict.Block,
      reason: said(result.stderr.trim()) ?? said(specific?.permissionDecisionReason) ?? said(output?.reason) ?? VETO_WITHOUT_REASON,
    };
  }
  if (result.exitCode !== 0 || result.stdoutTruncated) return null;
  if (!result.stdout.trim()) return { verdict: ToolVerdict.Allow };
  if (output === null) return null;
  const decision = specific?.permissionDecision;
  if (decision === PermissionDecision.Deny) {
    return { verdict: ToolVerdict.Block, reason: said(specific?.permissionDecisionReason) ?? said(output.reason) ?? VETO_WITHOUT_REASON };
  }
  if (output.decision === HOOK_DECISION_BLOCK || output.continue === false) {
    return { verdict: ToolVerdict.Block, reason: said(output.reason) ?? said(output.stopReason) ?? VETO_WITHOUT_REASON };
  }
  if (decision === PermissionDecision.Ask) return null;
  return { verdict: ToolVerdict.Allow };
}
