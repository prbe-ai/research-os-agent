import { execFile } from "node:child_process";
import { accessSync, chmodSync, constants, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { INLINE_SKILL_NAMES, inlineSkillsText } from "../src/inlineNotice.js";
import { agentOwes, owedSendBack, type OwedDeps } from "../src/owed.js";

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

  it("asks the CLI what is owed only when the ledger holds the agent's own items", async () => {
    const dir = join(env.XDG_STATE_HOME!, "probe", "owed");
    mkdirSync(dir, { recursive: true });
    expect(agentOwes(SID, env)).toBe(false);
    writeFileSync(join(dir, `${SID}.json`), JSON.stringify({ seq: 1, items: [{ why: "note", kind: "run", ref: "r1", by: "daemon" }] }));
    expect(agentOwes(SID, env)).toBe(false);
    writeFileSync(join(dir, `${SID}.json`), JSON.stringify({ seq: 2, items: [{ why: "note", kind: "run", ref: "r1", by: "agent" }] }));
    expect(agentOwes(SID, env)).toBe(true);
    // Asked twice already: exhausted, so no CLI starts for it.
    writeFileSync(join(dir, `${SID}.json`), JSON.stringify({
      seq: 2, items: [{ why: "note", kind: "run", ref: "r1", by: "agent" }], asks: { "note run r1": 2 },
    }));
    expect(agentOwes(SID, env)).toBe(false);
    // A summary is asked about only while recorder mode was seen, as the CLI asks.
    writeFileSync(join(dir, `${SID}.json`), JSON.stringify({ seq: 3, items: [{ why: "summary", kind: "experiment", ref: "e", by: "agent" }] }));
    expect(agentOwes(SID, env)).toBe(false);
    mkdirSync(join(env.XDG_STATE_HOME!, "probe", "summaries"), { recursive: true });
    writeFileSync(join(env.XDG_STATE_HOME!, "probe", "summaries", "mode.json"), JSON.stringify({ mode: "recorder", at: Date.now() / 1000 }));
    expect(agentOwes(SID, env)).toBe(true);
    writeFileSync(join(dir, `${SID}.json`), JSON.stringify({ seq: 2, items: [{ why: "note", kind: "run", ref: "r1", by: "agent" }] }));

    mkdirSync(join(tmp, "bin"));
    // The child gets the extension's usual env (`trackingChildEnv`): PROBE_CONFIG_PATH and PROBE_AGENT=pi too.
    writeFileSync(join(tmp, "bin", "probe"), '#!/bin/sh\n[ "$1 $2 $3 $5" = "session owed --send-back ' + SID
      + '" ] && [ "$PROBE_CONFIG_PATH" = /cfg.json ] && [ "$PROBE_AGENT" = pi ] && echo "Not ended: owed."\n');
    env.PROBE_CONFIG_PATH = "/cfg.json";
    chmodSync(join(tmp, "bin", "probe"), 0o755);
    const deps: OwedDeps = {
      execFile: (command, args, options, callback) =>
        execFile(command, args, options, (error, stdout, stderr) => callback(error, String(stdout), String(stderr))),
      existsSync,
      isExecutable: (path) => {
        try {
          accessSync(path, constants.X_OK);
          return true;
        } catch {
          return false;
        }
      },
      env,
    };
    expect(await owedSendBack(SID, deps)).toBe("Not ended: owed.");
    writeFileSync(join(tmp, "bin", "probe"), "#!/bin/sh\nexit 2\n");
    expect(await owedSendBack(SID, deps)).toBeNull();
  });
});
