import { execFile } from "node:child_process";
import { accessSync, chmodSync, constants, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { INLINE_SKILL_NAMES, inlineSkillsText } from "../src/inlineNotice.js";

const SID = "11111111-2222-3333-4444-555555555555";
const pkg = join(__dirname, "..");
let tmp: string;
let env: Record<string, string>;

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-parity-"));
  env = { XDG_STATE_HOME: join(tmp, "state"), HOME: tmp, PATH: join(tmp, "bin") };
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

describe("notes parity: pi", () => {
  it("gives an inline agent the three skills whole, as the daemon's writer reads them", () => {
    // The text `bite.skills_text(SHARED_SKILLS, reader="main-agent")` renders,
    // written by `make sync-pi-skills`; test_notes_parity.py holds the fixture to
    // the daemon's own function.
    const daemons = readFileSync(join(__dirname, "fixtures", "inline-skills.md"), "utf8");
    expect(daemons.startsWith("# Skill: track-work\n\n")).toBe(true);
    expect(inlineSkillsText(pkg)).toBe(daemons);
    expect(INLINE_SKILL_NAMES).toEqual(["track-work", "write-summary", "edit-notes"]);
    expect(inlineSkillsText(tmp)).toBe("");
  });

});
