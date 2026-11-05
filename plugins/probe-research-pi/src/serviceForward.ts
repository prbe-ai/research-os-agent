/**
 * pi's events sent to the Probe service (PR 7, D26): `hooks.run` v2 with `reply: "actions"`.
 *
 * pi has no hooks, so the extension forwards each event itself, from inside pi's own process
 * (the service checks the caller IS pi, or runs under it). The service maps the event to the
 * Claude Code lane it runs, runs that lane's routes from its own copy, and answers with
 * actions (`serviceActions.ts`). Contract: `src/probe/service/hooks/proxy/DESIGN.md` §6.
 *
 * THE SERVICE DECIDES (Richard 2026-11-03: the hooks are dumb plumbing). `Answered`: its actions
 * are the answer. `NotAccepted` (no socket, an older service, a refusal, an error) or `NoAnswer`
 * (asked, then out of time): a turn's waiting event goes on with no change, a record-keeping one
 * is queued for the next service (`queueEvent`, NotAccepted only: a NoAnswer may have run), and
 * the extension asks for a service. It never runs Probe's logic itself.
 *
 * WHEN: every turn of a session, on every machine (service-only task 16: pi runs no code of its own).
 */

import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { createConnection, type Socket } from "node:net";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";

import type { PathEnv } from "./core/paths.js";
import { privateFolder, SERVICE_API_VERSION, SERVICE_CLIENT_PI, ServiceMethod } from "./core/service.js";
import { readActions, type ReplyAction } from "./serviceActions.js";

/** The service runs extension events (`Cap.HOOKS_PROXY_V2`). */
export const CAP_HOOKS_PROXY_V2 = "hooks.proxy.v2";
/** `hooks.run` v2 (`HOOK_PROXY_V2`) and its `reply` (`HOOK_REPLY_ACTIONS`). */
export const HOOK_PROXY_V2 = 2;
export const REPLY_ACTIONS = "actions";
/** `routes: "service"`: the shape the service runs from its own copy of Probe's routes. */
export const ROUTES_SERVICE = "service";
/** `hooks.ack`: settle a delivery receipt. */
export const HOOKS_ACK = "hooks.ack";
/** The registry id the service checks this extension's events against. */
export const PI_HARNESS = "pi"; // harness-literal-ok: pi's own extension naming itself to the service
/** A protocol reply line longer than this is not read (`wire.MAX_LINE_BYTES`). */
const MAX_LINE_BYTES = 1024 * 1024;
const NOTIFICATION_METHOD = "event";

/** The events pi forwards, by its own names (`shim.ts` ShimEvent; `resources_discover` stays here). */
export const ForwardedEvent = {
  SessionStart: "session_start",
  SessionShutdown: "session_shutdown",
  BeforeAgentStart: "before_agent_start",
  ToolCall: "tool_call",
  AgentBeforeSettle: "agent_before_settle",
  AgentSettled: "agent_settled",
  SessionCompact: "session_compact",
  Input: "input",
} as const;
export type ForwardedEventName = (typeof ForwardedEvent)[keyof typeof ForwardedEvent];

/**
 * How a forward ended (D50). `NotAccepted`: no service reached, or one that cannot run it (an older
 * service: no v2, an error answer). `Refused`: a running service would not run it and said why (a
 * caller it does not serve): never "no service", so nothing is queued or started for it. `NoAnswer`:
 * sent, and no answer in time (it may have run).
 */
export const ForwardOutcome = {
  Answered: "answered", NotAccepted: "not_accepted", Refused: "refused", NoAnswer: "no_answer",
} as const;
export type ForwardOutcomeValue = (typeof ForwardOutcome)[keyof typeof ForwardOutcome];

export interface ForwardRequest {
  event: ForwardedEventName;
  /** pi's event, as `harnesses/pi/hook_routes.py` reads it: `session_id`, `transcript_path`, `cwd`, and the event's fields. */
  payload: Record<string, unknown>;
  env: Record<string, string>;
  cwd: string;
  /** What the service may spend on it. */
  timeoutMs: number;
  /** How long this caller waits for the answer (the service goes on past it). Defaults to `timeoutMs` + 500. */
  waitMs?: number;
}

export interface ForwardAnswer {
  outcome: ForwardOutcomeValue;
  actions: ReplyAction[];
  exitCode: number | null;
  timedOut: boolean;
  /** A refusal's `reason` (`sandboxed`, `not_harness`, ...), for a NotAccepted answer. */
  refusedReason: string | null;
}

export interface ForwardDeps {
  clientVersion: string;
  connect?: (path: string) => Socket;
  privateFolder?: (path: string) => boolean;
  log?: (message: string) => void;
}

const NOT_ACCEPTED: ForwardAnswer = { outcome: ForwardOutcome.NotAccepted, actions: [], exitCode: null, timedOut: false, refusedReason: null };

/** A fresh event id (32 lowercase hex): the receipt a prompt's delivery is acked by. */
export function makeEventId(): string {
  return randomBytes(16).toString("hex");
}

function line(id: number, method: string, params: Record<string, unknown>): string {
  return JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n";
}

/**
 * One request after `service.hello` on a fresh connection: `onHello` sees hello's result (false
 * ends it as not accepted), `onAnswer` the request's result. Never rejects.
 */
function ask<T>(
  socketPath: string | null,
  method: string,
  params: Record<string, unknown>,
  waitMs: number,
  deps: ForwardDeps,
  read: (result: unknown) => T,
  notAccepted: T,
  noAnswer: T,
  cap: string | null = CAP_HOOKS_PROXY_V2,
): Promise<T> {
  return new Promise((done) => {
    if (!socketPath || !(deps.privateFolder ?? privateFolder)(dirname(socketPath))) {
      done(notAccepted);
      return;
    }
    let sock: Socket | undefined;
    let settled = false;
    let asked = false;
    let buffer = "";
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (value: T): void => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) clearTimeout(timer);
      sock?.destroy();
      done(value);
    };
    const lost = (): void => finish(asked ? noAnswer : notAccepted);
    try {
      sock = (deps.connect ?? ((path: string) => createConnection({ path })))(socketPath);
    } catch {
      finish(notAccepted);
      return;
    }
    timer = setTimeout(lost, waitMs);
    sock.setEncoding("utf8");
    sock.on("connect", () => {
      sock!.write(line(1, ServiceMethod.Hello, { client: SERVICE_CLIENT_PI, client_version: deps.clientVersion || "unknown", api: SERVICE_API_VERSION }));
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
          lost();
          return;
        }
        if (typeof message !== "object" || message === null || message.method === NOTIFICATION_METHOD) continue;
        if ("error" in message) {
          finish(notAccepted); // hello refused, an older service, bad params: nothing ran
          return;
        }
        if (message.id === 1) {
          const caps = (message.result as Record<string, unknown> | undefined)?.caps;
          if (cap !== null && (!Array.isArray(caps) || !caps.includes(cap))) {
            finish(notAccepted); // an older service (no v2): treated as no service, a waiting event goes on unchanged
            return;
          }
          asked = true;
          sock!.write(line(2, method, params));
        } else if (message.id === 2) {
          finish(read(message.result));
          return;
        }
      }
      if (buffer.length > MAX_LINE_BYTES) lost();
    });
    sock.on("error", lost);
    sock.on("close", lost);
  });
}

function readAnswer(result: unknown, log?: (message: string) => void): ForwardAnswer {
  if (typeof result !== "object" || result === null) return { ...NOT_ACCEPTED, outcome: ForwardOutcome.NoAnswer };
  const fields = result as Record<string, unknown>;
  if (typeof fields.refused === "string") {
    // A running service's word that it will not run it: not "no service" (nothing queued, none started).
    return { ...NOT_ACCEPTED, outcome: ForwardOutcome.Refused, refusedReason: typeof fields.reason === "string" ? fields.reason : null };
  }
  const exitCode = typeof fields.exit_code === "number" && Number.isInteger(fields.exit_code) ? fields.exit_code : null;
  return {
    outcome: ForwardOutcome.Answered,
    actions: readActions(fields.actions, log),
    exitCode,
    timedOut: fields.timed_out === true,
    refusedReason: null,
  };
}

/** Send one event; see the module docstring for what each outcome means. Never rejects. */
export function forwardEvent(socketPath: string | null, request: ForwardRequest, deps: ForwardDeps): Promise<ForwardAnswer> {
  const params: Record<string, unknown> = {
    routes: ROUTES_SERVICE,
    v: HOOK_PROXY_V2,
    reply: REPLY_ACTIONS,
    harness: PI_HARNESS,
    event: request.event,
    payload: JSON.stringify(request.payload),
    env: request.env,
    cwd: request.cwd,
    timeout_ms: request.timeoutMs,
    event_id: makeEventId(),
  };
  return ask(socketPath, ServiceMethod.HooksRun, params, request.waitMs ?? request.timeoutMs + 500, deps,
    (result) => readAnswer(result, deps.log), NOT_ACCEPTED, { ...NOT_ACCEPTED, outcome: ForwardOutcome.NoAnswer });
}

/**
 * One plain request to the service (a method of its own, not an event): its result object, or null
 * (no service, an older one without the method, an error answer, no answer within `waitMs`). Never rejects.
 */
export function callService(socketPath: string | null, method: string, params: Record<string, unknown>, waitMs: number,
  deps: ForwardDeps): Promise<Record<string, unknown> | null> {
  const read = (result: unknown): Record<string, unknown> | null =>
    typeof result === "object" && result !== null && !Array.isArray(result) ? result as Record<string, unknown> : null;
  return ask(socketPath, method, params, waitMs, deps, read, null, null, null);
}

/** `hooks.ack`: this answer's daemon messages were shown (or could not be). True when the service settled it. */
export function ackReceipt(socketPath: string | null, receipt: string, shown: boolean, deps: ForwardDeps, waitMs = 6500): Promise<boolean> {
  return ask(socketPath, HOOKS_ACK, { receipt, shown }, waitMs, deps,
    (result) => typeof result === "object" && result !== null && (result as Record<string, unknown>).acked === true, false, false);
}

/** The service's queue folder (`paths.hook_queue_dir()`): an event no service took waits there for the next. */
export const QUEUE_FOLDER = "hooks-queue";

/**
 * Write `request` for the next service to run (its record-keeping routes only: v2 queue file,
 * `params.need_hook_proxy_queue`): `<service dir>/hooks-queue/<id>.json`, 0600, whole (a temporary
 * file renamed into place). False when it could not be written. Never throws.
 */
export function queueEvent(serviceDir: string | null, request: ForwardRequest, now: () => number = Date.now): boolean {
  if (!serviceDir) return false;
  const folder = join(serviceDir, QUEUE_FOLDER);
  const id = makeEventId();
  const item = {
    v: HOOK_PROXY_V2, id, routes: ROUTES_SERVICE, harness: PI_HARNESS, event: request.event,
    payload: JSON.stringify(request.payload), env: makeQueueEnv(request.env), cwd: request.cwd, at: now() / 1000,
  };
  const temporary = join(folder, `.${id}.${process.pid}.tmp`);
  try {
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    writeFileSync(temporary, JSON.stringify(item), { mode: 0o600, flag: "wx" });
    renameSync(temporary, join(folder, `${id}.json`));
    return true;
  } catch {
    try {
      rmSync(temporary, { force: true });
    } catch {
      // nothing to clean up
    }
    return false;
  }
}

/** What a queue file keeps of the session's environment: where its files are and who it is, never a
 *  secret (a file on disk; the service keeps less still, `env.hook_queue_env`). */
const QUEUE_ENV_NAMES: ReadonlySet<string> = new Set(["HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TERM"]);
const QUEUE_ENV_PREFIXES = ["XDG_", "LC_", "PI_", "PROBE_"];
const SECRET_NAME = /TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|KEY|AUTH|COOKIE|SESSION_ID/i;

export function makeQueueEnv(env: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(env).filter(([name]) =>
      (QUEUE_ENV_NAMES.has(name) || QUEUE_ENV_PREFIXES.some((prefix) => name.startsWith(prefix))) && !SECRET_NAME.test(name)),
  );
}

/** pi's config folder, the key its ledger record is filed under (`hook_migration.pi_root`). */
export function piConfigRoot(env: PathEnv): string {
  const chosen = env.PI_CODING_AGENT_DIR;
  if (chosen) {
    const expanded = chosen.startsWith("~/") ? join(homedir(), chosen.slice(2)) : chosen;
    return isAbsolute(expanded) ? expanded : resolve(expanded);
  }
  return join(env.HOME || homedir(), ".pi", "agent");
}
