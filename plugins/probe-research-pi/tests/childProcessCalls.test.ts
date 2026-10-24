/**
 * How this package starts its short-lived helper processes, asked of the calls
 * themselves (`node:child_process` is replaced here):
 *  - by ABSOLUTE path where a PATH search could pick a planted program: Windows
 *    searches the current folder first, so a bare `reg` runs `.\reg.exe`;
 *  - with no console window on Windows (`windowsHide`): every python probe would
 *    otherwise flash one.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const calls: { command: string; args: string[]; options: Record<string, unknown> }[] = [];

vi.mock("node:child_process", async () => {
  const actual = await vi.importActual<typeof import("node:child_process")>("node:child_process");
  return {
    ...actual,
    execFileSync: (command: string, args: string[], options: Record<string, unknown>) => {
      calls.push({ command, args, options });
      throw new Error("not run in this test");
    },
  };
});

const { machineId } = await import("../src/core/hosted.js");
const { pythonRunsTap, pythonSeen } = await import("../src/core/tapRuntime.js");

afterEach(() => {
  calls.length = 0;
});

describe("the machine id's helpers run from where the OS keeps them", () => {
  it("the Mac's ioreg is /usr/sbin/ioreg", () => {
    machineId({ platform: "darwin", hostname: () => "mac" });
    expect(calls.map((c) => c.command)).toEqual(["/usr/sbin/ioreg"]);
  });

  it("Windows's reg.exe is %SystemRoot%\\System32\\reg.exe, never a bare `reg`", () => {
    const saved = process.env.SystemRoot;
    process.env.SystemRoot = "D:\\Windows";
    try {
      machineId({ platform: "win32", hostname: () => "pc" });
    } finally {
      if (saved === undefined) delete process.env.SystemRoot;
      else process.env.SystemRoot = saved;
    }
    expect(calls.map((c) => c.command)).toEqual(["D:\\Windows\\System32\\reg.exe"]);
    expect(calls[0].options.windowsHide).toBe(true);
  });
});

describe("python probes open no console window", () => {
  it("the version probe", () => {
    pythonRunsTap("/opt/never-asked-before/bin/python3");
    expect(calls).toHaveLength(1);
    expect(calls[0].options.windowsHide).toBe(true);
  });

  it("the what-is-python3 probe", () => {
    pythonSeen({ existsSync: () => true, isExecutable: () => true, env: { PATH: "/opt/seen/bin" } });
    expect(calls).toHaveLength(1);
    expect(calls[0].options.windowsHide).toBe(true);
  });
});
