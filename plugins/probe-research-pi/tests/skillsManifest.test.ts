/**
 * Proves the package.json "pi.skills" manifest actually reaches pi's own
 * resource loader, using pi 0.84.3's REAL, PUBLIC resource-loading code
 * (`DefaultResourceLoader` / `SettingsManager`, exported from the package's
 * own "." entry point) rather than re-implementing pi's discovery rules.
 *
 * This exists because declaring "pi.skills" in package.json is necessary but
 * NOT sufficient: it only takes effect once this package is registered as a
 * pi PACKAGE SOURCE (`pi install <path>`, which records the source in
 * settings.json's "packages" array and routes through
 * `collectPackageResources`). Simply symlinking this directory into
 * `~/.pi/agent/extensions/` -- the install method this plugin's README
 * documents for the extension itself -- goes through a DIFFERENT code path
 * (`collectAutoExtensionEntries`, auto-discovery of extension entry files)
 * that never reads package.json's "pi" manifest at all. Verified live via
 * `pi install <this-dir>` against a scratch HOME plus this same
 * DefaultResourceLoader call, both before this manifest existed (zero
 * matching skills, only the noise from the real machine's ~/.agents/skills)
 * and after (exactly the three skills below, from this package's own
 * skills/ directory). See the pi-harness-capture project notes for the full
 * trace.
 *
 * Also pins a real finding from that same run: track-work's description is
 * 1142 characters, over the Agent Skills spec's 1024-character cap pi
 * enforces (`core/skills.js`'s MAX_DESCRIPTION_LENGTH). pi does not refuse
 * the skill for it -- validateDescription() only appends a warning
 * diagnostic, the skill still loads with its full untruncated description --
 * so this is pinned as an EXPECTED diagnostic, not asserted away. If
 * track-work's canonical description (skills/track-work/SKILL.md, synced
 * from there -- never edit the copy here) is ever shortened under 1024
 * characters, this assertion needs to be relaxed in the same change, and its
 * disappearance is the signal that the finding has been resolved.
 *
 * HOME is overridden for the duration of this file's tests so the resolved
 * skill set is not polluted by whatever the machine running the suite has
 * under its real `~/.agents/skills/` (pi's cross-agent skills convention,
 * unrelated to this package, reads real HOME regardless of the scratch
 * agentDir passed to SettingsManager).
 */

import { existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

// @earendil-works/pi-coding-agent is a devDependency of THIS package (see
// package.json) -- the same real pi build the README's install
// instructions and the rest of this plugin are verified against.
import {
  DefaultResourceLoader,
  formatSkillsForPrompt,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";

const PACKAGE_ROOT = join(__dirname, "..");

const EXPECTED_SKILLS = [
  "track-work",
  "show-research-status",
  "instrument-training-runs",
].sort();

let scratchHome: string;
let realHome: string | undefined;

beforeEach(() => {
  scratchHome = mkdtempSync(join(tmpdir(), "probe-pi-skills-manifest-"));
  realHome = process.env.HOME;
  process.env.HOME = scratchHome;
});

afterEach(() => {
  process.env.HOME = realHome;
  rmSync(scratchHome, { recursive: true, force: true });
});

/** Registers this package as a real pi package source, the same shape
 * `pi install <path>` writes to settings.json -- without shelling out to the
 * CLI, so this stays a fast, hermetic unit test. */
function installThisPackage(agentDir: string): SettingsManager {
  mkdirSync(agentDir, { recursive: true });
  const settingsManager = SettingsManager.create(scratchHome, agentDir);
  settingsManager.setPackages([PACKAGE_ROOT]);
  return settingsManager;
}

describe("pi.skills manifest, resolved by pi's own loader", () => {
  it("resolves exactly the three vendored skills once this package is installed", async () => {
    const agentDir = join(scratchHome, ".pi", "agent");
    const settingsManager = installThisPackage(agentDir);

    const loader = new DefaultResourceLoader({
      cwd: scratchHome,
      agentDir,
      settingsManager,
    });
    await loader.reload();

    const { skills } = loader.getSkills();
    const names = skills.map((s) => s.name).sort();
    expect(names).toEqual(EXPECTED_SKILLS);

    // Every resolved skill must come from THIS package's vendored copy, not
    // the canonical skills/ (which is not on any auto-discovered path) and
    // not the machine's real ~/.agents/skills (excluded by the HOME
    // override above -- this assertion is what would catch a leak).
    for (const skill of skills) {
      expect(skill.filePath.startsWith(join(PACKAGE_ROOT, "skills"))).toBe(true);
      expect(skill.disableModelInvocation).toBe(false);
    }
  });

  it("finds nothing before the package is installed (no auto-discovery fallback)", async () => {
    // Same scratch HOME, but settings.json's "packages" is never written --
    // proves the three skills above are reached ONLY through the manifest
    // install path this test suite exercises, not some ambient directory
    // pi already scans by convention.
    const agentDir = join(scratchHome, ".pi", "agent");
    mkdirSync(agentDir, { recursive: true });
    const settingsManager = SettingsManager.create(scratchHome, agentDir);

    const loader = new DefaultResourceLoader({
      cwd: scratchHome,
      agentDir,
      settingsManager,
    });
    await loader.reload();

    expect(loader.getSkills().skills).toEqual([]);
  });

  it("still loads track-work despite its description exceeding pi's 1024-char cap", async () => {
    const agentDir = join(scratchHome, ".pi", "agent");
    const settingsManager = installThisPackage(agentDir);

    const loader = new DefaultResourceLoader({
      cwd: scratchHome,
      agentDir,
      settingsManager,
    });
    await loader.reload();

    const { skills, diagnostics } = loader.getSkills();
    const trackWork = skills.find((s) => s.name === "track-work");
    expect(trackWork).toBeDefined();
    expect(trackWork!.description.length).toBeGreaterThan(1024);

    // Pinned finding, not silently accepted: pi's Agent Skills validation
    // warns rather than truncates or refuses. See the file header before
    // touching this assertion.
    const overLength = diagnostics.filter(
      (d: { message?: string }) =>
        typeof d.message === "string" && d.message.includes("exceeds 1024 characters"),
    );
    expect(overLength).toHaveLength(1);
    expect(overLength[0]).toMatchObject({ path: trackWork!.filePath });
  });

  it("renders a well-formed <available_skills> prompt block", async () => {
    const agentDir = join(scratchHome, ".pi", "agent");
    const settingsManager = installThisPackage(agentDir);

    const loader = new DefaultResourceLoader({
      cwd: scratchHome,
      agentDir,
      settingsManager,
    });
    await loader.reload();

    const block = formatSkillsForPrompt(loader.getSkills().skills);
    expect(block).toContain("<available_skills>");
    expect(block).toContain("Use the read tool to load a skill's file");
    for (const name of EXPECTED_SKILLS) {
      expect(block).toContain(`<name>${name}</name>`);
    }
  });
});

/**
 * Proves "pi.mcp" in package.json is not just present but actually resolves
 * to a real, well-formed manifest -- pi-mcp-adapter's package-mcp-loader.ts
 * reads this path package-relative and loads only its "mcpServers" entries
 * (fact 3 in the pi-package-install plan); a manifest that drifts from the
 * file on disk, or a dead relative path, fails SILENTLY at the adapter with
 * nothing surfaced here, so this is the only tripwire.
 */
describe("pi.mcp manifest, resolved the way pi-mcp-adapter resolves it", () => {
  const pkg = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"));

  it("declares pi.mcp as a string pointing at a file that exists on disk", () => {
    expect(typeof pkg.pi.mcp).toBe("string");
    const resolved = isAbsolute(pkg.pi.mcp)
      ? pkg.pi.mcp
      : join(PACKAGE_ROOT, pkg.pi.mcp);
    expect(existsSync(resolved)).toBe(true);
  });

  it("parses as JSON with an mcpServers object", () => {
    const manifestPath = join(PACKAGE_ROOT, pkg.pi.mcp);
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    expect(manifest.mcpServers).toBeTypeOf("object");
    expect(manifest.mcpServers).not.toBeNull();
    expect(Object.keys(manifest.mcpServers).length).toBeGreaterThan(0);
  });

  it("every server entry uses an https:// url with oauth (D5 -- no bearerToken)", () => {
    const manifestPath = join(PACKAGE_ROOT, pkg.pi.mcp);
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    for (const [name, server] of Object.entries(manifest.mcpServers) as [
      string,
      Record<string, unknown>,
    ][]) {
      expect(typeof server.url, `${name}.url`).toBe("string");
      expect((server.url as string).startsWith("https://"), `${name}.url`).toBe(true);
      expect(server.auth, `${name}.auth`).toBe("oauth");
    }
  });

  it("carries no bearerToken/bearerTokenEnv/headers (a failing !command stops the adapter's connection, so it can never be a fallback -- see plan Non-goals)", () => {
    const manifestPath = join(PACKAGE_ROOT, pkg.pi.mcp);
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    for (const [name, server] of Object.entries(manifest.mcpServers) as [
      string,
      Record<string, unknown>,
    ][]) {
      expect(Object.keys(server), name).not.toContain("bearerToken");
      expect(Object.keys(server), name).not.toContain("bearerTokenEnv");
      expect(Object.keys(server), name).not.toContain("headers");
    }
  });
});
