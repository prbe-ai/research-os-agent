/**
 * This computer's WORKSPACE LIMIT (`probe workspace limit`): a client-side
 * limiter on what Probe reads, never auth. The computer stores "limit to
 * workspace W" beside its filing pin in the probe CLI config
 * (`contexts.<ctx>.workspace = {id, limit: true, team_items: "show"|"hide", ...}`);
 * every Probe MCP connection sends it, and the hosted MCP forwards it on its API
 * calls, which narrow to W.
 *
 * Mirrors `probe.sdk.workspace_limit.resolve_limit` and
 * `agent/plugins/probe-research/bin/probe-mcp-headers` (tests/test_pi_core_parity.py):
 * `PROBE_WORKSPACE_LIMIT` first (`team` = none), else the active context's limit;
 * `PROBE_WORKSPACE_LIMIT_TEAM_ITEMS` overrides the team-items choice of either.
 *
 * FAIL CLOSED. pi cannot look an environment slug up, so a value it cannot vouch
 * for goes as found when it is header-safe, else as `unreadable`: the API refuses
 * it (400 `invalid_workspace_limit`) instead of the MCP reading the whole team.
 */

import { readActiveProbeContext } from "./mcpAuth.js";
import type { PathEnv } from "./paths.js";

export const WORKSPACE_LIMIT_HEADER = "X-Probe-Workspace-Limit";
export const WORKSPACE_LIMIT_TEAM_ITEMS_HEADER = "X-Probe-Workspace-Limit-Team-Items";
export const WORKSPACE_LIMIT_ENV = "PROBE_WORKSPACE_LIMIT";
export const WORKSPACE_LIMIT_TEAM_ITEMS_ENV = "PROBE_WORKSPACE_LIMIT_TEAM_ITEMS";
/** `PROBE_WORKSPACE_LIMIT=team`: this process sees the whole team. */
export const WORKSPACE_LIMIT_TEAM_WORD = "team";
/** What a value pi cannot vouch for is sent as (the API refuses it). */
export const UNREADABLE_LIMIT = "unreadable";

/** Team-wide items under a limit: the team note, the All workspaces summary, GitHub docs. */
export const TeamItems = {
  Show: "show",
  Hide: "hide",
} as const;
export type TeamItemsValue = (typeof TeamItems)[keyof typeof TeamItems];

export interface WorkspaceLimit {
  /** The workspace id (the config always stores one), or what the environment named. */
  ref: string;
  /** As found; checked when sent. */
  teamItems: string;
}

const HEADER_SAFE = /^[A-Za-z0-9._:-]{1,200}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The limit in effect for this process, or `null` for the whole team. Reads no network. */
export function resolveWorkspaceLimit(env: PathEnv = process.env): WorkspaceLimit | null {
  const raw = (env[WORKSPACE_LIMIT_ENV] ?? "").trim();
  const items = (env[WORKSPACE_LIMIT_TEAM_ITEMS_ENV] ?? "").trim();
  if (raw.toLowerCase() === WORKSPACE_LIMIT_TEAM_WORD) return null;
  if (raw) return { ref: raw, teamItems: items || TeamItems.Show };
  const anchor = readActiveProbeContext(env).workspace;
  if (!isRecord(anchor) || anchor.limit !== true) return null;
  const id = typeof anchor.id === "string" ? anchor.id.trim() : "";
  const stored = typeof anchor.team_items === "string" && anchor.team_items.trim() ? anchor.team_items.trim() : TeamItems.Show;
  return { ref: id, teamItems: items || stored };
}

/** The two request headers for this process's limit, or `{}` for the whole team. */
export function workspaceLimitHeaders(env: PathEnv = process.env): Record<string, string> {
  const limit = resolveWorkspaceLimit(env);
  if (!limit) return {};
  const id = limit.ref.startsWith("id:") ? limit.ref.slice(3) : limit.ref;
  const items = limit.teamItems.trim().toLowerCase();
  return {
    [WORKSPACE_LIMIT_HEADER]: HEADER_SAFE.test(id) ? id : UNREADABLE_LIMIT,
    [WORKSPACE_LIMIT_TEAM_ITEMS_HEADER]: items === TeamItems.Show || items === TeamItems.Hide ? items : UNREADABLE_LIMIT,
  };
}

/**
 * Does the limit hide team-wide items? Then pi neither syncs nor injects the team
 * note (`team_note_file.hidden_by_limit` on the CLI's side).
 */
export function teamItemsHidden(env: PathEnv = process.env): boolean {
  const limit = resolveWorkspaceLimit(env);
  return limit !== null && limit.teamItems.trim().toLowerCase() === TeamItems.Hide;
}
