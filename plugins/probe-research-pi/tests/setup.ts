/**
 * Runs before every test file (vitest `setupFiles`). Points every home and
 * config root at one throwaway directory and drops every Probe credential the
 * developer's shell might carry, so no test can read the real
 * `~/.config/probe/config.json` (a live `mcp_token` there silently sent the
 * OAuth tests down the bearer path) or write into a real `~/.pi`, `~/.codex`
 * or `~/.claude`. Fix the class here, never in `src/`.
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll } from "vitest";

const root = mkdtempSync(join(tmpdir(), "probe-pi-tests-"));
// One root per test file; without this every run left one per file in /tmp
// (540 of them on the devbox by 2026-10-02).
afterAll(() => rmSync(root, { recursive: true, force: true }));

export const TEST_HOME = root;

process.env.HOME = root;
process.env.USERPROFILE = root;
process.env.XDG_CONFIG_HOME = join(root, ".config");
process.env.XDG_STATE_HOME = join(root, ".local", "state");
process.env.PI_CODING_AGENT_DIR = join(root, ".pi", "agent");
process.env.CODEX_HOME = join(root, ".codex");
process.env.CLAUDE_CONFIG_DIR = join(root, ".claude");
for (const name of [
  "PROBE_MCP_TOKEN",
  "PROBE_INGEST_TOKEN",
  "PROBE_PI_TAP_TOKEN",
  "PROBE_PI_TAP_PLUGIN_DIR",
  "PROBE_CONFIG_PATH",
  "PROBE_AGENT",
  "PROBE_SESSION_STATE",
  "PROBE_SESSION_TRACKING",
  "PROBE_BASE_URL",
  "PI_SESSION_ID",
  "PI_CODING_AGENT",
]) {
  delete process.env[name];
}
