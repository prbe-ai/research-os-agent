/**
 * The bridge: connect to the Probe Research read MCP server and register
 * each of its tools as a native pi tool (`examples/extensions/dynamic-tools.ts`
 * is pi's own reference for registering tools outside the synchronous top
 * of `session_start`). Two entry points:
 *
 *   `connectAndRegisterTools()` — the NON-interactive path, called from
 *   `session_start`. Tries the bearer fast path (`mcpAuth.ts`), then
 *   previously-saved OAuth tokens if any exist, and otherwise degrades
 *   immediately with no network call at all. Every network step is bounded
 *   (see `withTimeout`) so a slow or hung server can never stall the
 *   session — pi wraps extension handlers in a bare try/catch with NO
 *   timeout of its own (verified against `dist/core/extensions/runner.js`),
 *   so the bound has to come from here.
 *
 *   `interactiveOAuthLogin()` — the OAuth fallback, called only from the
 *   `/probe-mcp-login` command, which is the one place in this extension
 *   allowed to block on a human: the human just typed the command and is
 *   watching. See `mcpOAuth.ts`'s module docstring for why this asks the
 *   researcher to paste back a URL instead of running a callback server.
 *
 * DEGRADE, NEVER CRASH. Every failure path here returns a `{ skipped:
 * string }` (or, for the interactive path, a `status`/`message` pair)
 * rather than throwing — `extension.ts` turns that into the same
 * announce-once-on-stderr-and-notify shape the unpaired-tap case already
 * uses. Nothing in this module lets a Probe MCP problem take down capture,
 * the team note, or the rest of the pi session.
 *
 * TESTABILITY: everything above the `McpConnector` line is orchestration
 * (auth resolution, retry-on-401, timeout/degrade behaviour, tool
 * registration and content translation) and is exercised directly in
 * `tests/mcpBridge.test.ts` against a FAKE connector — never the real MCP
 * SDK, and never the network (see that file, and the task's own "never hit
 * the real MCP server in a test" instruction). `realMcpConnector` below is
 * the thin, load-bearing seam that does talk to `@modelcontextprotocol/sdk`;
 * its own three methods are intentionally small enough to read against the
 * SDK's types rather than needing their own mocked-network test.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { UnauthorizedError } from "@modelcontextprotocol/sdk/client/auth.js";
import { StreamableHTTPClientTransport, StreamableHTTPError } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { resolveMcpBearerToken } from "./mcpAuth.js";
import { defaultOAuthStorageDeps, ProbeOAuthClientProvider, type OAuthStorageDeps } from "./mcpOAuth.js";
import { jsonSchemaToTypeBox, prefixToolName } from "./mcpSchema.js";
import { MCP_SERVER_URL, MCP_URL_ENV, type PathEnv } from "./paths.js";

const CLIENT_INFO = { name: "probe-research-pi", version: "0.1.0" };

export interface McpBridgeTimeouts {
  connectMs: number;
  listToolsMs: number;
  callToolMs: number;
}

export const DEFAULT_MCP_TIMEOUTS: McpBridgeTimeouts = {
  connectMs: 6000,
  listToolsMs: 6000,
  callToolMs: 30000,
};

// pi's AgentToolResult.content is `(TextContent | ImageContent)[]` (verified
// against the installed @earendil-works/pi-agent-core types) — declared
// locally rather than imported because pi-coding-agent's public d.ts does
// not re-export those two names, and these two literal shapes are the
// entire contract this module needs from them.
type PiTextContent = { type: "text"; text: string };
type PiImageContent = { type: "image"; data: string; mimeType: string };
type PiContent = PiTextContent | PiImageContent;

export class BridgeTimeoutError extends Error {}

export function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new BridgeTimeoutError(`${label} timed out after ${ms}ms`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** True for exactly the errors `realMcpConnector` raises for a 401/403 response — see its own comment. */
export function isAuthError(err: unknown): boolean {
  if (err instanceof UnauthorizedError) return true;
  if (err instanceof StreamableHTTPError) return err.code === 401 || err.code === 403;
  return false;
}

export function mcpServerUrl(env: PathEnv): URL {
  const override = env[MCP_URL_ENV];
  return new URL(override && override.trim() ? override.trim() : MCP_SERVER_URL);
}

// ---------------------------------------------------------------------------
// MCP content -> pi content
// ---------------------------------------------------------------------------

function summarizeUnsupportedBlock(block: Record<string, unknown>): string {
  if (block.type === "resource" && block.resource && typeof block.resource === "object") {
    const resource = block.resource as Record<string, unknown>;
    if (typeof resource.text === "string") return resource.text;
    if (typeof resource.uri === "string") return `[resource: ${resource.uri}]`;
  }
  if (block.type === "resource_link" && typeof block.uri === "string") {
    return `[resource link: ${block.uri}]`;
  }
  try {
    return JSON.stringify(block);
  } catch {
    return String(block);
  }
}

/** pi's content union only knows text/image; other MCP block types (audio, resource, resource_link) fall back to a text summary rather than being dropped. */
export function translateContent(blocks: readonly unknown[]): PiContent[] {
  const out: PiContent[] = [];
  for (const block of blocks) {
    if (!block || typeof block !== "object") continue;
    const b = block as Record<string, unknown>;
    if (b.type === "text" && typeof b.text === "string") {
      out.push({ type: "text", text: b.text });
    } else if (b.type === "image" && typeof b.data === "string" && typeof b.mimeType === "string") {
      out.push({ type: "image", data: b.data, mimeType: b.mimeType });
    } else {
      out.push({ type: "text", text: summarizeUnsupportedBlock(b) });
    }
  }
  if (out.length === 0) out.push({ type: "text", text: "(no content)" });
  return out;
}

function extractText(blocks: unknown): string {
  if (!Array.isArray(blocks)) return "";
  return blocks
    .filter((b): b is { type: "text"; text: string } => !!b && typeof b === "object" && (b as Record<string, unknown>).type === "text")
    .map((b) => b.text)
    .join("\n");
}

// ---------------------------------------------------------------------------
// The connector seam — real SDK on one side, a fake on the test side
// ---------------------------------------------------------------------------

export interface McpToolInfo {
  name: string;
  description?: string;
  title?: string;
  inputSchema: unknown;
}

/**
 * Exactly what this module needs from an MCP client — narrow on purpose so a
 * fake can satisfy it in tests without touching the real SDK. `callTool`'s
 * return type is deliberately `unknown` rather than a `{content, isError}`
 * shape: the real SDK's `Client.callTool()` return type is a union with a
 * second, content-less "task" branch (`{toolResult: unknown}`), and TypeScript's
 * weak-type check rejects that union against any all-optional target shape
 * — narrowing happens defensively in `callMcpTool` instead, which is the
 * right place for it anyway since this is externally-supplied data.
 */
export interface McpClientLike {
  listTools(): Promise<{ tools: McpToolInfo[] }>;
  callTool(params: { name: string; arguments?: Record<string, unknown> }): Promise<unknown>;
  close(): Promise<void>;
}

/** Result of starting (or completing) an OAuth login attempt. */
export type OAuthLoginStart =
  | { authorized: true; client: McpClientLike }
  | {
      authorized: false;
      /** Exchange the pasted-back authorization code and connect. Throws on failure. */
      finishAuth: (code: string) => Promise<McpClientLike>;
    };

/**
 * Everything this module needs to talk to an MCP server, behind one small
 * interface. `realMcpConnector` is the only implementation that touches
 * `@modelcontextprotocol/sdk`; every other function in this file is written
 * against this interface and is exercised in tests through a fake one.
 */
export interface McpConnector {
  connectWithBearer(url: URL, token: string, timeoutMs: number): Promise<McpClientLike>;
  connectWithOAuthProvider(url: URL, provider: ProbeOAuthClientProvider, timeoutMs: number): Promise<McpClientLike>;
  /**
   * Attempt a connection using `authProvider`. If stored tokens are already
   * valid, resolves `{ authorized: true, client }` directly. If the server
   * demands a fresh login (`UnauthorizedError` — by which point
   * `provider.redirectToAuthorization` has already fired), resolves
   * `{ authorized: false, finishAuth }`, where `finishAuth(code)` completes
   * the exchange and reconnects. Rejects for any other failure (network,
   * timeout, ...).
   */
  startOAuthLogin(url: URL, provider: ProbeOAuthClientProvider, timeoutMs: number): Promise<OAuthLoginStart>;
}

async function realConnect(url: URL, transport: StreamableHTTPClientTransport, timeoutMs: number): Promise<Client> {
  const client = new Client(CLIENT_INFO, { capabilities: {} });
  await withTimeout(client.connect(transport), timeoutMs, `MCP connect (${url.toString()})`);
  return client;
}

export const realMcpConnector: McpConnector = {
  async connectWithBearer(url, token, timeoutMs) {
    const transport = new StreamableHTTPClientTransport(url, { requestInit: { headers: { Authorization: `Bearer ${token}` } } });
    return realConnect(url, transport, timeoutMs);
  },

  async connectWithOAuthProvider(url, provider, timeoutMs) {
    const transport = new StreamableHTTPClientTransport(url, { authProvider: provider });
    return realConnect(url, transport, timeoutMs);
  },

  async startOAuthLogin(url, provider, timeoutMs) {
    const transport = new StreamableHTTPClientTransport(url, { authProvider: provider });
    try {
      const client = await realConnect(url, transport, timeoutMs);
      return { authorized: true, client };
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) throw err;
      // provider.redirectToAuthorization already fired as a side effect of the
      // failed connect above (see mcpOAuth.ts) — the caller now owns getting a
      // code back from the researcher.
      return {
        authorized: false,
        finishAuth: async (code: string) => {
          await withTimeout(transport.finishAuth(code), timeoutMs, "MCP token exchange");
          const retryTransport = new StreamableHTTPClientTransport(url, { authProvider: provider });
          return realConnect(url, retryTransport, timeoutMs);
        },
      };
    }
  },
};

// ---------------------------------------------------------------------------
// Registering tools
// ---------------------------------------------------------------------------

/** Register every listed tool. `reconnect` is called on a 401/403 from a tool call; return `null` from it to give up (no silent retry available). */
function registerTools(
  pi: ExtensionAPI,
  tools: McpToolInfo[],
  getClient: () => McpClientLike,
  reconnect: () => Promise<McpClientLike | null>,
  callToolTimeoutMs: number,
  log: (message: string) => void,
): number {
  for (const tool of tools) {
    const registeredName = prefixToolName(tool.name);
    pi.registerTool({
      name: registeredName,
      label: tool.title ?? tool.name,
      description: (tool.description ?? `Probe Research MCP tool: ${tool.name}`).slice(0, 4000),
      parameters: jsonSchemaToTypeBox(tool.inputSchema),
      async execute(_toolCallId, params) {
        try {
          return await callMcpTool(getClient(), tool.name, params, callToolTimeoutMs);
        } catch (err) {
          if (!isAuthError(err)) throw err;
          log(`probe_mcp: "${tool.name}" got an auth error (${errMessage(err)}); re-resolving credentials and retrying once`);
          const fresh = await reconnect();
          if (!fresh) {
            throw new Error(
              `Probe MCP tool "${tool.name}" failed: not authorized, and credentials could not be re-resolved. ` +
                "Run `probe mcp token set`, or `/probe-mcp-login` for an interactive re-authentication.",
            );
          }
          return await callMcpTool(fresh, tool.name, params, callToolTimeoutMs);
        }
      },
    });
  }
  return tools.length;
}

async function callMcpTool(client: McpClientLike, mcpToolName: string, params: unknown, timeoutMs: number) {
  const raw = await withTimeout(
    client.callTool({ name: mcpToolName, arguments: (params ?? {}) as Record<string, unknown> }),
    timeoutMs,
    `MCP callTool(${mcpToolName})`,
  );
  const result = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const content = Array.isArray(result.content) ? result.content : [];
  if (result.isError) {
    throw new Error(extractText(content) || `Probe MCP tool "${mcpToolName}" returned an error with no message.`);
  }
  return {
    content: translateContent(content),
    details: { mcpTool: mcpToolName },
  };
}

// ---------------------------------------------------------------------------
// Shared bridge deps
// ---------------------------------------------------------------------------

export interface McpBridgeDeps {
  env: PathEnv;
  storage: OAuthStorageDeps;
  connector: McpConnector;
  timeouts: McpBridgeTimeouts;
  /** User-facing: stderr + extension log + ctx.ui.notify when a UI exists. Mirrors extension.ts's `announce`. */
  announce: (message: string, level?: "info" | "warning" | "error") => void;
  /** Extension-log-only, no stderr/notify — for routine, non-actionable detail. */
  log: (message: string) => void;
}

export function defaultMcpBridgeDeps(env: PathEnv, announce: McpBridgeDeps["announce"], log: McpBridgeDeps["log"]): McpBridgeDeps {
  return { env, storage: defaultOAuthStorageDeps, connector: realMcpConnector, timeouts: DEFAULT_MCP_TIMEOUTS, announce, log };
}

function describeConnectFailure(err: unknown, credentialDescription: string): string {
  if (err instanceof BridgeTimeoutError) {
    return `Probe MCP connection timed out (${credentialDescription}) — the server may be unreachable. Probe tools are unavailable for this session.`;
  }
  return `Probe MCP connection failed (${credentialDescription}): ${errMessage(err)}. Probe tools are unavailable for this session.`;
}

// ---------------------------------------------------------------------------
// session_start: non-interactive connect
// ---------------------------------------------------------------------------

export type ConnectResult =
  | {
      registered: number;
      close: () => Promise<void>;
      /**
       * Re-run `pi.registerTool()` for the already-fetched tool list against
       * a (possibly new) `ExtensionAPI` instance, with NO network call. Exists
       * for `/reload`: pi rebuilds the extension's tool registry from scratch
       * on reload but keeps this module's top-level state (including the
       * live MCP client) — without this, a session's Probe tools would
       * silently vanish after `/reload` until the next real reconnect.
       */
      reregister: (pi: ExtensionAPI) => number;
    }
  | { skipped: string };

/**
 * Non-interactive: bearer fast path, else previously-saved OAuth tokens if
 * any exist, else degrade immediately (no network call, and no unsolicited
 * login prompt for a device that has never used the OAuth fallback).
 */
export async function connectAndRegisterTools(pi: ExtensionAPI, deps: McpBridgeDeps): Promise<ConnectResult> {
  const url = mcpServerUrl(deps.env);
  const bearer = resolveMcpBearerToken(deps.env);

  let client: McpClientLike;
  if (bearer) {
    try {
      client = await deps.connector.connectWithBearer(url, bearer.token, deps.timeouts.connectMs);
    } catch (err) {
      return { skipped: describeConnectFailure(err, `bearer token (${bearer.source})`) };
    }
  } else {
    const provider = new ProbeOAuthClientProvider({
      storage: deps.storage,
      env: deps.env,
      onAuthorizationUrl: (authUrl) => deps.announce(`Probe MCP re-authorization needed — open this URL:\n${authUrl}`, "warning"),
    });
    if (!provider.tokens()) {
      return {
        skipped:
          "no Probe MCP token found — run `/probe-mcp-login` to authenticate interactively, or `probe login` / `probe mcp token set` on a paired device.",
      };
    }
    try {
      client = await deps.connector.connectWithOAuthProvider(url, provider, deps.timeouts.connectMs);
    } catch (err) {
      return { skipped: describeConnectFailure(err, "stored OAuth credentials") };
    }
  }

  let currentClient = client;
  let tools: McpToolInfo[];
  try {
    tools = (await withTimeout(currentClient.listTools(), deps.timeouts.listToolsMs, "MCP listTools")).tools;
  } catch (err) {
    await currentClient.close().catch(() => {});
    return { skipped: `connected to Probe MCP but failed to list its tools: ${errMessage(err)}` };
  }

  const reconnect = async (): Promise<McpClientLike | null> => {
    const freshBearer = resolveMcpBearerToken(deps.env);
    if (!freshBearer) return null;
    try {
      currentClient = await deps.connector.connectWithBearer(url, freshBearer.token, deps.timeouts.connectMs);
      return currentClient;
    } catch {
      return null;
    }
  };

  const reregister = (targetPi: ExtensionAPI): number =>
    registerTools(targetPi, tools, () => currentClient, reconnect, deps.timeouts.callToolMs, deps.log);
  const registered = reregister(pi);
  deps.log(`probe_mcp: registered ${registered} tool(s) from ${url.toString()}`);
  return { registered, close: () => currentClient.close(), reregister };
}

// ---------------------------------------------------------------------------
// /probe-mcp-login: interactive OAuth fallback
// ---------------------------------------------------------------------------

export interface InteractiveUI {
  hasUI: boolean;
  input: (title: string, placeholder?: string) => Promise<string | undefined>;
}

export interface InteractiveLoginResult {
  status: "connected" | "authorized" | "requires-interactive" | "failed";
  message: string;
  registered?: number;
}

export function extractAuthorizationCode(pasted: string): string | null {
  try {
    const asUrl = new URL(pasted);
    const code = asUrl.searchParams.get("code");
    if (code) return code;
  } catch {
    // Not a URL — fall through to the query-fragment / bare-code cases below.
  }
  const match = pasted.match(/(?:^|[?&])code=([^&\s]+)/);
  if (match) return decodeURIComponent(match[1]);
  return /^[A-Za-z0-9._~-]+$/.test(pasted) ? pasted : null;
}

/**
 * `/probe-mcp-login`'s implementation. Tries the fast path first (so this
 * command doubles as "reconnect Probe MCP now" for anyone already paired);
 * only falls into the OAuth dance when no bearer token exists AND the
 * session has an interactive UI to complete it.
 */
export async function interactiveOAuthLogin(pi: ExtensionAPI, deps: McpBridgeDeps, ui: InteractiveUI): Promise<InteractiveLoginResult> {
  const fastPath = await connectAndRegisterTools(pi, deps);
  if ("registered" in fastPath) {
    return { status: "connected", message: `Probe MCP is available — registered ${fastPath.registered} tool(s).`, registered: fastPath.registered };
  }
  const bearer = resolveMcpBearerToken(deps.env);
  if (bearer) {
    // A bearer token exists but the connection itself failed (network,
    // server down, etc.) — OAuth would not help here, so surface the real
    // failure instead of masking it behind a login prompt.
    return { status: "failed", message: fastPath.skipped };
  }

  if (!ui.hasUI) {
    return {
      status: "requires-interactive",
      message:
        "No Probe MCP token found, and this session has no interactive UI to complete OAuth login. " +
        "Run `probe login` / `probe mcp token set` on a paired device, or retry `/probe-mcp-login` from an interactive pi session.",
    };
  }

  const url = mcpServerUrl(deps.env);
  const provider = new ProbeOAuthClientProvider({
    storage: deps.storage,
    env: deps.env,
    onAuthorizationUrl: (authUrl) => deps.announce(`Open this URL to authorize Probe Research read access for pi:\n${authUrl}`, "info"),
  });

  let start: OAuthLoginStart;
  try {
    start = await deps.connector.startOAuthLogin(url, provider, deps.timeouts.connectMs);
  } catch (err) {
    return { status: "failed", message: `Could not reach Probe MCP: ${errMessage(err)}` };
  }

  let client: McpClientLike;
  let justAuthorized: boolean;
  if (start.authorized) {
    client = start.client;
    justAuthorized = false;
  } else {
    const pasted = await ui.input("Paste the URL you were redirected to (or just the authorization code)", `${provider.redirectUrl}?code=...`);
    if (!pasted || !pasted.trim()) {
      return { status: "failed", message: "Login cancelled — no authorization code provided." };
    }
    const code = extractAuthorizationCode(pasted.trim());
    if (!code) {
      return { status: "failed", message: "Could not find an authorization `code` in what was pasted." };
    }
    try {
      client = await start.finishAuth(code);
    } catch (err) {
      return { status: "failed", message: `Token exchange failed: ${errMessage(err)}` };
    }
    justAuthorized = true;
  }

  let tools: McpToolInfo[];
  try {
    tools = (await withTimeout(client.listTools(), deps.timeouts.listToolsMs, "MCP listTools")).tools;
  } catch (err) {
    return { status: "failed", message: `Signed in, but listing tools failed: ${errMessage(err)}` };
  }

  const registered = registerTools(pi, tools, () => client, async () => null, deps.timeouts.callToolMs, deps.log);
  return justAuthorized
    ? { status: "authorized", message: `Signed in — registered ${registered} Probe MCP tool(s) for this session.`, registered }
    : { status: "connected", message: `Probe MCP was already authorized — registered ${registered} tool(s).`, registered };
}
