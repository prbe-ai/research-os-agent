import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

import { isDaemonAlive, pruneStaleShutdownSentinels, spawnDaemon, stopDaemon, waitForSpawnConfirmation, type DaemonDeps, type PruneDeps } from "../src/daemon.js";
import { pidFile, shutdownSentinelFile } from "../src/paths.js";
import type { TapRuntime } from "../src/tapRuntime.js";

/**
 * The `tap start` argv, built from the contract the Python side reads too.
 *
 * Three callers in two languages spawn this command. Each used to pin the
 * argv as a literal in its own test, so a renamed flag stayed green
 * everywhere and produced a pi session that captured nothing. The flag names
 * now live in one file; see its `_why`, and `agent/tests/test_spawn_contract.py`
 * for the end that asks `tap start`'s own parser whether it still accepts them.
 */
function expectedTapStartArgv(values: Record<string, string>): string[] {
  const contractPath = join(
    dirname(fileURLToPath(import.meta.url)),
    "..",
    "..",
    "probe-research-tap",
    "spawn-contract.json",
  );
  const contract = JSON.parse(readFileSync(contractPath, "utf-8")) as {
    subcommand: string[];
    flags: Record<string, string>;
    flag_order: string[];
  };
  const argv = [...contract.subcommand];
  for (const name of contract.flag_order) {
    argv.push(contract.flags[name], values[name]);
  }
  return argv;
}

/** A tiny in-memory filesystem + recording spawn, standing in for real fs/child_process. */
function fakeDeps(overrides: Partial<DaemonDeps> = {}): DaemonDeps & { files: Map<string, string>; spawnCalls: Array<{ command: string; args: string[]; options: unknown }> } {
  const files = new Map<string, string>();
  const spawnCalls: Array<{ command: string; args: string[]; options: unknown }> = [];

  const deps: DaemonDeps & { files: Map<string, string>; spawnCalls: typeof spawnCalls } = {
    files,
    spawnCalls,
    spawn: vi.fn((command: string, args: string[], options: unknown) => {
      spawnCalls.push({ command, args, options });
      return { pid: 4242, unref: vi.fn() };
    }),
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
    // What `ps -p <pid> -o command=` prints for a real wrapper (verified
    // against a live one: the whole script text plus its positional args, so
    // both `-m tap watch` and the `probe-research-tap` prefix are in there).
    // Tests that care about the identity check override this.
    commandForPid: () =>
      '/bin/sh -c SID="$1"; ... "$PY" -m tap watch --session-id "$SID" ... sh sess /repo ' +
      "/usr/bin/python3 /tmp/sess.log /tmp/probe-research-tap-watcher-sess.pid probe-research-tap",
    log: () => {},
    // Instant by default so nothing in this suite pays real wall-clock time
    // for the bounded poll in waitForSpawnConfirmation; tests that care about
    // the polling itself override this and assert on call counts instead.
    sleep: async () => {},
    ...overrides,
  };
  return deps;
}

const runtime: TapRuntime = { python: "/usr/bin/python3", tapRoot: "/opt/tap-checkout" };

describe("spawnDaemon", () => {
  it("spawns `tap start`, not its own wrapper script", () => {
    const deps = fakeDeps();

    const result = spawnDaemon(
      {
        sessionId: "01a06383-6f4e-751a-b94c-bcefef19938c",
        transcriptPath: "/Users/oded/.pi/agent/sessions/x/s.jsonl",
        cwd: "/Users/oded/Repos/rosetta-workers/visium",
        runtime: { python: "/usr/bin/python3" },
      },
      deps,
    );

    expect(result).toEqual({ spawned: true, pid: 4242 });
    expect(deps.spawnCalls).toHaveLength(1);
    expect(deps.spawnCalls[0].command).toBe("/usr/bin/python3");
    // Built from the SHARED contract, never retyped here. This argv has three
    // callers in two languages; a literal in each test file is three copies of
    // one contract that nothing compares, which is the exact shape of the bug
    // `tap start` was created to delete. See spawn-contract.json's own note,
    // and agent/tests/test_spawn_contract.py for the other end.
    expect(deps.spawnCalls[0].args).toEqual(
      expectedTapStartArgv({
        session_id: "01a06383-6f4e-751a-b94c-bcefef19938c",
        cwd: "/Users/oded/Repos/rosetta-workers/visium",
        transcript: "/Users/oded/.pi/agent/sessions/x/s.jsonl",
      }),
    );
  });

  it("no longer builds a wrapper script of its own", async () => {
    const mod = await import("../src/daemon.js");
    expect("buildWrapperScript" in mod).toBe(false);
  });

  it("spawns with PROBE_TAP_SOURCE=pi in the child's env", () => {
    const deps = fakeDeps();

    const result = spawnDaemon(
      { sessionId: "sess-1", transcriptPath: "/home/user/.pi/agent/sessions/x/20260101_sess-1.jsonl", cwd: "/repo", runtime },
      deps,
    );

    expect(result.spawned).toBe(true);
    expect(deps.spawnCalls).toHaveLength(1);
    const { command, options } = deps.spawnCalls[0];
    expect(command).toBe("/usr/bin/python3");
    const env = (options as { env: Record<string, string | undefined> }).env;
    expect(env.PROBE_TAP_SOURCE).toBe("pi");
  });

  it("passes --session-id, --cwd and --transcript through to `tap start`'s argv", () => {
    const deps = fakeDeps();

    spawnDaemon(
      { sessionId: "sess-2", transcriptPath: "/tmp/sess-2.jsonl", cwd: "/repo/proj", runtime },
      deps,
    );

    const { command, args } = deps.spawnCalls[0];
    expect(command).toBe("/usr/bin/python3");
    expect(args).toEqual([
      "-m",
      "tap",
      "start",
      "--session-id",
      "sess-2",
      "--cwd",
      "/repo/proj",
      "--transcript",
      "/tmp/sess-2.jsonl",
    ]);
  });

  it("prepends the resolved tap root to PYTHONPATH", () => {
    const deps = fakeDeps();

    spawnDaemon({ sessionId: "sess-3", transcriptPath: "/tmp/sess-3.jsonl", cwd: "/repo", runtime }, deps);

    const env = (deps.spawnCalls[0].options as { env: Record<string, string | undefined> }).env;
    expect(env.PYTHONPATH).toContain("/opt/tap-checkout");
  });

  it("does NOT spawn a second daemon for a session that already has a live watcher (not spawn twice)", () => {
    const deps = fakeDeps({
      kill: () => {
        /* no throw => pid is alive */
      },
    });
    deps.files.set(pidFile("sess-4"), "9999");

    const result = spawnDaemon(
      { sessionId: "sess-4", transcriptPath: "/tmp/sess-4.jsonl", cwd: "/repo", runtime },
      deps,
    );

    expect(result).toEqual({ spawned: false, reason: "already-running" });
    expect(deps.spawnCalls).toHaveLength(0);
  });

  it("still calls the spawner when the pidfile names a live process that is not the tap", () => {
    const deps = fakeDeps({
      kill: () => {
        /* no throw => pid is alive */
      },
      commandForPid: () => "/usr/bin/some-unrelated-daemon",
    });
    deps.files.set(pidFile("sess-4b"), "9999");

    const result = spawnDaemon(
      { sessionId: "sess-4b", transcriptPath: "/tmp/sess-4b.jsonl", cwd: "/repo", runtime },
      deps,
    );

    expect(result.spawned).toBe(true);
    expect(deps.spawnCalls).toHaveLength(1);
  });

  it("leaves a stale shutdown sentinel alone — `tap start` owns clearing it", () => {
    const deps = fakeDeps();
    deps.files.set(shutdownSentinelFile("sess-5"), "");

    spawnDaemon({ sessionId: "sess-5", transcriptPath: "/tmp/sess-5.jsonl", cwd: "/repo", runtime }, deps);

    // Deliberately NOT cleared here any more: the sentinel, the pid file and
    // the wrapper are one lifecycle, and that lifecycle now lives in exactly
    // one place. Clearing it from this side too would be a second copy of the
    // rule, free to drift from the one that matters.
    expect(deps.files.has(shutdownSentinelFile("sess-5"))).toBe(true);
    expect(deps.spawnCalls).toHaveLength(1);
  });
});

describe("waitForSpawnConfirmation", () => {
  it("resolves without sleeping when the pidfile already has content", async () => {
    const deps = fakeDeps();
    const sleepCalls: number[] = [];
    deps.files.set(pidFile("confirmed-immediately"), "123");

    await waitForSpawnConfirmation("confirmed-immediately", {
      ...deps,
      sleep: async (ms) => {
        sleepCalls.push(ms);
      },
    });

    expect(sleepCalls).toEqual([]);
  });

  it("polls until the wrapper writes its pidfile, then stops", async () => {
    const deps = fakeDeps({ kill: () => {} });
    let sleeps = 0;

    await waitForSpawnConfirmation("appears-on-third-poll", {
      ...deps,
      sleep: async () => {
        sleeps += 1;
        // Simulate the wrapper's async `echo $$ >"$PIDF"` landing mid-poll.
        if (sleeps === 3) deps.files.set(pidFile("appears-on-third-poll"), "555");
      },
    });

    expect(sleeps).toBe(3);
  });

  it("logs the spawn-failure diagnostic when the pidfile never appears within the bound", async () => {
    const deps = fakeDeps();
    const logs: string[] = [];
    let sleeps = 0;

    await waitForSpawnConfirmation(
      "never-appears",
      { ...deps, log: (m) => logs.push(m), sleep: async () => { sleeps += 1; } },
      { pollMs: 50, maxPolls: 5 },
    );

    expect(sleeps).toBe(5);
    expect(logs).toHaveLength(1);
    expect(logs[0]).toContain("wrapper wrote no pid file within 0.25s");
    expect(logs[0]).toContain("never-appears");
    expect(logs[0]).toContain("reconciler");
  });

  it("logs the not-alive diagnostic when the pidfile exists but the pid cannot be signaled", async () => {
    const deps = fakeDeps(); // default kill() always throws ESRCH -> "not alive"
    const logs: string[] = [];
    deps.files.set(pidFile("dead-on-arrival"), "999");

    await waitForSpawnConfirmation("dead-on-arrival", { ...deps, log: (m) => logs.push(m) });

    expect(logs).toHaveLength(1);
    expect(logs[0]).toContain("wrapper pid not alive just after spawn");
    expect(logs[0]).toContain("dead-on-arrival");
  });

  it("logs nothing when the pidfile appears and the pid is alive (the quiet, common case)", async () => {
    const deps = fakeDeps({ kill: () => {} });
    const logs: string[] = [];
    deps.files.set(pidFile("healthy"), "42");

    await waitForSpawnConfirmation("healthy", { ...deps, log: (m) => logs.push(m) });

    expect(logs).toEqual([]);
  });

  it("treats a pidfile that is present but empty (write in progress) as not yet confirmed", async () => {
    const deps = fakeDeps({ kill: () => {} });
    deps.files.set(pidFile("empty-write-in-progress"), "");
    let sleeps = 0;

    await waitForSpawnConfirmation("empty-write-in-progress", {
      ...deps,
      sleep: async () => {
        sleeps += 1;
        if (sleeps === 1) deps.files.set(pidFile("empty-write-in-progress"), "77");
      },
    });

    expect(sleeps).toBe(1);
  });

  it("defaults to session-start.sh's own bound: 40 polls of 50ms (2s total)", async () => {
    const deps = fakeDeps();
    let sleeps = 0;
    const sleepMs: number[] = [];

    await waitForSpawnConfirmation("defaults-check", {
      ...deps,
      sleep: async (ms) => {
        sleeps += 1;
        sleepMs.push(ms);
      },
    });

    expect(sleeps).toBe(40);
    expect(new Set(sleepMs)).toEqual(new Set([50]));
  });
});

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
});

describe("stopDaemon", () => {
  it("writes the shutdown sentinel before signaling, and never deletes it", () => {
    const killed: Array<{ pid: number; signal: number | string }> = [];
    const deps = fakeDeps({
      kill: (pid, signal) => {
        killed.push({ pid, signal });
      },
    });
    deps.files.set(pidFile("sess-6"), "555");

    stopDaemon("sess-6", deps);

    expect(deps.files.has(shutdownSentinelFile("sess-6"))).toBe(true);
    expect(killed).toEqual([{ pid: -555, signal: "SIGTERM" }]);
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
