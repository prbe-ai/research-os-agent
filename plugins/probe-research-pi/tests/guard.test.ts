import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  DAEMON_PROFILE_DENY,
  DENY_REASON_APPROVALS,
  guardToolCall,
  INLINE_SWITCH_DENY,
  PiTool,
  RESEARCHER_SWITCH_DENY,
  resolveLikePi,
  sessionIsInline,
} from "../src/guard.js";
import { approvalsDir } from "../src/core/paths.js";

let tmp: string;
let env: Record<string, string | undefined>;

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-pi-guard-"));
  env = { XDG_STATE_HOME: join(tmp, "state") };
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

const agent = () => ({ daemonProfile: false, env, cwd: tmp });
const daemon = () => ({ daemonProfile: true, env, cwd: tmp });

function approvalsRefusal(tool: string, path: string): string {
  return DENY_REASON_APPROVALS.replace("{tool}", tool).replace("{path}", path);
}

describe("the daemon's question folder, in both profiles", () => {
  it("refuses a write or edit of a file in it, however the path is spelled", () => {
    const answer = join(approvalsDir(env), "answers", "7f3a09c1.json");
    for (const context of [agent(), daemon()]) {
      expect(guardToolCall(PiTool.Write, { path: answer, content: "{}" }, context)).toBe(approvalsRefusal("write", answer));
      expect(guardToolCall(PiTool.Edit, { path: answer, edits: [] }, context)).toBe(approvalsRefusal("edit", answer));
    }
    // Relative to the session's cwd, and with pi's own `@` prefix.
    const relative = join("state", "probe", "approvals", "requests", "x.json");
    expect(guardToolCall(PiTool.Write, { path: relative }, agent())).toBe(approvalsRefusal("write", relative));
    expect(guardToolCall(PiTool.Write, { path: `@${relative}` }, agent())).toBe(approvalsRefusal("write", `@${relative}`));
    // The folder itself.
    expect(guardToolCall(PiTool.Write, { path: approvalsDir(env) }, agent())).not.toBeNull();
  });

  it("refuses a file:// URL into it, and one with pi's `@` prefix (pi's tools resolve both)", () => {
    const answer = join(approvalsDir(env), "answers", "7f3a09c1.json");
    const url = pathToFileURL(answer).href;
    for (const path of [url, `@${url}`]) {
      expect(guardToolCall(PiTool.Write, { path, content: "{}" }, agent())).toBe(approvalsRefusal("write", path));
      expect(guardToolCall(PiTool.Edit, { path, edits: [] }, daemon())).toBe(approvalsRefusal("edit", path));
    }
    // A unicode space pi folds to a plain one.
    const spaced = join(tmp, "state", "probe", "approvals", "answers", "a\u00A0b.json");
    expect(guardToolCall(PiTool.Write, { path: spaced }, agent())).not.toBeNull();
  });

  it("sees through a symlink into the folder", () => {
    mkdirSync(join(approvalsDir(env), "answers"), { recursive: true });
    const link = join(tmp, "innocent");
    symlinkSync(approvalsDir(env), link);
    expect(guardToolCall(PiTool.Write, { path: join(link, "answers", "x.json") }, agent())).not.toBeNull();
  });

  it("refuses a shell command that names it", () => {
    const folder = approvalsDir(env);
    for (const command of [
      `echo '{"answer":"yes"}' > ${join(folder, "answers", "x.json")}`,
      "cat ~/.local/state/probe/approvals/requests/x.json",
      "cd $XDG_STATE_HOME/probe && cp y approvals/answers/x.json",
    ]) {
      expect(guardToolCall(PiTool.Bash, { command }, agent())).toBe(approvalsRefusal("bash", folder));
      expect(guardToolCall(PiTool.PowerShell, { command }, daemon())).toBe(approvalsRefusal("powershell", folder));
    }
  });

  it("leaves everything else alone", () => {
    expect(guardToolCall(PiTool.Write, { path: join(tmp, "approvals.md") }, agent())).toBeNull();
    expect(guardToolCall(PiTool.Write, { path: join(approvalsDir(env) + "-old", "x.json") }, agent())).toBeNull();
    expect(guardToolCall("read", { path: join(approvalsDir(env), "requests", "x.json") }, agent())).toBeNull();
    expect(guardToolCall(PiTool.Bash, { command: "ls approvals/" }, agent())).toBeNull();
    expect(guardToolCall(PiTool.Write, { content: "no path" }, agent())).toBeNull();
    expect(guardToolCall(PiTool.Bash, null, agent())).toBeNull();
  });
});

describe("the daemon profile's allowlist", () => {
  it("refuses a probe command the daemon owns, naming it", () => {
    expect(guardToolCall(PiTool.Bash, { command: "cd repo && probe run list --limit 5" }, daemon())).toBe(
      DAEMON_PROFILE_DENY.replace("{matched}", "probe run list"),
    );
  });

  it("lets the agent ask, launch and instrument its runs", () => {
    for (const command of ['probe ask "what did we try"', "probe exec -- python train.py", "probe session status", "probe run end r1"]) {
      expect(guardToolCall(PiTool.Bash, { command }, daemon())).toBeNull();
    }
  });

  it("refuses every Probe MCP tool, naming it", () => {
    for (const tool of ["probe-research-pi__probe_browse", "probe_mcp_search_knowledge", "mcp__probe_research__entity"]) {
      expect(guardToolCall(tool, { query: "x" }, daemon())).toBe(DAEMON_PROFILE_DENY.replace("{matched}", tool));
    }
  });

  it("is off in the agent profile", () => {
    expect(guardToolCall(PiTool.Bash, { command: "probe run list" }, agent())).toBeNull();
    expect(guardToolCall("probe_mcp_browse", {}, agent())).toBeNull();
  });

  it("refuses the researcher's switch as the switch, never with DAEMON_PROFILE_DENY's pointer to `/probe inline`", () => {
    for (const [command, matched] of [
      ["probe session state on", "probe session state"],
      ["probe session --help; probe session toggle", "probe session toggle"],
      ["probe run list; probe session default on", "probe session default"],
    ]) {
      expect(guardToolCall(PiTool.Bash, { command }, daemon())).toBe(RESEARCHER_SWITCH_DENY.replace("{matched}", matched));
    }
  });

  it("never lets the agent hand Probe to itself (`/probe inline` is the researcher's)", () => {
    expect(guardToolCall(PiTool.Bash, { command: "probe session state inline" }, daemon())).toBe(
      RESEARCHER_SWITCH_DENY.replace("{matched}", "probe session state"),
    );
    for (const command of ["probe session inline", "probe session daemon"]) {
      expect(guardToolCall(PiTool.Bash, { command }, daemon())).toBe(
        DAEMON_PROFILE_DENY.replace("{matched}", "probe session"),
      );
    }
    expect(DAEMON_PROFILE_DENY).toContain("ask the researcher to type `/probe inline`");
  });

  it("refuses only the question folder and the researcher's switch while Probe is inline", () => {
    const inline = { ...daemon(), inline: true };
    for (const command of ["probe project create my-sweep", "probe run list", "probe session status"]) {
      expect(guardToolCall(PiTool.Bash, { command }, inline)).toBeNull();
    }
    expect(guardToolCall("probe_mcp_browse", {}, inline)).toBeNull();
    const answer = join(approvalsDir(env), "answers", "x.json");
    expect(guardToolCall(PiTool.Write, { path: answer }, inline)).toBe(approvalsRefusal(PiTool.Write, answer));
    for (const [command, matched] of [
      ["probe session state on", "probe session state"],
      ["probe session state inline", "probe session state"],
      ["probe run list; probe session default on", "probe session default"],
    ]) {
      expect(guardToolCall(PiTool.Bash, { command }, inline)).toBe(INLINE_SWITCH_DENY.replace("{matched}", matched));
    }
  });
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
