import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { PiTool, resolveLikePi, sessionIsInline, sessionInlineReadOnly } from "../src/guard.js";
import { approvalsDir, sessionsDir } from "../src/core/paths.js";
import { ProbeState } from "../src/core/trackingState.js";

let tmp: string;
let env: Record<string, string | undefined>;

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-pi-guard-"));
  env = { XDG_STATE_HOME: join(tmp, "state") };
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

describe("sessionIsInline (session_marker.is_inline)", () => {
  const SID = "11111111-2222-3333-4444-555555555555";
  const sessions = () => join(tmp, "state", "probe", "sessions");
  const write = (name: string, body: string) => {
    mkdirSync(sessions(), { recursive: true });
    writeFileSync(join(sessions(), `${SID}.${name}`), body);
  };

  it("needs `full` and an open marker naming the state `/probe inline` was typed from", () => {
    expect(sessionIsInline(SID, env)).toBe(false);
    write("state", "full\n");
    expect(sessionIsInline(SID, env)).toBe(false);
    for (const from of ["daemon", "read-only", "off"]) {
      write("inline", JSON.stringify({ from, since: 1 }));
      expect(sessionIsInline(SID, env)).toBe(true);
    }
    write("state", "daemon\n");
    expect(sessionIsInline(SID, env)).toBe(false);
    write("state", "full\n");
    write("inline", JSON.stringify({ from: "elsewhere", since: 1 }));
    expect(sessionIsInline(SID, env)).toBe(false);
    // A stretch whose log could not be written is kept closed: not inline.
    write("inline", JSON.stringify({ from: "daemon", since: 1, until: 2 }));
    expect(sessionIsInline(SID, env)).toBe(false);
    // `session_marker.inline_marker`: a finite number of seconds, nothing else.
    for (const since of ['"1"', "true", "null"]) {
      write("inline", `{"from": "daemon", "since": ${since}}`);
      expect(sessionIsInline(SID, env)).toBe(false);
    }
    write("inline", '{"from": "daemon"}');
    expect(sessionIsInline(SID, env)).toBe(false);
    expect(sessionIsInline(undefined, env)).toBe(false);
  });
});

describe("pi's own tools, as the guard reads them", () => {
  // Read out of the installed @earendil-works/pi-coding-agent (a devDependency
  // for exactly this), so a pi that renames a tool or its field fails here
  // instead of letting every call through.
  const tools = new URL("../node_modules/@earendil-works/pi-coding-agent/dist/core/tools/", import.meta.url);
  const schema = (tool: string) => readFileSync(new URL(`${tool}.d.ts`, tools), "utf-8");

  it("names the file in `path` for write and edit, and the command in `command` for bash", () => {
    expect(schema(PiTool.Write)).toMatch(/\bpath: Type\.TString;/);
    expect(schema(PiTool.Edit)).toMatch(/\bpath: Type\.TString;/);
    expect(schema(PiTool.Bash)).toMatch(/\bcommand: Type\.TString;/);
    expect(schema(PiTool.PowerShell)).toContain("export type PowerShellToolInput = BashToolInput;");
  });
});

describe("the guard resolves a path exactly as pi's write and edit do", () => {
  // pi does not export resolveToCwd, so the guard ports it; this holds the
  // port to the installed pi (the devDependency), file by file.
  const here = dirname(fileURLToPath(import.meta.url));
  const piPathUtils = join(here, "..", "node_modules", "@earendil-works", "pi-coding-agent", "dist", "core", "tools", "path-utils.js");

  it("agrees with pi's resolveToCwd on every spelling", async () => {
    const { resolveToCwd } = (await import(pathToFileURL(piPathUtils).href)) as {
      resolveToCwd: (path: string, cwd: string) => string;
    };
    const cwd = join(tmp, "work");
    const inputs = [
      "plain.json",
      "./a/../b.json",
      "@at.json",
      "~",
      "~/x.json",
      "@~/x.json",
      "/abs/olute.json",
      pathToFileURL(join(tmp, "url target.json")).href,
      `@${pathToFileURL(join(tmp, "u.json")).href}`,
      "a\u00A0b\u3000c.json",
      join(homedir(), "h.json"),
    ];
    for (const input of inputs) {
      expect(resolveLikePi(input, cwd), input).toBe(resolveToCwd(input, cwd));
    }
  });
});
