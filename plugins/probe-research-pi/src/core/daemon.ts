/**
 * Starts and stops capture for one pi session.
 *
 * THIS FILE USED TO OWN THE PROCESS LIFECYCLE and no longer does. It carried
 * its own POSIX-sh crash-recovery wrapper — a TypeScript string that was a
 * hand-port of the bash string in
 * `agent/plugins/probe-research-tap/hooks/session-start.sh` — plus the pid
 * file, the shutdown-sentinel clear, and nothing else that decided whether
 * capture should start at all. Two copies of a process-lifecycle contract is
 * where lifecycle bugs live, so both were replaced by one spawner,
 * `python -m tap start` (`probe-research-tap/tap/start.py`), which also holds
 * the gates (killswitch, disabled paths, pairing, transcript, interpreter
 * version) that each caller would otherwise re-implement and drift on.
 *
 * What is left here is exactly what is pi's and nobody else's:
 *
 *  - WHICH INTERPRETER and WHICH ENV. Claude Code and Codex ship `tap/`
 *    bundled beside the hook that spawns it; a pi extension is pure
 *    TypeScript and has to go find a Python that can `import tap` (see
 *    tapRuntime.ts), then hand it a PYTHONPATH.
 *  - THE DETACH. Node's `spawn(..., {detached: true})` puts the child in its
 *    own session/process group on POSIX (libuv calls setsid() internally),
 *    which is the property session-start.sh needed a nohup+python shim to
 *    get. Nothing here reproduces that shim.
 *  - THE PRE-CHECK, THE WAIT AND THE STOP. `isWatcherAlive()` (which must
 *    agree with the spawner — see its own comment),
 *    `waitForSpawnConfirmation()`'s post-spawn diagnostic, `stopDaemon()`'s
 *    session_shutdown analog of session-end.sh, and the sentinel pruning.
 *    All four read and write the same `/tmp` files `tap start` does.
 *  - A SESSION THE PROBE SERVICE RECORDS (plan A2): `tap start` asks the
 *    service first, and when it takes the session there is no wrapper and no
 *    pid file, only a `.hosted` marker naming the service (`hosted.ts`). The
 *    readers here accept that marker, and `finishHostedSession()` is the
 *    `tap finish` half of session-end.sh.
 */

import { execFileSync } from "node:child_process";
import { delimiter, join } from "node:path";

import { hostedMarkerStamp, hostedServicePid, sameMarkerStamp, type MarkerStamp } from "./hosted.js";
import { hostedFile, pidFile, sessionLogFile, shutdownSentinelFile, WATCHER_PREFIX, watcherDir, type PathEnv } from "./paths.js";
import { SERVICE_OWNER_PID_ENV } from "./service.js";
import type { TapRuntime } from "./tapRuntime.js";
import type { CaptureReading, TrackingChild, TrackingExecFileFn } from "./trackingState.js";

export interface ChildLike {
  pid: number | undefined;
  unref: () => void;
  /** Node's ChildProcess `once`; read here for "exit", whose first argument is the exit code (null when signalled). */
  once?: (event: string, listener: (payload?: unknown) => void) => unknown;
}

export type SpawnFn = (
  command: string,
  args: string[],
  options: { detached: boolean; stdio: "ignore"; env: PathEnv },
) => ChildLike;

export interface DaemonDeps {
  spawn: SpawnFn;
  existsSync: (path: string) => boolean;
  mkdirSync: (path: string) => void;
  readFileSync: (path: string) => string;
  rmSync: (path: string) => void;
  writeFileSync: (path: string, content: string) => void;
  /** process.kill-shaped: signal 0 is a liveness probe, never delivers a signal. */
  kill: (pid: number, signal: number | string) => void;
  /**
   * What the OS says this pid IS, or null if it cannot be asked. Optional
   * because every real caller wants the same `ps` answer — see
   * `psCommandForPid` below — and only tests have a reason to substitute one.
   */
  commandForPid?: (pid: number) => string | null;
  /**
   * The Probe service's pid when it records this session (its `.hosted`
   * marker, checked), else null. Optional for the same reason: every real
   * caller wants `hosted.ts::hostedServicePid`.
   */
  hostedServicePid?: (sessionId: string) => number | null;
  /** The session's `.hosted` marker as it is now; defaults to `hosted.ts::hostedMarkerStamp`. */
  markerStamp?: (sessionId: string) => MarkerStamp | null;
  log: (message: string) => void;
  /** Real callers pass a setTimeout-backed sleep; tests pass an instant one so
   * the bounded poll below never actually costs wall-clock time in the suite. */
  sleep: (ms: number) => Promise<void>;
}

/**
 * Ask the OS what a pid actually IS. The default `commandForPid`.
 *
 * Byte-for-byte the same question `tap/start.py::_looks_like_the_uploader`
 * asks (`/bin/ps -ww -p <pid> -o command=`, 5s bound, any failure reads as "not
 * the tap"), because the two answers have to match — see `isWatcherAlive`.
 *
 * Never throws: an unreadable `ps` is indistinguishable, from here, from a
 * process that is not ours, and both must resolve to "do not claim it is
 * alive".
 */
function psCommandForPid(pid: number): string | null {
  try {
    return execFileSync("/bin/ps", ["-ww", "-p", String(pid), "-o", "command="], {
      encoding: "utf-8",
      timeout: 5_000,
      stdio: ["ignore", "pipe", "ignore"],
    });
  } catch {
    return null;
  }
}

/**
 * Is a `tap watch` (its wrapper) watching this session?
 *
 * TWO conditions, not one: the pidfile's pid must be signalable AND the OS
 * must say that process is the tap. The second half is not belt-and-braces —
 * it is what keeps this function's answer identical to
 * `tap/start.py::daemon_state`'s, which classifies a live-but-unrelated pid
 * as `stale` rather than `running`.
 *
 * That agreement is load-bearing now that `spawnDaemon` delegates to `tap
 * start`. The pre-check here exists purely to skip a python process spawn on
 * the common resumed-session path, so it is allowed to be CHEAPER than the
 * spawner's check but never more PERMISSIVE: a false "alive" here returns
 * `already-running` and the spawner that would have corrected it is never
 * called, leaving a tracked session with nothing capturing it — silently, and
 * for the whole session. /tmp is world-writable and pids get reused, so that
 * case is ordinary, not adversarial.
 *
 * (`probe.cli.capture._looks_like_the_uploader` spells the same check
 * slightly more strictly — plugin name, or "tap" as a whole word. This
 * mirrors `tap start`'s looser substring form on purpose: this function must
 * match the process that decides the spawn, not a third variant.)
 */
export function isWatcherAlive(
  sessionId: string,
  deps: Pick<DaemonDeps, "existsSync" | "readFileSync" | "kill" | "commandForPid">,
): boolean {
  const pf = pidFile(sessionId);
  if (!deps.existsSync(pf)) return false;
  let raw: string;
  try {
    raw = deps.readFileSync(pf);
  } catch {
    return false;
  }
  const pid = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(pid) || pid <= 0) return false;
  try {
    deps.kill(pid, 0);
  } catch {
    return false;
  }
  const command = (deps.commandForPid ?? psCommandForPid)(pid);
  return command !== null && command.includes("tap");
}

/**
 * Is anything recording this session: its `tap watch` (`isWatcherAlive`), or
 * else the Probe service, by its `.hosted` marker (`hosted.ts`)? What
 * `/probe-status` and the post-spawn wait report.
 *
 * A dead or foreign pid file falls through to the marker, as lane A's
 * `capture_state` does (a SessionStart the service took may leave a dead
 * watcher's pid file behind).
 *
 * NOT the spawn pre-check: `spawnDaemon` asks `isWatcherAlive` alone. For a
 * hosted session `tap start` also asks the service whether it still records the
 * session (`tap/start.py::_hosted`: a marker outlives a recorder that ended,
 * say when its agent exited), and a pre-check more permissive than that would
 * skip `tap start` and leave the session unrecorded. See `isWatcherAlive`.
 */
export function isDaemonAlive(
  sessionId: string,
  deps: Pick<DaemonDeps, "existsSync" | "readFileSync" | "kill" | "commandForPid" | "hostedServicePid">,
): boolean {
  return isWatcherAlive(sessionId, deps) || (deps.hostedServicePid ?? ((sid: string) => hostedServicePid(sid)))(sessionId) !== null;
}

export type SpawnResult =
  | {
      spawned: true;
      pid: number | undefined;
      /** How `tap start` exited: undefined while it runs, null when signalled (or unknown). */
      exitCode: () => number | null | undefined;
    }
  | { spawned: false; reason: "already-running" };

export interface SpawnParams {
  sessionId: string;
  transcriptPath: string;
  cwd: string;
  runtime: TapRuntime;
  baseEnv?: PathEnv;
  /** This pi process: the session's owner for the Probe service (`service.ts`). */
  ownerPid?: number;
}

// session-start.sh's own post-spawn wait: `for _ in $(seq 1 40); do [ -s
// "$PID_FILE" ] && break; sleep 0.05; done` -- 40 * 50ms = 2s, mirrored
// exactly rather than re-guessed. See waitForSpawnConfirmation().
const SPAWN_CONFIRM_POLL_MS = 50;
const SPAWN_CONFIRM_MAX_POLLS = 40;

/** How a fresh spawn was confirmed (`waitForSpawnConfirmation`'s answer). */
export const SpawnConfirmation = {
  /** The wrapper wrote its pid file, and it is the tap. */
  Watcher: "watcher",
  /** The Probe service records the session: its `.hosted` marker names it. */
  Hosted: "hosted",
  /** Neither showed up within the bound (logged). */
  Missing: "missing",
  /** A pid file showed up, but nothing behind it is alive (logged). */
  NotAlive: "not-alive",
} as const;
export type SpawnConfirmationValue = (typeof SpawnConfirmation)[keyof typeof SpawnConfirmation];

/** What the caller knows about the spawn it waits on (`waitForSpawnConfirmation`). */
export interface SpawnWitness {
  /** The `.hosted` marker as it was just BEFORE the spawn (`hostedMarkerStamp`), null for none. */
  markerBefore: MarkerStamp | null;
  /** How the spawned `tap start` exited: undefined while it runs (`SpawnResult.exitCode`). */
  exitCode: () => number | null | undefined;
  /**
   * The session's Probe switch reads `off`: the Probe service takes the session
   * and holds it, recording nothing, and names no process of its own in the
   * marker once it goes idle. `tap start` exiting 0 is then the confirmation.
   */
  switchedOff?: boolean;
}

/**
 * Poll for the wrapper's pidfile to appear and become non-empty, bounded to
 * ~2s by default -- the async port of session-start.sh's own post-spawn wait
 * (that script's lines ~266-284), called after a FRESH spawn only ("already
 * running" callers have nothing to wait for; some other wrapper already owns
 * the pidfile).
 *
 * Node's `spawn(..., {detached: true})` calls setsid() natively, which is
 * why this file no longer needs session-start.sh's nohup/disown-plus-shim
 * dance -- but that shim and this wait were solving two DIFFERENT problems,
 * and setsid() only retires one of them:
 *
 *  - It narrows a double-spawn race, and that window got WIDER when this
 *    file stopped spawning the wrapper itself: `deps.spawn()` now returns as
 *    soon as a python interpreter has been forked, and the pidfile is not
 *    written until that interpreter has started, run `tap start`'s gates,
 *    spawned the sh wrapper, and the wrapper has run its own first line
 *    (`echo $$ >"$PIDF"`). Through that whole window a second concurrent
 *    session_start for the SAME session id -- another pi process resuming
 *    the same conversation, or a second event firing before the in-process
 *    `spawnedSessionIds` guard in extension.ts has a chance to matter --
 *    sees no pidfile via `isWatcherAlive()`. It is `tap start`'s own
 *    three-state pid check that ultimately makes a doubled spawn harmless;
 *    waiting here, before the caller's session_start handler completes,
 *    keeps the window from being left wide open in the first place.
 *  - It emits the same spawn-failure diagnostic session-start.sh's own log
 *    line exists for. A start that writes no pidfile at all (spawn failed
 *    outright) or writes one and dies immediately (pidfile present, pid not
 *    signalable) is otherwise silent -- observed once in production, on the
 *    bash side, as "wrapper wrote no pid file, no process, no log line",
 *    recoverable only because the reconciler backstops a missed daemon
 *    regardless. Bounding the wait is what makes that diagnostic possible at
 *    all: an unbounded wait would never reach the "it did not show up"
 *    branch. Note that a REFUSAL by one of `tap start`'s gates lands in that
 *    same branch: extension.ts pre-checks pairing and the killswitch itself,
 *    so the remaining refusals (a gate flipped between the check and the
 *    spawn, an interpreter below 3.11, a transcript not yet on disk) are
 *    exactly the cases worth a log line.
 *
 * A SESSION THE PROBE SERVICE TAKES writes no pid file at all: `tap start`
 * asks the service first (`tap record`), and the service's `.hosted` marker
 * is its confirmation. A live one (`hosted.ts`: this user's file, naming the
 * running service) ends the wait, quietly; waiting the full bound for a pid
 * file that never comes cost every such session start 2 s and a false
 * "spawn failed?" line (audit #9). But only a marker THIS start produced: one
 * that appeared or was rewritten since the spawn (`tap record` writes it whole,
 * so a new inode), or, unchanged, once `tap start` itself exited 0 (it found
 * the service already recording the session). A marker from before the spawn
 * says nothing about it: a service that stopped recording the session leaves
 * its marker, and a `tap start` that then refused would read as confirmed.
 * `opts.spawned` says what the caller knows (the marker before the spawn, and
 * how `tap start` exited); without it the marker as it is when the wait starts
 * is taken as the one from before, and no exit is known.
 *
 * Never throws and never changes the caller's success/failure story: exactly
 * like session-start.sh, which always prints `{"continue": true}` whether or
 * not its own diagnostic fired, this only logs. The answer says which
 * confirmation came, for the caller's own log line.
 */
export async function waitForSpawnConfirmation(
  sessionId: string,
  deps: Pick<DaemonDeps, "existsSync" | "readFileSync" | "kill" | "commandForPid" | "hostedServicePid" | "markerStamp" | "log" | "sleep">,
  opts: { pollMs?: number; maxPolls?: number; spawned?: SpawnWitness } = {},
): Promise<SpawnConfirmationValue> {
  const pollMs = opts.pollMs ?? SPAWN_CONFIRM_POLL_MS;
  const maxPolls = opts.maxPolls ?? SPAWN_CONFIRM_MAX_POLLS;
  const pf = pidFile(sessionId);
  const hosted = deps.hostedServicePid ?? ((sid: string) => hostedServicePid(sid, { log: deps.log }));
  const stamp = deps.markerStamp ?? hostedMarkerStamp;
  const witness: SpawnWitness = opts.spawned ?? { markerBefore: stamp(sessionId), exitCode: () => undefined };
  /** The service records the session, and this start is why we know. */
  const hostedByThisStart = (): boolean =>
    (!sameMarkerStamp(stamp(sessionId), witness.markerBefore) || witness.exitCode() === 0) && hosted(sessionId) !== null;

  const hasContent = (): boolean => {
    if (!deps.existsSync(pf)) return false;
    try {
      return deps.readFileSync(pf).trim().length > 0;
    } catch {
      return false;
    }
  };
  const confirmation = (): SpawnConfirmationValue | null => {
    if (hasContent()) return SpawnConfirmation.Watcher;
    // Probe off: the service took the session and holds it; `tap start` said so.
    if (witness.switchedOff && witness.exitCode() === 0) return SpawnConfirmation.Hosted;
    return hostedByThisStart() ? SpawnConfirmation.Hosted : null;
  };

  let confirmed = confirmation();
  for (let i = 0; confirmed === null && i < maxPolls; i++) {
    await deps.sleep(pollMs);
    confirmed = confirmation();
  }

  if (confirmed === null) {
    deps.log(
      `tap: wrapper wrote no pid file within ${(pollMs * maxPolls) / 1000}s (spawn failed?); ` +
        `session=${sessionId} — transcript will be recovered by the reconciler`,
    );
    return SpawnConfirmation.Missing;
  }
  if (confirmed === SpawnConfirmation.Hosted) return confirmed;

  if (!isDaemonAlive(sessionId, { ...deps, hostedServicePid: hosted })) {
    deps.log(
      `tap: wrapper pid not alive just after spawn; session=${sessionId} — ` +
        "transcript will be recovered by the reconciler",
    );
    return SpawnConfirmation.NotAlive;
  }
  return confirmed;
}

/** The environment a `python -m tap ...` child of this package runs with:
 * ours, PROBE_TAP_SOURCE set to pi, and the resolved tap first on PYTHONPATH. */
function tapChildEnv(runtime: TapRuntime, baseEnv: PathEnv): PathEnv {
  const pythonPath = runtime.tapRoot
    ? [runtime.tapRoot, baseEnv.PYTHONPATH].filter((v): v is string => Boolean(v)).join(delimiter)
    : baseEnv.PYTHONPATH;
  return {
    ...baseEnv,
    PROBE_TAP_SOURCE: "pi",
    ...(pythonPath ? { PYTHONPATH: pythonPath } : {}),
  };
}

/**
 * Spawn the tap daemon for one pi session, idempotently.
 *
 * Idempotent via the pidfile + liveness check ONLY — not via any in-memory
 * state here, so this is safe to call from a fresh process too (a resumed
 * session in a brand-new pi invocation asks the same question a live
 * extension runtime would). Callers that also want a fast synchronous
 * same-process guard should keep their own Set of session ids they've
 * already spawned; see extension.ts.
 *
 * Deliberately still SYNCHRONOUS, even though `waitForSpawnConfirmation()`
 * above is async: extension.ts marks its own in-process `spawnedSessionIds`
 * guard immediately after this call returns, with no `await` in between, and
 * that ordering matters — see the comment at that call site. Callers that
 * want the wait/diagnostic should call `waitForSpawnConfirmation()`
 * themselves, AFTER marking, once this returns `{ spawned: true, ... }`.
 */
export function spawnDaemon(params: SpawnParams, deps: DaemonDeps): SpawnResult {
  const { sessionId, transcriptPath, cwd, runtime } = params;

  // The watcher alone, never the `.hosted` marker: `tap start` decides a hosted
  // session by asking the service too (see `isDaemonAlive`).
  if (isWatcherAlive(sessionId, deps)) {
    return { spawned: false, reason: "already-running" };
  }

  const baseEnv = params.baseEnv ?? process.env;
  const logFile = sessionLogFile(sessionId, baseEnv);

  deps.mkdirSync(join(logFile, ".."));

  const env: PathEnv = {
    ...tapChildEnv(runtime, baseEnv),
    // Read by the tap's registration thread only while the service is on.
    ...(params.ownerPid ? { [SERVICE_OWNER_PID_ENV]: String(params.ownerPid) } : {}),
  };

  // `tap start` owns the wrapper, the pid file, the stale-sentinel clear and
  // every gate. This function keeps only what is genuinely pi's: which
  // interpreter, which env, and the UI announcements its caller makes from
  // the pre-checks in extension.ts.
  const child = deps.spawn(
    runtime.python,
    [
      "-m",
      "tap",
      "start",
      "--session-id",
      sessionId,
      "--cwd",
      cwd,
      "--transcript",
      transcriptPath,
    ],
    { detached: true, stdio: "ignore", env },
  );
  let exit: number | null | undefined;
  child.once?.("exit", (code) => {
    exit = typeof code === "number" ? code : null;
  });
  child.unref();

  deps.log(`spawned tap watcher for session ${sessionId} (pid ${child.pid ?? "unknown"})`);
  return { spawned: true, pid: child.pid, exitCode: () => exit };
}

/**
 * Stop the daemon for one pi session — the session_shutdown-event analog of
 * session-end.sh. Always safe to call even if nothing was ever spawned.
 *
 * Every wrapper we spawn is its own process-group leader (Node's
 * `detached: true`), so — unlike session-end.sh, which has to check pgid==pid
 * to stay compatible with a pre-0.1.3 wrapper that predates that guarantee —
 * the negated `kill(-pid, ...)` form is always correct here.
 */
export function stopDaemon(sessionId: string, deps: Pick<DaemonDeps, "existsSync" | "readFileSync" | "rmSync" | "writeFileSync" | "kill">): void {
  const shutdownFile = shutdownSentinelFile(sessionId);
  const pf = pidFile(sessionId);

  // Touched BEFORE the signal so a respawn racing the wrapper's own restart
  // loop still sees it and exits, instead of relaunching one more time.
  try {
    deps.writeFileSync(shutdownFile, "");
  } catch {
    // Best-effort; the daemon's own orphan-session detection is the fallback.
  }

  if (deps.existsSync(pf)) {
    let raw = "";
    try {
      raw = deps.readFileSync(pf);
    } catch {
      raw = "";
    }
    const pid = Number.parseInt(raw.trim(), 10);
    if (Number.isFinite(pid) && pid > 0) {
      try {
        deps.kill(-pid, "SIGTERM");
      } catch {
        // Already gone — fine.
      }
    }
    try {
      deps.rmSync(pf);
    } catch {
      // Fine.
    }
  }

  // Deliberately do NOT remove the shutdown sentinel — mirrors session-end.sh:
  // if the wrapper missed this signal, the sentinel is the last-resort stop
  // condition both the wrapper's loop and the daemon's per-tick check watch.
  // The next spawnDaemon() for this session id clears it before spawning.
}

/**
 * How long a quit waits for the Probe service to finish a session it records
 * (`tap finish --wait`): its recorder's last pass and FINALIZE. session-end.sh
 * waits 1.1 s under Claude Code's default exit budget, 2.6 s under Codex and
 * 15 s for a harness that awaits its hooks; pi awaits its handlers with no
 * budget of its own, so this is pi's choice: long enough for a usual last pass,
 * short enough that a quit is not held. The service finishes the session
 * whether or not pi is still waiting.
 */
export const HOSTED_FINISH_WAIT_S = 3;
/** On top of the wait: the client's Python start and its connect. Then the client is stopped. */
export const HOSTED_FINISH_GRACE_MS = 1_000;

/** What `finishHostedSession` did. */
export const HostedFinish = {
  /** No `.hosted` marker: the session was not the service's. */
  NotHosted: "not-hosted",
  /** The marker went; no `tap finish` was asked (a switch, a dead service, no interpreter). */
  MarkerDropped: "marker-dropped",
  /** `tap finish` ran (bounded), then the marker went. */
  Finished: "finished",
} as const;
export type HostedFinishValue = (typeof HostedFinish)[keyof typeof HostedFinish];

export interface FinishParams {
  /**
   * The interpreter `tap finish` runs on (null: none found; the marker still
   * goes). A function is called only when `tap finish` will run: resolving one
   * may ask interpreters their version, which a switch, or a session the
   * service does not record, must not pay for.
   */
  runtime: TapRuntime | null | (() => TapRuntime | null);
  /** Seconds to wait for the last pass: HOSTED_FINISH_WAIT_S on a quit, 0 on a session switch. */
  waitS: number;
  baseEnv?: PathEnv;
}

/**
 * The hosted half of session-end.sh, after `stopDaemon` touched the sentinel.
 *
 * For a session the Probe service records (its `.hosted` marker, plan A2):
 *  - on a quit (`waitS` > 0) and while the marker names the live service, ask
 *    the service to finish the session now and wait, bounded, for its last
 *    pass (`python -m tap finish --session-id S --wait W`). The sentinel alone
 *    ends the recorder only at its next pass, and a recorder still finishing
 *    when the session is resumed answered "recording" to the resume;
 *  - then remove the marker, as session-end.sh does. This is what lets a
 *    resume record again (audit #10): `tap start` reads a session whose
 *    marker is gone as recorded by no one, clears the sentinel and asks the
 *    service to record it (`session.record`, which starts a new recorder even
 *    while an old one finishes). Left in place, the marker plus a finishing
 *    recorder read "running", and nothing recorded the resumed session.
 *
 * A session switch (`waitS` 0) asks no `tap finish`, as session-end.sh skips
 * it on `clear` and `resume`: the sentinel ends the recorder, and a wait would
 * hold pi's switch. Never throws; every failure ends in the marker removed.
 *
 * The bound holds whatever the client does: at `waitS` + HOSTED_FINISH_GRACE_MS
 * this stops waiting on its own timer and kills the client (SIGKILL), so a
 * `tap finish` that ignores the SIGTERM `execFile`'s timeout sends cannot hold
 * pi's quit.
 */
export async function finishHostedSession(
  sessionId: string,
  params: FinishParams,
  deps: Pick<DaemonDeps, "existsSync" | "rmSync" | "hostedServicePid" | "log"> & { execFile: TrackingExecFileFn },
): Promise<HostedFinishValue> {
  const marker = hostedFile(sessionId);
  if (!deps.existsSync(marker)) return HostedFinish.NotHosted;
  let outcome: HostedFinishValue = HostedFinish.MarkerDropped;
  const { waitS } = params;
  const hosted = deps.hostedServicePid ?? ((sid: string) => hostedServicePid(sid, { log: deps.log }));
  const runtime =
    waitS > 0 && hosted(sessionId) !== null
      ? typeof params.runtime === "function"
        ? params.runtime()
        : params.runtime
      : null;
  if (runtime) {
    const bound = waitS * 1000 + HOSTED_FINISH_GRACE_MS;
    await new Promise<void>((resolve) => {
      let settled = false;
      let child: TrackingChild | void = undefined;
      const settle = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        try {
          child?.kill("SIGKILL");
        } catch {
          // gone already
        }
        settle();
      }, bound);
      try {
        child = deps.execFile(
          runtime.python,
          ["-m", "tap", "finish", "--session-id", sessionId, "--wait", String(waitS)],
          {
            env: tapChildEnv(runtime, params.baseEnv ?? process.env),
            encoding: "utf8",
            timeout: bound,
            maxBuffer: 64 * 1024,
            windowsHide: true,
          },
          // Exit 1 (the service does not record it) and 2 (not finished in
          // time) read the same here: the service finishes it either way.
          () => settle(),
        );
      } catch (err) {
        deps.log(`tap finish did not run for ${sessionId}: ${err instanceof Error ? err.message : String(err)}`);
        settle();
      }
    });
    outcome = HostedFinish.Finished;
  }
  try {
    deps.rmSync(marker);
  } catch {
    // Not ours to remove (another user's file in the shared /tmp), or gone.
  }
  return outcome;
}

/**
 * Capture's words (`probe.cli.capture_state.REASONS`) this package acts on.
 * `not recording`: the Probe service's recorder lives for the session but does
 * not hold it yet. It claims the session after its first server read (2.0 s
 * after the start, #3619), so the footer read right after the spawn says
 * "not capturing" of a session that is captured.
 */
export const CaptureReason = {
  NotRecording: "not recording",
} as const;

/**
 * The waits between a start's footer read and each look after it while the
 * recorder has not claimed the session: 3 s, 15 s and 60 s after the start.
 * Each look runs `probe session initialize`, the start's own read, so only
 * near the start (it marks the session's profile as the machine says now) and
 * a few: a recorder whose server does not answer stays "not recording", which
 * is then true.
 */
export const CLAIM_RECHECK_MS: readonly number[] = [3_000, 12_000, 45_000];

/** A re-read's answer that ends the looks: the session ended, or pi switched to another. */
export const ClaimRecheck = {
  Stop: "stop",
} as const;

/** The recorder lives and has not claimed the session yet: a reading that will change on its own. */
export function awaitingClaim(capture: CaptureReading | undefined): boolean {
  return capture !== undefined && !capture.running && capture.reason === CaptureReason.NotRecording;
}

export interface ClaimRecheckDeps {
  /**
   * Read capture again and redraw the footer: the new reading; undefined when
   * it could not be read (the next look still comes); `ClaimRecheck.Stop` when
   * the session is no longer the one looked at.
   */
  reread: () => Promise<CaptureReading | undefined | typeof ClaimRecheck.Stop>;
  /** Run `fn` in `ms`, never keeping the process alive for it (an unref'd `setTimeout`). */
  later: (fn: () => void, ms: number) => void;
  log: (message: string) => void;
}

/**
 * Look at a just-started session's capture again, on `delaysMs`, while its
 * recorder has not claimed it (`awaitingClaim`), and stop at the first reading
 * that says anything else. Never throws.
 */
export function recheckUntilClaimed(
  capture: CaptureReading | undefined,
  deps: ClaimRecheckDeps,
  delaysMs: readonly number[] = CLAIM_RECHECK_MS,
): void {
  if (!awaitingClaim(capture) || delaysMs.length === 0) return;
  const [wait, ...rest] = delaysMs;
  deps.later(() => {
    deps.reread().then(
      (next) => {
        if (next !== ClaimRecheck.Stop) recheckUntilClaimed(next ?? capture, deps, rest);
      },
      (err) => deps.log(`capture re-read failed: ${err instanceof Error ? err.message : String(err)}`),
    );
  }, wait);
}

/** Sentinels older than this are from a session that is long over — see below. */
const STALE_SENTINEL_MS = 2 * 24 * 60 * 60 * 1000;

export interface PruneDeps {
  readdirSync: (dir: string) => string[];
  statMtimeMs: (path: string) => number;
  rmSync: (path: string) => void;
  now: () => number;
}

/**
 * Delete `.shutdown` sentinels older than two days — session-end.sh's own
 * SessionEnd hook never deletes one (it is the last-resort stop signal for
 * an orphaned daemon; see stopDaemon() above), and only a LATER
 * session_start *for that same session id* clears it. Session ids are
 * UUIDs and never recur, so without this, every session that ever ran
 * leaks one file into /tmp forever — session-start.sh's own comment records
 * "120 stale sentinels against 0 live daemons" as the observed cost of
 * skipping this.
 *
 * Both Claude Code's hook and this extension write into the SAME
 * `probe-research-tap-watcher-*` namespace (see paths.ts), so a machine
 * that also runs Claude Code already gets this pruning for free from its
 * hook. A pi-only install would not, hence doing it here too.
 *
 * `fs.readdirSync("/tmp")` needs none of session-start.sh's `find`-specific
 * workaround: that comment is about `find /tmp` matching the /tmp symlink
 * itself on macOS (find defaults to not following a symlink that IS the
 * search root) and therefore silently listing nothing. `readdir()` — what
 * Node's fs module is built on — resolves the symlink as part of opening
 * the directory, the same way `stat`/`open` do, so it has no such gotcha.
 */
export function pruneStaleShutdownSentinels(deps: PruneDeps): void {
  let names: string[];
  try {
    names = deps.readdirSync(watcherDir());
  } catch {
    return;
  }
  const cutoff = deps.now() - STALE_SENTINEL_MS;
  const prefix = `${WATCHER_PREFIX}-watcher-`;
  for (const name of names) {
    if (!name.startsWith(prefix) || !name.endsWith(".shutdown")) continue;
    const full = join(watcherDir(), name);
    let mtimeMs: number;
    try {
      mtimeMs = deps.statMtimeMs(full);
    } catch {
      continue; // Removed concurrently — fine.
    }
    if (mtimeMs < cutoff) {
      try {
        deps.rmSync(full);
      } catch {
        // Fine — another process may have already cleaned it up.
      }
    }
  }
}
