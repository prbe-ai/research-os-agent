/**
 * mcpBridge.ts's orchestration logic, tested entirely against a FAKE
 * `McpConnector` (see mcpBridge.ts's own module docstring for why) — no test
 * in this file ever imports `realMcpConnector`, constructs a real
 * `StreamableHTTPClientTransport`, or reaches the network. `UnauthorizedError`
 * / `StreamableHTTPError` are imported from the real SDK because they are
 * plain, side-effect-free `Error` subclasses used here purely as typed
 * signals — constructing one throws no request.
 */
import { describe, expect, it, vi } from "vitest";
import { UnauthorizedError } from "@modelcontextprotocol/sdk/client/auth.js";
import { StreamableHTTPError } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import {
  BridgeTimeoutError,
  connectAndRegisterTools,
  extractAuthorizationCode,
  interactiveOAuthLogin,
  isAuthError,
  mcpServerUrl,
  translateContent,
  withTimeout,
  type InteractiveUI,
  type McpBridgeDeps,
  type McpClientLike,
  type McpConnector,
  type McpToolInfo,
  type OAuthLoginStart,
} from "../src/mcpBridge.js";
import type { OAuthStorageDeps } from "../src/mcpOAuth.js";
import { mcpOAuthStateFile } from "../src/paths.js";

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

function fakeStorage(): OAuthStorageDeps & { files: Map<string, string> } {
  const files = new Map<string, string>();
  return {
    files,
    readFileSync: (p) => {
      const v = files.get(p);
      if (v === undefined) throw Object.assign(new Error(`ENOENT: ${p}`), { code: "ENOENT" });
      return v;
    },
    writeFileSync: (p, c) => {
      files.set(p, c);
    },
    mkdirSync: () => {},
  };
}

function unimplementedConnector(): McpConnector {
  return {
    connectWithBearer: async () => {
      throw new Error("connectWithBearer should not be called in this test");
    },
    connectWithOAuthProvider: async () => {
      throw new Error("connectWithOAuthProvider should not be called in this test");
    },
    startOAuthLogin: async () => {
      throw new Error("startOAuthLogin should not be called in this test");
    },
  };
}

function fakeDeps(overrides: Partial<McpBridgeDeps> & { env?: Record<string, string | undefined> } = {}) {
  const announced: Array<{ message: string; level?: string }> = [];
  const logged: string[] = [];
  const deps: McpBridgeDeps = {
    env: { PROBE_CONFIG_PATH: "/nonexistent/probe-config.json", PI_CODING_AGENT_DIR: "/fake/pi-agent-dir" },
    storage: fakeStorage(),
    connector: unimplementedConnector(),
    timeouts: { connectMs: 50, listToolsMs: 50, callToolMs: 50 },
    announce: (message, level) => announced.push({ message, level }),
    log: (message) => logged.push(message),
    ...overrides,
  };
  return { deps, announced, logged };
}

function fakeClient(overrides: Partial<McpClientLike> = {}): McpClientLike & { closeCount: number } {
  const state = { closeCount: 0 };
  const client: McpClientLike & { closeCount: number } = {
    listTools: async () => ({ tools: [] }),
    callTool: async () => ({ content: [{ type: "text", text: "ok" }] }),
    close: async () => {
      state.closeCount++;
    },
    get closeCount() {
      return state.closeCount;
    },
    ...overrides,
  };
  return client;
}

interface FakeToolDef {
  name: string;
  label: string;
  description: string;
  parameters: unknown;
  execute: (toolCallId: string, params: unknown) => Promise<{ content: unknown[]; details: unknown }>;
}

function fakePi() {
  const tools = new Map<string, FakeToolDef>();
  const api = { registerTool: (def: FakeToolDef) => tools.set(def.name, def) };
  return { pi: api as never, tools };
}

const SEARCH_TOOL: McpToolInfo = {
  name: "search_runs",
  description: "Search runs by query",
  inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
};

// ---------------------------------------------------------------------------
// connectAndRegisterTools
// ---------------------------------------------------------------------------

describe("connectAndRegisterTools", () => {
  it("degrades with zero network calls when no bearer token and no stored OAuth tokens exist", async () => {
    const { deps } = fakeDeps(); // unimplementedConnector() throws if ever invoked
    const { pi, tools } = fakePi();

    const result = await connectAndRegisterTools(pi, deps);

    expect("skipped" in result).toBe(true);
    if ("skipped" in result) {
      expect(result.skipped).toContain("/probe-mcp-login");
    }
    expect(tools.size).toBe(0);
  });

  it("connects via the bearer fast path and registers every listed tool", async () => {
    const client = fakeClient({ listTools: async () => ({ tools: [SEARCH_TOOL, { name: "get_run", inputSchema: { type: "object" } }] }) });
    const connectWithBearer = vi.fn(async () => client);
    const { deps } = fakeDeps({
      env: { PROBE_MCP_TOKEN: "bearer-token-1", PI_CODING_AGENT_DIR: "/fake/pi-agent-dir" },
      connector: { ...unimplementedConnector(), connectWithBearer },
    });
    const { pi, tools } = fakePi();

    const result = await connectAndRegisterTools(pi, deps);

    expect("registered" in result).toBe(true);
    if ("registered" in result) expect(result.registered).toBe(2);
    expect(connectWithBearer).toHaveBeenCalledTimes(1);
    expect(connectWithBearer).toHaveBeenCalledWith(mcpServerUrl(deps.env), "bearer-token-1", deps.timeouts.connectMs);
    expect(tools.has("probe_mcp_search_runs")).toBe(true);
    expect(tools.has("probe_mcp_get_run")).toBe(true);
    const registered = tools.get("probe_mcp_search_runs")!;
    expect(registered.label).toBe("search_runs");
    expect(registered.description).toBe("Search runs by query");
    expect((registered.parameters as Record<string, unknown>).type).toBe("object");
  });

  it("uses PROBE_MCP_URL to override the server URL when set", async () => {
    const client = fakeClient();
    let requestedUrl: URL | undefined;
    const connectWithBearer = vi.fn(async (url: URL) => {
      requestedUrl = url;
      return client;
    });
    const { deps } = fakeDeps({
      env: { PROBE_MCP_TOKEN: "t", PROBE_MCP_URL: "https://mcp.example.test/mcp", PI_CODING_AGENT_DIR: "/fake" },
      connector: { ...unimplementedConnector(), connectWithBearer },
    });
    const { pi } = fakePi();

    await connectAndRegisterTools(pi, deps);

    expect(requestedUrl?.toString()).toBe("https://mcp.example.test/mcp");
  });

  it("degrades cleanly (no throw) when the connector's connect rejects — server unreachable", async () => {
    const connectWithBearer = vi.fn(async () => {
      throw new Error("ECONNREFUSED");
    });
    const { deps } = fakeDeps({
      env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
      connector: { ...unimplementedConnector(), connectWithBearer },
    });
    const { pi, tools } = fakePi();

    const result = await connectAndRegisterTools(pi, deps);

    expect("skipped" in result).toBe(true);
    if ("skipped" in result) {
      expect(result.skipped).toContain("Probe MCP connection failed");
      expect(result.skipped).toContain("ECONNREFUSED");
    }
    expect(tools.size).toBe(0);
  });

  it("gives a timeout-specific message when the connector reports a BridgeTimeoutError", async () => {
    const connectWithBearer = vi.fn(async () => {
      throw new BridgeTimeoutError("MCP connect timed out after 50ms");
    });
    const { deps } = fakeDeps({
      env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
      connector: { ...unimplementedConnector(), connectWithBearer },
    });
    const { pi } = fakePi();

    const result = await connectAndRegisterTools(pi, deps);

    expect("skipped" in result).toBe(true);
    if ("skipped" in result) expect(result.skipped).toContain("timed out");
  });

  it("closes the client and returns skipped when listTools fails", async () => {
    const client = fakeClient({
      listTools: async () => {
        throw new Error("list failed");
      },
    });
    const connectWithBearer = vi.fn(async () => client);
    const { deps } = fakeDeps({
      env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
      connector: { ...unimplementedConnector(), connectWithBearer },
    });
    const { pi } = fakePi();

    const result = await connectAndRegisterTools(pi, deps);

    expect("skipped" in result).toBe(true);
    if ("skipped" in result) expect(result.skipped).toContain("list its tools");
    expect(client.closeCount).toBe(1);
  });

  it("uses previously-saved OAuth tokens when no bearer token exists, without prompting", async () => {
    const storage = fakeStorage();
    const env = { PI_CODING_AGENT_DIR: "/fake/pi-agent-dir" };
    // Simulate a prior successful `/probe-mcp-login` by pre-seeding storage
    // the same way ProbeOAuthClientProvider.saveTokens does.
    storage.files.set(mcpOAuthStateFile(env), JSON.stringify({ tokens: { access_token: "at-1", token_type: "bearer" } }));

    const client = fakeClient({ listTools: async () => ({ tools: [SEARCH_TOOL] }) });
    const connectWithOAuthProvider = vi.fn(async () => client);
    const { deps } = fakeDeps({ env, storage, connector: { ...unimplementedConnector(), connectWithOAuthProvider } });
    const { pi, tools } = fakePi();

    const result = await connectAndRegisterTools(pi, deps);

    expect("registered" in result).toBe(true);
    expect(connectWithOAuthProvider).toHaveBeenCalledTimes(1);
    expect(tools.has("probe_mcp_search_runs")).toBe(true);
  });

  it("reregister() re-runs pi.registerTool with no additional connector calls (the /reload case)", async () => {
    const client = fakeClient({ listTools: async () => ({ tools: [SEARCH_TOOL] }) });
    const connectWithBearer = vi.fn(async () => client);
    const { deps } = fakeDeps({
      env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
      connector: { ...unimplementedConnector(), connectWithBearer },
    });
    const { pi, tools } = fakePi();

    const result = await connectAndRegisterTools(pi, deps);
    expect("registered" in result).toBe(true);
    if (!("registered" in result)) return;

    const { pi: reloadedPi, tools: reloadedTools } = fakePi();
    const registeredAgain = result.reregister(reloadedPi);

    expect(registeredAgain).toBe(1);
    expect(reloadedTools.has("probe_mcp_search_runs")).toBe(true);
    expect(connectWithBearer).toHaveBeenCalledTimes(1); // still just the original connect
    expect(tools.has("probe_mcp_search_runs")).toBe(true); // the original registry is untouched
  });

  describe("tool execute()", () => {
    it("round-trips a text block and stamps details.mcpTool", async () => {
      const client = fakeClient({
        listTools: async () => ({ tools: [SEARCH_TOOL] }),
        callTool: async (params) => {
          expect(params).toEqual({ name: "search_runs", arguments: { query: "bird-sql" } });
          return { content: [{ type: "text", text: "3 runs found" }] };
        },
      });
      const { deps } = fakeDeps({
        env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
        connector: { ...unimplementedConnector(), connectWithBearer: async () => client },
      });
      const { pi, tools } = fakePi();
      await connectAndRegisterTools(pi, deps);

      const result = (await tools.get("probe_mcp_search_runs")!.execute("call-1", { query: "bird-sql" })) as {
        content: unknown[];
        details: unknown;
      };

      expect(result.content).toEqual([{ type: "text", text: "3 runs found" }]);
      expect(result.details).toEqual({ mcpTool: "search_runs" });
    });

    it("translates an image block and falls back to a text summary for an unsupported block type", async () => {
      const client = fakeClient({
        listTools: async () => ({ tools: [SEARCH_TOOL] }),
        callTool: async () => ({
          content: [
            { type: "image", data: "aGVsbG8=", mimeType: "image/png" },
            { type: "resource", resource: { uri: "probe://run/1", text: "embedded card" } },
          ],
        }),
      });
      const { deps } = fakeDeps({
        env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
        connector: { ...unimplementedConnector(), connectWithBearer: async () => client },
      });
      const { pi, tools } = fakePi();
      await connectAndRegisterTools(pi, deps);

      const result = (await tools.get("probe_mcp_search_runs")!.execute("call-1", {})) as { content: unknown[] };

      expect(result.content).toEqual([
        { type: "image", data: "aGVsbG8=", mimeType: "image/png" },
        { type: "text", text: "embedded card" },
      ]);
    });

    it("throws using the tool's error text when the MCP result is isError", async () => {
      const client = fakeClient({
        listTools: async () => ({ tools: [SEARCH_TOOL] }),
        callTool: async () => ({ isError: true, content: [{ type: "text", text: "invalid query" }] }),
      });
      const { deps } = fakeDeps({
        env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
        connector: { ...unimplementedConnector(), connectWithBearer: async () => client },
      });
      const { pi, tools } = fakePi();
      await connectAndRegisterTools(pi, deps);

      await expect(tools.get("probe_mcp_search_runs")!.execute("call-1", {})).rejects.toThrow("invalid query");
    });

    it("re-resolves the bearer token and retries once on a 401", async () => {
      const firstClient = fakeClient({
        listTools: async () => ({ tools: [SEARCH_TOOL] }),
        callTool: async () => {
          throw new StreamableHTTPError(401, "unauthorized");
        },
      });
      const secondClient = fakeClient({ callTool: async () => ({ content: [{ type: "text", text: "after reconnect" }] }) });
      const env: Record<string, string | undefined> = { PROBE_MCP_TOKEN: "stale-token", PI_CODING_AGENT_DIR: "/fake" };
      let connectAttempts = 0;
      const connectWithBearer = vi.fn(async () => (++connectAttempts === 1 ? firstClient : secondClient));
      const { deps } = fakeDeps({ env, connector: { ...unimplementedConnector(), connectWithBearer } });
      const { pi, tools } = fakePi();
      await connectAndRegisterTools(pi, deps);

      const result = (await tools.get("probe_mcp_search_runs")!.execute("call-1", {})) as { content: unknown[] };

      expect(result.content).toEqual([{ type: "text", text: "after reconnect" }]);
      expect(connectWithBearer).toHaveBeenCalledTimes(2);
    });

    it("also retries on a 403, and on the SDK's UnauthorizedError", async () => {
      for (const authError of [new StreamableHTTPError(403, "forbidden"), new UnauthorizedError()]) {
        const firstClient = fakeClient({
          listTools: async () => ({ tools: [SEARCH_TOOL] }),
          callTool: async () => {
            throw authError;
          },
        });
        const secondClient = fakeClient({ callTool: async () => ({ content: [{ type: "text", text: "ok" }] }) });
        const env: Record<string, string | undefined> = { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" };
        let calls = 0;
        const connectWithBearer = vi.fn(async () => (++calls === 1 ? firstClient : secondClient));
        const { deps } = fakeDeps({ env, connector: { ...unimplementedConnector(), connectWithBearer } });
        const { pi, tools } = fakePi();
        await connectAndRegisterTools(pi, deps);

        const result = (await tools.get("probe_mcp_search_runs")!.execute("call-1", {})) as { content: unknown[] };
        expect(result.content).toEqual([{ type: "text", text: "ok" }]);
      }
    });

    it("gives up cleanly when the token cannot be re-resolved after a 401", async () => {
      const client = fakeClient({
        listTools: async () => ({ tools: [SEARCH_TOOL] }),
        callTool: async () => {
          throw new StreamableHTTPError(401, "unauthorized");
        },
      });
      const env: Record<string, string | undefined> = { PROBE_MCP_TOKEN: "will-vanish", PI_CODING_AGENT_DIR: "/fake" };
      const connectWithBearer = vi.fn(async () => client);
      const { deps } = fakeDeps({ env, connector: { ...unimplementedConnector(), connectWithBearer } });
      const { pi, tools } = fakePi();
      await connectAndRegisterTools(pi, deps);

      // Token disappears between the initial connect and the tool call —
      // e.g. an export the researcher removed, or a config edited mid-session.
      delete env.PROBE_MCP_TOKEN;

      await expect(tools.get("probe_mcp_search_runs")!.execute("call-1", {})).rejects.toThrow(/not authorized/i);
      expect(connectWithBearer).toHaveBeenCalledTimes(1); // no second connect attempt without a token to use
    });

    it("does not retry on a non-auth error", async () => {
      const client = fakeClient({
        listTools: async () => ({ tools: [SEARCH_TOOL] }),
        callTool: async () => {
          throw new Error("some other failure");
        },
      });
      const connectWithBearer = vi.fn(async () => client);
      const { deps } = fakeDeps({
        env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
        connector: { ...unimplementedConnector(), connectWithBearer },
      });
      const { pi, tools } = fakePi();
      await connectAndRegisterTools(pi, deps);

      await expect(tools.get("probe_mcp_search_runs")!.execute("call-1", {})).rejects.toThrow("some other failure");
      expect(connectWithBearer).toHaveBeenCalledTimes(1);
    });
  });
});

// ---------------------------------------------------------------------------
// interactiveOAuthLogin
// ---------------------------------------------------------------------------

function fakeUI(overrides: Partial<InteractiveUI> = {}): InteractiveUI & { inputCalls: Array<{ title: string; placeholder?: string }> } {
  const inputCalls: Array<{ title: string; placeholder?: string }> = [];
  return {
    hasUI: true,
    input: async (title, placeholder) => {
      inputCalls.push({ title, placeholder });
      return undefined;
    },
    inputCalls,
    ...overrides,
  };
}

describe("interactiveOAuthLogin", () => {
  it("uses the bearer fast path when a token exists, and never touches OAuth", async () => {
    const client = fakeClient({ listTools: async () => ({ tools: [SEARCH_TOOL] }) });
    const startOAuthLogin = vi.fn();
    const { deps } = fakeDeps({
      env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
      connector: { ...unimplementedConnector(), connectWithBearer: async () => client, startOAuthLogin },
    });
    const { pi } = fakePi();

    const result = await interactiveOAuthLogin(pi, deps, fakeUI());

    expect(result.status).toBe("connected");
    expect(result.registered).toBe(1);
    expect(startOAuthLogin).not.toHaveBeenCalled();
  });

  it("reports the real failure (not a login prompt) when a bearer token exists but the connection fails", async () => {
    const { deps } = fakeDeps({
      env: { PROBE_MCP_TOKEN: "t", PI_CODING_AGENT_DIR: "/fake" },
      connector: {
        ...unimplementedConnector(),
        connectWithBearer: async () => {
          throw new Error("network down");
        },
      },
    });
    const { pi } = fakePi();

    const result = await interactiveOAuthLogin(pi, deps, fakeUI());

    expect(result.status).toBe("failed");
    expect(result.message).toContain("network down");
  });

  it("reports requires-interactive when there is no bearer token and no UI", async () => {
    const { deps } = fakeDeps({ env: { PI_CODING_AGENT_DIR: "/fake" } });
    const { pi } = fakePi();

    const result = await interactiveOAuthLogin(pi, deps, fakeUI({ hasUI: false }));

    expect(result.status).toBe("requires-interactive");
  });

  it("reports connected (no fresh login) when the OAuth provider already has valid stored tokens", async () => {
    const client = fakeClient({ listTools: async () => ({ tools: [SEARCH_TOOL] }) });
    const startOAuthLogin = vi.fn(async (): Promise<OAuthLoginStart> => ({ authorized: true, client }));
    const { deps } = fakeDeps({ env: { PI_CODING_AGENT_DIR: "/fake" }, connector: { ...unimplementedConnector(), startOAuthLogin } });
    const { pi } = fakePi();
    const ui = fakeUI();

    const result = await interactiveOAuthLogin(pi, deps, ui);

    expect(result.status).toBe("connected");
    expect(result.registered).toBe(1);
    expect(ui.inputCalls).toHaveLength(0);
  });

  it("walks the full paste-back flow: prompts, extracts the code from a full URL, exchanges it, and registers tools", async () => {
    const client = fakeClient({ listTools: async () => ({ tools: [SEARCH_TOOL] }) });
    const finishAuth = vi.fn(async (code: string) => {
      expect(code).toBe("abc123");
      return client;
    });
    const startOAuthLogin = vi.fn(async (): Promise<OAuthLoginStart> => ({ authorized: false, finishAuth }));
    const { deps } = fakeDeps({ env: { PI_CODING_AGENT_DIR: "/fake" }, connector: { ...unimplementedConnector(), startOAuthLogin } });
    const { pi } = fakePi();
    const ui = fakeUI({ input: async () => "http://127.0.0.1:8945/probe-research-pi/callback?code=abc123&state=xyz" });

    const result = await interactiveOAuthLogin(pi, deps, ui);

    expect(result.status).toBe("authorized");
    expect(result.registered).toBe(1);
    expect(finishAuth).toHaveBeenCalledWith("abc123");
  });

  it("accepts a bare authorization code, not just a full URL", async () => {
    const client = fakeClient({ listTools: async () => ({ tools: [] }) });
    const finishAuth = vi.fn(async (code: string) => {
      expect(code).toBe("bare-code-value");
      return client;
    });
    const startOAuthLogin = vi.fn(async (): Promise<OAuthLoginStart> => ({ authorized: false, finishAuth }));
    const { deps } = fakeDeps({ env: { PI_CODING_AGENT_DIR: "/fake" }, connector: { ...unimplementedConnector(), startOAuthLogin } });
    const { pi } = fakePi();
    const ui = fakeUI({ input: async () => "bare-code-value" });

    const result = await interactiveOAuthLogin(pi, deps, ui);

    expect(result.status).toBe("authorized");
  });

  it("fails cleanly when the researcher cancels the paste prompt", async () => {
    const startOAuthLogin = vi.fn(async (): Promise<OAuthLoginStart> => ({ authorized: false, finishAuth: vi.fn() }));
    const { deps } = fakeDeps({ env: { PI_CODING_AGENT_DIR: "/fake" }, connector: { ...unimplementedConnector(), startOAuthLogin } });
    const { pi } = fakePi();
    const ui = fakeUI({ input: async () => undefined });

    const result = await interactiveOAuthLogin(pi, deps, ui);

    expect(result.status).toBe("failed");
    expect(result.message).toMatch(/cancelled/i);
  });

  it("fails cleanly when nothing that looks like a code was pasted", async () => {
    const startOAuthLogin = vi.fn(async (): Promise<OAuthLoginStart> => ({ authorized: false, finishAuth: vi.fn() }));
    const { deps } = fakeDeps({ env: { PI_CODING_AGENT_DIR: "/fake" }, connector: { ...unimplementedConnector(), startOAuthLogin } });
    const { pi } = fakePi();
    // Non-blank (so this exercises extractAuthorizationCode's "no code
    // found" path, not the earlier "cancelled" path a blank paste takes).
    const ui = fakeUI({ input: async () => "totally not a code or url!!" });

    const result = await interactiveOAuthLogin(pi, deps, ui);

    expect(result.status).toBe("failed");
    expect(result.message).toMatch(/could not find an authorization/i);
  });

  it("fails cleanly when the token exchange itself fails", async () => {
    const finishAuth = vi.fn(async () => {
      throw new Error("invalid_grant");
    });
    const startOAuthLogin = vi.fn(async (): Promise<OAuthLoginStart> => ({ authorized: false, finishAuth }));
    const { deps } = fakeDeps({ env: { PI_CODING_AGENT_DIR: "/fake" }, connector: { ...unimplementedConnector(), startOAuthLogin } });
    const { pi } = fakePi();
    const ui = fakeUI({ input: async () => "code-value" });

    const result = await interactiveOAuthLogin(pi, deps, ui);

    expect(result.status).toBe("failed");
    expect(result.message).toContain("invalid_grant");
  });

  it("fails cleanly, without prompting, when the server cannot be reached at all", async () => {
    const startOAuthLogin = vi.fn(async () => {
      throw new Error("DNS lookup failed");
    });
    const { deps } = fakeDeps({ env: { PI_CODING_AGENT_DIR: "/fake" }, connector: { ...unimplementedConnector(), startOAuthLogin } });
    const { pi } = fakePi();
    const ui = fakeUI();

    const result = await interactiveOAuthLogin(pi, deps, ui);

    expect(result.status).toBe("failed");
    expect(result.message).toContain("DNS lookup failed");
    expect(ui.inputCalls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Small pure helpers
// ---------------------------------------------------------------------------

describe("isAuthError", () => {
  it("is true for UnauthorizedError", () => {
    expect(isAuthError(new UnauthorizedError())).toBe(true);
  });
  it("is true for a 401 StreamableHTTPError", () => {
    expect(isAuthError(new StreamableHTTPError(401, "nope"))).toBe(true);
  });
  it("is true for a 403 StreamableHTTPError", () => {
    expect(isAuthError(new StreamableHTTPError(403, "nope"))).toBe(true);
  });
  it("is false for a 500 StreamableHTTPError", () => {
    expect(isAuthError(new StreamableHTTPError(500, "server error"))).toBe(false);
  });
  it("is false for a generic Error or a non-error value", () => {
    expect(isAuthError(new Error("boom"))).toBe(false);
    expect(isAuthError("boom")).toBe(false);
    expect(isAuthError(undefined)).toBe(false);
  });
});

describe("translateContent", () => {
  it("passes through text and image blocks unchanged", () => {
    const blocks = [
      { type: "text", text: "hello" },
      { type: "image", data: "AAAA", mimeType: "image/png" },
    ];
    expect(translateContent(blocks)).toEqual(blocks);
  });

  it("summarizes a resource block using its embedded text when present", () => {
    expect(translateContent([{ type: "resource", resource: { uri: "probe://x", text: "the card" } }])).toEqual([
      { type: "text", text: "the card" },
    ]);
  });

  it("summarizes a resource block by uri when it has no embedded text", () => {
    expect(translateContent([{ type: "resource", resource: { uri: "probe://x", blob: "AAAA" } }])).toEqual([
      { type: "text", text: "[resource: probe://x]" },
    ]);
  });

  it("summarizes a resource_link block by its uri", () => {
    expect(translateContent([{ type: "resource_link", uri: "probe://y", name: "y" }])).toEqual([{ type: "text", text: "[resource link: probe://y]" }]);
  });

  it("falls back to a JSON dump for a wholly unknown block shape", () => {
    expect(translateContent([{ type: "audio", data: "AAAA", mimeType: "audio/wav" }])).toEqual([
      { type: "text", text: JSON.stringify({ type: "audio", data: "AAAA", mimeType: "audio/wav" }) },
    ]);
  });

  it("never returns an empty array — an empty result becomes a placeholder text block", () => {
    expect(translateContent([])).toEqual([{ type: "text", text: "(no content)" }]);
  });

  it("skips null/non-object entries rather than throwing", () => {
    expect(translateContent([null, undefined, "not an object", { type: "text", text: "kept" }])).toEqual([{ type: "text", text: "kept" }]);
  });
});

describe("extractAuthorizationCode", () => {
  it("extracts code from a full callback URL", () => {
    expect(extractAuthorizationCode("http://127.0.0.1:8945/callback?code=abc123&state=xyz")).toBe("abc123");
  });
  it("extracts code from a bare query-string fragment", () => {
    expect(extractAuthorizationCode("state=xyz&code=def456")).toBe("def456");
  });
  it("URL-decodes the extracted code", () => {
    expect(extractAuthorizationCode("code=abc%2B123")).toBe("abc+123");
  });
  it("accepts a bare code with no query string at all", () => {
    expect(extractAuthorizationCode("just-a-code-value_123")).toBe("just-a-code-value_123");
  });
  it("returns null for something with no discoverable code", () => {
    expect(extractAuthorizationCode("http://127.0.0.1:8945/callback?state=xyz")).toBeNull();
    expect(extractAuthorizationCode("nonsense with spaces")).toBeNull();
    expect(extractAuthorizationCode("")).toBeNull();
  });
});

describe("withTimeout", () => {
  it("resolves with the underlying value when it settles before the deadline", async () => {
    await expect(withTimeout(Promise.resolve(42), 1000, "op")).resolves.toBe(42);
  });

  it("rejects with the original error when the promise rejects before the deadline", async () => {
    await expect(withTimeout(Promise.reject(new Error("boom")), 1000, "op")).rejects.toThrow("boom");
  });

  it("rejects with a BridgeTimeoutError once the deadline passes", async () => {
    const neverResolves = new Promise<void>(() => {});
    await expect(withTimeout(neverResolves, 10, "slow op")).rejects.toBeInstanceOf(BridgeTimeoutError);
  });
});

describe("mcpServerUrl", () => {
  it("defaults to the production Probe MCP server", () => {
    expect(mcpServerUrl({}).toString()).toBe("https://mcp.research.prbe.ai/mcp");
  });
  it("honours PROBE_MCP_URL when set", () => {
    expect(mcpServerUrl({ PROBE_MCP_URL: "https://mcp.example.test/mcp" }).toString()).toBe("https://mcp.example.test/mcp");
  });
  it("ignores a whitespace-only PROBE_MCP_URL", () => {
    expect(mcpServerUrl({ PROBE_MCP_URL: "   " }).toString()).toBe("https://mcp.research.prbe.ai/mcp");
  });
});
