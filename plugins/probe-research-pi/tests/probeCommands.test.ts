import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  asksHelpPlainly,
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
    expect(daemonProfileAllows(["run", "delete", "r1"])).toEqual([false, "probe run"]);
    expect(daemonProfileAllows(["wizard", "--action", "recorder"])).toEqual([false, "probe wizard"]);
  });

  it("leaves Probe's own upkeep to the agent, and the wizard only plainly", () => {
    expect(daemonProfileAllows(["update", "--check"])).toEqual([true, "probe update"]);
    expect(daemonProfileAllows(["wizard", "--action", "update", "--yes"])).toEqual([true, "probe wizard"]);
    expect(daemonProfileAllows(["service", "status"])).toEqual([true, "probe service status"]);
    expect(daemonProfileAllows(["login"])).toEqual([false, "probe login"]);
    expect(daemonProfileAllows(["wizard", "--action", "login"])).toEqual([false, "probe wizard"]);
    expect(daemonProfileAllows(["wizard", "--action=update", "--who-records", "agent"])).toEqual([false, "probe wizard"]);
    expect(daemonProfileAllows(["wizard", "--action", "update", "--who-records=agent"])).toEqual([false, "probe wizard"]);
    expect(daemonProfileAllows(["wizard", "--action", "update", "--action", "recorder"])).toEqual([false, "probe wizard"]);
    expect(daemonProfileAllows(["wizard", "--action", "logout", "--yes"])).toEqual([false, "probe wizard"]);
    expect(daemonProfileAllows(["--base-url", "https://x.example", "wizard", "--action", "update"])).toEqual([false, "probe wizard"]);
    expect(daemonProfileAllows(["login", "--token", "t"])).toEqual([false, "probe login"]);
  });

  it("keeps what moves the daemon's key, folder or queue, and fails closed on a word it cannot read", () => {
    expect(daemonProfileAllows(["logout"])).toEqual([false, "probe logout"]);
    expect(daemonProfileAllows(["context", "use", "other"])).toEqual([false, "probe context"]);
    expect(daemonProfileAllows(["outbox", "discard", "--held"])).toEqual([false, "probe outbox"]);
    expect(daemonProfileAllows(["workspace", "delete", "ws"])).toEqual([false, "probe workspace"]);
    expect(daemonProfileAllows(["run", "reproduce", "r1", "--materialize", "d"])).toEqual([false, "probe run reproduce"]);
    expect(daemonProfileAllows(["run", "reproduce", "r1"])).toEqual([true, "probe run reproduce"]);
    expect(daemonProfileAllows(["$G", "delete", "r1"])).toEqual([false, "probe $G"]);
  });

  it("takes no --help that click hands to the option before it", () => {
    expect(asksHelpPlainly(["wizard", "--help"])).toBe(true);
    expect(asksHelpPlainly(["wizard", "--who-records", "agent", "--folder", "--help"])).toBe(false);
    expect(asksHelpPlainly(["wizard", "--who-records=agent", "--help"])).toBe(true);
  });

  it("lets the agent read one run by its id, and no listing or filing of it", () => {
    expect(daemonProfileAllows(["run", "get", "r1"])).toEqual([true, "probe run get"]);
    expect(daemonProfileAllows(["metrics", "plot", "r1"])).toEqual([true, "probe metrics plot"]);
    expect(daemonProfileAllows(["snapshot-show", "r1"])).toEqual([true, "probe snapshot-show"]);
    expect(daemonProfileAllows(["series", "latest", "--project", "p1"])).toEqual([false, "probe series"]);
    expect(daemonProfileAllows(["artifact", "list", "--project", "p1"])).toEqual([false, "probe artifact list"]);
    expect(daemonProfileAllows(["run", "tag", "r1", "good"])).toEqual([false, "probe run tag"]);
  });

  it("lets through a write the researcher asked for, and no read or switch move", () => {
    expect(daemonProfileAllows(["project", "move", "willow-dictation-training", "--workspace", "willow", "--directed"])).toEqual([
      true,
      "probe project move",
    ]);
    expect(daemonProfileAllows(["run", "list", "--directed"])).toEqual([false, "probe run list"]);
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
