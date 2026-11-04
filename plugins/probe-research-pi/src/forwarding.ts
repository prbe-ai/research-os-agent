/**
 * The per-turn switch between pi's own code and the Probe service (PR 7, D47/D50), for `extension.ts`.
 *
 * At a turn boundary (`session_start`, `before_agent_start`) `beginTurn` reads this machine's ledger
 * once; for the rest of the turn every handler asks `send`, which is null when this turn is not
 * forwarded (the machine has not moved pi: today's code runs) and the service's answer otherwise.
 * A forwarded turn never runs Probe's logic here (`serviceForward.ts`): a waiting event the service
 * did not answer goes on unchanged, a record-keeping one is queued, and the service is started. The decision and the session's context live in `ForwardState`,
 * the shim's shared state, so a newer implementation taking over carries them on.
 */

import { dirname } from "node:path";

import type { PathEnv } from "./core/paths.js";
import { serviceLockFile } from "./core/hosted.js";
import { boundedHookEnv } from "./core/service.js";
import { gatherEffects, type AnswerEffects } from "./serviceActions.js";
import {
  ackReceipt,
  ForwardedEvent,
  ForwardOutcome,
  forwardEvent,
  isForwarding,
  queueEvent,
  type ForwardAnswer,
  type ForwardDeps,
  type ForwardedEventName,
} from "./serviceForward.js";

/** What each forwarded event may spend in the service, and how long pi waits for it (ms). */
export const ForwardBudget: Readonly<Record<ForwardedEventName, { timeoutMs: number; waitMs: number }>> = {
  session_start: { timeoutMs: 10000, waitMs: 10500 },
  session_shutdown: { timeoutMs: 3000, waitMs: 2000 },
  before_agent_start: { timeoutMs: 3000, waitMs: 3500 },
  // A tool call waits as it did on the guard's own `hooks.run` (D28).
  tool_call: { timeoutMs: 250, waitMs: 300 },
  agent_before_settle: { timeoutMs: 3000, waitMs: 3500 },
  // Record-keeping: nobody waits on its answer (the service goes on past the wait).
  agent_settled: { timeoutMs: 15000, waitMs: 1000 },
  session_compact: { timeoutMs: 10000, waitMs: 1000 },
  input: { timeoutMs: 5000, waitMs: 5500 },
};

/** The shim's shared state for forwarding (a field of `SessionState`; containers only). */
export interface ForwardState {
  /** This turn's decision, and the session it was made for. */
  turn: { sessionId: string | null; isOn: boolean };
  /** The service's session-start context (`ServiceAction.Context`), per session: every turn's system prompt gets it. */
  context: { sessionId: string | null; text: string | null };
}

export function freshForwardState(): ForwardState {
  return { turn: { sessionId: null, isOn: false }, context: { sessionId: null, text: null } };
}

/** Where this machine's Probe service keeps its folder (the ledger's): its lock file's folder. */
export function findServiceDir(env: PathEnv): string | null {
  try {
    return dirname(serviceLockFile(env));
  } catch {
    return null;
  }
}

/** The fields every forwarded event carries (`harnesses/pi/hook_routes.py` `_make_base`). */
export function basePayload(ctx: any): Record<string, unknown> {
  const sessionId = ctx?.sessionManager?.getSessionId?.();
  const transcriptPath = ctx?.sessionManager?.getSessionFile?.();
  return {
    session_id: sessionId ?? null,
    ...(typeof transcriptPath === "string" ? { transcript_path: transcriptPath } : {}),
    cwd: ctx?.cwd ?? process.cwd(),
  };
}

/** Events the service runs only record-keeping routes of when queued: nobody waits on them. */
const QUEUED_EVENTS: ReadonlySet<ForwardedEventName> = new Set([
  ForwardedEvent.SessionStart, ForwardedEvent.SessionShutdown, ForwardedEvent.AgentSettled, ForwardedEvent.SessionCompact,
]);
/** A service is asked for at most this often while none takes the events (ms). */
export const START_EVERY_MS = 30_000;

export interface ForwarderDeps extends ForwardDeps {
  state: ForwardState;
  env: PathEnv;
  /** The service's socket, or null while unknown. */
  socket: () => string | null;
  /** Defaults to `findServiceDir(env)`. */
  serviceDir?: () => string | null;
  /** Defaults to `forwardEvent` / `ackReceipt` / `queueEvent` (tests stand in for the service). */
  forward?: typeof forwardEvent;
  ack?: typeof ackReceipt;
  queue?: typeof queueEvent;
  /** Start this machine's Probe service (the one thing pi's own code does when none answers). */
  startService?: () => void;
  now?: () => number;
}

/** One handler's view of the service: see the module docstring. */
export class Forwarder {
  private lastStart = Number.NEGATIVE_INFINITY;

  constructor(private readonly deps: ForwarderDeps) {}

  /** Does this machine's ledger send pi's events to the service (D47)? A file read. */
  isMarked(): boolean {
    return isForwarding((this.deps.serviceDir ?? (() => findServiceDir(this.deps.env)))(), this.deps.env);
  }

  /** A turn boundary: is this session's turn forwarded (D47)? Read once, kept for the turn. */
  beginTurn(sessionId: string | undefined | null, isProbeOff: boolean): boolean {
    const isOn = !isProbeOff && !!sessionId && this.deps.socket() !== null && this.isMarked();
    if (isOn !== this.deps.state.turn.isOn || (sessionId ?? null) !== this.deps.state.turn.sessionId) {
      this.deps.log?.(`probe service: ${sessionId}'s turns ${isOn ? "go to the Probe service" : "run here"}`);
    }
    this.deps.state.turn.sessionId = sessionId ?? null;
    this.deps.state.turn.isOn = isOn;
    return isOn;
  }

  /** Is this session's current turn forwarded? */
  isOn(sessionId: string | undefined | null): boolean {
    return !!sessionId && this.deps.state.turn.sessionId === sessionId && this.deps.state.turn.isOn;
  }

  /** The event's answer, or null: this turn runs pi's own code. Never rejects. */
  async send(event: ForwardedEventName, ctx: any, fields: Record<string, unknown> = {}, isQueueable = true): Promise<ForwardAnswer | null> {
    const sessionId = ctx?.sessionManager?.getSessionId?.();
    if (!this.isOn(sessionId)) return null;
    const budget = ForwardBudget[event];
    const request = {
      event,
      payload: { ...basePayload(ctx), ...fields },
      env: boundedHookEnv(this.deps.env),
      cwd: ctx?.cwd ?? process.cwd(),
      timeoutMs: budget.timeoutMs,
      waitMs: budget.waitMs,
    };
    const answer = await (this.deps.forward ?? forwardEvent)(this.deps.socket(), request, this.deps);
    if (answer.outcome === ForwardOutcome.Answered) return answer;
    const queued = isQueueable && answer.outcome === ForwardOutcome.NotAccepted && QUEUED_EVENTS.has(event)
      && (this.deps.queue ?? queueEvent)((this.deps.serviceDir ?? (() => findServiceDir(this.deps.env)))(), request);
    this.deps.log?.(`probe service: ${event} ${answer.outcome}${answer.refusedReason ? ` (${answer.refusedReason})` : ""}${queued ? ", queued for the next service" : ""}`);
    if (answer.outcome === ForwardOutcome.NotAccepted) this.askForService();
    return answer;
  }

  /** Start this machine's service, at most every START_EVERY_MS. */
  private askForService(): void {
    const now = (this.deps.now ?? Date.now)();
    if (now - this.lastStart < START_EVERY_MS) return;
    this.lastStart = now;
    try {
      this.deps.startService?.();
    } catch (err) {
      this.deps.log?.(`probe service: could not start it: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  /** The session-start context to keep (or clear) for `sessionId`. */
  keepContext(sessionId: string, effects: AnswerEffects | null): void {
    this.deps.state.context.sessionId = sessionId;
    this.deps.state.context.text = effects?.context.length ? effects.context.join("\n\n") : null;
  }

  /** Was a session start's context kept for this session (an empty one included)? */
  hasContext(sessionId: string | undefined | null): boolean {
    return !!sessionId && this.deps.state.context.sessionId === sessionId;
  }

  /** The kept context for this session, or null. */
  getContext(sessionId: string | undefined | null): string | null {
    return sessionId && this.deps.state.context.sessionId === sessionId ? this.deps.state.context.text : null;
  }

  /** Settle an answer's receipts: its daemon messages were applied (shown), or not. Not awaited by callers. */
  async settle(effects: AnswerEffects, isShown: boolean): Promise<void> {
    for (const receipt of effects.receipts) {
      await (this.deps.ack ?? ackReceipt)(this.deps.socket(), receipt, isShown, this.deps);
    }
  }
}

/** The answer's notices, shown to the person where there is a UI (best effort). */
export function showNotices(ctx: any, effects: AnswerEffects | null): void {
  if (!effects || !ctx?.hasUI) return;
  for (const notice of effects.notices) {
    try {
      ctx.ui.notify(notice.text, notice.level);
    } catch {
      // a notice is best effort
    }
  }
}

/** An answer's effects, or null when the service did not answer (`NotAccepted` or `NoAnswer`). */
export function answeredEffects(answer: ForwardAnswer | null): AnswerEffects | null {
  return answer !== null && answer.outcome === ForwardOutcome.Answered ? gatherEffects(answer.actions) : null;
}

/** Did the service run the event's routes to their end (an answer with an exit code, in time)? */
export function ranToEnd(answer: ForwardAnswer | null): boolean {
  return answer !== null && answer.outcome === ForwardOutcome.Answered && answer.exitCode !== null && !answer.timedOut;
}

/** Did the service take the event (answered, or asked and lost: it may have run it)? */
export function wasAccepted(answer: ForwardAnswer | null): boolean {
  return answer !== null && answer.outcome !== ForwardOutcome.NotAccepted;
}
