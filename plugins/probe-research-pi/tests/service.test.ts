import { describe, expect, it } from "vitest";

import { ensureService, serviceSupported, ServicelessPlatform } from "../src/core/service.js";
import type { SpawnFn } from "../src/core/daemon.js";

describe("ensureService", () => {
  it("starts the service on Windows too, since its named-pipe transport (R3)", () => {
    const calls: string[][] = [];
    const spawn: SpawnFn = (_command, args) => {
      calls.push(args);
      return { pid: 7, unref: () => {} };
    };
    const ok = ensureService(
      { python: "C:\\Python\\python.exe", tapRoot: "C:\\tap" },
      { spawn, env: {}, log: () => {}, platform: "win32" },
    );
    expect(ok).toBe(true);
    expect(calls).toEqual([["-m", "tap", "service", "ensure"]]);
    expect(Object.keys(ServicelessPlatform)).toEqual([]);
    for (const platform of ["win32", "darwin", "linux"] as const) {
      expect(serviceSupported(platform)).toBe(true);
    }
  });

  it("runs the tap's copy of the client, detached, with the tap on PYTHONPATH", () => {
    const calls: { command: string; args: string[]; options: unknown }[] = [];
    let unrefs = 0;
    const spawn: SpawnFn = (command, args, options) => {
      calls.push({ command, args, options });
      return { pid: 7, unref: () => { unrefs += 1; } };
    };
    const ok = ensureService(
      { python: "/usr/bin/python3", tapRoot: "/opt/tap" },
      { spawn, env: { PATH: "/usr/bin" }, log: () => {} },
    );
    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0].command).toBe("/usr/bin/python3");
    expect(calls[0].args).toEqual(["-m", "tap", "service", "ensure"]);
    const options = calls[0].options as { detached: boolean; stdio: string; env: Record<string, string> };
    expect(options.detached).toBe(true);
    expect(options.stdio).toBe("ignore");
    expect(options.env.PYTHONPATH).toBe("/opt/tap");
    expect(unrefs).toBe(1);
  });

  it("never throws when the spawn does", () => {
    const logged: string[] = [];
    const spawn: SpawnFn = () => {
      throw new Error("EACCES");
    };
    const ok = ensureService({ python: "/nope" }, { spawn, env: {}, log: (m) => logged.push(m) });
    expect(ok).toBe(false);
    expect(logged[0]).toContain("EACCES");
  });
});
