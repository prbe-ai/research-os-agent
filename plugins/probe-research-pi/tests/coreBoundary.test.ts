import { readdirSync, readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

/**
 * `src/core/` is the Probe-generic half of this package: it re-states Python
 * behaviour and knows nothing of pi, so a second extension-style harness could
 * carry it unchanged. An import of pi's API, or of the pi glue beside it,
 * would end that quietly; this says so instead.
 */
describe("src/core", () => {
  const core = new URL("../src/core/", import.meta.url);
  const modules = readdirSync(core).filter((name) => name.endsWith(".ts"));

  it.each(modules)("%s imports only node and its siblings in core", (name) => {
    const source = readFileSync(new URL(name, core), "utf-8");
    const specifiers = [...source.matchAll(/(?:^|\n)\s*(?:import|export)[^"';]*?from\s+"([^"]+)"/g)].map((m) => m[1]);
    for (const specifier of specifiers) {
      expect(specifier.startsWith("node:") || /^\.\/[\w-]+\.js$/.test(specifier), `${name} imports ${specifier}`).toBe(true);
    }
  });

  it("is the half it is said to be", () => {
    expect(modules.length).toBeGreaterThan(10);
  });
});
