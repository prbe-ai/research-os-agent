/**
 * pi's server notices (plan T9, R25): read at session start, shown once per computer to the person
 * (`ctx.ui.notify`) and once per session to the agent (the turn's message), through the seen ledger
 * every client on the computer shares (`serverLedger.ts`). The plugin hooks' twin is
 * `hooks/server_notices.py`; pi asks as the CLI (its Probe is the CLI's), at the CLI's version.
 * Never throws.
 */

import { readActiveProbeContext } from "./mcpAuth.js";
import type { PathEnv } from "./paths.js";
import {
  AGENT_AUDIENCES, DEFAULT_BASE_URL, PERSON_AUDIENCES, ServerAudience, ServerSeverity, renderServerBanner, renderServerText,
} from "./reads.js";
import { Destination, makeMessageKey, MessageLedger, type Claim } from "./serverLedger.js";

/** `probe.foundation.server_messages.MESSAGES_PATH`. */
export const MESSAGES_PATH = "/v1/client/messages";
/** The read's whole budget: a session start never waits on it (it is not awaited), and a hung one ends. */
export const FETCH_TIMEOUT_MS = 2000;
/** `server_notices.FRESH_S`: a list read this recently is reused. */
export const FRESH_S = 60;
/** `server_notices.STALE_S`: an older list is never shown from (the server may have ended its notices). */
export const STALE_S = 86400;
const isYounger = (fetchedAt: number, limit: number, now: number): boolean => now - fetchedAt >= 0 && now - fetchedAt < limit;
/** Who asks: the CLI, at the installed CLI's version (the server matches notices by client kind). */
export const CLIENT_KIND = "cli";
const PLAIN_ID = /^[A-Za-z0-9_.+-]{1,64}$/;
const SEMVER = /^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]{1,40})?$/;
const MAX_TEXT = 500;
/** `server_notices._CONTROL`: characters a terminal or banner would act on. */
const CONTROL = /[\x00-\x09\x0b-\x1f\x7f-\x9f]/g;

/** One notice as pi needs it (`server_notices.read_item`'s twin). */
export interface NoticeItem {
  messageId: string;
  revision: number;
  text: string;
  severity: string;
  audience: string;
}

/** `ctx.ui.notify`'s level for a notice's severity. */
export const NotifyLevel = {
  Error: "error",
  Warning: "warning",
  Info: "info",
} as const;
export type NotifyLevelValue = (typeof NotifyLevel)[keyof typeof NotifyLevel];

export function notifyLevel(severity: string): NotifyLevelValue {
  if (severity === ServerSeverity.Blocking) return NotifyLevel.Error;
  return severity === ServerSeverity.Warning ? NotifyLevel.Warning : NotifyLevel.Info;
}

/** The version in `probe --version`'s output (`probe 0.370.0`: its last word), or null. */
export function parseCliVersion(output: string | null): string | null {
  const words = (output ?? "").trim().split(/\s+/);
  const last = words[words.length - 1] ?? "";
  return SEMVER.test(last) ? last : null;
}

/** One wire `ClientMessage`, or null without a plain id or a sentence. */
export function readItem(item: unknown): NoticeItem | null {
  if (!item || typeof item !== "object" || Array.isArray(item)) return null;
  const data = item as Record<string, unknown>;
  if (typeof data.message_id !== "string" || !PLAIN_ID.test(data.message_id)) return null;
  const text = typeof data.text === "string" ? data.text.replace(CONTROL, "") : "";
  if (!text.trim()) return null;
  const revision = typeof data.revision === "number" && Number.isInteger(data.revision) && data.revision >= 0 ? data.revision : 0;
  const audience = typeof data.audience === "string" && [...PERSON_AUDIENCES, ...AGENT_AUDIENCES].includes(data.audience)
    ? data.audience : ServerAudience.Both;
  const severity = typeof data.severity === "string" ? data.severity : ServerSeverity.Info;
  return { messageId: data.message_id, revision, text: text.slice(0, MAX_TEXT), severity, audience };
}

/** The account the ledger is keyed by, in the SDK's order (`server_notices.read_account`): `PROBE_TOKEN`,
 * the config's `token`, a read-only MCP token only where neither exists; or null. */
export function readAccount(env: PathEnv = process.env): { base: string; token: string } | null {
  const ctx = readActiveProbeContext(env);
  const token = env.PROBE_TOKEN || ctx.token || env.PROBE_MCP_TOKEN || ctx.mcp_token;
  if (!token || typeof token !== "string") return null;
  const base = String(env.PROBE_BASE_URL || ctx.base_url || DEFAULT_BASE_URL).replace(/\/+$/, "");
  return /^https?:\/\//.test(base) ? { base, token } : null;
}

export function ledgerFor(env: PathEnv = process.env): MessageLedger | null {
  const account = readAccount(env);
  return account ? MessageLedger.fromAccount(account.base, account.token, env) : null;
}

export interface FetchNoticesDeps {
  env: PathEnv;
  fetchImpl: typeof fetch;
  /** The installed CLI's `--version` output (`probe 0.370.0`), or null. */
  cliVersion: () => Promise<string | null>;
  now?: () => number;
}

/** The notices the server holds for this computer's CLI: the shared list when fresh and asked for at this
 * CLI version (notices match by version), else one read (saved for every client). Empty when signed out,
 * with no CLI, or unreachable. */
export async function fetchNotices(deps: FetchNoticesDeps): Promise<NoticeItem[]> {
  try {
    const account = readAccount(deps.env);
    if (!account) return [];
    const ledger = MessageLedger.fromAccount(account.base, account.token, deps.env);
    const now = deps.now ? deps.now() : Date.now() / 1000;
    const version = parseCliVersion(await deps.cliVersion());
    if (!version) return [];
    const loaded = ledger.loadList(CLIENT_KIND);
    const saved = loaded && loaded.clientVersion === version ? loaded : null;
    let items = saved && isYounger(saved.fetchedAt, FRESH_S, now) ? saved.items : null;
    if (items === null) {
      const listed = await readList(account, version, deps.fetchImpl);
      if (listed !== null) ledger.saveList(listed.setRevision, listed.items, CLIENT_KIND, now, version);
      items = listed ? listed.items : saved && isYounger(saved.fetchedAt, STALE_S, now) ? saved.items : [];
    }
    return items.map(readItem).filter((item): item is NoticeItem => item !== null);
  } catch {
    return [];
  }
}

async function readList(account: { base: string; token: string }, version: string, fetchImpl: typeof fetch): Promise<{ setRevision: string | null; items: unknown[] } | null> {
  if (typeof fetchImpl !== "function") return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  timer.unref?.();
  try {
    const response = await fetchImpl(account.base + MESSAGES_PATH, {
      headers: {
        Authorization: `Bearer ${account.token}`, Accept: "application/json",
        "X-Probe-Client": CLIENT_KIND, "X-Probe-Client-Version": version,
      },
      redirect: "error",
      signal: controller.signal,
    });
    if (!response.ok) {
      await response.body?.cancel?.().catch(() => undefined);
      return null;
    }
    const body = (await response.json()) as { set_revision?: unknown; messages?: unknown };
    if (!Array.isArray(body?.messages)) return null;
    // Saved with its set revision, so the CLI's next answer reuses it instead of reading it again.
    return { setRevision: typeof body.set_revision === "string" ? body.set_revision : null, items: body.messages };
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** Show the person each notice for them they have not seen on this computer. Returns how many. */
export function notifyPerson(items: NoticeItem[], ledger: MessageLedger | null, notify: (text: string, level: NotifyLevelValue) => void): number {
  if (!ledger) return 0;
  let shown = 0;
  for (const item of items) {
    if (!PERSON_AUDIENCES.includes(item.audience)) continue;
    const claim = ledger.claim(makeMessageKey(item.messageId, item.revision), Destination.Person);
    if (!claim) continue;
    try {
      notify(renderServerBanner(item.text), notifyLevel(item.severity));
    } catch {
      claim.release();
      continue;
    }
    claim.commit();
    shown += 1;
  }
  return shown;
}

/** The agent's texts for the notices this session's agent was not told yet, from the saved list (no
 * network: a prompt never waits), each claimed: commit once the turn's message is handed to pi. */
export function takeAgentNotices(sessionId: string, env: PathEnv = process.env, now = Date.now() / 1000): { texts: string[]; claims: Claim[] } {
  const out = { texts: [] as string[], claims: [] as Claim[] };
  const ledger = ledgerFor(env);
  const saved = ledger?.loadList(CLIENT_KIND);
  if (!ledger || !saved || !sessionId || !isYounger(saved.fetchedAt, STALE_S, now)) return out;
  for (const item of saved.items.map(readItem)) {
    if (!item || !AGENT_AUDIENCES.includes(item.audience)) continue;
    const claim = ledger.claim(makeMessageKey(item.messageId, item.revision), Destination.Agent, sessionId);
    if (!claim) continue;
    out.texts.push(renderServerText(item.text));
    out.claims.push(claim);
  }
  return out;
}
