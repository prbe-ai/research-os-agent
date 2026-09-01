/**
 * "Is there a token to authenticate with" — the same question
 * `hooks/session-start.sh` asks (lines ~99-147) before it will spawn anything,
 * and the same precedence `tap/config.py`'s `load_token()` uses at daemon
 * start. Reimplemented here (not shelled out to Python) so the gate is a
 * synchronous, dependency-free file read: deciding whether to spawn must not
 * itself require finding a Python interpreter or the tap package first.
 *
 * Precedence, per config.py's docstring: plugin-local `.token` (written by
 * `python -m tap pair`) > `PROBE_PI_TAP_TOKEN` env > the probe CLI's
 * config.json `ingest_token` (v1 flat, or v2 named-contexts — the CLI writes
 * v2 as of the workspace-context pass, so both shapes must be read or capture
 * silently stops the first time the user runs any command that resaves the
 * config file).
 *
 * Deliberately narrower than `tap/status.py`: this answers ONLY "is a token
 * configured," matching session-start.sh's own scope exactly. It does not
 * resolve the backend base URL or contact the network — `tap watch` already
 * self-heals a missing base URL (touches its own shutdown sentinel and exits
 * cleanly), so duplicating that check here would be a second copy of logic
 * that can drift, not an extra safety margin.
 */

import { readFileSync } from "node:fs";

import { probeConfigPath, tokenFile, TOKEN_ENV, type PathEnv } from "./paths.js";

export type PairingResult =
  | { paired: true; source: "device-token" | "env" | "probe-cli"; detail: string }
  | { paired: false; reason: string };

function readTrimmed(path: string): string | null {
  try {
    const text = readFileSync(path, "utf-8").trim();
    return text.length > 0 ? text : null;
  } catch {
    return null;
  }
}

/** The probe CLI config file, flattened to one dict whatever the file shape (v1 or v2). */
function readProbeConfigIngestToken(env: PathEnv): string | null {
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
    const contextsMap = contexts as Record<string, unknown>;
    const current =
      typeof flat.current_context === "string" && flat.current_context ? flat.current_context : "default";
    const active = contextsMap[current];
    flat = typeof active === "object" && active !== null && !Array.isArray(active) ? (active as Record<string, unknown>) : {};
  }
  const tok = flat.ingest_token;
  return typeof tok === "string" && tok.trim() ? tok.trim() : null;
}

export function checkPairing(env: PathEnv = process.env): PairingResult {
  const devicePath = tokenFile(env);
  const deviceToken = readTrimmed(devicePath);
  if (deviceToken) {
    return { paired: true, source: "device-token", detail: devicePath };
  }

  const envToken = env[TOKEN_ENV];
  if (envToken && envToken.trim()) {
    return { paired: true, source: "env", detail: TOKEN_ENV };
  }

  const cliToken = readProbeConfigIngestToken(env);
  if (cliToken) {
    return { paired: true, source: "probe-cli", detail: probeConfigPath(env) };
  }

  return {
    paired: false,
    reason:
      `probe-research-pi: not paired — no device token at ${devicePath}, ` +
      `${TOKEN_ENV} is unset, and ${probeConfigPath(env)} carries no ingest_token. ` +
      "Pair this device (see the probe-research-pi README) or run `probe login`; skipping capture for this session.",
  };
}
