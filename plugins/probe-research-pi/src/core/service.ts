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

import { spawn as nodeSpawn } from "node:child_process";
import { existsSync, lstatSync } from "node:fs";
import { createConnection, type Socket } from "node:net";
import { constants as osConstants } from "node:os";
import { delimiter, dirname, join } from "node:path";

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

/**
 * `hooks.run` refused: the service will not run this event for this caller
 * (it cannot rule out a sandbox around pi: `caller.sandboxed`), and says so.
 * Unlike no answer at all, the event is then run here (`runHookLocally`).
 */
export interface HookRunRefused {
  refused: string;
  /** `RefusedReason`, from services that give one (`sandboxed`); else null. */
  reason: string | null;
}

/** `protocol.RefusedReason.SANDBOXED`, and the words a service from before
 * `reason` refuses a sandboxed caller with. */
export const REFUSED_SANDBOXED = "sandboxed";
export const REFUSED_SANDBOXED_WORDS = "a caller in a sandbox";

export function isRefused(value: HookRunResult | HookRunRefused | null): value is HookRunRefused {
  return value !== null && typeof (value as HookRunRefused).refused === "string";
}

/** A refusal for this caller's confinement (not busy, stopping, a folder it will not run). */
export function refusedForSandbox(value: HookRunRefused): boolean {
  return value.reason === REFUSED_SANDBOXED || (value.reason === null && value.refused === REFUSED_SANDBOXED_WORDS);
}

function readResult(value: unknown): HookRunResult | HookRunRefused | null {
  if (typeof value !== "object" || value === null) return null;
  if ("refused" in value) {
    const why = (value as Record<string, unknown>).refused;
    const reason = (value as Record<string, unknown>).reason;
    return { refused: typeof why === "string" ? why : "refused", reason: typeof reason === "string" ? reason : null };
  }
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
 * `hooks.run` over the service's socket, within `waitMs`: the answer, its
 * refusal (`HookRunRefused`), or null for anything else (no socket, a folder
 * that is not this user's alone, an error answer, an older service without
 * the method, a timeout). One connection per event; never rejects.
 */
export function runHook(socketPath: string, run: HookRun, deps: HookClientDeps): Promise<HookRunResult | HookRunRefused | null> {
  return new Promise((resolve) => {
    if (!(deps.privateFolder ?? privateFolder)(dirname(socketPath))) {
      resolve(null);
      return;
    }
    let sock: Socket | undefined;
    let settled = false;
    let buffer = "";
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (value: HookRunResult | HookRunRefused | null): void => {
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

/** The variables a fresh dispatcher of the service's sets itself, never the
 * caller's (`hookhosts.RUN_OWNED_ENV`). */
export const RUN_OWNED_ENV: readonly string[] = [
  "PLUGIN_ROOT",
  "CLAUDE_PLUGIN_ROOT",
  "PROBE_HOOKS_HANDED_OVER",
  "PROBE_HOOK_HOSTED",
  "PROBE_HOOK_HOST",
  "PROBE_HOOK_DEADLINE",
  "PROBE_HOOK_PARENT_PID",
];
/** A local run's stdout and stderr are each read this far (`hookhosts.MAX_HOOK_OUTPUT`). */
export const MAX_LOCAL_HOOK_OUTPUT = 128 * 1024;
/** `HookRunResult.via` for an event run here, not by the service. */
export const VIA_LOCAL = "local";
/** A local run past its time gets TERM, then KILL this much later (`hookhosts.KILL_WAIT_S`'s pace). */
export const LOCAL_HOOK_KILL_AFTER_MS = 100;

export interface LocalHookDeps {
  /** Defaults to `node:child_process`'s. */
  spawn?: typeof nodeSpawn;
  /** The process the routes walk up to the agent from (PROBE_HOOK_PARENT_PID). Defaults to this one. */
  parentPid?: number;
}

/** The event's environment for a dispatcher run here, as the service's fresh
 * one gets it (`hookhosts.event_env`, not hosted): the session's, the
 * dispatcher's own variables set by us. */
export function localHookEnv(run: HookRun, parentPid: number): Record<string, string> {
  const env: Record<string, string> = Object.fromEntries(
    Object.entries(run.env).filter(([name]) => !RUN_OWNED_ENV.includes(name)),
  );
  for (const name of ["HOME", "PATH"]) {
    const ours = process.env[name];
    if (!env[name] && ours) env[name] = ours;
  }
  env.CLAUDE_PLUGIN_ROOT = run.pluginRoot;
  env.PROBE_HOOK_PARENT_PID = String(parentPid);
  // No warm host for it (the one the service keeps refuses this caller too),
  // and no hand-over to another installed copy: this folder's routes, here.
  env.PROBE_HOOK_HOST = "off";
  env.PROBE_HOOKS_HANDED_OVER = run.pluginRoot;
  return env;
}

/**
 * The event run HERE, as Claude Code runs a hook no warm host takes and as the
 * service runs it when it has no host (`hookhosts._via_process`): the folder's
 * `hooks/dispatch.sh <lane>`, the payload on its stdin, in a process group of
 * its own, killed with that group at `run.timeoutMs`. For when the service
 * refuses this caller (`HookRunRefused`): its routes still decide, inside
 * whatever sandbox pi runs in. The answer reads as `hooks.run`'s
 * (`preToolVerdict`); null when nothing could run. Never rejects.
 */
export function runHookLocally(run: HookRun, deps: LocalHookDeps = {}): Promise<HookRunResult | null> {
  return new Promise((resolve) => {
    const script = join(run.pluginRoot, "hooks", "dispatch.sh");
    const env = localHookEnv(run, deps.parentPid ?? process.pid);
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    const size = { out: 0, err: 0 };
    const cut = { out: false, err: false };
    const text = (chunks: Buffer[]): string => Buffer.concat(chunks).toString("utf8");
    const result = (exitCode: number | null, timedOut: boolean): HookRunResult => ({
      exitCode,
      stdout: text(out),
      stderr: text(err),
      timedOut,
      truncated: cut.out || cut.err,
      stdoutTruncated: cut.out,
      stderrTruncated: cut.err,
      via: VIA_LOCAL,
    });
    const finish = (value: HookRunResult | null): void => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) clearTimeout(timer);
      resolve(value);
    };
    let child: ReturnType<typeof nodeSpawn>;
    try {
      child = (deps.spawn ?? nodeSpawn)(script, [run.lane], {
        cwd: existsSync(run.cwd) ? run.cwd : (env.HOME ?? "/"),
        env,
        detached: true, // its own process group: the whole of it is stopped at the deadline
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch {
      finish(null);
      return;
    }
    const keep = (chunks: Buffer[], which: "out" | "err") => (chunk: Buffer) => {
      const room = MAX_LOCAL_HOOK_OUTPUT - size[which];
      if (room <= 0) {
        cut[which] = true;
        return;
      }
      const part = chunk.length > room ? chunk.subarray(0, room) : chunk;
      if (part.length < chunk.length) cut[which] = true;
      chunks.push(part);
      size[which] += part.length;
    };
    child.stdout?.on("data", keep(out, "out"));
    child.stderr?.on("data", keep(err, "err"));
    child.on("error", () => finish(null)); // could not run it at all
    child.on("close", (code: number | null, signal: NodeJS.Signals | null) => {
      // Killed by a signal: what its shell would report (128 + n), never a veto.
      const signalled = signal ? 128 + ((osConstants.signals as Record<string, number>)[signal] ?? 0) : null;
      finish(result(code ?? signalled, false));
    });
    timer = setTimeout(() => {
      // As the service stops one (`hookhosts._stop_group`): TERM first, which
      // the dispatcher passes to the routes it started in groups of their own,
      // then KILL what is left of its group. The verdict does not wait.
      const group = child.pid !== undefined ? -child.pid : undefined;
      try {
        if (group !== undefined) process.kill(group, "SIGTERM");
      } catch {
        // gone already
      }
      setTimeout(() => {
        try {
          if (group !== undefined) process.kill(group, "SIGKILL");
        } catch {
          // gone by now
        }
      }, LOCAL_HOOK_KILL_AFTER_MS).unref();
      finish(result(null, true));
    }, run.timeoutMs);
    child.stdin?.on("error", () => undefined); // a dispatcher that exits before reading
    child.stdin?.end(JSON.stringify(run.payload));
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
