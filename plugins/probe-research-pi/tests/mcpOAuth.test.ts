import { describe, expect, it, vi } from "vitest";

import { OAUTH_REDIRECT_URL, ProbeOAuthClientProvider, type OAuthStorageDeps } from "../src/mcpOAuth.js";
import { mcpOAuthStateFile } from "../src/paths.js";

/** In-memory storage standing in for real fs — no test in this file ever touches disk. */
function fakeStorage(): OAuthStorageDeps & { files: Map<string, string> } {
  const files = new Map<string, string>();
  return {
    files,
    readFileSync: (path) => {
      const v = files.get(path);
      if (v === undefined) throw Object.assign(new Error(`ENOENT: ${path}`), { code: "ENOENT" });
      return v;
    },
    writeFileSync: (path, content) => {
      files.set(path, content);
    },
    mkdirSync: () => {},
  };
}

function makeProvider(overrides: { storage?: OAuthStorageDeps; onAuthorizationUrl?: (url: string) => void } = {}) {
  const onAuthorizationUrl = overrides.onAuthorizationUrl ?? vi.fn();
  const storage = overrides.storage ?? fakeStorage();
  const env = { PI_CODING_AGENT_DIR: "/fake/pi-agent-dir" };
  return { provider: new ProbeOAuthClientProvider({ storage, env, onAuthorizationUrl }), storage, env, onAuthorizationUrl };
}

describe("ProbeOAuthClientProvider", () => {
  it("has no client info or tokens before anything is saved", () => {
    const { provider } = makeProvider();
    expect(provider.clientInformation()).toBeUndefined();
    expect(provider.tokens()).toBeUndefined();
  });

  it("round-trips saved client information", () => {
    const { provider } = makeProvider();
    const info = { client_id: "abc123", redirect_uris: [OAUTH_REDIRECT_URL] };

    provider.saveClientInformation(info as never);

    expect(provider.clientInformation()).toEqual(info);
  });

  it("round-trips saved tokens", () => {
    const { provider } = makeProvider();
    const tokens = { access_token: "at-1", token_type: "bearer", refresh_token: "rt-1", expires_in: 3600 };

    provider.saveTokens(tokens);

    expect(provider.tokens()).toEqual(tokens);
  });

  it("saving client information does not clobber previously saved tokens, and vice versa", () => {
    const { provider } = makeProvider();
    provider.saveTokens({ access_token: "at-1", token_type: "bearer" });
    provider.saveClientInformation({ client_id: "abc123", redirect_uris: [OAUTH_REDIRECT_URL] } as never);

    expect(provider.tokens()).toEqual({ access_token: "at-1", token_type: "bearer" });
    expect(provider.clientInformation()).toEqual({ client_id: "abc123", redirect_uris: [OAUTH_REDIRECT_URL] });
  });

  it("persists to the path mcpOAuthStateFile() computes for the given env, not somewhere ad hoc", () => {
    const storage = fakeStorage();
    const env = { PI_CODING_AGENT_DIR: "/fake/pi-agent-dir" };
    const provider = new ProbeOAuthClientProvider({ storage, env, onAuthorizationUrl: vi.fn() });

    provider.saveTokens({ access_token: "at-1", token_type: "bearer" });

    expect(storage.files.has(mcpOAuthStateFile(env))).toBe(true);
  });

  it("exposes a fixed, syntactically valid loopback redirect URL that nothing needs to listen on", () => {
    const { provider } = makeProvider();
    expect(provider.redirectUrl).toBe(OAUTH_REDIRECT_URL);
    expect(() => new URL(provider.redirectUrl)).not.toThrow();
    expect(new URL(provider.redirectUrl).hostname).toBe("127.0.0.1");
  });

  it("clientMetadata declares the public-client, authorization_code + refresh_token shape the real server expects", () => {
    const { provider } = makeProvider();
    const metadata = provider.clientMetadata;
    expect(metadata.redirect_uris).toEqual([OAUTH_REDIRECT_URL]);
    expect(metadata.token_endpoint_auth_method).toBe("none");
    expect(metadata.grant_types).toEqual(["authorization_code", "refresh_token"]);
  });

  it("redirectToAuthorization reports the URL through the injected callback and never throws", () => {
    const onAuthorizationUrl = vi.fn();
    const { provider } = makeProvider({ onAuthorizationUrl });

    provider.redirectToAuthorization(new URL("https://api.research.prbe.ai/oauth/authorize?client_id=x"));

    expect(onAuthorizationUrl).toHaveBeenCalledWith("https://api.research.prbe.ai/oauth/authorize?client_id=x");
  });

  it("codeVerifier round-trips a saved verifier", () => {
    const { provider } = makeProvider();
    provider.saveCodeVerifier("verifier-abc");
    expect(provider.codeVerifier()).toBe("verifier-abc");
  });

  it("codeVerifier throws when nothing was saved yet, rather than returning an empty/undefined value", () => {
    const { provider } = makeProvider();
    expect(() => provider.codeVerifier()).toThrow(/no pending PKCE code verifier/i);
  });

  describe("invalidateCredentials", () => {
    it("'tokens' clears only tokens", () => {
      const { provider } = makeProvider();
      provider.saveTokens({ access_token: "at-1", token_type: "bearer" });
      provider.saveClientInformation({ client_id: "abc", redirect_uris: [OAUTH_REDIRECT_URL] } as never);

      provider.invalidateCredentials("tokens");

      expect(provider.tokens()).toBeUndefined();
      expect(provider.clientInformation()).toBeDefined();
    });

    it("'client' clears only client information", () => {
      const { provider } = makeProvider();
      provider.saveTokens({ access_token: "at-1", token_type: "bearer" });
      provider.saveClientInformation({ client_id: "abc", redirect_uris: [OAUTH_REDIRECT_URL] } as never);

      provider.invalidateCredentials("client");

      expect(provider.clientInformation()).toBeUndefined();
      expect(provider.tokens()).toBeDefined();
    });

    it("'all' clears both tokens and client information", () => {
      const { provider } = makeProvider();
      provider.saveTokens({ access_token: "at-1", token_type: "bearer" });
      provider.saveClientInformation({ client_id: "abc", redirect_uris: [OAUTH_REDIRECT_URL] } as never);

      provider.invalidateCredentials("all");

      expect(provider.tokens()).toBeUndefined();
      expect(provider.clientInformation()).toBeUndefined();
    });

    it("'verifier' clears the pending code verifier without touching persisted state", () => {
      const { provider } = makeProvider();
      provider.saveTokens({ access_token: "at-1", token_type: "bearer" });
      provider.saveCodeVerifier("verifier-abc");

      provider.invalidateCredentials("verifier");

      expect(provider.tokens()).toBeDefined();
      expect(() => provider.codeVerifier()).toThrow();
    });
  });
});
