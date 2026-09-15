import { describe, expect, it, vi } from "vitest";

import { applyTrackingSwitch, parseSwitchIntent, switchAppliedNotice, type SwitchChild } from "../src/trackingSwitch.js";

describe("parseSwitchIntent", () => {
  it("reads every direction word the Python guard reads", () => {
    // Drift between the two vocabularies is the failure this pins: a
    // researcher whose `resume` works on Claude Code and not on pi has a
    // switch that appears to work only if you guess its words.
    for (const word of ["off", "stop", "disable", "end"]) {
      expect(parseSwitchIntent(`/skill:probe ${word}`)?.direction).toBe("off");
    }
    for (const word of ["on", "start", "resume", "full"]) {
      expect(parseSwitchIntent(`/skill:probe ${word}`)?.direction).toBe("full");
    }
    for (const word of ["toggle", "flip", "cycle", "next"]) {
      expect(parseSwitchIntent(`/skill:probe ${word}`)?.direction).toBe("cycle");
    }
    for (const word of ["read-only", "readonly", "read_only", "ro"]) {
      expect(parseSwitchIntent(`/skill:probe ${word}`)?.direction).toBe("read-only");
    }
  });

  it("treats a bare invocation as one step around the cycle", () => {
    expect(parseSwitchIntent("/skill:probe")?.direction).toBe("cycle");
    expect(parseSwitchIntent("/skill:track-work")?.direction).toBe("cycle");
  });

  it("reads `off` as read-only on a legacy slug, and as off on the new switch", () => {
    // `/track-work off` has always meant "stop recording, keep searching". The
    // hard off is reachable only by naming it on the switch that introduced it.
    expect(parseSwitchIntent("/skill:track-work off")?.direction).toBe("read-only");
    expect(parseSwitchIntent("/track-work stop")?.direction).toBe("read-only");
    expect(parseSwitchIntent("/skill:probe off")?.direction).toBe("off");
    expect(parseSwitchIntent("$probe disable")?.direction).toBe("off");
  });

  it("never flips on a question", () => {
    expect(parseSwitchIntent("/skill:probe status")?.direction).toBeNull();
    expect(parseSwitchIntent("/skill:track-work status")?.direction).toBeNull();
  });

  it("writes nothing on prose it does not recognise", () => {
    expect(parseSwitchIntent("/skill:probe maybe later")?.direction).toBeNull();
  });

  it("accepts the spellings pi cannot expand, and canonicalises them", () => {
    // pi expands ONLY `/skill:<name>`, so these two reach it as prose. They
    // are the spellings SKILL.md advertises and the ones a researcher arrives
    // from Claude Code already typing.
    for (const spelling of ["/track-work off", "$track-work off"]) {
      const intent = parseSwitchIntent(spelling);
      expect(intent?.direction).toBe("read-only");
      // A legacy spelling still opens the MANUAL it has always opened.
      expect(intent?.canonicalText).toBe("/skill:track-work off");
    }
    expect(parseSwitchIntent("/track-work")?.canonicalText).toBe("/skill:track-work");
    for (const spelling of ["/probe off", "$probe off"]) {
      const intent = parseSwitchIntent(spelling);
      expect(intent?.direction).toBe("off");
      expect(intent?.canonicalText).toBe("/skill:probe off");
    }
    expect(parseSwitchIntent("/probe")?.canonicalText).toBe("/skill:probe");
  });

  it("ignores lines that merely mention the skill", () => {
    expect(parseSwitchIntent("what does /skill:track-work do?")).toBeNull();
    expect(parseSwitchIntent("/skill:track-working")).toBeNull();
    expect(parseSwitchIntent("/skill:other")).toBeNull();
  });
});

function fakeDeps(exitCode: number, calls: string[][]) {
  return {
    spawn: (command: string, args: string[]): SwitchChild => {
      calls.push([command, ...args]);
      return {
        on: (event: string, cb: (arg: unknown) => void) => {
          if (event === "exit") setTimeout(() => cb(exitCode), 0);
        },
      };
    },
    existsSync: () => true,
    isExecutable: () => true,
    env: { PATH: "/usr/bin", HOME: "/home/x" },
    log: () => {},
  };
}

describe("applyTrackingSwitch", () => {
  it("delegates the write to the CLI rather than reimplementing it", async () => {
    const calls: string[][] = [];
    vi.spyOn(process, "env", "get").mockReturnValue(process.env);
    const ok = await applyTrackingSwitch("off", "pi-session-1", fakeDeps(0, calls));
    expect(ok).toBe(true);
    expect(calls[0].slice(1)).toEqual([
      "session", "state", "off", "--session", "pi-session-1",
    ]);
  });

  it("maps each of the three states, and the cycle, onto its CLI call", async () => {
    const cases = [
      ["full", ["state", "full"]],
      ["read-only", ["state", "read-only"]],
      ["off", ["state", "off"]],
      ["cycle", ["toggle"]],
    ] as const;
    for (const [direction, tail] of cases) {
      const calls: string[][] = [];
      await applyTrackingSwitch(direction, "s", fakeDeps(0, calls));
      expect(calls[0].slice(1)).toEqual(["session", ...tail, "--session", "s"]);
    }
  });

  it("reports failure instead of throwing when the CLI exits nonzero", async () => {
    expect(await applyTrackingSwitch("full", "s", fakeDeps(1, []))).toBe(false);
  });

  it("resolves rather than hanging when the probe binary is missing", async () => {
    const deps = { ...fakeDeps(0, []), existsSync: () => false, isExecutable: () => false, env: {} };
    expect(await applyTrackingSwitch("full", "s", deps)).toBe(false);
  });
});

describe("switchAppliedNotice", () => {
  it("says the switch moved without claiming which way", async () => {
    // Observed live: on a bare invocation the model summarised the manual and
    // told the researcher tracking was on, moments after it had flipped off.
    // The notice must push it to READ, never hand it an answer to repeat --
    // a direction stated here would compete with `probe session status`.
    const notice = switchAppliedNotice("cycle");
    expect(notice).toContain("probe session status");
    expect(notice).not.toMatch(/\b(is now|tracking is on|tracking is off)\b/i);
  });

  it("names the request that moved it, for each direction", () => {
    for (const direction of ["full", "read-only", "off", "cycle"] as const) {
      expect(switchAppliedNotice(direction)).toContain(`\`${direction}\``);
    }
  });
});
