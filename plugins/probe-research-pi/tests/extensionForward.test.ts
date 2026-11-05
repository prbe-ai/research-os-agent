/**
 * pi's events go to the Probe service (PR 7, D50; service-only task 16: every turn, on every
 * machine), through the real shim and implementation, against a stand-in service on a real Unix
 * socket. A service that does not take them leaves the turn unchanged: pi runs no code of its own.
 *
 * Its own file: the socket path is asked once per pi process, so every test here shares one.
 */
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";

import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { answer, denial, fakeService, socketFolder, type Behaviour, type FakeService } from "./fakeService.js";

const SOCKET = join(socketFolder(), "service.sock");

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
    if (args.join(" ") === "-m tap service socket") {
      callback(null, JSON.stringify({ socket: SOCKET }) + "\n", "");
      return;
    }
    callback(null, JSON.stringify({ session_id: args[args.indexOf("--session") + 1], tracking: true, signal: "on", seeded: true, source: "machine" }), "");
  },
}));

const { registerExtension } = await import("./shimHarness.js");
const { findServiceDir } = await import("../src/forwarding.js");
const { CAP_HOOKS_PROXY_V2 } = await import("../src/serviceForward.js");

let root: string;
let extensionDir: string;
let service: FakeService | null = null;

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "pi-ext-fwd-"));
  extensionDir = join(root, "plugins", "probe-research-pi");
  mkdirSync(extensionDir, { recursive: true });
  for (const folder of ["probe-research", "probe-research-daemon"]) {
    mkdirSync(join(root, "plugins", folder, "hooks"), { recursive: true });
    writeFileSync(join(root, "plugins", folder, "hooks", "dispatch.py"), "");
  }
  const bin = join(root, "bin");
  mkdirSync(bin);
  for (const name of ["probe", "python3"]) {
    writeFileSync(join(bin, name), "#!/bin/sh\nexit 0\n");
    chmodSync(join(bin, name), 0o755);
  }
  process.env.PATH = `${bin}${delimiter}${process.env.PATH ?? ""}`;
  process.env.PROBE_PI_TAP_PLUGIN_DIR = join(root, "tap-state");
});

afterAll(() => rmSync(root, { recursive: true, force: true }));

afterEach(async () => {
  await service?.close();
  service = null;
});

async function serve(behaviour: (request: Record<string, unknown>) => Behaviour, caps = [CAP_HOOKS_PROXY_V2]): Promise<FakeService> {
  service = await fakeService(SOCKET, behaviour, { caps });
  return service;
}

/** v2 events the service was asked, by pi's own name. */
function forwarded(fake: FakeService): string[] {
  return fake.requests
    .filter((r) => r.method === "hooks.run" && (r.params as Record<string, unknown>).v === 2)
    .map((r) => (r.params as Record<string, unknown>).event as string);
}

function eventOf(request: Record<string, unknown>): string | undefined {
  return (request.params as Record<string, unknown> | undefined)?.event as string | undefined;
}

let sessions = 0;
async function start(): Promise<{ on: (name: string) => (event: unknown, ctx: unknown) => Promise<any>; ctx: any }> {
  const handlers = new Map<string, (event: unknown, ctx: unknown) => unknown>();
  const api = {
    on: (event: string, handler: (event: unknown, ctx: unknown) => unknown) => handlers.set(event, handler),
    registerCommand: () => {},
    sendMessage: () => {},
    getActiveTools: () => ["read", "bash", "edit", "write"],
    setActiveTools: () => {},
  };
  await registerExtension(api as never, extensionDir);
  sessions += 1;
  const sessionId = `fwd-${sessions}-${Date.now()}`;
  const notices: string[] = [];
  const ctx = {
    hasUI: true,
    notices,
    ui: { notify: (text: string) => notices.push(text), setStatus: () => {}, select: async () => undefined },
    cwd: root,
    sessionManager: { getSessionId: () => sessionId, getSessionFile: () => undefined },
  };
  await handlers.get("session_start")!({ reason: "startup" }, ctx);
  return { on: (name) => handlers.get(name) as (event: unknown, ctx: unknown) => Promise<any>, ctx };
}

describe("every turn goes to the service", () => {
  it("runs the session start there and keeps its context for every turn's prompt", async () => {
    const fake = await serve((request) => {
      if (eventOf(request) === "session_start") return { result: answer({ actions: [{ kind: "context", text: "SERVICE CONTEXT" }, { kind: "notify", text: "hello person", level: "info" }] }) };
      if (eventOf(request) === "before_agent_start") {
        return { result: answer({ actions: [{ kind: "message", text: "a daemon answer" }, { kind: "receipt", receipt: "cd".repeat(16) }] }) };
      }
      if (request.method === "hooks.ack") return { result: { acked: true } };
      return { result: answer({ actions: [] }) };
    });
    const { on, ctx } = await start();
    expect(ctx.notices).toContain("hello person");
    const turn = await on("before_agent_start")({ prompt: "hi", systemPrompt: "BASE", systemPromptOptions: {} }, ctx);
    expect(turn.systemPrompt).toContain("BASE");
    expect(turn.systemPrompt).toContain("SERVICE CONTEXT");
    expect(turn.message).toEqual({ customType: "probe-daemon", content: "a daemon answer", display: false });
    expect(forwarded(fake)).toEqual(["session_start", "before_agent_start"]);
    await vi.waitFor(() => expect(fake.requests.some((r) => r.method === "hooks.ack")).toBe(true));
    expect(fake.requests.find((r) => r.method === "hooks.ack")!.params).toEqual({ receipt: "cd".repeat(16), shown: true });
  });

  it("asks the service's pre-tool routes, and only them: pi's own guard does not run", async () => {
    const fake = await serve((request) => (eventOf(request) === "tool_call"
      ? { result: answer({ actions: [{ kind: "block", text: "`bash` refused by the routes" }] }) }
      : { result: answer({ actions: [] }) }));
    const { on, ctx } = await start();
    await on("before_agent_start")({ prompt: "hi", systemPrompt: "BASE", systemPromptOptions: {} }, ctx);
    expect(await on("tool_call")({ toolName: "bash", toolCallId: "c1", input: { command: "probe x" } }, ctx))
      .toEqual({ block: true, reason: "`bash` refused by the routes" });
    const run = fake.requests.filter((r) => eventOf(r) === "tool_call").at(-1)!.params as Record<string, unknown>;
    expect(JSON.parse(run.payload as string)).toMatchObject({ tool_name: "bash", input: { command: "probe x" }, tool_call_id: "c1" });
    // The service's answer is the whole answer (Richard 2026-11-03: hooks are plumbing).
    await service!.close();
    await serve(() => ({ result: answer({ actions: [] }) }));
    const { approvalsDir } = await import("../src/core/paths.js");
    expect(await on("tool_call")({ toolName: "write", input: { path: join(approvalsDir(process.env), "answers", "x.json"), content: "{}" } }, ctx))
      .toBeUndefined();
  });

  it("sends a big write without its body, and refuses a tool call too big to check without asking (C1)", async () => {
    const fake = await serve(() => ({ result: answer({ actions: [] }) }));
    const { on, ctx } = await start();
    await on("before_agent_start")({ prompt: "hi", systemPrompt: "BASE", systemPromptOptions: {} }, ctx);
    const big = "x".repeat(2 << 20);
    expect(await on("tool_call")({ toolName: "write", toolCallId: "w1", input: { path: "notes.md", content: big } }, ctx)).toBeUndefined();
    const sent = JSON.parse((fake.requests.filter((r) => eventOf(r) === "tool_call").at(-1)!.params as Record<string, unknown>).payload as string);
    expect(sent.input).toEqual({ path: "notes.md" });
    const before = fake.requests.length;
    for (const size of [300 * 1024, 1200 * 1024]) {
      const refused = await on("tool_call")({ toolName: "bash", toolCallId: "b1", input: { command: "echo " + "a".repeat(size) } }, ctx);
      expect(refused.block).toBe(true);
      expect(refused.reason).toContain("too large for Probe's guard");
    }
    expect(fake.requests.length).toBe(before);
  });

  it("refuses only a forged tool call; out of time or any other refusal goes on", async () => {
    let reply: Record<string, unknown> = { refused: "the caller is not a running pi process", reason: "not_harness" };
    await serve((request) => (eventOf(request) === "tool_call" ? { result: reply } : { result: answer({ actions: [] }) }));
    const { on, ctx } = await start();
    await on("before_agent_start")({ prompt: "hi", systemPrompt: "BASE", systemPromptOptions: {} }, ctx);
    const forged = await on("tool_call")({ toolName: "bash", toolCallId: "c2", input: { command: "ls" } }, ctx);
    expect(forged.block).toBe(true);
    expect(forged.reason).toContain("found no running agent");
    // A slow service never stops the agent; a broken plugin folder (the service's `no_routes`) neither.
    reply = answer({ actions: [], exit_code: null, timed_out: true });
    expect(await on("tool_call")({ toolName: "bash", toolCallId: "c3", input: { command: "ls" } }, ctx)).toBeUndefined();
    reply = { refused: "the dispatcher would not run: the plugin folder is incomplete", reason: "no_routes" };
    expect(await on("tool_call")({ toolName: "bash", toolCallId: "c4", input: { command: "ls" } }, ctx)).toBeUndefined();
  });

  it("sends the typed switch only for a line a person typed", async () => {
    const fake = await serve(() => ({ result: answer({ actions: [] }) }));
    const { on, ctx } = await start();
    await on("before_agent_start")({ prompt: "hi", systemPrompt: "BASE", systemPromptOptions: {} }, ctx);
    const typed = await on("input")({ text: "/skill:track-work on", source: "interactive" }, ctx);
    expect(typed.action).toBe("transform");
    expect(typed.text).toContain("/skill:track-work on");
    expect(typed.text.length).toBeGreaterThan("/skill:track-work on".length); // the switch moved: pi's own notice follows
    const before = forwarded(fake).filter((e) => e === "input").length;
    await on("input")({ text: "/skill:track-work off", source: "rpc" }, ctx);
    expect(forwarded(fake).filter((e) => e === "input").length).toBe(before);
  });

  it("continues a completed run the service's owed route sends back, and never a stopped one", async () => {
    const fake = await serve((request) => (eventOf(request) === "agent_before_settle"
      ? { result: answer({ actions: [{ kind: "continue", text: "write your summary first" }] }) }
      : { result: answer({ actions: [] }) }));
    const { on, ctx } = await start();
    await on("before_agent_start")({ prompt: "hi", systemPrompt: "BASE", systemPromptOptions: {} }, ctx);
    expect(await on("agent_before_settle")({ outcome: "completed" }, ctx)).toEqual({
      entries: [{ type: "custom_message", customType: "probe-owed", content: "write your summary first", display: true }],
      continue: true,
    });
    expect(await on("agent_before_settle")({ outcome: "aborted" }, ctx)).toBeUndefined();
    expect(forwarded(fake).filter((e) => e === "agent_before_settle")).toHaveLength(1);
  });

  it("never ends the session at a reload", async () => {
    const fake = await serve(() => ({ result: answer({ actions: [] }) }));
    const { on, ctx } = await start();
    await on("before_agent_start")({ prompt: "hi", systemPrompt: "BASE", systemPromptOptions: {} }, ctx);
    await on("session_shutdown")({ reason: "reload" }, ctx);
    expect(forwarded(fake)).not.toContain("session_shutdown");
  });
});

describe("pi runs no code of its own when the service does not take the turn", () => {
  it("with no ledger mark at all: every turn goes to the service", async () => {
    const fake = await serve((request) => (eventOf(request) === "tool_call"
      ? { result: answer({ actions: [{ kind: "block", text: "`write` refused" }] }) }
      : { result: answer({ actions: [] }) }));
    const { on, ctx } = await start();
    await on("before_agent_start")({ prompt: "hi", systemPrompt: "BASE", systemPromptOptions: {} }, ctx);
    const refused = await on("tool_call")({ toolName: "write", input: { path: "notes.md", content: "x" } }, ctx);
    expect(refused).toEqual({ block: true, reason: "`write` refused" });
    expect(forwarded(fake)).toEqual(expect.arrayContaining(["session_start", "before_agent_start", "tool_call"]));
  });

  it("with a service from before v2: a turn goes on unchanged and queues its record-keeping", async () => {
    const fake = await serve(() => ({ result: denial("`Write` refused") }), []);
    const { on, ctx } = await start();
    expect(await on("before_agent_start")({ prompt: "hi", systemPrompt: "BASE", systemPromptOptions: {} }, ctx)).toBeUndefined();
    expect(await on("tool_call")({ toolName: "write", input: { path: "notes.md", content: "x" } }, ctx)).toBeUndefined();
    expect(await on("input")({ text: "/skill:track-work off", source: "interactive" }, ctx)).toEqual({ action: "continue" });
    // No event asked of v1 and nothing run here: the hellos, and the plain methods pi calls (its session state).
    expect(fake.requests.filter((r) => (r.params as Record<string, unknown> | undefined)?.payload !== undefined)).toEqual([]);
    const queue = join(findServiceDir(process.env)!, "hooks-queue");
    const before = existsSync(queue) ? readdirSync(queue).filter((name) => name.endsWith(".json")).length : 0;
    await on("agent_settled")({}, ctx);
    const items = readdirSync(queue).filter((name) => name.endsWith(".json"));
    expect(items.length).toBe(before + 1); // the turn end; the session start's was queued at its start, once
    const events = items.map((name) => JSON.parse(readFileSync(join(queue, name), "utf-8")));
    expect(events.filter((e) => e.event === "agent_settled")).toEqual([
      expect.objectContaining({ v: 2, routes: "service", harness: "pi", event: "agent_settled" }),
    ]);
  });
});
