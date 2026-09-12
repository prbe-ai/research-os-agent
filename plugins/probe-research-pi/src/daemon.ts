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
 *  - THE PRE-CHECK, THE WAIT AND THE STOP. `isDaemonAlive()` (which must
 *    agree with the spawner — see its own comment),
 *    `waitForSpawnConfirmation()`'s post-spawn diagnostic, `stopDaemon()`'s
 *    session_shutdown analog of session-end.sh, and the sentinel pruning.
 *    All four read and write the same `/tmp` files `tap start` does.
 */

import { execFileSync } from "node:child_process";
import { delimiter, join } from "node:path";

import { pidFile, sessionLogFile, shutdownSentinelFile, WATCHER_PREFIX, type PathEnv } from "./paths.js";
import type { TapRuntime } from "./tapRuntime.js";

export interface ChildLike {
  pid: number | undefined;
  unref: () => void;
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
  log: (message: string) => void;
  /** Real callers pass a setTimeout-backed sleep; tests pass an instant one so
   * the bounded poll below never actually costs wall-clock time in the suite. */
  sleep: (ms: number) => Promise<void>;
}

/**
 * Ask the OS what a pid actually IS. The default `commandForPid`.
 *
 * Byte-for-byte the same question `tap/start.py::_looks_like_the_uploader`
 * asks (`/bin/ps -p <pid> -o command=`, 5s bound, any failure reads as "not
 * the tap"), because the two answers have to match — see `isDaemonAlive`.
 *
 * Never throws: an unreadable `ps` is indistinguishable, from here, from a
 * process that is not ours, and both must resolve to "do not claim it is
 * alive".
 */
function psCommandForPid(pid: number): string | null {
  try {
    return execFileSync("/bin/ps", ["-p", String(pid), "-o", "command="], {
      encoding: "utf-8",
      timeout: 5_000,
      stdio: ["ignore", "pipe", "ignore"],
    });
  } catch {
    return null;
  }
}

/**
 * Is a tap daemon watching this session?
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
export function isDaemonAlive(
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

export type SpawnResult =
  | { spawned: true; pid: number | undefined }
  | { spawned: false; reason: "already-running" };

export interface SpawnParams {
  sessionId: string;
  transcriptPath: string;
  cwd: string;
  runtime: TapRuntime;
  baseEnv?: PathEnv;
}

// session-start.sh's own post-spawn wait: `for _ in $(seq 1 40); do [ -s
// "$PID_FILE" ] && break; sleep 0.05; done` -- 40 * 50ms = 2s, mirrored
// exactly rather than re-guessed. See waitForSpawnConfirmation().
const SPAWN_CONFIRM_POLL_MS = 50;
const SPAWN_CONFIRM_MAX_POLLS = 40;

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
 *    sees no pidfile via `isDaemonAlive()`. It is `tap start`'s own
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
 * Never throws and never changes the caller's success/failure story: exactly
 * like session-start.sh, which always prints `{"continue": true}` whether or
 * not its own diagnostic fired, this only logs.
 */
export async function waitForSpawnConfirmation(
  sessionId: string,
  deps: Pick<DaemonDeps, "existsSync" | "readFileSync" | "kill" | "commandForPid" | "log" | "sleep">,
  opts: { pollMs?: number; maxPolls?: number } = {},
): Promise<void> {
  const pollMs = opts.pollMs ?? SPAWN_CONFIRM_POLL_MS;
  const maxPolls = opts.maxPolls ?? SPAWN_CONFIRM_MAX_POLLS;
  const pf = pidFile(sessionId);

  const hasContent = (): boolean => {
    if (!deps.existsSync(pf)) return false;
    try {
      return deps.readFileSync(pf).trim().length > 0;
    } catch {
      return false;
    }
  };

  let confirmed = hasContent();
  for (let i = 0; !confirmed && i < maxPolls; i++) {
    await deps.sleep(pollMs);
    confirmed = hasContent();
  }

  if (!confirmed) {
    deps.log(
      `tap: wrapper wrote no pid file within ${(pollMs * maxPolls) / 1000}s (spawn failed?); ` +
        `session=${sessionId} — transcript will be recovered by the reconciler`,
    );
    return;
  }

  if (!isDaemonAlive(sessionId, deps)) {
    deps.log(
      `tap: wrapper pid not alive just after spawn; session=${sessionId} — ` +
        "transcript will be recovered by the reconciler",
    );
  }
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

  if (isDaemonAlive(sessionId, deps)) {
    return { spawned: false, reason: "already-running" };
  }

  const baseEnv = params.baseEnv ?? process.env;
  const logFile = sessionLogFile(sessionId, baseEnv);

  deps.mkdirSync(join(logFile, ".."));

  const pythonPath = runtime.tapRoot
    ? [runtime.tapRoot, baseEnv.PYTHONPATH].filter((v): v is string => Boolean(v)).join(delimiter)
    : baseEnv.PYTHONPATH;

  const env: PathEnv = {
    ...baseEnv,
    PROBE_TAP_SOURCE: "pi",
    ...(pythonPath ? { PYTHONPATH: pythonPath } : {}),
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
  child.unref();

  deps.log(`spawned tap watcher for session ${sessionId} (pid ${child.pid ?? "unknown"})`);
  return { spawned: true, pid: child.pid };
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
    names = deps.readdirSync("/tmp");
  } catch {
    return;
  }
  const cutoff = deps.now() - STALE_SENTINEL_MS;
  const prefix = `${WATCHER_PREFIX}-watcher-`;
  for (const name of names) {
    if (!name.startsWith(prefix) || !name.endsWith(".shutdown")) continue;
    const full = join("/tmp", name);
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
