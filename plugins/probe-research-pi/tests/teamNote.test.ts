/**
 * Unit tests for the team-note logic in src/teamNote.ts and the path helpers
 * it depends on in src/paths.ts. Everything here is pure-function or
 * deps-injected: no real spawn, no real network, no touching the actual
 * `~/.pi` on the machine running the suite.
 */

import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { piAgentDir, teamNoteDocumentPath } from "../src/paths.js";
import { findProbeBinary, readTeamNote, renderTeamNoteForPrompt, spawnTeamNoteSync, type ProbeBinaryDeps, type TeamNoteSyncDeps } from "../src/teamNote.js";

describe("piAgentDir / teamNoteDocumentPath", () => {
  it("defaults to ~/.pi/agent, and the document sits beside it", () => {
    const env = {};
    expect(piAgentDir(env)).toMatch(/\.pi[\\/]agent$/);
    expect(teamNoteDocumentPath(env)).toBe(join(piAgentDir(env), "probe-team-note.md"));
  });

  it("PI_CODING_AGENT_DIR relocates the agent dir directly -- nothing appended", () => {
    const env = { PI_CODING_AGENT_DIR: "/custom/pi-dir" };
    expect(piAgentDir(env)).toBe("/custom/pi-dir");
    expect(teamNoteDocumentPath(env)).toBe(join("/custom/pi-dir", "probe-team-note.md"));
  });

  it("ignores a blank override the same way the Python side does", () => {
    const env = { PI_CODING_AGENT_DIR: "   " };
    expect(piAgentDir(env)).toMatch(/\.pi[\\/]agent$/);
  });
});

describe("readTeamNote", () => {
  it("returns the file's text when present and non-empty", () => {
    const note = readTeamNote(
      { PI_CODING_AGENT_DIR: "/agent-dir" },
      { readFileSync: (p) => (p === join("/agent-dir", "probe-team-note.md") ? "# Team note\ncontent\n" : "") },
    );
    expect(note).toBe("# Team note\ncontent\n");
  });

  it("fails open to null when the file does not exist", () => {
    const note = readTeamNote(
      {},
      {
        readFileSync: () => {
          throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
        },
      },
    );
    expect(note).toBeNull();
  });

  it("fails open to null when the file is unreadable for any other reason", () => {
    const note = readTeamNote(
      {},
      {
        readFileSync: () => {
          throw new Error("EACCES: permission denied");
        },
      },
    );
    expect(note).toBeNull();
  });

  it("treats a whitespace-only file as no note", () => {
    const note = readTeamNote({}, { readFileSync: () => "   \n\n  " });
    expect(note).toBeNull();
  });
});

describe("renderTeamNoteForPrompt", () => {
  it("names the real file path and carries the note body", () => {
    const rendered = renderTeamNoteForPrompt("Some team note text.", "/home/x/.pi/agent/probe-team-note.md");
    expect(rendered).toContain("Some team note text.");
    expect(rendered).toContain("/home/x/.pi/agent/probe-team-note.md");
    expect(rendered).toContain("## Probe team note");
  });
});

function fakeProbeDeps(existing: Set<string>, executable: Set<string>, env: Record<string, string | undefined>): ProbeBinaryDeps {
  return {
    existsSync: (p) => existing.has(p),
    isExecutable: (p) => executable.has(p),
    env,
  };
}

describe("findProbeBinary", () => {
  it("finds probe on PATH first", () => {
    const onPath = join("/usr/local/bin", "probe");
    const deps = fakeProbeDeps(new Set([onPath]), new Set([onPath]), { PATH: "/usr/local/bin", HOME: "/home/x" });
    expect(findProbeBinary(deps)).toBe(onPath);
  });

  it("falls back to ~/.local/bin/probe when PATH has nothing", () => {
    const fallback = join("/home/x", ".local", "bin", "probe");
    const deps = fakeProbeDeps(new Set([fallback]), new Set([fallback]), { PATH: "/usr/local/bin", HOME: "/home/x" });
    expect(findProbeBinary(deps)).toBe(fallback);
  });

  it("falls back to the uv tool install location when the first fallback is absent", () => {
    const fallback2 = join("/home/x", ".local", "share", "uv", "tools", "probe-research", "bin", "probe");
    const deps = fakeProbeDeps(new Set([fallback2]), new Set([fallback2]), { PATH: "", HOME: "/home/x" });
    expect(findProbeBinary(deps)).toBe(fallback2);
  });

  it("returns null when nothing resolves anywhere", () => {
    const deps = fakeProbeDeps(new Set(), new Set(), { PATH: "/usr/local/bin", HOME: "/home/x" });
    expect(findProbeBinary(deps)).toBeNull();
  });
});

function fakeSyncDeps(overrides: Partial<TeamNoteSyncDeps> = {}): TeamNoteSyncDeps {
  return {
    spawn: vi.fn(() => ({ pid: 999, unref: vi.fn() })),
    existsSync: () => false,
    isExecutable: () => false,
    env: { PATH: "", HOME: "/home/x" },
    log: vi.fn(),
    ...overrides,
  };
}

describe("spawnTeamNoteSync", () => {
  it("spawns `probe notes sync` detached, with PROBE_AGENT=pi, and unrefs it", () => {
    const probeBin = join("/home/x", ".local", "bin", "probe");
    const spawnMock = vi.fn(
      (_command: string, _args: string[], _options: { detached: boolean; stdio: string; env: Record<string, string | undefined> }) => ({
        pid: 4242,
        unref: vi.fn(),
      }),
    );
    const deps = fakeSyncDeps({
      spawn: spawnMock,
      existsSync: (p) => p === probeBin,
      isExecutable: (p) => p === probeBin,
    });

    spawnTeamNoteSync(deps);

    expect(spawnMock).toHaveBeenCalledTimes(1);
    const [command, args, options] = spawnMock.mock.calls[0];
    expect(command).toBe(probeBin);
    expect(args).toEqual(["notes", "sync"]);
    expect(options.detached).toBe(true);
    expect(options.stdio).toBe("ignore");
    expect(options.env.PROBE_AGENT).toBe("pi");
    // The child is unref()'d -- this call must not keep the process alive.
    const child = spawnMock.mock.results[0]!.value as { unref: ReturnType<typeof vi.fn> };
    expect(child.unref).toHaveBeenCalledTimes(1);
  });

  it("fails open and silent when no probe CLI can be found: no spawn, no throw", () => {
    const spawnMock = vi.fn();
    const logMock = vi.fn();
    const deps = fakeSyncDeps({ spawn: spawnMock, log: logMock });

    expect(() => spawnTeamNoteSync(deps)).not.toThrow();
    expect(spawnMock).not.toHaveBeenCalled();
    expect(logMock).toHaveBeenCalledWith(expect.stringContaining("skipped"));
  });

  it("fails open and silent when spawn itself throws (e.g. ENOENT racing a deleted binary)", () => {
    const probeBin = join("/home/x", ".local", "bin", "probe");
    const spawnMock = vi.fn(() => {
      throw new Error("spawn EACCES");
    });
    const logMock = vi.fn();
    const deps = fakeSyncDeps({
      spawn: spawnMock,
      existsSync: (p) => p === probeBin,
      isExecutable: (p) => p === probeBin,
      log: logMock,
    });

    expect(() => spawnTeamNoteSync(deps)).not.toThrow();
    expect(logMock).toHaveBeenCalledWith(expect.stringContaining("spawn failed"));
  });
});
