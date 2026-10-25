/**
 * The shim (`src/shim.ts`) against FAKE implementations and a fake pi: what it
 * registers, where it forwards, when it switches versions, how a failed switch
 * rolls back, and the live record it keeps for the Probe CLI. The real
 * implementation through the shim is `extension.test.ts` (every wiring test
 * goes through `shimHarness.ts`) and `shimRealImpl.test.ts`; pi's own loader
 * and tool registry are `shimPiLoader.test.ts`.
 *
 * Pins are real folders under a throwaway state directory (the shim checks
 * `.pinned` and the entry file); their `impl.ts` is never evaluated here: the
 * injected `importImpl` hands back the fake module the test set for that pin.
 */
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  copyShared,
  currentFile,
  emptySharedState,
  IMPL_ENTRY,
  liveRecordFile,
  PINNED_MARKER,
  pinPackageDir,
  pinsDir,
  SHIM_API,
  SHIM_EVENTS,
  ShimEvent,
  ShimHost,
  UnsupportedStateSchemaError,
  type Candidate,
  type ImplEnv,
  type ImplModule,
  type LiveRecord,
  type SharedState,
  type ToolDeclaration,
} from "../src/shim.js";

type Handler = (event: unknown, ctx: unknown) => unknown;

/** pi's registration surface, as far as the shim uses it, plus a way to emit. */
function fakePi(initialActive: string[] = ["read", "bash"]) {
  const handlers = new Map<string, Handler[]>();
  const commands = new Map<string, { description?: string; handler: (args: string, ctx: unknown) => unknown }>();
  const tools = new Map<string, Record<string, unknown>>();
  let active = [...initialActive];
  const api = {
    on: vi.fn((event: string, handler: Handler) => {
      const list = handlers.get(event) ?? [];
      list.push(handler);
      handlers.set(event, list);
      return () => {};
    }),
    registerCommand: vi.fn((name: string, options: { description?: string; handler: (args: string, ctx: unknown) => unknown }) => {
      commands.set(name, options);
    }),
    // As pi does: a NEW name is activated on registration; a known one keeps its activity.
    registerTool: vi.fn((tool: Record<string, unknown>) => {
      const known = tools.has(tool.name as string);
      tools.set(tool.name as string, tool);
      if (!known) active.push(tool.name as string);
    }),
    getActiveTools: vi.fn(() => [...active]),
    setActiveTools: vi.fn((names: string[]) => {
      active = [...names];
    }),
    sendMessage: vi.fn(),
  };
  async function emit(event: string, payload: unknown, ctx: unknown = fakeCtx()): Promise<unknown> {
    let result: unknown;
    for (const handler of handlers.get(event) ?? []) result = await handler(payload, ctx);
    return result;
  }
  return { api, handlers, commands, tools, activeTools: () => active, emit };
}

function fakeCtx(sessionId = "sess-1") {
  return {
    hasUI: true,
    cwd: "/repo",
    ui: { notify: vi.fn(), setStatus: vi.fn() },
    sessionManager: { getSessionId: () => sessionId, getSessionFile: () => undefined },
  };
}

interface FakeOptions {
  requires?: number;
  schema?: number;
  migratesFrom?: number[];
  fingerprint?: string;
  recorded?: boolean;
  prepareThrows?: Error;
  activateThrows?: Error;
  tools?: (env: ImplEnv) => ToolDeclaration[];
  migrate?: (shared: SharedState) => void;
  commandDescription?: string;
}

/** An implementation that records every call in `log`, each entry prefixed with its label. */
function fakeImpl(label: string, log: string[], opts: FakeOptions = {}): ImplModule {
  const schema = opts.schema ?? 1;
  const migratesFrom = opts.migratesFrom ?? [];
  return {
    REQUIRES_SHIM_API: opts.requires ?? SHIM_API,
    STATE_SCHEMA: schema,
    MIGRATES_FROM: migratesFrom,
    RESOURCES_FINGERPRINT: opts.fingerprint ?? "fp-1",
    prepare(env: ImplEnv): Candidate {
      log.push(`${label}:prepare`);
      if (opts.prepareThrows) throw opts.prepareThrows;
      const shared = env.shared;
      if (shared.schema === 0) {
        shared.session = { turns: { count: 0 } };
        shared.transports = {};
        shared.schema = schema;
      } else if (shared.schema !== schema) {
        if (!migratesFrom.includes(shared.schema)) throw new UnsupportedStateSchemaError(shared.schema, [schema, ...migratesFrom]);
        opts.migrate?.(shared);
        shared.schema = schema;
      }
      const events = Object.fromEntries(
        SHIM_EVENTS.map((name) => [
          name,
          (event: unknown, _ctx: unknown) => {
            log.push(`${label}:${name}`);
            return { by: label, name, event };
          },
        ]),
      );
      const command = (name: string) => ({
        description: opts.commandDescription ?? `${name} (${label})`,
        handler: (args: string) => {
          log.push(`${label}:/${name} ${args}`);
        },
      });
      return {
        shared,
        declarations: {
          events,
          commands: { "probe-status": command("probe-status"), "probe-mcp-login": command("probe-mcp-login") },
          tools: () => opts.tools?.(env) ?? [],
        },
        activate() {
          log.push(`${label}:activate`);
          if (opts.activateThrows) throw opts.activateThrows;
        },
        deactivate() {
          log.push(`${label}:deactivate`);
        },
        status: () => ({ recorded: opts.recorded ?? false }),
      };
    },
  };
}

function tool(name: string, description: string, onCall: () => unknown): ToolDeclaration {
  return {
    name,
    label: name,
    description,
    parameters: { type: "object", properties: {} },
    execute: async () => onCall(),
  };
}

let state: string;
let env: Record<string, string | undefined>;
let logs: string[];
/** The pi clone the package was loaded from (`<clone>/plugins/probe-research-pi`). */
let cloneDir: string;
let installRoot: string;

beforeEach(() => {
  state = mkdtempSync(join(tmpdir(), "probe-pi-shim-"));
  env = { XDG_STATE_HOME: state };
  logs = [];
  cloneDir = join(state, "clone");
  installRoot = join(cloneDir, "plugins", "probe-research-pi");
  mkdirSync(installRoot, { recursive: true });
  writeFileSync(join(installRoot, "package.json"), JSON.stringify({ name: "probe-research-pi", version: "0.6.3" }));
});

afterEach(() => {
  rmSync(state, { recursive: true, force: true });
});

/** A complete pin folder (`.pinned`, the entry file, a version). */
function makePin(pin: string, version = "9.9.9", complete = true): void {
  const pkg = pinPackageDir(pin, env);
  mkdirSync(join(pkg, ...IMPL_ENTRY.slice(0, -1)), { recursive: true });
  writeFileSync(join(pkg, ...IMPL_ENTRY), "// never evaluated in this file\n");
  writeFileSync(join(pkg, "package.json"), JSON.stringify({ name: "probe-research-pi", version }));
  if (complete) writeFileSync(join(pinsDir(env), pin, PINNED_MARKER), "");
}

/** `current`, replaced atomically as the CLI does (a new inode every time). */
function setCurrent(pin: string, version = "9.9.9"): void {
  const path = currentFile(env);
  mkdirSync(join(path, ".."), { recursive: true });
  const temp = `${path}.tmp`;
  // The CLI pins the clone as `pi update` left it: the clone holds that version.
  writeFileSync(join(installRoot, "package.json"), JSON.stringify({ name: "probe-research-pi", version }));
  writeFileSync(temp, JSON.stringify({ pin, version, from: cloneDir, head: null }));
  renameSync(temp, path);
}

function host(cloneImpl: ImplModule, modules: Record<string, ImplModule | Error> = {}, extra: Partial<ConstructorParameters<typeof ShimHost>[0]> = {}) {
  const imported: string[] = [];
  const importImpl = async (path: string) => {
    const pin = /pins[\\/]([^\\/]+)[\\/]/.exec(path)?.[1] ?? "?";
    imported.push(pin);
    const mod = modules[pin];
    if (mod === undefined) throw new Error(`no fake module for ${pin}`);
    if (mod instanceof Error) throw mod;
    return mod;
  };
  const shim = new ShimHost({
    installRoot,
    cloneImpl,
    env,
    importImpl,
    pid: 4242,
    log: (line) => logs.push(line),
    ...extra,
  });
  return { shim, imported };
}

function liveRecord(): LiveRecord {
  return JSON.parse(readFileSync(liveRecordFile(4242, env), "utf-8")) as LiveRecord;
}

describe("only the shim registers with pi (O1, O4)", () => {
  const src = new URL("../src/", import.meta.url);
  const files = (readdirSync(src, { recursive: true }) as string[]).filter((name) => name.endsWith(".ts") && name !== "shim.ts");

  it.each(files)("src/%s never calls pi's registration API", (name) => {
    // Code only: a comment may name the API it describes.
    const code = readFileSync(new URL(name, src), "utf-8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/.*$/gm, "");
    expect(code).not.toMatch(/\.(?:registerTool|registerCommand)\(/);
    expect(code).not.toMatch(/\bpi\.(?:on|setActiveTools)\(/);
  });

  it("covers the implementation", () => {
    expect(files).toEqual(expect.arrayContaining(["extension.ts", "impl.ts", "mcpBridge.ts"]));
  });
});

describe("forwarding", () => {
  it("registers each of the 8 events once and forwards each, with its result, to the active implementation", async () => {
    const calls: string[] = [];
    const pi = fakePi();
    const { shim } = host(fakeImpl("v1", calls));
    await shim.attach(pi.api as never);

    expect([...pi.handlers.keys()].sort()).toEqual([...SHIM_EVENTS].sort());
    for (const name of SHIM_EVENTS) expect(pi.handlers.get(name)).toHaveLength(1);
    for (const name of SHIM_EVENTS) {
      const payload = { type: name, reason: "startup" };
      await expect(pi.emit(name, payload)).resolves.toEqual({ by: "v1", name, event: payload });
    }
    expect(calls.filter((c) => !c.endsWith("prepare") && !c.endsWith("activate"))).toEqual(SHIM_EVENTS.map((name) => `v1:${name}`));
  });

  it("registers both commands and forwards them to the active implementation", async () => {
    const calls: string[] = [];
    const pi = fakePi();
    const { shim } = host(fakeImpl("v1", calls));
    await shim.attach(pi.api as never);

    expect([...pi.commands.keys()].sort()).toEqual(["probe-mcp-login", "probe-status"]);
    await pi.commands.get("probe-status")!.handler("now", fakeCtx());
    await pi.commands.get("probe-mcp-login")!.handler("", fakeCtx());
    expect(calls).toContain("v1:/probe-status now");
    expect(calls).toContain("v1:/probe-mcp-login ");
  });

  it("lets an implementation's own error reach pi, as before the shim", async () => {
    const calls: string[] = [];
    const impl = fakeImpl("v1", calls);
    const prepare = impl.prepare.bind(impl);
    impl.prepare = (envArg) => {
      const candidate = prepare(envArg) as Candidate;
      (candidate.declarations.events as Record<string, Handler>)[ShimEvent.ToolCall] = () => {
        throw new Error("guard bug");
      };
      return candidate;
    };
    const pi = fakePi();
    const { shim } = host(impl);
    await shim.attach(pi.api as never);
    await expect(pi.emit(ShimEvent.ToolCall, {})).rejects.toThrow("guard bug");
  });

  it("binds a new pi runtime (a session switch) with the same implementation and state", async () => {
    const calls: string[] = [];
    const { shim } = host(fakeImpl("v1", calls, { tools: () => [tool("probe_t", "t", () => "ok")] }));
    const first = fakePi();
    await shim.attach(first.api as never);
    const second = fakePi();
    await shim.attach(second.api as never);

    expect(calls.filter((c) => c.endsWith("prepare"))).toEqual(["v1:prepare"]);
    expect([...second.handlers.keys()].sort()).toEqual([...SHIM_EVENTS].sort());
    expect(second.tools.has("probe_t")).toBe(true);
    await expect(second.emit(ShimEvent.Input, {})).resolves.toMatchObject({ by: "v1" });
  });
});

describe("bootstrap", () => {
  it("starts on the clone's implementation when no version is pinned", async () => {
    const calls: string[] = [];
    const { shim, imported } = host(fakeImpl("clone", calls));
    await shim.attach(fakePi().api as never);
    expect(shim.activePin).toBeNull();
    expect(imported).toEqual([]);
    expect(liveRecord()).toMatchObject({ pin: null, fallback_pin: null, bad_pins: [], shim_api: SHIM_API, state_schema: 1 });
  });

  it("starts on the pin `current` names when it is complete", async () => {
    makePin("0.7.0-abc", "0.7.0");
    setCurrent("0.7.0-abc", "0.7.0");
    const calls: string[] = [];
    const { shim } = host(fakeImpl("clone", calls), { "0.7.0-abc": fakeImpl("pin", calls) });
    const pi = fakePi();
    await shim.attach(pi.api as never);

    expect(shim.activePin).toBe("0.7.0-abc");
    expect(calls).toEqual(["pin:prepare", "pin:activate"]);
    await expect(pi.emit(ShimEvent.SessionCompact, {})).resolves.toMatchObject({ by: "pin" });
    expect(liveRecord()).toMatchObject({ pin: "0.7.0-abc", fallback_pin: null, resources_pin: "0.7.0-abc", version: "0.7.0" });
  });

  it("never runs a pin of a version the clone no longer holds (a `pi update` by hand, not pinned yet)", async () => {
    makePin("0.7.0-abc", "0.7.0");
    setCurrent("0.7.0-abc", "0.7.0");
    writeFileSync(join(installRoot, "package.json"), JSON.stringify({ name: "probe-research-pi", version: "0.7.1" }));
    const calls: string[] = [];
    const { shim } = host(fakeImpl("clone", calls), { "0.7.0-abc": fakeImpl("pin", calls) });
    await shim.attach(fakePi().api as never);

    expect(shim.activePin).toBeNull();
    expect(calls).toEqual(["clone:prepare", "clone:activate"]);
  });

  it("starts while `pi update` reinstalls the clone's packages, and runs the pin at the next turn", async () => {
    const calls: string[] = [];
    let loads = 0;
    const missing = async () => {
      loads += 1;
      throw new Error("Cannot find module '@modelcontextprotocol/sdk'");
    };
    const { shim } = host(missing as unknown as ImplModule, { "0.6.3-abc": fakeImpl("pin", calls) });
    const pi = fakePi();
    await expect(shim.attach(pi.api as never)).resolves.toBeUndefined();
    expect(shim.activePin).toBeUndefined();  // nothing runs yet, and pi started
    expect(loads).toBe(1);
    expect(logs.some((line) => line.includes("did not start"))).toBe(true);

    await pi.emit(ShimEvent.SessionStart, { reason: "startup" });  // nothing runs: no implementation gets it
    makePin("0.6.3-abc", "0.6.3");
    setCurrent("0.6.3-abc", "0.6.3");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(shim.activePin).toBe("0.6.3-abc");
    // The pin gets the session start it missed (capture, MCP, the team note start there), then the turn.
    expect(calls).toEqual(["pin:prepare", "pin:activate", "pin:session_start", "pin:before_agent_start"]);
  });

  it("tells the person once to /reload when nothing could load and no pin runs (a `pi update` by hand)", async () => {
    const missing = async () => {
      throw new Error("Cannot find module '@modelcontextprotocol/sdk'");
    };
    const { shim } = host(missing as unknown as ImplModule);
    const pi = fakePi();
    await shim.attach(pi.api as never);
    const ctx = fakeCtx();
    await pi.emit(ShimEvent.SessionStart, { reason: "startup" }, ctx);
    await pi.emit(ShimEvent.BeforeAgentStart, {}, ctx);
    await pi.emit(ShimEvent.BeforeAgentStart, {}, ctx);
    expect(shim.activePin).toBeUndefined();
    expect(ctx.ui.notify).toHaveBeenCalledTimes(1);
    expect(ctx.ui.notify).toHaveBeenCalledWith(
      "Probe did not load in this pi (its packages were being reinstalled); run /reload in pi to load it", "warning");
  });

  it("loads the clone's implementation through a loader only when no pin runs", async () => {
    const calls: string[] = [];
    let loads = 0;
    const loader = async () => {
      loads += 1;
      return fakeImpl("clone", calls);
    };
    makePin("0.7.0-abc", "0.7.0");
    setCurrent("0.7.0-abc", "0.7.0");
    const { shim } = host(loader as unknown as ImplModule, { "0.7.0-abc": fakeImpl("pin", calls) });
    await shim.attach(fakePi().api as never);
    expect(shim.activePin).toBe("0.7.0-abc");
    expect(loads).toBe(0);
  });

  it("looks at a turned-down `current` again at the next event: the clone may have been mid-update", async () => {
    makePin("0.7.1-abc", "0.7.1");
    setCurrent("0.7.1-abc", "0.7.1");
    writeFileSync(join(installRoot, "package.json"), "{");  // pi update rewriting it: unreadable for a moment
    const calls: string[] = [];
    const { shim } = host(fakeImpl("clone", calls), { "0.7.1-abc": fakeImpl("pin", calls) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    expect(shim.activePin).toBeNull();

    writeFileSync(join(installRoot, "package.json"), JSON.stringify({ name: "probe-research-pi", version: "0.7.1" }));
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(shim.activePin).toBe("0.7.1-abc");
    expect(logs.filter((line) => line.includes("not the version the clone holds")).length).toBe(1);
  });

  it("never runs a pin made from another install (a local checkout pi now loads)", async () => {
    makePin("0.7.0-abc", "0.7.0");
    const path = currentFile(env);
    mkdirSync(join(path, ".."), { recursive: true });
    writeFileSync(path, JSON.stringify({ pin: "0.7.0-abc", version: "0.7.0", from: "/elsewhere" }));
    const calls: string[] = [];
    const { shim } = host(fakeImpl("clone", calls), { "0.7.0-abc": fakeImpl("pin", calls) });
    await shim.attach(fakePi().api as never);

    expect(shim.activePin).toBeNull();
    expect(calls).toEqual(["clone:prepare", "clone:activate"]);
    expect(liveRecord()).toMatchObject({ pin: null, resources_pin: null });
  });

  it("starts on the clone when the pin is not complete, or does not load", async () => {
    makePin("half", "0.7.0", false);
    setCurrent("half");
    const calls: string[] = [];
    let { shim } = host(fakeImpl("clone", calls), { half: fakeImpl("pin", calls) });
    await shim.attach(fakePi().api as never);
    expect(shim.activePin).toBeNull();

    makePin("broken");
    setCurrent("broken");
    ({ shim } = host(fakeImpl("clone", calls), { broken: new Error("SyntaxError: nope") }));
    await shim.attach(fakePi().api as never);
    expect(shim.activePin).toBeNull();
    expect(liveRecord().bad_pins).toEqual(["broken"]);
  });

  it("falls back to the clone when the pin's activate throws, stopping what it started", async () => {
    makePin("p1");
    setCurrent("p1");
    const calls: string[] = [];
    const { shim } = host(fakeImpl("clone", calls), { p1: fakeImpl("pin", calls, { activateThrows: new Error("half started") }) });
    await shim.attach(fakePi().api as never);
    expect(shim.activePin).toBeNull();
    expect(calls).toEqual(["pin:prepare", "pin:activate", "pin:deactivate", "clone:prepare", "clone:activate"]);
    expect(liveRecord()).toMatchObject({ pin: null, bad_pins: ["p1"] });
  });

  it("never throws into pi, even when no implementation starts", async () => {
    const calls: string[] = [];
    const { shim } = host(fakeImpl("clone", calls, { prepareThrows: new Error("boom") }));
    const pi = fakePi();
    await expect(shim.attach(pi.api as never)).resolves.toBeUndefined();
    expect(shim.activePin).toBeUndefined();
    await expect(pi.emit(ShimEvent.BeforeAgentStart, {})).resolves.toBeUndefined();
    expect(logs.join("\n")).toContain("did not start");
  });
});

describe("switching versions", () => {
  async function running(modules: Record<string, ImplModule | Error>, clone?: ImplModule) {
    const calls: string[] = [];
    const h = host(clone ?? fakeImpl("v1", calls, { tools: () => [tool("probe_t", "v1 tool", () => "v1 result")] }), modules);
    const pi = fakePi();
    await h.shim.attach(pi.api as never);
    return { ...h, pi, calls };
  }

  it("switches only at a turn boundary, never on a tool call, a settle or anything else", async () => {
    const calls: string[] = [];
    makePin("p2");
    const { shim, pi } = await running({ p2: fakeImpl("v2", calls) });
    setCurrent("p2");

    for (const name of [ShimEvent.ToolCall, ShimEvent.AgentSettled, ShimEvent.Input, ShimEvent.SessionCompact, ShimEvent.ResourcesDiscover]) {
      await expect(pi.emit(name, {})).resolves.toMatchObject({ by: "v1" });
    }
    await pi.commands.get("probe-status")!.handler("", fakeCtx());
    expect(shim.activePin).toBeNull();
    expect(calls).toEqual([]);

    await expect(pi.emit(ShimEvent.BeforeAgentStart, {})).resolves.toMatchObject({ by: "v2", name: ShimEvent.BeforeAgentStart });
    expect(shim.activePin).toBe("p2");
    await expect(pi.emit(ShimEvent.ToolCall, {})).resolves.toMatchObject({ by: "v2" });
  });

  it("switches at session_start too, before the new version handles it", async () => {
    const calls: string[] = [];
    makePin("p2");
    const { pi } = await running({ p2: fakeImpl("v2", calls) });
    setCurrent("p2");
    await expect(pi.emit(ShimEvent.SessionStart, { reason: "new" })).resolves.toMatchObject({ by: "v2" });
    expect(calls).toEqual(["v2:prepare", "v2:activate", "v2:session_start"]);
  });

  it("runs prepare on a copy, then deactivates the old version, then activates the new one, in that order", async () => {
    const order: string[] = [];
    makePin("p2");
    const clone = fakeImpl("v1", order);
    const { pi, shim } = await running({ p2: fakeImpl("v2", order) }, clone);
    const before = shim.sharedState;
    order.length = 0;
    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(order).toEqual(["v2:prepare", "v1:deactivate", "v2:activate", "v2:before_agent_start"]);
    expect(shim.sharedState).not.toBe(before);
    // The containers inside carry over: a deactivated version's late write still lands.
    expect(shim.sharedState.session.turns).toBe(before.session.turns);
  });

  it("re-registers a changed tool, registers a new one, hides a removed one, and calls the new closures only", async () => {
    const calls: string[] = [];
    makePin("p2");
    const v1Calls: string[] = [];
    const v2Calls: string[] = [];
    const clone = fakeImpl("v1", calls, {
      tools: () => [tool("probe_same", "same", () => v1Calls.push("same")), tool("probe_changed", "old words", () => v1Calls.push("changed")), tool("probe_gone", "gone", () => v1Calls.push("gone"))],
    });
    const v2 = fakeImpl("v2", calls, {
      tools: () => [tool("probe_same", "same", () => v2Calls.push("same")), tool("probe_changed", "new words", () => v2Calls.push("changed")), tool("probe_new", "new", () => v2Calls.push("new"))],
    });
    const { pi } = await running({ p2: v2 }, clone);
    const registeredAt = (name: string) => pi.api.registerTool.mock.calls.filter(([t]) => t.name === name).length;
    expect(pi.activeTools()).toEqual(["read", "bash", "probe_same", "probe_changed", "probe_gone"]);
    const sameDefinition = pi.tools.get("probe_same");

    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});

    expect(registeredAt("probe_same")).toBe(1); // unchanged: not registered again
    expect(pi.tools.get("probe_same")).toBe(sameDefinition);
    expect(registeredAt("probe_changed")).toBe(2);
    expect(pi.tools.get("probe_changed")!.description).toBe("new words");
    expect(registeredAt("probe_new")).toBe(1);
    expect(pi.activeTools()).toEqual(["read", "bash", "probe_same", "probe_changed", "probe_new"]);

    for (const name of ["probe_same", "probe_changed", "probe_new"]) {
      await (pi.tools.get(name)!.execute as (...a: unknown[]) => Promise<unknown>)("call-1", {});
    }
    expect(v1Calls).toEqual([]); // the old closures are never called again
    expect(v2Calls).toEqual(["same", "changed", "new"]);
    // A removed tool a stale caller still holds answers plainly.
    await expect((pi.tools.get("probe_gone")!.execute as (...a: unknown[]) => Promise<unknown>)("call-2", {})).rejects.toThrow(
      "not offered by the Probe version running now",
    );
  });

  it("re-registers a command whose description changed", async () => {
    const calls: string[] = [];
    makePin("p2");
    const { pi } = await running({ p2: fakeImpl("v2", calls, { commandDescription: "a new description" }) });
    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(pi.api.registerCommand).toHaveBeenCalledTimes(4);
    expect(pi.commands.get("probe-status")!.description).toBe("a new description");
    await pi.commands.get("probe-status")!.handler("x", fakeCtx());
    expect(calls).toContain("v2:/probe-status x");
  });

  it("applies tools an implementation declares later (`declarationsChanged`), and ignores a replaced version's", async () => {
    let v1Tools: ToolDeclaration[] = [];
    let v1Env: ImplEnv | undefined;
    const calls: string[] = [];
    const clone = fakeImpl("v1", calls, {
      tools: (e) => {
        v1Env = e;
        return v1Tools;
      },
    });
    makePin("p2");
    const { pi } = await running({ p2: fakeImpl("v2", calls) }, clone);
    v1Tools = [tool("probe_late", "late", () => "late")];
    v1Env!.api.declarationsChanged();
    expect(pi.tools.has("probe_late")).toBe(true);

    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(pi.activeTools()).not.toContain("probe_late"); // v2 does not offer it

    v1Tools = [tool("probe_late", "late", () => "late"), tool("probe_stale", "stale", () => "stale")];
    v1Env!.api.declarationsChanged(); // the replaced version, from in-flight work
    expect(pi.tools.has("probe_stale")).toBe(false);
  });
});

describe("a switch that fails keeps the running version", () => {
  async function runningV1() {
    const calls: string[] = [];
    const v1Calls: string[] = [];
    const clone = fakeImpl("v1", calls, {
      tools: () => [tool("probe_t", "v1 words", () => v1Calls.push("t"))],
    });
    return { calls, v1Calls, clone };
  }

  it("when the import fails: the old version keeps working, the pin is bad and never retried", async () => {
    const { calls, clone } = await runningV1();
    makePin("p2");
    const { shim, imported } = host(clone, { p2: new Error("Cannot find module '@modelcontextprotocol/sdk'") });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    setCurrent("p2");

    for (let turn = 0; turn < 3; turn++) {
      await expect(pi.emit(ShimEvent.BeforeAgentStart, {})).resolves.toMatchObject({ by: "v1" });
    }
    expect(imported).toEqual(["p2"]);
    expect(shim.activePin).toBeNull();
    expect(calls).not.toContain("v1:deactivate");
    expect(liveRecord().bad_pins).toEqual(["p2"]);
    expect(logs.join("\n")).toContain("pin p2: import failed: Cannot find module");
  });

  it("when prepare throws: nothing was stopped, the pin is bad and never retried", async () => {
    const { calls, clone } = await runningV1();
    makePin("p2");
    const { shim, imported } = host(clone, { p2: fakeImpl("v2", calls, { prepareThrows: new Error("bad state") }) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    await pi.emit(ShimEvent.SessionStart, { reason: "new" });

    expect(imported).toEqual(["p2"]);
    expect(calls.filter((c) => c.startsWith("v2:"))).toEqual(["v2:prepare"]);
    expect(calls).not.toContain("v1:deactivate");
    expect(shim.activePin).toBeNull();
    expect(liveRecord().bad_pins).toEqual(["p2"]);
  });

  it("when activate throws: the old declarations come back, the old version is activated again on the untouched state", async () => {
    const { calls, v1Calls, clone } = await runningV1();
    makePin("p2");
    const v2 = fakeImpl("v2", calls, {
      activateThrows: new Error("activate exploded"),
      tools: () => [tool("probe_t", "v2 words", () => "v2"), tool("probe_v2_only", "v2", () => "v2")],
    });
    const { shim, imported } = host(clone, { p2: v2 });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    const before = shim.sharedState;
    const activeBefore = [...pi.activeTools()];
    setCurrent("p2");

    await expect(pi.emit(ShimEvent.BeforeAgentStart, {})).resolves.toMatchObject({ by: "v1" });

    expect(calls).toEqual(["v1:prepare", "v1:activate", "v2:prepare", "v1:deactivate", "v2:activate", "v2:deactivate", "v1:activate", "v1:before_agent_start"]);
    expect(pi.tools.get("probe_t")!.description).toBe("v1 words");
    expect(pi.activeTools()).toEqual(activeBefore);
    expect(shim.sharedState).toBe(before);
    expect(shim.activePin).toBeNull();
    await (pi.tools.get("probe_t")!.execute as (...a: unknown[]) => Promise<unknown>)("c", {});
    expect(v1Calls).toEqual(["t"]);
    expect(liveRecord().bad_pins).toEqual(["p2"]);

    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(imported).toEqual(["p2"]);
  });

  it("tries a newer pin after a bad one", async () => {
    const { calls, clone } = await runningV1();
    makePin("p2");
    makePin("p3");
    const { shim } = host(clone, { p2: new Error("broken"), p3: fakeImpl("v3", calls) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    setCurrent("p3");
    await expect(pi.emit(ShimEvent.BeforeAgentStart, {})).resolves.toMatchObject({ by: "v3" });
    expect(liveRecord()).toMatchObject({ pin: "p3", bad_pins: ["p2"] });
  });
});

describe("a version that needs a newer shim", () => {
  it("keeps the running version, records shim_reload_needed and says /reload once", async () => {
    const calls: string[] = [];
    makePin("p2");
    const { shim, imported } = host(fakeImpl("v1", calls), { p2: fakeImpl("v2", calls, { requires: SHIM_API + 1 }) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    setCurrent("p2");

    const ctx = fakeCtx();
    await expect(pi.emit(ShimEvent.BeforeAgentStart, {}, ctx)).resolves.toMatchObject({ by: "v1" });
    await pi.emit(ShimEvent.BeforeAgentStart, {}, ctx);
    await pi.emit(ShimEvent.SessionStart, { reason: "new" }, ctx);

    expect(shim.activePin).toBeNull();
    expect(imported).toEqual(["p2"]);
    expect(calls.filter((c) => c.startsWith("v2:"))).toEqual([]);
    expect(ctx.ui.notify).toHaveBeenCalledTimes(1);
    expect(ctx.ui.notify).toHaveBeenCalledWith("Probe updated; run /reload in pi to apply", "info");
    expect(liveRecord()).toMatchObject({ shim_reload_needed: true, bad_pins: [], pin: null });
  });

  it("at bootstrap too: the clone runs, and the notice waits for the first event", async () => {
    const calls: string[] = [];
    makePin("p2");
    setCurrent("p2");
    const { shim } = host(fakeImpl("clone", calls), { p2: fakeImpl("v2", calls, { requires: SHIM_API + 1 }) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    expect(shim.activePin).toBeNull();
    const ctx = fakeCtx();
    await pi.emit(ShimEvent.SessionStart, { reason: "startup" }, ctx);
    expect(ctx.ui.notify).toHaveBeenCalledWith("Probe updated; run /reload in pi to apply", "info");
    expect(liveRecord().shim_reload_needed).toBe(true);
  });
});

describe("the shared state's schema", () => {
  it("a version that migrates the running schema takes over with the migrated copy; the old state is untouched", async () => {
    const calls: string[] = [];
    makePin("p2");
    const v2 = fakeImpl("v2", calls, {
      schema: 2,
      migratesFrom: [1],
      migrate: (shared) => {
        const old = shared.session.turns as { count: number };
        shared.session.turns = { count: old.count, migrated: true };
      },
    });
    const { shim } = host(fakeImpl("v1", calls), { p2: v2 });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    const before = shim.sharedState;
    const turnsBefore = before.session.turns;
    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});

    expect(shim.activePin).toBe("p2");
    expect(shim.sharedState.schema).toBe(2);
    expect(shim.sharedState.session.turns).toEqual({ count: 0, migrated: true });
    expect(before.schema).toBe(1);
    expect(before.session.turns).toBe(turnsBefore);
    expect(turnsBefore).toEqual({ count: 0 });
    expect(liveRecord().state_schema).toBe(2);
  });

  it("a version that cannot take over the running schema is not started, and the old one keeps running", async () => {
    const calls: string[] = [];
    makePin("p2");
    const { shim } = host(fakeImpl("v1", calls), { p2: fakeImpl("v2", calls, { schema: 3, migratesFrom: [2] }) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    setCurrent("p2");
    await expect(pi.emit(ShimEvent.BeforeAgentStart, {})).resolves.toMatchObject({ by: "v1" });
    expect(calls.filter((c) => c.startsWith("v2:"))).toEqual([]);
    expect(liveRecord()).toMatchObject({ bad_pins: ["p2"], state_schema: 1 });
  });

  it("prepare's own UnsupportedStateSchemaError is a bad pin too", async () => {
    const calls: string[] = [];
    makePin("p2");
    // Claims to migrate 1 but refuses in prepare: the typed error is caught.
    const v2 = fakeImpl("v2", calls, { schema: 2, migratesFrom: [1] });
    const prepare = v2.prepare.bind(v2);
    v2.prepare = (e) => {
      prepare(e);
      throw new UnsupportedStateSchemaError(1, [2]);
    };
    const { shim } = host(fakeImpl("v1", calls), { p2: v2 });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(shim.activePin).toBeNull();
    expect(logs.join("\n")).toContain("state schema:");
  });

  it("copyShared copies the two records and keeps their values", () => {
    const shared: SharedState = { schema: 1, session: { a: { n: 1 } }, transports: { mcp: new Map() } };
    const copy = copyShared(shared);
    expect(copy.session).not.toBe(shared.session);
    expect(copy.transports).not.toBe(shared.transports);
    expect(copy.session.a).toBe(shared.session.a);
    copy.session.a = { n: 2 };
    expect(shared.session.a).toEqual({ n: 1 });
    expect(emptySharedState()).toEqual({ schema: 0, session: {}, transports: {} });
  });
});

describe("skills pi has to load again", () => {
  it("a switch at a prompt to a version with other skills sets skills_reload_needed; the code still switches", async () => {
    const calls: string[] = [];
    makePin("p2");
    const { shim } = host(fakeImpl("v1", calls, { fingerprint: "aaa" }), { p2: fakeImpl("v2", calls, { fingerprint: "bbb" }) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(shim.activePin).toBe("p2");
    expect(liveRecord()).toMatchObject({ skills_reload_needed: true, fallback_pin: null });

    // A new session runtime discovers resources again, from the running version.
    await pi.emit(ShimEvent.SessionStart, { reason: "new" });
    expect(liveRecord().skills_reload_needed).toBe(false);
  });

  it("the same skills need nothing", async () => {
    const calls: string[] = [];
    makePin("p2");
    const { shim } = host(fakeImpl("v1", calls, { fingerprint: "aaa" }), { p2: fakeImpl("v2", calls, { fingerprint: "aaa" }) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    setCurrent("p2");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(liveRecord().skills_reload_needed).toBe(false);
  });

  it("a switch at session_start needs nothing: pi discovers resources right after it", async () => {
    const calls: string[] = [];
    makePin("p2");
    const { shim } = host(fakeImpl("v1", calls, { fingerprint: "aaa" }), { p2: fakeImpl("v2", calls, { fingerprint: "bbb" }) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    setCurrent("p2");
    await pi.emit(ShimEvent.SessionStart, { reason: "resume" });
    expect(liveRecord().skills_reload_needed).toBe(false);
  });
});

describe("the live record", () => {
  it("is written at start, rewritten when a field changes, and removed when pi quits", async () => {
    let recorded = false;
    const calls: string[] = [];
    const clone = fakeImpl("v1", calls);
    const prepare = clone.prepare.bind(clone);
    clone.prepare = (e) => ({ ...(prepare(e) as Candidate), status: () => ({ recorded }) });
    makePin("p1", "0.7.0");
    makePin("p2", "0.8.0");
    let now = 1_761_400_000_000;
    const { shim } = host(clone, { p1: fakeImpl("p1", calls, { fingerprint: "one" }), p2: fakeImpl("p2", calls, { fingerprint: "two" }) }, { now: () => now });
    setCurrent("p1", "0.7.0");
    const pi = fakePi();
    await shim.attach(pi.api as never);

    const first = liveRecord();
    expect(first).toEqual({
      shim_api: 1,
      state_schema: 1,
      pin: "p1",
      fallback_pin: null,
      resources_pin: "p1",
      pins_used: ["p1"],
      install: realpathSync(cloneDir),
      bad_pins: [],
      recorded: false,
      skills_reload_needed: false,
      shim_reload_needed: false,
      version: "0.7.0",
      started: 1_761_400_000,
      updated: 1_761_400_000,
    });

    // Nothing changed: not rewritten (updated stays).
    now += 5_000;
    await pi.emit(ShimEvent.AgentSettled, {});
    expect(liveRecord().updated).toBe(1_761_400_000);

    // The switch: the pin pi loaded skills from stays referenced.
    setCurrent("p2", "0.8.0");
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(liveRecord()).toMatchObject({ pin: "p2", fallback_pin: "p1", pins_used: ["p1", "p2"], version: "0.8.0", skills_reload_needed: true, updated: 1_761_400_005 });

    // A session switch keeps the record; a quit removes it.
    await pi.emit(ShimEvent.SessionShutdown, { reason: "new" });
    expect(existsSync(liveRecordFile(4242, env))).toBe(true);
    await pi.emit(ShimEvent.SessionShutdown, { reason: "quit" });
    expect(existsSync(liveRecordFile(4242, env))).toBe(false);
    await pi.emit(ShimEvent.AgentSettled, {});
    expect(existsSync(liveRecordFile(4242, env))).toBe(false);
  });

  it("says when the Probe service records the session", async () => {
    let recorded = false;
    const calls: string[] = [];
    const clone = fakeImpl("v1", calls);
    const prepare = clone.prepare.bind(clone);
    clone.prepare = (e) => ({ ...(prepare(e) as Candidate), status: () => ({ recorded }) });
    const { shim } = host(clone);
    const pi = fakePi();
    await shim.attach(pi.api as never);
    expect(liveRecord().recorded).toBe(false);
    recorded = true;
    await pi.emit(ShimEvent.AgentSettled, {});
    expect(liveRecord().recorded).toBe(true);
  });

  it("reads `current` only when the file changed", async () => {
    const calls: string[] = [];
    makePin("p2");
    setCurrent("p2");
    const { shim } = host(fakeImpl("v1", calls), { p2: fakeImpl("v2", calls) });
    const pi = fakePi();
    await shim.attach(pi.api as never);
    expect(shim.activePin).toBe("p2");
    // A malformed or unsafe `current` names nothing: the running version stays.
    writeFileSync(currentFile(env), JSON.stringify({ pin: "../../etc" }));
    await pi.emit(ShimEvent.BeforeAgentStart, {});
    expect(shim.activePin).toBe("p2");
  });
});
