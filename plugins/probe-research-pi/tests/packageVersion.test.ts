import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { packageVersion, UNKNOWN_VERSION } from "../src/packageVersion.js";

describe("packageVersion", () => {
  it("is the version package.json declares, so the MCP handshake reports the release that runs", () => {
    const manifest = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf-8")) as { version: string };
    expect(packageVersion()).toBe(manifest.version);
  });

  it("falls back to a version no release carries when the file cannot be read", () => {
    expect(
      packageVersion(() => {
        throw new Error("EACCES");
      }),
    ).toBe(UNKNOWN_VERSION);
    expect(packageVersion(() => "{}")).toBe(UNKNOWN_VERSION);
  });
});
