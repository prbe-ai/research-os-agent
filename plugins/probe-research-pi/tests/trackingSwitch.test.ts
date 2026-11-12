import { describe, expect, it, vi } from "vitest";

import { readFileSync } from "node:fs";

import { agentSessionEnv } from "../src/extension.js";
import { handBackNotice, parseSwitchIntent, switchAppliedNotice } from "../src/core/trackingSwitch.js";

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

describe("/probe auto", () => {
  it("is its own direction in every spelling, never a state", () => {
    for (const text of ["/probe auto", "$probe auto", "/skill:probe auto", "/probe AUTO"]) {
      expect(parseSwitchIntent(text)).toEqual({ direction: "auto", canonicalText: `/skill:probe ${text.split(" ")[1]}` });
    }
  });

  it("hands the model the service's outcome, and nothing when the service said none", () => {
    expect(handBackNotice(["Probe: this chat is already in auto mode."])).toBe("\n\n(Probe: this chat is already in auto mode.)");
    expect(handBackNotice([])).toBe("");
  });
});
