import { accessSync, chmodSync, constants, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { pythonSeen, resolveTapRuntime, type TapRuntimeDeps } from "../src/core/tapRuntime.js";

/** Every interpreter here is new enough unless a test says otherwise, and no runtime.json is read. */
function fakeFs(
  existing: Set<string>,
  executable: Set<string>,
): Pick<TapRuntimeDeps, "existsSync" | "isExecutable" | "runsTap" | "readFileSync"> {
  return {
    existsSync: (p) => existing.has(p),
    isExecutable: (p) => executable.has(p),
    runsTap: () => true,
    readFileSync: (p) => {
      throw new Error(`ENOENT: ${p}`);
    },
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

/**
 * The tap needs Python 3.11 or newer (tap/start.py MIN_PYTHON). pi used to run it
 * on the first `python3` on PATH, which on a Mac is Apple's 3.9: every `-m tap`
 * died on an ImportError and no pi session was recorded (#3338 fixed the
 * Claude Code / Codex hooks, `hooks/tap-python.sh`; pi kept its own search).
 * The order is tap-python.sh's: the checkout's .venv, `python3`, the Probe CLI's
 * interpreter (the service's runtime.json, else uv's tool folder), then
 * python3.14 .. python3.11.
 */
describe("resolveTapRuntime picks a Python the tap runs on (3.11+)", () => {
  const extensionDir = "/home/user/.pi/agent/extensions/probe-research-pi";
  const root = "/opt/tap-checkout";
  const home = "/home/user";
  const checkout = join(root, "tap", "__init__.py");

  function deps(
    pythons: Record<string, boolean>,
    extra: { files?: Record<string, string>; env?: Record<string, string> } = {},
  ): TapRuntimeDeps {
    const files = extra.files ?? {};
    return {
      existsSync: (p) => p === checkout || p in pythons || p in files,
      isExecutable: (p) => p in pythons,
      runsTap: (p) => pythons[p] === true,
      readFileSync: (p) => {
        if (!(p in files)) throw new Error(`ENOENT: ${p}`);
        return files[p];
      },
      env: { PROBE_PI_TAP_ROOT: root, PATH: "/usr/bin:/opt/homebrew/bin", HOME: home, ...extra.env },
      extensionDir,
      platform: "darwin",
    };
  }

  it("skips Apple's python3 3.9 for a newer python3.x on PATH", () => {
    const runtime = resolveTapRuntime(
      deps({ "/usr/bin/python3": false, "/opt/homebrew/bin/python3.12": true, "/opt/homebrew/bin/python3.11": true }),
    );
    expect(runtime).toEqual({ python: "/opt/homebrew/bin/python3.12", tapRoot: root });
  });

  it("takes the Probe CLI's interpreter, named by the service's runtime.json, before python3.x", () => {
    const cli = "/Users/me/.local/share/uv/tools/probe-research/bin/python";
    const runtime = resolveTapRuntime(
      deps(
        { "/usr/bin/python3": false, [cli]: true, "/opt/homebrew/bin/python3.12": true },
        { files: { [join(home, ".local", "state", "probe", "service", "runtime.json")]: JSON.stringify({ python: cli, cli_version: "0.299.3" }) } },
      ),
    );
    expect(runtime?.python).toBe(cli);
  });

  it("takes uv's tool interpreter when runtime.json names none", () => {
    const uv = join(home, ".local", "share", "uv", "tools", "probe-research", "bin", "python");
    const runtime = resolveTapRuntime(deps({ "/usr/bin/python3": false, [uv]: true, "/opt/homebrew/bin/python3.13": true }));
    expect(runtime?.python).toBe(uv);
  });

  it("keeps the checkout's .venv and a new-enough python3 first, as before", () => {
    const venv = join(root, ".venv", "bin", "python3");
    expect(resolveTapRuntime(deps({ [venv]: true, "/usr/bin/python3": true }))?.python).toBe(venv);
    expect(resolveTapRuntime(deps({ [venv]: false, "/usr/bin/python3": true, "/opt/homebrew/bin/python3.12": true }))?.python).toBe(
      "/usr/bin/python3",
    );
  });

  it("finds nothing when no interpreter is 3.11 or newer", () => {
    expect(resolveTapRuntime(deps({ "/usr/bin/python3": false, "/usr/bin/python": false }))).toBeNull();
  });

  it("asks each interpreter itself (no injected check): a python3 that is too old is skipped", () => {
    const dir = mkdtempSync(join(tmpdir(), "pi-tap-python-"));
    try {
      const bin = join(dir, "bin");
      mkdirSync(bin);
      // Exit 1 to the version probe, as Apple's 3.9 does; exit 0 as a 3.11+ does.
      const old = join(bin, "python3");
      const fresh = join(bin, "python3.11");
      writeFileSync(old, "#!/bin/sh\nexit 1\n");
      writeFileSync(fresh, "#!/bin/sh\nexit 0\n");
      chmodSync(old, 0o755);
      chmodSync(fresh, 0o755);
      const runtime = resolveTapRuntime({
        existsSync,
        isExecutable: (p) => {
          try {
            accessSync(p, constants.X_OK);
            return true;
          } catch {
            return false;
          }
        },
        env: { PATH: bin, HOME: dir },
        extensionDir: join(dir, "no-extension"),
      });
      expect(runtime).toEqual({ python: fresh });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

/** What the researcher is told pi found instead (`tap-python.sh::tap_python_seen`). */
describe("pythonSeen", () => {
  function withPython3(script: string | null, fn: (deps: Pick<TapRuntimeDeps, "existsSync" | "isExecutable" | "env">) => void): void {
    const dir = mkdtempSync(join(tmpdir(), "pi-python-seen-"));
    try {
      if (script !== null) {
        writeFileSync(join(dir, "python3"), script);
        chmodSync(join(dir, "python3"), 0o755);
      }
      fn({
        existsSync,
        isExecutable: (p) => {
          try {
            accessSync(p, constants.X_OK);
            return true;
          } catch {
            return false;
          }
        },
        env: { PATH: dir },
      });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }

  it("names python3's version", () => withPython3("#!/bin/sh\necho 3.9.6\n", (deps) => expect(pythonSeen(deps)).toBe("python3 3.9.6")));
  it("says when there is no python3", () => withPython3(null, (deps) => expect(pythonSeen(deps)).toBe("no python3")));
  it("says when python3 does not run", () => withPython3("#!/bin/sh\nexit 3\n", (deps) => expect(pythonSeen(deps)).toBe("a python3 that does not run")));
});
