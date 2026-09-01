/**
 * Wiring layer: registers the session_start / session_shutdown handlers and
 * the /probe-status command against a real pi ExtensionAPI. Everything with
 * actual logic (pairing, runtime resolution, spawning, stopping) lives in
 * sibling modules and is unit-tested directly with injected fakes; this file
 * is the thin, mostly-untested seam that binds them to real fs/child_process
 * and to pi's event bus. `index.ts` calls `registerExtension(pi, __dirname)`.
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import { constants as fsConstants } from "node:fs";
import { dirname } from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { detectAdapterHandoff, MCP_SERVED_VIA_ADAPTER_MESSAGE } from "./adapterHandoff.js";
import { pruneStaleShutdownSentinels, spawnDaemon, stopDaemon, waitForSpawnConfirmation, type DaemonDeps, type SpawnFn } from "./daemon.js";
import { connectAndRegisterTools, defaultMcpBridgeDeps, interactiveOAuthLogin, type ConnectResult } from "./mcpBridge.js";
import { disabledFile, extensionLogFile, teamNoteDocumentPath } from "./paths.js";
import { checkPairing } from "./pairing.js";
import { buildStatusReport } from "./status.js";
import { resolveTapRuntime, type TapRuntimeDeps } from "./tapRuntime.js";
import { readTeamNote, renderTeamNoteForPrompt, spawnTeamNoteSync, type TeamNoteSyncDeps } from "./teamNote.js";

// Module-scope, NOT inside registerExtension: pi tears down and rebuilds the
// extension runtime on /reload and on session switches (new/resume/fork),
// re-invoking this module's exports each time — but Node keeps this file's
// top-level state alive across those re-invocations within one process
// (module cache keyed by resolved path). That's what makes the same-session
// double-spawn guard, and knowing which session's daemon to stop in the
// session_shutdown handler, survive a reload instead of resetting on every
// one. The real dedup authority is still the pidfile check inside
// spawnDaemon()/isDaemonAlive() — this set is a same-process fast path that
// also closes the narrow race where session_start could fire twice before a
// freshly-spawned wrapper has written its own pidfile.
const spawnedSessionIds = new Set<string>();

// The team-note cache. Module-scope for the same reason spawnedSessionIds is:
// pi rebuilds the extension runtime on /reload and session switches, and this
// must survive that (a reload should not blank out the note mid-session).
// Populated ONCE per real session_start by readTeamNote() below, read by
// EVERY before_agent_start of that session, and never refreshed mid-session —
// see teamNote.ts's module docstring ("cache once per session") for why.
let cachedTeamNote: string | null = null;

// Live Probe MCP connections, keyed by session id. Module-scope for the same
// reload-survives-reasoning as spawnedSessionIds/cachedTeamNote — but unlike
// those two, THIS one is read as well as written on every session_start: pi
// rebuilds the extension's tool registry from scratch on every reload
// (verified against loader.js — a fresh `extension.tools` Map per load), so
// a session_start firing with a sessionId already in this map means
// "re-register the tools we already fetched against the new registry," not
// "skip, already done" — otherwise a session's Probe tools would silently
// disappear after /reload. A session ending removes its own entry.
const mcpConnections = new Map<string, Extract<ConnectResult, { registered: number }>>();

function isExecutable(path: string): boolean {
  try {
    fs.accessSync(path, fsConstants.X_OK);
    return true;
  } catch {
    return false;
  }
}

const realSpawn: SpawnFn = (command, args, options) =>
  spawn(command, args, options) as unknown as { pid: number | undefined; unref: () => void };

function logLine(message: string): void {
  try {
    fs.mkdirSync(dirname(extensionLogFile()), { recursive: true });
    fs.appendFileSync(extensionLogFile(), `[${new Date().toISOString()}] ${message}\n`);
  } catch {
    // Best-effort; losing an extension log line must never break capture.
  }
}

function realDaemonDeps(): DaemonDeps {
  return {
    spawn: realSpawn,
    existsSync: fs.existsSync,
    mkdirSync: (path) => fs.mkdirSync(path, { recursive: true }),
    readFileSync: (path) => fs.readFileSync(path, "utf-8"),
    rmSync: (path) => fs.rmSync(path, { force: true }),
    writeFileSync: (path, content) => fs.writeFileSync(path, content),
    kill: (pid, signal) => process.kill(pid, signal),
    log: logLine,
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  };
}

function realTeamNoteSyncDeps(): TeamNoteSyncDeps {
  return {
    spawn: realSpawn,
    existsSync: fs.existsSync,
    isExecutable,
    env: process.env,
    log: logLine,
  };
}

/** One clear message, on stderr (never stdout — `--mode json` reserves stdout for structured
 * output) plus the extension log, plus a UI toast when a UI exists to show one. */
function announce(ctx: { hasUI: boolean; ui: { notify: (msg: string, level?: "info" | "warning" | "error") => void } }, message: string, level: "info" | "warning" | "error" = "warning"): void {
  process.stderr.write(`probe-research-pi: ${message}\n`);
  logLine(message);
  if (ctx.hasUI) ctx.ui.notify(message, level);
}

export function registerExtension(pi: ExtensionAPI, extensionDir: string): void {
  pi.on("session_start", async (event, ctx) => {
    // Team note: refreshes the cache for EVERY session_start reason,
    // including "reload" — a sync since the last read may have refreshed the
    // file on disk, and this is cheap (one local file read, never a network
    // call). Independent of the capture-daemon logic below on purpose: a
    // session with no transcript file, no pairing, or the killswitch active
    // should still see the team note. readTeamNote() never throws.
    cachedTeamNote = readTeamNote(process.env);

    const sessionId = ctx.sessionManager.getSessionId();
    const transcriptPath = ctx.sessionManager.getSessionFile();

    // Probe MCP read tools: entirely independent of the capture daemon below
    // (a separate credential — mcp_token, never the tap's ingest/device
    // token — and no transcript file is needed to register tools), so this
    // runs unconditionally, before the no-transcript-file early return and
    // before the killswitch/pairing checks that gate capture only. Bounded
    // internally (see mcpBridge.ts) so an unreachable server cannot stall
    // this handler; any failure degrades to a single announce(), never a
    // thrown error out of session_start.
    //
    // D6 stand-down check FIRST, before touching mcpConnections at all: when
    // pi-mcp-adapter already owns the Probe MCP server (see
    // adapterHandoff.ts), connecting our own bridge on top would register
    // every tool twice. Re-run on EVERY session_start (cheap: two local file
    // reads, no network) rather than caching — a `packages` entry can change
    // between sessions (an adapter or our own package installed/removed) and
    // this must track that without a restart. Gating the whole block —
    // including the reregister branch — behind `!standDown` is also what
    // keeps `/reload` well-behaved while stood down: mcpConnections never
    // gets an entry for this session in the first place, so there is no
    // stale connection for a later reload's `existingMcpConnection` check to
    // trip on.
    const handoff = detectAdapterHandoff({ env: process.env, cwd: ctx.cwd, packageRoot: extensionDir });
    if (handoff.standDown) {
      logLine(`session_start(${event.reason}): ${MCP_SERVED_VIA_ADAPTER_MESSAGE} — ${handoff.reason}`);
    } else {
      const existingMcpConnection = mcpConnections.get(sessionId);
      if (existingMcpConnection) {
        // Same session, extension reloaded (or session_start fired again for
        // some other reason) — re-register against the fresh tool registry
        // with no network call. See mcpConnections' own comment above.
        const registered = existingMcpConnection.reregister(pi);
        logLine(`session_start(${event.reason}): Probe MCP re-registered ${registered} tool(s) for ${sessionId}`);
      } else {
        try {
          const result = await connectAndRegisterTools(pi, defaultMcpBridgeDeps(process.env, (message, level) => announce(ctx, message, level), logLine));
          if ("registered" in result) {
            mcpConnections.set(sessionId, result);
            logLine(`session_start(${event.reason}): Probe MCP ready, ${result.registered} tool(s) registered for ${sessionId}`);
          } else {
            logLine(`session_start(${event.reason}): Probe MCP unavailable for ${sessionId}: ${result.skipped}`);
          }
        } catch (err) {
          // connectAndRegisterTools degrades internally and should never throw
          // — this catch exists only so a bug there can never take capture or
          // the team note down with it.
          logLine(`session_start(${event.reason}): Probe MCP bridge threw unexpectedly: ${err instanceof Error ? err.message : String(err)}`);
        }
      }
    }

    if (!transcriptPath) {
      // In-memory / not-yet-persisted session (e.g. SessionManager.inMemory()) — no
      // JSONL file exists yet for the daemon to tail. Nothing to do.
      logLine(`session_start(${event.reason}): no session file for ${sessionId}, skipping`);
      return;
    }

    // Best-effort hygiene, every session start, matching session-start.sh:
    // never allowed to fail this handler.
    try {
      pruneStaleShutdownSentinels({
        readdirSync: (dir) => fs.readdirSync(dir),
        statMtimeMs: (path) => fs.statSync(path).mtimeMs,
        rmSync: (path) => fs.rmSync(path, { force: true }),
        now: () => Date.now(),
      });
    } catch {
      // Never let hygiene block capture.
    }

    // Killswitch: presence of .disabled disables the daemon entirely — checked
    // FIRST, same order as session-start.sh, and silently (log file only, no
    // stderr/notify): the user turned capture off on purpose, so re-announcing
    // that on every single session start would just be noise.
    if (fs.existsSync(disabledFile(process.env))) {
      logLine(`session_start(${event.reason}): killswitch active, skipping ${sessionId}`);
      return;
    }

    if (spawnedSessionIds.has(sessionId)) {
      return;
    }

    const pairing = checkPairing(process.env);
    if (!pairing.paired) {
      announce(ctx, pairing.reason, "warning");
      return;
    }

    const runtimeDeps: TapRuntimeDeps = {
      existsSync: fs.existsSync,
      isExecutable,
      env: process.env,
      extensionDir,
    };
    const runtime = resolveTapRuntime(runtimeDeps);
    if (!runtime) {
      announce(
        ctx,
        "no python3 interpreter found for the probe-research-tap daemon — capture disabled for this session. " +
          "Install Python 3.11+ and ensure `python3` is on PATH, or set PROBE_PI_TAP_ROOT.",
        "error",
      );
      return;
    }

    const deps = realDaemonDeps();
    const result = spawnDaemon({ sessionId, transcriptPath, cwd: ctx.cwd, runtime }, deps);
    // Marked SYNCHRONOUSLY, with no await between the spawn decision and this
    // line: spawnedSessionIds is the same-process fast path (see its own
    // comment above), and the whole point of it is to close before
    // waitForSpawnConfirmation's multi-second wait below even starts — a
    // second session_start racing in during that wait must see this set
    // already updated, not find the window still open.
    spawnedSessionIds.add(sessionId);
    if (result.spawned) {
      logLine(`session_start(${event.reason}): spawned capture for ${sessionId} (pid ${result.pid ?? "unknown"})`);
      await waitForSpawnConfirmation(sessionId, deps);
    } else {
      logLine(`session_start(${event.reason}): capture already running for ${sessionId}`);
    }
  });

  pi.on("session_shutdown", async (event, ctx) => {
    if (event.reason === "reload") {
      // Extensions are reloading, not the session — the SAME session id fires
      // session_start again right after this. Leave the daemon running:
      // stopping it here would spuriously FINALIZE a session that isn't
      // actually ending (main.py's shutdown path always enqueues a FINALIZE,
      // which is what triggers server-side knowledge-unit extraction). Leave
      // the Probe MCP connection open for the same reason — the imminent
      // session_start will find it in mcpConnections and re-register against
      // the fresh registry rather than reconnecting.
      return;
    }
    const sessionId = ctx.sessionManager.getSessionId();
    stopDaemon(sessionId, realDaemonDeps());
    spawnedSessionIds.delete(sessionId);
    const mcpConnection = mcpConnections.get(sessionId);
    if (mcpConnection) {
      mcpConnections.delete(sessionId);
      // Best-effort: closing the MCP client (an SSE stream + HTTP session)
      // must never block or fail this handler — a hung close() would stall
      // shutdown, and a failed one is inert (the process is ending anyway).
      mcpConnection.close().catch((err) => logLine(`session_shutdown(${event.reason}): Probe MCP close failed: ${err instanceof Error ? err.message : String(err)}`));
    }
    logLine(`session_shutdown(${event.reason}): stopped capture for ${sessionId}`);
  });

  // Inject, don't render — see teamNote.ts's module docstring. Fires on
  // EVERY turn, so this must stay a cheap string append against the cache
  // populated at session_start: no file read, no CLI spawn, here.
  pi.on("before_agent_start", async (event) => {
    if (!cachedTeamNote) return;
    return {
      systemPrompt: event.systemPrompt + renderTeamNoteForPrompt(cachedTeamNote, teamNoteDocumentPath(process.env)),
    };
  });

  // agent_settled, NOT turn_end — agent_settled is pi's analogue of Claude
  // Code's Stop (fires once an agent run has fully settled, no automatic
  // retry/compaction/continuation pending); turn_end fires several times per
  // user message and would push/pull far more than needed. See teamNote.ts's
  // module docstring for why this is a full sync, detached, and fail-open.
  pi.on("agent_settled", async () => {
    spawnTeamNoteSync(realTeamNoteSyncDeps());
  });

  pi.registerCommand("probe-status", {
    description: "Show Probe Research session-capture status",
    handler: async (_args, ctx) => {
      const sessionId = ctx.sessionManager.getSessionId();
      const report = buildStatusReport(sessionId, {
        existsSync: fs.existsSync,
        isExecutable,
        readFileSync: (path) => fs.readFileSync(path, "utf-8"),
        kill: (pid, signal) => process.kill(pid, signal),
        env: process.env,
        extensionDir,
        cwd: ctx.cwd,
      });
      logLine("probe-status invoked");
      if (ctx.hasUI) {
        ctx.ui.notify(report, "info");
      }
      // Always also go to stderr: notify() is a silent no-op in headless
      // modes (print/json), and this command is meaningless without SOME
      // visible output.
      process.stderr.write(report + "\n");
    },
  });

  pi.registerCommand("probe-mcp-login", {
    description: "Connect Probe Research's read MCP tools — bearer token first, interactive OAuth login otherwise",
    handler: async (_args, ctx) => {
      const sessionId = ctx.sessionManager.getSessionId();
      const existing = mcpConnections.get(sessionId);
      if (existing) {
        // Already connected this session (session_start's own attempt, or an
        // earlier /probe-mcp-login) — report that rather than opening a
        // SECOND, untracked connection interactiveOAuthLogin() would leak
        // (it always calls connectAndRegisterTools() fresh; that path has no
        // reason to know about this session's cached entry).
        const message = `Probe MCP is already connected — ${existing.registered} tool(s) registered.`;
        logLine(`probe-mcp-login(connected): ${message}`);
        announce(ctx, message, "info");
        return;
      }
      // Known, accepted gap: a FRESH sign-in done here registers tools
      // straight against `pi` for this session immediately, but is not
      // cached in mcpConnections (interactiveOAuthLogin() does not hand back
      // a close()/reregister() pair), so a later /reload reconnects once
      // more over the network (via the freshly-saved OAuth tokens) instead
      // of a free in-memory re-register — a minor latency cost, not a
      // correctness one, and the existing-connection check above at least
      // stops a second `/probe-mcp-login` in the same session from doubling
      // it.
      const result = await interactiveOAuthLogin(
        pi,
        defaultMcpBridgeDeps(process.env, (message, level) => announce(ctx, message, level), logLine),
        { hasUI: ctx.hasUI, input: (title, placeholder) => ctx.ui.input(title, placeholder) },
      );
      logLine(`probe-mcp-login(${result.status}): ${result.message}`);
      const level = result.status === "failed" ? "error" : result.status === "requires-interactive" ? "warning" : "info";
      announce(ctx, result.message, level);
    },
  });
}
