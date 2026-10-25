/**
 * The REAL implementation (`src/impl.ts`) under the shim:
 *
 *   - its two roots: `installRoot` (where pi loaded the package: the identity
 *     pi's settings name) for the tracking opt-out and pi-mcp-adapter's
 *     hand-off, `resourceRoot` (this version's files) for skills;
 *   - a real pin: this package copied into a throwaway pin layout, its
 *     `node_modules` a symlink, loaded through the shim's own dynamic
 *     `import()` -- the imports resolve from the pin, and the pin's code runs;
 *   - a Probe MCP connection carried across that switch: the same client, no
 *     reconnect, the tools rebuilt by the new version's code;
 *   - the shared-state schema refusal, and the resources fingerprint.
 *
 * `node:child_process` is mocked as in `extension.test.ts`: no process runs,
 * and the CLI's `session initialize` answer is set per test.
 */
import { chmodSync, cpSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let cliAnswer: Record<string, unknown> = {};

vi.mock("node:child_process", () => ({
  spawn: () => ({
    pid: 4242,
    unref: () => {},
    once: (event: string, callback: (value: unknown) => void) => {
      if (event === "exit") queueMicrotask(() => callback(0));
    },
  }),
  execFileSync: () => "",
  execFile: (_command: string, args: string[], _options: unknown, callback: (error: Error | null, stdout: string, stderr: string) => void) => {
    callback(
      null,
      JSON.stringify({ session_id: args[args.indexOf("--session") + 1], tracking: true, signal: "on", seeded: true, source: "machine", ...cliAnswer }),
      "",
    );
  },
}));

const impl = await import("../src/impl.js");
const shim = await import("../src/shim.js");
const { resourcesFingerprint } = await import("../src/resources.js");
const { MCP_SERVED_VIA_ADAPTER_MESSAGE } = await import("../src/adapterHandoff.js");
const { extensionLogFile } = await import("../src/core/paths.js");
type McpTransport = import("../src/mcpBridge.js").McpTransport;

const PACKAGE_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

type Handler = (event: unknown, ctx: unknown) => unknown;

function fakePi() {
  const handlers = new Map<string, Handler>();
  const tools = new Map<string, { name: string; description: string; execute: (...a: unknown[]) => Promise<any> }>();
  let active = ["read", "bash"];
  const api = {
    on: vi.fn((event: string, handler: Handler) => handlers.set(event, handler)),
    registerCommand: vi.fn(),
    registerTool: vi.fn((tool: { name: string; description: string; execute: (...a: unknown[]) => Promise<any> }) => {
      if (!tools.has(tool.name)) active.push(tool.name);
      tools.set(tool.name, tool);
    }),
    getActiveTools: vi.fn(() => [...active]),
    setActiveTools: vi.fn((names: string[]) => {
      active = [...names];
    }),
    sendMessage: vi.fn(),
  };
  return { api, handlers, tools, activeTools: () => active };
}

function fakeCtx(sessionId: string, cwd: string) {
  return {
    hasUI: false,
    cwd,
    ui: { notify: vi.fn(), setStatus: vi.fn(), select: vi.fn(), input: vi.fn() },
    sessionManager: { getSessionId: () => sessionId, getSessionFile: () => undefined },
  };
}

let tmp: string;
let saved: Record<string, string | undefined>;
const ENV_KEYS = ["XDG_STATE_HOME", "PI_CODING_AGENT_DIR", "PROBE_PI_TAP_PLUGIN_DIR", "PROBE_CONFIG_PATH", "PATH", "PROBE_MCP_TOKEN"];

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-pi-real-"));
  saved = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));
  process.env.XDG_STATE_HOME = join(tmp, "state");
  process.env.PI_CODING_AGENT_DIR = join(tmp, "pi-agent");
  process.env.PROBE_PI_TAP_PLUGIN_DIR = join(tmp, "tap-state");
  process.env.PROBE_CONFIG_PATH = join(tmp, "no-config.json");
  delete process.env.PROBE_MCP_TOKEN;
  // A `probe` on PATH, so `session initialize` is asked (the mock answers).
  const bin = join(tmp, "bin");
  mkdirSync(bin, { recursive: true });
  writeFileSync(join(bin, "probe"), "#!/bin/sh\nexit 0\n");
  chmodSync(join(bin, "probe"), 0o755);
  process.env.PATH = `${bin}${delimiter}${process.env.PATH ?? ""}`;
  cliAnswer = {};
});

afterEach(() => {
  for (const [key, value] of Object.entries(saved)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  rmSync(tmp, { recursive: true, force: true });
});

function fakeApi() {
  return { sendMessage: vi.fn(), getActiveTools: () => ["read"], setActiveTools: vi.fn(), declarationsChanged: vi.fn() };
}

/** pi's user settings, naming packages. */
function piSettings(packages: unknown[]): void {
  mkdirSync(process.env.PI_CODING_AGENT_DIR!, { recursive: true });
  writeFileSync(join(process.env.PI_CODING_AGENT_DIR!, "settings.json"), JSON.stringify({ packages }));
}

/** A folder standing for the pi clone pi loaded the package from. */
function cloneRoot(): string {
  const root = join(tmp, "clone", "plugins", "probe-research-pi");
  mkdirSync(root, { recursive: true });
  writeFileSync(join(root, "package.json"), JSON.stringify({ name: "probe-research-pi", version: "0.6.3" }));
  return root;
}

describe("two roots: identity from installRoot, files from resourceRoot (O8)", () => {
  it("honours the tracking opt-out on the package pi loaded, though the code runs from elsewhere", async () => {
    const installRoot = cloneRoot();
    piSettings([{ source: installRoot, probeTracking: false }]);
    cliAnswer = { profile: "daemon", state: "daemon" };
    const candidate = impl.prepare({ api: fakeApi(), shared: shim.emptySharedState(), installRoot, resourceRoot: PACKAGE_ROOT });
    candidate.activate();
    const ctx = fakeCtx("o8-off", tmp);
    await candidate.declarations.events.session_start({ reason: "startup" }, ctx);
    // Tracking off wins over the daemon profile: no daemon skill is added.
    expect(await candidate.declarations.events.resources_discover({ type: "resources_discover", cwd: tmp, reason: "startup" }, ctx)).toBeUndefined();
    expect(readFileSync(extensionLogFile(process.env), "utf-8")).toContain("Probe tracking is off, no Probe MCP tools for o8-off");
  });

  it("adds the daemon's skills from this version's own files, not the clone's", async () => {
    const installRoot = cloneRoot();
    cliAnswer = { profile: "daemon", state: "daemon" };
    const candidate = impl.prepare({ api: fakeApi(), shared: shim.emptySharedState(), installRoot, resourceRoot: PACKAGE_ROOT });
    candidate.activate();
    const ctx = fakeCtx("o8-files", tmp);
    await candidate.declarations.events.session_start({ reason: "startup" }, ctx);
    const found = (await candidate.declarations.events.resources_discover({ type: "resources_discover", cwd: tmp, reason: "startup" }, ctx)) as {
      skillPaths: string[];
    };
    expect(found.skillPaths).toEqual([
      join(PACKAGE_ROOT, "daemon-skills", "probe"),
      join(PACKAGE_ROOT, "daemon-skills", "visualize-progress"),
      join(PACKAGE_ROOT, "skills", "plan-research-review"),
    ]);
  });

  it("stands down for pi-mcp-adapter when pi's settings name the package pi loaded", async () => {
    const installRoot = cloneRoot();
    piSettings(["npm:pi-mcp-adapter", { source: installRoot }]);
    const candidate = impl.prepare({ api: fakeApi(), shared: shim.emptySharedState(), installRoot, resourceRoot: PACKAGE_ROOT });
    candidate.activate();
    await candidate.declarations.events.session_start({ reason: "startup" }, fakeCtx("o8-adapter", tmp));
    expect(readFileSync(extensionLogFile(process.env), "utf-8")).toContain(MCP_SERVED_VIA_ADAPTER_MESSAGE);
  });
});

describe("the shared state's schema", () => {
  it("refuses a schema it neither runs on nor migrates, with the typed error the shim recognises", () => {
    const api = fakeApi();
    let thrown: unknown;
    try {
      impl.prepare({ api, shared: { schema: 99, session: {}, transports: {} }, installRoot: PACKAGE_ROOT, resourceRoot: PACKAGE_ROOT });
    } catch (err) {
      thrown = err;
    }
    expect(shim.isUnsupportedStateSchema(thrown)).toBe(true);
    expect(impl.MIGRATES_FROM).toEqual([]);
    expect(impl.STATE_SCHEMA).toBe(1);
    expect(impl.REQUIRES_SHIM_API).toBe(shim.SHIM_API);
  });

  it("initializes an empty state, and takes over its own schema as it is", () => {
    const api = fakeApi();
    const first = impl.prepare({ api, shared: shim.emptySharedState(), installRoot: PACKAGE_ROOT, resourceRoot: PACKAGE_ROOT });
    expect(first.shared.schema).toBe(1);
    const spawned = (first.shared.session.capture as { spawned: Set<string> }).spawned;
    spawned.add("carried");
    const second = impl.prepare({ api, shared: shim.copyShared(first.shared), installRoot: PACKAGE_ROOT, resourceRoot: PACKAGE_ROOT });
    expect((second.shared.session.capture as { spawned: Set<string> }).spawned).toBe(spawned);
  });

  it("prepare has no side effect: no pi call, no timer", () => {
    const api = fakeApi();
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    try {
      impl.prepare({ api, shared: shim.emptySharedState(), installRoot: PACKAGE_ROOT, resourceRoot: PACKAGE_ROOT });
      expect(setIntervalSpy).not.toHaveBeenCalled();
      expect(api.sendMessage).not.toHaveBeenCalled();
      expect(api.setActiveTools).not.toHaveBeenCalled();
      expect(api.declarationsChanged).not.toHaveBeenCalled();
    } finally {
      setIntervalSpy.mockRestore();
    }
  });
});

describe("the resources fingerprint", () => {
  function copySkills(): string {
    const copy = join(tmp, "copy");
    for (const entry of ["package.json", "mcp.json", "skills", "daemon-skills"]) {
      cpSync(join(PACKAGE_ROOT, entry), join(copy, entry), { recursive: true });
    }
    return copy;
  }

  it("is the module's, and the same for a byte-identical copy", () => {
    expect(impl.RESOURCES_FINGERPRINT).toBe(resourcesFingerprint(PACKAGE_ROOT));
    expect(resourcesFingerprint(copySkills())).toBe(impl.RESOURCES_FINGERPRINT);
  });

  it("changes with a skill's description, not with its body", () => {
    const copy = copySkills();
    const skill = join(copy, "daemon-skills", "probe", "SKILL.md");
    const text = readFileSync(skill, "utf-8");
    writeFileSync(skill, `${text}\nOne more line of body.\n`);
    expect(resourcesFingerprint(copy)).toBe(impl.RESOURCES_FINGERPRINT);
    writeFileSync(skill, text.replace(/^description:.*$/m, "description: something else entirely"));
    expect(resourcesFingerprint(copy)).not.toBe(impl.RESOURCES_FINGERPRINT);
  });

  it("changes when the manifest lists another skill", () => {
    const copy = copySkills();
    const pkg = JSON.parse(readFileSync(join(copy, "package.json"), "utf-8"));
    pkg.pi.skills = pkg.pi.skills.slice(1);
    writeFileSync(join(copy, "package.json"), JSON.stringify(pkg));
    expect(resourcesFingerprint(copy)).not.toBe(impl.RESOURCES_FINGERPRINT);
  });
});

describe("a real pin, loaded through the shim's dynamic import", () => {
  const PIN = "0.7.0-test";

  /** This package, copied into `<state>/pi/pins/<pin>/plugins/probe-research-pi`, `node_modules` a symlink. */
  function makeRealPin(patch?: (packageDir: string) => void): string {
    const env = process.env;
    const pinRoot = join(shim.pinsDir(env), PIN);
    const packageDir = shim.pinPackageDir(PIN, env);
    for (const entry of ["index.ts", "package.json", "mcp.json", "src", "skills", "daemon-skills"]) {
      cpSync(join(PACKAGE_ROOT, entry), join(packageDir, entry), { recursive: true });
    }
    symlinkSync(join(PACKAGE_ROOT, "node_modules"), join(pinRoot, "node_modules"));
    patch?.(packageDir);
    writeFileSync(join(pinRoot, shim.PINNED_MARKER), "");
    return packageDir;
  }

  function setCurrent(): void {
    const path = shim.currentFile(process.env);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(`${path}.tmp`, JSON.stringify({ pin: PIN, version: "0.6.3", from: join(tmp, "clone"), head: null }));
    renameSync(`${path}.tmp`, path);
  }

  it("starts on it at load, and its own files answer", async () => {
    const packageDir = makeRealPin();
    setCurrent();
    const host = new shim.ShimHost({ installRoot: cloneRoot(), cloneImpl: impl, pid: 777 });
    const pi = fakePi();
    await host.attach(pi.api as never);

    expect(host.activePin).toBe(PIN);
    expect(new Set(pi.handlers.keys())).toEqual(new Set(shim.SHIM_EVENTS));
    cliAnswer = { profile: "daemon", state: "daemon" };
    const ctx = fakeCtx("pin-start", tmp);
    await pi.handlers.get("session_start")!({ reason: "startup" }, ctx);
    const found = (await pi.handlers.get("resources_discover")!({ type: "resources_discover", cwd: tmp, reason: "startup" }, ctx)) as {
      skillPaths: string[];
    };
    expect(found.skillPaths[0]).toBe(join(packageDir, "daemon-skills", "probe"));
    const record = JSON.parse(readFileSync(shim.liveRecordFile(777, process.env), "utf-8"));
    expect(record).toMatchObject({ pin: PIN, bad_pins: [], state_schema: 1 });
    await pi.handlers.get("session_shutdown")!({ reason: "quit" }, ctx);
  });

  it("takes over a running clone at the next prompt, carrying its Probe MCP connection: same client, no reconnect, the pin's code", async () => {
    const SESSION = "pin-mcp";
    const host = new shim.ShimHost({ installRoot: cloneRoot(), cloneImpl: impl, pid: 778 });
    const pi = fakePi();
    await host.attach(pi.api as never);
    expect(host.activePin).toBeNull();

    // A connection the clone's version opened for this session.
    const callTool = vi.fn(async ({ name }: { name: string }) => ({ content: [{ type: "text", text: `answer from ${name}` }] }));
    const client = { listTools: vi.fn(), callTool, close: vi.fn(async () => {}) };
    const transport: McpTransport = {
      client,
      tools: [{ name: "search_runs", description: "Search runs", inputSchema: { type: "object", properties: { query: { type: "string" } } } }],
      url: "https://mcp.invalid/mcp",
      reconnect: "bearer",
    };
    (host.sharedState.transports.mcp as Map<string, McpTransport>).set(SESSION, transport);
    const ctx = fakeCtx(SESSION, tmp);
    await pi.handlers.get("session_start")!({ reason: "startup" }, ctx);
    expect(pi.tools.has("probe_mcp_search_runs")).toBe(true);
    const before = await pi.tools.get("probe_mcp_search_runs")!.execute("call-1", { query: "x" });
    expect(before.details).toEqual({ mcpTool: "search_runs" });

    // The pin's copy stamps its results, so the test can tell whose code ran.
    makeRealPin((packageDir) => {
      const bridge = join(packageDir, "src", "mcpBridge.ts");
      const source = readFileSync(bridge, "utf-8");
      const stamped = source.replace("details: { mcpTool: mcpToolName },", 'details: { mcpTool: mcpToolName, via: "pin" },');
      expect(stamped).not.toBe(source);
      writeFileSync(bridge, stamped);
    });
    setCurrent();
    await pi.handlers.get("before_agent_start")!({ systemPrompt: "BASE" }, ctx);
    expect(host.activePin).toBe(PIN);

    const after = await pi.tools.get("probe_mcp_search_runs")!.execute("call-2", { query: "y" });
    expect(after.details).toEqual({ mcpTool: "search_runs", via: "pin" });
    expect(callTool).toHaveBeenCalledTimes(2);
    expect(callTool.mock.calls[1][0]).toEqual({ name: "search_runs", arguments: { query: "y" } });
    expect((host.sharedState.transports.mcp as Map<string, McpTransport>).get(SESSION)).toBe(transport);
    expect(transport.client).toBe(client);
    expect(client.close).not.toHaveBeenCalled();

    await pi.handlers.get("session_shutdown")!({ reason: "quit" }, ctx);
    expect(client.close).toHaveBeenCalledTimes(1);
  });
});
