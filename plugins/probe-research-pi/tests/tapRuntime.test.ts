import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { resolveTapRuntime, type TapRuntimeDeps } from "../src/tapRuntime.js";

function fakeFs(existing: Set<string>, executable: Set<string>): Pick<TapRuntimeDeps, "existsSync" | "isExecutable"> {
  return {
    existsSync: (p) => existing.has(p),
    isExecutable: (p) => executable.has(p),
  };
}

describe("resolveTapRuntime", () => {
  const extensionDir = "/home/user/.pi/agent/extensions/probe-research-pi";

  it("prefers PROBE_PI_TAP_ROOT's venv python when the checkout and venv both exist", () => {
    const root = "/opt/tap-checkout";
    const existing = new Set([join(root, "tap", "__init__.py"), join(root, ".venv", "bin", "python3")]);
    const executable = new Set([join(root, ".venv", "bin", "python3")]);

    const runtime = resolveTapRuntime({
      ...fakeFs(existing, executable),
      env: { PROBE_PI_TAP_ROOT: root },
      extensionDir,
    });

    expect(runtime).toEqual({ python: join(root, ".venv", "bin", "python3"), tapRoot: root });
  });

  it("falls back to PATH when PROBE_PI_TAP_ROOT exists but has no venv", () => {
    const root = "/opt/tap-checkout";
    const existing = new Set([join(root, "tap", "__init__.py"), "/usr/bin/python3"]);
    const executable = new Set(["/usr/bin/python3"]);

    const runtime = resolveTapRuntime({
      ...fakeFs(existing, executable),
      env: { PROBE_PI_TAP_ROOT: root, PATH: "/usr/bin" },
      extensionDir,
    });

    expect(runtime).toEqual({ python: "/usr/bin/python3", tapRoot: root });
  });

  it("uses the sibling probe-research-tap checkout when PROBE_PI_TAP_ROOT is unset", () => {
    const siblingRoot = join(extensionDir, "..", "probe-research-tap");
    const existing = new Set([join(siblingRoot, "tap", "__init__.py"), "/usr/bin/python3"]);
    const executable = new Set(["/usr/bin/python3"]);

    const runtime = resolveTapRuntime({
      ...fakeFs(existing, executable),
      env: { PATH: "/usr/bin" },
      extensionDir,
    });

    expect(runtime).toEqual({ python: "/usr/bin/python3", tapRoot: siblingRoot });
  });

  it("falls back to a bare interpreter on PATH when no tap checkout is found anywhere (assumed pip install)", () => {
    const existing = new Set(["/usr/bin/python3"]);
    const executable = new Set(["/usr/bin/python3"]);

    const runtime = resolveTapRuntime({
      ...fakeFs(existing, executable),
      env: { PATH: "/usr/bin" },
      extensionDir,
    });

    expect(runtime).toEqual({ python: "/usr/bin/python3" });
    expect(runtime?.tapRoot).toBeUndefined();
  });

  it("prefers python3 over python when both are on PATH", () => {
    const existing = new Set(["/usr/bin/python3", "/usr/bin/python"]);
    const executable = new Set(["/usr/bin/python3", "/usr/bin/python"]);

    const runtime = resolveTapRuntime({
      ...fakeFs(existing, executable),
      env: { PATH: "/usr/bin" },
      extensionDir,
    });

    expect(runtime?.python).toBe("/usr/bin/python3");
  });

  it("returns null when no interpreter can be found at all", () => {
    const runtime = resolveTapRuntime({
      ...fakeFs(new Set(), new Set()),
      env: { PATH: "/usr/bin" },
      extensionDir,
    });

    expect(runtime).toBeNull();
  });

  it("skips a PATH entry where the file exists but is not executable", () => {
    const existing = new Set(["/usr/bin/python3"]);
    const executable = new Set<string>(); // exists, but not marked executable

    const runtime = resolveTapRuntime({
      ...fakeFs(existing, executable),
      env: { PATH: "/usr/bin" },
      extensionDir,
    });

    expect(runtime).toBeNull();
  });
});
