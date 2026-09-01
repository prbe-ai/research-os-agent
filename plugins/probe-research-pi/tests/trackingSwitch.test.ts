import { describe, expect, it, vi } from "vitest";

import { applyTrackingSwitch, parseSwitchIntent, switchAppliedNotice, type SwitchChild } from "../src/trackingSwitch.js";

describe("parseSwitchIntent", () => {
  it("reads every direction word the Python guard reads", () => {
    // Drift between the two vocabularies is the failure this pins: a
    // researcher whose `resume` works on Claude Code and not on pi has a
    // switch that appears to work only if you guess its words.
    for (const word of ["off", "stop", "disable", "end"]) {
      expect(parseSwitchIntent(`/skill:track-work ${word}`)?.direction).toBe("off");
    }
    for (const word of ["on", "start", "resume"]) {
      expect(parseSwitchIntent(`/skill:track-work ${word}`)?.direction).toBe("on");
    }
    for (const word of ["toggle", "flip"]) {
      expect(parseSwitchIntent(`/skill:track-work ${word}`)?.direction).toBe("toggle");
    }
  });

  it("treats a bare invocation as a toggle", () => {
    expect(parseSwitchIntent("/skill:track-work")?.direction).toBe("toggle");
  });

  it("never flips on a question", () => {
    expect(parseSwitchIntent("/skill:track-work status")?.direction).toBeNull();
  });

  it("writes nothing on prose it does not recognise", () => {
    expect(parseSwitchIntent("/skill:track-work maybe later")?.direction).toBeNull();
  });

  it("accepts the spellings pi cannot expand, and canonicalises them", () => {
    // pi expands ONLY `/skill:<name>`, so these two reach it as prose. They
    // are the spellings SKILL.md advertises and the ones a researcher arrives
    // from Claude Code already typing.
    for (const spelling of ["/track-work off", "$track-work off"]) {
      const intent = parseSwitchIntent(spelling);
      expect(intent?.direction).toBe("off");
      expect(intent?.canonicalText).toBe("/skill:track-work off");
    }
    expect(parseSwitchIntent("/track-work")?.canonicalText).toBe("/skill:track-work");
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
    expect(calls[0].slice(1)).toEqual(["session", "untrack", "--session", "pi-session-1"]);
  });

  it("maps each direction onto its existing subcommand", async () => {
    for (const [direction, subcommand] of [["on", "track"], ["off", "untrack"], ["toggle", "toggle"]] as const) {
      const calls: string[][] = [];
      await applyTrackingSwitch(direction, "s", fakeDeps(0, calls));
      expect(calls[0][1]).toBe("session");
      expect(calls[0][2]).toBe(subcommand);
    }
  });

  it("reports failure instead of throwing when the CLI exits nonzero", async () => {
    expect(await applyTrackingSwitch("on", "s", fakeDeps(1, []))).toBe(false);
  });

  it("resolves rather than hanging when the probe binary is missing", async () => {
    const deps = { ...fakeDeps(0, []), existsSync: () => false, isExecutable: () => false, env: {} };
    expect(await applyTrackingSwitch("on", "s", deps)).toBe(false);
  });
});

describe("switchAppliedNotice", () => {
  it("says the switch moved without claiming which way", async () => {
    // Observed live: on a bare invocation the model summarised the manual and
    // told the researcher tracking was on, moments after it had flipped off.
    // The notice must push it to READ, never hand it an answer to repeat --
    // a direction stated here would compete with `probe session status`.
    const notice = switchAppliedNotice("toggle");
    expect(notice).toContain("probe session status");
    expect(notice).not.toMatch(/\b(is now|tracking is on|tracking is off)\b/i);
  });

  it("names the request that moved it, for each direction", () => {
    for (const direction of ["on", "off", "toggle"] as const) {
      expect(switchAppliedNotice(direction)).toContain(`\`${direction}\``);
    }
  });
});
