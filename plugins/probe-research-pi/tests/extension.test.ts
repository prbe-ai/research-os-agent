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

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const spawnMock = vi.fn(
  (_command: string, _args: string[], _options: { detached: boolean; stdio: string; env: Record<string, string | undefined> }) => ({
    pid: 4242,
    unref: vi.fn(),
  }),
);

vi.mock("node:child_process", () => ({
  spawn: (command: string, args: string[], options: { detached: boolean; stdio: string; env: Record<string, string | undefined> }) =>
    spawnMock(command, args, options),
}));

// Imported AFTER the mock is registered (vitest hoists vi.mock, but the
// dynamic import below keeps intent obvious without relying on hoisting
// semantics for anything except the mock itself).
const { registerExtension } = await import("../src/extension.js");
const { pidFile, shutdownSentinelFile, disabledFile, teamNoteDocumentPath, extensionLogFile } = await import("../src/paths.js");
const { MCP_SERVED_VIA_ADAPTER_MESSAGE } = await import("../src/adapterHandoff.js");

type Handler = (event: unknown, ctx: unknown) => Promise<void> | void;

function fakeExtensionAPI() {
  const handlers = new Map<string, Handler>();
  const commands = new Map<string, { handler: (args: string, ctx: unknown) => Promise<void> }>();
  const api = {
    on: vi.fn((event: string, handler: Handler) => {
      handlers.set(event, handler);
    }),
    registerCommand: vi.fn((name: string, options: { handler: (args: string, ctx: unknown) => Promise<void> }) => {
      commands.set(name, options);
    }),
  };
  return { api, handlers, commands };
}

function fakeContext(overrides: { sessionId: string; sessionFile?: string; cwd?: string }) {
  const notify = vi.fn();
  return {
    hasUI: false,
    ui: { notify },
    cwd: overrides.cwd ?? "/repo/project",
    sessionManager: {
      getSessionId: () => overrides.sessionId,
      getSessionFile: () => overrides.sessionFile,
    },
    notify,
  };
}

let tmp: string;
let originalEnv: Record<string, string | undefined>;

const ENV_KEYS = [
  "PROBE_PI_TAP_PLUGIN_DIR",
  "PROBE_PI_TAP_TOKEN",
  "PROBE_CONFIG_PATH",
  "PROBE_PI_TAP_ROOT",
  "PATH",
  "PI_CODING_AGENT_DIR",
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

describe("registerExtension — session_start", () => {
  it("does not spawn when the device is unpaired, and says so on stderr", async () => {
    const { api, handlers } = fakeExtensionAPI();
    registerExtension(api as never, tmp);
    const sessionId = uniqueSessionId("unpaired");
    const ctx = fakeContext({ sessionId, sessionFile: `/tmp/${sessionId}.jsonl` });

    const chunks = await captureStderr(() => handlers.get("session_start")!({ reason: "startup" }, ctx) as Promise<void>);

    expect(spawnMock).not.toHaveBeenCalled();
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

    expect(spawnMock).toHaveBeenCalledTimes(1);
    const [, , options] = spawnMock.mock.calls[0];
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

    expect(spawnMock).toHaveBeenCalledTimes(1);

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

    expect(spawnMock).not.toHaveBeenCalled();
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

    expect(spawnMock).not.toHaveBeenCalled();
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
    expect(spawnMock).toHaveBeenCalledTimes(1);

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
    expect(spawnMock).toHaveBeenCalledTimes(1);

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
      expect(spawnMock).not.toHaveBeenCalled();
    } finally {
      process.env.PATH = previousPath;
      process.env.HOME = previousHome;
    }
  });
});
