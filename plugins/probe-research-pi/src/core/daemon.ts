/**
 * pi's side of a session's capture: reading it back, and its end. Nothing here starts it: the Probe
 * service asks for the session's recorder itself, at pi's session start and its next events (the
 * tap's own `start`, gates included; service-only task 16, M9).
 *
 * What is left is what the extension still reads and writes beside the tap:
 *
 *  - `isWatcherAlive()` / `isDaemonAlive()`: whether an older tap's wrapper, or the Probe service
 *    (its `.hosted` marker, `hosted.ts`), records the session, for `/probe-status` and the footer.
 *  - `stopDaemon()`: the session_shutdown analog of session-end.sh (the sentinel), and
 *    `finishHostedSession()`, the `tap finish` half of it, through the service.
 *  - The footer's re-read until a recorder claims the session (`recheckUntilClaimed`), and the
 *    sentinel pruning: a pi-only install has no session-start.sh to prune them.
 *  - The service's worker stamp (`loadWorkerStamp`, `getStartingEndMs`): a daemon with no lease yet
 *    is starting for DAEMON_START_GRACE_MS, then the footer says `daemon not recording`.
 */

import { execFileSync } from "node:child_process";
import { join } from "node:path";

import { hostedServicePid } from "./hosted.js";
import { hostedFile, pidFile, probeStateDir, shutdownSentinelFile, WATCHER_PREFIX, watcherDir, type PathEnv } from "./paths.js";
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
 * /tmp is world-writable and pids get reused, so a false "alive" is ordinary,
 * not adversarial: `/probe-status` would report a session captured that is not.
 * (`tap start`, which the Probe service runs, spells the same substring check.)
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
 * Is anything recording this session: an older tap's `tap watch` (`isWatcherAlive`),
 * or else the Probe service, by its `.hosted` marker (`hosted.ts`)? What
 * `/probe-status` reports.
 *
 * A dead or foreign pid file falls through to the marker, as lane A's
 * `capture_state` does (a SessionStart the service took may leave a dead
 * watcher's pid file behind).
 */
export function isDaemonAlive(
  sessionId: string,
  deps: Pick<DaemonDeps, "existsSync" | "readFileSync" | "kill" | "commandForPid" | "hostedServicePid">,
): boolean {
  return isWatcherAlive(sessionId, deps) || (deps.hostedServicePid ?? ((sid: string) => hostedServicePid(sid)))(sessionId) !== null;
}

/**
 * A pi session ended: touch its shutdown sentinel (what a recorder reads as the session's stop) and
 * remove a pid file an older tap's wrapper left. Nothing is signalled (service-only task 16): the
 * Probe service records the session, and `finishHostedSession` asks it to finish. Always safe.
 */
export function stopDaemon(sessionId: string, deps: Pick<DaemonDeps, "existsSync" | "rmSync" | "writeFileSync">): void {
  try {
    deps.writeFileSync(shutdownSentinelFile(sessionId), "");
  } catch {
    // Best-effort; the service's own end-of-session check is the fallback.
  }
  const pf = pidFile(sessionId);
  if (deps.existsSync(pf)) {
    try {
      deps.rmSync(pf);
    } catch {
      // Fine.
    }
  }
  // Deliberately do NOT remove the shutdown sentinel — mirrors session-end.sh: a recorder still
  // making its last pass reads it. The next capture start for this session id clears it.
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
/** On top of the wait: the round trip to the service. Then pi stops waiting. */
export const HOSTED_FINISH_GRACE_MS = 1_000;

/** What `finishHostedSession` did. */
export const HostedFinish = {
  /** No `.hosted` marker: the session was not the service's. */
  NotHosted: "not-hosted",
  /** The marker went; the service was not asked to finish it (a switch, a dead service). */
  MarkerDropped: "marker-dropped",
  /** The service was asked to finish it (bounded), then the marker went. */
  Finished: "finished",
} as const;
export type HostedFinishValue = (typeof HostedFinish)[keyof typeof HostedFinish];

export interface FinishParams {
  /** Seconds to wait for the last pass: HOSTED_FINISH_WAIT_S on a quit, 0 on a session switch. */
  waitS: number;
}

/**
 * The hosted half of session-end.sh, after `stopDaemon` touched the sentinel.
 *
 * For a session the Probe service records (its `.hosted` marker, plan A2):
 *  - on a quit (`waitS` > 0) and while the marker names the live service, ask
 *    the service to finish the session now (`session.finish {sid, wait}`, on its
 *    socket: pi spawns no `tap finish`, service-only task 16) and wait, bounded,
 *    for its last pass. The sentinel alone ends the recorder only at its next
 *    pass, and a recorder still finishing when the session is resumed answered
 *    "recording" to the resume;
 *  - then remove the marker, as session-end.sh does. This is what lets a resume
 *    record again (audit #10).
 *
 * A session switch (`waitS` 0) asks for no finish, as session-end.sh skips it on
 * `clear` and `resume`: the sentinel ends the recorder, and a wait would hold
 * pi's switch. Never throws; every failure ends in the marker removed. The bound
 * holds whatever the service does: at `waitS` + HOSTED_FINISH_GRACE_MS pi stops waiting.
 */
export async function finishHostedSession(
  sessionId: string,
  params: FinishParams,
  deps: Pick<DaemonDeps, "existsSync" | "rmSync" | "hostedServicePid" | "log"> & {
    finish: (sessionId: string, waitS: number) => Promise<unknown>;
  },
): Promise<HostedFinishValue> {
  const marker = hostedFile(sessionId);
  if (!deps.existsSync(marker)) return HostedFinish.NotHosted;
  let outcome: HostedFinishValue = HostedFinish.MarkerDropped;
  const { waitS } = params;
  const hosted = deps.hostedServicePid ?? ((sid: string) => hostedServicePid(sid, { log: deps.log }));
  if (waitS > 0 && hosted(sessionId) !== null) {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        deps.finish(sessionId, waitS),
        new Promise<void>((resolve) => {
          timer = setTimeout(resolve, waitS * 1000 + HOSTED_FINISH_GRACE_MS);
        }),
      ]);
    } catch (err) {
      deps.log(`session.finish did not run for ${sessionId}: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
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

/**
 * `<state>/sessions/<sid>.worker`: what the Probe service last did about this session's daemon worker, by wall clock
 * (`session_marker.WORKER_SUFFIX`): `{"state": "starting" | "gave_up" | "crash_loop" | "exited", "at": <unix seconds>}`.
 * `exited`: any other exit, so a launch that already died never reads as starting.
 */
export const WORKER_SUFFIX = ".worker";
export const WorkerState = {
  Starting: "starting",
  GaveUp: "gave_up",
  CrashLoop: "crash_loop",
  Exited: "exited",
} as const;
export type WorkerStateValue = (typeof WorkerState)[keyof typeof WorkerState];

/** How long a spawned worker may go without its first lease before the footer says `daemon not recording`
 * (`session_marker.DAEMON_START_GRACE_S`). */
export const DAEMON_START_GRACE_MS = 120_000;

export interface WorkerStamp {
  state: WorkerStateValue;
  atMs: number;
}

/** The session's worker stamp when well-formed, else null (an older service, or no worker asked for yet). An
 * unknown state is no stamp (a newer service's word). */
export function loadWorkerStamp(
  sessionId: string,
  deps: { env: PathEnv; readFileSync: (path: string) => string },
): WorkerStamp | null {
  let data: unknown;
  try {
    data = JSON.parse(deps.readFileSync(join(probeStateDir(deps.env), "sessions", `${sessionId}${WORKER_SUFFIX}`)));
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) return null;
  const { state, at } = data as { state?: unknown; at?: unknown };
  const known = Object.values(WorkerState).find((value) => value === state);
  if (known === undefined || typeof at !== "number" || !Number.isFinite(at)) return null;
  return { state: known, atMs: at * 1000 };
}

/**
 * When a worker with no lease yet stops counting as STARTING (`session_marker.is_worker_starting`): the window's end
 * (ms) while the stamp says `starting` and is at most DAEMON_START_GRACE_MS old, else null (any other state, too
 * old, or stamped in the future).
 */
export function getStartingEndMs(stamp: WorkerStamp, nowMs: number): number | null {
  if (stamp.state !== WorkerState.Starting) return null;
  const age = nowMs - stamp.atMs;
  return age >= 0 && age <= DAEMON_START_GRACE_MS ? stamp.atMs + DAEMON_START_GRACE_MS : null;
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
