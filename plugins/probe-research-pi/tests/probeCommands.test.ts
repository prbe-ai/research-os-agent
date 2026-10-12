import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  carriesDirected,
  daemonProfileAllows,
  daemonProfileRefusal,
  probeInvocations,
  shlexTokens,
  stripHeredocs,
} from "../src/core/probeCommands.js";

/** The Python guard's answers, shared with agent/tests/test_pi_guard_parity.py. */
const FIXTURE = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/pi_probe_commands.json", import.meta.url), "utf-8"),
) as { cases: Array<{ command: string; invocations: string[][]; refused: string | null; write: boolean | null }> };

describe("the shell parse, against the Python guard's answers", () => {
  it("covers a fixture worth the name", () => {
    expect(FIXTURE.cases.length).toBeGreaterThan(50);
    expect(FIXTURE.cases.some((c) => c.refused === null && c.invocations.length > 0)).toBe(true);
    expect(FIXTURE.cases.some((c) => c.refused !== null)).toBe(true);
  });

  it("covers a write the researcher asked for, and a read carrying the flag", () => {
    expect(FIXTURE.cases.some((c) => c.command.includes("--directed") && c.refused === null)).toBe(true);
    expect(FIXTURE.cases.some((c) => c.command.includes("--directed") && c.refused !== null && c.write === false)).toBe(true);
  });

  it.each(FIXTURE.cases)("$command", ({ command, invocations, refused, write }) => {
    expect(probeInvocations(command)).toEqual(invocations);
    const answer = daemonProfileRefusal(command);
    expect(answer?.matched ?? null).toBe(refused);
    expect(answer === null ? null : answer.write).toBe(write);
  });
});

describe("shlexTokens", () => {
  it("splits operators out of words and keeps quoted text whole", () => {
    expect(shlexTokens(`a&&b 'c|d' "e\\"f" g\\ h ''`).tokens).toEqual(["a", "&&", "b", "c|d", 'e"f', "g h", ""]);
  });

  it("reports the tokens it had before an unclosed quote", () => {
    expect(shlexTokens("a b 'c d")).toEqual({ tokens: ["a", "b"], complete: false });
  });
});

describe("stripHeredocs", () => {
  it("drops a heredoc's body and keeps the line that opens it", () => {
    expect(stripHeredocs("cat <<EOF\nprobe run list\nEOF\nls")).toBe("cat <<EOF\nls");
  });
});

describe("daemonProfileAllows", () => {
  it("lets the agent ask, check status, expect a run, and write its runs' own data", () => {
    for (const args of [["ask", "x"], ["session", "status"], ["run", "expect", "r"], ["doctor"], ["exec", "--", "python"], ["run", "start"], ["trial", "add"]]) {
      expect(daemonProfileAllows(args)[0]).toBe(true);
    }
  });

  it("refuses every other probe command, reads included, by the words it names", () => {
    expect(daemonProfileAllows(["run", "list"])).toEqual([false, "probe run list"]);
    expect(daemonProfileAllows(["notes", "push"])).toEqual([false, "probe notes push"]);
    expect(daemonProfileAllows(["session", "track"])).toEqual([false, "probe session"]);
  });

  it("lets through a write the researcher asked for, and no read or switch move", () => {
    expect(daemonProfileAllows(["project", "move", "willow-dictation-training", "--workspace", "willow", "--directed"])).toEqual([
      true,
      "probe project move",
    ]);
    expect(daemonProfileAllows(["workspace", "list", "--directed"])).toEqual([false, "probe workspace"]);
    expect(daemonProfileAllows(["session", "state", "on", "--directed"])).toEqual([false, "probe session"]);
  });
});

describe("carriesDirected", () => {
  it("reads the flag only before a standalone `--`, as the CLI strips it", () => {
    expect(carriesDirected(["--directed", "notes", "push"])).toBe(true);
    expect(carriesDirected(["notes", "push", "--directed"])).toBe(true);
    expect(carriesDirected(["exec", "--", "python", "--directed"])).toBe(false);
  });
});
