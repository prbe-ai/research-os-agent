import { tmpdir } from "node:os";

import { describe, expect, it } from "vitest";

import { probeConfigPath } from "../src/core/paths.js";

// Guards tests/setup.ts: without it the suite reads the developer's real
// Probe config, and the MCP OAuth tests pass or fail by whose machine it is.
describe("test isolation", () => {
  it("resolves the probe CLI config under a temp dir, never the real home", () => {
    expect(probeConfigPath(process.env).startsWith(tmpdir())).toBe(true);
  });

  it("carries no Probe credential from the developer's shell", () => {
    expect(process.env.PROBE_MCP_TOKEN).toBeUndefined();
    expect(process.env.PROBE_INGEST_TOKEN).toBeUndefined();
    expect(process.env.PROBE_PI_TAP_TOKEN).toBeUndefined();
  });
});
