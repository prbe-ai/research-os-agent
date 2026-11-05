/**
 * Unit tests for the team-note logic in src/core/teamNote.ts (and the prompt
 * text in src/teamNotePrompt.ts) and the path helpers it depends on in
 * src/core/paths.ts. Everything here is pure-function or
 * deps-injected: no real spawn, no real network, no touching the actual
 * `~/.pi` on the machine running the suite.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

import { piAgentDir, piAgentsFile, teamNoteDocumentPath } from "../src/core/paths.js";
import {
  findProbeBinary,
  hasProbePointer,
  POINTER_BEGIN_MARKER,
  POINTER_END_MARKER,
  readTeamNote,
  type ProbeBinaryDeps,
} from "../src/core/teamNote.js";
import { renderTeamNoteForPrompt } from "../src/teamNotePrompt.js";

describe("hasProbePointer", () => {
  const env = { PI_CODING_AGENT_DIR: "/custom/pi-dir" };
  const reading = (text: string) => (path: string) => {
    expect(path).toBe(piAgentsFile(env));
    expect(path).toBe(join("/custom/pi-dir", "AGENTS.md"));
    return text;
  };

  it("finds Probe's block in pi's AGENTS.md, a damaged one included", () => {
    expect(hasProbePointer(env, reading(`# rules\n${POINTER_BEGIN_MARKER}\nbody\n${POINTER_END_MARKER}\n`))).toBe(true);
    expect(hasProbePointer(env, reading(`${POINTER_BEGIN_MARKER}\nbody, end lost`))).toBe(true);
    expect(hasProbePointer(env, reading(`body, begin lost\n${POINTER_END_MARKER}`))).toBe(true);
  });

  it("is false for a file without it, and for a file that cannot be read", () => {
    expect(hasProbePointer(env, reading("# my own rules\n<!-- probe-team-note:begin -->\n"))).toBe(false);
    expect(
      hasProbePointer(env, () => {
        throw new Error("ENOENT");
      }),
    ).toBe(false);
  });
});

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

