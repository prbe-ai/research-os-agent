import { describe, expect, it } from "vitest";

import {
  renderOffFooter,
  Configured,
  getLoweredNote,
  initializeTrackingState,
  isSignedOut,
  parseLowered,
  parseProfile,
  Recorder,
  renderFooter,
  SESSION_INITIALIZE,
  STATUS_UNAVAILABLE_FOOTER,
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
      // ...and no `auto`: the researcher sets the switch.
      auto: false,
      // ...and no lowering: nobody signed it out.
      signedOut: false,
    });
  });

  it.each([
    { sent: true, read: true },
    { sent: false, read: false },
    { sent: "true", read: false },
    { sent: undefined, read: false },
  ])("reads auto $sent as $read (probe auto mode), and never fails the payload over it", async ({ sent, read }) => {
    const answer = { session_id: "pi-session-1", tracking: false, signal: "off", seeded: true, source: "machine", auto: sent };
    const state = await initializeTrackingState("pi-session-1", "/repo", deps(service(answer)));
    expect(state?.auto).toBe(read);
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
    { sent: [{ scope: "profile", status: null }, { scope: "default", status: "Probe: runs only" }], read: "Probe: runs only" },
    // The sign-out is the whole footer, never a note (`get_lowered_status_word` skips it).
    { sent: [{ scope: "sign_in", status: "Probe: signed out" }, { scope: "default", status: "Probe: new chats off" }],
      read: "Probe: new chats off" },
    { sent: [{ scope: "sign_in", status: "Probe: signed out" }], read: undefined },
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

  it.each([
    { sent: [{ scope: "sign_in", state: "signed_out", status: "Probe: signed out" }], read: true },
    { sent: [{ scope: "default", status: "Probe: new chats off" }], read: false },
    { sent: [], read: false },
    { sent: undefined, read: false },
  ])("reads a sign-out by the agent ($sent) apart from the notes", async ({ sent, read }) => {
    const answer = {
      session_id: "pi-session-1", tracking: true, signal: "on", seeded: true, source: "machine",
      recording_lowered: sent,
    };
    const state = await initializeTrackingState("pi-session-1", "/repo", deps(service(answer)));
    expect(state?.signedOut).toBe(read);
    expect(isSignedOut(sent)).toBe(read);
  });

  it("makes it the footer's note in the status line's words", () => {
    expect(renderFooter({ tracking: true, lowered: "Probe: new chats off" })).toBe("● on (inline) | new chats off");
    expect(getLoweredNote("Probe: runs only")).toBe("runs only");
    expect(getLoweredNote(undefined)).toBe("");
  });

  it.each([
    { sent: Configured.Ok, read: Configured.Ok },
    { sent: Configured.Missing, read: Configured.Missing },
    { sent: Configured.Unreadable, read: Configured.Unreadable },
    { sent: undefined, read: undefined },
    { sent: "broken", read: undefined },
    { sent: 1, read: undefined },
  ])("reads whether Probe is set up ($sent) leniently: unknown is absent, never a guess", async ({ sent, read }) => {
    const answer = { session_id: "pi-session-1", tracking: true, signal: "on", seeded: true, source: "machine", configured: sent };
    const state = await initializeTrackingState("pi-session-1", "/repo", deps(service(answer)));
    expect(state?.tracking).toBe(true);
    expect(state?.configured).toBe(read);
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

describe("renderFooter", () => {
  it("says the status line's words after its prefix, with pi's glyphs (● records, ○ reads)", () => {
    expect(renderFooter({ tracking: true })).toBe("● on (inline)");
    expect(renderFooter({ tracking: false })).toBe("○ read (inline)");
    expect(renderFooter({ tracking: false, daemonReads: true })).toBe("○ read (daemon)");
    expect(renderFooter({ tracking: true, daemonLive: true })).toBe("● on (daemon)");
    expect(STATUS_UNAVAILABLE_FOOTER).toBe("● Probe status unavailable");
  });

  it("says a daemon that is not recording in the note, never in the state words", () => {
    expect(renderFooter({ tracking: true, daemonLive: false })).toBe("● on (daemon) | daemon not recording");
    expect(renderFooter({ tracking: true, capture: { running: true, reason: "running" }, daemonLive: false })).toBe(
      "● on (daemon) | daemon not recording",
    );
    // The researcher set Probe inline: the agent records, so the daemon is nobody's problem.
    expect(renderFooter({ tracking: true, daemonLive: false, inline: true })).toBe("● on (inline)");
  });

  it("carries one note, in the status line's order: capture down, daemon, lowering, transcripts off", () => {
    const down = { running: false, reason: "halted" };
    const off = { running: false, reason: "turned off" };
    const lowered = "Probe: new chats off";
    expect(renderFooter({ tracking: true, capture: down, daemonLive: false, lowered })).toBe(
      "● on (daemon) | session transcript not recorded: halted",
    );
    expect(renderFooter({ tracking: true, capture: down, lowered })).toBe(
      "● on (inline) | session transcript not recorded: halted",
    );
    expect(renderFooter({ tracking: true, daemonLive: false, lowered })).toBe("● on (daemon) | daemon not recording");
    expect(renderFooter({ tracking: true, daemonLive: true, capture: off, lowered })).toBe("● on (daemon) | new chats off");
    // Transcripts turned off on purpose explain a daemon that records nothing: no fault, no `daemon not recording`.
    expect(renderFooter({ tracking: true, daemonLive: false, capture: off })).toBe("● on (daemon) | transcripts off");
    expect(renderFooter({ tracking: true, capture: off })).toBe("● on (inline) | transcripts off");
  });

  it("says nothing about capture or the daemon when the session reads: a lowering only", () => {
    expect(renderFooter({ tracking: false, capture: { running: false, reason: "killswitch" } })).toBe("○ read (inline)");
    expect(renderFooter({ tracking: false, daemonReads: true, lowered: "Probe: runs only" })).toBe(
      "○ read (daemon) | runs only",
    );
  });

  it("keeps the footer one line when a reason is not", () => {
    expect(renderFooter({ tracking: true, capture: { running: false, reason: "a\nb" } })).toBe(
      "● on (inline) | session transcript not recorded: a b",
    );
  });
});

describe("renderFooter in an auto chat (probe auto mode)", () => {
  it("puts auto inside the parentheses, as the status line does", () => {
    expect(renderFooter({ tracking: true, daemonLive: true, isAuto: true })).toBe("● on (daemon, auto)");
    expect(renderFooter({ tracking: true, isAuto: true })).toBe("● on (inline, auto)");
    expect(renderFooter({ tracking: false, isAuto: true })).toBe("○ read (inline, auto)");
    expect(renderFooter({ tracking: false, daemonReads: true, isAuto: true, lowered: "Probe: runs only" }))
      .toBe("○ read (daemon, auto) | runs only");
  });
  it("leaves a manual chat's words as they were", () => {
    expect(renderFooter({ tracking: true, daemonLive: true, isAuto: false })).toBe("● on (daemon)");
  });
});

describe("renderOffFooter", () => {
  it("says off, and auto when the agent sets it", () => {
    expect(renderOffFooter({})).toBe("○ off");
    expect(renderOffFooter({ isAuto: true })).toBe("○ off (auto)");
  });
});
