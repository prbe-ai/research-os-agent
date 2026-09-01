/**
 * Builds the text for the `/probe-status` command — "is capture on for this
 * session, and if not, why."
 *
 * Deliberately does not reimplement `python -m tap status`'s reporting
 * (delivery counts, outbox size, last-401 halt state — see
 * `tap/status.py`): that lives in a SQLite file this package has no
 * dependency to read, and duplicating its precedence logic a third time
 * (bash session-start.sh, config.py, and now this) is exactly the kind of
 * drift the paths/pairing modules' docstrings warn against. This reports
 * what is cheaply and reliably knowable from TypeScript alone — is a token
 * configured, is a daemon alive for the current session, where its log is —
 * and points at the fuller Python tool for the rest.
 */

import { detectAdapterHandoff, MCP_SERVED_VIA_ADAPTER_MESSAGE } from "./adapterHandoff.js";
import { isDaemonAlive } from "./daemon.js";
import { checkPairing } from "./pairing.js";
import { pidFile, pluginDir, sessionLogFile, type PathEnv } from "./paths.js";
import { resolveTapRuntime, type TapRuntimeDeps } from "./tapRuntime.js";

export interface StatusDeps {
  existsSync: (path: string) => boolean;
  isExecutable: (path: string) => boolean;
  readFileSync: (path: string) => string;
  kill: (pid: number, signal: number | string) => void;
  env: PathEnv;
  extensionDir: string;
  /** The session's cwd — needed for D6's project-scope settings.json read. */
  cwd: string;
}

export function buildStatusReport(sessionId: string | undefined, deps: StatusDeps): string {
  const lines: string[] = ["probe-research-pi capture status"];

  const pairing = checkPairing(deps.env);
  if (pairing.paired) {
    lines.push(`  paired:        yes (${pairing.source}: ${pairing.detail})`);
  } else {
    lines.push("  paired:        no");
    lines.push(`    ${pairing.reason}`);
  }

  // Probe MCP read tools: a separate subsystem from capture pairing above
  // (see extension.ts's own comment on the same split). D6: when
  // pi-mcp-adapter already owns the server (adapterHandoff.ts), this
  // extension's direct bridge stands down — say so plainly here rather than
  // reporting an MCP connection that was never attempted as if it failed.
  const handoff = detectAdapterHandoff({ env: deps.env, cwd: deps.cwd, packageRoot: deps.extensionDir });
  if (handoff.standDown) {
    lines.push(`  mcp:           ${MCP_SERVED_VIA_ADAPTER_MESSAGE}`);
  } else {
    lines.push("  mcp:           bridged directly by this extension (see /probe-mcp-login)");
  }

  const runtimeDeps: TapRuntimeDeps = {
    existsSync: deps.existsSync,
    isExecutable: deps.isExecutable,
    env: deps.env,
    extensionDir: deps.extensionDir,
  };
  const runtime = resolveTapRuntime(runtimeDeps);
  if (runtime) {
    lines.push(`  interpreter:   ${runtime.python}${runtime.tapRoot ? ` (tap root: ${runtime.tapRoot})` : " (assumed pip-installed)"}`);
  } else {
    lines.push("  interpreter:   not found — no python3/python on PATH");
  }

  lines.push(`  state dir:     ${pluginDir(deps.env)}`);

  if (sessionId) {
    const alive = isDaemonAlive(sessionId, deps);
    lines.push(`  this session:  ${alive ? "capturing" : "not capturing"}`);
    lines.push(`    pid file:    ${pidFile(sessionId)}`);
    lines.push(`    log:         ${sessionLogFile(sessionId, deps.env)}`);
  } else {
    lines.push("  this session:  no session file yet (nothing to capture)");
  }

  lines.push("  for delivery/outbox detail, run: PROBE_TAP_SOURCE=pi python3 -m tap status");
  return lines.join("\n");
}
