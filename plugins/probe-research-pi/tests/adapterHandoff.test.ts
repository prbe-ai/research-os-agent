/**
 * D6's stand-down check (adapterHandoff.ts): does pi-mcp-adapter already own
 * the Probe MCP server, in which case this extension's direct bridge must
 * not also connect. Every fixture here is a REAL temp directory tree —
 * settings.json files, package.json files, and (for local-path cases) an
 * actual package root — never a mocked fs, because the whole point of this
 * module is resolving real paths (relative-to-base-dir, realpath, package.json
 * reads) the way pi's own package manager does. See adapterHandoff.ts's
 * module docstring for the identity rules under test.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { detectAdapterHandoff, MCP_SERVED_VIA_ADAPTER_MESSAGE } from "../src/adapterHandoff.js";

let tmp: string;
let agentDir: string;
let projectDir: string;
let ourPackageRoot: string;

function writeJson(path: string, data: unknown): void {
  mkdirSync(join(path, ".."), { recursive: true });
  writeFileSync(path, JSON.stringify(data));
}

function userSettingsPath(): string {
  return join(agentDir, "settings.json");
}

function projectSettingsPath(): string {
  return join(projectDir, ".pi", "settings.json");
}

function baseOpts() {
  // PI_CODING_AGENT_DIR points user scope at our fixture agentDir instead of
  // the real machine's ~/.pi/agent -- exactly like extension.test.ts's own
  // env isolation, and load-bearing here: without it, piAgentDir() falls
  // back to the real homedir() regardless of what this file writes to
  // agentDir/settings.json.
  return { env: { PI_CODING_AGENT_DIR: agentDir }, cwd: projectDir, packageRoot: ourPackageRoot };
}

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-pi-adapter-handoff-"));
  agentDir = join(tmp, "pi-agent-dir");
  projectDir = join(tmp, "project");
  ourPackageRoot = join(tmp, "our-package");
  mkdirSync(agentDir, { recursive: true });
  mkdirSync(projectDir, { recursive: true });
  mkdirSync(ourPackageRoot, { recursive: true });
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

describe("detectAdapterHandoff — the both-or-neither rule", () => {
  it("stands down when both pi-mcp-adapter (npm) and our own package (local path) are present", () => {
    writeJson(userSettingsPath(), { packages: ["npm:pi-mcp-adapter", ourPackageRoot] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
    expect(result.reason).toContain("pi-mcp-adapter");
  });

  it("keeps the bridge running when the adapter is present but our package is not listed (legacy symlink install)", () => {
    // No entry pointing at ourPackageRoot at all -- e.g. this extension was
    // symlinked into ~/.pi/agent/extensions/ rather than installed as a
    // packages entry, so the adapter cannot see our mcp.json manifest.
    writeJson(userSettingsPath(), { packages: ["npm:pi-mcp-adapter"] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(false);
    expect(result.reason.toLowerCase()).toContain("legacy");
    expect(result.reason).toContain("pi-mcp-adapter");
  });

  it("keeps the bridge running when our package is present but the adapter is not", () => {
    writeJson(userSettingsPath(), { packages: [ourPackageRoot] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(false);
    expect(result.reason).toContain("not present");
  });

  it("keeps the bridge running when neither package is present (no settings.json at all)", () => {
    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(false);
  });

  it("keeps the bridge running when packages is present but empty", () => {
    writeJson(userSettingsPath(), { packages: [] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(false);
  });
});

describe("detectAdapterHandoff — packages entry shapes", () => {
  it("accepts object {\"source\": ...} entries alongside bare strings", () => {
    writeJson(userSettingsPath(), {
      packages: [{ source: "npm:pi-mcp-adapter" }, { source: ourPackageRoot }],
    });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
  });

  it("matches a git-source adapter by its path basename (minus .git)", () => {
    writeJson(userSettingsPath(), {
      packages: ["git:github.com/example-org/pi-mcp-adapter", ourPackageRoot],
    });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
  });

  it("does NOT match a scoped npm package with the same tail name (@scope/pi-mcp-adapter)", () => {
    writeJson(userSettingsPath(), {
      packages: ["npm:@scope/pi-mcp-adapter", ourPackageRoot],
    });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(false);
  });

  it("matches our own package via npm:probe-research-pi with a pinned version", () => {
    writeJson(userSettingsPath(), {
      packages: ["npm:probe-research-pi@0.1.0", "npm:pi-mcp-adapter"],
    });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
  });

  it("matches a local adapter package by reading its package.json name field", () => {
    const adapterRoot = join(tmp, "adapter-package");
    mkdirSync(adapterRoot, { recursive: true });
    writeJson(join(adapterRoot, "package.json"), { name: "pi-mcp-adapter" });
    writeJson(userSettingsPath(), { packages: [adapterRoot, ourPackageRoot] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
  });

  it("resolves a relative local source against the AGENT dir for user scope, not cwd", () => {
    // ourPackageRoot is a sibling of agentDir (both live under tmp), so a
    // path relative to agentDir reaches it -- but the SAME relative string
    // would resolve somewhere else entirely if resolved against cwd
    // (projectDir), which is exactly the distinction this test pins.
    const relative = join("..", "our-package");
    writeJson(userSettingsPath(), { packages: ["npm:pi-mcp-adapter", relative] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
  });
});

describe("detectAdapterHandoff — the mirror install (git source)", () => {
  // pi has no marketplace: a real install is a git source naming the public
  // mirror repo, whose ROOT package.json carries the pi manifest and whose
  // `plugins/probe-research-pi/` is where this extension actually lives. Both
  // halves of that shape have to read as "ours", or D6 fails condition (b) on
  // every installed machine and a second copy of the Probe MCP tools gets
  // registered alongside the adapter's.
  it("stands down when our package arrives as the mirror git source", () => {
    writeJson(userSettingsPath(), {
      packages: ["npm:pi-mcp-adapter", "github:prbe-ai/research-os-agent"],
    });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
  });

  it("matches the mirror however the git source is spelled, including a pinned ref", () => {
    for (const source of [
      "github:prbe-ai/research-os-agent",
      "git:github.com/prbe-ai/research-os-agent",
      "https://github.com/prbe-ai/research-os-agent.git",
      "github:prbe-ai/research-os-agent#v1.2.3",
    ]) {
      writeJson(userSettingsPath(), { packages: ["npm:pi-mcp-adapter", source] });
      expect(detectAdapterHandoff(baseOpts()).standDown, source).toBe(true);
    }
  });

  it("does NOT match a different repo that happens to be a git source", () => {
    writeJson(userSettingsPath(), {
      packages: ["npm:pi-mcp-adapter", "github:someone-else/research-os-agent-fork"],
    });

    expect(detectAdapterHandoff(baseOpts()).standDown).toBe(false);
  });

  it("matches a local clone of the mirror, whose root is our package's PARENT", () => {
    // `pi install <path>` against a hand-cloned mirror: the settings entry is
    // the repo root, while packageRoot (this extension's own dir) is nested
    // at <repo>/plugins/probe-research-pi. Confirmed by the root package.json
    // name, so a bare ancestor directory can never match on prefix alone.
    const clone = join(tmp, "mirror-clone");
    const nested = join(clone, "plugins", "probe-research-pi");
    mkdirSync(nested, { recursive: true });
    writeJson(join(clone, "package.json"), { name: "probe-research-pi" });
    writeJson(userSettingsPath(), { packages: ["npm:pi-mcp-adapter", clone] });

    const result = detectAdapterHandoff({ ...baseOpts(), packageRoot: nested });

    expect(result.standDown).toBe(true);
  });

  it("does NOT match an unrelated ancestor directory that merely contains us", () => {
    const parent = join(tmp, "some-parent");
    const nested = join(parent, "plugins", "probe-research-pi");
    mkdirSync(nested, { recursive: true });
    writeJson(join(parent, "package.json"), { name: "totally-unrelated" });
    writeJson(userSettingsPath(), { packages: ["npm:pi-mcp-adapter", parent] });

    const result = detectAdapterHandoff({ ...baseOpts(), packageRoot: nested });

    expect(result.standDown).toBe(false);
  });
});

describe("detectAdapterHandoff — merging user + project scope", () => {
  it("finds the adapter in user scope and our package in project scope, and merges them", () => {
    writeJson(userSettingsPath(), { packages: ["npm:pi-mcp-adapter"] });
    writeJson(projectSettingsPath(), { packages: [ourPackageRoot] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
  });

  it("finds our package in user scope and the adapter in project scope, and merges them", () => {
    writeJson(userSettingsPath(), { packages: [ourPackageRoot] });
    writeJson(projectSettingsPath(), { packages: ["npm:pi-mcp-adapter"] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
  });
});

describe("detectAdapterHandoff — fail open on unreadable settings", () => {
  it("ignores a scope with malformed JSON but still counts the other scope's entries", () => {
    writeFileSync(userSettingsPath(), "{ not valid json");
    mkdirSync(join(projectDir, ".pi"), { recursive: true });
    writeJson(projectSettingsPath(), { packages: ["npm:pi-mcp-adapter", ourPackageRoot] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(true);
  });

  it("fails open (keeps the bridge running) when malformed JSON hides the half of the pair it carried", () => {
    // The adapter entry lives ONLY in the malformed file -- it can never be
    // seen, so the conjunction can never be satisfied. This must resolve to
    // "run the bridge," never a thrown error and never a false standDown.
    writeFileSync(userSettingsPath(), "{ not valid json");
    mkdirSync(join(projectDir, ".pi"), { recursive: true });
    writeJson(projectSettingsPath(), { packages: [ourPackageRoot] });

    const result = detectAdapterHandoff(baseOpts());

    expect(result.standDown).toBe(false);
  });

  it("ignores a non-object settings.json root", () => {
    writeFileSync(userSettingsPath(), JSON.stringify(["not", "an", "object"]));

    expect(() => detectAdapterHandoff(baseOpts())).not.toThrow();
    expect(detectAdapterHandoff(baseOpts()).standDown).toBe(false);
  });

  it("ignores a non-list packages value", () => {
    writeJson(userSettingsPath(), { packages: "not-a-list" });

    expect(() => detectAdapterHandoff(baseOpts())).not.toThrow();
    expect(detectAdapterHandoff(baseOpts()).standDown).toBe(false);
  });

  it("never throws even when packageRoot itself does not exist on disk", () => {
    rmSync(ourPackageRoot, { recursive: true, force: true });
    writeJson(userSettingsPath(), { packages: ["npm:pi-mcp-adapter", ourPackageRoot] });

    expect(() => detectAdapterHandoff(baseOpts())).not.toThrow();
    // Path still resolves and compares equal even though nothing is on disk
    // to realpath -- see realpathOrResolved's fallback.
    expect(detectAdapterHandoff(baseOpts()).standDown).toBe(true);
  });
});

describe("MCP_SERVED_VIA_ADAPTER_MESSAGE", () => {
  it("is the exact phrase D6 requires in the log and /probe-status", () => {
    expect(MCP_SERVED_VIA_ADAPTER_MESSAGE).toBe("Probe MCP served via pi-mcp-adapter");
  });
});
