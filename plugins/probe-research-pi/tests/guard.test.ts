import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { DAEMON_PROFILE_DENY, DENY_REASON_APPROVALS, guardToolCall, PiTool, resolveLikePi } from "../src/guard.js";
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
