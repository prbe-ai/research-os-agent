/**
 * The team note document's path (`teamNoteDocumentPath`) and pi's agent folder, against the
 * Python side's own answers. Pure functions: nothing touches the actual `~/.pi` on the machine
 * running the suite.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { piAgentDir, teamNoteDocumentPath } from "../src/core/paths.js";

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
