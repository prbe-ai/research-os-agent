import { describe, expect, it } from "vitest";

import { ENSURE_ARGS, ensureService, lookupServiceSocket, serviceSupported, ServicelessPlatform } from "../src/core/service.js";
import type { TrackingExecOptions } from "../src/core/trackingState.js";
import type { SpawnFn } from "../src/core/daemon.js";

describe("ensureService: the CLI's own `ensure`, the one every client runs (service-only T37)", () => {
  it("runs `python -I -m probe.service.client ensure` on the CLI's interpreter, detached", () => {
    const calls: { command: string; args: string[]; options: unknown }[] = [];
    let unrefs = 0;
    const spawn: SpawnFn = (command, args, options) => {
      calls.push({ command, args, options });
      return { pid: 7, unref: () => { unrefs += 1; } };
    };
    const ok = ensureService("/home/u/.local/share/uv/tools/probe-research/bin/python", {
      spawn, env: { PATH: "/usr/bin", PYTHONPATH: "/project" }, log: () => {},
    });
    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0].command).toBe("/home/u/.local/share/uv/tools/probe-research/bin/python");
    expect(calls[0].args).toEqual(["-I", "-m", "probe.service.client", "ensure"]);
    expect([...ENSURE_ARGS]).toEqual(calls[0].args);
    const options = calls[0].options as { detached: boolean; stdio: string; env: Record<string, string> };
    expect(options.detached).toBe(true);
    expect(options.stdio).toBe("ignore");
    expect(unrefs).toBe(1);
  });

  it("starts nothing with no CLI installed, and never throws when the spawn does", () => {
    const logged: string[] = [];
    let spawned = 0;
    const spawn: SpawnFn = () => {
      spawned += 1;
      throw new Error("EACCES");
    };
    expect(ensureService(null, { spawn, env: {}, log: (m) => logged.push(m) })).toBe(false);
    expect(spawned).toBe(0);
    expect(ensureService("/nope", { spawn, env: {}, log: (m) => logged.push(m) })).toBe(false);
    expect(logged[0]).toContain("EACCES");
  });

  it("knows no platform the service cannot run on", () => {
    expect(Object.keys(ServicelessPlatform)).toEqual([]);
    for (const platform of ["darwin", "linux"] as const) {
      expect(serviceSupported(platform)).toBe(true);
    }
  });
});


describe("lookupServiceSocket: `python -m tap` starts in the tap's folder, never pi's", () => {
  it("names the tap's own folder (or /) as the child's folder, so a planted tap/ package is never imported", async () => {
    const seen: TrackingExecOptions[] = [];
    const execFile = (_command: string, _args: string[], options: TrackingExecOptions,
      callback: (error: Error | null, stdout: string, stderr: string) => void) => {
      seen.push(options);
      callback(null, JSON.stringify({ socket: "/tmp/probe/service.sock" }), "");
    };
    const deps = { execFile, env: { PATH: "/usr/bin", HOME: "/home/u" }, log: () => {}, platform: "linux" as const };
    expect(await lookupServiceSocket({ python: "/usr/bin/python3", tapRoot: "/opt/tap" }, deps)).toBe("/tmp/probe/service.sock");
    expect(await lookupServiceSocket({ python: "/usr/bin/python3" }, deps)).toBe("/tmp/probe/service.sock");
    expect(seen.map((options) => options.cwd)).toEqual(["/opt/tap", "/"]);
  });
});
