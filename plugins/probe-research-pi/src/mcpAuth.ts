/**
 * The Probe MCP fast path: "is there already a read bearer token for this
 * device." Mirrors `agent/plugins/probe-research/bin/probe-mcp-headers`
 * exactly — same resolution order, same two config shapes, same env var —
 * because that script is Claude Code's proven-in-production implementation
 * of this exact question, and its own comments record what happens when the
 * order or the shapes are wrong (see the file for the full story: the fast
 * path silently returned nothing on every install the wizard has ever
 * produced, because the read only knew the config file's v1 flat shape and
 * the file has been v2 — named contexts — since the workspace surface
 * landed).
 *
 * Resolution order, from `probe-mcp-headers`:
 *   1. `PROBE_MCP_TOKEN` env — a shell that already exports it keeps working
 *      untouched.
 *   2. The `mcp_token` key in the probe CLI's config.json, read from BOTH
 *      shapes: v2 (`contexts.<current_context>.mcp_token`, current_context
 *      defaulting to "default" exactly like `probe-mcp-headers`' python
 *      snippet and `pairing.ts`'s `readProbeConfigIngestToken`), and v1
 *      (flat top-level `mcp_token`).
 *
 * NEVER READS THE WRITE TOKEN. `probe mcp token set` writes `mcp_token`
 * (read-only) alongside `ingest_token` (capture) in the same config file;
 * this module reads only the former, on purpose — this whole surface is
 * read-only, and the day it accidentally reads a write-scoped credential
 * instead is the day a read-only MCP client can silently do more than it
 * should.
 *
 * CAVEAT, carried over from `probe-mcp-headers`' own comment: if the
 * launching shell exports a *stale* `PROBE_MCP_TOKEN`, this re-emits it on
 * every re-resolve and the token can never heal on its own — `probe mcp
 * token set` writes a config the export keeps shadowing. There is nothing
 * this module can do about that; it is surfaced in status/error text instead
 * (see `mcpBridge.ts`) so a researcher who hits it has somewhere to look.
 */

import { readFileSync } from "node:fs";

import { probeConfigPath, MCP_TOKEN_ENV, type PathEnv } from "./paths.js";

export type McpTokenSource = "env" | "probe-cli";

export interface McpBearerToken {
  token: string;
  source: McpTokenSource;
  detail: string;
}

/** The probe CLI config file's `mcp_token`, flattened to one dict whatever the file shape (v1 or v2). */
function readProbeConfigMcpToken(env: PathEnv): string | null {
  const path = probeConfigPath(env);
  let raw: string;
  try {
    raw = readFileSync(path, "utf-8");
  } catch {
    return null;
  }
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) return null;
  let flat = data as Record<string, unknown>;
  const contexts = flat.contexts;
  if (typeof contexts === "object" && contexts !== null && !Array.isArray(contexts)) {
    // v2: named contexts. An ABSENT current_context falls back to "default";
    // a current_context naming a context that does not exist resolves to {}
    // (no token), NOT to "default" either — this never hands the MCP a
    // credential for an endpoint the user is not pointed at, in either
    // direction. Verified against the actual behavior (not just the
    // intent) of pairing.ts's readProbeConfigIngestToken, which this
    // mirrors byte-for-byte, and probe-mcp-headers' python snippet.
    const contextsMap = contexts as Record<string, unknown>;
    const current =
      typeof flat.current_context === "string" && flat.current_context ? flat.current_context : "default";
    const active = contextsMap[current];
    flat = typeof active === "object" && active !== null && !Array.isArray(active) ? (active as Record<string, unknown>) : {};
  }
  const tok = flat.mcp_token;
  return typeof tok === "string" && tok.trim() ? tok.trim() : null;
}

/**
 * Resolve a Probe MCP bearer token for this device, or `null` if none is
 * configured. Synchronous and dependency-free — same reasons as
 * `pairing.ts`'s `checkPairing`: deciding whether to attempt a connection
 * must not itself require a CLI, a Python interpreter, or a network call.
 *
 * Call this AGAIN on every connect attempt and again after a 401/403 rather
 * than caching its result — see `mcpBridge.ts` — so a token rotated via
 * `probe mcp token set` (or a device re-paired) is picked up without
 * restarting the session, matching Claude Code's behaviour.
 */
export function resolveMcpBearerToken(env: PathEnv = process.env): McpBearerToken | null {
  const envToken = env[MCP_TOKEN_ENV];
  if (envToken && envToken.trim()) {
    return { token: envToken.trim(), source: "env", detail: MCP_TOKEN_ENV };
  }

  const cliToken = readProbeConfigMcpToken(env);
  if (cliToken) {
    return { token: cliToken, source: "probe-cli", detail: probeConfigPath(env) };
  }

  return null;
}
