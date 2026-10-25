import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  UNREADABLE_LIMIT,
  WORKSPACE_LIMIT_HEADER,
  WORKSPACE_LIMIT_TEAM_ITEMS_HEADER,
  resolveWorkspaceLimit,
  teamItemsHidden,
  workspaceLimitHeaders,
} from "../src/core/workspaceLimit.js";

const W = "11111111-1111-1111-1111-111111111111";

describe("workspace limit (core/workspaceLimit.ts)", () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "probe-pi-workspace-limit-"));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  function configWith(workspace: Record<string, unknown> | undefined, current = "default"): string {
    const path = join(dir, "config.json");
    writeFileSync(
      path,
      JSON.stringify({ version: 2, current_context: current, contexts: { default: { mcp_token: "t", workspace } } }),
    );
    return path;
  }

  it("is the whole team with no config, no limit key, or limit false", () => {
    expect(resolveWorkspaceLimit({ PROBE_CONFIG_PATH: join(dir, "missing.json") })).toBeNull();
    expect(workspaceLimitHeaders({ PROBE_CONFIG_PATH: configWith({ id: W }) })).toEqual({});
    expect(workspaceLimitHeaders({ PROBE_CONFIG_PATH: configWith({ id: W, limit: false }) })).toEqual({});
  });

  it("reads the active context's limit and team-items choice", () => {
    const env = { PROBE_CONFIG_PATH: configWith({ id: W, limit: true, team_items: "hide", name: "Eng" }) };
    expect(workspaceLimitHeaders(env)).toEqual({ [WORKSPACE_LIMIT_HEADER]: W, [WORKSPACE_LIMIT_TEAM_ITEMS_HEADER]: "hide" });
    expect(teamItemsHidden(env)).toBe(true);
  });

  it("never reads another context's limit", () => {
    const env = { PROBE_CONFIG_PATH: configWith({ id: W, limit: true }, "other") };
    expect(resolveWorkspaceLimit(env)).toBeNull();
  });

  it("lets the environment override it, `team` included", () => {
    const path = configWith({ id: W, limit: true, team_items: "hide" });
    expect(workspaceLimitHeaders({ PROBE_CONFIG_PATH: path, PROBE_WORKSPACE_LIMIT: "team" })).toEqual({});
    expect(workspaceLimitHeaders({ PROBE_CONFIG_PATH: path, PROBE_WORKSPACE_LIMIT_TEAM_ITEMS: "show" })).toEqual({
      [WORKSPACE_LIMIT_HEADER]: W,
      [WORKSPACE_LIMIT_TEAM_ITEMS_HEADER]: "show",
    });
    expect(teamItemsHidden({ PROBE_CONFIG_PATH: path, PROBE_WORKSPACE_LIMIT_TEAM_ITEMS: "show" })).toBe(false);
  });

  it("fails closed: a value it cannot vouch for is sent so the API refuses it, never dropped", () => {
    const env = { PROBE_CONFIG_PATH: join(dir, "missing.json"), PROBE_WORKSPACE_LIMIT: 'a"b', PROBE_WORKSPACE_LIMIT_TEAM_ITEMS: "maybe" };
    expect(workspaceLimitHeaders(env)).toEqual({
      [WORKSPACE_LIMIT_HEADER]: UNREADABLE_LIMIT,
      [WORKSPACE_LIMIT_TEAM_ITEMS_HEADER]: UNREADABLE_LIMIT,
    });
    // A slug pi cannot look up goes as it is (the API answers 400).
    expect(workspaceLimitHeaders({ PROBE_CONFIG_PATH: join(dir, "missing.json"), PROBE_WORKSPACE_LIMIT: "eng" })[WORKSPACE_LIMIT_HEADER]).toBe("eng");
    // limit: true with no id is unreadable, not the whole team.
    expect(workspaceLimitHeaders({ PROBE_CONFIG_PATH: configWith({ limit: true }) })[WORKSPACE_LIMIT_HEADER]).toBe(UNREADABLE_LIMIT);
  });
});
