import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { DaemonReason, DaemonStatus } from "../src/core/daemonNotice.js";
import { prefixToolName } from "../src/mcpSchema.js";
import {
  DAEMON_OWN_SKILLS,
  DAEMON_PROFILE_SKILLS,
  daemonSkillDir,
  isProbeMcpTool,
  leanSkills,
  PACKAGE_SKILLS,
  readDaemonSkill,
  readDaemonSkills,
  researcherDaemonNotice,
} from "../src/profile.js";

const PACKAGE_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

function skill(name: string, filePath = `/pkg/skills/${name}/SKILL.md`) {
  return { name, description: `${name} (full)`, filePath, baseDir: dirname(filePath), sourceInfo: { source: "package" } };
}

describe("the daemon's own skills", () => {
  it("ship in the package, and their descriptions are the lean plugin's", () => {
    const daemon = readDaemonSkill(PACKAGE_ROOT, "probe");
    expect(daemon).not.toBeNull();
    expect(daemon!.filePath).toBe(join(daemonSkillDir(PACKAGE_ROOT, "probe"), "SKILL.md"));
    expect(existsSync(daemon!.filePath)).toBe(true);
    expect(daemon!.description).toContain("the daemon records and reads");
    const all = readDaemonSkills(PACKAGE_ROOT);
    expect([...all.keys()]).toEqual([...DAEMON_OWN_SKILLS]);
    expect(all.get("visualize-progress")!.filePath).toBe(join(PACKAGE_ROOT, "daemon-skills", "visualize-progress", "SKILL.md"));
  });

  it("is null when the file cannot be read or declares no description", () => {
    expect(readDaemonSkill("/nowhere", "probe", () => {
      throw new Error("ENOENT");
    })).toBeNull();
    expect(readDaemonSkill("/pkg", "probe", () => "---\nname: probe\n---\n# Probe\n")).toBeNull();
    expect(readDaemonSkills("/nowhere", () => {
      throw new Error("ENOENT");
    }).size).toBe(0);
  });

  it("is never declared in package.json, so the agent profile cannot load it", () => {
    const manifest = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf-8")) as { pi: { skills: string[] } };
    expect(manifest.pi.skills.some((entry) => entry.includes("daemon-skills"))).toBe(false);
  });
});

describe("leanSkills", () => {
  const daemon = { description: "the daemon's", filePath: "/pkg/daemon-skills/probe/SKILL.md", baseDir: "/pkg/daemon-skills/probe" };
  const progress = {
    description: "the daemon's progress",
    filePath: "/pkg/daemon-skills/visualize-progress/SKILL.md",
    baseDir: "/pkg/daemon-skills/visualize-progress",
  };
  const own = new Map([["probe", daemon], ["visualize-progress", progress]]);
  const none = new Map();

  it("keeps only instrument-code, plan-research-review, probe and visualize-progress", () => {
    const every = ["probe", "track-work", "visualize-progress", "instrument-code", "audit-team-note", "edit-notes", "notes-audit", "plan-research-review"];
    const lean = leanSkills(every.map((name) => skill(name)), own);
    expect(lean.map((s) => s.name).sort()).toEqual([...DAEMON_PROFILE_SKILLS].sort());
  });

  it("keeps every skill this package did not bring", () => {
    // The researcher's own ~/.pi/agent/skills, a project's .pi/skills, another
    // package's: Claude Code's lean plugin never touches other plugins' skills.
    const lean = leanSkills([skill("deploy", "/home/r/.pi/agent/skills/deploy/SKILL.md"), skill("track-work")], none);
    expect(lean.map((s) => s.name)).toEqual(["deploy"]);
  });

  it("knows every skill package.json ships", () => {
    const manifest = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf-8")) as { pi: { skills: string[] } };
    expect(manifest.pi.skills.map((entry) => entry.split("/").pop()).sort()).toEqual([...PACKAGE_SKILLS].sort());
  });

  it("keeps the daemon's probe when it is the one pi loaded", () => {
    const loaded = skill("probe", daemon.filePath);
    expect(leanSkills([loaded], own)).toEqual([loaded]);
  });

  it("points a full probe skill that won pi's name collision at the daemon's file", () => {
    // A project entry that loads every skill of this package: pi keeps the
    // first `probe`, which is the full one, and it tells the agent it records.
    const full = skill("probe");
    const [lean] = leanSkills([full], own);
    expect(lean).toMatchObject({ name: "probe", ...daemon, sourceInfo: full.sourceInfo });
    expect(full.filePath).toBe("/pkg/skills/probe/SKILL.md"); // pi's own object is not edited
  });

  it("points a full visualize-progress at the daemon's too, and leaves the rest where pi loaded them", () => {
    // The package's skills/visualize-progress reads Probe through the MCP,
    // which the daemon profile does not have.
    const lean = leanSkills([skill("visualize-progress"), skill("instrument-code")], own);
    expect(lean[0]).toMatchObject({ name: "visualize-progress", ...progress });
    expect(lean[1].filePath).toBe("/pkg/skills/instrument-code/SKILL.md");
  });

  it("drops an own skill whose daemon file could not be read, never listing the agent profile's", () => {
    // The package's skills/visualize-progress reads Probe through the MCP.
    const onlyProbe = readDaemonSkills("/pkg", (path) => {
      if (path.includes("visualize-progress")) throw new Error("ENOENT");
      return "---\nname: probe\ndescription: the daemon's\n---\n";
    });
    expect([...onlyProbe.keys()]).toEqual(["probe"]);
    const lean = leanSkills([skill("visualize-progress"), skill("probe"), skill("instrument-code")], onlyProbe);
    expect(lean.map((s) => s.name)).toEqual(["probe", "instrument-code"]);
  });

  it("names the daemon's own skills in the profile it keeps", () => {
    for (const name of DAEMON_OWN_SKILLS) expect(DAEMON_PROFILE_SKILLS.has(name)).toBe(true);
  });
});

describe("isProbeMcpTool", () => {
  it("knows this extension's bridge tools and any client's probe-research tools", () => {
    expect(isProbeMcpTool(prefixToolName("search_knowledge"))).toBe(true);
    expect(isProbeMcpTool("probe-research-pi__probe_browse")).toBe(true);
    expect(isProbeMcpTool("mcp__probe_research__entity")).toBe(true);
  });

  it("leaves pi's own tools and every other extension's alone", () => {
    for (const name of ["bash", "read", "edit", "write", "grep", "find", "ls", "mcp__github__search", "probe"]) {
      expect(isProbeMcpTool(name)).toBe(false);
    }
  });
});

describe("researcherDaemonNotice", () => {
  const held = () => true;
  const keyless = () => false;

  it("gives a worker with a key one prompt to take its first lease", () => {
    expect(researcherDaemonNotice(null, DaemonStatus.Degraded, DaemonReason.NotStarted, true, held)).toEqual({
      notice: null,
      next: null,
    });
    const second = researcherDaemonNotice(null, DaemonStatus.Degraded, DaemonReason.NotStarted, false, held);
    expect(second.notice).toBe(
      "Probe: the daemon is not running, so it is not recording this session. `probe doctor` says why.",
    );
    expect(second.next).toBe(DaemonStatus.Degraded);
  });

  it("speaks at once when the daemon cannot start, and names the reason", () => {
    expect(researcherDaemonNotice(null, DaemonStatus.Degraded, DaemonReason.NotStarted, true, keyless).notice).toContain(
      "not running",
    );
    expect(researcherDaemonNotice(DaemonStatus.Live, DaemonStatus.Degraded, DaemonReason.Budget, false, held).notice).toContain(
      "out of budget",
    );
  });

  it("says it once per lapse, and again only after the daemon came back", () => {
    expect(researcherDaemonNotice(DaemonStatus.Degraded, DaemonStatus.Degraded, DaemonReason.Expired, false, held).notice).toBeNull();
    expect(researcherDaemonNotice(DaemonStatus.Degraded, DaemonStatus.Live, null, false, held)).toEqual({
      notice: null,
      next: DaemonStatus.Live,
    });
    expect(researcherDaemonNotice(DaemonStatus.Live, DaemonStatus.Degraded, DaemonReason.Expired, false, held).notice).toContain(
      "not responding",
    );
  });
});
