/**
 * pi's guard asks the Probe service first (plan D38): the `hooks.run` client
 * (`core/service.ts`), the tool-call mapping and the reading of the answer
 * (`serviceGuard.ts`). The service is a stand-in on a real Unix socket
 * (`fakeService.ts`); the real one is `agent/tests/test_service_hooks_run.py`'s.
 */
import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  boundedHookEnv,
  HookLane,
  lookupServiceSocket,
  MAX_HOOK_ENV,
  MAX_HOOK_ENV_VALUE,
  preToolVerdict,
  privateFolder,
  runHook,
  runHookLocally,
  SERVICE_HOOK_RUN_MS,
  SERVICE_HOOK_WAIT_MS,
  ToolVerdict,
  VETO_WITHOUT_REASON,
  type HookRun,
  type HookRunResult,
} from "../src/core/service.js";
import { claudeToolCall, CLAUDE_PROBE_MCP_PREFIX, guardViaService, piReason, pluginRoot, PluginFolder, type ServiceGuardCall } from "../src/serviceGuard.js";
import { answer, denial, fakeService, socketFolder, type FakeService } from "./fakeService.js";

const running: FakeService[] = [];
afterEach(async () => {
  while (running.length) await running.pop()!.close();
});

async function serve(behaviour: Parameters<typeof fakeService>[1]): Promise<FakeService> {
  const service = await fakeService(join(socketFolder(), "service.sock"), behaviour);
  running.push(service);
  return service;
}

const RUN: HookRun = {
  pluginRoot: "/opt/probe/plugins/probe-research",
  lane: HookLane.PreTool,
  payload: { session_id: "s", hook_event_name: "PreToolUse", tool_name: "Bash", tool_input: { command: "ls" } },
  env: { HOME: "/home/x" },
  cwd: "/work",
  timeoutMs: SERVICE_HOOK_RUN_MS,
};

function result(overrides: Partial<HookRunResult> = {}): HookRunResult {
  return {
    exitCode: 0,
    stdout: "",
    stderr: "",
    timedOut: false,
    truncated: false,
    stdoutTruncated: false,
    stderrTruncated: false,
    via: "host",
    ...overrides,
  };
}

describe("runHook", () => {
  it("sends hello then hooks.run, as pi, and returns the service's answer", async () => {
    const service = await serve(() => ({ result: denial("not this one") }));
    const got = await runHook(service.socket, RUN, { clientVersion: "9.9.9" });
    expect(got).toMatchObject({ exitCode: 0, timedOut: false, via: "host" });
    expect(preToolVerdict(got as HookRunResult | null)).toEqual({ verdict: ToolVerdict.Block, reason: "not this one" });
    const [hello, run] = service.requests;
    expect(hello).toMatchObject({ method: "service.hello", params: { client: "pi", client_version: "9.9.9", api: 1 } });
    expect(run).toMatchObject({
      method: "hooks.run",
      params: {
        plugin_root: RUN.pluginRoot,
        lane: "pre-tool",
        payload: RUN.payload,
        env: RUN.env,
        cwd: "/work",
        timeout_ms: SERVICE_HOOK_RUN_MS,
      },
    });
  });

  it("reads which stream was cut from the service's answer", async () => {
    const service = await serve(() => ({
      result: { ...denial("not this one"), stderr: "x".repeat(100), truncated: true, stdout_truncated: false, stderr_truncated: true },
    }));
    const got = await runHook(service.socket, RUN, { clientVersion: "1" });
    expect(got).toMatchObject({ truncated: true, stdoutTruncated: false, stderrTruncated: true });
    expect(preToolVerdict(got as HookRunResult | null)).toEqual({ verdict: ToolVerdict.Block, reason: "not this one" });
  });

  it("returns an allow when the routes say nothing", async () => {
    const service = await serve(() => ({ result: answer() }));
    expect(preToolVerdict((await runHook(service.socket, RUN, { clientVersion: "1" })) as HookRunResult | null)).toEqual({ verdict: ToolVerdict.Allow });
  });

  it("gives up after its wait when the service never answers", async () => {
    expect(SERVICE_HOOK_WAIT_MS).toBe(300); // D28
    const service = await serve(() => ({ silent: true }));
    const started = Date.now();
    expect(await runHook(service.socket, RUN, { clientVersion: "1", waitMs: 150 })).toBeNull();
    expect(Date.now() - started).toBeLessThan(1000);
  });

  it("is null when no service listens, at once", async () => {
    const started = Date.now();
    expect(await runHook(join(socketFolder(), "missing.sock"), RUN, { clientVersion: "1" })).toBeNull();
    expect(Date.now() - started).toBeLessThan(SERVICE_HOOK_WAIT_MS);
  });

  it("says so when the service refuses this caller (it cannot rule out a sandbox)", async () => {
    const service = await serve(() => ({ result: { refused: "a caller in a sandbox" } }));
    expect(await runHook(service.socket, RUN, { clientVersion: "1" })).toEqual({ refused: "a caller in a sandbox", reason: null });
    const newer = await serve(() => ({ result: { refused: "a caller in a sandbox", reason: "sandboxed" } }));
    expect(await runHook(newer.socket, RUN, { clientVersion: "1" })).toEqual({ refused: "a caller in a sandbox", reason: "sandboxed" });
  });

  it("is null for an error answer, or an older service without the method", async () => {
    for (const told of [
      { error: { code: -32601, message: "unknown method: hooks.run" } },
      { error: { code: -32602, message: "invalid params" } },
      { result: { exit_code: "0" } },
    ]) {
      const service = await serve(() => told);
      expect(await runHook(service.socket, RUN, { clientVersion: "1" })).toBeNull();
    }
  });

  it("never talks to a socket in a folder others could plant one in", async () => {
    const service = await serve(() => ({ result: answer() }));
    const folder = join(service.socket, "..");
    chmodSync(folder, 0o777);
    try {
      expect(privateFolder(folder)).toBe(false);
      expect(await runHook(service.socket, RUN, { clientVersion: "1" })).toBeNull();
      expect(service.requests).toEqual([]);
    } finally {
      chmodSync(folder, 0o700);
    }
    expect(privateFolder(folder)).toBe(true);
  });
});

describe("preToolVerdict", () => {
  const deny = (reason: string) =>
    JSON.stringify({ hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: reason } });

  it("reads a PreToolUse answer as Claude Code does", () => {
    expect(preToolVerdict(result())).toEqual({ verdict: ToolVerdict.Allow });
    expect(preToolVerdict(result({ stdout: deny("no") }))).toEqual({ verdict: ToolVerdict.Block, reason: "no" });
    expect(preToolVerdict(result({ exitCode: 2, stderr: "vetoed\n" }))).toEqual({ verdict: ToolVerdict.Block, reason: "vetoed" });
    expect(preToolVerdict(result({ exitCode: 2 }))).toEqual({ verdict: ToolVerdict.Block, reason: VETO_WITHOUT_REASON });
    expect(preToolVerdict(result({ stdout: JSON.stringify({ decision: "block", reason: "old form" }) }))).toEqual({
      verdict: ToolVerdict.Block,
      reason: "old form",
    });
    expect(preToolVerdict(result({ stdout: JSON.stringify({ continue: false, stopReason: "stop" }) }))).toEqual({
      verdict: ToolVerdict.Block,
      reason: "stop",
    });
    const allow = JSON.stringify({ hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "allow" } });
    expect(preToolVerdict(result({ stdout: allow }))).toEqual({ verdict: ToolVerdict.Allow });
  });

  it("honours a refusal whose stderr was cut, and every exit-2 veto (Codex review of #3294)", () => {
    const flood = "x".repeat(50);
    expect(preToolVerdict(result({ stdout: deny("no"), stderr: flood, truncated: true, stderrTruncated: true }))).toEqual({
      verdict: ToolVerdict.Block,
      reason: "no",
    });
    expect(preToolVerdict(result({ exitCode: 2, stderr: "vetoed" + flood, truncated: true, stderrTruncated: true }))).toEqual({
      verdict: ToolVerdict.Block,
      reason: "vetoed" + flood,
    });
    // A veto stands even when both streams were cut: a refusal never becomes an allow.
    expect(preToolVerdict(result({ exitCode: 2, stderr: "", truncated: true, stdoutTruncated: true, stderrTruncated: true }))).toEqual({
      verdict: ToolVerdict.Block,
      reason: VETO_WITHOUT_REASON,
    });
  });

  it("gives no verdict for anything unclear, so pi's own guard decides", () => {
    const ask = JSON.stringify({ hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "ask" } });
    for (const unclear of [
      null,
      result({ timedOut: true, exitCode: null }),
      result({ exitCode: null }),
      result({ truncated: true, stdoutTruncated: true, stdout: deny("cut").slice(0, 40) }),
      result({ truncated: true, stdoutTruncated: true, stdout: deny("whole, but cut after it") }),
      result({ exitCode: 1, stderr: "Traceback" }),
      result({ stdout: "not json" }),
      result({ stdout: "[1]" }),
      result({ stdout: ask }),
    ]) {
      expect(preToolVerdict(unclear)).toBeNull();
    }
  });
});

describe("claudeToolCall: pi's tool call as Claude Code's PreToolUse", () => {
  const cwd = "/work/project";

  it("maps the shell tools to Bash", () => {
    expect(claudeToolCall("bash", { command: "probe run list", timeout: 5 }, cwd)).toEqual({ toolName: "Bash", toolInput: { command: "probe run list" } });
    expect(claudeToolCall("powershell", { command: "probe ask hi" }, cwd)).toEqual({ toolName: "Bash", toolInput: { command: "probe ask hi" } });
    expect(claudeToolCall("bash", {}, cwd)).toBeNull();
  });

  it("maps write and edit with the path pi will write, resolved as pi resolves it", () => {
    expect(claudeToolCall("write", { path: "notes.md", content: "x" }, cwd)).toEqual({
      toolName: "Write",
      toolInput: { file_path: "/work/project/notes.md", content: "x" },
    });
    expect(claudeToolCall("write", { path: "@~/a.json", content: "" }, cwd)!.toolInput.file_path).toBe(join(homedir(), "a.json"));
    expect(claudeToolCall("write", { path: "file:///tmp/b.json" }, cwd)!.toolInput.file_path).toBe("/tmp/b.json");
    expect(claudeToolCall("edit", { path: "a.py", edits: [{ oldText: "x", newText: "y" }] }, cwd)).toEqual({
      toolName: "Edit",
      toolInput: { file_path: "/work/project/a.py", old_string: "x", new_string: "y" },
    });
    expect(claudeToolCall("edit", { path: "a.py", edits: [{ oldText: "x", newText: "y" }, { oldText: "p", newText: "q" }] }, cwd)).toEqual({
      toolName: "MultiEdit",
      toolInput: { file_path: "/work/project/a.py", edits: [{ old_string: "x", new_string: "y" }, { old_string: "p", new_string: "q" }] },
    });
  });

  it("names a Probe MCP tool so the routes' matcher takes it", () => {
    expect(claudeToolCall("probe_mcp_search", { query: "q" }, cwd)).toEqual({ toolName: `${CLAUDE_PROBE_MCP_PREFIX}search`, toolInput: { query: "q" } });
    expect(claudeToolCall("probe-research-pi__probe_search", {}, cwd)!.toolName).toBe("probe-research-pi__probe_search");
    expect(claudeToolCall("Probe_Research_search", {}, cwd)!.toolName).toBe(`${CLAUDE_PROBE_MCP_PREFIX}Probe_Research_search`);
  });

  it("sends nothing for a tool the guard does not read", () => {
    for (const tool of ["read", "grep", "find", "ls", "web_search"]) expect(claudeToolCall(tool, { path: "x" }, cwd)).toBeNull();
  });

  it("gives a refusal back in pi's names", () => {
    const write = claudeToolCall("write", { path: "/x" }, cwd)!;
    expect(piReason("`Write` would touch /x, so it was refused", write, "write")).toBe("`write` would touch /x, so it was refused");
    const mcp = claudeToolCall("probe_mcp_search", {}, cwd)!;
    expect(piReason("so `mcp__probe-research__search` was refused", mcp, "probe_mcp_search")).toBe("so `probe_mcp_search` was refused");
    const bash = claudeToolCall("bash", { command: "probe run list" }, cwd)!;
    expect(piReason("`probe run list` was refused", bash, "bash")).toBe("`probe run list` was refused");
  });
});

describe("boundedHookEnv", () => {
  it("keeps what the service takes, Probe's and the folders' first", () => {
    const env: Record<string, string | undefined> = { HOME: "/h", XDG_STATE_HOME: "/s", LONG: "x".repeat(MAX_HOOK_ENV_VALUE + 1), NUL: "a\0b", GONE: undefined };
    for (let i = 0; i < MAX_HOOK_ENV + 50; i += 1) env[`A_${String(i).padStart(3, "0")}`] = "v";
    env.PROBE_X = "1";
    const bounded = boundedHookEnv(env);
    expect(Object.keys(bounded)).toHaveLength(MAX_HOOK_ENV);
    expect(bounded).toMatchObject({ HOME: "/h", XDG_STATE_HOME: "/s", PROBE_X: "1" });
    expect(bounded).not.toHaveProperty("LONG");
    expect(bounded).not.toHaveProperty("NUL");
    expect(bounded).not.toHaveProperty("GONE");
  });
});

describe("pluginRoot and guardViaService", () => {
  function plugins(): string {
    const root = socketFolder();
    for (const folder of Object.values(PluginFolder)) {
      mkdirSync(join(root, folder, "hooks"), { recursive: true });
      writeFileSync(join(root, folder, "hooks", "dispatch.py"), "");
    }
    mkdirSync(join(root, "probe-research-pi"));
    return join(root, "probe-research-pi");
  }

  function call(extensionDir: string, socket: string | null, overrides: Partial<ServiceGuardCall> = {}): ServiceGuardCall {
    return {
      toolName: "write",
      input: { path: "x.json", content: "{}" },
      toolCallId: "call-1",
      sessionId: "s-1",
      transcriptPath: "/sessions/s-1.jsonl",
      cwd: "/work",
      daemonProfile: false,
      extensionDir,
      env: { HOME: "/h", XDG_STATE_HOME: "/s" },
      socket,
      ...overrides,
    };
  }

  it("asks the profile's plugin folder beside this package", () => {
    const ext = plugins();
    expect(pluginRoot(ext, false)).toBe(resolve(ext, "..", "probe-research"));
    expect(pluginRoot(ext, true)).toBe(resolve(ext, "..", "probe-research-daemon"));
    rmSync(join(ext, "..", "probe-research", "hooks", "dispatch.py"));
    expect(pluginRoot(ext, false)).toBeNull();
  });

  it("sends Claude Code's PreToolUse payload and returns the routes' verdict in pi's words", async () => {
    const service = await serve(() => ({ result: denial("`Write` would touch /work/x.json") }));
    const ext = plugins();
    const got = await guardViaService(call(ext, service.socket), { clientVersion: "1" });
    expect(got).toEqual({ reason: "`write` would touch /work/x.json", via: "host" });
    expect(service.requests[1]).toMatchObject({
      params: {
        plugin_root: resolve(ext, "..", "probe-research"),
        lane: "pre-tool",
        cwd: "/work",
        payload: {
          session_id: "s-1",
          transcript_path: "/sessions/s-1.jsonl",
          cwd: "/work",
          hook_event_name: "PreToolUse",
          tool_name: "Write",
          tool_input: { file_path: "/work/x.json", content: "{}" },
          tool_use_id: "call-1",
        },
        env: { HOME: "/h", XDG_STATE_HOME: "/s" },
      },
    });
  });

  it("lets the call run when the routes allow it", async () => {
    const service = await serve(() => ({ result: answer() }));
    expect(await guardViaService(call(plugins(), service.socket), { clientVersion: "1" })).toEqual({ reason: null, via: "host" });
  });

  it("asks nothing it cannot ask well, and answers null so pi's guard decides", async () => {
    const service = await serve(() => ({ result: denial("no") }));
    const ext = plugins();
    expect(await guardViaService(call(ext, null), { clientVersion: "1" })).toBeNull(); // no socket yet
    expect(await guardViaService(call(ext, service.socket, { sessionId: undefined }), { clientVersion: "1" })).toBeNull();
    expect(await guardViaService(call(ext, service.socket, { toolName: "read" }), { clientVersion: "1" })).toBeNull();
    expect(await guardViaService(call(join(ext, "..", "nowhere", "pkg"), service.socket), { clientVersion: "1" })).toBeNull();
    // A path that cannot be resolved (a `file://` URL with a host): no answer, never a throw.
    const bad = call(ext, service.socket, { input: { path: "file://elsewhere/x.json" } });
    await expect(guardViaService(bad, { clientVersion: "1" })).resolves.toBeNull();
    await expect(guardViaService(call(ext, service.socket), { clientVersion: "1", run: () => Promise.reject(new Error("boom")) })).resolves.toBeNull();
    expect(service.requests).toEqual([]);
  });
});

describe("a refusal runs the routes here (Codex P2, round 4)", () => {
  /** A Probe plugin folder beside an extension folder, its dispatcher `script`. */
  function pluginWith(script: string): string {
    const root = socketFolder();
    const hooks = join(root, PluginFolder.Agent, "hooks");
    mkdirSync(hooks, { recursive: true });
    writeFileSync(join(hooks, "dispatch.py"), "");
    writeFileSync(join(hooks, "dispatch.sh"), "#!/bin/sh\n" + script + "\n");
    chmodSync(join(hooks, "dispatch.sh"), 0o755);
    mkdirSync(join(root, "probe-research-pi"));
    return join(root, "probe-research-pi");
  }

  function call(extensionDir: string, socket: string, overrides: Partial<ServiceGuardCall> = {}): ServiceGuardCall {
    return {
      toolName: "bash",
      input: { command: "probe run list" },
      toolCallId: "call-1",
      sessionId: "0199f0a2-6b1c-7d3e-8f40-5a6b7c8d9e0f",
      cwd: "/",
      daemonProfile: false,
      extensionDir,
      env: { HOME: homedir(), PATH: process.env.PATH ?? "/usr/bin:/bin" },
      socket,
      ...overrides,
    };
  }

  const DENY_LOCALLY =
    'cat > "$CLAUDE_PLUGIN_ROOT/../payload.json"; ' +
    'env > "$CLAUDE_PLUGIN_ROOT/../env.txt"; ' +
    "echo '{\"hookSpecificOutput\": {\"hookEventName\": \"PreToolUse\", \"permissionDecision\": \"deny\", " +
    "\"permissionDecisionReason\": \"`Bash` refused here\"}}'";

  it("runs the folder's pre-tool lane in place and takes its verdict", async () => {
    const service = await serve(() => ({ result: { refused: "a caller in a sandbox" } }));
    const ext = pluginWith(DENY_LOCALLY);
    const got = await guardViaService(call(ext, service.socket), { clientVersion: "1" });
    expect(got).toEqual({ reason: "`bash` refused here", via: "local" });
    const folder = join(ext, "..");
    const payload = JSON.parse(readFileSync(join(folder, "payload.json"), "utf8"));
    expect(payload).toMatchObject({ hook_event_name: "PreToolUse", tool_name: "Bash", tool_input: { command: "probe run list" } });
    const env = readFileSync(join(folder, "env.txt"), "utf8");
    expect(env).toContain(`CLAUDE_PLUGIN_ROOT=${resolve(folder, PluginFolder.Agent)}`);
    expect(env).toContain("PROBE_HOOK_HOST=off");
    expect(env).toContain(`PROBE_HOOK_PARENT_PID=${process.pid}`);
  });

  it("runs it here only for a sandbox refusal, never for a busy or stopping service", async () => {
    for (const refused of ["busy: too many hook events in flight", "the service could not take it (stopping)"]) {
      const service = await serve(() => ({ result: { refused } }));
      const ext = pluginWith(DENY_LOCALLY);
      expect(await guardViaService(call(ext, service.socket), { clientVersion: "1" })).toBeNull();
      expect(existsSync(join(ext, "..", "payload.json"))).toBe(false);
    }
    const sandboxed = await serve(() => ({ result: { refused: "anything", reason: "sandboxed" } }));
    const ext = pluginWith(DENY_LOCALLY);
    expect((await guardViaService(call(ext, sandboxed.socket), { clientVersion: "1" }))?.via).toBe("local");
  });

  it("stops the routes the dispatcher started in groups of their own, as the service does", async () => {
    if (process.platform !== "linux") return; // `setsid`
    const service = await serve(() => ({ result: { refused: "a caller in a sandbox" } }));
    // A dispatcher as dispatch.py is: each route in a session of its own,
    // stopped by the dispatcher when it is told to stop (TERM).
    const ext = pluginWith(
      'setsid sleep 30 & echo $! > "$CLAUDE_PLUGIN_ROOT/../route.pid"; ' +
        'trap \'kill "$(cat "$CLAUDE_PLUGIN_ROOT/../route.pid")"; exit 143\' TERM; wait',
    );
    expect(await guardViaService(call(ext, service.socket), { clientVersion: "1" })).toBeNull();
    const route = Number(readFileSync(join(ext, "..", "route.pid"), "utf8").trim());
    await new Promise((done) => setTimeout(done, 400));
    expect(() => process.kill(route, 0)).toThrow(); // TERM first: the dispatcher stopped its route
  });

  it("never runs it here for an answer that is not a refusal", async () => {
    const service = await serve(() => ({ error: { code: -32601, message: "unknown method: hooks.run" } }));
    const ext = pluginWith(DENY_LOCALLY);
    expect(await guardViaService(call(ext, service.socket), { clientVersion: "1" })).toBeNull();
    expect(existsSync(join(ext, "..", "payload.json"))).toBe(false);
  });

  it("gives no verdict when the lane here runs past the time it has, and stops it", async () => {
    const service = await serve(() => ({ result: { refused: "a caller in a sandbox" } }));
    const ext = pluginWith('echo $$ > "$CLAUDE_PLUGIN_ROOT/../pid"; exec sleep 30');
    const started = Date.now();
    expect(await guardViaService(call(ext, service.socket), { clientVersion: "1" })).toBeNull();
    expect(Date.now() - started).toBeLessThan(SERVICE_HOOK_RUN_MS + 1000);
    const pid = Number(readFileSync(join(ext, "..", "pid"), "utf8").trim());
    await new Promise((done) => setTimeout(done, 200));
    expect(() => process.kill(pid, 0)).toThrow(); // its group was stopped
  });

  it("answers as the service's run of the real routes does (parity, daemon profile)", async () => {
    // The shipped lean plugin beside this package, as `test_service_hooks_run.py`
    // asks it through the service: a `probe` read in a daemon-recorded session
    // is refused, in the routes' own words; `probe ask` is not.
    const ext = resolve(__dirname, "..");
    const home = socketFolder();
    const sessions = join(home, ".local", "state", "probe", "sessions");
    mkdirSync(sessions, { recursive: true });
    const sid = "0199f0a2-6b1c-7d3e-8f40-5a6b7c8d9e0f";
    writeFileSync(join(sessions, `${sid}.state`), "daemon\n");
    const service = await serve(() => ({ result: { refused: "a caller in a sandbox" } }));
    const env = { HOME: home, PATH: process.env.PATH ?? "/usr/bin:/bin" };
    const deps = { clientVersion: "1" };
    const generous = (run: HookRun, local: Parameters<typeof runHookLocally>[1]) =>
      runHookLocally({ ...run, timeoutMs: 10_000 }, local); // a loaded runner's cold python is not the subject
    const refused = await guardViaService(call(ext, service.socket, { daemonProfile: true, env }), { ...deps, runLocally: generous });
    expect(refused?.via).toBe("local");
    expect(refused?.reason).toContain("`probe run list` was refused before it ran");
    const asked = await guardViaService(
      call(ext, service.socket, { daemonProfile: true, env, input: { command: "probe ask hi" } }),
      { ...deps, runLocally: generous },
    );
    expect(asked).toEqual({ reason: null, via: "local" });
  });
});

describe("lookupServiceSocket", () => {
  const runtime = { python: "/usr/bin/python3", tapRoot: "/opt/tap" };

  it("asks the tap's copy of the client once, and reads its path", async () => {
    const calls: string[][] = [];
    const socket = await lookupServiceSocket(runtime, {
      execFile: (_command, args, options, callback) => {
        calls.push(args);
        expect(options.env.PYTHONPATH).toBe("/opt/tap");
        callback(null, '{"socket": "/home/x/.local/state/probe/service/abc/service.sock"}\n', "");
      },
      env: {},
      log: () => {},
    });
    expect(socket).toBe("/home/x/.local/state/probe/service/abc/service.sock");
    expect(calls).toEqual([["-m", "tap", "service", "socket"]]);
  });

  it("is null for an older tap, garbage, a relative path, or Windows", async () => {
    const answers: [Error | null, string][] = [
      [new Error("usage: probe-service-client: invalid choice: 'socket'"), ""],
      [null, "not json"],
      [null, '{"socket": "relative.sock"}'],
    ];
    for (const [error, stdout] of answers) {
      expect(await lookupServiceSocket(runtime, { execFile: (_c, _a, _o, callback) => callback(error, stdout, ""), env: {}, log: () => {} })).toBeNull();
    }
  });
});
