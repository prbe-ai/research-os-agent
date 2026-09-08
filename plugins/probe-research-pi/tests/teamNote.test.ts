/**
 * Unit tests for the team-note logic in src/teamNote.ts and the path helpers
 * it depends on in src/paths.ts. Everything here is pure-function or
 * deps-injected: no real spawn, no real network, no touching the actual
 * `~/.pi` on the machine running the suite.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

import { piAgentDir, teamNoteDocumentPath } from "../src/paths.js";
import { findProbeBinary, readTeamNote, renderTeamNoteForPrompt, spawnTeamNoteSync, syncTeamNoteThenRead, type ProbeBinaryDeps, type TeamNoteSyncDeps } from "../src/teamNote.js";

describe("piAgentDir / teamNoteDocumentPath", () => {
  it("PI_CODING_AGENT_DIR relocates the agent dir directly -- nothing appended", () => {
    const env = { PI_CODING_AGENT_DIR: "/custom/pi-dir" };
    expect(piAgentDir(env)).toBe("/custom/pi-dir");
  });

  it("ignores a blank override the same way the Python side does", () => {
    const env = { PI_CODING_AGENT_DIR: "   " };
    expect(piAgentDir(env)).toMatch(/\.pi[\\/]agent$/);
  });

  it("puts the document where the CLI puts it, for every case in the shared fixture", () => {
    // THE CONTRACT WITH THE PYTHON SIDE. This extension cannot import the CLI,
    // and it briefs a pi session from whatever path this returns -- so a drift
    // here means an agent reads a file nothing syncs, silently. The same
    // fixture is asserted against `team_note_file.paths().document` by
    // `agent/tests/test_agent_rules.py`, so neither side can move alone.
    const fixture = JSON.parse(
      readFileSync(
        join(
          fileURLToPath(new URL("../../../tests/fixtures/", import.meta.url)),
          "team-note-document-path.json",
        ),
        "utf-8",
      ),
    ) as { cases: { why: string; env: Record<string, string>; path: string }[] };

    expect(fixture.cases.length).toBeGreaterThan(0);
    for (const testCase of fixture.cases) {
      const expected = testCase.path.startsWith("~")
        ? join(homedir(), testCase.path.slice(2))
        : testCase.path;
      expect(teamNoteDocumentPath(testCase.env), testCase.why).toBe(expected);
    }
  });

  it("never resolves under a harness home, whatever the harness says", () => {
    // The ping-pong, as an assertion: three harness documents behind one merge
    // base is what made every session start push a whole document over the
    // last one (2026-09-07, v765-v770).
    const env = {
      XDG_STATE_HOME: "/tmp/state",
      PI_CODING_AGENT_DIR: "/custom/pi-dir",
      CODEX_HOME: "/custom/codex",
      CLAUDE_CONFIG_DIR: "/custom/claude",
    };
    expect(teamNoteDocumentPath(env)).toBe(
      join("/tmp/state", "probe", "team-note", "probe-team-note.md"),
    );
  });
});

describe("readTeamNote", () => {
  it("returns the file's text when present and non-empty", () => {
    const note = readTeamNote(
      { XDG_STATE_HOME: "/state-dir" },
      {
        readFileSync: (p) =>
          p === join("/state-dir", "probe", "team-note", "probe-team-note.md")
            ? "# Team note\ncontent\n"
            : "",
      },
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
    const rendered = renderTeamNoteForPrompt("Some team note text.", "/home/x/.local/state/probe/team-note/probe-team-note.md");
    expect(rendered).toContain("Some team note text.");
    expect(rendered).toContain("/home/x/.local/state/probe/team-note/probe-team-note.md");
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

  it("ignores relative PATH entries that could execute a repository-owned probe", () => {
    const fallback = join("/home/x", ".local", "bin", "probe");
    const deps = fakeProbeDeps(
      new Set(["probe", join("repo-bin", "probe"), fallback]),
      new Set(["probe", join("repo-bin", "probe"), fallback]),
      { PATH: [".", "repo-bin"].join(":"), HOME: "/home/x" },
    );

    expect(findProbeBinary(deps)).toBe(fallback);
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

describe("syncTeamNoteThenRead", () => {
  // WHY THESE EXIST: session_start awaits this before reading the note, so its
  // outcomes decide whether a pi session briefs from a fresh file, a stale one,
  // or hangs. A hang here is the worst of the three and the least visible.
  const deps = (over: Partial<TeamNoteSyncDeps> = {}): TeamNoteSyncDeps => ({
    spawn: () => ({ pid: 1, unref: () => {} }),
    existsSync: () => true,
    isExecutable: () => true,
    env: { PATH: "/usr/bin" },
    log: () => {},
    ...over,
  });

  it("resolves 'synced' when the child exits", async () => {
    const outcome = await syncTeamNoteThenRead(
      deps({
        spawn: () => ({
          pid: 1,
          unref: () => {},
          once: (event: string, cb: (v?: unknown) => void) => {
            if (event === "exit") queueMicrotask(() => cb(0));
          },
        }),
      }),
      50,
    );
    expect(outcome).toBe("synced");
  });

  it("resolves 'timeout' and leaves the child running when the sync is slow", async () => {
    let unrefs = 0;
    const outcome = await syncTeamNoteThenRead(
      deps({ spawn: () => ({ pid: 1, unref: () => { unrefs += 1; } }) }),
      10,
    );
    expect(outcome).toBe("timeout");
    // Unref'd, not killed: it still finishes, and its result reaches the NEXT
    // session's read. Killing it would make a slow network mean no sync at all.
    expect(unrefs).toBe(1);
  });

  it("resolves 'skipped' when the child errors", async () => {
    const outcome = await syncTeamNoteThenRead(
      deps({
        spawn: () => ({
          pid: 1,
          unref: () => {},
          once: (event: string, cb: (v?: unknown) => void) => {
            if (event === "error") queueMicrotask(() => cb(new Error("boom")));
          },
        }),
      }),
      50,
    );
    expect(outcome).toBe("skipped");
  });

  it("resolves 'skipped' when spawning throws, and never rejects", async () => {
    const outcome = await syncTeamNoteThenRead(
      deps({
        spawn: () => {
          throw new Error("ENOENT");
        },
      }),
      50,
    );
    expect(outcome).toBe("skipped");
  });

  it("resolves 'skipped' when no probe binary can be found", async () => {
    const outcome = await syncTeamNoteThenRead(
      deps({ existsSync: () => false, isExecutable: () => false, env: {} }),
      50,
    );
    expect(outcome).toBe("skipped");
  });
});
