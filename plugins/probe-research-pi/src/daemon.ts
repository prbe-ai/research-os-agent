/**
 * Spawns and stops the probe-research-tap daemon for one pi session.
 *
 * This is the TypeScript/Node equivalent of what
 * `agent/plugins/probe-research-tap/hooks/session-start.sh` and
 * `hooks/session-end.sh` do for Claude Code and Codex — ported, not copied
 * verbatim, because Node's `spawn(..., {detached: true})` already puts the
 * child in its own session/process group on POSIX (libuv calls setsid()
 * internally), which is exactly the property session-start.sh's own comments
 * say it had to work around bash's lack of a `setsid(1)` builtin to get. That
 * whole shim (nohup + a python `os.setsid()`-or-fork inline script) is not
 * needed here.
 *
 * What IS ported: the crash-recovery wrapper. `tap watch` is a long-running
 * daemon; if it dies (transient error, OOM, etc.) nothing restarts it unless
 * something is watching. session-start.sh handles this with a small bash
 * loop that respawns the daemon up to 5 times per rolling 60s window and
 * exits for good once the shutdown sentinel appears. That loop is
 * reproduced here nearly verbatim in POSIX sh (not bash — no arrays needed,
 * and `/bin/sh` is what both target platforms guarantee), spawned as the
 * detached process instead of the daemon directly. The wrapper — not this
 * Node process — is the long-lived thing; this function returns as soon as
 * it starts.
 *
 * ALSO ported, separately from the setsid question: session-start.sh's
 * post-spawn wait (its own lines ~266-284). setsid() replaces that wait's
 * ORIGINAL motivation (a stale `$!` from the old nohup/disown shim) but not
 * two further things it did that have nothing to do with process groups —
 * see `waitForSpawnConfirmation()` below for both, and why they still apply
 * here even though this is native Node spawn, not a bash shim.
 */

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
  log: (message: string) => void;
  /** Real callers pass a setTimeout-backed sleep; tests pass an instant one so
   * the bounded poll below never actually costs wall-clock time in the suite. */
  sleep: (ms: number) => Promise<void>;
}

/** Build POSIX-sh crash-recovery wrapper source. Pure function — easy to snapshot in tests. */
export function buildWrapperScript(): string {
  return [
    'SID="$1"; CWD="$2"; PY="$3"; LOG="$4"; PIDF="$5"; PREFIX="$6"; shift 6',
    'echo $$ >"$PIDF"',
    'SHUTDOWN="/tmp/${PREFIX}-watcher-${SID}.shutdown"',
    "RESTART_COUNT=0",
    'WINDOW_START=$(date +%s)',
    'CHILD_PID=""',
    "trap '[ -n \"$CHILD_PID\" ] && kill -TERM \"$CHILD_PID\" 2>/dev/null; exit 0' TERM INT",
    "while true; do",
    '  [ -f "$SHUTDOWN" ] && exit 0',
    '  NOW=$(date +%s)',
    '  if [ $((NOW - WINDOW_START)) -ge 60 ]; then',
    "    WINDOW_START=$NOW",
    "    RESTART_COUNT=0",
    "  fi",
    '  if [ "$RESTART_COUNT" -ge 5 ]; then',
    '    echo "[$(date -u +%FT%TZ)] tap: too many restarts in 1min, giving up" >>"$LOG"',
    "    exit 1",
    "  fi",
    '  "$PY" -m tap watch --session-id "$SID" --cwd "$CWD" "$@" >>"$LOG" 2>&1 &',
    "  CHILD_PID=$!",
    '  wait "$CHILD_PID" 2>/dev/null || true',
    '  CHILD_PID=""',
    '  [ -f "$SHUTDOWN" ] && exit 0',
    "  RESTART_COUNT=$((RESTART_COUNT + 1))",
    "  sleep 5",
    "done",
  ].join("\n");
}

/** Read a pidfile and check whether that pid (== pgid, since we always spawn detached) is alive. */
export function isDaemonAlive(sessionId: string, deps: Pick<DaemonDeps, "existsSync" | "readFileSync" | "kill">): boolean {
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
    return true;
  } catch {
    return false;
  }
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
 *  - It narrows a double-spawn race. The wrapper writes its pidfile
 *    asynchronously from inside `/bin/sh -c` (its first line is `echo $$
 *    >"$PIDF"`, run only once the shell has actually started), so between
 *    `deps.spawn()` returning and the pidfile existing on disk there is a
 *    real window in which a second concurrent session_start for the SAME
 *    session id -- another pi process resuming the same conversation, or a
 *    second event firing before the in-process `spawnedSessionIds` guard in
 *    extension.ts has a chance to matter -- sees no pidfile via
 *    `isDaemonAlive()` and spawns a second daemon. Waiting here, before the
 *    caller's session_start handler completes, shrinks that window instead
 *    of leaving it fully open the way the pre-wait code did.
 *  - It emits the same spawn-failure diagnostic session-start.sh's own log
 *    line exists for. A wrapper that writes no pidfile at all (spawn failed
 *    outright) or writes one and dies immediately (pidfile present, pid not
 *    signalable) is otherwise silent -- observed once in production, on the
 *    bash side, as "wrapper wrote no pid file, no process, no log line",
 *    recoverable only because the reconciler backstops a missed daemon
 *    regardless. Bounding the wait is what makes that diagnostic possible at
 *    all: an unbounded wait would never reach the "it did not show up" branch.
 *
 * Never throws and never changes the caller's success/failure story: exactly
 * like session-start.sh, which always prints `{"continue": true}` whether or
 * not its own diagnostic fired, this only logs.
 */
export async function waitForSpawnConfirmation(
  sessionId: string,
  deps: Pick<DaemonDeps, "existsSync" | "readFileSync" | "kill" | "log" | "sleep">,
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

  const pf = pidFile(sessionId);
  const shutdownFile = shutdownSentinelFile(sessionId);
  const logFile = sessionLogFile(sessionId, params.baseEnv ?? process.env);

  deps.mkdirSync(join(logFile, ".."));

  // A resumed session's PREVIOUS run may have left its shutdown sentinel behind
  // (session-end.sh's Claude Code/Codex analog deliberately never deletes it —
  // see stopDaemon() below). With no live wrapper for this session id, that
  // sentinel is stale: clear it before spawning, or the fresh wrapper's very
  // first `[ -f "$SHUTDOWN" ] && exit 0` check would kill it immediately.
  try {
    deps.rmSync(shutdownFile);
  } catch {
    // Fine — most sessions have no leftover sentinel to clear.
  }

  const baseEnv = params.baseEnv ?? process.env;
  const pythonPath = runtime.tapRoot
    ? [runtime.tapRoot, baseEnv.PYTHONPATH].filter((v): v is string => Boolean(v)).join(delimiter)
    : baseEnv.PYTHONPATH;

  const env: PathEnv = {
    ...baseEnv,
    PROBE_TAP_SOURCE: "pi",
    ...(pythonPath ? { PYTHONPATH: pythonPath } : {}),
  };

  const child = deps.spawn(
    "/bin/sh",
    ["-c", buildWrapperScript(), "sh", sessionId, cwd, runtime.python, logFile, pf, WATCHER_PREFIX, "--transcript", transcriptPath],
    { detached: true, stdio: "ignore", env },
  );
  child.unref();

  deps.log(`spawned tap watcher for session ${sessionId} (wrapper pid ${child.pid ?? "unknown"})`);
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
