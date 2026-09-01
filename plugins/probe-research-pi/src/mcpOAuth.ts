/**
 * The Probe MCP fallback path: a real `OAuthClientProvider` (the
 * `@modelcontextprotocol/sdk` interface — see `client/auth.js`) for when no
 * bearer token exists anywhere `mcpAuth.ts` looks. This module implements
 * STORAGE ONLY, per the spec: PKCE, discovery, and dynamic client
 * registration are the SDK's job (`StreamableHTTPClientTransport`'s
 * `authProvider` option, and `transport.finishAuth()`); nothing here
 * hand-rolls a byte of the OAuth protocol.
 *
 * Verified live against the real server (unauthenticated `.well-known`
 * reads only — see the plan's "Verified facts" section for why that is safe
 * to do outside of a test): `https://mcp.research.prbe.ai/.well-known/oauth-protected-resource`
 * names `https://api.research.prbe.ai` as the authorization server, whose
 * `/.well-known/oauth-authorization-server` advertises
 * `grant_types_supported: ["authorization_code", "refresh_token"]`,
 * `code_challenge_methods_supported: ["S256"]`,
 * `token_endpoint_auth_methods_supported: ["none"]` (public client), and a
 * `registration_endpoint` (RFC 7591 dynamic client registration). There is
 * no `device_code` or `client_credentials` grant — this is the standard
 * browser-redirect + PKCE flow, which needs a human to visit a URL and
 * approve, once.
 *
 * NO LOCAL CALLBACK SERVER, NO BROWSER AUTOMATION. The obvious way to
 * complete an authorization-code flow from a CLI is to open a browser and
 * listen on a loopback port for the redirect — that is real infrastructure
 * (a bound port, signal handling, a browser-launch command per platform),
 * it is exactly what the brief's escape hatch calls "infrastructure that
 * does not belong in a coding-agent extension," and it is unusable from a
 * pi session with no local display (SSH, a container, `pi -p`) — the
 * majority of scripted pi usage this whole project cares about. So this
 * follows pi's OWN precedent instead: `examples/extensions/custom-provider-gitlab-duo/index.ts`,
 * pi's canonical OAuth example, does not run a server either. It prints the
 * authorization URL, then blocks on `ctx.ui.input()` for the user to paste
 * back the URL they land on. `redirect_uri` still has to be SOME syntactically
 * valid loopback URL for the auth server's dynamic client registration to
 * accept — nothing needs to be listening on it, because the human copies the
 * address bar rather than a server catching the request. See
 * `mcpBridge.ts`'s `interactiveOAuthLogin()` for the paste-back prompt this
 * provider's `redirectToAuthorization` sets up.
 *
 * This provider's `redirectToAuthorization` NEVER blocks and never opens a
 * browser itself — it only reports the URL through an injected callback
 * (`notify`), because it runs deep inside `client.connect()` and the
 * SDK docs promise a synchronous-ish `UnauthorizedError` right after it
 * fires. The actual "wait for the user" step lives one layer up, in the
 * command handler, which is allowed to block because a human just typed a
 * command and is watching.
 */

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import type { OAuthClientInformationFull, OAuthClientInformationMixed, OAuthClientMetadata, OAuthTokens } from "@modelcontextprotocol/sdk/shared/auth.js";
import type { OAuthClientProvider } from "@modelcontextprotocol/sdk/client/auth.js";

import { mcpOAuthStateFile, type PathEnv } from "./paths.js";

/**
 * A syntactically valid loopback redirect URI. Nothing listens on it — see
 * the module docstring. Path/port are arbitrary but stable, so a
 * previously-registered OAuth client (see `saveClientInformation`) stays
 * valid across runs instead of needing re-registration every time.
 */
export const OAUTH_REDIRECT_URL = "http://127.0.0.1:8945/probe-research-pi/callback";
const OAUTH_SCOPE = "research:read";

interface StoredOAuthState {
  clientInformation?: OAuthClientInformationFull;
  tokens?: OAuthTokens;
}

export interface OAuthStorageDeps {
  readFileSync: (path: string) => string;
  writeFileSync: (path: string, content: string) => void;
  mkdirSync: (path: string) => void;
}

export const defaultOAuthStorageDeps: OAuthStorageDeps = {
  readFileSync: (path) => readFileSync(path, "utf-8"),
  writeFileSync: (path, content) => writeFileSync(path, content),
  mkdirSync: (path) => mkdirSync(path, { recursive: true }),
};

function readState(path: string, deps: OAuthStorageDeps): StoredOAuthState {
  try {
    const raw = deps.readFileSync(path);
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as StoredOAuthState;
    }
    return {};
  } catch {
    return {};
  }
}

function writeState(path: string, state: StoredOAuthState, deps: OAuthStorageDeps): void {
  deps.mkdirSync(dirname(path));
  deps.writeFileSync(path, JSON.stringify(state, null, 2));
}

export interface ProbeOAuthClientProviderDeps {
  storage: OAuthStorageDeps;
  env: PathEnv;
  /** Called with the authorization URL the researcher needs to open. Never blocks, never throws. */
  onAuthorizationUrl: (url: string) => void;
}

/**
 * `OAuthClientProvider` implementation: storage for client registration,
 * tokens, and the PKCE code verifier, plus the (non-blocking) authorization
 * prompt hook. Everything else — discovery, PKCE generation, dynamic client
 * registration, token exchange, refresh — is the SDK's.
 */
export class ProbeOAuthClientProvider implements OAuthClientProvider {
  private readonly path: string;
  private readonly deps: ProbeOAuthClientProviderDeps;
  // In-memory only, deliberately: the verifier is a short-lived secret that
  // only needs to survive the gap between redirectToAuthorization() firing
  // and the paste-back prompt resolving, both inside the same interactive
  // command invocation. Persisting it to disk would leave a used-once
  // secret lying around for no benefit.
  private pendingCodeVerifier: string | undefined;

  constructor(deps: ProbeOAuthClientProviderDeps) {
    this.deps = deps;
    this.path = mcpOAuthStateFile(deps.env);
  }

  get redirectUrl(): string {
    return OAUTH_REDIRECT_URL;
  }

  get clientMetadata(): OAuthClientMetadata {
    return {
      redirect_uris: [OAUTH_REDIRECT_URL],
      token_endpoint_auth_method: "none",
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
      client_name: "probe-research-pi",
      scope: OAUTH_SCOPE,
    };
  }

  clientInformation(): OAuthClientInformationMixed | undefined {
    return readState(this.path, this.deps.storage).clientInformation;
  }

  saveClientInformation(clientInformation: OAuthClientInformationFull): void {
    const state = readState(this.path, this.deps.storage);
    state.clientInformation = clientInformation;
    writeState(this.path, state, this.deps.storage);
  }

  tokens(): OAuthTokens | undefined {
    return readState(this.path, this.deps.storage).tokens;
  }

  saveTokens(tokens: OAuthTokens): void {
    const state = readState(this.path, this.deps.storage);
    state.tokens = tokens;
    writeState(this.path, state, this.deps.storage);
  }

  redirectToAuthorization(authorizationUrl: URL): void {
    this.deps.onAuthorizationUrl(authorizationUrl.toString());
  }

  saveCodeVerifier(codeVerifier: string): void {
    this.pendingCodeVerifier = codeVerifier;
  }

  codeVerifier(): string {
    if (!this.pendingCodeVerifier) {
      throw new Error("probe-research-pi: no pending PKCE code verifier — call redirectToAuthorization's flow first");
    }
    return this.pendingCodeVerifier;
  }

  invalidateCredentials(scope: "all" | "client" | "tokens" | "verifier" | "discovery"): void {
    if (scope === "verifier") {
      this.pendingCodeVerifier = undefined;
      return;
    }
    const state = readState(this.path, this.deps.storage);
    if (scope === "all" || scope === "client") delete state.clientInformation;
    if (scope === "all" || scope === "tokens") delete state.tokens;
    writeState(this.path, state, this.deps.storage);
    if (scope === "all") this.pendingCodeVerifier = undefined;
  }
}
