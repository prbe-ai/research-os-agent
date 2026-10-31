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
import { newAskerState, QuestionAsker, feedbackUnsent, spawnFeedbackFlush, type ApprovalsDeps, type AskerState } from "./core/approvals.js";
import { guardToolCall, sessionInlineReadOnly, sessionIsInline } from "./guard.js";
import {
  cliHasInlineBridge,
  forgetInlineShown,
  inlineNotice,
  inlineNoticeDue,
  inlineSkillsText,
  localCliVersion,
  markInlineShown,
} from "./inlineNotice.js";
import { agentOwes, owedSendBack } from "./owed.js";
import {
  ClaimRecheck,
  finishHostedSession,
  HOSTED_FINISH_WAIT_S,
  pruneStaleShutdownSentinels,
  recheckUntilClaimed,
  SpawnConfirmation,
  spawnDaemon,
  stopDaemon,
  waitForSpawnConfirmation,
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
import { approvalsDir, disabledFile, extensionLogFile, probeStateDir, pythonMissingFile, teamNoteDocumentPath } from "./core/paths.js";
import { hostedMarkerStamp, hostedServicePid } from "./core/hosted.js";
import { Mailbox } from "./core/reads.js";
import { checkPairing } from "./core/pairing.js";
import { buildStatusReport } from "./status.js";
import { pythonSeen, resolveTapRuntime, type TapRuntime, type TapRuntimeDeps } from "./core/tapRuntime.js";
import { ensureService, lookupServiceSocket } from "./core/service.js";
import { packageVersion } from "./packageVersion.js";
import { guardViaService } from "./serviceGuard.js";
import { applyTrackingSwitch, parseSwitchIntent, switchAppliedNotice, type SwitchChild, type SwitchSpawnFn } from "./core/trackingSwitch.js";
import { writeTurnSignal, type TurnSignalDeps } from "./core/turnSignal.js";
import { teamItemsHidden } from "./core/workspaceLimit.js";
import {
  companionKeyHeld,
  DAEMON_CONTEXT,
  daemonNotice,
  daemonStatus,
  DaemonStatus,
  type DaemonNoticeDeps,
  type DaemonStatusValue,
} from "./core/daemonNotice.js";
import { daemonSkillPaths, isProbeMcpTool, leanSkills, readDaemonSkills, researcherDaemonNotice, withoutPackageSkills } from "./profile.js";
import { trackingOff } from "./trackingSetting.js";
import {
  initializeTrackingState,
  ProbeState,
  Recorder,
  trackingStatusText,
  type CaptureReading,
  type ProbeStateValue,
  type RecorderValue,
  type TrackingExecFileFn,
} from "./core/trackingState.js";
import {
  hasProbePointer,
  readTeamNote,
  spawnTeamNoteSync,
  syncTeamNoteThenRead,
  type TeamNoteSyncDeps,
} from "./core/teamNote.js";
import { renderTeamNoteForPrompt } from "./teamNotePrompt.js";
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
   * `spawned`: sessions this process started capture for -- the same-session
   * double-spawn guard, and which session's daemon to stop at shutdown. The
   * real dedup authority is still the pidfile check inside spawnDaemon(); this
   * is a same-process fast path that also closes the narrow race where
   * session_start could fire twice before a freshly-spawned wrapper has written
   * its own pidfile.
   *
   * `pending`: sessions whose file pi had not written at session_start. pi 1.0
   * writes a NEW session's file at its first message, after session_start:
   * `tap start` refuses a transcript that does not exist (EXIT_NO_TRANSCRIPT)
   * and nothing retried, so a new interactive session was never captured and
   * the daemon never started for it. Session id -> the file to wait for.
   */
  capture: { spawned: Set<string>; pending: Map<string, string> };
  /**
   * The team-note cache. Populated ONCE per real session_start by readTeamNote(),
   * read by EVERY before_agent_start of that session, and never refreshed
   * mid-session -- see teamNote.ts's module docstring ("cache once per session").
   *
   * `hidden`: were team-wide items hidden by this computer's workspace limit
   * when the note was last looked at? While they are, the note is neither
   * synced nor cached -- the machine's one document may be another account's
   * by the time they show again -- so the first prompt after `show` syncs and
   * reloads it first.
   */
  note: { text: string | null; hidden: boolean };
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
     * when the daemon's lease changes without spawning the CLI again. */
    tracking: { tracking: boolean; capture?: CaptureReading } | undefined;
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
    /**
     * `.probe.config` (paths Probe never records), from `session initialize`:
     * the line that goes into the prompt while Probe records, and the question
     * about files above the launch folder, delivered once with the next turn.
     */
    configContext: string | null;
    parentAsk: string | null;
  };
  /**
   * This machine's Probe service socket, for the guard's `hooks.run` (plan D38):
   * asked of the tap once per pi process (`lookupServiceSocket`, a Python start,
   * so never on a tool call's path). Until it answers, and if it cannot, the
   * guard is `guard.ts` alone.
   */
  service: { socket: string | null; asked: boolean };
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
    note: { text: null, hidden: false },
    probe: {
      state: undefined,
      tracking: undefined,
      profile: undefined,
      trackingOff: undefined,
      daemonHealth: undefined,
      lastTurnInDaemon: false,
      configContext: null,
      parentAsk: null,
    },
    service: { socket: null, asked: false },
    reads: { poller: null },
    questions: newAskerState(),
    current: { sessionId: null },
    mcp: { declared: null },
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

/** The footer for this session: in `daemon`, it reads the lease file. */
function footerText(s: SessionState, sessionId: string, daemonLive?: boolean): string | undefined {
  const cached = s.probe.tracking;
  if (!cached) return undefined;
  const live =
    s.probe.state !== ProbeState.Daemon
      ? undefined
      : (daemonLive ?? daemonStatus(sessionId, realDaemonNoticeDeps()).status === DaemonStatus.Live);
  // `on (inline)`: the researcher set Probe inline (`/probe inline`).
  const inline = inDaemonProfile(s, sessionId) && sessionIsInline(sessionId);
  if (inline && sessionInlineReadOnly(sessionId)) return "○ read only (inline)";
  // Probe off: no Probe calls, and the Probe service holds the session
  // without capturing it. The status line's word for it.
  if (probeIsOff(s)) return "○ off";
  return trackingStatusText(cached.tracking, cached.capture, live, inline);
}

/** Has the researcher switched Probe off for this session? */
function probeIsOff(s: SessionState): boolean {
  return s.probe.state === ProbeState.Off;
}

function askServiceSocket(s: SessionState, runtime: TapRuntime): void {
  if (s.service.asked) return;
  s.service.asked = true;
  const service = s.service;
  void lookupServiceSocket(runtime, { execFile: realTrackingExecFile, env: process.env, log: logLine }).then((socket) => {
    service.socket = socket;
    if (socket) logLine(`probe service: the guard asks the service at ${socket}`);
  });
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
    sendFeedback: () => spawnFeedbackFlush(realTeamNoteSyncDeps()),
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

/** Same call, kept separate because the switch WAITS on its child. */
const realSwitchSpawn: SwitchSpawnFn = (command, args, options) =>
  spawn(command, args, options) as unknown as SwitchChild;

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
    spawn: realSpawn,
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

function realTeamNoteSyncDeps(): TeamNoteSyncDeps {
  return {
    spawn: realSpawn,
    existsSync: fs.existsSync,
    isExecutable,
    env: process.env,
    log: logLine,
  };
}

/**
 * Read the session's state back from the CLI (`probe session initialize`) and
 * draw the footer from it. A reading that comes back after pi switched to
 * another session is the old one's: never committed over the new one's (its
 * switch, profile, `.probe.config` context and footer). `keepOnFailure`: a
 * read that fails leaves the last reading, not an unknown one (a re-read after
 * the start, `recheckUntilClaimed`). True when a reading was committed.
 */
async function refreshTrackingStatus(
  s: SessionState,
  ctx: { hasUI: boolean; ui: { setStatus: (key: string, value: string | undefined) => void } },
  sessionId: string,
  cwd: string,
  opts: { keepOnFailure?: boolean } = {},
): Promise<boolean> {
  const state = await initializeTrackingState(sessionId, cwd, {
    execFile: realTrackingExecFile,
    existsSync: fs.existsSync,
    isExecutable,
    env: process.env,
    log: logLine,
  });
  if (s.current.sessionId !== null && s.current.sessionId !== sessionId) return false;
  if (!state && opts.keepOnFailure) return false;
  if (!state) {
    // Unknown, not the last value seen: see SessionState.probe.state's docstring.
    s.probe.state = undefined;
    s.probe.tracking = undefined;
    if (ctx.hasUI) {
      try {
        // Never leave a previous authoritative-looking value visible when
        // the current state could not be read back.
        ctx.ui.setStatus("probe-tracking", undefined);
      } catch (err) {
        logLine(
          `tracking footer clear failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    }
    return false;
  }
  s.probe.state = state.state;
  s.probe.tracking = { tracking: state.tracking, capture: state.capture };
  s.probe.configContext = state.probeConfig?.context ?? null;
  if (state.probeConfig?.ask) s.probe.parentAsk = state.probeConfig.ask;
  s.probe.profile = { sessionId, profile: state.profile };
  if (ctx.hasUI) {
    try {
      ctx.ui.setStatus("probe-tracking", footerText(s, sessionId));
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
 * unregister, and the guard (`guard.ts`) refuses a call that reaches one
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
  ctx: { hasUI: boolean; ui: { notify: (msg: string, level?: "info" | "warning" | "error") => void; setStatus: (key: string, value: string | undefined) => void } },
  sessionId: string,
): void {
  const deps = realDaemonNoticeDeps();
  const { status, reason } = daemonStatus(sessionId, deps);
  const previous = s.probe.daemonHealth;
  const health = previous?.sessionId === sessionId ? previous : { sessionId, status: null, prompts: 0 };
  const { notice, next } = researcherDaemonNotice(health.status, status, reason, health.prompts === 0, () =>
    companionKeyHeld(deps),
  );
  s.probe.daemonHealth = { sessionId, status: next, prompts: health.prompts + 1 };
  if (notice) logLine(notice);
  if (!ctx.hasUI) return;
  try {
    ctx.ui.setStatus("probe-tracking", footerText(s, sessionId, status === DaemonStatus.Live));
    if (notice) ctx.ui.notify(notice, "warning");
  } catch (err) {
    logLine(`daemon status notice failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

/**
 * Every coding agent's session variable, from the harness registry beside this
 * file (`harnesses.json`, synced from `probe.foundation.harness_registry`): pi's switch spawns the
 * CLI without them (`trackingSwitch.ts`). Empty when the copy cannot be read.
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
  let active = false;

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
        void box.reportDelivered(box.lastClaimed); // Probe hears it was shown (inbox only; never awaited)
      }
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
   * Start capture (the tap watcher, which starts the daemon) for one session:
   * the killswitch, pairing and interpreter checks, the spawn, then the footer
   * read back. From session_start when the session file already exists, and
   * otherwise once pi has written it (`startPendingCapture`).
   */
  async function startCapture(ctx: any, sessionId: string, transcriptPath: string, label: string): Promise<void> {
    // Best-effort hygiene, every session start, matching session-start.sh:
    // never allowed to fail this handler.
    try {
      pruneStaleShutdownSentinels({
        readdirSync: (dir) => fs.readdirSync(dir),
        statMtimeMs: (path) => fs.statSync(path).mtimeMs,
        rmSync: (path) => fs.rmSync(path, { force: true }),
        now: () => Date.now(),
      });
    } catch {
      // Never let hygiene block capture.
    }

    // Killswitch: presence of .disabled disables the daemon entirely — checked
    // FIRST, same order as session-start.sh, and silently (log file only, no
    // stderr/notify): the user turned capture off on purpose, so re-announcing
    // that on every single session start would just be noise.
    if (fs.existsSync(disabledFile(process.env))) {
      logLine(`${label}: killswitch active, skipping ${sessionId}`);
      return;
    }

    if (s.capture.spawned.has(sessionId)) {
      return;
    }

    const pairing = checkPairing(process.env);
    if (!pairing.paired) {
      announce(ctx, pairing.reason, "warning");
      return;
    }

    const runtimeDeps = tapRuntimeDeps();
    const runtime = resolveTapRuntime(runtimeDeps);
    if (!runtime) {
      // What was found instead, for the researcher and for `probe doctor`
      // (`.python-missing`, as the tap's own session-start.sh leaves it).
      const seen = pythonSeen(runtimeDeps);
      try {
        fs.writeFileSync(pythonMissingFile(process.env), `${seen}\n`);
      } catch {
        // No tap state folder to note it in: the announcement still says it.
      }
      announce(
        ctx,
        `no Python 3.11 or newer found for the probe-research-tap daemon (${seen}) — capture disabled for this session. ` +
          "Install Python 3.11 or newer, or set PROBE_PI_TAP_ROOT, then start a new session.",
        "error",
      );
      return;
    }
    try {
      fs.rmSync(pythonMissingFile(process.env), { force: true });
    } catch {
      // Best-effort, as the hook's `rm -f`.
    }

    const deps = realDaemonDeps();
    // The service's marker as it was before this start: only one this start
    // produced (or `tap start` exiting 0 beside it) confirms it below.
    const markerBefore = hostedMarkerStamp(sessionId);
    const result = spawnDaemon({ sessionId, transcriptPath, cwd: ctx.cwd, runtime, ownerPid: process.pid }, deps);
    // Marked SYNCHRONOUSLY, with no await between the spawn decision and this
    // line: `capture.spawned` is the same-process fast path (see its own
    // comment), and the whole point of it is to close before
    // waitForSpawnConfirmation's multi-second wait below even starts — a
    // second session_start racing in during that wait must see this set
    // already updated, not find the window still open.
    s.capture.spawned.add(sessionId);
    if (result.spawned) {
      logLine(`${label}: spawned capture for ${sessionId} (pid ${result.pid ?? "unknown"})`);
      // A session the Probe service records confirms by its `.hosted` marker,
      // with no pid file: the wait ends then instead of at its 2 s bound.
      const confirmed = await waitForSpawnConfirmation(sessionId, deps, {
        spawned: { markerBefore, exitCode: result.exitCode, switchedOff: probeIsOff(s) },
      });
      if (confirmed === SpawnConfirmation.Hosted) {
        logLine(`${label}: the Probe service records ${sessionId}`);
      }
    } else {
      logLine(`${label}: capture already running for ${sessionId}`);
    }

    // Read the footer back now that `tap start` has had its say. The refresh
    // at the top of this handler ran BEFORE the spawn, so its capture reading
    // is the one reading guaranteed to be stale — and a `tap start` that
    // refused (a gate this handler does not pre-check, an interpreter below
    // 3.11) would otherwise leave "● tracking" on screen for the rest of the
    // session with nothing capturing behind it.
    await refreshTrackingStatus(s, ctx, sessionId, ctx.cwd);
    // A live recorder claims the session after its first server read, ~2 s on
    // (#3619): read it again a few times while it has not, or the footer says
    // "not capturing" of a session that is captured until something redraws it.
    recheckUntilClaimed(s.probe.tracking?.capture, {
      reread: async () => {
        const current = () => s.current.sessionId === sessionId && s.capture.spawned.has(sessionId);
        if (!current()) return ClaimRecheck.Stop;
        const read = await refreshTrackingStatus(s, ctx, sessionId, ctx.cwd, { keepOnFailure: true });
        if (!current()) return ClaimRecheck.Stop;
        return read ? s.probe.tracking?.capture : undefined;
      },
      later: (fn, ms) => {
        setTimeout(fn, ms).unref?.();
      },
      log: logLine,
    });
  }

  /** Start a pending session's capture once its file exists. Never throws. */
  async function startPendingCapture(ctx: any, label: string): Promise<void> {
    try {
      const sessionId = ctx?.sessionManager?.getSessionId();
      const transcriptPath = sessionId ? s.capture.pending.get(sessionId) : undefined;
      if (!sessionId || !transcriptPath || !fs.existsSync(transcriptPath)) return;
      s.capture.pending.delete(sessionId);
      await startCapture(ctx, sessionId, transcriptPath, label);
    } catch (err) {
      logLine(`${label}: deferred capture start failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  // -- the events ----------------------------------------------------------------

  const onSessionStart: EventHandler = async (event, ctx) => {
    // Team note: SYNC FIRST, then refresh the cache, for EVERY session_start
    // reason including "reload". The document is one file per machine, written
    // by whichever credential last synced — so reading it cold briefs this
    // session from whatever was left there, which after a credential switch is
    // the previous tenant's note. The sync is what parks that copy and installs
    // ours; waiting for it (bounded, never longer than
    // SYNC_BEFORE_READ_TIMEOUT_MS, child left running on timeout) makes the
    // read an ordinary one. Independent of the capture-daemon logic below on
    // purpose: a session with no transcript file, no pairing, or the killswitch
    // active should still see the team note. Neither call ever throws.
    const sessionId = ctx.sessionManager.getSessionId();
    const transcriptPath = ctx.sessionManager.getSessionFile();
    s.current.sessionId = sessionId ?? null;

    // THE SWITCH IS RESOLVED FIRST, and it did not used to be. The note sync
    // below is a network call and the read after it becomes text in every
    // prompt, so both have to know whether this session is `off` -- and they
    // cannot ask after the fact. Nothing in resolving the switch depends on the
    // note, so this is a pure reorder.
    //
    // Seeds the ONE durable session signal before any capture-specific early
    // return, then renders that same answer in Pi's persistent footer. Reloads
    // are safe: the hidden initializer uses set-if-absent and returns the
    // existing signal rather than resolving the folder again.
    await refreshTrackingStatus(s, ctx, sessionId, ctx.cwd);
    // A resumed session's agent hears again that Probe is inline (`/probe inline`).
    if (sessionId && event.reason === "resume") forgetInlineShown(sessionId);

    if (probeIsOff(s)) {
      // `off` means no Probe calls and nothing injected. Leaving this out was
      // the difference between a switch and a promise: the note still synced
      // over the network every settle, and still rode into every prompt.
      s.note.text = null;
      logLine("probe state is off: skipping the team-note sync and injection");
    } else if (!hasProbePointer(process.env)) {
      // NO POINTER, NO NOTE, in both profiles (teamNote.ts::hasProbePointer):
      // pi's AGENTS.md without Probe's block is the researcher's opt-out.
      s.note.text = null;
      logLine("no Probe block in pi's AGENTS.md: skipping the team-note sync and injection");
    } else if (teamItemsHidden(process.env)) {
      // This computer's workspace limit hides team-wide items, the team note
      // among them (`probe workspace limit --team-items hide`): no sync and no
      // cached copy. `before_agent_start` asks the limit again on every prompt,
      // and once they show it syncs and reads the CURRENT account's note.
      s.note.text = null;
      s.note.hidden = true;
      logLine("the workspace limit hides team-wide items: skipping the team-note sync and injection");
    } else {
      s.note.hidden = false;
      await syncTeamNoteThenRead(realTeamNoteSyncDeps());
      s.note.text = readTeamNote(process.env);
    }
    // The machine-wide Probe service (core/service.ts): started now, detached
    // and never waited on, so it is up by the time capture registers this
    // session. Not under `off` (plan A2), and not where capture cannot start.
    // Its socket is asked for once per process, for the guard (`tool_call`).
    if (!probeIsOff(s)) {
      const runtime = resolveTapRuntime(tapRuntimeDeps());
      if (runtime) askServiceSocket(s, runtime);
      if (runtime && checkPairing(process.env).paired) {
        ensureService(runtime, { spawn: realSpawn, env: process.env, log: logLine });
      }
    }
    // Feedback for the Probe team the researcher said yes to and that was not
    // sent yet (offline, or the dialog's own send failed). Not under `off`.
    if (!probeIsOff(s) && feedbackUnsent(realApprovalsDeps())) spawnFeedbackFlush(realTeamNoteSyncDeps());

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

    if (!transcriptPath) {
      // In-memory / not-yet-persisted session (e.g. SessionManager.inMemory()) — no
      // JSONL file exists yet for the daemon to tail. Nothing to do.
      logLine(`session_start(${event.reason}): no session file for ${sessionId}, skipping`);
      return;
    }

    if (!fs.existsSync(transcriptPath)) {
      // A new session: pi writes its file at the first message. Capture starts
      // then (`startPendingCapture`), not never.
      s.capture.pending.set(sessionId, transcriptPath);
      logLine(`session_start(${event.reason}): ${sessionId}'s session file is not written yet; capture starts once pi writes it`);
      return;
    }
    await startCapture(ctx, sessionId, transcriptPath, `session_start(${event.reason})`);
  };

  const onSessionShutdown: EventHandler = async (event, ctx) => {
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
    s.capture.spawned.delete(sessionId);
    // A session the Probe service records (its `.hosted` marker): on a quit the
    // service finishes it now, waited on for a few seconds; then the marker goes,
    // so a resume of this session is recorded again (daemon.ts, audit #10).
    try {
      await finishHostedSession(
        sessionId,
        {
          // Resolved only if `tap finish` will run (a marker, a quit, a live service).
          runtime: () => resolveTapRuntime(tapRuntimeDeps()),
          waitS: event.reason === ShutdownReason.Quit ? HOSTED_FINISH_WAIT_S : 0,
        },
        { ...realDaemonDeps(), execFile: realTrackingExecFile },
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
  };

  // THE DAEMON PROFILE'S SKILLS. pi asks for extra resource paths right after
  // every session_start (startup, reload, new, resume, fork), by which point
  // the CLI has answered for this session; the agent profile adds nothing.
  const onResourcesDiscover: EventHandler = (_event, ctx) => {
    const id = ctx?.sessionManager?.getSessionId();
    if (!inDaemonProfile(s, id) || trackingOffFor(s, id)) return;
    return { skillPaths: daemonSkillPaths(resourceRoot) };
  };

  // Inject, don't render — see teamNote.ts's module docstring. Fires on
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
  const onBeforeAgentStart: EventHandler = async (event, ctx) => {
    // Not awaited: the prompt must not wait on a spawn.
    void startPendingCapture(ctx, "before_agent_start");
    const sessionId = ctx?.sessionManager?.getSessionId();
    // `probe session state inline` from a terminal moves the switch on disk
    // without a typed `/probe`: read it back, or a cached `off` would return
    // below before the agent hears it is inline.
    if (sessionId && ctx && s.probe.state !== ProbeState.Full && inDaemonProfile(s, sessionId) && sessionIsInline(sessionId)) {
      await refreshTrackingStatus(s, ctx, sessionId, ctx.cwd);
    }
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
    if (probeIsOff(s)) return;
    let systemPrompt = event.systemPrompt;
    // Asked again on every prompt (one small config read), not only at
    // session_start: `--team-items hide` set mid-session stops the very next
    // injection. Shown again after being hidden, the note is synced and re-read
    // first (bounded, as at session_start): whichever account is signed in NOW
    // is whose note this session gets, never a copy cached before.
    const hiddenNow = teamItemsHidden(process.env);
    if (!hiddenNow && s.note.hidden && hasProbePointer(process.env)) {
      await syncTeamNoteThenRead(realTeamNoteSyncDeps());
      s.note.text = readTeamNote(process.env);
    }
    s.note.hidden = hiddenNow;
    if (s.note.text && !hiddenNow) {
      systemPrompt += renderTeamNoteForPrompt(s.note.text, teamNoteDocumentPath(process.env));
    }
    if (s.probe.configContext) systemPrompt += `\n\n${s.probe.configContext}`;
    const inDaemon = s.probe.state === ProbeState.Daemon;
    let notice: string | null = null;
    if (daemonProfile) {
      if (inDaemon && sessionId && ctx) tellResearcherAboutDaemon(s, ctx, sessionId);
      // `/probe inline` (typed, or from a terminal): the agent hears it once per
      // stretch, and again after a compaction or resume (`forgetInlineShown`).
      const since = sessionId ? inlineNoticeDue(sessionId) : null;
      if (sessionId && since !== null) {
        const cli = await localCliVersion({ execFile: realTrackingExecFile, existsSync: fs.existsSync, isExecutable, env: process.env });
        notice = inlineNotice(resourceRoot, cliHasInlineBridge(cli), sessionId);
        // The skills it records by, whole, as the daemon's writer reads them.
        const skills = sessionInlineReadOnly(sessionId) ? "" : inlineSkillsText(resourceRoot);
        if (skills) notice += `\n\n${skills}`;
        markInlineShown(sessionId, since);
      }
    } else {
      // Only claim the daemon records while it DOES: its lease is live, or this
      // is the first turn and it has a key to start with. Otherwise daemonNotice
      // below tells the model recording is back with it.
      if (inDaemon) {
        const deps = realDaemonNoticeDeps();
        const live = sessionId ? daemonStatus(sessionId, deps).status === DaemonStatus.Live : false;
        if (live || (!s.probe.lastTurnInDaemon && companionKeyHeld(deps))) {
          systemPrompt += `\n\n${DAEMON_CONTEXT}`;
        }
        // The lease goes live (or lapses) between refreshes: redraw the footer.
        if (sessionId && ctx?.hasUI) {
          try {
            ctx.ui.setStatus("probe-tracking", footerText(s, sessionId, live));
          } catch (err) {
            logLine(`tracking footer redraw failed: ${err instanceof Error ? err.message : String(err)}`);
          }
        }
      }
      if ((inDaemon || s.probe.lastTurnInDaemon) && sessionId) {
        notice = daemonNotice(sessionId, inDaemon, realDaemonNoticeDeps());
      }
      s.probe.lastTurnInDaemon = inDaemon;
    }
    // The daemon's held questions, before the turn starts: pi's own dialog,
    // the researcher's pick written as the Board's answer (approvals.ts).
    if (sessionId) await askHeldQuestions(ctx, sessionId, true);
    // Daemon reads: a new turn, every waiting answer and this turn's one
    // unasked message (reads.ts), beside the daemon notice.
    if (sessionId) {
      try {
        const box = mailboxFor(sessionId);
        const served = box.served();
        if (served || heldQuestionsBoardExists()) startReadsPoller(ctx);
        if (served) {
          box.touchAlive("before_agent_start");
          const turn = box.wasWoken() ? box.currentTurn() : box.newTurn();
          const texts = box.deliver(turn, "before_agent_start");
          if (texts.length) {
            notice = [notice, ...texts].filter(Boolean).join("\n\n");
            box.markShown(box.lastClaimed, "before_agent_start"); // returned to pi as this turn's message
            void box.reportDelivered(box.lastClaimed); // Probe hears it was shown (inbox only; never awaited)
          }
          readsNotify(ctx, box.researcherNotice());
        }
      } catch (err) {
        logLine(`reads at prompt: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
    if (s.probe.parentAsk) {
      notice = [notice, s.probe.parentAsk].filter(Boolean).join("\n\n");
      s.probe.parentAsk = null;
    }
    if (systemPrompt === event.systemPrompt && !notice) return;
    return {
      ...(systemPrompt !== event.systemPrompt ? { systemPrompt } : {}),
      ...(notice ? { message: { customType: "probe-daemon", content: notice, display: false } } : {}),
    };
  };

  // THE GUARD: refused before the tool runs. In both profiles a write aimed at
  // the daemon's question folder; in the daemon profile also every `probe`
  // command but the agent's own and every Probe MCP call. Fires for calls a
  // codemode script makes too, so an inactive tool cannot slip by.
  //
  // THE SERVICE FIRST (plan D38, serviceGuard.ts): the call goes to the Probe
  // service as Claude Code's PreToolUse payload, and the plugin folder's own
  // pre-tool routes (the Python guard Claude Code runs) answer within 300 ms;
  // a refusal of theirs is the answer, in their words. guard.ts runs on every
  // call all the same, as the floor: the Python guard fails OPEN (a route that
  // broke exits 0 with nothing to say, which reads as "allowed"), so its allow
  // never overrides a refusal of guard.ts's, whose rules are a port of the
  // same ones. No service, an older one, out of time, an unclear answer: guard.ts
  // decides alone, exactly as before. Not under `off`: such a session never
  // reaches the service (A2).
  const onToolCall: EventHandler = async (event, ctx) => {
    // The session file exists by the first tool call. Not awaited: a tool must
    // not wait on a spawn.
    void startPendingCapture(ctx, "tool_call");
    try {
      const sessionId = ctx?.sessionManager?.getSessionId();
      const daemonProfile = inDaemonProfile(s, sessionId);
      const cwd = ctx?.cwd ?? process.cwd();
      let own: string | null = null;
      try {
        own = guardToolCall(event.toolName, event.input, {
          daemonProfile,
          inline: daemonProfile && sessionIsInline(sessionId),
          inlineReadOnly: sessionInlineReadOnly(sessionId),
          env: process.env,
          cwd,
        });
      } catch (err) {
        // A guard bug must never take the session's tools down with it.
        logLine(`guard failed open: ${err instanceof Error ? err.message : String(err)}`);
      }
      if (!s.service.asked && !probeIsOff(s)) {
        // A session that started `off` asked for no socket at session_start.
        const runtime = resolveTapRuntime(tapRuntimeDeps());
        if (runtime) askServiceSocket(s, runtime);
      }
      const answered = probeIsOff(s)
        ? null
        : await guardViaService(
            {
              toolName: event.toolName,
              input: event.input,
              toolCallId: event.toolCallId,
              sessionId,
              transcriptPath: ctx?.sessionManager?.getSessionFile?.(),
              cwd,
              daemonProfile,
              extensionDir: resourceRoot,
              env: process.env,
              socket: s.service.socket,
            },
            { clientVersion: PACKAGE_VERSION },
          );
      const reason = answered?.reason ?? own;
      if (reason === null) return;
      const by = answered?.reason ? " (the service's routes)" : "";
      logLine(`guard refused ${event.toolName}${by}: ${reason.slice(0, 120)}`);
      return { block: true, reason };
    } catch (err) {
      // A guard bug must never take the session's tools down with it.
      logLine(`guard failed open: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
  };

  // agent_settled, NOT turn_end — agent_settled is pi's analogue of Claude
  // Code's Stop (fires once an agent run has fully settled, no automatic
  // retry/compaction/continuation pending); turn_end fires several times per
  // user message and would push/pull far more than needed. See teamNote.ts's
  // module docstring for why this is a full sync, detached, and fail-open.
  const onAgentSettled: EventHandler = async (_event, ctx) => {
    await startPendingCapture(ctx, "agent_settled");
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
      const footer = sid && ctx?.hasUI ? footerText(s, sid) : undefined;
      if (footer !== undefined) ctx.ui.setStatus("probe-tracking", footer);
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
    // Read per settle, not cached: one small file, and an opt-out made in the
    // wizard mid-session stops the next sync rather than the next session's.
    if (hasProbePointer(process.env) && !teamItemsHidden(process.env)) spawnTeamNoteSync(realTeamNoteSyncDeps());
  };

  // An inline agent that leaves a summary or a note owed is sent back, as the
  // plugin's Stop hook does (owed.ts). At `agent_before_settle`, NOT
  // agent_settled: pi fires that one after every run, a run the researcher
  // stopped (Esc) and a provider error included, and a new turn there would
  // restart an agent they just stopped. Here only a run that `completed` is
  // continued, by pi's own boundary result.
  const onAgentBeforeSettle: EventHandler = async (event, ctx) => {
    if (event?.outcome !== ActivityOutcome.Completed || probeIsOff(s)) return;
    try {
      const sid = ctx?.sessionManager?.getSessionId();
      if (!sid || !inDaemonProfile(s, sid) || !sessionIsInline(sid) || !agentOwes(sid)) return;
      const text = await owedSendBack(sid, { execFile: realTrackingExecFile, existsSync: fs.existsSync, isExecutable, env: process.env });
      if (!text) return;
      return { entries: [{ type: "custom_message", customType: "probe-owed", content: text, display: true }], continue: true };
    } catch (err) {
      logLine(`owed before settle: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  // A compaction took the inline notice out of the agent's context: the next
  // prompt tells it again (`before_agent_start`).
  const onSessionCompact: EventHandler = async (_event, ctx) => {
    const sessionId = ctx?.sessionManager?.getSessionId();
    if (sessionId) forgetInlineShown(sessionId);
  };

  // THE TRACKING SWITCH. `input` fires on the RAW line, before pi expands
  // `/skill:<name>` — which is what lets a `/track-work` typed out of Claude
  // Code habit be rewritten to pi's own spelling instead of reaching the model
  // as prose pi ignored. See trackingSwitch.ts for why this is so much smaller
  // than the Python guard it brings to parity.
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
      if (sessionId) {
        applied = await applyTrackingSwitch(intent.direction, sessionId, {
          spawn: realSwitchSpawn,
          existsSync: fs.existsSync,
          isExecutable,
          env: process.env,
          log: logLine,
          agentSessionEnv: agentSessionEnv(),
        });
        if (applied) {
          // Read back through the same atomic host bridge instead of deriving
          // the result from the requested direction (especially `toggle`).
          await refreshTrackingStatus(s, ctx, sessionId, ctx.cwd);
        }
      } else {
        logLine("tracking switch skipped: no session id");
      }
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
        await refreshTrackingStatus(s, ctx, sessionId, ctx.cwd);
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
        announce(ctx, "The Probe daemon reads for this session, so Probe's MCP tools stay off. Who records is set in probe wizard.", "info");
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
    },
    status() {
      const id = s.current.sessionId;
      return { recorded: id !== null && hostedServicePid(id) !== null };
    },
  };
}
