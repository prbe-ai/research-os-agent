import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  ensureService,
  SERVICE_CONFIG_KEY,
  SERVICE_ENV,
  serviceEnabled,
} from "../src/core/service.js";
import type { SpawnFn } from "../src/core/daemon.js";

function readerOf(files: Record<string, string>): (path: string) => string {
  return (path) => {
    if (path in files) return files[path];
    throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
  };
}

describe("serviceEnabled", () => {
  const config = join("/home/r", ".config", "probe", "config.json");
  const env = { XDG_CONFIG_HOME: join("/home/r", ".config") };

  it("is off by default", () => {
    expect(serviceEnabled({ env, readFileSync: readerOf({}) })).toBe(false);
  });

  it("reads the config key", () => {
    const files = { [config]: JSON.stringify({ [SERVICE_CONFIG_KEY]: "on" }) };
    expect(serviceEnabled({ env, readFileSync: readerOf(files) })).toBe(true);
  });

  it("lets the environment win, both ways", () => {
    const on = { [config]: JSON.stringify({ [SERVICE_CONFIG_KEY]: "on" }) };
    expect(serviceEnabled({ env: { ...env, [SERVICE_ENV]: "off" }, readFileSync: readerOf(on) })).toBe(false);
    expect(serviceEnabled({ env: { ...env, [SERVICE_ENV]: "on" }, readFileSync: readerOf({}) })).toBe(true);
  });

  it("treats a broken config as off", () => {
    expect(serviceEnabled({ env, readFileSync: readerOf({ [config]: "{not json" }) })).toBe(false);
  });
});

describe("ensureService", () => {
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
