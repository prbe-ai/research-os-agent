/**
 * pi's tool guard, asked of the Probe service first (plan D38).
 *
 * Claude Code runs Probe's guard as a PreToolUse hook: `tracking_guard.py` and
 * its siblings, the routes of the plugin folder's pre-tool lane. pi has no
 * hooks, so `guard.ts` carries a port of them. Here the call goes to the real
 * ones instead: the service runs the pre-tool routes of the Probe plugin
 * folder beside this package (`hooks.run`, `core/service.ts`), and a refusal
 * of theirs is pi's, in their words. `guard.ts` stays, word for word, and runs
 * on every call as the floor (`extension.ts`): whenever the service gives no
 * clear answer in SERVICE_HOOK_WAIT_MS (none running, an older one, out of
 * time, a route that exited non-zero), pi's own guard decides exactly as it
 * did before. A service that REFUSES this caller (it cannot rule out a sandbox
 * around pi) is no such case: the same routes then run here
 * (`runHookLocally`, the folder's dispatcher in place, within the same time),
 * so the Python-only rules still hold. And since the Python guard fails OPEN (a route that broke
 * exits 0 with nothing to say), an allow from the service never overrides a
 * refusal of `guard.ts`'s. The service can only add refusals: the Python
 * rules `guard.ts` has not ported (a `probe` write in a `read` session, the
 * `.probe.config` question held before the first Probe call).
 *
 * THE FOLDER is the one this package ships beside, in both install shapes (the
 * public mirror pi installs, and this monorepo): `../probe-research` in the
 * agent profile, `../probe-research-daemon` (its routes export
 * PROBE_PLUGIN_PROFILE=daemon) in the daemon profile. The same commit as this
 * extension, so the routes that answer are this release's.
 *
 * THE MAPPING, pi's tool call -> Claude Code's PreToolUse payload
 * (`session_id`, `transcript_path`, `cwd`, `hook_event_name`, `tool_name`,
 * `tool_input`, `tool_use_id`), for the tools `guard.ts` reads and nothing else
 * (every other tool goes to no route, so it never leaves pi):
 *
 *   bash {command}            -> Bash {command}
 *   powershell {command}      -> Bash {command}  (`guard.ts` reads both as one
 *                                shell line; the Python guard reads `Bash` only)
 *   write {path, content}     -> Write {file_path, content}
 *   edit {path, edits[1]}     -> Edit {file_path, old_string, new_string}
 *   edit {path, edits[2+]}    -> MultiEdit {file_path, edits: [{old_string, new_string}]}
 *   a Probe MCP tool          -> its own name when routes.json's matcher
 *                                (`probe[-_]research`, case as written) takes it,
 *                                else `mcp__probe-research__<tool>`, the name
 *                                Claude Code gives Probe's own MCP tools
 *
 * `file_path` is the path pi will write, resolved as pi resolves it
 * (`resolveLikePi`: `@` stripped, `~` and `file://` expanded, relative to cwd):
 * the Python resolves only `~` and cwd, so the raw spelling would be a way past
 * it. A refusal names the tool as Claude Code's (`Write`); `piReason` gives it
 * back pi's name (`write`).
 */

import { existsSync } from "node:fs";
import { join, resolve } from "node:path";

import {
  boundedHookEnv,
  HookLane,
  isRefused,
  refusedForSandbox,
  preToolVerdict,
  runHook,
  runHookLocally,
  SERVICE_HOOK_RUN_MS,
  SERVICE_HOOK_WAIT_MS,
  ToolVerdict,
  type HookClientDeps,
  type HookRun,
  type HookRunRefused,
  type HookRunResult,
  type LocalHookDeps,
} from "./core/service.js";
import type { PathEnv } from "./core/paths.js";
import { PiTool, resolveLikePi } from "./guard.js";
import { TOOL_NAME_PREFIX } from "./mcpSchema.js";
import { isProbeMcpTool } from "./profile.js";

/** Claude Code's names for the tools pi's guard reads. */
export const ClaudeTool = { Bash: "Bash", Write: "Write", Edit: "Edit", MultiEdit: "MultiEdit" } as const;
/** The event the pre-tool lane serves (`protocol.HOOK_LANES`). */
export const PRE_TOOL_USE = "PreToolUse";
/** The name Claude Code gives Probe's own MCP tools, which the routes take. */
export const CLAUDE_PROBE_MCP_PREFIX = "mcp__probe-research__";
/** routes.json's matcher for Probe MCP tools, case as written: the harness's regex search is case-sensitive. */
const ROUTED_MCP = /probe[-_]research/;
/** The Probe plugin folders beside this package, by profile. */
export const PluginFolder = { Agent: "probe-research", Daemon: "probe-research-daemon" } as const;

export interface ClaudeToolCall {
  toolName: string;
  toolInput: Record<string, unknown>;
}

function claudeEdits(edits: unknown): { old_string: unknown; new_string: unknown }[] {
  if (!Array.isArray(edits)) return [];
  return edits
    .filter((edit): edit is Record<string, unknown> => typeof edit === "object" && edit !== null)
    .map((edit) => ({ old_string: edit.oldText, new_string: edit.newText }));
}

/** pi's tool call as Claude Code's PreToolUse `tool_name` and `tool_input` (see the mapping above), or null for a tool the guard does not read. */
export function claudeToolCall(toolName: string, input: unknown, cwd: string): ClaudeToolCall | null {
  const fields = typeof input === "object" && input !== null ? (input as Record<string, unknown>) : {};
  switch (toolName) {
    case PiTool.Bash:
    case PiTool.PowerShell:
      return typeof fields.command === "string" ? { toolName: ClaudeTool.Bash, toolInput: { command: fields.command } } : null;
    case PiTool.Write:
      if (typeof fields.path !== "string" || !fields.path) return null;
      return { toolName: ClaudeTool.Write, toolInput: { file_path: resolveLikePi(fields.path, cwd), content: fields.content } };
    case PiTool.Edit: {
      if (typeof fields.path !== "string" || !fields.path) return null;
      const filePath = resolveLikePi(fields.path, cwd);
      const edits = claudeEdits(fields.edits);
      if (edits.length === 1) {
        return { toolName: ClaudeTool.Edit, toolInput: { file_path: filePath, ...edits[0] } };
      }
      return { toolName: ClaudeTool.MultiEdit, toolInput: { file_path: filePath, edits } };
    }
    default:
      if (!isProbeMcpTool(toolName)) return null;
      return {
        toolName: ROUTED_MCP.test(toolName)
          ? toolName
          : CLAUDE_PROBE_MCP_PREFIX + (toolName.startsWith(TOOL_NAME_PREFIX) ? toolName.slice(TOOL_NAME_PREFIX.length) : toolName),
        toolInput: fields,
      };
  }
}

/** A refusal in pi's words: the Claude Code tool name it quotes (`` `Write` ``) given back as pi's (`` `write` ``). */
export function piReason(reason: string, call: ClaudeToolCall, piToolName: string): string {
  return call.toolName === piToolName ? reason : reason.split("`" + call.toolName + "`").join("`" + piToolName + "`");
}

/** The Probe plugin folder whose routes answer for this profile, or null when it is not beside this package. */
export function pluginRoot(extensionDir: string, daemonProfile: boolean, exists: (path: string) => boolean = existsSync): string | null {
  const root = resolve(extensionDir, "..", daemonProfile ? PluginFolder.Daemon : PluginFolder.Agent);
  return exists(join(root, "hooks", "dispatch.py")) ? root : null;
}

export interface ServiceGuardCall {
  toolName: string;
  input: unknown;
  toolCallId?: string;
  sessionId?: string;
  transcriptPath?: string;
  cwd: string;
  daemonProfile: boolean;
  extensionDir: string;
  env: PathEnv;
  /** The service's socket (`lookupServiceSocket`), or null while unknown. */
  socket: string | null;
}

export interface ServiceGuardDeps extends HookClientDeps, LocalHookDeps {
  exists?: (path: string) => boolean;
  /** Defaults to `runHook`; tests stand in for the service. */
  run?: (socket: string, run: HookRun, deps: HookClientDeps) => Promise<HookRunResult | HookRunRefused | null>;
  /** Defaults to `runHookLocally`: the lane run here when the service refuses. */
  runLocally?: (run: HookRun, deps: LocalHookDeps) => Promise<HookRunResult | null>;
}

/** The service's answer: `reason` null lets the call run. */
export interface ServiceGuardAnswer {
  reason: string | null;
  via: string | null;
}

/** Claude Code's PreToolUse payload for this call. */
export function preToolUsePayload(call: ClaudeToolCall, at: ServiceGuardCall): Record<string, unknown> {
  return {
    session_id: at.sessionId,
    ...(at.transcriptPath ? { transcript_path: at.transcriptPath } : {}),
    cwd: at.cwd,
    hook_event_name: PRE_TOOL_USE,
    tool_name: call.toolName,
    tool_input: call.toolInput,
    ...(at.toolCallId ? { tool_use_id: at.toolCallId } : {}),
  };
}

/**
 * Ask the service's pre-tool routes about this call. Null whenever they give
 * no clear answer (or cannot be asked: no socket yet, no session id, which the
 * Python guard needs, a tool it does not read, no plugin folder beside this
 * package): the caller then runs `guard.ts`. Never rejects.
 */
export async function guardViaService(at: ServiceGuardCall, deps: ServiceGuardDeps): Promise<ServiceGuardAnswer | null> {
  try {
    if (!at.socket || !at.sessionId) return null;
    // Can throw (a `file://` path pi itself would refuse): no answer, then.
    const call = claudeToolCall(at.toolName, at.input, at.cwd);
    if (call === null) return null;
    const root = pluginRoot(at.extensionDir, at.daemonProfile, deps.exists);
    if (root === null) return null;
    const request: HookRun = {
      pluginRoot: root,
      lane: HookLane.PreTool,
      payload: preToolUsePayload(call, at),
      env: boundedHookEnv(at.env),
      cwd: resolve(at.cwd),
      timeoutMs: SERVICE_HOOK_RUN_MS,
    };
    const began = Date.now();
    const asked = await (deps.run ?? runHook)(at.socket, request, deps);
    let result: HookRunResult | null;
    if (isRefused(asked)) {
      // Refused for pi's confinement (the service cannot rule out a sandbox
      // around it): the same routes run here, as Claude Code runs a hook no
      // host takes -- never just pi's own port, which lacks the Python-only
      // rules -- within what is left of the tool call's wait (D28). Any other
      // refusal (busy, stopping) leaves it to pi's own guard, as before.
      const left = Math.min(SERVICE_HOOK_RUN_MS, (deps.waitMs ?? SERVICE_HOOK_WAIT_MS) - (Date.now() - began));
      result = refusedForSandbox(asked) && left > 0
        ? await (deps.runLocally ?? runHookLocally)({ ...request, timeoutMs: left }, deps)
        : null;
    } else {
      result = asked;
    }
    const verdict = preToolVerdict(result);
    if (verdict === null) return null;
    return {
      reason: verdict.verdict === ToolVerdict.Block ? piReason(verdict.reason, call, at.toolName) : null,
      via: result?.via ?? null,
    };
  } catch {
    return null;
  }
}
