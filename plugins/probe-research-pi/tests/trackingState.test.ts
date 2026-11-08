import { describe, expect, it } from "vitest";

import {
  initializeTrackingState,
  loweredSuffix,
  parseLowered,
  parseProfile,
  Recorder,
  SESSION_INITIALIZE,
  trackingStatusText,
  type TrackingStateDeps,
} from "../src/core/trackingState.js";

/** The Probe service as `initializeTrackingState` asks it: `session.initialize` answers `{state}`, the
 * JSON `probe session initialize` prints (the service runs it: service-only task 16). */
function service(state: unknown, calls: Array<{ method: string; params: Record<string, unknown>; waitMs: number }> = []):
  TrackingStateDeps["call"] {
  return async (method, params, waitMs) => {
    calls.push({ method, params, waitMs });
    return state === null ? null : { state };
  };
}

function deps(call: TrackingStateDeps["call"], env: Record<string, string> = { PATH: "/usr/bin", HOME: "/home/x" }): TrackingStateDeps {
  return { call, env, log: () => {} };
}

describe("initializeTrackingState", () => {
  it("asks the Probe service to initialize the session and parses its state: pi spawns no CLI", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown>; waitMs: number }> = [];
    const answer = { session_id: "pi-session-1", tracking: false, signal: "off", seeded: true, source: "/repo/.probe/config.json" };
    const state = await initializeTrackingState("pi-session-1", "/repo/packages/app", deps(service(answer, calls)));
    expect(calls.map(({ method, params }) => [method, params.sid, params.cwd, params.agent])).toEqual([
      [SESSION_INITIALIZE, "pi-session-1", "/repo/packages/app", "pi"],
    ]);
    expect(state).toEqual({
      tracking: false,
      signal: "off",
      // An older CLI sends no `profile`: the session is the agent's.
      profile: Recorder.Agent,
      seeded: true,
      source: "/repo/.probe/config.json",
    });
  });

  it.each([
    { sent: Recorder.Daemon, read: Recorder.Daemon },
    { sent: Recorder.Agent, read: Recorder.Agent },
    { sent: undefined, read: Recorder.Agent },
    { sent: "lean", read: Recorder.Agent },
    { sent: 1, read: Recorder.Agent },
  ])("reads profile $sent as $read, and never fails the payload over it", async ({ sent, read }) => {
    const answer = { session_id: "pi-session-1", tracking: true, signal: "on", seeded: true, source: "machine", profile: sent };
    const state = await initializeTrackingState("pi-session-1", "/repo", deps(service(answer)));
    expect(state?.profile).toBe(read);
    expect(parseProfile(sent)).toBe(read);
  });

  it.each([
    {
      sent: "Probe no longer reads .probe.config (removed 2026-11-04): folders it marked off are recorded.",
      read: "Probe no longer reads .probe.config (removed 2026-11-04): folders it marked off are recorded.",
    },
    { sent: null, read: undefined },
    { sent: "", read: undefined },
    { sent: 3, read: undefined },
    { sent: undefined, read: undefined },
  ])("reads the retired-config notice $sent leniently", async ({ sent, read }) => {
    const answer = {
      session_id: "pi-session-1", tracking: true, signal: "on", seeded: true, source: "machine",
      retired_config_notice: sent,
    };
    const state = await initializeTrackingState("pi-session-1", "/repo", deps(service(answer)));
    expect(state?.tracking).toBe(true);
    expect(state?.retiredConfigNotice).toBe(read);
  });

  it.each([
    { sent: [{ scope: "default", status: "Probe: new chats off" }], read: "Probe: new chats off" },
    { sent: [{ scope: "profile", status: null }, { scope: "sign_in", status: "Probe: signed out" }], read: "Probe: signed out" },
    { sent: [{ status: "Probe: a\nb" }], read: undefined },
    { sent: [], read: undefined },
    { sent: "Probe: new chats off", read: undefined },
    { sent: undefined, read: undefined },
  ])("reads what the agent turned down ($sent) for the footer, leniently (fixes-1107b PR B)", async ({ sent, read }) => {
    const answer = {
      session_id: "pi-session-1", tracking: true, signal: "on", seeded: true, source: "machine",
      recording_lowered: sent,
    };
    const state = await initializeTrackingState("pi-session-1", "/repo", deps(service(answer)));
    expect(state?.tracking).toBe(true);
    expect(state?.lowered).toBe(read);
    expect(parseLowered(sent)).toBe(read);
  });

  it("puts it after the footer's label in the status line's words", () => {
    expect(trackingStatusText(true) + loweredSuffix("Probe: new chats off")).toBe("● on (inline) · new chats off");
    expect(loweredSuffix("Probe: runs only")).toBe(" · runs only");
    expect(loweredSuffix(undefined)).toBe("");
  });

  it.each([
    { name: "an invalid state", state: { tracking: "off" } },
    { name: "another session", state: { session_id: "pi-session-2", tracking: false, signal: "off", seeded: false, source: "session" } },
    { name: "a contradictory boolean and signal",
      state: { session_id: "pi-session-1", tracking: true, signal: "off", seeded: false, source: "session" } },
    { name: "no service (or no reading from it)", state: null },
  ])("reads $name as unknown", async ({ state }) => {
    expect(await initializeTrackingState("pi-session-1", "/repo", deps(service(state)))).toBeNull();
  });

  it("hands the service only what the command reads of pi's environment, and names pi", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown>; waitMs: number }> = [];
    await initializeTrackingState("pi-session-1", "/repo", deps(service(null, calls), {
      PATH: "/usr/bin",
      HOME: "/home/x",
      XDG_STATE_HOME: "/state",
      PI_CODING_AGENT_DIR: "/pi-home",
      PROBE_PI_TAP_PLUGIN_DIR: "/pi-tap",
      PRBE_API_KEY: "must-not-leak",
    }));
    const env = calls[0].params.env as Record<string, string>;
    // pi's home and tap folder: without them the CLI reads capture state from the default folders.
    expect(env).toMatchObject({ XDG_STATE_HOME: "/state", PI_CODING_AGENT_DIR: "/pi-home", PROBE_PI_TAP_PLUGIN_DIR: "/pi-tap" });
    expect(env).not.toHaveProperty("PRBE_API_KEY");
  });

  it("reads a call that throws as unknown", async () => {
    const call: TrackingStateDeps["call"] = async () => {
      throw new Error("socket exploded");
    };
    await expect(initializeTrackingState("pi-session-1", "/repo", deps(call))).resolves.toBeNull();
  });
});

describe("trackingStatusText", () => {
  it("renders the two persistent footer states, never the old \"tracking\" (PR32)", () => {
    expect(trackingStatusText(true)).toBe("● on (inline)");
    expect(trackingStatusText(false)).toBe("○ read only (inline)");
    expect(trackingStatusText(false, undefined, undefined, false, true)).toBe("○ read only (daemon)");
  });

  it("names the switch position in the daemon state", () => {
    expect(trackingStatusText(true, undefined, true)).toBe("● on (daemon)");
    expect(trackingStatusText(true, undefined, false)).toBe("● on (daemon degraded)");
    expect(trackingStatusText(true, { running: true, reason: "running" }, false)).toBe(
      "● on (daemon degraded)",
    );
  });

  it("keeps the short daemon label when capture is down, so the reason reads whole", () => {
    expect(trackingStatusText(true, { running: false, reason: "halted" }, false)).toBe(
      "◐ on (daemon) · not capturing session transcript: halted",
    );
    expect(trackingStatusText(true, { running: false, reason: "halted" })).toBe(
      "◐ on (inline) · not capturing session transcript: halted",
    );
  });
});
