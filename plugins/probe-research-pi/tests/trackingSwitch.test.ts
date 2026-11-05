import { describe, expect, it, vi } from "vitest";

import { readFileSync } from "node:fs";

import { agentSessionEnv } from "../src/extension.js";
import { parseSwitchIntent, switchAppliedNotice } from "../src/core/trackingSwitch.js";

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
