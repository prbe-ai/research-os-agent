/**
 * "Is there a token to authenticate with" — the same question
 * `hooks/session-start.sh` asks before it will spawn anything, and the same
 * precedence `tap/config.py`'s `load_token()` uses at daemon start for pi.
 * Reimplemented here (not shelled out to Python) so the gate is a synchronous,
 * dependency-free file read: deciding whether to spawn must not itself require
 * finding a Python interpreter or the tap package first.
 *
 * Precedence: plugin-local `.token` (written by `python -m tap pair`) >
 * `PROBE_PI_TAP_TOKEN` env. Nothing else: the probe CLI config's
 * `ingest_token` is the capture token minted for Claude Code, and the server
 * refuses a paired token on any other agent's route, so falling back to it only
 * ever produced a 403 on every upload (decision D3, 2026-09-09).
 *
 * Deliberately narrower than `tap/status.py`: this answers ONLY "is a token
 * configured". It does not resolve the backend base URL or contact the network
 * — `tap watch` already self-heals a missing base URL.
 */

import { readFileSync } from "node:fs";

import { tokenFile, TOKEN_ENV, type PathEnv } from "./paths.js";
import { WIZARD_HINT } from "./wizardHint.js";

export type PairingResult =
  | { paired: true; source: "device-token" | "env"; detail: string }
  | { paired: false; reason: string };

function readTrimmed(path: string): string | null {
  try {
    const text = readFileSync(path, "utf-8").trim();
    return text.length > 0 ? text : null;
  } catch {
    return null;
  }
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

  return {
    paired: false,
    reason:
      `probe-research-pi: not paired — no device token at ${devicePath}, ` +
      `and ${TOKEN_ENV} is unset. ` +
      `Pair this device (see the probe-research-pi README) or ${WIZARD_HINT}; skipping capture for this session.`,
  };
}
