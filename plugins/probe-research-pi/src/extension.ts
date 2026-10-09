/**
 * Wiring layer: registers the session_start / session_shutdown handlers and
 * the /probe-status command against a real pi ExtensionAPI. Everything with
 * actual logic (pairing, runtime resolution, spawning, stopping) lives in
 * sibling modules and is unit-tested directly with injected fakes; this file
 * is the thin, mostly-untested seam that binds them to real fs/child_process
 * and to pi's event bus. `index.ts` calls `registerExtension(pi, __dirname)`.
 */

import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import * as fs from "node:fs";
import { constants as fsConstants } from "node:fs";
import { dirname, join } from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { detectAdapterHandoff, MCP_SERVED_VIA_ADAPTER_MESSAGE } from "./adapterHandoff.js";
import { QuestionAsker, type ApprovalsDeps } from "./core/approvals.js";
import { guardToolCall, sessionIsInline } from "./guard.js";
import {
  cliHasInlineBridge,
  forgetInlineShown,
  inlineNotice,
  inlineNoticeDue,
  localCliVersion,
  markInlineShown,
} from "./inlineNotice.js";
import { pruneStaleShutdownSentinels, spawnDaemon, stopDaemon, waitForSpawnConfirmation, type DaemonDeps, type SpawnFn } from "./core/daemon.js";
import { connectAndRegisterTools, defaultMcpBridgeDeps, interactiveOAuthLogin, type ConnectResult } from "./mcpBridge.js";
import { approvalsDir, disabledFile, extensionLogFile, probeStateDir, teamNoteDocumentPath } from "./core/paths.js";
import { Mailbox } from "./core/reads.js";
import { checkPairing } from "./core/pairing.js";
import { buildStatusReport } from "./status.js";
import { resolveTapRuntime, type TapRuntimeDeps } from "./core/tapRuntime.js";
import { applyTrackingSwitch, parseSwitchIntent, switchAppliedNotice, type SwitchChild, type SwitchSpawnFn } from "./core/trackingSwitch.js";
import { writeTurnSignal, type TurnSignalDeps } from "./core/turnSignal.js";
import {
  companionKeyHeld,
  DAEMON_CONTEXT,
  daemonNotice,
  daemonStatus,
  DaemonStatus,
  type DaemonNoticeDeps,
  type DaemonStatusValue,
} from "./core/daemonNotice.js";
import { daemonSkillDir, isProbeMcpTool, leanSkills, readDaemonSkill, researcherDaemonNotice, withoutPackageSkills } from "./profile.js";
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

// Module-scope, NOT inside registerExtension: pi tears down and rebuilds the
// extension runtime on /reload and on session switches (new/resume/fork),
// re-invoking this module's exports each time — but Node keeps this file's
// top-level state alive across those re-invocations within one process
// (module cache keyed by resolved path). That's what makes the same-session
// double-spawn guard, and knowing which session's daemon to stop in the
// session_shutdown handler, survive a reload instead of resetting on every
// one. The real dedup authority is still the pidfile check inside
// spawnDaemon()/isDaemonAlive() — this set is a same-process fast path that
// also closes the narrow race where session_start could fire twice before a
// freshly-spawned wrapper has written its own pidfile.
const spawnedSessionIds = new Set<string>();

// The team-note cache. Module-scope for the same reason spawnedSessionIds is:
// pi rebuilds the extension runtime on /reload and session switches, and this
// must survive that (a reload should not blank out the note mid-session).
// Populated ONCE per real session_start by readTeamNote() below, read by
// EVERY before_agent_start of that session, and never refreshed mid-session —
// see teamNote.ts's module docstring ("cache once per session") for why.
let cachedTeamNote: string | null = null;

/**
 * The three-valued switch for this session, as the CLI last reported it.
 *
 * `undefined` means unknown -- an older probe that does not send `state`, or a
 * read that failed -- and unknown behaves exactly as it did before this existed.
 * Only a confirmed `off` suppresses anything, because suppressing on a failed
 * read would silently stop the team note syncing on a machine that never asked
 * for it, and a note that quietly stops sending is the one failure that surface
 * must never have.
 */
let cachedProbeState: ProbeStateValue | undefined;

/** The last tracking reading, so a turn in `daemon` can redraw the footer
 * when the daemon's lease changes without spawning the CLI again. */
let cachedTracking: { tracking: boolean; capture?: CaptureReading } | undefined;

/**
 * Who records, as the CLI last reported it, and for WHICH session.
 *
 * Read by session id because this module's state outlives a session switch:
 * a resumed or new session must never inherit the previous one's profile. A
 * session the CLI has not answered for is the agent's (`profileFor`), which
 * is today's extension; a failed re-read later in a session keeps the answer
 * it already had, so one slow CLI call cannot flip a session between profiles.
 */
let cachedProfile: { sessionId: string; profile: RecorderValue } | undefined;

function profileFor(sessionId: string | undefined): RecorderValue {
  return sessionId !== undefined && cachedProfile?.sessionId === sessionId ? cachedProfile.profile : Recorder.Agent;
}

function inDaemonProfile(sessionId: string | undefined): boolean {
  return profileFor(sessionId) === Recorder.Daemon;
}

/**
 * Probe's tracking switched off on our settings entry (`trackingSetting.ts`),
 * read at each session_start straight from pi's settings files. Keyed by
 * session like `cachedProfile`, so a new or resumed session never inherits it.
 */
let cachedTrackingOff: { sessionId: string; off: boolean } | undefined;

function trackingOffFor(sessionId: string | undefined): boolean {
  return sessionId !== undefined && cachedTrackingOff?.sessionId === sessionId && cachedTrackingOff.off;
}

/**
 * The daemon's health as the researcher last heard it, in the daemon profile:
 * `status` null until the first notice-worthy reading, `prompts` for the
 * not-started grace. See `profile.ts::researcherDaemonNotice`.
 */
let researcherDaemonHealth: { sessionId: string; status: DaemonStatusValue | null; prompts: number } | undefined;

/** The footer for this session: in `daemon`, it reads the lease file. */
function footerText(sessionId: string, daemonLive?: boolean): string | undefined {
  if (!cachedTracking) return undefined;
  const live =
    cachedProbeState !== ProbeState.Daemon
      ? undefined
      : (daemonLive ?? daemonStatus(sessionId, realDaemonNoticeDeps()).status === DaemonStatus.Live);
  // `on (inline)`: the researcher set Probe inline (`/probe inline`).
  const inline = inDaemonProfile(sessionId) && sessionIsInline(sessionId);
  return trackingStatusText(cachedTracking.tracking, cachedTracking.capture, live, inline);
}

/** Has the researcher switched Probe off for this session? */
function probeIsOff(): boolean {
  return cachedProbeState === ProbeState.Off;
}

/**
 * Was the last turn in the `daemon` state? The daemon notice reads two small
 * files, so a session that has never been in the state skips it entirely; a
 * session that just LEFT it runs it once more to clear the notified record.
 */
let lastTurnInDaemon = false;

// `.probe.config` (paths Probe never records), from `session initialize`:
// the line that goes into the prompt while Probe records, and the question
// about files above the launch folder, delivered once with the next turn.
let cachedProbeConfigContext: string | null = null;
let pendingParentAsk: string | null = null;

// DAEMON READS (reads.ts): the daemon's reader answers `probe ask` and sends
// team context; pi gets it at each prompt and, between prompts, from a 2 s
// poller that steers an answer into a running agent or starts a turn for one
// that lands while pi is idle (pi's analogue of Claude Code's Stop wake). The
// same poller asks the daemon's held questions (approvals.ts).
const READS_POLL_MS = 2000;
let readsCtx: { ctx: any; timer: ReturnType<typeof setInterval> } | null = null;

// The daemon's held questions: one dialog at a time, each asked once per
// process (Esc: again at the next prompt). Module scope for the same reason as
// spawnedSessionIds: a /reload must not ask a question a second time.
const asker = new QuestionAsker();

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
  };
}

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

/** Start the reads poller for this process, once a session the reader serves
 * (or on a machine where the daemon holds questions) has had its first prompt
 * (`before_agent_start`). */
function startReadsPoller(pi: ExtensionAPI, ctx: any): void {
  if (readsCtx) {
    readsCtx.ctx = ctx;
    return;
  }
  const timer = setInterval(() => {
    try {
      if (probeIsOff() || !readsCtx) return;
      const live = readsCtx.ctx;
      const sid = live?.sessionManager?.getSessionId();
      if (!sid) return;
      // Not awaited: a dialog stays open across ticks, and the asker opens
      // one at a time.
      void askHeldQuestions(live, sid, false);
      const box = mailboxFor(sid);
      if (!box.served()) return;
      const idle = typeof live.isIdle === "function" ? live.isIdle() : true;
      // Idle: answers only, and they start a turn. Running: one unasked
      // message per turn too, steered in after the current tool calls.
      const texts = box.deliver(box.currentTurn(), "pi poller", !idle);
      if (texts.length) {
        if (idle) box.markWoke(); // the turn this starts is the same turn going on
        pi.sendMessage(
          { customType: "probe-reads", content: texts.join("\n\n"), display: true },
          { deliverAs: "steer", triggerTurn: true },
        );
      }
      readsNotify(live, box.researcherNotice());
    } catch (err) {
      logLine(`reads poller: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, READS_POLL_MS);
  timer.unref?.();
  readsCtx = { ctx, timer };
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

// Live Probe MCP connections, keyed by session id. Module-scope for the same
// reload-survives-reasoning as spawnedSessionIds/cachedTeamNote — but unlike
// those two, THIS one is read as well as written on every session_start: pi
// rebuilds the extension's tool registry from scratch on every reload
// (verified against loader.js — a fresh `extension.tools` Map per load), so
// a session_start firing with a sessionId already in this map means
// "re-register the tools we already fetched against the new registry," not
// "skip, already done" — otherwise a session's Probe tools would silently
// disappear after /reload. A session ending removes its own entry.
const mcpConnections = new Map<string, Extract<ConnectResult, { registered: number }>>();

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

async function refreshTrackingStatus(
  ctx: { hasUI: boolean; ui: { setStatus: (key: string, value: string | undefined) => void } },
  sessionId: string,
  cwd: string,
): Promise<boolean> {
  const state = await initializeTrackingState(sessionId, cwd, {
    execFile: realTrackingExecFile,
    existsSync: fs.existsSync,
    isExecutable,
    env: process.env,
    log: logLine,
  });
  if (!state) {
    // Unknown, not the last value seen: see cachedProbeState's docstring.
    cachedProbeState = undefined;
    cachedTracking = undefined;
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
  cachedProbeState = state.state;
  cachedTracking = { tracking: state.tracking, capture: state.capture };
  cachedProbeConfigContext = state.probeConfig?.context ?? null;
  if (state.probeConfig?.ask) pendingParentAsk = state.probeConfig.ask;
  cachedProfile = { sessionId, profile: state.profile };
  if (ctx.hasUI) {
    try {
      ctx.ui.setStatus("probe-tracking", footerText(sessionId));
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
function deactivateProbeTools(pi: ExtensionAPI): void {
  try {
    const active = pi.getActiveTools();
    const kept = active.filter((name) => !isProbeMcpTool(name));
    if (kept.length !== active.length) {
      pi.setActiveTools(kept);
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
  ctx: { hasUI: boolean; ui: { notify: (msg: string, level?: "info" | "warning" | "error") => void; setStatus: (key: string, value: string | undefined) => void } },
  sessionId: string,
): void {
  const deps = realDaemonNoticeDeps();
  const { status, reason } = daemonStatus(sessionId, deps);
  const health =
    researcherDaemonHealth?.sessionId === sessionId ? researcherDaemonHealth : { sessionId, status: null, prompts: 0 };
  const { notice, next } = researcherDaemonNotice(health.status, status, reason, health.prompts === 0, () =>
    companionKeyHeld(deps),
  );
  researcherDaemonHealth = { sessionId, status: next, prompts: health.prompts + 1 };
  if (notice) logLine(notice);
  if (!ctx.hasUI) return;
  try {
    ctx.ui.setStatus("probe-tracking", footerText(sessionId, status === DaemonStatus.Live));
    if (notice) ctx.ui.notify(notice, "warning");
  } catch (err) {
    logLine(`daemon status notice failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

/**
 * Every coding agent's session variable, from the harness registry beside this
 * file (`harnesses.json`, synced from `probe.harness`): pi's switch spawns the
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

export function registerExtension(pi: ExtensionAPI, extensionDir: string): void {
  // Read once: the file ships with the package and only changes with it.
  const daemonSkill = readDaemonSkill(extensionDir);

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

  if (spawnedSessionIds.has(sessionId)) {
    return;
  }

  const pairing = checkPairing(process.env);
  if (!pairing.paired) {
    announce(ctx, pairing.reason, "warning");
    return;
  }

  const runtimeDeps: TapRuntimeDeps = {
    existsSync: fs.existsSync,
    isExecutable,
    env: process.env,
    extensionDir,
  };
  const runtime = resolveTapRuntime(runtimeDeps);
  if (!runtime) {
    announce(
      ctx,
      "no python3 interpreter found for the probe-research-tap daemon — capture disabled for this session. " +
        "Install Python 3.11+ and ensure `python3` is on PATH, or set PROBE_PI_TAP_ROOT.",
      "error",
    );
    return;
  }

  const deps = realDaemonDeps();
  const result = spawnDaemon({ sessionId, transcriptPath, cwd: ctx.cwd, runtime }, deps);
  // Marked SYNCHRONOUSLY, with no await between the spawn decision and this
  // line: spawnedSessionIds is the same-process fast path (see its own
  // comment above), and the whole point of it is to close before
  // waitForSpawnConfirmation's multi-second wait below even starts — a
  // second session_start racing in during that wait must see this set
  // already updated, not find the window still open.
  spawnedSessionIds.add(sessionId);
  if (result.spawned) {
    logLine(`${label}: spawned capture for ${sessionId} (pid ${result.pid ?? "unknown"})`);
    await waitForSpawnConfirmation(sessionId, deps);
  } else {
    logLine(`${label}: capture already running for ${sessionId}`);
  }

  // Read the footer back now that `tap start` has had its say. The refresh
  // at the top of this handler ran BEFORE the spawn, so its capture reading
  // is the one reading guaranteed to be stale — and a `tap start` that
  // refused (a gate this handler does not pre-check, an interpreter below
  // 3.11) would otherwise leave "● tracking" on screen for the rest of the
  // session with nothing capturing behind it.
  await refreshTrackingStatus(ctx, sessionId, ctx.cwd);  }

  /**
   * Sessions whose file pi had not written at session_start. pi 1.0 writes a
   * NEW session's file at its first message, after session_start: `tap start`
   * refuses a transcript that does not exist (EXIT_NO_TRANSCRIPT) and nothing
   * retried, so a new interactive session was never captured and the daemon
   * never started for it. Session id -> the file to wait for.
   */
  const pendingCapture = new Map<string, string>();

  /** Start a pending session's capture once its file exists. Never throws. */
  async function startPendingCapture(ctx: any, label: string): Promise<void> {
    try {
      const sessionId = ctx?.sessionManager?.getSessionId();
      const transcriptPath = sessionId ? pendingCapture.get(sessionId) : undefined;
      if (!sessionId || !transcriptPath || !fs.existsSync(transcriptPath)) return;
      pendingCapture.delete(sessionId);
      await startCapture(ctx, sessionId, transcriptPath, label);
    } catch (err) {
      logLine(`${label}: deferred capture start failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  pi.on("session_start", async (event, ctx) => {
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
    await refreshTrackingStatus(ctx, sessionId, ctx.cwd);
    // A resumed session's agent hears again that Probe is inline (`/probe inline`).
    if (sessionId && event.reason === "resume") forgetInlineShown(sessionId);

    if (probeIsOff()) {
      // `off` means no Probe calls and nothing injected. Leaving this out was
      // the difference between a switch and a promise: the note still synced
      // over the network every settle, and still rode into every prompt.
      cachedTeamNote = null;
      logLine("probe state is off: skipping the team-note sync and injection");
    } else if (!hasProbePointer(process.env)) {
      // NO POINTER, NO NOTE, in both profiles (teamNote.ts::hasProbePointer):
      // pi's AGENTS.md without Probe's block is the researcher's opt-out.
      cachedTeamNote = null;
      logLine("no Probe block in pi's AGENTS.md: skipping the team-note sync and injection");
    } else {
      await syncTeamNoteThenRead(realTeamNoteSyncDeps());
      cachedTeamNote = readTeamNote(process.env);
    }

    // Probe MCP read tools: entirely independent of the capture daemon below
    // (a separate credential — mcp_token, never the tap's ingest/device
    // token — and no transcript file is needed to register tools), so this
    // runs unconditionally, before the no-transcript-file early return and
    // before the killswitch/pairing checks that gate capture only. Bounded
    // internally (see mcpBridge.ts) so an unreachable server cannot stall
    // this handler; any failure degrades to a single announce(), never a
    // thrown error out of session_start.
    //
    // D6 stand-down check FIRST, before touching mcpConnections at all: when
    // pi-mcp-adapter already owns the Probe MCP server (see
    // adapterHandoff.ts), connecting our own bridge on top would register
    // every tool twice. Re-run on EVERY session_start (cheap: two local file
    // reads, no network) rather than caching — a `packages` entry can change
    // between sessions (an adapter or our own package installed/removed) and
    // this must track that without a restart. Gating the whole block —
    // including the reregister branch — behind `!standDown` is also what
    // keeps `/reload` well-behaved while stood down: mcpConnections never
    // gets an entry for this session in the first place, so there is no
    // stale connection for a later reload's `existingMcpConnection` check to
    // trip on.
    //
    // THE DAEMON PROFILE reads for the agent (`probe ask`), so no Probe MCP
    // tool is offered at all: the bridge stays off, a connection an earlier
    // agent-profile start of this session opened is closed, and an adapter's
    // tools are deactivated (see `deactivateProbeTools`).
    // TRACKING OFF (trackingSetting.ts) offers no Probe MCP tool either,
    // read from the settings files themselves so no CLI answer can undo it.
    if (sessionId) {
      cachedTrackingOff = {
        sessionId,
        off: trackingOff({ env: process.env, cwd: ctx.cwd, packageRoot: extensionDir }),
      };
    }
    const handoff = inDaemonProfile(sessionId) || trackingOffFor(sessionId)
      ? null
      : detectAdapterHandoff({ env: process.env, cwd: ctx.cwd, packageRoot: extensionDir });
    if (!handoff) {
      const stale = mcpConnections.get(sessionId);
      if (stale) {
        mcpConnections.delete(sessionId);
        stale.close().catch((err) => logLine(`session_start(${event.reason}): Probe MCP close failed: ${err instanceof Error ? err.message : String(err)}`));
      }
      deactivateProbeTools(pi);
      const why = trackingOffFor(sessionId) ? "Probe tracking is off" : "daemon profile";
      logLine(`session_start(${event.reason}): ${why}, no Probe MCP tools for ${sessionId}`);
    } else if (handoff.standDown) {
      logLine(`session_start(${event.reason}): ${MCP_SERVED_VIA_ADAPTER_MESSAGE} — ${handoff.reason}`);
    } else {
      const existingMcpConnection = mcpConnections.get(sessionId);
      if (existingMcpConnection) {
        // Same session, extension reloaded (or session_start fired again for
        // some other reason) — re-register against the fresh tool registry
        // with no network call. See mcpConnections' own comment above.
        const registered = existingMcpConnection.reregister(pi);
        logLine(`session_start(${event.reason}): Probe MCP re-registered ${registered} tool(s) for ${sessionId}`);
      } else {
        try {
          const result = await connectAndRegisterTools(pi, defaultMcpBridgeDeps(process.env, (message, level) => announce(ctx, message, level), logLine));
          if ("registered" in result) {
            mcpConnections.set(sessionId, result);
            logLine(`session_start(${event.reason}): Probe MCP ready, ${result.registered} tool(s) registered for ${sessionId}`);
          } else {
            logLine(`session_start(${event.reason}): Probe MCP unavailable for ${sessionId}: ${result.skipped}`);
          }
        } catch (err) {
          // connectAndRegisterTools degrades internally and should never throw
          // — this catch exists only so a bug there can never take capture or
          // the team note down with it.
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
      pendingCapture.set(sessionId, transcriptPath);
      logLine(`session_start(${event.reason}): ${sessionId}'s session file is not written yet; capture starts once pi writes it`);
      return;
    }
    await startCapture(ctx, sessionId, transcriptPath, `session_start(${event.reason})`);
  });

  pi.on("session_shutdown", async (event, ctx) => {
    if (readsCtx) {
      clearInterval(readsCtx.timer);
      readsCtx = null;
    }
    if (event.reason === "reload") {
      // Extensions are reloading, not the session — the SAME session id fires
      // session_start again right after this. Leave the daemon running:
      // stopping it here would spuriously FINALIZE a session that isn't
      // actually ending (main.py's shutdown path always enqueues a FINALIZE,
      // which is what triggers server-side knowledge-unit extraction). Leave
      // the Probe MCP connection open for the same reason — the imminent
      // session_start will find it in mcpConnections and re-register against
      // the fresh registry rather than reconnecting.
      return;
    }
    const sessionId = ctx.sessionManager.getSessionId();
    stopDaemon(sessionId, realDaemonDeps());
    spawnedSessionIds.delete(sessionId);
    const mcpConnection = mcpConnections.get(sessionId);
    if (mcpConnection) {
      mcpConnections.delete(sessionId);
      // Best-effort: closing the MCP client (an SSE stream + HTTP session)
      // must never block or fail this handler — a hung close() would stall
      // shutdown, and a failed one is inert (the process is ending anyway).
      mcpConnection.close().catch((err) => logLine(`session_shutdown(${event.reason}): Probe MCP close failed: ${err instanceof Error ? err.message : String(err)}`));
    }
    logLine(`session_shutdown(${event.reason}): stopped capture for ${sessionId}`);
  });

  // THE DAEMON PROFILE'S SKILL. pi asks for extra resource paths right after
  // every session_start (startup, reload, new, resume, fork), by which point
  // the CLI has answered for this session; the agent profile adds nothing.
  pi.on("resources_discover", (_event, ctx) => {
    const id = ctx?.sessionManager?.getSessionId();
    if (!inDaemonProfile(id) || trackingOffFor(id)) return;
    return { skillPaths: [daemonSkillDir(extensionDir)] };
  });

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
  pi.on("before_agent_start", async (event, ctx) => {
    // Not awaited: the prompt must not wait on a spawn.
    void startPendingCapture(ctx, "before_agent_start");
    const sessionId = ctx?.sessionManager?.getSessionId();
    // `probe session state inline` from a terminal moves the switch on disk
    // without a typed `/probe`: read it back, or a cached `off` would return
    // below before the agent hears it is inline.
    if (sessionId && ctx && cachedProbeState !== ProbeState.Full && inDaemonProfile(sessionId) && sessionIsInline(sessionId)) {
      await refreshTrackingStatus(ctx, sessionId, ctx.cwd);
    }
    if (trackingOffFor(sessionId)) {
      // Capture already started above; tracking adds nothing: no package
      // skill, no Probe MCP tool, nothing injected into the prompt.
      if (event.systemPromptOptions?.skills) {
        event.systemPromptOptions.skills = withoutPackageSkills(event.systemPromptOptions.skills);
      }
      deactivateProbeTools(pi);
      return;
    }
    const daemonProfile = inDaemonProfile(sessionId);
    if (daemonProfile) {
      // In every state, `off` included: the profile is the machine's choice of
      // recorder, the state is this session's. Trimmed BEFORE the prompt is
      // read below: `event.systemPrompt` renders from these options.
      if (event.systemPromptOptions?.skills) {
        event.systemPromptOptions.skills = leanSkills(event.systemPromptOptions.skills, daemonSkill);
      }
      deactivateProbeTools(pi);
    }
    if (probeIsOff()) return;
    let systemPrompt = event.systemPrompt;
    if (cachedTeamNote) {
      systemPrompt += renderTeamNoteForPrompt(cachedTeamNote, teamNoteDocumentPath(process.env));
    }
    if (cachedProbeConfigContext) systemPrompt += `\n\n${cachedProbeConfigContext}`;
    const inDaemon = cachedProbeState === ProbeState.Daemon;
    let notice: string | null = null;
    if (daemonProfile) {
      if (inDaemon && sessionId && ctx) tellResearcherAboutDaemon(ctx, sessionId);
      // `/probe inline` (typed, or from a terminal): the agent hears it once per
      // stretch, and again after a compaction or resume (`forgetInlineShown`).
      const since = sessionId ? inlineNoticeDue(sessionId) : null;
      if (sessionId && since !== null) {
        const cli = await localCliVersion({ execFile: realTrackingExecFile, existsSync: fs.existsSync, isExecutable, env: process.env });
        notice = inlineNotice(extensionDir, cliHasInlineBridge(cli));
        markInlineShown(sessionId, since);
      }
    } else {
      // Only claim the daemon records while it DOES: its lease is live, or this
      // is the first turn and it has a key to start with. Otherwise daemonNotice
      // below tells the model recording is back with it.
      if (inDaemon) {
        const deps = realDaemonNoticeDeps();
        const live = sessionId ? daemonStatus(sessionId, deps).status === DaemonStatus.Live : false;
        if (live || (!lastTurnInDaemon && companionKeyHeld(deps))) {
          systemPrompt += `\n\n${DAEMON_CONTEXT}`;
        }
        // The lease goes live (or lapses) between refreshes: redraw the footer.
        if (sessionId && ctx?.hasUI) {
          try {
            ctx.ui.setStatus("probe-tracking", footerText(sessionId, live));
          } catch (err) {
            logLine(`tracking footer redraw failed: ${err instanceof Error ? err.message : String(err)}`);
          }
        }
      }
      if ((inDaemon || lastTurnInDaemon) && sessionId) {
        notice = daemonNotice(sessionId, inDaemon, realDaemonNoticeDeps());
      }
      lastTurnInDaemon = inDaemon;
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
        if (served || heldQuestionsBoardExists()) startReadsPoller(pi, ctx);
        if (served) {
          const turn = box.wasWoken() ? box.currentTurn() : box.newTurn();
          const texts = box.deliver(turn, "before_agent_start");
          if (texts.length) notice = [notice, ...texts].filter(Boolean).join("\n\n");
          readsNotify(ctx, box.researcherNotice());
        }
      } catch (err) {
        logLine(`reads at prompt: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
    if (pendingParentAsk) {
      notice = [notice, pendingParentAsk].filter(Boolean).join("\n\n");
      pendingParentAsk = null;
    }
    if (systemPrompt === event.systemPrompt && !notice) return;
    return {
      ...(systemPrompt !== event.systemPrompt ? { systemPrompt } : {}),
      ...(notice ? { message: { customType: "probe-daemon", content: notice, display: false } } : {}),
    };
  });

  // THE GUARD: refused before the tool runs (guard.ts). In both profiles a
  // write aimed at the daemon's question folder; in the daemon profile also
  // every `probe` command but the agent's own and every Probe MCP call. Fires
  // for calls a codemode script makes too, so an inactive tool cannot slip by.
  pi.on("tool_call", async (event, ctx) => {
    // The session file exists by the first tool call. Not awaited: a tool must
    // not wait on a spawn.
    void startPendingCapture(ctx, "tool_call");
    try {
      const sessionId = ctx?.sessionManager?.getSessionId();
      const daemonProfile = inDaemonProfile(sessionId);
      const reason = guardToolCall(event.toolName, event.input, {
        daemonProfile,
        inline: daemonProfile && sessionIsInline(sessionId),
        env: process.env,
        cwd: ctx?.cwd ?? process.cwd(),
      });
      if (reason === null) return;
      logLine(`guard refused ${event.toolName}: ${reason.slice(0, 120)}`);
      return { block: true, reason };
    } catch (err) {
      // A guard bug must never take the session's tools down with it.
      logLine(`guard failed open: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
  });

  // agent_settled, NOT turn_end — agent_settled is pi's analogue of Claude
  // Code's Stop (fires once an agent run has fully settled, no automatic
  // retry/compaction/continuation pending); turn_end fires several times per
  // user message and would push/pull far more than needed. See teamNote.ts's
  // module docstring for why this is a full sync, detached, and fail-open.
  pi.on("agent_settled", async (_event, ctx) => {
    await startPendingCapture(ctx, "agent_settled");
    if (probeIsOff()) return;
    // The reads poller starts at a prompt only once the daemon serves this
    // session. A daemon that came up mid-turn (a new session's capture starts
    // at its first tool call) would otherwise leave an asked answer waiting
    // for the researcher's next prompt instead of arriving while pi is idle.
    try {
      const sid = ctx?.sessionManager?.getSessionId();
      if (sid && (mailboxFor(sid).served() || heldQuestionsBoardExists())) startReadsPoller(pi, ctx);
      // Redraw the footer: it was drawn at the prompt, before a daemon that
      // starts with the session's first tool call could go live, and a turn
      // the daemon's answer starts has no prompt to redraw it.
      const footer = sid && ctx?.hasUI ? footerText(sid) : undefined;
      if (footer !== undefined) ctx.ui.setStatus("probe-tracking", footer);
    } catch (err) {
      logLine(`reads at settle: ${err instanceof Error ? err.message : String(err)}`);
    }
    // The daemon's turn signal (turnSignal.ts), before the note sync: it is two
    // small file writes, and only in `daemon`.
    if (cachedProbeState === ProbeState.Daemon) {
      try {
        writeTurnSignal(ctx.sessionManager.getSessionId(), ctx.sessionManager.getSessionFile(), realTurnSignalDeps());
      } catch {
        // No session manager on this event: the worker keeps its quiet timer.
      }
    }
    // Read per settle, not cached: one small file, and an opt-out made in the
    // wizard mid-session stops the next sync rather than the next session's.
    if (hasProbePointer(process.env)) spawnTeamNoteSync(realTeamNoteSyncDeps());
  });

  // A compaction took the inline notice out of the agent's context: the next
  // prompt tells it again (`before_agent_start`).
  pi.on("session_compact", async (_event, ctx) => {
    const sessionId = ctx?.sessionManager?.getSessionId();
    if (sessionId) forgetInlineShown(sessionId);
  });

  // THE TRACKING SWITCH. `input` fires on the RAW line, before pi expands
  // `/skill:<name>` — which is what lets a `/track-work` typed out of Claude
  // Code habit be rewritten to pi's own spelling instead of reaching the model
  // as prose pi ignored. See trackingSwitch.ts for why this is so much smaller
  // than the Python guard it brings to parity.
  pi.on("input", async (event, ctx) => {
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
          await refreshTrackingStatus(ctx, sessionId, ctx.cwd);
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
  });

  pi.registerCommand("probe-status", {
    description: "Show Probe Research session-capture status",
    handler: async (_args, ctx) => {
      const sessionId = ctx.sessionManager.getSessionId();
      const report = buildStatusReport(sessionId, {
        existsSync: fs.existsSync,
        isExecutable,
        readFileSync: (path) => fs.readFileSync(path, "utf-8"),
        kill: (pid, signal) => process.kill(pid, signal),
        env: process.env,
        extensionDir,
        cwd: ctx.cwd,
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
  });

  pi.registerCommand("probe-mcp-login", {
    description: "Connect Probe Research's read MCP tools — bearer token first, interactive OAuth login otherwise",
    handler: async (_args, ctx) => {
      const sessionId = ctx.sessionManager.getSessionId();
      if (inDaemonProfile(sessionId)) {
        // The guard refuses every Probe MCP call in this profile, so tools
        // registered here would only be offered to be refused.
        announce(ctx, "The Probe daemon reads for this session, so Probe's MCP tools stay off. Who records is set in probe wizard.", "info");
        return;
      }
      const existing = mcpConnections.get(sessionId);
      if (existing) {
        // Already connected this session (session_start's own attempt, or an
        // earlier /probe-mcp-login) — report that rather than opening a
        // SECOND, untracked connection interactiveOAuthLogin() would leak
        // (it always calls connectAndRegisterTools() fresh; that path has no
        // reason to know about this session's cached entry).
        const message = `Probe MCP is already connected — ${existing.registered} tool(s) registered.`;
        logLine(`probe-mcp-login(connected): ${message}`);
        announce(ctx, message, "info");
        return;
      }
      // Known, accepted gap: a FRESH sign-in done here registers tools
      // straight against `pi` for this session immediately, but is not
      // cached in mcpConnections (interactiveOAuthLogin() does not hand back
      // a close()/reregister() pair), so a later /reload reconnects once
      // more over the network (via the freshly-saved OAuth tokens) instead
      // of a free in-memory re-register — a minor latency cost, not a
      // correctness one, and the existing-connection check above at least
      // stops a second `/probe-mcp-login` in the same session from doubling
      // it.
      const result = await interactiveOAuthLogin(
        pi,
        defaultMcpBridgeDeps(process.env, (message, level) => announce(ctx, message, level), logLine),
        { hasUI: ctx.hasUI, input: (title, placeholder) => ctx.ui.input(title, placeholder) },
      );
      logLine(`probe-mcp-login(${result.status}): ${result.message}`);
      const level = result.status === "failed" ? "error" : result.status === "requires-interactive" ? "warning" : "info";
      announce(ctx, result.message, level);
    },
  });
}
