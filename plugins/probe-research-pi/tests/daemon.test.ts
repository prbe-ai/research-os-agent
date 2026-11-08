import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import {
  awaitingClaim,
  CaptureReason,
  CLAIM_RECHECK_MS,
  ClaimRecheck,
  DAEMON_START_GRACE_MS,
  finishHostedSession,
  getStartingEndMs,
  HOSTED_FINISH_GRACE_MS,
  HostedFinish,
  isDaemonAlive,
  loadWorkerStamp,
  pruneStaleShutdownSentinels,
  recheckUntilClaimed,
  stopDaemon,
  WorkerState,
  type ClaimRecheckDeps,
  type DaemonDeps,
  type PruneDeps,
} from "../src/core/daemon.js";
import { hostedFile, OsPlatform, pidFile, shutdownSentinelFile, WATCHER_DIR_ENV, watcherDir } from "../src/core/paths.js";
import type { CaptureReading } from "../src/core/trackingState.js";

/** A tiny in-memory filesystem, standing in for real fs. */
function fakeDeps(overrides: Partial<DaemonDeps> = {}): DaemonDeps & { files: Map<string, string> } {
  const files = new Map<string, string>();

  const deps: DaemonDeps & { files: Map<string, string> } = {
    files,
    existsSync: (p) => files.has(p),
    mkdirSync: () => {},
    readFileSync: (p) => {
      const v = files.get(p);
      if (v === undefined) throw new Error(`ENOENT: ${p}`);
      return v;
    },
    rmSync: (p) => {
      files.delete(p);
    },
    writeFileSync: (p, content) => {
      files.set(p, content);
    },
    kill: () => {
      throw Object.assign(new Error("ESRCH"), { code: "ESRCH" });
    },
    // What `ps -ww -p <pid> -o command=` prints for a real wrapper (verified
    // against a live one: the whole script text plus its positional args -- only
    // with `-ww`; without it `ps` cuts the line at `$COLUMNS` -- so
    // both `-m tap watch` and the `probe-research-tap` prefix are in there).
    // Tests that care about the identity check override this.
    commandForPid: () =>
      '/bin/sh -c SID="$1"; ... "$PY" -m tap watch --session-id "$SID" ... sh sess /repo ' +
      "/usr/bin/python3 /tmp/sess.log /tmp/probe-research-tap-watcher-sess.pid probe-research-tap",
    // No session here is the Probe service's unless a test says so (and no
    // test reads the real /tmp marker by accident).
    hostedServicePid: () => null,
    log: () => {},
    // Instant by default: nothing in this suite pays real wall-clock time.
    sleep: async () => {},
    ...overrides,
  };
  return deps;
}


describe("isDaemonAlive", () => {
  it("is false when no pidfile exists", () => {
    const deps = fakeDeps();
    expect(isDaemonAlive("nope", deps)).toBe(false);
  });

  it("is false when the pidfile's pid is not signalable (process gone)", () => {
    const deps = fakeDeps();
    deps.files.set(pidFile("dead"), "123");
    expect(isDaemonAlive("dead", deps)).toBe(false);
  });

  it("is true when kill(pid, 0) succeeds", () => {
    const deps = fakeDeps({ kill: () => {} });
    deps.files.set(pidFile("live"), "123");
    expect(isDaemonAlive("live", deps)).toBe(true);
  });

  it("is false for a malformed pidfile", () => {
    const deps = fakeDeps({ kill: () => {} });
    deps.files.set(pidFile("garbage"), "not-a-pid");
    expect(isDaemonAlive("garbage", deps)).toBe(false);
  });

  // The identity half of the rule, and the reason it exists: `tap start`
  // (tap/start.py::daemon_state) treats a live pid that is NOT the tap as
  // STALE, not as `running`. If this function stopped at kill(pid, 0) the two
  // would disagree about the word "alive" — and the disagreement is silent and
  // one-directional: spawnDaemon would return `already-running` for a recycled
  // pid and never call the spawner that would have corrected it.
  it("is false when the pid is alive but is not the tap (pid reuse or a planted pidfile)", () => {
    const deps = fakeDeps({
      kill: () => {},
      commandForPid: () => "/Applications/Firefox.app/Contents/MacOS/firefox",
    });
    deps.files.set(pidFile("impostor"), "123");
    expect(isDaemonAlive("impostor", deps)).toBe(false);
  });

  it("is false when the process cannot be identified at all", () => {
    const deps = fakeDeps({ kill: () => {}, commandForPid: () => null });
    deps.files.set(pidFile("unidentifiable"), "123");
    expect(isDaemonAlive("unidentifiable", deps)).toBe(false);
  });

  it("is true for a session the Probe service records (no pid file, a live marker)", () => {
    expect(isDaemonAlive("hosted", fakeDeps({ hostedServicePid: () => 9001 }))).toBe(true);
  });

  it("falls through a dead watcher's pid file to the service's marker", () => {
    const deps = fakeDeps({ hostedServicePid: () => 9001 });
    deps.files.set(pidFile("dead-then-hosted"), "123");
    expect(isDaemonAlive("dead-then-hosted", deps)).toBe(true);
  });
});

describe("finishHostedSession", () => {
  /** The service's `session.finish` as pi asks it: every call recorded; `answer` how it settles. */
  function finishDeps(overrides: Partial<DaemonDeps> = {}, answer: () => Promise<unknown> = async () => ({ finished: true })) {
    const deps = fakeDeps(overrides);
    const calls: Array<{ sessionId: string; waitS: number }> = [];
    const finish = vi.fn((sessionId: string, waitS: number) => {
      calls.push({ sessionId, waitS });
      return answer();
    });
    return { deps: { ...deps, finish }, files: deps.files, calls };
  }

  it("does nothing for a session the service does not record", async () => {
    const { deps, calls } = finishDeps({ hostedServicePid: () => 9001 });
    expect(await finishHostedSession("not-hosted", { waitS: 3 }, deps)).toBe(HostedFinish.NotHosted);
    expect(calls).toEqual([]);
  });

  it("on a quit, asks the service to finish it with the wait, then drops the marker: pi spawns no `tap finish`", async () => {
    const { deps, files, calls } = finishDeps({ hostedServicePid: () => 9001 });
    files.set(hostedFile("quit"), "9001");
    expect(await finishHostedSession("quit", { waitS: 3 }, deps)).toBe(HostedFinish.Finished);
    expect(calls).toEqual([{ sessionId: "quit", waitS: 3 }]);
    expect(files.has(hostedFile("quit"))).toBe(false);
  });

  it("holds the quit no longer than its bound, whatever the service does", async () => {
    const { deps, files } = finishDeps({ hostedServicePid: () => 9001 }, () => new Promise(() => {}));
    files.set(hostedFile("wedged"), "9001");
    const started = Date.now();
    expect(await finishHostedSession("wedged", { waitS: 1 }, deps)).toBe(HostedFinish.Finished);
    expect(Date.now() - started).toBeLessThan(1000 + HOSTED_FINISH_GRACE_MS + 500);
    expect(files.has(hostedFile("wedged"))).toBe(false);
  });

  it("on a session switch, asks no finish (the sentinel ends the recorder) and drops the marker", async () => {
    const { deps, files, calls } = finishDeps({ hostedServicePid: () => 9001 });
    files.set(hostedFile("switch"), "9001");
    expect(await finishHostedSession("switch", { waitS: 0 }, deps)).toBe(HostedFinish.MarkerDropped);
    expect(calls).toEqual([]);
    expect(files.has(hostedFile("switch"))).toBe(false);
  });

  it("asks no finish of a service that is gone, and still drops the marker", async () => {
    const gone = finishDeps({ hostedServicePid: () => null });
    gone.files.set(hostedFile("gone"), "9001");
    expect(await finishHostedSession("gone", { waitS: 3 }, gone.deps)).toBe(HostedFinish.MarkerDropped);
    expect(gone.calls).toEqual([]);
    expect(gone.files.has(hostedFile("gone"))).toBe(false);
  });

  it("never throws: a call that fails is logged, and the marker still goes", async () => {
    const logged: string[] = [];
    const { deps, files } = finishDeps({ hostedServicePid: () => 9001, log: (m) => logged.push(m) }, async () => {
      throw new Error("socket EACCES");
    });
    files.set(hostedFile("call-throws"), "9001");
    expect(await finishHostedSession("call-throws", { waitS: 3 }, deps)).toBe(HostedFinish.Finished);
    expect(logged.join("\n")).toContain("socket EACCES");
    expect(files.has(hostedFile("call-throws"))).toBe(false);
  });
});

describe("stopDaemon", () => {
  it("writes the shutdown sentinel, signals nothing, and never deletes it (service-only task 16)", () => {
    const killed: Array<{ pid: number; signal: number | string }> = [];
    const deps = fakeDeps({
      kill: (pid, signal) => {
        killed.push({ pid, signal });
      },
    });
    deps.files.set(pidFile("sess-6"), "555");

    stopDaemon("sess-6", deps);

    expect(deps.files.has(shutdownSentinelFile("sess-6"))).toBe(true);
    expect(killed).toEqual([]);
    expect(deps.files.has(pidFile("sess-6"))).toBe(false);
  });

  it("is a harmless no-op when nothing was ever spawned for this session", () => {
    const deps = fakeDeps();
    expect(() => stopDaemon("never-spawned", deps)).not.toThrow();
    expect(deps.files.has(shutdownSentinelFile("never-spawned"))).toBe(true);
  });
});

describe("pruneStaleShutdownSentinels", () => {
  const TWO_DAYS_MS = 2 * 24 * 60 * 60 * 1000;

  function fakePruneDeps(entries: Record<string, number>, now: number): PruneDeps & { removed: string[] } {
    const removed: string[] = [];
    return {
      removed,
      readdirSync: () => Object.keys(entries),
      statMtimeMs: (path) => {
        const name = path.split("/").pop()!;
        if (!(name in entries)) throw new Error("ENOENT");
        return entries[name];
      },
      rmSync: (path) => {
        removed.push(path);
      },
      now: () => now,
    };
  }

  it("removes only .shutdown sentinels older than two days", () => {
    const now = 10_000_000_000;
    const deps = fakePruneDeps(
      {
        "probe-research-tap-watcher-old-session.shutdown": now - TWO_DAYS_MS - 1,
        "probe-research-tap-watcher-fresh-session.shutdown": now - 1000,
        "probe-research-tap-watcher-old-session.pid": now - TWO_DAYS_MS - 1, // not a .shutdown file
        "unrelated-file.shutdown": now - TWO_DAYS_MS - 1, // wrong prefix
      },
      now,
    );

    pruneStaleShutdownSentinels(deps);

    expect(deps.removed).toEqual(["/tmp/probe-research-tap-watcher-old-session.shutdown"]);
  });

  it("never throws when /tmp cannot be read", () => {
    const deps: PruneDeps = {
      readdirSync: () => {
        throw new Error("permission denied");
      },
      statMtimeMs: () => 0,
      rmSync: () => {},
      now: () => Date.now(),
    };
    expect(() => pruneStaleShutdownSentinels(deps)).not.toThrow();
  });

  it("skips a file that disappears between listing and stat (race with a real cleanup)", () => {
    const deps: PruneDeps = {
      readdirSync: () => ["probe-research-tap-watcher-raced.shutdown"],
      statMtimeMs: () => {
        throw new Error("ENOENT");
      },
      rmSync: () => {
        throw new Error("should not be called");
      },
      now: () => Date.now(),
    };
    expect(() => pruneStaleShutdownSentinels(deps)).not.toThrow();
  });
});

describe("watcherDir", () => {
  it("is the folder the tap's watcher-dir variable names on every OS, else /tmp (the user's temp folder on Windows)", () => {
    // The tap reads PROBE_TAP_WATCHER_DIR on every OS from 0.10.19 (`tap/config.py::_watcher_dir`):
    // pi must look where the tap it spawns writes, or a test's files land in the shared /tmp.
    expect(WATCHER_DIR_ENV).toBe("PROBE_TAP_WATCHER_DIR");
    expect(watcherDir(OsPlatform.Linux, { [WATCHER_DIR_ENV]: "/srv/watchers" })).toBe("/srv/watchers");
    expect(watcherDir(OsPlatform.Mac, { [WATCHER_DIR_ENV]: "/srv/watchers" })).toBe("/srv/watchers");
    expect(watcherDir(OsPlatform.Windows, { [WATCHER_DIR_ENV]: "C:\\Users\\r\\AppData\\Local\\Temp" })).toBe(
      "C:\\Users\\r\\AppData\\Local\\Temp",
    );
    expect(watcherDir(OsPlatform.Linux, {})).toBe("/tmp");
    expect(watcherDir(OsPlatform.Linux, { [WATCHER_DIR_ENV]: "relative/not-a-folder" })).toBe("/tmp");
    expect(watcherDir(OsPlatform.Windows, {})).toBe(tmpdir());
  });
});

describe("recheckUntilClaimed", () => {
  // #3619: pi's footer read capture once, right after the spawn, while the Probe
  // service's recorder had not claimed the session yet (it did 2.0 s later), and
  // said "not capturing session transcript: not recording" of a captured session.
  const unclaimed: CaptureReading = { running: false, reason: CaptureReason.NotRecording };
  const claimed: CaptureReading = { running: true, reason: "running" };
  type Answer = CaptureReading | undefined | typeof ClaimRecheck.Stop;

  function harness(answers: Answer[]) {
    const waits: number[] = [];
    const pending: (() => void)[] = [];
    let reads = 0;
    const deps: ClaimRecheckDeps = {
      reread: async () => answers[reads++],
      later: (fn, ms) => {
        waits.push(ms);
        pending.push(fn);
      },
      log: () => {},
    };
    /** Fire the next wait's look and let its re-read settle. */
    const tick = async () => {
      pending.shift()?.();
      await new Promise((resolve) => setImmediate(resolve));
    };
    return { deps, waits, pending, tick, reads: () => reads };
  }

  it("looks again 3 s, 15 s and 60 s after the start, until the recorder claims it", async () => {
    expect(CLAIM_RECHECK_MS).toEqual([3_000, 12_000, 45_000]);
    const h = harness([unclaimed, claimed]);
    recheckUntilClaimed(unclaimed, h.deps);
    await h.tick();
    await h.tick();
    expect(h.waits).toEqual([3_000, 12_000]);
    expect(h.reads()).toBe(2);
    expect(h.pending, "claimed: no more looks").toEqual([]);
  });

  it("keeps looking after a read that failed, and stops when the session is gone", async () => {
    const failed = harness([undefined, claimed]);
    recheckUntilClaimed(unclaimed, failed.deps);
    await failed.tick();
    await failed.tick();
    expect(failed.reads(), "an unreadable answer is not the end").toBe(2);
    const gone = harness([ClaimRecheck.Stop]);
    recheckUntilClaimed(unclaimed, gone.deps);
    await gone.tick();
    expect(gone.pending).toEqual([]);
  });

  it("stops after its last look", async () => {
    const h = harness([unclaimed, unclaimed, unclaimed]);
    recheckUntilClaimed(unclaimed, h.deps);
    for (let i = 0; i < 4; i++) await h.tick();
    expect(h.reads()).toBe(3);
    expect(h.pending, "a recorder whose server does not answer stays 'not recording'").toEqual([]);
  });

  it("never looks again for a session captured, or not captured for a reason time does not change", () => {
    for (const capture of [undefined, claimed, { running: false, reason: "not paired" }, { running: false, reason: "Probe is off" }]) {
      const h = harness([]);
      recheckUntilClaimed(capture, h.deps);
      expect(h.waits).toEqual([]);
      expect(awaitingClaim(capture)).toBe(false);
    }
    expect(awaitingClaim(unclaimed)).toBe(true);
  });
});

describe("the worker stamp", () => {
  // The Probe service writes `<state>/sessions/<sid>.worker` when it asks for a session's daemon worker and when it
  // gives up on one: the footer tells a daemon still starting from one that never will.
  const env = { XDG_STATE_HOME: "/state" };
  const path = join("/state", "probe", "sessions", "sid-1.worker");
  const read = (content: string | null) => loadWorkerStamp("sid-1", {
    env,
    readFileSync: (asked) => {
      if (asked !== path || content === null) throw new Error("ENOENT");
      return content;
    },
  });

  it("reads the service's stamp, by wall clock", () => {
    expect(read(JSON.stringify({ state: "starting", at: 1_700_000_000.5 }))).toEqual({
      state: WorkerState.Starting,
      atMs: 1_700_000_000_500,
    });
    expect(read(JSON.stringify({ state: "gave_up", at: 1 }))?.state).toBe(WorkerState.GaveUp);
    expect(read(JSON.stringify({ state: "crash_loop", at: 1 }))?.state).toBe(WorkerState.CrashLoop);
    expect(read(JSON.stringify({ state: "exited", at: 1 }))?.state).toBe(WorkerState.Exited);
  });

  it.each([
    { name: "no stamp (an older service)", content: null },
    { name: "not JSON", content: "{" },
    { name: "an unknown state", content: JSON.stringify({ state: "running", at: 1 }) },
    { name: "no time", content: JSON.stringify({ state: "starting" }) },
    { name: "a time that is not a number", content: JSON.stringify({ state: "starting", at: "1" }) },
    { name: "not an object", content: "[]" },
  ])("reads $name as no stamp", ({ content }) => {
    expect(read(content)).toBeNull();
  });

  it("counts as starting for DAEMON_START_GRACE_MS after the service asked, then not", () => {
    expect(DAEMON_START_GRACE_MS).toBe(120_000);
    const at = 1_000_000;
    const starting = { state: WorkerState.Starting, atMs: at };
    expect(getStartingEndMs(starting, at)).toBe(at + DAEMON_START_GRACE_MS);
    expect(getStartingEndMs(starting, at + DAEMON_START_GRACE_MS), "the window's last moment").toBe(at + DAEMON_START_GRACE_MS);
    expect(getStartingEndMs(starting, at + DAEMON_START_GRACE_MS + 1)).toBeNull();
    expect(getStartingEndMs(starting, at - 1), "stamped in the future").toBeNull();
    expect(getStartingEndMs({ state: WorkerState.GaveUp, atMs: at }, at)).toBeNull();
    expect(getStartingEndMs({ state: WorkerState.CrashLoop, atMs: at }, at)).toBeNull();
    // A launch that already died never reads as starting, however fresh its stamp.
    expect(getStartingEndMs({ state: WorkerState.Exited, atMs: at }, at)).toBeNull();
  });
});
