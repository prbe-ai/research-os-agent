import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  INLINE_NOTICE,
  forgetInlineShown,
  inlineNotice,
  inlineNoticeDue,
  markInlineShown,
} from "../src/inlineNotice.js";

const SID = "11111111-2222-3333-4444-555555555555";
let tmp: string;
let env: Record<string, string>;
const sessions = () => join(tmp, "state", "probe", "sessions");
const write = (name: string, body: string) => {
  mkdirSync(sessions(), { recursive: true });
  writeFileSync(join(sessions(), `${SID}.${name}`), body);
};

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-inline-"));
  env = { XDG_STATE_HOME: join(tmp, "state"), HOME: tmp };
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

describe("the inline notice (/probe inline)", () => {
  it("names this package's main-agent skills, which ship with it", () => {
    const pkg = join(__dirname, "..");
    const notice = inlineNotice(pkg);
    expect(notice).not.toContain("{");
    for (const name of ["track-work", "edit-notes"]) {
      const path = join(pkg, "skills", name, "SKILL.md");
      expect(notice).toContain(path);
      expect(readFileSync(path, "utf8")).toContain(`name: ${name}`);
    }
    expect(INLINE_NOTICE.length).toBeLessThan(1000);
  });

  it("is due once per stretch, and again after a compaction or resume", () => {
    expect(inlineNoticeDue(SID, env)).toBeNull();
    write("state", "full\n");
    write("inline", JSON.stringify({ from: "daemon", since: 1791266269.25 }));
    expect(inlineNoticeDue(SID, env)).toBe(1791266269.25);
    markInlineShown(SID, 1791266269.25, env);
    expect(inlineNoticeDue(SID, env)).toBeNull();
    forgetInlineShown(SID, env);
    expect(inlineNoticeDue(SID, env)).toBe(1791266269.25);
    markInlineShown(SID, 1791266269.25, env);
    write("inline", JSON.stringify({ from: "daemon", since: 1791266300 }));
    expect(inlineNoticeDue(SID, env)).toBe(1791266300);
  });

  it("reads the Python hooks' spelling of the stretch as the same number", () => {
    write("state", "full\n");
    write("inline", JSON.stringify({ from: "off", since: 100 }));
    write("inline-shown", "100.0\n"); // `repr(100.0)`, as session_marker writes it
    expect(inlineNoticeDue(SID, env)).toBeNull();
  });
});
