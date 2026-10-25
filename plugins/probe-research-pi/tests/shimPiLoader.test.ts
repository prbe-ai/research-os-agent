/**
 * The shim inside REAL pi (the `@earendil-works/pi-coding-agent`
 * devDependency, as `skillsManifest.test.ts` uses it): pi's own extension
 * loader (jiti) loads `index.ts`, pi's own runner emits the events, and pi's
 * own tool registry decides what the model is offered. Two claims the fakes
 * cannot prove:
 *
 *   1. Re-registering a tool NAME at a switch refreshes what the model sees
 *      and what runs (`registerTool` replaces the definition, then pi rebuilds
 *      its registry and the agent's declared tools), and a tool the new
 *      version drops leaves the model's list. A tiny fake implementation keeps
 *      this about the registry, not about Probe.
 *   2. Under pi's jiti, the shim's dynamic `import()` of a pinned `impl.ts`
 *      resolves -- this package's real code, its dependencies through the
 *      pin's own `node_modules`, pi's aliased ones (typebox) -- so a session
 *      starts on the pin.
 *
 * No `session_start` of the REAL implementation runs here (it would spawn the
 * real `probe` CLI: jiti's modules are not vitest's, so no mock reaches them).
 */
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createAgentSession, DefaultResourceLoader, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";

import { currentFile, liveRecordFile, PINNED_MARKER, pinPackageDir, pinsDir, SHIM_EVENTS } from "../src/shim.js";

const PACKAGE_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

let tmp: string;
let saved: Record<string, string | undefined>;

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-pi-loader-"));
  saved = { XDG_STATE_HOME: process.env.XDG_STATE_HOME, PI_CODING_AGENT_DIR: process.env.PI_CODING_AGENT_DIR };
  process.env.XDG_STATE_HOME = join(tmp, "state");
  process.env.PI_CODING_AGENT_DIR = join(tmp, "agent");
  mkdirSync(process.env.PI_CODING_AGENT_DIR, { recursive: true });
});

afterEach(() => {
  for (const [key, value] of Object.entries(saved)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  rmSync(tmp, { recursive: true, force: true });
});

/** A fake implementation's source: one tool, `probe_shim_probe`, unless `tool` is null. */
function fakeImplSource(tool: { description: string; answer: string; parameter: string } | null): string {
  const tools = tool
    ? `[{
        name: "probe_shim_probe",
        label: "probe_shim_probe",
        description: ${JSON.stringify(tool.description)},
        parameters: { type: "object", properties: { ${tool.parameter}: { type: "string" } } },
        execute: async () => ({ content: [{ type: "text", text: ${JSON.stringify(tool.answer)} }], details: {} }),
      }]`
    : "[]";
  return `
export const REQUIRES_SHIM_API = 1;
export const STATE_SCHEMA = 1;
export const MIGRATES_FROM: number[] = [];
export const RESOURCES_FINGERPRINT = "fixed";
const TOOLS = ${tools};
export function prepare(env: any) {
  if (env.shared.schema === 0) env.shared.schema = 1;
  return {
    shared: env.shared,
    declarations: { events: {}, commands: {}, tools: () => TOOLS },
    activate() {},
    deactivate() {},
    status: () => ({ recorded: false }),
  };
}
`;
}

/** The shim as pi loads it (`index.ts`, `src/shim.ts` and what it imports), beside a fake implementation. */
function fakeClone(): string {
  const root = join(tmp, "clone", "plugins", "probe-research-pi");
  mkdirSync(join(root, "src", "core"), { recursive: true });
  for (const file of ["index.ts", "package.json", "src/shim.ts", "src/core/paths.ts"]) cpSync(join(PACKAGE_ROOT, file), join(root, file));
  writeFileSync(join(root, "src", "impl.ts"), fakeImplSource({ description: "v1 words", answer: "v1 answer", parameter: "query" }));
  return root;
}

/** `current`, made from the clone at `from` (the fake clone's, unless a test boots this package itself). */
function setCurrent(pin: string, from = join(tmp, "clone")): void {
  const path = currentFile(process.env);
  mkdirSync(dirname(path), { recursive: true });
  // The version the clone holds now: the CLI pins the clone as `pi update` left it.
  const version = (JSON.parse(readFileSync(join(from, "plugins", "probe-research-pi", "package.json"), "utf-8")) as { version: string }).version;
  writeFileSync(`${path}.tmp`, JSON.stringify({ pin, version, from, head: null }));
  renameSync(`${path}.tmp`, path);
}

function fakePin(pin: string, source: string): void {
  const packageDir = pinPackageDir(pin, process.env);
  mkdirSync(join(packageDir, "src"), { recursive: true });
  writeFileSync(join(packageDir, "package.json"), JSON.stringify({ name: "probe-research-pi", version: "9.9.9" }));
  writeFileSync(join(packageDir, "src", "impl.ts"), source);
  writeFileSync(join(pinsDir(process.env), pin, PINNED_MARKER), "");
}

async function piSession(extensionPath: string) {
  const cwd = join(tmp, "work");
  mkdirSync(cwd, { recursive: true });
  const settingsManager = SettingsManager.inMemory();
  const resourceLoader = new DefaultResourceLoader({
    cwd,
    agentDir: process.env.PI_CODING_AGENT_DIR!,
    settingsManager,
    additionalExtensionPaths: [extensionPath],
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
  });
  await resourceLoader.reload();
  const { session, extensionsResult } = await createAgentSession({
    cwd,
    agentDir: process.env.PI_CODING_AGENT_DIR!,
    resourceLoader,
    settingsManager,
    sessionManager: SessionManager.inMemory(cwd),
  });
  expect(extensionsResult.errors).toEqual([]);
  return session;
}

/** What the model is offered for the tool: its declared description and schema, or undefined. */
function declared(session: Awaited<ReturnType<typeof piSession>>, name: string) {
  return (session.agent.state.tools as Array<{ name: string; description: string; parameters: { properties: Record<string, unknown> } }>).find(
    (tool) => tool.name === name,
  );
}

describe("in real pi", () => {
  it("a switch that re-registers a tool name changes what the model is offered and what runs; a dropped tool leaves the model's list", async () => {
    const session = await piSession(join(fakeClone(), "index.ts"));
    expect(declared(session, "probe_shim_probe")?.description).toBe("v1 words");

    fakePin("p2", fakeImplSource({ description: "v2 words", answer: "v2 answer", parameter: "topic" }));
    setCurrent("p2");
    await session.extensionRunner.emit({ type: "session_start", reason: "startup" } as never);

    const tool = declared(session, "probe_shim_probe");
    expect(tool?.description).toBe("v2 words");
    expect(Object.keys(tool!.parameters.properties)).toEqual(["topic"]);
    expect(session.getToolDefinition("probe_shim_probe")?.description).toBe("v2 words");
    const result = await (session.agent.state.tools as Array<{ name: string; execute: (...a: unknown[]) => Promise<any> }>)
      .find((t) => t.name === "probe_shim_probe")!
      .execute("call-1", { topic: "x" });
    expect(result.content[0].text).toBe("v2 answer");

    fakePin("p3", fakeImplSource(null));
    setCurrent("p3");
    await session.extensionRunner.emit({ type: "session_start", reason: "startup" } as never);
    expect(declared(session, "probe_shim_probe")).toBeUndefined();
    expect(session.getActiveToolNames()).not.toContain("probe_shim_probe");
    session.dispose();
  });

  it("evaluates the shim afresh at /reload (module state does not survive it), and the reloaded shim offers the tools again", async () => {
    process.env.PROBE_PI_TAP_PLUGIN_DIR = join(tmp, "tap-state");
    try {
      const session = await piSession(join(fakeClone(), "index.ts"));
      const starts = () => (readFileSync(join(tmp, "tap-state", "logs", "extension.log"), "utf-8").match(/shim: running /g) ?? []).length;
      expect(starts()).toBe(1);
      await session.reload();
      expect(starts()).toBe(2);
      expect(declared(session, "probe_shim_probe")?.description).toBe("v1 words");
      session.dispose();
    } finally {
      delete process.env.PROBE_PI_TAP_PLUGIN_DIR;
    }
  });

  it("starts this package's real implementation from a pin through pi's own loader", async () => {
    const PIN = "0.7.0-jiti";
    const pinRoot = join(pinsDir(process.env), PIN);
    const packageDir = pinPackageDir(PIN, process.env);
    for (const entry of ["index.ts", "package.json", "mcp.json", "src", "skills", "daemon-skills"]) {
      cpSync(join(PACKAGE_ROOT, entry), join(packageDir, entry), { recursive: true });
    }
    symlinkSync(join(PACKAGE_ROOT, "node_modules"), join(pinRoot, "node_modules"));
    writeFileSync(join(pinRoot, PINNED_MARKER), "");
    setCurrent(PIN, dirname(dirname(PACKAGE_ROOT)));

    const session = await piSession(join(PACKAGE_ROOT, "index.ts"));
    const record = JSON.parse(readFileSync(liveRecordFile(process.pid, process.env), "utf-8"));
    expect(record).toMatchObject({ pin: PIN, bad_pins: [], shim_api: 1, state_schema: 1 });
    for (const event of SHIM_EVENTS) expect(session.extensionRunner.hasHandlers(event)).toBe(true);
    expect(session.extensionRunner.getCommand("probe-status")).toBeDefined();
    expect(session.extensionRunner.getCommand("probe-mcp-login")).toBeDefined();
    session.dispose();
    rmSync(liveRecordFile(process.pid, process.env), { force: true });
    expect(existsSync(pinRoot)).toBe(true);
  });
});
