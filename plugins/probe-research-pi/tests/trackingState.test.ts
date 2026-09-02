import { describe, expect, it } from "vitest";
import { vi } from "vitest";

import {
  initializeTrackingState,
  trackingStatusText,
  type TrackingExecFileFn,
} from "../src/trackingState.js";

describe("initializeTrackingState", () => {
  it("delegates initialization to the atomic CLI bridge and parses its state", async () => {
    const calls: Array<{ command: string; args: string[] }> = [];
    const execFile: TrackingExecFileFn = (command, args, _options, callback) => {
      calls.push({ command, args });
      callback(
        null,
        JSON.stringify({
          session_id: "pi-session-1",
          tracking: false,
          signal: "off",
          seeded: true,
          source: "/repo/.probe/config.json",
        }),
        "",
      );
    };

    const state = await initializeTrackingState("pi-session-1", "/repo/packages/app", {
      execFile,
      existsSync: () => true,
      isExecutable: () => true,
      env: { PATH: "/usr/bin", HOME: "/home/x" },
      log: () => {},
    });

    expect(calls).toEqual([
      {
        command: "/usr/bin/probe",
        args: [
          "session",
          "initialize",
          "--session",
          "pi-session-1",
          "--cwd",
          "/repo/packages/app",
        ],
      },
    ]);
    expect(state).toEqual({
      tracking: false,
      signal: "off",
      seeded: true,
      source: "/repo/.probe/config.json",
    });
  });

  it("fails open when the CLI does not return a valid tracking state", async () => {
    const execFile: TrackingExecFileFn = (_command, _args, _options, callback) => {
      callback(null, '{"tracking":"off"}', "");
    };

    const state = await initializeTrackingState("pi-session-1", "/repo", {
      execFile,
      existsSync: () => true,
      isExecutable: () => true,
      env: { PATH: "/usr/bin", HOME: "/home/x" },
      log: () => {},
    });

    expect(state).toBeNull();
  });

  it.each([
    {
      name: "another session",
      payload: {
        session_id: "pi-session-2",
        tracking: false,
        signal: "off",
        seeded: false,
        source: "session",
      },
    },
    {
      name: "a contradictory boolean and signal",
      payload: {
        session_id: "pi-session-1",
        tracking: true,
        signal: "off",
        seeded: false,
        source: "session",
      },
    },
  ])("rejects a payload for $name", async ({ payload }) => {
    const execFile: TrackingExecFileFn = (_command, _args, _options, callback) => {
      callback(null, JSON.stringify(payload), "");
    };

    const state = await initializeTrackingState("pi-session-1", "/repo", {
      execFile,
      existsSync: () => true,
      isExecutable: () => true,
      env: { PATH: "/usr/bin", HOME: "/home/x" },
      log: () => {},
    });

    expect(state).toBeNull();
  });

  it("settles on its own deadline and force-kills a stuck CLI", async () => {
    vi.useFakeTimers();
    try {
      const kill = vi.fn();
      const execFile: TrackingExecFileFn = () => ({ kill });

      const pending = initializeTrackingState("pi-session-1", "/repo", {
        execFile,
        existsSync: () => true,
        isExecutable: () => true,
        env: { PATH: "/usr/bin", HOME: "/home/x" },
        log: () => {},
        timeoutMs: 25,
      });
      await vi.advanceTimersByTimeAsync(25);

      await expect(pending).resolves.toBeNull();
      expect(kill).toHaveBeenCalledWith("SIGKILL");
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not forward unrelated parent-process secrets", async () => {
    let childEnv: Record<string, string | undefined> = {};
    const execFile: TrackingExecFileFn = (_command, _args, options, callback) => {
      childEnv = options.env;
      callback(
        null,
        JSON.stringify({
          session_id: "pi-session-1",
          tracking: true,
          signal: "on",
          seeded: true,
          source: "shipped",
        }),
        "",
      );
    };

    await initializeTrackingState("pi-session-1", "/repo", {
      execFile,
      existsSync: () => true,
      isExecutable: () => true,
      env: {
        PATH: "/usr/bin",
        HOME: "/home/x",
        XDG_STATE_HOME: "/state",
        PRBE_API_KEY: "must-not-leak",
      },
      log: () => {},
    });

    expect(childEnv).toMatchObject({
      PATH: "/usr/bin",
      HOME: "/home/x",
      XDG_STATE_HOME: "/state",
      PROBE_AGENT: "pi",
    });
    expect(childEnv).not.toHaveProperty("PRBE_API_KEY");
  });

  it("fails open when launching the CLI throws synchronously", async () => {
    const execFile: TrackingExecFileFn = () => {
      throw new Error("spawn exploded");
    };

    await expect(
      initializeTrackingState("pi-session-1", "/repo", {
        execFile,
        existsSync: () => true,
        isExecutable: () => true,
        env: { PATH: "/usr/bin", HOME: "/home/x" },
        log: () => {},
      }),
    ).resolves.toBeNull();
  });
});

describe("trackingStatusText", () => {
  it("renders the two persistent footer states", () => {
    expect(trackingStatusText(true)).toBe("● tracking");
    expect(trackingStatusText(false)).toBe("○ not tracking");
  });
});
