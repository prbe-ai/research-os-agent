/**
 * The implementation: what this package does on each pi event and command,
 * as DECLARATIONS for the shim (`src/shim.ts`) to register. Everything with
 * actual logic (pairing, runtime resolution, spawning, stopping) lives in
 * sibling modules and is unit-tested directly with injected fakes; this file
 * binds them to real fs/child_process and to the events pi emits.
 *
 * `prepareExtension(env)` builds it with no side effect: no pi call, no timer,
 * no child process. `activate()` / `deactivate()` start and stop the one thing
 * it runs on its own (the reads poller). It never registers anything with pi:
 * the shim does, from `declarations`, and forwards each event here while this
 * version is the active one. `src/impl.ts` is the entry the shim loads.
 *
 * TWO ROOTS (`ImplEnv`). `installRoot` is where pi loaded the package from (the
 * pi clone): the package's IDENTITY, which pi's settings name -- the tracking
 * opt-out and pi-mcp-adapter's hand-off compare against it. `resourceRoot` is
 * where THIS version's files are (a pinned copy, or the clone itself): its
 * skills, and the sibling plugin folders beside it (the tap, the hook routes).
 */

import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import * as fs from "node:fs";
import { constants as fsConstants } from "node:fs";
import { dirname, join } from "node:path";

import type { AgentActivityOutcome, SessionShutdownEvent } from "@earendil-works/pi-coding-agent";

import { detectAdapterHandoff, MCP_SERVED_VIA_ADAPTER_MESSAGE } from "./adapterHandoff.js";
import { newAskerState, QuestionAsker, type ApprovalsDeps, type AskerState } from "./core/approvals.js";
import { PiTool, resolveLikePi, sessionInlineReadOnly, sessionIsInline } from "./guard.js";
import { forgetInlineShown } from "./inlineNotice.js";
import {
  CLAIM_RECHECK_MS,
  ClaimRecheck,
  finishHostedSession,
  getStartingEndMs,
  HOSTED_FINISH_WAIT_S,
  loadWorkerStamp,
  pruneStaleShutdownSentinels,
  recheckUntilClaimed,
  stopDaemon,
  type DaemonDeps,
  type SpawnFn,
} from "./core/daemon.js";
import {
  connectMcp,
  defaultMcpBridgeDeps,
  interactiveOAuthConnect,
  mcpToolDeclarations,
  type McpTransport,
} from "./mcpBridge.js";
import { approvalsDir, extensionLogFile, MCP_TOKEN_ENV, probeConfigPath, probeStateDir, pythonMissingFile } from "./core/paths.js";
import { hostedServicePid } from "./core/hosted.js";
import { Mailbox } from "./core/reads.js";
import { ledgerFor, notifyLevel } from "./core/serverNotices.js";
import { checkPairing } from "./core/pairing.js";
import { buildStatusReport } from "./status.js";
import { findCliPython, resolveTapRuntime, type TapRuntime, type TapRuntimeDeps } from "./core/tapRuntime.js";
import { boundedHookEnv, ensureService, lookupServiceSocket } from "./core/service.js";
import { packageVersion } from "./packageVersion.js";
import { answeredEffects, Forwarder, freshForwardState, ranToEnd, showNotices, wasAccepted, type ForwardState } from "./forwarding.js";
import { callService, ForwardedEvent, ForwardOutcome, piConfigRoot } from "./serviceForward.js";
import { parseSwitchIntent, switchAppliedNotice } from "./core/trackingSwitch.js";
import { writeTurnSignal, type TurnSignalDeps } from "./core/turnSignal.js";
import {
  companionKeyHeld,
  DaemonReason,
  daemonStatus,
  DaemonStatus,
  type DaemonNoticeDeps,
  type DaemonStatusValue,
} from "./core/daemonNotice.js";
import { daemonSkillPaths, isProbeMcpTool, leanSkills, readDaemonSkills, researcherDaemonNotice, withoutPackageSkills } from "./profile.js";
import { trackingOff } from "./trackingSetting.js";
import {
  Configured,
  initializeTrackingState,
  ProbeState,
  Recorder,
  renderFooter,
  SIGNED_OUT_FOOTER,
  STATUS_UNAVAILABLE_FOOTER,
  type CaptureReading,
  type ConfiguredValue,
  type ProbeStateValue,
  type RecorderValue,
  type TrackingExecFileFn,
} from "./core/trackingState.js";
import {
  EMPTY_STATE_SCHEMA,
  ShimEvent,
  UnsupportedStateSchemaError,
  type Candidate,
  type CommandDeclaration,
  type Declarations,
  type EventHandler,
  type ImplEnv,
  type SharedState,
  type ShimApi,
  type ToolDeclaration,
} from "./shim.js";

/** Why pi tears an extension runtime down (`SessionShutdownEvent.reason`), the ones this file tells apart. */
const ShutdownReason = { Quit: "quit", Reload: "reload" } as const satisfies Record<string, SessionShutdownEvent["reason"]>;

/** How a run ended (`AgentBeforeSettleEvent.outcome`), the one this file acts on. */
const ActivityOutcome = { Completed: "completed" } as const satisfies Record<string, AgentActivityOutcome>;

// ---------------------------------------------------------------------------
// The state that outlives this version
// ---------------------------------------------------------------------------

/**
 * The layout of `shared.session` and `shared.transports` this version reads
 * and writes. Bump it, and list the old number in `MIGRATES_FROM` with its
 * migration in `adoptShared`, whenever a field changes shape: a running pi
 * hands its state to the next version at a turn boundary (`src/shim.ts`).
 */
export const STATE_SCHEMA = 1;

/** The older schemas `adoptShared` migrates from. None yet: 1 is the first. */
export const MIGRATES_FROM: readonly number[] = [];

/**
 * Session state: everything a later handler relies on that an earlier one
 * set, for this pi process until a `/reload` (which evaluates the extension
 * afresh). It used to be this module's top-level variables; it lives in the
 * shim's shared state now, so a newer version taking over at the next turn
 * carries on with it instead of starting cold.
 *
 * Every field is a CONTAINER, never a bare value (see `src/shim.ts`): a copy
 * of `session` shares them, so a replaced version's in-flight work (a socket
 * lookup, a capture start awaiting its confirmation) lands where the new one
 * reads.
 */
export interface SessionState {
  /**
   * The shared state's schema 1 shape, kept: another version of this extension takes it over.
   *
   * `pending`: sessions whose file pi had not written at session_start (pi 1.0 writes a NEW
   * session's file at its first message), session id -> that file. The service asks for their
   * recorder at the next events (M9); this only says the footer is read back once the file is there.
   * `spawned` (its schema 1 name): sessions whose footer is read again until a recorder claims
   * them; their end stops it.
   */
  capture: { spawned: Set<string>; pending: Map<string, string> };
  probe: {
    /**
     * The three-valued switch for this session, as the CLI last reported it.
     *
     * `undefined` means unknown -- an older probe that does not send `state`, or
     * a read that failed -- and unknown behaves exactly as it did before this
     * existed. Only a confirmed `off` suppresses anything, because suppressing
     * on a failed read would silently stop the team note syncing on a machine
     * that never asked for it, and a note that quietly stops sending is the one
     * failure that surface must never have.
     */
    state: ProbeStateValue | undefined;
    /** The last tracking reading, so a turn in `daemon` can redraw the footer
     * when the daemon's lease changes without spawning the CLI again. `readAtMs`: when it was read;
     * `keptUntilMs`: a refresh failed and it is kept until then (`refreshTrackingStatus`). */
    tracking:
      | {
          tracking: boolean;
          capture?: CaptureReading;
          lowered?: string;
          signedOut?: boolean;
          configured?: ConfiguredValue;
          readAtMs?: number;
          keptUntilMs?: number;
        }
      | undefined;
    /**
     * Who records, as the CLI last reported it, and for WHICH session.
     *
     * Read by session id because this state outlives a session switch: a
     * resumed or new session must never inherit the previous one's profile. A
     * session the CLI has not answered for is the agent's (`profileFor`), which
     * is today's extension; a failed re-read later in a session keeps the answer
     * it already had, so one slow CLI call cannot flip a session between profiles.
     */
    profile: { sessionId: string; profile: RecorderValue } | undefined;
    /**
     * Probe's tracking switched off on our settings entry (`trackingSetting.ts`),
     * read at each session_start straight from pi's settings files. Keyed by
     * session like `profile`, so a new or resumed session never inherits it.
     */
    trackingOff: { sessionId: string; off: boolean } | undefined;
    /**
     * Probe auto mode: the main agent sets this session's switch (`session initialize`'s `auto`).
     * Keyed by session like `profile`; absent is not auto.
     */
    auto: { sessionId: string; auto: boolean } | undefined;
    /**
     * The daemon's health as the researcher last heard it, in the daemon profile:
     * `status` null until the first notice-worthy reading, `prompts` for the
     * not-started grace. See `profile.ts::researcherDaemonNotice`.
     */
    daemonHealth: { sessionId: string; status: DaemonStatusValue | null; prompts: number } | undefined;
    /**
     * Was the last turn in the `daemon` state? The daemon notice reads two small
     * files, so a session that has never been in the state skips it entirely; a
     * session that just LEFT it runs it once more to clear the notified record.
     */
    lastTurnInDaemon: boolean;
  };
  /**
   * This machine's Probe service socket, where every event goes (`forwarding.ts`): asked of the
   * tap (`lookupServiceSocket`, a Python start, so never on a tool call's path) once it is found,
   * and again at most every SOCKET_RETRY_MS while it is not. A failed lookup never forgets one found.
   */
  service: { socket: string | null; asked: boolean; askedAt: number };
  /**
   * DAEMON READS (reads.ts): the daemon's reader answers `probe ask` and sends
   * team context, and the worker's run-alert poller sends run alerts; pi gets
   * them at each prompt and, between prompts, from a 2 s poller that steers an
   * answer or a run alert into a running agent or starts a turn for one that
   * lands while pi is idle (pi's analogue of Claude Code's Stop wake). The
   * same poller asks the daemon's held questions (approvals.ts). `timer` is
   * null while no version runs it (between a deactivate and the next activate).
   */
  reads: { poller: { ctx: any; timer: ReturnType<typeof setInterval> | null } | null };
  /** The daemon's held questions: one dialog at a time, each asked once per process. */
  questions: AskerState;
  /** The session the last session_start was for: what `status()` reports on. */
  current: { sessionId: string | null };
  /** Whose Probe MCP connection's tools are declared now (a session id), or none. */
  mcp: { declared: string | null };
  /** This turn and its session's context, as the Probe service gave them (`forwarding.ts`, PR 7). */
  forward: ForwardState;
}

/**
 * Live Probe MCP connections, keyed by session id: transports only, never a
 * closure (`McpTransport`). A session_start finding its session here
 * re-declares the tools it already fetched, with no network call; a session
 * ending removes its own entry.
 */
export interface TransportState {
  mcp: Map<string, McpTransport>;
}

function freshSession(): SessionState {
  return {
    capture: { spawned: new Set(), pending: new Map() },
    probe: {
      state: undefined,
      tracking: undefined,
      profile: undefined,
      trackingOff: undefined,
      auto: undefined,
      daemonHealth: undefined,
      lastTurnInDaemon: false,
    },
    service: { socket: null, asked: false, askedAt: 0 },
    reads: { poller: null },
    questions: newAskerState(),
    current: { sessionId: null },
    mcp: { declared: null },
    forward: freshForwardState(),
  };
}

function freshTransports(): TransportState {
  return { mcp: new Map() };
}

/**
 * Make `shared` (a copy the shim handed over) this version's: initialize an
 * empty one, take one of `STATE_SCHEMA` as it is (adding any field it lacks),
 * migrate one listed in `MIGRATES_FROM`, refuse anything else. Replaces fields
 * of the copy; never mutates a container it holds.
 */
export function adoptShared(shared: SharedState): { session: SessionState; transports: TransportState } {
  if (shared.schema === EMPTY_STATE_SCHEMA) {
    shared.session = freshSession() as unknown as Record<string, unknown>;
    shared.transports = freshTransports() as unknown as Record<string, unknown>;
    shared.schema = STATE_SCHEMA;
  } else if (shared.schema !== STATE_SCHEMA) {
    // No migration exists yet: MIGRATES_FROM is empty.
    throw new UnsupportedStateSchemaError(shared.schema, [STATE_SCHEMA, ...MIGRATES_FROM]);
  }
  const fresh = freshSession() as unknown as Record<string, unknown>;
  for (const [key, value] of Object.entries(fresh)) if (!(key in shared.session)) shared.session[key] = value;
  const freshT = freshTransports() as unknown as Record<string, unknown>;
  for (const [key, value] of Object.entries(freshT)) if (!(key in shared.transports)) shared.transports[key] = value;
  return { session: shared.session as unknown as SessionState, transports: shared.transports as unknown as TransportState };
}

// ---------------------------------------------------------------------------
// Readings of the session state
// ---------------------------------------------------------------------------

function profileFor(s: SessionState, sessionId: string | undefined): RecorderValue {
  return sessionId !== undefined && s.probe.profile?.sessionId === sessionId ? s.probe.profile.profile : Recorder.Agent;
}

function inDaemonProfile(s: SessionState, sessionId: string | undefined): boolean {
  return profileFor(s, sessionId) === Recorder.Daemon;
}

function trackingOffFor(s: SessionState, sessionId: string | undefined): boolean {
  return sessionId !== undefined && s.probe.trackingOff?.sessionId === sessionId && s.probe.trackingOff.off;
}

/** The footer pi shows (undefined: none, Probe off or not set up) and when to draw it again with no event (null: never). */
interface Footer {
  text: string | undefined;
  redrawAtMs: number | null;
}

/** What draws the footer: an event's context, or the one a redraw kept. */
type FooterContext = { hasUI: boolean; ui: { setStatus: (key: string, value: string | undefined) => void } };

/** A refresh that fails keeps the last reading this long after it was read, then the footer says unavailable: the
 * claim rechecks' whole window. */
const KEEP_FAILED_READ_MS = CLAIM_RECHECK_MS.reduce((total, ms) => total + ms, 0);

/** A Probe token in pi's own environment (the CLI child the service runs never sees it). */
function hasProbeTokenInEnv(): boolean {
  return Boolean(process.env.PROBE_TOKEN || process.env[MCP_TOKEN_ENV]);
}

/** The earliest of the given times, or null. */
function getEarliest(...times: (number | null)[]): number | null {
  const known = times.filter((time): time is number => time !== null);
  return known.length ? Math.min(...known) : null;
}

/**
 * The footer for this session: the status line's words (`trackingState.ts`). In `daemon` it reads the lease and the
 * worker stamp: a worker with no lease yet is STARTING (no note) while the companion key is held, for
 * DAEMON_START_GRACE_MS after the service asked for it (no stamp: no end). pi's own failures (no reading, a kept one
 * past its time, an unreadable config) read `status unavailable`, never hidden: hidden is Probe off, or not set up.
 */
function getFooter(s: SessionState, sessionId: string, lease?: { status: DaemonStatusValue; reason: string | null }): Footer {
  const cached = s.probe.tracking;
  const now = Date.now();
  const unavailable: Footer = { text: STATUS_UNAVAILABLE_FOOTER, redrawAtMs: null };
  const hidden: Footer = { text: undefined, redrawAtMs: null };
  if (!cached) return hasProbeTokenInEnv() || fs.existsSync(probeConfigPath(process.env)) ? unavailable : hidden;
  if (cached.keptUntilMs !== undefined && now >= cached.keptUntilMs) return unavailable;
  if (cached.configured === Configured.Unreadable) return unavailable;
  const kept = cached.keptUntilMs ?? null;
  // The agent signed the person out: the status alone, before off or any note (`render_signed_out`). A token in
  // pi's own environment still signs it in, as the caller's does for the status line.
  if (cached.signedOut && !hasProbeTokenInEnv()) return { text: SIGNED_OUT_FOOTER, redrawAtMs: kept };
  if (cached.configured === Configured.Missing && !hasProbeTokenInEnv()) return hidden;
  // `on (inline)`: the researcher set Probe inline (`/probe inline`).
  const inline = inDaemonProfile(s, sessionId) && sessionIsInline(sessionId);
  if (inline && sessionInlineReadOnly(sessionId)) {
    return {
      text: renderFooter({ tracking: false, lowered: cached.lowered, isAuto: isAutoChat(s, sessionId) }),
      redrawAtMs: kept,
    };
  }
  // Probe off: no Probe calls, and the Probe service holds the session without capturing it. No footer, as the
  // status line draws none.
  if (probeIsOff(s)) return { text: undefined, redrawAtMs: kept };
  let daemonLive: boolean | undefined;
  let startingEndMs: number | null = null;
  if (s.probe.state === ProbeState.Daemon) {
    const deps = realDaemonNoticeDeps();
    const { status, reason } = lease ?? daemonStatus(sessionId, deps);
    if (status === DaemonStatus.Live) {
      daemonLive = true;
    } else if (reason !== DaemonReason.NotStarted) {
      daemonLive = false;
    } else {
      // No lease yet: STARTING while the key is held and the stamp says so (`is_worker_starting`). No stamp (an
      // older service, or none written yet) is starting with no end: no redraw time.
      const stamp = loadWorkerStamp(sessionId, deps);
      const endMs = stamp ? getStartingEndMs(stamp, now) : null;
      daemonLive = (stamp === null || endMs !== null) && companionKeyHeld(deps);
      startingEndMs = daemonLive ? endMs : null;
    }
  }
  const text = renderFooter({
    tracking: cached.tracking,
    capture: cached.capture,
    daemonLive,
    inline,
    daemonReads: inDaemonProfile(s, sessionId) && !inline,
    lowered: cached.lowered,
    isAuto: isAutoChat(s, sessionId),
  });
  // The window's last moment still counts as starting (`is_worker_starting`): redraw just past it.
  return { text, redrawAtMs: getEarliest(kept, startingEndMs === null ? null : startingEndMs + 1) };
}

/**
 * pi redraws its footer only on events: this redraws it when what it shows runs out by the clock alone (a starting
 * daemon's window, a kept reading). One pending redraw per version, re-armed by every draw; cancelled on a session
 * change and at deactivate.
 */
class FooterClock {
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly redraw: (ctx: FooterContext, sessionId: string) => void) {}

  schedule(ctx: FooterContext, sessionId: string, atMs: number | null): void {
    this.cancel();
    if (atMs === null) return;
    const timer = setTimeout(() => {
      this.timer = null;
      this.redraw(ctx, sessionId);
    }, Math.max(0, atMs - Date.now()));
    timer.unref?.();
    this.timer = timer;
  }

  cancel(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}

/** Draw this session's footer and arm its redraw. Throws what `setStatus` throws: each caller logs it its own way. */
function renderTrackingFooter(
  s: SessionState,
  clock: FooterClock,
  ctx: FooterContext,
  sessionId: string,
  lease?: { status: DaemonStatusValue; reason: string | null },
): void {
  const footer = getFooter(s, sessionId, lease);
  ctx.ui.setStatus("probe-tracking", footer.text);
  clock.schedule(ctx, sessionId, footer.redrawAtMs);
}

/** No reading to draw the footer from: none (a failed read), or one kept past its time. */
function isTrackingStale(s: SessionState): boolean {
  const cached = s.probe.tracking;
  return cached === undefined || (cached.keptUntilMs !== undefined && Date.now() >= cached.keptUntilMs);
}

/** Has the researcher switched Probe off for this session? */
function probeIsOff(s: SessionState): boolean {
  return s.probe.state === ProbeState.Off;
}

/** Does the main agent set this session's switch (probe auto mode), as the CLI last said? */
function isAutoChat(s: SessionState, sessionId: string | undefined): boolean {
  return sessionId !== undefined && s.probe.auto?.sessionId === sessionId && s.probe.auto.auto;
}

/** A tool call's input fields the service's guard never reads (the body of a write or an edit): never sent
 * (`hooks/_hook_forward.py` GATE_BULK_FIELDS). */
const GATE_BULK_FIELDS: ReadonlySet<string> = new Set([
  "content", "oldText", "newText", "edits", "old_string", "new_string", "new_source", "old_str", "new_str", "file_text",
]);
/** A tool call's fields after that, at most (`_hook_forward.MAX_GATE_PAYLOAD`): past it, refused. */
const MAX_GATE_PAYLOAD = 256 * 1024;
/** `mailbox.delivered`'s wait (the service's own `DELIVERED_WAIT_S`, and the round trip). */
const DELIVERED_WAIT_MS = 3500;
/** An inbox message's id (`reads.INBOX_ID_PREFIX`, then 32 hex): the only ones Probe hears were shown. */
const INBOX_ID = /^s[0-9a-f]{32}$/;
/** What a refused tool call says to the agent (`_hook_forward.DENIED`). */
const GuardDenied = {
  too_big: "Probe refused this tool call: it is too large for Probe's guard to check ({detail}). Split it into smaller steps.",
  not_harness: "Probe refused this tool call: the Probe service found no running agent this event belongs to.",
} as const;
/** The service's refusal of a forged event (`proxy.RefusedReason.NOT_HARNESS`): the one that refuses a tool call. */
const REFUSED_NOT_HARNESS = "not_harness";

function guardDenied(key: keyof typeof GuardDenied, detail = ""): string {
  return GuardDenied[key].replace("{detail}", detail);
}

/** A socket lookup that found none is asked again after this long (M8: a service started meanwhile). */
const SOCKET_RETRY_MS = 30_000;

function askServiceSocket(s: SessionState, runtime: TapRuntime): void {
  const service = s.service;
  if (service.socket || (service.asked && Date.now() - service.askedAt < SOCKET_RETRY_MS)) return;
  service.asked = true;
  service.askedAt = Date.now();
  void lookupServiceSocket(runtime, { execFile: realTrackingExecFile, env: process.env, log: logLine }).then((socket) => {
    if (!socket) return; // a failed lookup never forgets a socket found (and is asked again later)
    service.socket = socket;
    logLine(`probe service: pi's events go to the service at ${socket}`);
  });
}

/** At most this long, a forwarded session start waits for the socket lookup it needs. */
const SOCKET_WAIT_MS = 1500;

/** The service's socket, looked up now if unknown (bounded): a forwarded session start needs it. */
async function waitForSocket(s: SessionState, runtime: TapRuntime): Promise<void> {
  const found = await Promise.race([
    lookupServiceSocket(runtime, { execFile: realTrackingExecFile, env: process.env, log: logLine }),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), SOCKET_WAIT_MS).unref?.()),
  ]);
  if (found) s.service.socket = found;
}

/** This package's version, for the service's `service.hello`. */
const PACKAGE_VERSION = packageVersion();

const READS_POLL_MS = 2000;

// ---------------------------------------------------------------------------
// Real dependencies
// ---------------------------------------------------------------------------

function realApprovalsDeps(): ApprovalsDeps {
  return {
    env: process.env,
    readdirSync: (dir) => fs.readdirSync(dir),
    readFileSync: (path) => fs.readFileSync(path, "utf-8"),
    existsSync: fs.existsSync,
    // 0600, as the Board's own temporary files are (`tempfile.mkstemp`).
    writeFileSync: (path, content) => fs.writeFileSync(path, content, { mode: 0o600 }),
    renameSync: (from, to) => fs.renameSync(from, to),
    rmSync: (path) => fs.rmSync(path, { force: true }),
    mkdirSync: (path) => fs.mkdirSync(path, { recursive: true }),
    now: () => Date.now(),
    token: () => randomBytes(6).toString("hex"),
    // The Probe service sends it (its session-start maintenance flushes unsent feedback): nothing runs here.
    sendFeedback: () => {},
  };
}

/** Has the daemon ever held a question on this machine? (`Board()` creates the folder.) */
function heldQuestionsBoardExists(): boolean {
  return fs.existsSync(join(approvalsDir(process.env), "requests"));
}

function mailboxFor(sessionId: string): Mailbox {
  const state = probeStateDir(process.env);
  return new Mailbox(`${state}/reads`, `${state}/sessions`, sessionId);
}

/** The person's notices for the server messages this session's mailbox delivered (R17, R28): this
 * extension is pi's one emitter. A notice counts as shown on this computer only once `notify` returned. */
function notifyServerBanners(ctx: any, box: Mailbox): void {
  if (!ctx?.hasUI) return; // print mode: nobody to show it to, so nothing is taken
  for (const banner of box.takeBanners(() => ledgerFor(process.env))) {
    try {
      ctx.ui.notify(banner.text, notifyLevel(banner.severity));
    } catch {
      banner.claim?.release();
      fs.rmSync(banner.marker, { force: true });
      continue;
    }
    banner.claim?.commit();
  }
}

function readsNotify(ctx: any, line: string | null): void {
  if (line && ctx?.hasUI) {
    try {
      ctx.ui.notify(line, "warning");
    } catch {
      // a notice is best effort
    }
  }
}

function realTurnSignalDeps(): TurnSignalDeps {
  return {
    env: process.env,
    now: () => Date.now(),
    pid: process.pid,
    readFileSync: (path) => fs.readFileSync(path, "utf-8"),
    sizeOf: (path) => fs.statSync(path).size,
    writeFileSync: (path, content) => fs.writeFileSync(path, content),
    renameSync: (from, to) => fs.renameSync(from, to),
  };
}

function realDaemonNoticeDeps(): DaemonNoticeDeps {
  return {
    env: process.env,
    readFileSync: (path) => fs.readFileSync(path, "utf-8"),
    writeFileSync: (path, content) => fs.writeFileSync(path, content),
    mkdirSync: (path) => fs.mkdirSync(path, { recursive: true }),
    rmSync: (path) => fs.rmSync(path, { force: true }),
    now: () => Date.now(),
  };
}

function isExecutable(path: string): boolean {
  try {
    fs.accessSync(path, fsConstants.X_OK);
    return true;
  } catch {
    return false;
  }
}

const realSpawn: SpawnFn = (command, args, options) =>
  spawn(command, args, options) as unknown as {
    pid: number | undefined;
    unref: () => void;
    once: (event: string, listener: (payload?: unknown) => void) => unknown;
  };

const realTrackingExecFile: TrackingExecFileFn = (command, args, options, callback) => {
  return execFile(command, args, options, (error, stdout, stderr) => {
    callback(error, String(stdout), String(stderr));
  });
};

function logLine(message: string): void {
  try {
    fs.mkdirSync(dirname(extensionLogFile()), { recursive: true });
    fs.appendFileSync(extensionLogFile(), `[${new Date().toISOString()}] ${message}\n`);
  } catch {
    // Best-effort; losing an extension log line must never break capture.
  }
}

function realDaemonDeps(): DaemonDeps {
  return {
    existsSync: fs.existsSync,
    mkdirSync: (path) => fs.mkdirSync(path, { recursive: true }),
    readFileSync: (path) => fs.readFileSync(path, "utf-8"),
    rmSync: (path) => fs.rmSync(path, { force: true }),
    writeFileSync: (path, content) => fs.writeFileSync(path, content),
    kill: (pid, signal) => process.kill(pid, signal),
    log: logLine,
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  };
}

/**
 * Read the session's state back through the Probe service (`session.initialize`, which runs
 * `probe session initialize`: the extension spawns no CLI) and draw the footer from it. A reading that comes back after pi switched to
 * another session is the old one's: never committed over the new one's (its
 * switch, profile and footer). `keepOnFailure`: a
 * read that fails leaves the last reading, not an unknown one (a re-read after
 * the start, `recheckUntilClaimed`), for KEEP_FAILED_READ_MS after it was read. True when a reading was committed.
 */
async function refreshTrackingStatus(
  s: SessionState,
  clock: FooterClock,
  ctx: {
    hasUI: boolean;
    ui: {
      setStatus: (key: string, value: string | undefined) => void;
      notify?: (message: string, level?: "info" | "warning" | "error") => void;
    };
  },
  sessionId: string,
  cwd: string,
  opts: { keepOnFailure?: boolean } = {},
): Promise<boolean> {
  const state = await initializeTrackingState(sessionId, cwd, {
    call: (method, params, waitMs) =>
      callService(s.service.socket, method, params, waitMs, { clientVersion: PACKAGE_VERSION, log: logLine }),
    env: process.env,
    log: logLine,
  });
  if (s.current.sessionId !== null && s.current.sessionId !== sessionId) return false;
  if (!state) {
    const last = s.probe.tracking;
    if (opts.keepOnFailure && last) {
      last.readAtMs ??= Date.now(); // a reading an older version committed: kept from now
      last.keptUntilMs = last.readAtMs + KEEP_FAILED_READ_MS;
    } else {
      // Unknown, not the last value seen: see SessionState.probe.state's docstring.
      s.probe.state = undefined;
      s.probe.tracking = undefined;
    }
    if (ctx.hasUI) {
      try {
        // Never leave a previous authoritative-looking value visible when the current state could not be read
        // back, and never hide the footer either (that reads as off): `status unavailable`.
        renderTrackingFooter(s, clock, ctx, sessionId);
      } catch (err) {
        logLine(
          `tracking footer redraw failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    }
    return false;
  }
  s.probe.state = state.state;
  s.probe.tracking = {
    tracking: state.tracking,
    capture: state.capture,
    lowered: state.lowered,
    signedOut: state.signedOut,
    configured: state.configured,
    readAtMs: Date.now(),
  };
  if (state.retiredConfigNotice) {
    // The researcher's, once (the CLI marked it given): never the agent's prompt.
    logLine(state.retiredConfigNotice);
    if (ctx.hasUI) {
      try {
        ctx.ui.notify?.(state.retiredConfigNotice, "warning");
      } catch {
        // a notice is best effort
      }
    }
  }
  s.probe.profile = { sessionId, profile: state.profile };
  s.probe.auto = { sessionId, auto: state.auto };
  if (ctx.hasUI) {
    try {
      renderTrackingFooter(s, clock, ctx, sessionId);
    } catch (err) {
      logLine(
        `tracking footer refresh failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      return false;
    }
  }
  return true;
}

/** One clear message, on stderr (never stdout — `--mode json` reserves stdout for structured
 * output) plus the extension log, plus a UI toast when a UI exists to show one. */
function announce(ctx: { hasUI: boolean; ui: { notify: (msg: string, level?: "info" | "warning" | "error") => void } }, message: string, level: "info" | "warning" | "error" = "warning"): void {
  process.stderr.write(`probe-research-pi: ${message}\n`);
  logLine(message);
  if (ctx.hasUI) ctx.ui.notify(message, level);
}

/**
 * The daemon profile's tools: no Probe MCP tool stays active, whoever
 * registered it. Run at session start AND at every prompt, because
 * pi-mcp-adapter registers its tools on its own schedule, which may be after
 * this extension's `session_start`. Deactivated, not unregistered: pi has no
 * unregister, and the service's guard refuses a call that reaches one
 * anyway (a codemode script can call an inactive tool).
 */
function deactivateProbeTools(api: ShimApi): void {
  try {
    const active = api.getActiveTools();
    const kept = active.filter((name) => !isProbeMcpTool(name));
    if (kept.length !== active.length) {
      api.setActiveTools(kept);
      logLine(`daemon profile: deactivated ${active.length - kept.length} Probe MCP tool(s)`);
    }
  } catch (err) {
    logLine(`daemon profile: tool deactivation failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

/**
 * The daemon profile's footer and its one notice to the researcher, at a
 * prompt in the `daemon` state. The model hears nothing: see `profile.ts`.
 */
function tellResearcherAboutDaemon(
  s: SessionState,
  clock: FooterClock,
  ctx: { hasUI: boolean; ui: { notify: (msg: string, level?: "info" | "warning" | "error") => void; setStatus: (key: string, value: string | undefined) => void } },
  sessionId: string,
): void {
  const deps = realDaemonNoticeDeps();
  const lease = daemonStatus(sessionId, deps);
  const { status, reason } = lease;
  const previous = s.probe.daemonHealth;
  const health = previous?.sessionId === sessionId ? previous : { sessionId, status: null, prompts: 0 };
  const { notice, next } = researcherDaemonNotice(health.status, status, reason, health.prompts === 0, () =>
    companionKeyHeld(deps),
  );
  s.probe.daemonHealth = { sessionId, status: next, prompts: health.prompts + 1 };
  if (notice) logLine(notice);
  if (!ctx.hasUI) return;
  try {
    renderTrackingFooter(s, clock, ctx, sessionId, lease);
    if (notice) ctx.ui.notify(notice, "warning");
  } catch (err) {
    logLine(`daemon status notice failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

/**
 * Every coding agent's session variable, from the harness registry beside this
 * file (`harnesses.json`, synced from `probe.foundation.harness_registry`). Empty when the copy
 * cannot be read.
 */
export function agentSessionEnv(): readonly string[] {
  try {
    const doc = JSON.parse(fs.readFileSync(new URL("./harnesses.json", import.meta.url), "utf8")) as {
      harnesses?: { session_env?: unknown }[];
    };
    return (doc.harnesses ?? [])
      .map((row) => row.session_env)
      .filter((name): name is string => typeof name === "string" && name.length > 0);
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// The implementation
// ---------------------------------------------------------------------------

/** This version, ready to run: see the module docstring. No side effect. */
export function prepareExtension(env: ImplEnv): Candidate {
  const { api, installRoot, resourceRoot } = env;
  const shared = env.shared;
  const { session: s, transports } = adoptShared(shared);
  // Read once: the files ship with the package and only change with it.
  const daemonSkills = readDaemonSkills(resourceRoot);
  const asker = new QuestionAsker(s.questions);
  const forwarder = new Forwarder({
    state: s.forward, env: process.env, socket: () => s.service.socket, clientVersion: PACKAGE_VERSION, log: logLine,
    // The one thing pi does itself when no service answers: ask the CLI to start one (`ensure`).
    startService: () => {
      if (checkPairing(process.env).paired) ensureService(findCliPython(tapRuntimeDeps()), { spawn: realSpawn, env: process.env, log: logLine });
    },
  });
  let active = false;
  // The footer's own redraws (a starting daemon's window, a kept reading): this version's, never another's.
  const footerClock = new FooterClock((ctx, sessionId) => {
    if (!active || s.current.sessionId !== sessionId) return;
    try {
      if (ctx.hasUI) renderTrackingFooter(s, footerClock, ctx, sessionId);
    } catch (err) {
      logLine(`tracking footer redraw failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  });

  const tapRuntimeDeps = (): TapRuntimeDeps => ({ existsSync: fs.existsSync, isExecutable, env: process.env, extensionDir: resourceRoot });

  // -- Probe MCP tools, as declarations over the shared transports --------------

  const mcpToolDeps = defaultMcpBridgeDeps(process.env, () => {}, logLine);
  let toolCache: { transport: McpTransport; tools: McpTransport["tools"]; declared: ToolDeclaration[] } | null = null;

  /** The tools of the declared session's connection, built once per connection. */
  function mcpTools(): readonly ToolDeclaration[] {
    const id = s.mcp.declared;
    const transport = id === null ? undefined : transports.mcp.get(id);
    if (!transport) return [];
    if (toolCache?.transport !== transport || toolCache.tools !== transport.tools) {
      toolCache = { transport, tools: transport.tools, declared: mcpToolDeclarations(transport, mcpToolDeps) };
    }
    return toolCache.declared;
  }

  /** Offer `sessionId`'s connection's tools (null: none), and have the shim register them now. */
  function declareMcp(sessionId: string | null): void {
    s.mcp.declared = sessionId;
    api.declarationsChanged();
  }

  // -- the reads poller --------------------------------------------------------

  function askHeldQuestions(ctx: any, sessionId: string, atPrompt: boolean): Promise<number> {
    if (!ctx?.hasUI) return Promise.resolve(0); // print/json mode: `probe approvals` in a terminal
    return asker.ask(
      sessionId,
      { hasUI: true, select: (title, options) => ctx.ui.select(title, options) },
      realApprovalsDeps(),
      atPrompt,
      logLine,
    );
  }

  function readsTick(): void {
    try {
      const poller = s.reads.poller;
      if (probeIsOff(s) || !poller) return;
      const live = poller.ctx;
      const sid = live?.sessionManager?.getSessionId();
      if (!sid) return;
      // Not awaited: a dialog stays open across ticks, and the asker opens
      // one at a time.
      void askHeldQuestions(live, sid, false);
      const box = mailboxFor(sid);
      if (!box.served()) return;
      const idle = typeof live.isIdle === "function" ? live.isIdle() : true;
      // Idle: answers and run alerts only, and they start a turn. Running:
      // one unasked message per turn too, steered in after the current tool
      // calls.
      box.touchAlive("pi poller");
      const texts = box.deliver(box.currentTurn(), "pi poller", !idle);
      if (texts.length) {
        if (idle) box.markWoke(); // the turn this starts is the same turn going on
        try {
          api.sendMessage(
            { customType: "probe-reads", content: texts.join("\n\n"), display: true },
            { deliverAs: "steer", triggerTurn: true },
          );
        } catch (err) {
          box.returnUnshown(box.lastClaimed); // never handed over: its inbox messages wait again
          throw err;
        }
        box.markShown(box.lastClaimed, "pi poller"); // only once sendMessage returned
        // Probe hears it was shown (inbox only; never awaited): through the service, never pi's own call.
        void reportDelivered(sid, box.lastClaimed);
      }
      notifyServerBanners(live, box);
      readsNotify(live, box.researcherNotice());
    } catch (err) {
      logLine(`reads poller: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  function startTimer(): ReturnType<typeof setInterval> {
    const timer = setInterval(readsTick, READS_POLL_MS);
    timer.unref?.();
    return timer;
  }

  /** Start the reads poller for this process, once a session the reader serves
   * (or on a machine where the daemon holds questions) has had its first prompt
   * (`before_agent_start`). */
  /** The inbox messages among `ids` were shown: the service tells Probe (`mailbox.delivered`). Never rejects. */
  async function reportDelivered(sessionId: string, ids: string[]): Promise<void> {
    const inbox = ids.filter((id) => INBOX_ID.test(id));
    if (!inbox.length) return;
    await callService(s.service.socket, "mailbox.delivered", { sid: sessionId, message_ids: inbox, env: boundedHookEnv(process.env) },
      DELIVERED_WAIT_MS, { clientVersion: PACKAGE_VERSION, log: logLine });
  }

  function startReadsPoller(ctx: any): void {
    const poller = s.reads.poller;
    if (poller) {
      poller.ctx = ctx;
      if (poller.timer === null && active) poller.timer = startTimer();
      return;
    }
    s.reads.poller = { ctx, timer: active ? startTimer() : null };
  }

  /** Stop this version's timer, keeping what it polls for the next version. */
  function pauseReadsPoller(): void {
    const poller = s.reads.poller;
    if (poller?.timer) {
      clearInterval(poller.timer);
      poller.timer = null;
    }
  }

  // -- capture -----------------------------------------------------------------

  /**
   * The footer read back after the service asked for this session's recorder (it does, at the
   * session start and at the next events while pi has not written the session's file: M9). A live
   * recorder claims the session after its first server read, ~2 s on (#3619): read it again a few
   * times while it has not, or the footer says "not capturing" of a session that is captured.
   */
  async function rereadCapture(ctx: any, sessionId: string): Promise<void> {
    s.capture.spawned.add(sessionId);
    await refreshTrackingStatus(s, footerClock, ctx, sessionId, ctx.cwd);
    const current = () => s.current.sessionId === sessionId && s.capture.spawned.has(sessionId);
    recheckUntilClaimed(s.probe.tracking?.capture, {
      reread: async () => {
        if (!current()) return ClaimRecheck.Stop;
        const read = await refreshTrackingStatus(s, footerClock, ctx, sessionId, ctx.cwd, { keepOnFailure: true });
        if (!current()) return ClaimRecheck.Stop;
        return read ? s.probe.tracking?.capture : undefined;
      },
      later: (fn, ms) => {
        setTimeout(fn, ms).unref?.();
      },
      log: logLine,
    });
  }

  /** A session whose file pi had not written at its start: once it has, the footer is read back. Never throws. */
  async function rereadPendingCapture(ctx: any, label: string): Promise<void> {
    try {
      const sessionId = ctx?.sessionManager?.getSessionId();
      const transcriptPath = sessionId ? s.capture.pending.get(sessionId) : undefined;
      if (!sessionId || !transcriptPath || !fs.existsSync(transcriptPath)) return;
      s.capture.pending.delete(sessionId);
      await rereadCapture(ctx, sessionId);
    } catch (err) {
      logLine(`${label}: capture read-back failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }


  // -- the events ----------------------------------------------------------------

  const onSessionStart: EventHandler = async (event, ctx) => {
    const sessionId = ctx.sessionManager.getSessionId();
    const transcriptPath = ctx.sessionManager.getSessionFile();
    s.current.sessionId = sessionId ?? null;
    footerClock.cancel(); // the previous session's (or context's) redraw; this start draws and arms its own

    // The service's socket first: everything below asks it. Asked for once found; this event
    // waits for it, bounded.
    const runtime = resolveTapRuntime(tapRuntimeDeps());
    if (runtime) {
      askServiceSocket(s, runtime);
      if (!s.service.socket) await waitForSocket(s, runtime);
    }
    // Seeds the ONE durable session signal before any capture-specific early
    // return, then renders that same answer in Pi's persistent footer. Reloads
    // are safe: the hidden initializer uses set-if-absent and returns the
    // existing signal rather than resolving the folder again.
    await refreshTrackingStatus(s, footerClock, ctx, sessionId, ctx.cwd);
    // A resumed session's agent hears again that Probe is inline (`/probe inline`).
    if (sessionId && event.reason === "resume") forgetInlineShown(sessionId);

    // THE SERVICE'S SESSION START (PR 7; service-only task 16): its routes give this session's
    // context (every turn's prompt gets it, the team note among it) and the server's notices, sync
    // the team note and flush unsent feedback. Nothing of it runs here.
    forwarder.beginTurn(sessionId);
    const started = await forwarder.send(ForwardedEvent.SessionStart, ctx, { reason: event.reason });
    if (sessionId && wasAccepted(started)) forwarder.keepContext(sessionId, answeredEffects(started));
    showNotices(ctx, answeredEffects(started));

    // Probe MCP read tools: entirely independent of the capture daemon below
    // (a separate credential — mcp_token, never the tap's ingest/device
    // token — and no transcript file is needed to register tools), so this
    // runs unconditionally, before the no-transcript-file early return and
    // before the killswitch/pairing checks that gate capture only. Bounded
    // internally (see mcpBridge.ts) so an unreachable server cannot stall
    // this handler; any failure degrades to a single announce(), never a
    // thrown error out of session_start.
    //
    // D6 stand-down check FIRST, before touching the connections at all: when
    // pi-mcp-adapter already owns the Probe MCP server (see
    // adapterHandoff.ts), connecting our own bridge on top would register
    // every tool twice. Re-run on EVERY session_start (cheap: two local file
    // reads, no network) rather than caching — a `packages` entry can change
    // between sessions (an adapter or our own package installed/removed) and
    // this must track that without a restart. Gating the whole block —
    // including the re-declare branch — behind `!standDown` is also what
    // keeps `/reload` well-behaved while stood down: the connections never
    // get an entry for this session in the first place, so there is no
    // stale connection for a later reload's check to trip on.
    //
    // THE DAEMON PROFILE reads for the agent (`probe ask`), so no Probe MCP
    // tool is offered at all: the bridge stays off, a connection an earlier
    // agent-profile start of this session opened is closed, and an adapter's
    // tools are deactivated (see `deactivateProbeTools`).
    // TRACKING OFF (trackingSetting.ts) offers no Probe MCP tool either,
    // read from the settings files themselves so no CLI answer can undo it.
    // Both compare against `installRoot`: the package pi's settings name.
    if (sessionId) {
      s.probe.trackingOff = {
        sessionId,
        off: trackingOff({ env: process.env, cwd: ctx.cwd, packageRoot: installRoot }),
      };
    }
    const handoff = inDaemonProfile(s, sessionId) || trackingOffFor(s, sessionId)
      ? null
      : detectAdapterHandoff({ env: process.env, cwd: ctx.cwd, packageRoot: installRoot });
    if (!handoff) {
      const stale = transports.mcp.get(sessionId);
      if (stale) {
        transports.mcp.delete(sessionId);
        stale.client.close().catch((err) => logLine(`session_start(${event.reason}): Probe MCP close failed: ${err instanceof Error ? err.message : String(err)}`));
      }
      if (s.mcp.declared !== null) declareMcp(null);
      deactivateProbeTools(api);
      const why = trackingOffFor(s, sessionId) ? "Probe tracking is off" : "daemon profile";
      logLine(`session_start(${event.reason}): ${why}, no Probe MCP tools for ${sessionId}`);
    } else if (handoff.standDown) {
      logLine(`session_start(${event.reason}): ${MCP_SERVED_VIA_ADAPTER_MESSAGE} — ${handoff.reason}`);
    } else {
      const existing = transports.mcp.get(sessionId);
      if (existing) {
        // Same session, extension reloaded (or session_start fired again for
        // some other reason, or a newer version took over) — offer the tools
        // already fetched again, with no network call.
        declareMcp(sessionId);
        logLine(`session_start(${event.reason}): Probe MCP re-registered ${existing.tools.length} tool(s) for ${sessionId}`);
      } else {
        try {
          const result = await connectMcp(defaultMcpBridgeDeps(process.env, (message, level) => announce(ctx, message, level), logLine));
          if ("transport" in result) {
            transports.mcp.set(sessionId, result.transport);
            declareMcp(sessionId);
            logLine(`session_start(${event.reason}): Probe MCP ready, ${result.transport.tools.length} tool(s) registered for ${sessionId}`);
          } else {
            logLine(`session_start(${event.reason}): Probe MCP unavailable for ${sessionId}: ${result.skipped}`);
          }
        } catch (err) {
          // connectMcp degrades internally and should never throw — this
          // catch exists only so a bug there can never take capture or the
          // team note down with it.
          logLine(`session_start(${event.reason}): Probe MCP bridge threw unexpectedly: ${err instanceof Error ? err.message : String(err)}`);
        }
      }
    }

    // Best-effort hygiene, every session start, as session-start.sh does: a pi-only install has no
    // other pruner of the sentinels `stopDaemon` leaves; and an earlier version's "no Python 3.11"
    // note goes (the tap runs on the service's own python now, M9). Never allowed to fail this handler.
    try {
      pruneStaleShutdownSentinels({
        readdirSync: (dir) => fs.readdirSync(dir),
        statMtimeMs: (path) => fs.statSync(path).mtimeMs,
        rmSync: (path) => fs.rmSync(path, { force: true }),
        now: () => Date.now(),
      });
      fs.rmSync(pythonMissingFile(process.env), { force: true });
    } catch {
      // Never let hygiene block the session.
    }
    if (!transcriptPath) {
      // In-memory / not-yet-persisted session (e.g. SessionManager.inMemory()) — no
      // JSONL file exists yet for the daemon to tail. Nothing to do.
      logLine(`session_start(${event.reason}): no session file for ${sessionId}, skipping`);
      return;
    }

    if (!fs.existsSync(transcriptPath)) {
      // A new session: pi writes its file at the first message; the service asks for its recorder
      // then (at the next event), and the footer is read back (`rereadPendingCapture`).
      s.capture.pending.set(sessionId, transcriptPath);
      logLine(`session_start(${event.reason}): ${sessionId}'s session file is not written yet; capture starts once pi writes it`);
      return;
    }
    // The service asked for this session's recorder at its start (above): read the footer back.
    await rereadCapture(ctx, sessionId);
  };

  const onSessionShutdown: EventHandler = async (event, ctx) => {
    footerClock.cancel();
    pauseReadsPoller();
    s.reads.poller = null;
    if (event.reason === ShutdownReason.Reload) {
      // Extensions are reloading, not the session — the SAME session id fires
      // session_start again right after this. Leave the daemon running:
      // stopping it here would spuriously FINALIZE a session that isn't
      // actually ending (main.py's shutdown path always enqueues a FINALIZE,
      // which is what triggers server-side knowledge-unit extraction). The
      // Probe MCP connection is left open too. Under pi 1.0 a reload evaluates
      // the extension afresh (its shim and this state included), so the
      // imminent session_start connects again and this one is left to the
      // process (`tests/shimPiLoader.test.ts`); a `session_start` that does
      // find its connection (a session or version switch) offers its tools
      // again with no network call.
      return;
    }
    const sessionId = ctx.sessionManager.getSessionId();
    stopDaemon(sessionId, realDaemonDeps());
    s.capture.pending.delete(sessionId);
    s.capture.spawned.delete(sessionId);
    // A session the Probe service records (its `.hosted` marker): on a quit the
    // service finishes it now, waited on for a few seconds; then the marker goes,
    // so a resume of this session is recorded again (daemon.ts, audit #10).
    try {
      await finishHostedSession(
        sessionId,
        { waitS: event.reason === ShutdownReason.Quit ? HOSTED_FINISH_WAIT_S : 0 },
        {
          ...realDaemonDeps(),
          // `session.finish` on the service itself: the extension spawns no `tap finish`.
          finish: (sid, waitS) => callService(s.service.socket, "session.finish", { sid, wait: waitS },
            waitS * 1000 + 1000, { clientVersion: PACKAGE_VERSION, log: logLine }),
        },
      );
    } catch (err) {
      logLine(`session_shutdown(${event.reason}): hosted finish failed: ${err instanceof Error ? err.message : String(err)}`);
    }
    const mcpConnection = transports.mcp.get(sessionId);
    if (mcpConnection) {
      transports.mcp.delete(sessionId);
      // The runtime is going away: nothing to register on it any more.
      if (s.mcp.declared === sessionId) s.mcp.declared = null;
      // Best-effort: closing the MCP client (an SSE stream + HTTP session)
      // must never block or fail this handler — a hung close() would stall
      // shutdown, and a failed one is inert (the process is ending anyway).
      mcpConnection.client.close().catch((err) => logLine(`session_shutdown(${event.reason}): Probe MCP close failed: ${err instanceof Error ? err.message : String(err)}`));
    }
    logLine(`session_shutdown(${event.reason}): stopped capture for ${sessionId}`);
    // The service's SessionEnd routes (record-keeping); a reload returned above, never an end (D28).
    await forwarder.send(ForwardedEvent.SessionShutdown, ctx, { reason: event.reason });
  };

  // THE DAEMON PROFILE'S SKILLS. pi asks for extra resource paths right after
  // every session_start (startup, reload, new, resume, fork), by which point
  // the CLI has answered for this session; the agent profile adds nothing.
  const onResourcesDiscover: EventHandler = (_event, ctx) => {
    const id = ctx?.sessionManager?.getSessionId();
    if (!inDaemonProfile(s, id) || trackingOffFor(s, id)) return;
    return { skillPaths: daemonSkillPaths(resourceRoot) };
  };

  // Inject, don't render: the team note rides the prompt the service builds (its prompt route). Fires on
  // EVERY turn, so this must stay a cheap string append against the cache
  // populated at session_start: no CLI spawn, and no file I/O EXCEPT in the
  // `daemon` state (and one turn after leaving it), where daemonNotice reads
  // the lease, the notified record and the config, and writes the record.
  //
  // THE DAEMON STATE rides the same event: `DAEMON_CONTEXT` on every turn it
  // holds (pi has no SessionStart hook to say it once), plus a one-off message
  // at the turn where recording changes hands -- see daemonNotice.ts. Both
  // belong to the AGENT profile only: in the daemon profile the prompt is the
  // lean set and the model is told nothing about the daemon (profile.ts).
  /**
   * The turn's prompt through the service (PR 7, D62): its prompt routes give this turn's
   * messages (daemon reads, inline and daemon notices, the parent question), with no consent
   * parsed; the session-start context goes into the prompt. pi keeps the person's dialogs,
   * its idle poller and the team note. Null: the service did not take it (pi's own code runs).
   */
  async function forwardPrompt(event: any, ctx: any, sessionId: string, base: string): Promise<{ systemPrompt: string; message: string | null } | null> {
    if (!forwarder.hasContext(sessionId)) {
      // Its record-keeping was queued at the session start already: this asks only for the context.
      const late = await forwarder.send(ForwardedEvent.SessionStart, ctx, { reason: "startup" }, false);
      if (wasAccepted(late)) forwarder.keepContext(sessionId, answeredEffects(late));
    }
    await askHeldQuestions(ctx, sessionId, true);
    const answer = await forwarder.send(ForwardedEvent.BeforeAgentStart, ctx, { prompt: event.prompt, agent_dir: piConfigRoot(process.env) });
    if (!wasAccepted(answer)) return null;
    const effects = answeredEffects(answer);
    const parts = [base, forwarder.getContext(sessionId), ...(effects?.systemPrompt ?? [])].filter(Boolean);
    showNotices(ctx, effects);
    // The service's routes told the agent about the daemon this turn.
    s.probe.lastTurnInDaemon = s.probe.state === ProbeState.Daemon;
    if (s.probe.state === ProbeState.Daemon && inDaemonProfile(s, sessionId)) tellResearcherAboutDaemon(s, footerClock, ctx, sessionId);
    try {
      if (mailboxFor(sessionId).served() || heldQuestionsBoardExists()) startReadsPoller(ctx);
    } catch (err) {
      logLine(`reads at prompt: ${err instanceof Error ? err.message : String(err)}`);
    }
    // Acked once pi has the result (after this handler returns): its messages count as shown then.
    if (effects?.receipts.length) setImmediate(() => void forwarder.settle(effects, true));
    return { systemPrompt: parts.join("\n\n"), message: effects?.messages.length ? effects.messages.join("\n\n") : null };
  }

  /**
   * Probe auto mode, an off chat: the turn's prompt through the service for its one line on where
   * this chat's Probe stands (the guard's prompt route), as a hidden message. Never the team note or
   * the session-start context: nothing else of Probe rides an off chat's prompt.
   */
  async function forwardAutoLine(event: any, ctx: any, sessionId: string) {
    const answer = await forwarder.send(ForwardedEvent.BeforeAgentStart, ctx, { prompt: event.prompt, agent_dir: piConfigRoot(process.env) });
    if (!wasAccepted(answer)) return undefined;
    const effects = answeredEffects(answer);
    if (effects?.receipts.length) setImmediate(() => void forwarder.settle(effects, true));
    if (!effects?.messages.length) return undefined;
    logLine(`auto mode line for ${sessionId}`);
    return { message: { customType: "probe-daemon", content: effects.messages.join("\n\n"), display: false } };
  }

  const onBeforeAgentStart: EventHandler = async (event, ctx) => {
    // Not awaited: the prompt must not wait on a footer read.
    void rereadPendingCapture(ctx, "before_agent_start");
    const sessionId = ctx?.sessionManager?.getSessionId();
    // `probe session state inline` from a terminal moves the switch on disk
    // without a typed `/probe`: read it back, or a cached `off` would return
    // below before the agent hears it is inline. In an auto chat the agent
    // moves an off switch from its own shell, so an off reading is read back too.
    if (sessionId && ctx && ((s.probe.state !== ProbeState.Full && inDaemonProfile(s, sessionId) && sessionIsInline(sessionId))
      || (probeIsOff(s) && isAutoChat(s, sessionId)))) {
      await refreshTrackingStatus(s, footerClock, ctx, sessionId, ctx.cwd);
    } else if (sessionId && ctx && isTrackingStale(s)) {
      // A footer left on `status unavailable` (a failed read, or a kept one past its time) is read again here,
      // since nothing else would; in the background, so a service that is down never holds up the prompt.
      void refreshTrackingStatus(s, footerClock, ctx, sessionId, ctx.cwd).catch(() => undefined);
    }
    forwarder.beginTurn(sessionId);
    if (trackingOffFor(s, sessionId)) {
      // Capture already started above; tracking adds nothing: no package
      // skill, no Probe MCP tool, nothing injected into the prompt.
      if (event.systemPromptOptions?.skills) {
        event.systemPromptOptions.skills = withoutPackageSkills(event.systemPromptOptions.skills);
      }
      deactivateProbeTools(api);
      return;
    }
    const daemonProfile = inDaemonProfile(s, sessionId);
    if (daemonProfile) {
      // In every state, `off` included: the profile is the machine's choice of
      // recorder, the state is this session's. Trimmed BEFORE the prompt is
      // read below: `event.systemPrompt` renders from these options.
      if (event.systemPromptOptions?.skills) {
        event.systemPromptOptions.skills = leanSkills(event.systemPromptOptions.skills, daemonSkills);
      }
      deactivateProbeTools(api);
    }
    if (!sessionId) return;
    // An off auto chat still hears the service's line on where Probe stands; nothing else rides it.
    if (probeIsOff(s)) return isAutoChat(s, sessionId) ? await forwardAutoLine(event, ctx, sessionId) : undefined;
    // The service builds the prompt's additions (the team note, daemon reads and notices included);
    // one it did not answer goes on with no change (service-only task 16).
    const forwarded = await forwardPrompt(event, ctx, sessionId, event.systemPrompt);
    if (forwarded === null || (forwarded.systemPrompt === event.systemPrompt && !forwarded.message)) return;
    return {
      ...(forwarded.systemPrompt !== event.systemPrompt ? { systemPrompt: forwarded.systemPrompt } : {}),
      ...(forwarded.message ? { message: { customType: "probe-daemon", content: forwarded.message, display: false } } : {}),
    };
  };

  // THE GUARD, in the Probe service: every tool call goes there (a codemode script's too, so an
  // inactive tool cannot slip by), and its pre-tool routes (the Python guard Claude Code runs)
  // refuse it in their own words. No service, an older one, out of time: the call runs (fail open,
  // as every Probe hook); pi runs no guard of its own (service-only task 16).
  /** A tool call as the service's pi routes read it: the path pi will write resolved here, as pi resolves it. */
  /** The tool call as the service's guard reads it: never the body of a write or an edit (C1). */
  function toolCallFields(event: any, cwd: string): Record<string, unknown> {
    const raw = typeof event.input === "object" && event.input !== null ? event.input as Record<string, unknown> : {};
    const input = Object.fromEntries(Object.entries(raw).filter(([name]) => !GATE_BULK_FIELDS.has(name)));
    const fields: Record<string, unknown> = { tool_name: event.toolName, input, tool_call_id: event.toolCallId };
    const path = (event.input as Record<string, unknown> | undefined)?.path;
    if ((event.toolName === PiTool.Write || event.toolName === PiTool.Edit) && typeof path === "string" && path) {
      try {
        fields.resolved_path = resolveLikePi(path, cwd);
      } catch {
        // a path pi itself would refuse: the service reads the call without it
      }
    }
    return fields;
  }

  const onToolCall: EventHandler = async (event, ctx) => {
    // The session file exists by the first tool call. Not awaited: a tool must
    // not wait on a footer read.
    void rereadPendingCapture(ctx, "tool_call");
    try {
      const sessionId = ctx?.sessionManager?.getSessionId();
      if (!sessionId) return;
      const cwd = ctx?.cwd ?? process.cwd();
      const fields = toolCallFields(event, cwd);
      const size = Buffer.byteLength(JSON.stringify(fields));
      if (size > MAX_GATE_PAYLOAD) {
        // Too big to check is never let through: the input is the agent's to pad (C1).
        return { block: true, reason: guardDenied("too_big", `${Math.floor(size / 1024)} KiB; the limit is ${MAX_GATE_PAYLOAD / 1024} KiB`) };
      }
      const answer = await forwarder.send(ForwardedEvent.ToolCall, ctx, fields);
      // Only a forgery is refused (`not_harness`: it only ever stops the forger). No service, out of
      // time (a slow service never stops the agent), or any other refusal: the call goes on, logged.
      if (answer?.outcome === ForwardOutcome.Refused) {
        if (answer.refusedReason === REFUSED_NOT_HARNESS) return { block: true, reason: guardDenied("not_harness") };
        logLine(`guard: the Probe service did not check ${event.toolName} (${answer.refusedReason ?? "refused"}); `
          + "it went on. If this keeps happening, run `probe doctor`.");
        return;
      }
      const block = answeredEffects(answer)?.block;
      if (!block) return;
      logLine(`guard refused ${event.toolName} (the service's routes): ${block.slice(0, 120)}`);
      return { block: true, reason: block };
    } catch (err) {
      // A guard bug must never take the session's tools down with it.
      logLine(`guard failed open: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
  };

  // agent_settled, NOT turn_end — agent_settled is pi's analogue of Claude
  // Code's Stop (fires once an agent run has fully settled, no automatic
  // retry/compaction/continuation pending); turn_end fires several times per
  // user message and would push/pull far more than needed. The team note's
  // sync is the service's (its `agent_settled` route), never this extension's.
  const onAgentSettled: EventHandler = async (_event, ctx) => {
    await rereadPendingCapture(ctx, "agent_settled");
    if (probeIsOff(s)) return;
    // The reads poller starts at a prompt only once the daemon serves this
    // session. A daemon that came up mid-turn (a new session's capture starts
    // at its first tool call) would otherwise leave an asked answer waiting
    // for the researcher's next prompt instead of arriving while pi is idle.
    try {
      const sid = ctx?.sessionManager?.getSessionId();
      if (sid && (mailboxFor(sid).served() || heldQuestionsBoardExists())) startReadsPoller(ctx);
      // Redraw the footer: it was drawn at the prompt, before a daemon that
      // starts with the session's first tool call could go live, and a turn
      // the daemon's answer starts has no prompt to redraw it.
      if (sid && ctx?.hasUI) renderTrackingFooter(s, footerClock, ctx, sid);
    } catch (err) {
      logLine(`reads at settle: ${err instanceof Error ? err.message : String(err)}`);
    }
    // The daemon's turn signal (turnSignal.ts), before the note sync: it is two
    // small file writes, and only in `daemon`.
    if (s.probe.state === ProbeState.Daemon) {
      try {
        writeTurnSignal(ctx.sessionManager.getSessionId(), ctx.sessionManager.getSessionFile(), realTurnSignalDeps());
      } catch {
        // No session manager on this event: the worker keeps its quiet timer.
      }
    }
    // The service's turn-end record-keeping (the team note's sync among it), or queued for the next one.
    await forwarder.send(ForwardedEvent.AgentSettled, ctx);
  };

  // An inline agent that leaves a summary or a note owed is sent back, as the
  // plugin's Stop hook does (the service's routes decide). At `agent_before_settle`, NOT
  // agent_settled: pi fires that one after every run, a run the researcher
  // stopped (Esc) and a provider error included, and a new turn there would
  // restart an agent they just stopped. Here only a run that `completed` is
  // continued, by pi's own boundary result.
  const onAgentBeforeSettle: EventHandler = async (event, ctx) => {
    if (event?.outcome !== ActivityOutcome.Completed || probeIsOff(s)) return;
    const forwarded = await forwarder.send(ForwardedEvent.AgentBeforeSettle, ctx, { outcome: event.outcome });
    const text = answeredEffects(forwarded)?.continueText;
    return text ? { entries: [{ type: "custom_message", customType: "probe-owed", content: text, display: true }], continue: true } : undefined;
  };

  // A compaction took the inline notice out of the agent's context: the next
  // prompt tells it again (`before_agent_start`).
  const onSessionCompact: EventHandler = async (event, ctx) => {
    const sessionId = ctx?.sessionManager?.getSessionId();
    if (sessionId) forgetInlineShown(sessionId);
    await forwarder.send(ForwardedEvent.SessionCompact, ctx, { reason: event?.reason });
  };

  // THE TRACKING SWITCH. `input` fires on the RAW line, before pi expands
  // `/skill:<name>` — which is what lets a `/track-work` typed out of Claude
  // Code habit be rewritten to pi's own spelling instead of reaching the model
  // as prose pi ignored. The switch itself is the service's guard (a typed line, from pi itself).
  const onInput: EventHandler = async (event, ctx) => {
    // Cheap gate FIRST: this handler is on the path of every message the
    // researcher sends, and on all but a handful it must cost one string
    // compare and return.
    const intent = parseSwitchIntent(event.text);
    if (!intent) return { action: "continue" };

    // THE CONSENT RULE, as a field rather than an inference. "interactive" is
    // typed by a person; "rpc" and "extension" are not, and an agent that can
    // flip this switch can turn tracking back on to make its own writes legal,
    // which is the entire thing the switch exists to prevent. An agent
    // invocation still gets the guidance — it just moves nothing.
    if (event.source !== "interactive") {
      logLine(`tracking switch ignored: source=${event.source}`);
      return { action: "transform", text: intent.canonicalText };
    }

    let applied = false;
    if (intent.direction !== null) {
      const sessionId = ctx.sessionManager.getSessionId();
      if (!sessionId) {
        logLine("tracking switch skipped: no session id");
        return { action: "transform", text: intent.canonicalText };
      }
      // The service's switch (service-only task 16): one it did not answer leaves the line unchanged.
      forwarder.beginTurn(sessionId);
      const forwarded = await forwarder.send(ForwardedEvent.Input, ctx, { text: event.text, source: event.source });
      applied = ranToEnd(forwarded);
      logLine(`tracking switch: the Probe service ${applied ? "ran it" : `did not (${forwarded?.outcome ?? "no session"})`}`);
      if (!applied) return { action: "continue" };
      await refreshTrackingStatus(s, footerClock, ctx, sessionId, ctx.cwd);
    }
    // Transform, never "handled": the researcher asked for the skill, and the
    // model still needs the manual — not least to READ THE STATE BACK and say
    // what the switch actually did.
    return {
      action: "transform",
      text:
        applied && intent.direction !== null
          ? intent.canonicalText + switchAppliedNotice(intent.direction)
          : intent.canonicalText,
    };
  };

  // -- the commands --------------------------------------------------------------

  const probeStatus: CommandDeclaration = {
    description: "Show Probe Research session-capture status",
    handler: async (_args, ctx) => {
      const sessionId = ctx.sessionManager.getSessionId();
      // The CLI's reading now (`probe session initialize` asks the Probe
      // service what its recorder does), so this line, the footer and `probe
      // doctor` agree; a reading that cannot be had leaves the marker's.
      if (sessionId) {
        await refreshTrackingStatus(s, footerClock, ctx, sessionId, ctx.cwd);
      }
      const report = buildStatusReport(sessionId, {
        existsSync: fs.existsSync,
        isExecutable,
        readFileSync: (path) => fs.readFileSync(path, "utf-8"),
        kill: (pid, signal) => process.kill(pid, signal),
        env: process.env,
        extensionDir: resourceRoot,
        installRoot,
        cwd: ctx.cwd,
        capture: sessionId ? s.probe.tracking?.capture : undefined,
      });
      logLine("probe-status invoked");
      if (ctx.hasUI) {
        ctx.ui.notify(report, "info");
      }
      // Always also go to stderr: notify() is a silent no-op in headless
      // modes (print/json), and this command is meaningless without SOME
      // visible output.
      process.stderr.write(report + "\n");
    },
  };

  const probeMcpLogin: CommandDeclaration = {
    description: "Connect Probe Research's read MCP tools — bearer token first, interactive OAuth login otherwise",
    handler: async (_args, ctx) => {
      const sessionId = ctx.sessionManager.getSessionId();
      if (inDaemonProfile(s, sessionId)) {
        // The guard refuses every Probe MCP call in this profile, so tools
        // registered here would only be offered to be refused.
        announce(ctx, "The Probe daemon reads for this session, so Probe's MCP tools stay off. To change who records, ask your agent.", "info");
        return;
      }
      const existing = transports.mcp.get(sessionId);
      if (existing) {
        // Already connected this session (session_start's own attempt, or an
        // earlier /probe-mcp-login) — report that rather than opening a
        // SECOND connection.
        const message = `Probe MCP is already connected — ${existing.tools.length} tool(s) registered.`;
        logLine(`probe-mcp-login(connected): ${message}`);
        announce(ctx, message, "info");
        return;
      }
      // A connection made here is kept like session_start's, so its tools are
      // offered again after a reload and it closes with the session.
      const result = await interactiveOAuthConnect(
        defaultMcpBridgeDeps(process.env, (message, level) => announce(ctx, message, level), logLine),
        { hasUI: ctx.hasUI, input: (title, placeholder) => ctx.ui.input(title, placeholder) },
      );
      if (result.transport) {
        transports.mcp.set(sessionId, result.transport);
        declareMcp(sessionId);
      }
      logLine(`probe-mcp-login(${result.status}): ${result.message}`);
      const level = result.status === "failed" ? "error" : result.status === "requires-interactive" ? "warning" : "info";
      announce(ctx, result.message, level);
    },
  };

  const declarations: Declarations = {
    events: {
      [ShimEvent.SessionStart]: onSessionStart,
      [ShimEvent.SessionShutdown]: onSessionShutdown,
      [ShimEvent.ResourcesDiscover]: onResourcesDiscover,
      [ShimEvent.BeforeAgentStart]: onBeforeAgentStart,
      [ShimEvent.ToolCall]: onToolCall,
      [ShimEvent.AgentSettled]: onAgentSettled,
      [ShimEvent.AgentBeforeSettle]: onAgentBeforeSettle,
      [ShimEvent.SessionCompact]: onSessionCompact,
      [ShimEvent.Input]: onInput,
    },
    commands: {
      "probe-status": probeStatus,
      "probe-mcp-login": probeMcpLogin,
    },
    tools: mcpTools,
  };

  return {
    shared,
    declarations,
    activate() {
      active = true;
      // A poller the version before this one ran (or this one, before a
      // rolled-back switch) goes on with this version's tick.
      const poller = s.reads.poller;
      if (poller && poller.timer === null) poller.timer = startTimer();
    },
    deactivate() {
      active = false;
      pauseReadsPoller();
      footerClock.cancel();
    },
    status() {
      const id = s.current.sessionId;
      return { recorded: id !== null && hostedServicePid(id) !== null };
    },
  };
}
