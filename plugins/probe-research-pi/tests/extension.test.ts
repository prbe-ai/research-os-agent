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
 * (the note is the team's document, not this session's capture), and the Probe
 * service's `tap service ensure` (core/service.ts). Assertions about "did we
 * spawn the daemon" have to say which spawn they mean. */
const daemonSpawns = () =>
  spawnMock.mock.calls.filter(
    ([, args]) =>
      !(args[0] === "notes" && args[1] === "sync") && !(args[2] === "service" && args[3] === "ensure"),
  );

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

/**
 * The Probe service, as the extension asks it (`serviceForward.callService`): `session.initialize`
 * answers what `probe session initialize` (execFileMock, as these tests have always stood it in)
 * prints, as the service's child would; `session.finish` is recorded. Every call lands in serviceCalls.
 */
const serviceCalls: Array<{ method: string; params: Record<string, unknown> }> = [];
type ExecImpl = Parameters<typeof execFileMock.mockImplementation>[0];
/** The next `session initialize` answers, in order (what `execFileMock.mockImplementationOnce` did
 * before the extension looked up its service socket first, itself a call of execFileMock's). */
const initializeQueue: ExecImpl[] = [];
const initializeOnce = (impl: ExecImpl): void => {
  initializeQueue.push(impl);
};

vi.mock("../src/serviceForward.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/serviceForward.js")>();
  return {
    ...actual,
    callService: async (_socket: string | null, method: string, params: Record<string, unknown>) => {
      serviceCalls.push({ method, params });
      if (method === "session.initialize") {
        const run = initializeQueue.shift() ?? execFileMock;
        return await new Promise((resolve) => {
          run("probe", ["session", "initialize", "--session", String(params.sid), "--cwd", String(params.cwd)],
            { env: params.env as Record<string, string | undefined> }, (error: Error | null, stdout: string) => {
              if (error) {
                resolve(null);
                return;
              }
              try {
                resolve({ state: JSON.parse(stdout) });
              } catch {
                resolve({ state: "not json" });
              }
            });
        });
      }
      if (method === "session.finish") return { recording: true, finished: true };
      return null;
    },
  };
});

// Imported AFTER the mock is registered (vitest hoists vi.mock, but the
// dynamic import below keeps intent obvious without relying on hoisting
// semantics for anything except the mock itself).
const { registerExtension } = await import("./shimHarness.js");
const { pidFile, shutdownSentinelFile, disabledFile, teamNoteDocumentPath, extensionLogFile } = await import("../src/core/paths.js");
const { MCP_SERVED_VIA_ADAPTER_MESSAGE } = await import("../src/adapterHandoff.js");
const { trackingStatusText } = await import("../src/core/trackingState.js");
const paths = await import("../src/core/paths.js");
// The real module, for a stand-in Probe service process (the mock above stands in for every spawn the extension makes).
const realChildProcess = await vi.importActual<typeof import("node:child_process")>("node:child_process");

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
    initializeOnce((_command, _args, _options, callback) => {
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
    await registerExtension(api as never, tmp);
    const ctx = fakeContext({
      sessionId: "pi-folder-session",
      sessionFile: undefined,
      cwd: "/repo/packages/app",
      hasUI: true,
    });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    // Through the Probe service (`session.initialize`, which runs `probe session initialize`): pi spawns no CLI.
    expect(serviceCalls).toContainEqual({
      method: "session.initialize",
      params: expect.objectContaining({ sid: "pi-folder-session", cwd: "/repo/packages/app", agent: "pi" }),
    });
    expect(execFileMock.mock.calls.some(([command]) => command === bin)).toBe(false);
    expect(ctx.setStatus).toHaveBeenCalledWith("probe-tracking", "○ read only (inline)");
  });

  it("says a session switched off is off, in the footer", async () => {
    // Probe off: the footer names the state, as the status line's `off` does
    // (it read "read only (inline)", as for `read`).
    installProbeCli();
    initializeOnce((_command, _args, _options, callback) => {
      callback(
        null,
        JSON.stringify({
          session_id: "pi-off-session",
          tracking: false,
          signal: "off",
          state: "off",
          seeded: false,
          source: "session",
          capture: { running: false, reason: "Probe is off" },
        }),
        "",
      );
    });
    const { api, handlers } = fakeExtensionAPI();
    await registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId: "pi-off-session", sessionFile: undefined, cwd: "/repo", hasUI: true });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    expect(ctx.setStatus).toHaveBeenCalledWith("probe-tracking", "○ off");
  });

  it("continues session startup when the footer renderer throws", async () => {
    pair();
    installProbeCli();
    const { api, handlers } = fakeExtensionAPI();
    await registerExtension(api as never, tmp);
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
    expect(ctx.setStatus).toHaveBeenCalledWith("probe-tracking", "● on (inline)");
    expect(daemonSpawns()).toHaveLength(0);
  });

  it("never starts capture itself: the service asks for the session's recorder (M9)", async () => {
    // Paired or not, killswitch or not, a written file or one pi writes later: pi spawns no `tap
    // start` (the service runs it, gates included, and tells the person an unpaired device).
    for (const [label, setup] of [
      ["unpaired", () => {}],
      ["paired", () => pair()],
      ["killswitch", () => {
        pair();
        mkdirSync(process.env.PROBE_PI_TAP_PLUGIN_DIR!, { recursive: true });
        writeFileSync(disabledFile(process.env), "");
      }],
    ] as const) {
      setup();
      const { api, handlers } = fakeExtensionAPI();
      await registerExtension(api as never, tmp);
      const sessionId = uniqueSessionId(label);
      const transcriptPath = join(tmp, `${sessionId}.jsonl`);
      const ctx = fakeContext({ sessionId, sessionFile: transcriptPath, writeSessionFile: label !== "paired" });
      await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);
      await handlers.get("tool_call")!({ toolName: "read", input: { path: "x" } }, ctx);
      writeFileSync(transcriptPath, "{}\n");
      await invokeSessionStart(handlers.get("agent_settled")!, {}, ctx);
      await handlers.get("session_start")!({ reason: "reload" }, ctx);
      expect(daemonSpawns(), label).toHaveLength(0);
      rmSync(shutdownSentinelFile(sessionId), { force: true });
    }
  });

  it("skips sessions with no persisted session file (nothing to tail)", async () => {
    pair();
    const { api, handlers } = fakeExtensionAPI();
    await registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("inmemory");
    const ctx = fakeContext({ sessionId, sessionFile: undefined });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    expect(daemonSpawns()).toHaveLength(0);
  });
});

describe("trackingStatusText", () => {
  it("shows on (inline) when capture is live", () => {
    expect(trackingStatusText(true, { running: true, reason: "running" })).toBe("● on (inline)");
  });

  it("names the reason when tracking is on but capture is not", () => {
    expect(trackingStatusText(true, { running: false, reason: "not paired" })).toBe(
      "◐ on (inline) · not capturing session transcript: not paired",
    );
  });

  it("says nothing about capture when tracking is off", () => {
    expect(trackingStatusText(false, { running: false, reason: "killswitch" })).toBe(
      "○ read only (inline)",
    );
  });

  it("falls back to the plain state when the CLI returned no capture block", () => {
    expect(trackingStatusText(true, undefined)).toBe("● on (inline)");
  });

  it("says `on (inline)` while the researcher has Probe inline, even over a cached `read`", () => {
    expect(trackingStatusText(true, { running: true, reason: "running" }, undefined, true)).toBe("● on (inline)");
    expect(trackingStatusText(false, { running: true, reason: "running" }, undefined, true)).toBe("● on (inline)");
  });
});

describe("registerExtension — the footer's third state", () => {
  it("renders `tracked, not capturing session transcript` from the CLI's own capture block", async () => {
    installProbeCli();
    initializeOnce((_command, _args, _options, callback) => {
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
    await registerExtension(api as never, tmp);
    const ctx = fakeContext({
      sessionId: "pi-uncaptured-session",
      sessionFile: undefined,
      cwd: "/repo",
      hasUI: true,
    });

    await handlers.get("session_start")!({ reason: "startup" }, ctx);

    expect(ctx.setStatus).toHaveBeenCalledWith(
      "probe-tracking",
      "◐ on (inline) · not capturing session transcript: not paired",
    );
  });

  // #3619: the footer read capture once, right after the spawn, while the Probe
  // service's recorder had not claimed the session yet (~2 s later), and said
  // "not capturing" of a captured session.
  const NOT_RECORDING = { running: false, pid: null, reason: "not recording" };
  const RUNNING = { running: true, pid: null, reason: "running" };

  /** `probe session initialize`'s answers, by session (the last one repeats);
   * `hold(sid, n)`: that session's n-th read waits for `release()`. Every
   * other CLI call keeps the suite's default. */
  function initializeReads(answers: Record<string, { capture: typeof RUNNING; state?: string }[]>) {
    const calls: string[] = [];
    const held: (() => void)[] = [];
    let holdAt: [string, number] | null = null;
    const original = execFileMock.getMockImplementation()!;
    execFileMock.mockImplementation((command, args, options, callback) => {
      if (args[0] !== "session" || args[1] !== "initialize") return original(command, args, options, callback);
      const sid = args[args.indexOf("--session") + 1];
      calls.push(sid);
      const n = calls.filter((c) => c === sid).length;
      const list = answers[sid] ?? [{ capture: RUNNING }];
      const { capture, state = "full" } = list[Math.min(n, list.length) - 1];
      const reply = () =>
        callback(
          null,
          JSON.stringify({ session_id: sid, tracking: state !== "off", signal: state === "off" ? "off" : "on", state,
            seeded: true, source: "shipped", capture }),
          "",
        );
      if (holdAt && holdAt[0] === sid && holdAt[1] === n) held.push(reply);
      else reply();
    });
    return {
      calls: (sid: string) => calls.filter((c) => c === sid).length,
      hold: (sid: string, n: number) => (holdAt = [sid, n]),
      release: () => held.splice(0).forEach((reply) => reply()),
      restore: () => execFileMock.mockImplementation(original),
    };
  }

  it("reads capture again after the start until the recorder claims the session (#3619)", async () => {
    pair();
    installProbeCli();
    const sessionId = uniqueSessionId("claim");
    // The start's read, its read-back after the spawn, then the 3 s look.
    const reads = initializeReads({ [sessionId]: [{ capture: NOT_RECORDING }, { capture: NOT_RECORDING }, { capture: RUNNING }] });
    try {
      const { api, handlers } = fakeExtensionAPI();
      await registerExtension(api as never, tmp);
      const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl`, hasUI: true });

      await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);

      expect(reads.calls(sessionId)).toBe(3);
      expect(ctx.setStatus).toHaveBeenLastCalledWith("probe-tracking", "● on (inline)");
      await handlers.get("agent_settled")!({}, ctx);
      expect(reads.calls(sessionId), "a turn's end reads nothing").toBe(3);
    } finally {
      reads.restore();
      rmSync(pidFile(sessionId), { force: true });
      rmSync(shutdownSentinelFile(sessionId), { force: true });
    }
  });

  it("stops looking once the session ends, and after its last look", async () => {
    pair();
    installProbeCli();
    const ended = uniqueSessionId("claim-ended");
    const never = uniqueSessionId("claim-never");
    const reads = initializeReads({ [ended]: [{ capture: NOT_RECORDING }], [never]: [{ capture: NOT_RECORDING }] });
    vi.useFakeTimers();
    try {
      const { api, handlers } = fakeExtensionAPI();
      await registerExtension(api as never, tmp);
      const ctx = fakeContext({ sessionId: ended, sessionFile: `/tmp/${ended}.jsonl`, hasUI: true });
      const started = handlers.get("session_start")!({ reason: "startup" }, ctx);
      await vi.advanceTimersByTimeAsync(2_500); // the session start settles
      await started;
      expect(reads.calls(ended)).toBe(2);
      await handlers.get("session_shutdown")!({ reason: "quit" }, ctx);
      await vi.advanceTimersByTimeAsync(120_000);
      expect(reads.calls(ended), "no look after the quit").toBe(2);

      // Control: one never claimed is looked at 3 s, 15 s and 60 s after its start, then left.
      const ctx2 = fakeContext({ sessionId: never, sessionFile: `/tmp/${never}.jsonl`, hasUI: true });
      const started2 = handlers.get("session_start")!({ reason: "startup" }, ctx2);
      await vi.advanceTimersByTimeAsync(2_500);
      await started2;
      await vi.advanceTimersByTimeAsync(600_000);
      expect(reads.calls(never)).toBe(2 + 3);
    } finally {
      vi.useRealTimers();
      reads.restore();
      for (const sid of [ended, never]) {
        rmSync(pidFile(sid), { force: true });
        rmSync(shutdownSentinelFile(sid), { force: true });
      }
    }
  });

  it("never commits a re-read that comes back after pi switched sessions", async () => {
    pair();
    installProbeCli();
    const first = uniqueSessionId("claim-first");
    const second = uniqueSessionId("claim-second");
    const reads = initializeReads({
      [first]: [{ capture: NOT_RECORDING }],
      [second]: [{ capture: RUNNING, state: "off" }],
    });
    reads.hold(first, 3); // the 3 s look's CLI call is still running when pi switches
    vi.useFakeTimers();
    try {
      const { api, handlers } = fakeExtensionAPI();
      await registerExtension(api as never, tmp);
      const ctx1 = fakeContext({ sessionId: first, sessionFile: `/tmp/${first}.jsonl`, hasUI: true });
      const started = handlers.get("session_start")!({ reason: "startup" }, ctx1);
      await vi.advanceTimersByTimeAsync(2_500);
      await started;
      await vi.advanceTimersByTimeAsync(3_000);
      expect(reads.calls(first)).toBe(3);

      const ctx2 = fakeContext({ sessionId: second, sessionFile: `/tmp/${second}.jsonl`, hasUI: true });
      const switched = handlers.get("session_start")!({ reason: "new" }, ctx2);
      await vi.advanceTimersByTimeAsync(2_500);
      await switched;
      expect(ctx2.setStatus).toHaveBeenLastCalledWith("probe-tracking", "○ off");
      const drawn = ctx1.setStatus.mock.calls.length;

      reads.release();
      await vi.advanceTimersByTimeAsync(0);
      expect(ctx1.setStatus.mock.calls.length, "the old session's footer is not redrawn").toBe(drawn);
      await handlers.get("agent_settled")!({}, ctx2);
      expect(ctx2.setStatus, "the new session's state is its own").toHaveBeenLastCalledWith("probe-tracking", "○ off");
      await vi.advanceTimersByTimeAsync(120_000);
      expect(reads.calls(first), "and the looks stop").toBe(3);
    } finally {
      vi.useRealTimers();
      reads.restore();
      for (const sid of [first, second]) {
        rmSync(pidFile(sid), { force: true });
        rmSync(shutdownSentinelFile(sid), { force: true });
      }
    }
  });

  it("ignores a malformed capture block rather than failing the whole read", async () => {
    installProbeCli();
    initializeOnce((_command, _args, _options, callback) => {
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
    await registerExtension(api as never, tmp);
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
    expect(ctx.setStatus).toHaveBeenCalledWith("probe-tracking", "● on (inline)");
  });

  it("re-reads the footer after the spawn attempt, so a refused spawn is visible", async () => {
    pair();
    installProbeCli();
    const { api, handlers } = fakeExtensionAPI();
    await registerExtension(api as never, tmp);
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
    await registerExtension(api as never, tmp);
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
    expect(daemonSpawns()).toHaveLength(0); // capture is the service's to ask for (M9)

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
    await registerExtension(api as never, tmp);
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
    await registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("reload-shutdown");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);

    await handlers.get("session_shutdown")!({ reason: "reload" }, ctx);
    // No shutdown sentinel should appear — the daemon was deliberately left running.
    expect(existsSync(shutdownSentinelFile(sessionId))).toBe(false);

    rmSync(pidFile(sessionId), { force: true });
    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });

  it("touches the shutdown sentinel on a real quit", async () => {
    pair();
    const { api, handlers } = fakeExtensionAPI();
    await registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("quit-shutdown");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);
    await handlers.get("session_shutdown")!({ reason: "quit" }, ctx);

    expect(existsSync(shutdownSentinelFile(sessionId))).toBe(true);

    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });

  /**
   * Audit #10: a session the Probe service records has no `.pid` file, only a
   * `.hosted` marker. pi's quit touched the sentinel and left the marker, so a
   * resume right after read "running" in `tap start` (marker + a recorder still
   * finishing) and nothing recorded the resumed session. Now the quit asks the
   * service to finish the session (`tap finish`, as session-end.sh does) and
   * drops the marker, so the next `tap start` asks the service again.
   */
  describe.runIf(existsSync("/proc/self"))("a session the Probe service records", () => {
    async function withService(fn: (pid: number) => Promise<void>): Promise<void> {
      const child = realChildProcess.spawn(process.execPath, ["-e", "setTimeout(() => {}, 60000)", "probe.service"], { stdio: "ignore" });
      try {
        for (let i = 0; i < 100; i++) {
          try {
            if (readFileSync(`/proc/${child.pid}/cmdline`, "utf-8").includes("probe.service")) break;
          } catch {
            // not exec'd yet
          }
          await new Promise((r) => setTimeout(r, 20));
        }
        await fn(child.pid!);
      } finally {
        child.kill("SIGKILL");
      }
    }

    async function startHosted(label: string, pid: number) {
      pair();
      const { api, handlers } = fakeExtensionAPI();
      await registerExtension(api as never, tmp);
      const sessionId = uniqueSessionId(label);
      const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });
      await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);
      const hosted = `/tmp/${paths.WATCHER_PREFIX}-watcher-${sessionId}.hosted`;
      writeFileSync(hosted, String(pid), { mode: 0o600 });
      execFileMock.mockClear();
      serviceCalls.length = 0;
      return { handlers, ctx, sessionId, hosted };
    }

    const finishCalls = () => serviceCalls.filter(({ method }) => method === "session.finish");

    it("on quit: the service finishes the session (`session.finish`, bounded wait), and the marker goes", async () => {
      await withService(async (pid) => {
        const { handlers, ctx, sessionId, hosted } = await startHosted("hosted-quit", pid);
        try {
          await handlers.get("session_shutdown")!({ reason: "quit" }, ctx);

          // On the service's own socket: pi spawns no `tap finish` (service-only task 16).
          expect(finishCalls()).toEqual([{ method: "session.finish", params: { sid: sessionId, wait: 3 } }]);
          expect(execFileMock.mock.calls.some(([, args]) => args.includes("finish"))).toBe(false);
          expect(existsSync(hosted)).toBe(false);
          expect(existsSync(shutdownSentinelFile(sessionId))).toBe(true);
        } finally {
          rmSync(hosted, { force: true });
          rmSync(shutdownSentinelFile(sessionId), { force: true });
        }
      });
    });

    it("on a switch to another session: the marker goes, and nothing waits on a finish", async () => {
      await withService(async (pid) => {
        const { handlers, ctx, sessionId, hosted } = await startHosted("hosted-switch", pid);
        try {
          await handlers.get("session_shutdown")!({ reason: "new" }, ctx);

          expect(finishCalls()).toHaveLength(0);
          expect(existsSync(hosted)).toBe(false);
        } finally {
          rmSync(hosted, { force: true });
          rmSync(shutdownSentinelFile(sessionId), { force: true });
        }
      });
    });

    it("with no marker, neither a quit nor a switch asks any interpreter anything", async () => {
      pair();
      const { api, handlers } = fakeExtensionAPI();
      await registerExtension(api as never, tmp);
      // A checkout no start in this process asked about: any version probe would be a new call.
      const fresh = join(tmp, "never-asked");
      mkdirSync(join(fresh, "tap"), { recursive: true });
      writeFileSync(join(fresh, "tap", "__init__.py"), "");
      mkdirSync(join(fresh, ".venv", "bin"), { recursive: true });
      writeFileSync(join(fresh, ".venv", "bin", "python3"), "#!/bin/sh\nexit 0\n");
      chmodSync(join(fresh, ".venv", "bin", "python3"), 0o755);
      process.env.PROBE_PI_TAP_ROOT = fresh;
      for (const reason of ["new", "quit"]) {
        const sessionId = uniqueSessionId(`no-marker-${reason}`);
        const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });
        execFileSyncMock.mockClear();
        await handlers.get("session_shutdown")!({ reason }, ctx);
        expect(execFileSyncMock).not.toHaveBeenCalled();
        rmSync(shutdownSentinelFile(sessionId), { force: true });
      }
    });

    it("on reload: the marker stays (the same session starts again)", async () => {
      await withService(async (pid) => {
        const { handlers, ctx, sessionId, hosted } = await startHosted("hosted-reload", pid);
        try {
          await handlers.get("session_shutdown")!({ reason: "reload" }, ctx);

          expect(finishCalls()).toHaveLength(0);
          expect(existsSync(hosted)).toBe(true);
        } finally {
          rmSync(hosted, { force: true });
          rmSync(shutdownSentinelFile(sessionId), { force: true });
        }
      });
    });
  });
});

describe("registerExtension — the tap's python", () => {
  it("is the service's now (M9): a session start asks for none, and an earlier start's note goes", async () => {
    pair();
    const missing = paths.pythonMissingFile(process.env);
    mkdirSync(dirname(missing), { recursive: true });
    writeFileSync(missing, "no python3\n");
    const { api, handlers } = fakeExtensionAPI();
    await registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("old-python");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });
    process.env.PATH = join(tmp, "empty-path");

    const chunks = await captureStderr(() => invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx));

    expect(chunks.join("")).not.toContain("no Python 3.11 or newer found");
    expect(daemonSpawns()).toHaveLength(0);
    expect(existsSync(missing)).toBe(false);
  });
});

describe("registerExtension — /probe-status command", () => {
  it("registers a status command that reports pairing state", async () => {
    pair();
    const { api, commands } = fakeExtensionAPI();
    await registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("status");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    expect(commands.has("probe-status")).toBe(true);

    const chunks = await captureStderr(() => commands.get("probe-status")!.handler("", ctx));

    expect(chunks.join("\n")).toContain("paired:");
  });
});

describe("registerExtension — the daemon profile", () => {
  const PACKAGE_ROOT = dirname(dirname(new URL(import.meta.url).pathname));
  const EVERY_SKILL = ["probe", "track-work", "visualize-progress", "instrument-code", "audit-team-note", "edit-notes", "notes-audit", "plan-research-review"];
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
    await registerExtension(api as never, tmp);
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
    await registerExtension(api as never, tmp);
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

  it("adds the daemon's own skills and plan-research-review through resources_discover, and only in the daemon profile", async () => {
    installProbeCli();
    cliReports({ state: "daemon", profile: "daemon" });
    const { api, handlers } = fakeExtensionAPI();
    await registerExtension(api as never, PACKAGE_ROOT);
    const daemonCtx = fakeContext({ sessionId: uniqueSessionId("discover-daemon"), sessionFile: undefined });
    await handlers.get("session_start")!({ reason: "startup" }, daemonCtx);

    const discovered = (await handlers.get("resources_discover")!({ type: "resources_discover", cwd: "/repo", reason: "startup" }, daemonCtx)) as unknown as {
      skillPaths: string[];
    };
    expect(discovered.skillPaths).toEqual([
      join(PACKAGE_ROOT, "daemon-skills", "probe"),
      join(PACKAGE_ROOT, "daemon-skills", "visualize-progress"),
      join(PACKAGE_ROOT, "skills", "plan-research-review"),
    ]);
    for (const path of discovered.skillPaths) expect(existsSync(join(path, "SKILL.md"))).toBe(true);

    // The next session is the agent's (an older CLI sends no profile): the
    // previous session's profile must not carry over.
    cliReports({});
    const agentCtx = fakeContext({ sessionId: uniqueSessionId("discover-agent"), sessionFile: undefined });
    await handlers.get("session_start")!({ reason: "new" }, agentCtx);
    expect(await handlers.get("resources_discover")!({ type: "resources_discover", cwd: "/repo", reason: "startup" }, agentCtx)).toBeUndefined();
  });

  it("leaves the agent profile's skills exactly as pi loaded them", async () => {
    installProbeCli();
    cliReports({ state: "full", profile: "agent" });
    const { api, handlers, activeTools } = fakeExtensionAPI(["read", "probe_mcp_entity"]);
    await registerExtension(api as never, PACKAGE_ROOT);
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
    await registerExtension(api as never, tmp);
    const ctx = { ...fakeContext({ sessionId: uniqueSessionId("mcp-login"), sessionFile: undefined, hasUI: true }), ui: { notify: vi.fn(), setStatus: vi.fn(), input: vi.fn() } };
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    await captureStderr(() => commands.get("probe-mcp-login")!.handler("", ctx));
    expect(ctx.ui.input).not.toHaveBeenCalled();
    expect(ctx.ui.notify.mock.calls[0][0]).toContain("Probe daemon reads for this session");
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
    await registerExtension(api as never, tmp);
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
    await registerExtension(api as never, tmp);
    const ctx = fakeContext({ sessionId, sessionFile: undefined, hasUI: false });
    await handlers.get("session_start")!({ reason: "startup" }, ctx);
    await (handlers.get("before_agent_start") as unknown as BeforeAgentStart)({ systemPrompt: "BASE" }, ctx);
    expect(existsSync(answer)).toBe(false);
  });
});

describe("registerExtension — Probe's tracking switched off (probeTracking: false)", () => {
  const PACKAGE_SKILLS = ["probe", "track-work", "visualize-progress", "instrument-code", "audit-team-note", "edit-notes", "notes-audit", "plan-research-review"];

  type BeforeAgentStart = (
    event: { systemPrompt: string; systemPromptOptions?: { skills: Array<{ name: string; filePath: string }> } },
    ctx: unknown,
  ) => Promise<{ systemPrompt?: string; message?: { content: string } } | undefined>;

  function writeOurEntry(entry: Record<string, unknown>, scopeDir = process.env.PI_CODING_AGENT_DIR!): void {
    mkdirSync(scopeDir, { recursive: true });
    writeFileSync(join(scopeDir, "settings.json"), JSON.stringify({ packages: [{ source: tmp, ...entry }] }));
  }

  it("offers no Probe MCP tool, no package skill and no prompt text, while capture keeps running", async () => {
    pair();
    writeOurEntry({ skills: [], probeTracking: false });
    const { api, handlers, activeTools } = fakeExtensionAPI(["read", "bash", "probe-research-pi__probe_browse"]);
    await registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("tracking-off");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    await invokeSessionStart(handlers.get("session_start")!, { reason: "startup" }, ctx);

    expect(activeTools()).toEqual(["read", "bash"]);
    const log = readFileSync(extensionLogFile(process.env), "utf-8");
    expect(log).toContain("Probe tracking is off, no Probe MCP tools");
    expect(log).not.toContain("Probe MCP ready");
    expect(daemonSpawns()).toHaveLength(0); // capture is the service's to ask for (M9), off switch or not

    const skills = [...PACKAGE_SKILLS, "their-own"].map((name) => ({ name, filePath: `/x/${name}/SKILL.md` }));
    const event = { systemPrompt: "BASE", systemPromptOptions: { skills } };
    const result = await (handlers.get("before_agent_start") as unknown as BeforeAgentStart)(event, ctx);
    expect(result).toBeUndefined();
    expect(event.systemPromptOptions.skills.map((s) => s.name)).toEqual(["their-own"]);

    rmSync(pidFile(sessionId), { force: true });
    rmSync(shutdownSentinelFile(sessionId), { force: true });
  });

  it("adds no daemon skill either, even when the CLI says the daemon records", async () => {
    installProbeCli();
    writeOurEntry({ skills: [], probeTracking: false });
    const original = execFileMock.getMockImplementation()!;
    execFileMock.mockImplementation((_command, args, _options, callback) => {
      callback(null, JSON.stringify({ session_id: args[args.indexOf("--session") + 1], tracking: true, signal: "on",
        seeded: true, source: "machine", state: "daemon", profile: "daemon" }), "");
    });
    try {
      const { api, handlers } = fakeExtensionAPI();
      await registerExtension(api as never, tmp);
      const ctx = fakeContext({ sessionId: uniqueSessionId("tracking-off-daemon"), sessionFile: undefined });
      await handlers.get("session_start")!({ reason: "startup" }, ctx);
      expect(await handlers.get("resources_discover")!({ type: "resources_discover", cwd: "/repo", reason: "startup" }, ctx)).toBeUndefined();
    } finally {
      execFileMock.mockImplementation(original);
    }
  });

  it("stays off when the CLI cannot answer: the flag is read from pi's settings, not from `probe`", async () => {
    writeOurEntry({ skills: [], probeTracking: false });
    const original = execFileMock.getMockImplementation()!;
    execFileMock.mockImplementation((_command, _args, _options, callback) => callback(new Error("probe: not found"), "", ""));
    try {
      const { api, handlers, activeTools } = fakeExtensionAPI(["read", "probe-research-pi__probe_browse"]);
      await registerExtension(api as never, tmp);
      const ctx = fakeContext({ sessionId: uniqueSessionId("tracking-off-nocli"), sessionFile: undefined });
      await handlers.get("session_start")!({ reason: "startup" }, ctx);
      expect(activeTools()).toEqual(["read"]);
    } finally {
      execFileMock.mockImplementation(original);
    }
  });

  it("follows pi's merge: a project entry with tracking on wins over the global switch", async () => {
    writeOurEntry({ skills: [], probeTracking: false });
    const project = join(tmp, "proj");
    writeOurEntry({}, join(project, ".pi"));
    const { trackingOff } = await import("../src/trackingSetting.js");
    expect(trackingOff({ env: process.env, cwd: project, packageRoot: tmp })).toBe(false);
    // `autoload: false` makes the project entry a delta over the global one.
    writeOurEntry({ autoload: false }, join(project, ".pi"));
    expect(trackingOff({ env: process.env, cwd: project, packageRoot: tmp })).toBe(true);
  });
});

