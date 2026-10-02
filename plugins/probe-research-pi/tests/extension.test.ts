/**
 * Wiring-level tests for src/extension.ts — the layer that binds pairing.ts,
 * tapRuntime.ts and daemon.ts to a real pi ExtensionAPI/ExtensionContext.
 *
 * `node:child_process`'s `spawn` is mocked at the module level (per the task:
 * "Mock the spawn; do not actually launch daemons") — no real process is ever
 * started by this file. Everything else (pairing's token-file reads,
 * tapRuntime's PATH/venv checks, the extension's own log file) goes through
 * REAL temporary files: `PROBE_PI_TAP_PLUGIN_DIR`/`PROBE_PI_TAP_ROOT`/
 * `PROBE_CONFIG_PATH`/`PATH` are pointed at a fresh temp directory per test,
 * so this never touches the real ~/.pi state on the machine running the
 * suite, and never collides with a real daemon on disk.
 */

import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join } from "node:path";

import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const spawnMock = vi.fn(
  (_command: string, _args: string[], _options: { detached: boolean; stdio: string; env: Record<string, string | undefined> }) => ({
    pid: 4242,
    unref: vi.fn(),
    // `session_start` WAITS for its `probe notes sync` child before reading the
    // note (a cold read after a credential switch briefs the session from the
    // previous tenant's file). Exiting immediately here keeps these tests about
    // the daemon, instead of parking each one on the 2s sync timeout.
    once: (event: string, callback: (value: unknown) => void) => {
      if (event === "exit") queueMicrotask(() => callback(0));
    },
  }),
);

/** Daemon spawns only — `session_start` also spawns the team-note sync, which
 * is deliberately independent of pairing, the killswitch and the dedup guard
 * (the note is the team's document, not this session's capture). Assertions
 * about "did we spawn the daemon" have to say which spawn they mean. */
const daemonSpawns = () =>
  spawnMock.mock.calls.filter(([, args]) => !(args[0] === "notes" && args[1] === "sync"));

const execFileMock = vi.fn(
  (
    _command: string,
    args: string[],
    _options: { env: Record<string, string | undefined> },
    callback: (error: Error | null, stdout: string, stderr: string) => void,
  ) => {
    callback(
      null,
      JSON.stringify({
        session_id: args[args.indexOf("--session") + 1],
        tracking: true,
        signal: "on",
        seeded: true,
        source: "shipped",
      }),
      "",
    );
  },
);

/**
 * `daemon.ts::isDaemonAlive` asks `/bin/ps` what a pidfile's pid actually is,
 * so that its answer matches `tap start`'s. No test here plants a pidfile, so
 * this is never reached — it is in the mock so that a test which one day does
 * plant one gets a fake rather than a real `ps` (and, until then, so the mock
 * names everything the modules under test import).
 */
const execFileSyncMock = vi.fn(() => "");

vi.mock("node:child_process", () => ({
  spawn: (command: string, args: string[], options: { detached: boolean; stdio: string; env: Record<string, string | undefined> }) =>
    spawnMock(command, args, options),
  execFileSync: () => execFileSyncMock(),
  execFile: (
    command: string,
    args: string[],
    options: { env: Record<string, string | undefined> },
    callback: (error: Error | null, stdout: string, stderr: string) => void,
  ) => execFileMock(command, args, options, callback),
}));

// Imported AFTER the mock is registered (vitest hoists vi.mock, but the
// dynamic import below keeps intent obvious without relying on hoisting
// semantics for anything except the mock itself).
const { registerExtension } = await import("../src/extension.js");
const { pidFile, shutdownSentinelFile, disabledFile, teamNoteDocumentPath, extensionLogFile } = await import("../src/core/paths.js");
const { MCP_SERVED_VIA_ADAPTER_MESSAGE } = await import("../src/adapterHandoff.js");
const { trackingStatusText } = await import("../src/core/trackingState.js");
const paths = await import("../src/core/paths.js");

type Handler = (event: unknown, ctx: unknown) => Promise<void> | void;

function fakeExtensionAPI(activeTools: string[] = ["read", "bash", "edit", "write"]) {
  const handlers = new Map<string, Handler>();
  const commands = new Map<string, { handler: (args: string, ctx: unknown) => Promise<void> }>();
  let active = [...activeTools];
  const api = {
    on: vi.fn((event: string, handler: Handler) => {
      handlers.set(event, handler);
    }),
    registerCommand: vi.fn((name: string, options: { handler: (args: string, ctx: unknown) => Promise<void> }) => {
      commands.set(name, options);
    }),
    getActiveTools: vi.fn(() => [...active]),
    setActiveTools: vi.fn((names: string[]) => {
      active = [...names];
    }),
  };
  return { api, handlers, commands, activeTools: () => active };
}

function fakeContext(overrides: {
  sessionId: string;
  sessionFile?: string;
  cwd?: string;
  hasUI?: boolean;
  /** pi 1.0 writes a NEW session's file at its first message; false leaves it unwritten. */
  writeSessionFile?: boolean;
}) {
  const notify = vi.fn();
  const setStatus = vi.fn();
  if (overrides.sessionFile && overrides.writeSessionFile !== false && !existsSync(overrides.sessionFile)) {
    writeFileSync(overrides.sessionFile, "");
    createdSessionFiles.push(overrides.sessionFile);
  }
  return {
    hasUI: overrides.hasUI ?? false,
    ui: { notify, setStatus },
    cwd: overrides.cwd ?? "/repo/project",
    sessionManager: {
      getSessionId: () => overrides.sessionId,
      getSessionFile: () => overrides.sessionFile,
    },
    notify,
    setStatus,
  };
}

let tmp: string;
let originalEnv: Record<string, string | undefined>;
const createdSessionFiles: string[] = [];
afterAll(() => {
  for (const path of createdSessionFiles) rmSync(path, { force: true });
});

const ENV_KEYS = [
  "PROBE_PI_TAP_PLUGIN_DIR",
  "PROBE_PI_TAP_TOKEN",
  "PROBE_CONFIG_PATH",
  "PROBE_PI_TAP_ROOT",
  "PATH",
  "PI_CODING_AGENT_DIR",
  // The team-note DOCUMENT lives in the state directory now, one per machine,
  // so `XDG_STATE_HOME` is what isolates it -- `PI_CODING_AGENT_DIR` no longer
  // does. Without this a "no note file exists" test reads the REAL note off
  // the developer's machine and passes or fails on its contents.
  "XDG_STATE_HOME",
  // session_start now also attempts a Probe MCP connect (mcpBridge.ts) —
  // PROBE_MCP_TOKEN must be isolated exactly like PROBE_PI_TAP_TOKEN, or a
  // real one exported in the shell running this suite would make these
  // tests attempt a REAL network connection to the REAL MCP server. See
  // this file's own module docstring and mcpBridge.test.ts's for the same
  // rule stated more fully.
  "PROBE_MCP_TOKEN",
] as const;

function uniqueSessionId(label: string): string {
  return `test-${label}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * `session_start` now awaits `waitForSpawnConfirmation()` (daemon.ts) after a
 * fresh spawn — up to 2s of real `setTimeout`-backed polling for the
 * wrapper's pidfile. `spawn` is mocked in this file (see the module
 * docstring), so no wrapper process ever actually writes that pidfile: every
 * test below that spawns hits the full bound by construction. Fake timers
 * let it exercise that await instantly instead of costing 2s of real
 * wall-clock time per test.
 */
async function invokeSessionStart(handler: Handler, event: unknown, ctx: unknown): Promise<void> {
  vi.useFakeTimers();
  try {
    const result = handler(event, ctx) as Promise<void>;
    await vi.runAllTimersAsync();
    await result;
  } finally {
    vi.useRealTimers();
  }
}

/**
 * `vi.spyOn(process.stderr, "write")` does not reliably intercept calls made
 * from other modules in this codebase's Vitest setup (Vitest's own
 * output-capturing machinery appears to already own that property) — a
 * direct property swap does, so that's what this uses instead of vi.spyOn.
 */
async function captureStderr(fn: () => Promise<void>): Promise<string[]> {
  const chunks: string[] = [];
  const original = process.stderr.write.bind(process.stderr);
  (process.stderr as unknown as { write: typeof process.stderr.write }).write = ((chunk: unknown) => {
    chunks.push(String(chunk));
    return true;
  }) as typeof process.stderr.write;
  try {
    await fn();
  } finally {
    (process.stderr as unknown as { write: typeof process.stderr.write }).write = original;
  }
  return chunks;
}

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-pi-ext-"));
  originalEnv = {};
  for (const key of ENV_KEYS) originalEnv[key] = process.env[key];

  spawnMock.mockClear();
  execFileMock.mockClear();

  // Isolate state dir + probe CLI config from the real machine.
  process.env.PROBE_PI_TAP_PLUGIN_DIR = join(tmp, "state");
  delete process.env.PROBE_PI_TAP_TOKEN;
  process.env.PROBE_CONFIG_PATH = join(tmp, "no-such-config.json");
  // Isolate the team-note document too -- session_start reads it
  // unconditionally now, and this file's own header promises no test
  // ever touches the real ~/.pi state. Same directory also isolates
  // mcpOAuthStateFile() (paths.ts), so the MCP bridge's "any stored OAuth
  // tokens?" check below reads nothing real either.
  process.env.PI_CODING_AGENT_DIR = join(tmp, "pi-agent-dir");
  // The document itself: `<XDG_STATE_HOME>/probe/team-note/probe-team-note.md`.
  process.env.XDG_STATE_HOME = join(tmp, "state");
  // session_start now also attempts a Probe MCP connect — with no bearer
  // token AND no stored OAuth tokens (both isolated above), it degrades
  // immediately with zero network calls (see mcpBridge.ts's
  // connectAndRegisterTools). Deleting this is what makes that hold
  // regardless of the shell this suite happens to run in.
  delete process.env.PROBE_MCP_TOKEN;

  // A fake, always-resolvable tap checkout + interpreter so paired tests
  // don't depend on a real Python being installed on the test machine.
  const tapRoot = join(tmp, "tap-checkout");
  mkdirSync(join(tapRoot, "tap"), { recursive: true });
  writeFileSync(join(tapRoot, "tap", "__init__.py"), "");
  const venvBin = join(tapRoot, ".venv", "bin");
  mkdirSync(venvBin, { recursive: true });
  const fakePython = join(venvBin, "python3");
  writeFileSync(fakePython, "#!/bin/sh\nexit 0\n");
  chmodSync(fakePython, 0o755);
  process.env.PROBE_PI_TAP_ROOT = tapRoot;
});

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (originalEnv[key] === undefined) delete process.env[key];
    else process.env[key] = originalEnv[key];
  }
  rmSync(tmp, { recursive: true, force: true });
});

function pair(): void {
  const stateDir = process.env.PROBE_PI_TAP_PLUGIN_DIR!;
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(join(stateDir, ".token"), "paired-device-token");
}

function installProbeCli(): string {
  const binDir = join(tmp, "bin");
  mkdirSync(binDir, { recursive: true });
  const bin = join(binDir, "probe");
  writeFileSync(bin, "#!/bin/sh\nexit 0\n");
  chmodSync(bin, 0o755);
  process.env.PATH = `${binDir}${delimiter}${process.env.PATH ?? ""}`;
  return bin;
}

describe("registerExtension — session_start", () => {
  it("initializes tracking from the session cwd and renders the persistent footer", async () => {
    const bin = installProbeCli();
    execFileMock.mockImplementationOnce((_command, _args, _options, callback) => {
      callback(
        null,
        JSON.stringify({
          session_id: "pi-folder-session",
          tracking: false,
          signal: "off",
          seeded: true,
          source: "/repo/.probe/config.json",
        }),
        "",
      );
    });
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({
      sessionId: "pi-folder-session",
      sessionFile: undefined,
      cwd: "/repo/packages/app",
      hasUI: true,
    });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    expect(execFileMock).toHaveBeenCalledWith(
      bin,
      [
        "session",
        "initialize",
        "--session",
        "pi-folder-session",
        "--cwd",
        "/repo/packages/app",
      ],
      expect.any(Object),
      expect.any(Function),
    );
    expect(ctx.setStatus).toHaveBeenCalledWith("probe-tracking", "○ not tracking");
  });

  it("continues session startup when the footer renderer throws", async () => {
    pair();
    installProbeCli();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({
      sessionId: "pi-footer-failure",
      sessionFile: "/tmp/pi-footer-failure.jsonl",
      cwd: "/repo",
      hasUI: true,
    });
    ctx.setStatus.mockImplementation(() => {
      throw new Error("renderer unavailable");
    });

    await expect(invokeSessionStart(
      handlers.get("session_start")!,
      { reason: "startup" },
      ctx,
    )).resolves.toBeUndefined();
    expect(ctx.setStatus).toHaveBeenCalledWith("probe-tracking", "● tracking");
    expect(daemonSpawns()).toHaveLength(1);
  });

  it("does not spawn when the device is unpaired, and says so on stderr", async () => {
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("unpaired");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    const chunks = await captureStderr(() => handlers.get("session_start")!({ reason: "startup" }, ctx) as Promise<void>);

    expect(daemonSpawns()).toHaveLength(0);
    expect(chunks.length).toBeGreaterThan(0);
    expect(chunks.join("\n")).toContain("not paired");
  });

  it("spawns the daemon with PROBE_TAP_SOURCE=pi when paired", async () => {
    pair();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("paired");
    const transcriptPath = `/tmp/${sessionId}.jsonl`;
    const ctx = fakeContext({ sessionId, sessionFile: transcriptPath });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);

    expect(daemonSpawns()).toHaveLength(1);
    const [, , options] = daemonSpawns()[0];
    expect(options.env.PROBE_TAP_SOURCE).toBe("pi");

    // Cleanup: don't leave a shutdown-sentinel-free pidfile lying around for
    // other tests/processes that share the real /tmp namespace.
    rmSync(pidFile(sessionId), { force: true });
    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });

  it("does not spawn twice for a second session_start on the same session id", async () => {
    pair();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("dedup");
    const transcriptPath = `/tmp/${sessionId}.jsonl`;
    const ctx = fakeContext({ sessionId, sessionFile: transcriptPath });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);
    // The dedup guard (spawnedSessionIds) returns before any spawn or wait —
    // real timers are fine for this second call.
    await handlers.get("session_start")!({ reason: "reload" }, ctx);

    expect(daemonSpawns()).toHaveLength(1);

    rmSync(pidFile(sessionId), { force: true });
    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });

  it("starts capture once pi writes a new session's file, not never", async () => {
    // pi 1.0 writes a new session's file at its first message, after
    // session_start; `tap start` refuses a missing transcript and nothing
    // retried, so a new interactive session was never captured.
    pair();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("deferred");
    const transcriptPath = join(tmp, `${sessionId}.jsonl`);
    const ctx = fakeContext({ sessionId, sessionFile: transcriptPath, writeSessionFile: false });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);
    expect(daemonSpawns()).toHaveLength(0);

    // Still unwritten at the next event: still waiting, no spawn.
    await handlers.get("tool_call")!({ toolName: "read", input: { path: "x" } }, ctx);
    expect(daemonSpawns()).toHaveLength(0);

    writeFileSync(transcriptPath, "{}\n");
    await invokeSessionStart(handlers.get("agent_settled")!, {}, ctx);
    expect(daemonSpawns()).toHaveLength(1);
    // Once: a later event does not spawn again.
    await handlers.get("agent_settled")!({}, ctx);
    expect(daemonSpawns()).toHaveLength(1);

    rmSync(pidFile(sessionId), { force: true });
    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });

  it("skips sessions with no persisted session file (nothing to tail)", async () => {
    pair();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("inmemory");
    const ctx = fakeContext({ sessionId, sessionFile: undefined });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    expect(daemonSpawns()).toHaveLength(0);
  });

  it("refuses to spawn when the .disabled killswitch is present, even when paired", async () => {
    pair();
    const stateDir = process.env.PROBE_PI_TAP_PLUGIN_DIR!;
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(disabledFile(process.env), "");

    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("killswitch");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    expect(daemonSpawns()).toHaveLength(0);
  });
});

describe("trackingStatusText", () => {
  it("shows tracking when a daemon is live", () => {
    expect(trackingStatusText(true, { running: true, reason: "running" })).toBe("● tracking");
  });

  it("names the reason when tracking is on but capture is not", () => {
    expect(trackingStatusText(true, { running: false, reason: "not paired" })).toBe(
      "◐ tracking · not capturing session transcript: not paired",
    );
  });

  it("says nothing about capture when tracking is off", () => {
    expect(trackingStatusText(false, { running: false, reason: "killswitch" })).toBe(
      "○ not tracking",
    );
  });

  it("falls back to the plain state when the CLI returned no capture block", () => {
    expect(trackingStatusText(true, undefined)).toBe("● tracking");
  });
});

describe("registerExtension — the footer's third state", () => {
  it("renders `tracked, not capturing session transcript` from the CLI's own capture block", async () => {
    installProbeCli();
    execFileMock.mockImplementationOnce((_command, _args, _options, callback) => {
      callback(
        null,
        JSON.stringify({
          session_id: "pi-uncaptured-session",
          tracking: true,
          signal: "on",
          seeded: true,
          source: "shipped",
          capture: { running: false, pid: null, reason: "not paired" },
          effective: "tracked, not capturing session transcript",
        }),
        "",
      );
    });
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({
      sessionId: "pi-uncaptured-session",
      sessionFile: undefined,
      cwd: "/repo",
      hasUI: true,
    });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    expect(ctx.setStatus).toHaveBeenCalledWith(
      "probe-tracking",
      "◐ tracking · not capturing session transcript: not paired",
    );
  });

  it("ignores a malformed capture block rather than failing the whole read", async () => {
    installProbeCli();
    execFileMock.mockImplementationOnce((_command, _args, _options, callback) => {
      callback(
        null,
        JSON.stringify({
          session_id: "pi-malformed-capture",
          tracking: true,
          signal: "on",
          seeded: true,
          source: "shipped",
          capture: { running: "no", reason: 7 },
        }),
        "",
      );
    });
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({
      sessionId: "pi-malformed-capture",
      sessionFile: undefined,
      cwd: "/repo",
      hasUI: true,
    });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    // An older probe CLI has no `capture` key at all, and a broken one may
    // have a bad one. Neither is a reason to blank the footer: tracking is
    // still known, and that is what gets rendered.
    expect(ctx.setStatus).toHaveBeenCalledWith("probe-tracking", "● tracking");
  });

  it("re-reads the footer after the spawn attempt, so a refused spawn is visible", async () => {
    pair();
    installProbeCli();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("post-spawn-refresh");
    const ctx = fakeContext({
      sessionId,
      sessionFile: `/tmp/${sessionId}.jsonl`,
      cwd: "/repo",
      hasUI: true,
    });

    execFileMock.mockClear();
    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);

    // Once before the spawn (seeding the signal), once after it: the capture
    // reading taken before `tap start` has even been asked to run is the one
    // reading guaranteed to be out of date.
    const initializeCalls = execFileMock.mock.calls.filter(
      ([, args]) => args[0] === "session" && args[1] === "initialize",
    );
    expect(initializeCalls).toHaveLength(2);

    rmSync(pidFile(sessionId), { force: true });
    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });
});

describe("registerExtension — tracking switch footer", () => {
  it("refreshes the persistent footer after an interactive switch lands", async () => {
    installProbeCli();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({
      sessionId: "pi-switch-session",
      sessionFile: undefined,
      cwd: "/repo",
      hasUI: true,
    });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    expect(ctx.setStatus).toHaveBeenLastCalledWith("probe-tracking", "● tracking");
    ctx.setStatus.mockClear();
    execFileMock.mockClear();
    execFileMock.mockImplementationOnce((_command, _args, _options, callback) => {
      callback(
        null,
        JSON.stringify({
          session_id: "pi-switch-session",
          tracking: false,
          signal: "off",
          seeded: false,
          source: "session",
        }),
        "",
      );
    });
    spawnMock.mockImplementationOnce(() => ({
      pid: 4242,
      unref: vi.fn(),
      on: (event: string, callback: (value: unknown) => void) => {
        if (event === "exit") queueMicrotask(() => callback(0));
      },
      once: (event: string, callback: (value: unknown) => void) => {
        if (event === "exit") queueMicrotask(() => callback(0));
      },
    }));

    await handlers.get("input")!(
      { text: "/track-work off", source: "interactive" },
      ctx,
    );

    expect(execFileMock).toHaveBeenCalledTimes(1);
    expect(ctx.setStatus).toHaveBeenCalledWith("probe-tracking", "○ not tracking");
  });

  it("clears the old footer when a landed switch cannot be read back", async () => {
    installProbeCli();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({
      sessionId: "pi-switch-readback-failure",
      sessionFile: undefined,
      cwd: "/repo",
      hasUI: true,
    });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    expect(ctx.setStatus).toHaveBeenLastCalledWith("probe-tracking", "● tracking");
    ctx.setStatus.mockClear();
    execFileMock.mockImplementationOnce((_command, _args, _options, callback) => {
      callback(new Error("initializer unavailable"), "", "");
    });
    spawnMock.mockImplementationOnce(() => ({
      pid: 4242,
      unref: vi.fn(),
      on: (event: string, callback: (value: unknown) => void) => {
        if (event === "exit") queueMicrotask(() => callback(0));
      },
      once: (event: string, callback: (value: unknown) => void) => {
        if (event === "exit") queueMicrotask(() => callback(0));
      },
    }));

    await handlers.get("input")!(
      { text: "/track-work off", source: "interactive" },
      ctx,
    );

    expect(ctx.setStatus).toHaveBeenCalledWith("probe-tracking", undefined);
  });
});

describe("registerExtension — D6 MCP bridge stand-down", () => {
  it("skips the Probe MCP bridge and logs the hand-off when pi-mcp-adapter and our package are both installed", async () => {
    pair();
    // `tmp` is what this file passes as `extensionDir` (registerExtension's
    // `packageRoot` for adapterHandoff.ts) two lines below, so listing it
    // directly as a local-path packages entry is "our package is installed."
    // PI_CODING_AGENT_DIR (set in beforeEach, isolated from the real
    // machine) is the user-scope settings.json adapterHandoff.ts reads.
    const agentDir = process.env.PI_CODING_AGENT_DIR!;
    mkdirSync(agentDir, { recursive: true });
    writeFileSync(join(agentDir, "settings.json"), JSON.stringify({ packages: ["npm:pi-mcp-adapter", tmp] }));

    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("standdown");
    const transcriptPath = `/tmp/${sessionId}.jsonl`;
    const ctx = fakeContext({ sessionId, sessionFile: transcriptPath });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);

    const log = readFileSync(extensionLogFile(process.env), "utf-8");
    expect(log).toContain(MCP_SERVED_VIA_ADAPTER_MESSAGE);
    // The only three log lines connectAndRegisterTools' caller can produce
    // ("Probe MCP ready, ...", "Probe MCP unavailable for ...", "Probe MCP
    // bridge threw unexpectedly: ...") never appear -- proof the bridge
    // connector was never invoked at all, not just that it failed quietly.
    expect(log).not.toContain("Probe MCP ready");
    expect(log).not.toContain("Probe MCP unavailable");
    expect(log).not.toContain("Probe MCP bridge threw");

    // Capture itself is untouched by the stand-down -- it's an independent
    // subsystem (see extension.ts's own comment on the split).
    expect(daemonSpawns()).toHaveLength(1);

    rmSync(pidFile(sessionId), { force: true });
    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });

  it("still attempts the Probe MCP bridge when only the adapter is installed (legacy symlink case)", async () => {
    pair();
    const agentDir = process.env.PI_CODING_AGENT_DIR!;
    mkdirSync(agentDir, { recursive: true });
    // Adapter present, but no packages entry names this package at all --
    // the legacy ~/.pi/agent/extensions symlink install D6 must not stand
    // down for.
    writeFileSync(join(agentDir, "settings.json"), JSON.stringify({ packages: ["npm:pi-mcp-adapter"] }));

    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("legacy-symlink");
    const transcriptPath = `/tmp/${sessionId}.jsonl`;
    const ctx = fakeContext({ sessionId, sessionFile: transcriptPath });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);

    const log = readFileSync(extensionLogFile(process.env), "utf-8");
    expect(log).not.toContain(MCP_SERVED_VIA_ADAPTER_MESSAGE);
    // No bearer token and no stored OAuth tokens (isolated in beforeEach) ->
    // the bridge degrades immediately, but it WAS attempted.
    expect(log).toContain("Probe MCP unavailable");

    rmSync(pidFile(sessionId), { force: true });
    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });
});

describe("registerExtension — session_shutdown", () => {
  it("does not touch the daemon on a 'reload' shutdown (same session resumes right after)", async () => {
    pair();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("reload-shutdown");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);
    expect(daemonSpawns()).toHaveLength(1);

    await handlers.get("session_shutdown")!({ reason: "reload" }, ctx);
    // No shutdown sentinel should appear — the daemon was deliberately left running.
    expect(existsSync(shutdownSentinelFile(sessionId))).toBe(false);

    rmSync(pidFile(sessionId), { force: true });
    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });

  it("touches the shutdown sentinel on a real quit", async () => {
    pair();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("quit-shutdown");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);
    await handlers.get("session_shutdown")!({ reason: "quit" }, ctx);

    expect(existsSync(shutdownSentinelFile(sessionId))).toBe(true);

    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });
});

describe("registerExtension — /probe-status command", () => {
  it("registers a status command that reports pairing state", async () => {
    pair();
    const { api, commands } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("status");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    expect(commands.has("probe-status")).toBe(true);

    const chunks = await captureStderr(() => commands.get("probe-status")!.handler("", ctx));

    expect(chunks.join("\n")).toContain("paired:");
  });
});

describe("registerExtension — team note", () => {
  function writeTeamNote(text: string): string {
    const path = teamNoteDocumentPath(process.env);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, text);
    return path;
  }

  /** pi's AGENTS.md, as the wizard leaves it: the researcher's text and Probe's block. */
  function writeAgentsFile(text: string): void {
    const path = paths.piAgentsFile(process.env);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, text);
  }

  const POINTER = "# My rules\n\n<!-- probe-research:begin (managed by `probe wizard`) -->\n<!-- v36 -->\n## Probe Research\n<!-- probe-research:end -->\n";

  // Every test below runs beside the pointer unless it says otherwise.
  beforeEach(() => writeAgentsFile(POINTER));

  const noteSyncs = () => spawnMock.mock.calls.filter(([, args]) => args[0] === "notes" && args[1] === "sync");

  it("neither syncs nor injects the note when pi's AGENTS.md has no Probe block (the opt-out)", async () => {
    installProbeCli();
    writeTeamNote("# Team note\nDo not repeat the June outage.\n");
    writeAgentsFile("# My rules\n");
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId: uniqueSessionId("note-opted-out"), sessionFile: undefined });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    const result = await (handlers.get("before_agent_start") as unknown as BeforeAgentStartHandler)({ systemPrompt: "BASE" }, ctx);
    await handlers.get("agent_settled")!({}, ctx);

    expect(result).toBeUndefined();
    expect(noteSyncs()).toHaveLength(0);
  });

  it("treats a missing AGENTS.md as no pointer, and a damaged block as still opted in", async () => {
    installProbeCli();
    writeTeamNote("damaged-block note");
    rmSync(paths.piAgentsFile(process.env));
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    await handlers.get("agent_settled")!({}, {});
    expect(noteSyncs()).toHaveLength(0);

    writeAgentsFile("# My rules\n<!-- probe-research:end -->\n");
    const ctx = fakeContext({ sessionId: uniqueSessionId("note-damaged"), sessionFile: undefined });
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    const result = await (handlers.get("before_agent_start") as unknown as BeforeAgentStartHandler)({ systemPrompt: "BASE" }, ctx);
    expect(result!.systemPrompt).toContain("damaged-block note");
    expect(noteSyncs()).toHaveLength(1);
  });

  type BeforeAgentStartHandler = (
    event: { systemPrompt: string; prompt?: string },
    ctx: unknown,
  ) => Promise<{ systemPrompt?: string } | undefined>;

  it("injects the cached team note into the system prompt via before_agent_start", async () => {
    const notePath = writeTeamNote("# Team note\nDo not repeat the June outage.\n");
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId: uniqueSessionId("note-inject"), sessionFile: undefined });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    const beforeAgentStart = handlers.get("before_agent_start") as unknown as BeforeAgentStartHandler;
    const result = await beforeAgentStart({ systemPrompt: "BASE PROMPT" }, ctx);

    expect(result).toBeDefined();
    expect(result!.systemPrompt).toContain("BASE PROMPT");
    expect(result!.systemPrompt).toContain("Do not repeat the June outage.");
    expect(result!.systemPrompt).toContain(notePath);
  });

  it("does not re-read the file on every before_agent_start -- cached once at session_start", async () => {
    writeTeamNote("original note");
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId: uniqueSessionId("note-cache"), sessionFile: undefined });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    // Mutate the file AFTER the cache is populated. If before_agent_start
    // re-read the file per turn, both calls below would see this instead.
    writeTeamNote("CHANGED note -- must not appear this session");

    const beforeAgentStart = handlers.get("before_agent_start") as unknown as BeforeAgentStartHandler;
    const first = await beforeAgentStart({ systemPrompt: "P1" }, ctx);
    const second = await beforeAgentStart({ systemPrompt: "P2" }, ctx);

    expect(first!.systemPrompt).toContain("original note");
    expect(second!.systemPrompt).toContain("original note");
    expect(first!.systemPrompt).not.toContain("CHANGED note");
    expect(second!.systemPrompt).not.toContain("CHANGED note");
  });

  it("injects nothing when no team note file exists (fail open)", async () => {
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId: uniqueSessionId("note-absent"), sessionFile: undefined });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    const beforeAgentStart = handlers.get("before_agent_start") as unknown as BeforeAgentStartHandler;
    const result = await beforeAgentStart({ systemPrompt: "BASE" }, ctx);

    expect(result).toBeUndefined();
  });

  it("starts the reads poller at settle once the daemon serves, so an asked answer arrives while pi is idle", async () => {
    // A daemon that came up mid-turn (a new session's capture starts at its
    // first tool call) served nothing at the prompt, so the poller never
    // started and the answer waited for the researcher's next prompt.
    const { api, handlers } = fakeExtensionAPI();
    const sendMessage = vi.fn();
    (api as unknown as { sendMessage: typeof sendMessage }).sendMessage = sendMessage;
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("settle-reads");
    const ctx = { ...fakeContext({ sessionId, sessionFile: undefined }), isIdle: () => true };
    const reads = join(paths.probeStateDir(process.env), "reads");
    mkdirSync(join(reads, "status"), { recursive: true });
    writeFileSync(join(reads, "status", `${sessionId}.json`), JSON.stringify({ state: "ok", reason: "", since: 1 }));
    mkdirSync(join(reads, "messages", sessionId), { recursive: true });
    writeFileSync(
      join(reads, "messages", sessionId, `${"1".padStart(20, "0")}-a1.json`),
      JSON.stringify({
        id: "a1", session: sessionId, kind: "answer", text: "66.8%", ask: "aq1", question: "best?",
        reason: null, made_at: 1, expires_at: Date.now() / 1000 + 3600,
      }),
    );
    vi.useFakeTimers();
    try {
      await handlers.get("agent_settled")!({}, ctx);
      await vi.advanceTimersByTimeAsync(2_500);
    } finally {
      vi.useRealTimers();
      await handlers.get("session_shutdown")!({ reason: "quit" }, ctx);
    }
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(sendMessage.mock.calls[0][0].content).toContain("66.8%");
  });

  it("never registers a turn_end handler -- only agent_settled can trigger a sync", () => {
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);

    expect(handlers.has("agent_settled")).toBe(true);
    expect(handlers.has("turn_end")).toBe(false);
  });

  it("agent_settled spawns a detached `probe notes sync` with PROBE_AGENT=pi", async () => {
    const binDir = join(tmp, "bin");
    mkdirSync(binDir, { recursive: true });
    const bin = join(binDir, "probe");
    writeFileSync(bin, "#!/bin/sh\nexit 0\n");
    chmodSync(bin, 0o755);
    const previousPath = process.env.PATH;
    process.env.PATH = `${binDir}${delimiter}${previousPath ?? ""}`;

    try {
      const { api, handlers } = fakeExtensionAPI();
      registerExtension(api as never, tmp);
      spawnMock.mockClear();

      await handlers.get("agent_settled")!({}, {});

      const call = spawnMock.mock.calls.find(([cmd]) => cmd === bin);
      expect(call).toBeDefined();
      const [, args, options] = call!;
      expect(args).toEqual(["notes", "sync"]);
      expect(options.detached).toBe(true);
      expect(options.stdio).toBe("ignore");
      expect(options.env.PROBE_AGENT).toBe("pi");
    } finally {
      process.env.PATH = previousPath;
    }
  });

  it("fails open and silent on agent_settled when no probe CLI can be found anywhere", async () => {
    const previousPath = process.env.PATH;
    const previousHome = process.env.HOME;
    const emptyPath = join(tmp, "empty-path-dir");
    const emptyHome = join(tmp, "empty-home-dir");
    mkdirSync(emptyPath, { recursive: true });
    mkdirSync(emptyHome, { recursive: true });
    // Both PATH and the fallback-candidate HOME must be isolated: this real
    // dev machine has an actual `probe` CLI installed, and findProbeBinary's
    // documented fallbacks (~/.local/bin/probe, the uv tool-install path) are
    // real paths under the real $HOME -- only overriding PATH would let this
    // test pass by accident on a machine that has probe, and fail on one
    // that does not.
    process.env.PATH = emptyPath;
    process.env.HOME = emptyHome;

    try {
      const { api, handlers } = fakeExtensionAPI();
      registerExtension(api as never, tmp);
      spawnMock.mockClear();

      await expect(handlers.get("agent_settled")!({}, {})).resolves.not.toThrow();
      expect(daemonSpawns()).toHaveLength(0);
    } finally {
      process.env.PATH = previousPath;
      process.env.HOME = previousHome;
    }
  });
});


describe("registerExtension — the daemon state", () => {
  type BeforeAgentStart = (
    event: { systemPrompt: string },
    ctx: unknown,
  ) => Promise<{ systemPrompt?: string; message?: { content: string } } | undefined>;

  async function startInDaemon(sessionId: string, withKey: boolean) {
    installProbeCli();
    const config = join(tmp, "config.json");
    writeFileSync(
      config,
      JSON.stringify({ current_context: "default", contexts: { default: withKey ? { companion_token: "k" } : {} } }),
    );
    process.env.PROBE_CONFIG_PATH = config;
    execFileMock.mockImplementationOnce((_command, _args, _options, callback) => {
      callback(
        null,
        JSON.stringify({ session_id: sessionId, tracking: true, signal: "on", state: "daemon", seeded: true, source: "machine" }),
        "",
      );
    });
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId, sessionFile: undefined });
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    return { before: handlers.get("before_agent_start") as unknown as BeforeAgentStart, ctx };
  }

  it("tells the model the daemon records, and says once when it does not", async () => {
    const { before, ctx } = await startInDaemon(uniqueSessionId("daemon-live"), true);
    const first = await before({ systemPrompt: "BASE" }, ctx);
    expect(first!.systemPrompt).toContain("The Probe daemon is recording this session");
    expect(first!.message).toBeUndefined(); // one prompt of grace for the worker to start
    const second = await before({ systemPrompt: "BASE" }, ctx);
    expect(second!.message!.content).toContain("the daemon is not running, so recording is back with you");
    // ...and from then on the prompt stops claiming it records.
    expect(second!.systemPrompt ?? "").not.toContain("The Probe daemon is recording this session");
    const third = await before({ systemPrompt: "BASE" }, ctx);
    expect(third?.message).toBeUndefined(); // announced once
  });

  it("tells the model the whole split on the turn the switch lands on daemon", async () => {
    // A session that did not START in `daemon` never saw the daemon's
    // session-start text, so the turn after `/probe on` lands on `daemon` (the
    // CLI stores `on` as `daemon` where the daemon records) is where it must
    // arrive, in full.
    const { DAEMON_CONTEXT } = await import("../src/core/daemonNotice.js");
    installProbeCli();
    const config = join(tmp, "config.json");
    writeFileSync(
      config,
      JSON.stringify({ current_context: "default", contexts: { default: { companion_token: "k" } } }),
    );
    process.env.PROBE_CONFIG_PATH = config;
    const sessionId = uniqueSessionId("daemon-flip");
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId, sessionFile: undefined });
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    const before = handlers.get("before_agent_start") as unknown as BeforeAgentStart;
    const onTurn = await before({ systemPrompt: "BASE" }, ctx);
    expect(onTurn?.systemPrompt ?? "").not.toContain(DAEMON_CONTEXT);

    spawnMock.mockImplementationOnce(() => ({
      pid: 4242,
      unref: vi.fn(),
      on: (event: string, callback: (value: unknown) => void) => {
        if (event === "exit") queueMicrotask(() => callback(0));
      },
      once: (event: string, callback: (value: unknown) => void) => {
        if (event === "exit") queueMicrotask(() => callback(0));
      },
    }));
    execFileMock.mockImplementationOnce((_command, _args, _options, callback) => {
      callback(
        null,
        JSON.stringify({ session_id: sessionId, tracking: true, signal: "on", state: "daemon", seeded: true, source: "session" }),
        "",
      );
    });
    await handlers.get("input")!({ text: "/probe on", source: "interactive" }, ctx);

    const flipTurn = await before({ systemPrompt: "BASE" }, ctx);
    expect(flipTurn!.systemPrompt).toContain(DAEMON_CONTEXT);
    for (const word of ["project", "experiment", "group", "run end", "--directed"]) {
      expect(DAEMON_CONTEXT).toContain(word);
    }
  });

  it("moves nothing on a typed `/probe daemon`: the daemon is the wizard's", async () => {
    installProbeCli();
    const sessionId = uniqueSessionId("daemon-typed");
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId, sessionFile: undefined });
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    const switchCalls = () =>
      spawnMock.mock.calls.filter(([, args]) => Array.isArray(args) && args[0] === "session" && args[1] === "state");
    const before = switchCalls().length;
    await handlers.get("input")!({ text: "/probe daemon", source: "interactive" }, ctx);
    expect(switchCalls()).toHaveLength(before);
  });

  it("never claims a keyless daemon records", async () => {
    const { before, ctx } = await startInDaemon(uniqueSessionId("daemon-keyless"), false);
    const first = await before({ systemPrompt: "BASE" }, ctx);
    expect(first!.systemPrompt ?? "").not.toContain("The Probe daemon is recording this session");
    expect(first!.message!.content).toContain("the daemon is not running");
  });
});

describe("registerExtension — the daemon profile", () => {
  const PACKAGE_ROOT = dirname(dirname(new URL(import.meta.url).pathname));
  const EVERY_SKILL = ["probe", "track-work", "visualize-progress", "instrument-code", "audit-team-note", "edit-notes", "notes-audit"];
  const defaultExecFile = execFileMock.getMockImplementation()!;

  type BeforeAgentStart = (
    event: { systemPrompt: string; systemPromptOptions?: { skills: Array<{ name: string; filePath: string }> } },
    ctx: unknown,
  ) => Promise<{ systemPrompt?: string; message?: { content: string } } | undefined>;

  /** `session initialize` answers with this payload (plus the asked session id) until the test ends. */
  function cliReports(extra: Record<string, unknown>): void {
    execFileMock.mockImplementation((_command, args, _options, callback) => {
      callback(
        null,
        JSON.stringify({
          session_id: args[args.indexOf("--session") + 1],
          tracking: true,
          signal: "on",
          seeded: true,
          source: "machine",
          ...extra,
        }),
        "",
      );
    });
  }

  function skills() {
    return EVERY_SKILL.map((name) => ({
      name,
      description: name,
      filePath: join(PACKAGE_ROOT, "skills", name, "SKILL.md"),
      baseDir: join(PACKAGE_ROOT, "skills", name),
    }));
  }

  afterEach(() => {
    execFileMock.mockImplementation(defaultExecFile);
  });

  it("asks the CLI again on every session_start reason, so a resumed session re-reads its profile", async () => {
    installProbeCli();
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId: uniqueSessionId("every-reason"), sessionFile: undefined });
    for (const reason of ["startup", "reload", "new", "resume", "fork"]) {
      await handlers.get("session_start")!({ reason }, ctx);
    }
    const initializes = execFileMock.mock.calls.filter(([, args]) => args[0] === "session" && args[1] === "initialize");
    expect(initializes).toHaveLength(5);
  });

  it("offers no Probe MCP tool: the bridge stays off and an adapter's tools are deactivated", async () => {
    installProbeCli();
    cliReports({ state: "daemon", profile: "daemon" });
    const { api, handlers, activeTools } = fakeExtensionAPI(["read", "bash", "probe-research-pi__probe_browse", "probe_mcp_entity"]);
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId: uniqueSessionId("no-mcp"), sessionFile: undefined });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    expect(activeTools()).toEqual(["read", "bash"]);
    const log = readFileSync(extensionLogFile(process.env), "utf-8");
    expect(log).toContain("daemon profile, no Probe MCP tools");
    expect(log).not.toContain("Probe MCP unavailable");

    // pi-mcp-adapter registers on its own schedule: a tool that arrives later
    // is gone by the next prompt.
    api.setActiveTools([...activeTools(), "probe-research-pi__probe_search_knowledge"]);
    await (handlers.get("before_agent_start") as unknown as BeforeAgentStart)({ systemPrompt: "BASE" }, ctx);
    expect(activeTools()).toEqual(["read", "bash"]);
  });

  it("adds the daemon's probe skill through resources_discover, and only in the daemon profile", async () => {
    installProbeCli();
    cliReports({ state: "daemon", profile: "daemon" });
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, PACKAGE_ROOT);
    const daemonCtx = fakeContext({ sessionId: uniqueSessionId("discover-daemon"), sessionFile: undefined });
    await handlers.get("session_start")!({ reason: "startup" }, daemonCtx);

    const discovered = (await handlers.get("resources_discover")!({ type: "resources_discover", cwd: "/repo", reason: "startup" }, daemonCtx)) as unknown as {
      skillPaths: string[];
    };
    expect(discovered.skillPaths).toEqual([join(PACKAGE_ROOT, "daemon-skills", "probe")]);
    expect(existsSync(join(discovered.skillPaths[0], "SKILL.md"))).toBe(true);

    // The next session is the agent's (an older CLI sends no profile): the
    // previous session's profile must not carry over.
    cliReports({});
    const agentCtx = fakeContext({ sessionId: uniqueSessionId("discover-agent"), sessionFile: undefined });
    await handlers.get("session_start")!({ reason: "new" }, agentCtx);
    expect(await handlers.get("resources_discover")!({ type: "resources_discover", cwd: "/repo", reason: "startup" }, agentCtx)).toBeUndefined();
  });

  it("lists only the lean skills, with the daemon's probe, and tells the model nothing about the daemon", async () => {
    const { DAEMON_CONTEXT } = await import("../src/core/daemonNotice.js");
    installProbeCli();
    const config = join(tmp, "config.json");
    writeFileSync(config, JSON.stringify({ current_context: "default", contexts: { default: { companion_token: "k" } } }));
    process.env.PROBE_CONFIG_PATH = config;
    cliReports({ state: "daemon", profile: "daemon" });
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, PACKAGE_ROOT);
    const ctx = fakeContext({ sessionId: uniqueSessionId("lean-prompt"), sessionFile: undefined, hasUI: true });
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    const before = handlers.get("before_agent_start") as unknown as BeforeAgentStart;

    const results = [];
    for (let turn = 0; turn < 3; turn++) {
      const event = { systemPrompt: "BASE", systemPromptOptions: { skills: skills() } };
      results.push({ event, result: await before(event, ctx) });
    }

    const lean = results[0].event.systemPromptOptions.skills;
    expect(lean.map((skill) => skill.name).sort()).toEqual(["instrument-code", "probe"]);
    expect(lean.find((skill) => skill.name === "probe")!.filePath).toBe(join(PACKAGE_ROOT, "daemon-skills", "probe", "SKILL.md"));
    for (const { result } of results) {
      expect(result?.systemPrompt ?? "").not.toContain(DAEMON_CONTEXT);
      expect(result?.message).toBeUndefined();
    }
    // No worker ever took a lease: one prompt of grace, then the RESEARCHER
    // hears it, once, and the footer says degraded.
    const warnings = ctx.notify.mock.calls.filter(([, level]) => level === "warning");
    expect(warnings).toEqual([
      ["Probe: the daemon is not running, so it is not recording this session. `probe doctor` says why.", "warning"],
    ]);
    expect(ctx.setStatus).toHaveBeenLastCalledWith("probe-tracking", trackingStatusText(true, undefined, false));
  });

  it("leaves the agent profile's skills exactly as pi loaded them", async () => {
    installProbeCli();
    cliReports({ state: "full", profile: "agent" });
    const { api, handlers, activeTools } = fakeExtensionAPI(["read", "probe_mcp_entity"]);
    registerExtension(api as never, PACKAGE_ROOT);
    const ctx = fakeContext({ sessionId: uniqueSessionId("agent-skills"), sessionFile: undefined });
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    const event = { systemPrompt: "BASE", systemPromptOptions: { skills: skills() } };
    await (handlers.get("before_agent_start") as unknown as BeforeAgentStart)(event, ctx);
    expect(event.systemPromptOptions.skills.map((skill) => skill.name)).toEqual(EVERY_SKILL);
    expect(activeTools()).toEqual(["read", "probe_mcp_entity"]);
  });

  it("does not open Probe's MCP from /probe-mcp-login", async () => {
    installProbeCli();
    cliReports({ state: "daemon", profile: "daemon" });
    const { api, handlers, commands } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = { ...fakeContext({ sessionId: uniqueSessionId("mcp-login"), sessionFile: undefined, hasUI: true }), ui: { notify: vi.fn(), setStatus: vi.fn(), input: vi.fn() } };
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    await captureStderr(() => commands.get("probe-mcp-login")!.handler("", ctx));
    expect(ctx.ui.input).not.toHaveBeenCalled();
    expect(ctx.ui.notify.mock.calls[0][0]).toContain("Probe daemon reads for this session");
  });
});

describe("registerExtension — the guard", () => {
  const defaultExecFile = execFileMock.getMockImplementation()!;
  afterEach(() => {
    execFileMock.mockImplementation(defaultExecFile);
  });

  type ToolCall = (event: { toolName: string; input: Record<string, unknown> }, ctx: unknown) => Promise<{ block?: boolean; reason?: string } | undefined>;

  async function start(profile: string | undefined) {
    installProbeCli();
    execFileMock.mockImplementation((_command, args, _options, callback) => {
      callback(
        null,
        JSON.stringify({ session_id: args[args.indexOf("--session") + 1], tracking: true, signal: "on", seeded: true, source: "machine", profile }),
        "",
      );
    });
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId: uniqueSessionId(`guard-${profile}`), sessionFile: undefined, cwd: tmp });
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    return { toolCall: handlers.get("tool_call") as unknown as ToolCall, ctx };
  }

  it("blocks a daemon-profile session's probe read and lets its probe ask run", async () => {
    const { toolCall, ctx } = await start("daemon");
    const refused = await toolCall({ toolName: "bash", input: { command: "probe run list" } }, ctx);
    expect(refused).toMatchObject({ block: true });
    expect(refused!.reason).toContain("`probe run list` was refused before it ran");
    expect(await toolCall({ toolName: "bash", input: { command: "probe ask hi" } }, ctx)).toBeUndefined();
  });

  it("blocks an agent-profile session's forged answer, and nothing else", async () => {
    const { toolCall, ctx } = await start(undefined);
    const answer = join(process.env.XDG_STATE_HOME!, "probe", "approvals", "answers", "x.json");
    const refused = await toolCall({ toolName: "write", input: { path: answer, content: "{}" } }, ctx);
    expect(refused).toMatchObject({ block: true });
    expect(refused!.reason).toContain("Only the researcher answers them");
    expect(await toolCall({ toolName: "bash", input: { command: "probe run list" } }, ctx)).toBeUndefined();
  });
});

describe("registerExtension — the daemon's held questions", () => {
  type BeforeAgentStart = (event: { systemPrompt: string }, ctx: unknown) => Promise<unknown>;

  function holdQuestion(sessionId: string): string {
    const { approvalsDir } = paths;
    const id = "0a1b2c3d";
    mkdirSync(join(approvalsDir(process.env), "requests"), { recursive: true });
    writeFileSync(
      join(approvalsDir(process.env), "requests", `${id}.json`),
      JSON.stringify({
        id,
        session_id: sessionId,
        state: "waiting",
        asked_at: Date.now() / 1000 - 5,
        expires_at: Date.now() / 1000 + 3600,
        question: { header: `Probe ${id}`, question: "Run `python summarize.py`?", yes_label: "Run it", no_label: "Don't run it", terminal_only: false },
      }),
    );
    return join(approvalsDir(process.env), "answers", `${id}.json`);
  }

  it("asks at the prompt, in pi's own dialog, and writes the pick for the daemon", async () => {
    const sessionId = uniqueSessionId("held");
    const answer = holdQuestion(sessionId);
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const select = vi.fn(async () => "Run it");
    const ctx = { ...fakeContext({ sessionId, sessionFile: undefined, hasUI: true }), ui: { notify: vi.fn(), setStatus: vi.fn(), select } };
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    await (handlers.get("before_agent_start") as unknown as BeforeAgentStart)({ systemPrompt: "BASE" }, ctx);
    expect(select).toHaveBeenCalledWith("Run `python summarize.py`?", ["Run it", "Don't run it"]);
    expect(JSON.parse(readFileSync(answer, "utf-8"))).toMatchObject({ answer: "yes", choice: "Run it", channel: "pi-dialog" });
  });

  it("asks nothing in a session with no UI", async () => {
    const sessionId = uniqueSessionId("held-headless");
    const answer = holdQuestion(sessionId);
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId, sessionFile: undefined, hasUI: false });
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    await (handlers.get("before_agent_start") as unknown as BeforeAgentStart)({ systemPrompt: "BASE" }, ctx);
    expect(existsSync(answer)).toBe(false);
  });
});
