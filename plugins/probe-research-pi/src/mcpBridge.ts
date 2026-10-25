/**
 * The bridge: connect to the Probe Research read MCP server and offer each of
 * its tools as a native pi tool. It registers nothing itself: it returns the
 * connection (`McpTransport`) and the tools as DECLARATIONS built over it
 * (`mcpToolDeclarations`), which the shim (`src/shim.ts`), the only code that
 * registers with pi, registers. Two entry points:
 *
 *   `connectMcp()` — the NON-interactive path, called from `session_start`.
 *   Tries the bearer fast path (`mcpAuth.ts`), then previously-saved OAuth
 *   tokens if any exist, and otherwise degrades immediately with no network
 *   call at all. Every network step is bounded (see `withTimeout`) so a slow
 *   or hung server can never stall the session — pi wraps extension handlers
 *   in a bare try/catch with NO timeout of its own (verified against
 *   `dist/core/extensions/runner.js`), so the bound has to come from here.
 *
 *   `interactiveOAuthConnect()` — the OAuth fallback, called only from the
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
 * A CONNECTION OUTLIVES THE CODE THAT OPENED IT (`src/shim.ts`): a newer
 * version of this package can take over a running pi at the next turn, and
 * it must not reconnect, nor keep calling the old version's code. So the
 * connection is data -- the client, the tools it listed, how a call
 * re-authenticates -- and each version builds its own tools over it.
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

import { resolveMcpBearerToken } from "./core/mcpAuth.js";
import { workspaceLimitHeaders } from "./core/workspaceLimit.js";
import { defaultOAuthStorageDeps, ProbeOAuthClientProvider, type OAuthStorageDeps } from "./mcpOAuth.js";
import { jsonSchemaToTypeBox, prefixToolName } from "./mcpSchema.js";
import { packageVersion } from "./packageVersion.js";
import { MCP_SERVER_URL, MCP_URL_ENV, type PathEnv } from "./core/paths.js";
import { WIZARD_HINT } from "./core/wizardHint.js";

const CLIENT_INFO = { name: "probe-research-pi", version: packageVersion() };

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
  /** `headers`: sent beside the bearer on every request -- this computer's workspace limit (`core/workspaceLimit.ts`). */
  connectWithBearer(url: URL, token: string, timeoutMs: number, headers?: Record<string, string>): Promise<McpClientLike>;
  connectWithOAuthProvider(
    url: URL,
    provider: ProbeOAuthClientProvider,
    timeoutMs: number,
    headers?: Record<string, string>,
  ): Promise<McpClientLike>;
  /**
   * Attempt a connection using `authProvider`. If stored tokens are already
   * valid, resolves `{ authorized: true, client }` directly. If the server
   * demands a fresh login (`UnauthorizedError` — by which point
   * `provider.redirectToAuthorization` has already fired), resolves
   * `{ authorized: false, finishAuth }`, where `finishAuth(code)` completes
   * the exchange and reconnects. Rejects for any other failure (network,
   * timeout, ...).
   */
  startOAuthLogin(
    url: URL,
    provider: ProbeOAuthClientProvider,
    timeoutMs: number,
    headers?: Record<string, string>,
  ): Promise<OAuthLoginStart>;
}

async function realConnect(url: URL, transport: StreamableHTTPClientTransport, timeoutMs: number): Promise<Client> {
  const client = new Client(CLIENT_INFO, { capabilities: {} });
  await withTimeout(client.connect(transport), timeoutMs, `MCP connect (${url.toString()})`);
  return client;
}

export const realMcpConnector: McpConnector = {
  async connectWithBearer(url, token, timeoutMs, headers = {}) {
    const transport = new StreamableHTTPClientTransport(url, {
      requestInit: { headers: { ...headers, Authorization: `Bearer ${token}` } },
    });
    return realConnect(url, transport, timeoutMs);
  },

  async connectWithOAuthProvider(url, provider, timeoutMs, headers = {}) {
    const transport = new StreamableHTTPClientTransport(url, { authProvider: provider, requestInit: { headers } });
    return realConnect(url, transport, timeoutMs);
  },

  async startOAuthLogin(url, provider, timeoutMs, headers = {}) {
    // The workspace limit rides the login's connection AND the one after the
    // code exchange, exactly as on the non-interactive path.
    const transport = new StreamableHTTPClientTransport(url, { authProvider: provider, requestInit: { headers } });
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
          const retryTransport = new StreamableHTTPClientTransport(url, { authProvider: provider, requestInit: { headers } });
          return realConnect(url, retryTransport, timeoutMs);
        },
      };
    }
  },
};

// ---------------------------------------------------------------------------
// Registering tools
// ---------------------------------------------------------------------------

/** How a connection's tools re-authenticate after a 401/403 from a call. */
export const McpReconnect = {
  /** Re-resolve the bearer token and connect again, once (the non-interactive path). */
  Bearer: "bearer",
  /** Give up: a fresh OAuth sign-in has no silent way back (`/probe-mcp-login`). */
  None: "none",
} as const;
export type McpReconnectValue = (typeof McpReconnect)[keyof typeof McpReconnect];

/**
 * One live Probe MCP connection, as data: the client, the tools it listed,
 * the server it reached and how a call re-authenticates. Never a closure of
 * this module's, so it can outlive the version of this package that opened it
 * (see the module docstring). `client` is replaced in place when a call
 * re-authenticates, so every tool built over it then uses the new one.
 */
export interface McpTransport {
  client: McpClientLike;
  tools: McpToolInfo[];
  url: string;
  reconnect: McpReconnectValue;
}

/** A pi tool for one MCP tool: what `pi.registerTool` takes. */
export interface McpToolDeclaration {
  name: string;
  label: string;
  description: string;
  parameters: ReturnType<typeof jsonSchemaToTypeBox>;
  execute(toolCallId: string, params: unknown): Promise<{ content: PiContent[]; details: { mcpTool: string } }>;
}

/** What a tool call needs from the bridge deps. */
export type McpToolDeps = Pick<McpBridgeDeps, "env" | "connector" | "timeouts" | "log">;

/** The bearer reconnect: a fresh client, put in place on the transport, or null to give up. */
async function reconnectTransport(transport: McpTransport, deps: McpToolDeps): Promise<McpClientLike | null> {
  if (transport.reconnect !== McpReconnect.Bearer) return null;
  const freshBearer = resolveMcpBearerToken(deps.env);
  if (!freshBearer) return null;
  try {
    transport.client = await deps.connector.connectWithBearer(
      new URL(transport.url),
      freshBearer.token,
      deps.timeouts.connectMs,
      workspaceLimitHeaders(deps.env),
    );
    return transport.client;
  } catch {
    return null;
  }
}

/**
 * The pi tools for every tool the connection listed, each calling the
 * transport's CURRENT client; a 401/403 re-authenticates once (`reconnect`).
 * Pure: builds declarations, registers nothing.
 */
export function mcpToolDeclarations(transport: McpTransport, deps: McpToolDeps): McpToolDeclaration[] {
  return transport.tools.map((tool) => ({
    name: prefixToolName(tool.name),
    label: tool.title ?? tool.name,
    description: (tool.description ?? `Probe Research MCP tool: ${tool.name}`).slice(0, 4000),
    parameters: jsonSchemaToTypeBox(tool.inputSchema),
    async execute(_toolCallId: string, params: unknown) {
      try {
        return await callMcpTool(transport.client, tool.name, params, deps.timeouts.callToolMs);
      } catch (err) {
        if (!isAuthError(err)) throw err;
        deps.log(`probe_mcp: "${tool.name}" got an auth error (${errMessage(err)}); re-resolving credentials and retrying once`);
        const fresh = await reconnectTransport(transport, deps);
        if (!fresh) {
          throw new Error(
            `Probe MCP tool "${tool.name}" failed: not authorized, and credentials could not be re-resolved. ` +
              `To fix it, ${WIZARD_HINT}, or run \`/probe-mcp-login\` for an interactive re-authentication.`,
          );
        }
        return await callMcpTool(fresh, tool.name, params, deps.timeouts.callToolMs);
      }
    },
  }));
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

/**
 * Non-interactive: bearer fast path, else previously-saved OAuth tokens if
 * any exist, else degrade immediately (no network call, and no unsolicited
 * login prompt for a device that has never used the OAuth fallback). Connects
 * and lists the tools; registers nothing (`mcpToolDeclarations`).
 */
export async function connectMcp(deps: McpBridgeDeps): Promise<{ transport: McpTransport } | { skipped: string }> {
  const url = mcpServerUrl(deps.env);
  const bearer = resolveMcpBearerToken(deps.env);

  let client: McpClientLike;
  // This computer's workspace limit, read again on every (re)connect like the token.
  if (bearer) {
    try {
      client = await deps.connector.connectWithBearer(url, bearer.token, deps.timeouts.connectMs, workspaceLimitHeaders(deps.env));
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
          `no Probe MCP token found — run \`/probe-mcp-login\` to authenticate interactively, or ${WIZARD_HINT}.`,
      };
    }
    try {
      client = await deps.connector.connectWithOAuthProvider(url, provider, deps.timeouts.connectMs, workspaceLimitHeaders(deps.env));
    } catch (err) {
      return { skipped: describeConnectFailure(err, "stored OAuth credentials") };
    }
  }

  let tools: McpToolInfo[];
  try {
    tools = (await withTimeout(client.listTools(), deps.timeouts.listToolsMs, "MCP listTools")).tools;
  } catch (err) {
    await client.close().catch(() => {});
    return { skipped: `connected to Probe MCP but failed to list its tools: ${errMessage(err)}` };
  }
  deps.log(`probe_mcp: listed ${tools.length} tool(s) from ${url.toString()}`);
  return { transport: { client, tools, url: url.toString(), reconnect: McpReconnect.Bearer } };
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
 * session has an interactive UI to complete it. Registers nothing: a
 * connection it makes comes back as `transport`.
 */
export async function interactiveOAuthConnect(
  deps: McpBridgeDeps,
  ui: InteractiveUI,
): Promise<InteractiveLoginResult & { transport?: McpTransport }> {
  const fastPath = await connectMcp(deps);
  if ("transport" in fastPath) {
    const registered = fastPath.transport.tools.length;
    return {
      status: "connected",
      message: `Probe MCP is available — registered ${registered} tool(s).`,
      registered,
      transport: fastPath.transport,
    };
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
        `To fix it, ${WIZARD_HINT}, or retry \`/probe-mcp-login\` from an interactive pi session.`,
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
    start = await deps.connector.startOAuthLogin(url, provider, deps.timeouts.connectMs, workspaceLimitHeaders(deps.env));
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

  const transport: McpTransport = { client, tools, url: url.toString(), reconnect: McpReconnect.None };
  const registered = tools.length;
  return justAuthorized
    ? { status: "authorized", message: `Signed in — registered ${registered} Probe MCP tool(s) for this session.`, registered, transport }
    : { status: "connected", message: `Probe MCP was already authorized — registered ${registered} tool(s).`, registered, transport };
}
