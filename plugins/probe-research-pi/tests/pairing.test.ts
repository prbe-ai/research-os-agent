import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { checkPairing } from "../src/core/pairing.js";

describe("checkPairing", () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "probe-pi-pairing-"));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("is unpaired when no token source exists anywhere, with a clear reason", () => {
    const env = {
      PROBE_PI_TAP_PLUGIN_DIR: join(dir, "state"),
      PROBE_CONFIG_PATH: join(dir, "no-such-config.json"),
    };

    const result = checkPairing(env);

    expect(result.paired).toBe(false);
    if (!result.paired) {
      expect(result.reason).toContain("not paired");
      expect(result.reason.length).toBeGreaterThan(0);
    }
  });

  it("is paired via the plugin-local device token file (highest precedence)", () => {
    const stateDir = join(dir, "state");
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(join(stateDir, ".token"), "device-token-abc\n");

    const env = {
      PROBE_PI_TAP_PLUGIN_DIR: stateDir,
      // Deliberately ALSO set the env token, to prove the device token wins.
      PROBE_PI_TAP_TOKEN: "env-token-should-not-win",
      PROBE_CONFIG_PATH: join(dir, "no-such-config.json"),
    };

    const result = checkPairing(env);

    expect(result.paired).toBe(true);
    if (result.paired) {
      expect(result.source).toBe("device-token");
    }
  });

  it("treats a whitespace-only device token file as unset and falls through to env", () => {
    const stateDir = join(dir, "state");
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(join(stateDir, ".token"), "   \n");

    const env = {
      PROBE_PI_TAP_PLUGIN_DIR: stateDir,
      PROBE_PI_TAP_TOKEN: "env-token-xyz",
      PROBE_CONFIG_PATH: join(dir, "no-such-config.json"),
    };

    const result = checkPairing(env);

    expect(result.paired).toBe(true);
    if (result.paired) {
      expect(result.source).toBe("env");
    }
  });

  it("is paired via PROBE_PI_TAP_TOKEN when no device token file exists", () => {
    const env = {
      PROBE_PI_TAP_PLUGIN_DIR: join(dir, "state"),
      PROBE_PI_TAP_TOKEN: "env-token-xyz",
      PROBE_CONFIG_PATH: join(dir, "no-such-config.json"),
    };

    const result = checkPairing(env);

    expect(result.paired).toBe(true);
    if (result.paired) {
      expect(result.source).toBe("env");
    }
  });

  // D3 (2026-09-09): the probe CLI config's `ingest_token` is Claude Code's
  // capture token; the server 403s it on /sessions/pi, so it must never count
  // as pi being paired, in either config shape.
  it("is NOT paired by the probe CLI's v1 flat config.json ingest_token", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(configPath, JSON.stringify({ ingest_token: "cli-token-v1", base_url: "https://api.example" }));

    const result = checkPairing({ PROBE_PI_TAP_PLUGIN_DIR: join(dir, "state"), PROBE_CONFIG_PATH: configPath });

    expect(result.paired).toBe(false);
  });

  it("is NOT paired by the probe CLI's v2 named-contexts config.json ingest_token", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(
      configPath,
      JSON.stringify({ current_context: "work", contexts: { work: { ingest_token: "cli-token-v2" } } }),
    );

    const result = checkPairing({ PROBE_PI_TAP_PLUGIN_DIR: join(dir, "state"), PROBE_CONFIG_PATH: configPath });

    expect(result.paired).toBe(false);
  });

  it("is unpaired when config.json is malformed JSON", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(configPath, "{not json");

    const env = {
      PROBE_PI_TAP_PLUGIN_DIR: join(dir, "state"),
      PROBE_CONFIG_PATH: configPath,
    };

    const result = checkPairing(env);

    expect(result.paired).toBe(false);
  });
});
