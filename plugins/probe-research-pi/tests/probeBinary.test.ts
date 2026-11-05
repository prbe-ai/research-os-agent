/**
 * `findProbeBinary`: where the extension finds the `probe` CLI. Deps-injected: no real file
 * system or PATH is read.
 */

import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { findProbeBinary, type ProbeBinaryDeps } from "../src/core/probeBinary.js";

function fakeProbeDeps(existing: Set<string>, executable: Set<string>, env: Record<string, string | undefined>): ProbeBinaryDeps {
  return {
    existsSync: (p) => existing.has(p),
    isExecutable: (p) => executable.has(p),
    env,
  };
}

describe("findProbeBinary", () => {
  it("finds probe on PATH first", () => {
    const onPath = join("/usr/local/bin", "probe");
    const deps = fakeProbeDeps(new Set([onPath]), new Set([onPath]), { PATH: "/usr/local/bin", HOME: "/home/x" });
    expect(findProbeBinary(deps)).toBe(onPath);
  });

  it("ignores relative PATH entries that could execute a repository-owned probe", () => {
    const fallback = join("/home/x", ".local", "bin", "probe");
    const deps = fakeProbeDeps(
      new Set(["probe", join("repo-bin", "probe"), fallback]),
      new Set(["probe", join("repo-bin", "probe"), fallback]),
      { PATH: [".", "repo-bin"].join(":"), HOME: "/home/x" },
    );

    expect(findProbeBinary(deps)).toBe(fallback);
  });

  it("falls back to ~/.local/bin/probe when PATH has nothing", () => {
    const fallback = join("/home/x", ".local", "bin", "probe");
    const deps = fakeProbeDeps(new Set([fallback]), new Set([fallback]), { PATH: "/usr/local/bin", HOME: "/home/x" });
    expect(findProbeBinary(deps)).toBe(fallback);
  });

  it("falls back to the uv tool install location when the first fallback is absent", () => {
    const fallback2 = join("/home/x", ".local", "share", "uv", "tools", "probe-research", "bin", "probe");
    const deps = fakeProbeDeps(new Set([fallback2]), new Set([fallback2]), { PATH: "", HOME: "/home/x" });
    expect(findProbeBinary(deps)).toBe(fallback2);
  });

  it("returns null when nothing resolves anywhere", () => {
    const deps = fakeProbeDeps(new Set(), new Set(), { PATH: "/usr/local/bin", HOME: "/home/x" });
    expect(findProbeBinary(deps)).toBeNull();
  });
});

