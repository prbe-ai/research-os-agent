/**
 * The `tool_call` guard asks the Probe service first, and pi's own guard
 * (`guard.ts`) decides whenever the service does not (plan D38), wired through
 * a real pi-shaped extension: the service is a stand-in on a real Unix socket
 * (`fakeService.ts`), whose path the (mocked) `tap service socket` reports.
 *
 * Its own file: the socket path is asked once per pi process (module scope in
 * `extension.ts`), so every test here shares one path, and `extension.test.ts`
 * (whose `tap service socket` answers nothing) keeps testing `guard.ts` alone.
 */
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join, resolve } from "node:path";

import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { answer, denial, fakeService, socketFolder, type Behaviour, type FakeService } from "./fakeService.js";

const SOCKET = join(socketFolder(), "service.sock");
let profile: string | undefined;
let state: string | undefined;

const execFileMock = vi.fn(
  (_command: string, args: string[], _options: unknown, callback: (error: Error | null, stdout: string, stderr: string) => void) => {
    if (args.join(" ") === "-m tap service socket") {
      callback(null, JSON.stringify({ socket: SOCKET }) + "\n", "");
      return;
    }
    callback(
      null,
      JSON.stringify({
        session_id: args[args.indexOf("--session") + 1],
        tracking: state !== "off",
        signal: state === "off" ? "off" : "on",
        seeded: true,
        source: "machine",
        profile,
        ...(state ? { state } : {}),
      }),
      "",
    );
  },
);

vi.mock("node:child_process", () => ({
  spawn: () => ({
    pid: 4242,
    unref: () => {},
    once: (event: string, callback: (value: unknown) => void) => {
      if (event === "exit") queueMicrotask(() => callback(0));
    },
  }),
  execFileSync: () => "",
  execFile: (command: string, args: string[], options: unknown, callback: (error: Error | null, stdout: string, stderr: string) => void) =>
    execFileMock(command, args, options, callback),
}));

const { registerExtension } = await import("./shimHarness.js");
const { approvalsDir } = await import("../src/core/paths.js");

type ToolCall = (
  event: { toolName: string; toolCallId?: string; input: Record<string, unknown> },
  ctx: unknown,
) => Promise<{ block?: boolean; reason?: string } | undefined>;

let root: string;
let extensionDir: string;
let service: FakeService | null = null;

beforeAll(() => {
  // This package beside the two Probe plugin folders, as in the mirror pi installs.
  root = mkdtempSync(join(tmpdir(), "pi-ext-svc-"));
  extensionDir = join(root, "plugins", "probe-research-pi");
  mkdirSync(extensionDir, { recursive: true });
  for (const folder of ["probe-research", "probe-research-daemon"]) {
    mkdirSync(join(root, "plugins", folder, "hooks"), { recursive: true });
    writeFileSync(join(root, "plugins", folder, "hooks", "dispatch.py"), "");
  }
  const bin = join(root, "bin");
  mkdirSync(bin);
  writeFileSync(join(bin, "probe"), "#!/bin/sh\nexit 0\n");
  writeFileSync(join(bin, "python3"), "#!/bin/sh\nexit 0\n");
  chmodSync(join(bin, "probe"), 0o755);
  chmodSync(join(bin, "python3"), 0o755);
  process.env.PATH = `${bin}${delimiter}${process.env.PATH ?? ""}`;
  process.env.PROBE_PI_TAP_PLUGIN_DIR = join(root, "tap-state");
});

afterAll(() => rmSync(root, { recursive: true, force: true }));

afterEach(async () => {
  await service?.close();
  service = null;
  profile = undefined;
  state = undefined;
});

async function serve(behaviour: () => Behaviour): Promise<FakeService> {
  service = await fakeService(SOCKET, behaviour);
  return service;
}

let sessions = 0;
async function start(): Promise<{ toolCall: ToolCall; ctx: unknown }> {
  const handlers = new Map<string, (event: unknown, ctx: unknown) => unknown>();
  const api = {
    on: (event: string, handler: (event: unknown, ctx: unknown) => unknown) => handlers.set(event, handler),
    registerCommand: () => {},
    getActiveTools: () => ["read", "bash", "edit", "write"],
    setActiveTools: () => {},
  };
  await registerExtension(api as never, extensionDir);
  sessions += 1;
  const sessionId = `svc-guard-${sessions}-${Date.now()}`;
  const ctx = {
    hasUI: false,
    ui: { notify: () => {}, setStatus: () => {} },
    cwd: root,
    sessionManager: { getSessionId: () => sessionId, getSessionFile: () => undefined },
  };
  await handlers.get("session_start")!({ reason: "startup" }, ctx);
  return { toolCall: handlers.get("tool_call") as unknown as ToolCall, ctx };
}

function forgedAnswer(): Record<string, unknown> {
  return { path: join(approvalsDir(process.env), "answers", "x.json"), content: "{}" };
}

describe("the tool_call guard asks the Probe service first", () => {
  it("refuses with the routes' words, in pi's tool names", async () => {
    const fake = await serve(() => ({ result: denial("`Write` would touch the folder, so it was refused before it ran.") }));
    const { toolCall, ctx } = await start();
    const refused = await toolCall({ toolName: "write", toolCallId: "c1", input: { path: "notes.md", content: "x" } }, ctx);
    expect(refused).toEqual({ block: true, reason: "`write` would touch the folder, so it was refused before it ran." });
    const run = fake.requests.find((r) => r.method === "hooks.run")!.params as Record<string, unknown>;
    expect(run).toMatchObject({
      plugin_root: resolve(root, "plugins", "probe-research"),
      lane: "pre-tool",
      cwd: root,
      timeout_ms: 250,
      payload: { hook_event_name: "PreToolUse", tool_name: "Write", tool_input: { file_path: join(root, "notes.md") }, tool_use_id: "c1" },
    });
    expect((run.env as Record<string, string>).XDG_STATE_HOME).toBe(process.env.XDG_STATE_HOME);
  });

  it("refuses what only the routes refuse: the service adds refusals", async () => {
    await serve(() => ({ result: denial("Probe is READ-ONLY for this conversation, so `probe project create` was refused before it ran.") }));
    const { toolCall, ctx } = await start();
    // guard.ts lets this run in the agent profile; the Python guard does not in a `read` session.
    const refused = await toolCall({ toolName: "bash", input: { command: "probe project create x" } }, ctx);
    expect(refused?.reason).toContain("Probe is READ-ONLY");
  });

  it("refuses with the routes' words when only their stderr was cut (Codex review of #3294)", async () => {
    await serve(() => ({
      result: {
        ...denial("Probe is READ-ONLY for this conversation, so `probe project create` was refused before it ran."),
        stderr: "x".repeat(1000),
        truncated: true,
        stderr_truncated: true,
      },
    }));
    const { toolCall, ctx } = await start();
    const refused = await toolCall({ toolName: "bash", input: { command: "probe project create x" } }, ctx);
    expect(refused?.reason).toContain("Probe is READ-ONLY");
  });

  it("lets a call run when the routes allow it and pi's own guard does too", async () => {
    await serve(() => ({ result: answer() }));
    const { toolCall, ctx } = await start();
    expect(await toolCall({ toolName: "write", input: { path: "notes.md", content: "x" } }, ctx)).toBeUndefined();
  });

  it("never lets the routes' allow override pi's own refusal (the Python guard fails open)", async () => {
    // A route that broke inside its own try exits 0 with nothing to say: "allowed".
    await serve(() => ({ result: answer() }));
    const { toolCall, ctx } = await start();
    const refused = await toolCall({ toolName: "write", input: forgedAnswer() }, ctx);
    expect(refused?.reason).toContain("Only the researcher answers them");
  });

  it("asks the daemon plugin's folder in the daemon profile", async () => {
    profile = "daemon";
    const fake = await serve(() => ({ result: denial("so `probe run list` was refused before it ran") }));
    const { toolCall, ctx } = await start();
    const refused = await toolCall({ toolName: "bash", input: { command: "probe run list" } }, ctx);
    expect(refused?.reason).toBe("so `probe run list` was refused before it ran");
    const run = fake.requests.find((r) => r.method === "hooks.run")!.params as Record<string, unknown>;
    expect(run.plugin_root).toBe(resolve(root, "plugins", "probe-research-daemon"));
  });

  it("never sends a tool the guard does not read", async () => {
    const fake = await serve(() => ({ result: denial("no") }));
    const { toolCall, ctx } = await start();
    expect(await toolCall({ toolName: "read", input: { path: "x" } }, ctx)).toBeUndefined();
    expect(fake.requests).toEqual([]);
  });
});

describe("pi's own guard decides when the service does not", () => {
  it("a service that never answers: within the 300 ms wait", async () => {
    await serve(() => ({ silent: true }));
    const { toolCall, ctx } = await start();
    const started = Date.now();
    const refused = await toolCall({ toolName: "write", input: forgedAnswer() }, ctx);
    const elapsed = Date.now() - started;
    expect(refused?.reason).toContain("Only the researcher answers them");
    expect(elapsed).toBeGreaterThanOrEqual(250);
    expect(elapsed).toBeLessThan(1500);
  });

  it("no service at all (the socket is missing): at once", async () => {
    rmSync(SOCKET, { force: true });
    const { toolCall, ctx } = await start();
    const started = Date.now();
    const refused = await toolCall({ toolName: "write", input: forgedAnswer() }, ctx);
    expect(refused?.reason).toContain("Only the researcher answers them");
    expect(Date.now() - started).toBeLessThan(250);
    expect(await toolCall({ toolName: "bash", input: { command: "probe run list" } }, ctx)).toBeUndefined();
  });

  it("an answer that is no verdict (a route that failed, a timeout, an older service)", async () => {
    const answers: Behaviour[] = [
      { result: answer({ exit_code: 1, stderr: "Traceback" }) },
      { result: answer({ exit_code: null, timed_out: true }) },
      { error: { code: -32601, message: "unknown method: hooks.run" } },
    ];
    let next = 0;
    await serve(() => answers[next++]);
    const { toolCall, ctx } = await start();
    for (let i = 0; i < answers.length; i += 1) {
      const refused = await toolCall({ toolName: "write", input: forgedAnswer() }, ctx);
      expect(refused?.reason).toContain("Only the researcher answers them");
    }
    expect(next).toBe(answers.length);
  });

  it("an `off` session never asks the service", async () => {
    state = "off";
    const fake = await serve(() => ({ result: answer() }));
    const { toolCall, ctx } = await start();
    const refused = await toolCall({ toolName: "write", input: forgedAnswer() }, ctx);
    expect(refused?.reason).toContain("Only the researcher answers them");
    expect(fake.requests).toEqual([]);
  });
});
