import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { resolveMcpBearerToken } from "../src/mcpAuth.js";

describe("resolveMcpBearerToken", () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "probe-pi-mcp-auth-"));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("returns null when nothing is configured anywhere", () => {
    const env = { PROBE_CONFIG_PATH: join(dir, "no-such-config.json") };

    expect(resolveMcpBearerToken(env)).toBeNull();
  });

  it("prefers PROBE_MCP_TOKEN over the config file (env-var precedence)", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(configPath, JSON.stringify({ mcp_token: "config-token-should-lose" }));
    const env = { PROBE_CONFIG_PATH: configPath, PROBE_MCP_TOKEN: "env-token-wins" };

    const result = resolveMcpBearerToken(env);

    expect(result).not.toBeNull();
    expect(result?.token).toBe("env-token-wins");
    expect(result?.source).toBe("env");
    expect(result?.detail).toBe("PROBE_MCP_TOKEN");
  });

  it("treats a whitespace-only PROBE_MCP_TOKEN as unset and falls through to the config file", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(configPath, JSON.stringify({ mcp_token: "config-token-xyz" }));
    const env = { PROBE_CONFIG_PATH: configPath, PROBE_MCP_TOKEN: "   " };

    const result = resolveMcpBearerToken(env);

    expect(result?.token).toBe("config-token-xyz");
    expect(result?.source).toBe("probe-cli");
  });

  it("reads mcp_token from the v1 flat config shape", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(configPath, JSON.stringify({ mcp_token: "v1-mcp-token", ingest_token: "should-not-be-read" }));
    const env = { PROBE_CONFIG_PATH: configPath };

    const result = resolveMcpBearerToken(env);

    expect(result).not.toBeNull();
    expect(result?.token).toBe("v1-mcp-token");
    expect(result?.source).toBe("probe-cli");
    expect(result?.detail).toBe(configPath);
  });

  it("reads mcp_token from the v2 named-contexts config shape, honouring current_context", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        current_context: "work",
        contexts: {
          default: { mcp_token: "wrong-context-token" },
          work: { mcp_token: "v2-mcp-token" },
        },
      }),
    );
    const env = { PROBE_CONFIG_PATH: configPath };

    const result = resolveMcpBearerToken(env);

    expect(result?.token).toBe("v2-mcp-token");
  });

  it("falls back to the v2 'default' context when current_context is unset", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        contexts: { default: { mcp_token: "default-context-token" } },
      }),
    );
    const env = { PROBE_CONFIG_PATH: configPath };

    expect(resolveMcpBearerToken(env)?.token).toBe("default-context-token");
  });

  it("is null (not a fallback to 'default') when current_context names a context that does not exist", () => {
    // Matches pairing.ts's readProbeConfigIngestToken exactly, which this
    // module mirrors byte-for-byte: the "falls back to default" behavior is
    // for an ABSENT current_context, not one naming an unknown context — a
    // named-but-missing context resolves to {}, not another context's data.
    const configPath = join(dir, "config.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        current_context: "no-such-context",
        contexts: { default: { mcp_token: "default-context-token" } },
      }),
    );
    const env = { PROBE_CONFIG_PATH: configPath };

    expect(resolveMcpBearerToken(env)).toBeNull();
  });

  it("never reads the write-scoped ingest_token as if it were mcp_token", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(configPath, JSON.stringify({ ingest_token: "write-token-must-not-leak" }));
    const env = { PROBE_CONFIG_PATH: configPath };

    expect(resolveMcpBearerToken(env)).toBeNull();
  });

  it("is null when config.json is malformed JSON", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(configPath, "{not json");
    const env = { PROBE_CONFIG_PATH: configPath };

    expect(resolveMcpBearerToken(env)).toBeNull();
  });

  it("is null when mcp_token is present but whitespace-only", () => {
    const configPath = join(dir, "config.json");
    writeFileSync(configPath, JSON.stringify({ mcp_token: "   " }));
    const env = { PROBE_CONFIG_PATH: configPath };

    expect(resolveMcpBearerToken(env)).toBeNull();
  });

  it("respects XDG_CONFIG_HOME when PROBE_CONFIG_PATH is unset", () => {
    const xdgDir = join(dir, "xdg");
    mkdirSync(join(xdgDir, "probe"), { recursive: true });
    writeFileSync(join(xdgDir, "probe", "config.json"), JSON.stringify({ mcp_token: "xdg-token" }));
    const env = { XDG_CONFIG_HOME: xdgDir };

    expect(resolveMcpBearerToken(env)?.token).toBe("xdg-token");
  });
});
