/**
 * pi's server notices (plan T9, R19, R25): the seen ledger is the Python one's twin over the same
 * files (`agent/tests/fixtures/server_notices.json` pins the keys and texts for both), the person is
 * told once per computer, each session's agent once, and the mailbox's server messages have their
 * own lane with a notice for the person.
 */
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Mailbox, renderServerBanner, renderServerText } from "../src/core/reads.js";
import { CLAIM_TTL_S, Destination, makeAccountKey, makeMessageKey, MessageLedger } from "../src/core/serverLedger.js";
import { fetchNotices, ledgerFor, notifyPerson, parseCliVersion, readAccount, readItem, takeAgentNotices, type NoticeItem } from "../src/core/serverNotices.js";

const FIXTURE = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/server_notices.json", import.meta.url), "utf-8"),
) as {
  account_keys: Array<{ base_url: string; token: string; key: string }>;
  message_keys: Array<{ message_id: string; revision: number; key: string }>;
  texts: Array<{ text: string; agent: string; person: string }>;
};

const SID = "11111111-2222-3333-4444-555555555555";
const OTHER = "99999999-8888-7777-6666-555555555555";
const MAINTENANCE = {
  message_id: "maintenance_nov3", code: "maintenance", severity: "warning", action: "none", audience: "both",
  text: "Probe is read-only 02:00-03:00 UTC on Nov 3.", revision: 7,
};
let tmp: string;
let env: Record<string, string>;

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-notices-"));
  const config = join(tmp, "config.json");
  writeFileSync(config, JSON.stringify({ base_url: "https://probe.test/", token: "probe_pat_pi" }));
  env = { XDG_STATE_HOME: join(tmp, "state"), PROBE_CONFIG_PATH: config };
});
afterEach(() => rmSync(tmp, { recursive: true, force: true }));

function fakeFetch(messages: unknown[], seen: Array<{ url: string; headers: Record<string, string> }> = [], status = 200): typeof fetch {
  return (async (url: string, init: { headers: Record<string, string> }) => {
    seen.push({ url, headers: init.headers });
    return new Response(JSON.stringify({ set_revision: "0123456789abcdef", messages }), { status });
  }) as unknown as typeof fetch;
}

describe("the seen ledger is the Python one's twin", () => {
  it("makes the same keys", () => {
    for (const c of FIXTURE.account_keys) expect(makeAccountKey(c.base_url, c.token)).toBe(c.key);
    for (const c of FIXTURE.message_keys) expect(makeMessageKey(c.message_id, c.revision)).toBe(c.key);
  });

  it("shows the same texts", () => {
    for (const c of FIXTURE.texts) {
      expect(renderServerText(c.text)).toBe(c.agent);
      expect(renderServerBanner(c.text)).toBe(c.person);
    }
  });

  it("claims once per destination and scope, gives back, and takes over a stale claim", () => {
    const ledger = MessageLedger.fromAccount("https://probe.test", "probe_pat_pi", env);
    expect(ledger.folder).toBe(join(tmp, "state", "probe", "server-messages", makeAccountKey("https://probe.test", "probe_pat_pi")));
    const key = makeMessageKey("maintenance_nov3", 7);
    const claim = ledger.claim(key, Destination.Person)!;
    expect(ledger.claim(key, Destination.Person)).toBeNull();
    claim.release();
    const stuck = ledger.claim(key, Destination.Person)!;
    const old = Date.now() / 1000 - CLAIM_TTL_S - 5;
    utimesSync(stuck.path, old, old);
    const taken = ledger.claim(key, Destination.Person)!;
    expect(taken).not.toBeNull();
    taken.commit();
    expect(ledger.hasShown(key, Destination.Person)).toBe(true);
    expect(ledger.claim(key, Destination.Person)).toBeNull();
    expect(ledger.claim(key, Destination.Agent, SID)).not.toBeNull();
    expect(ledger.claim(key, Destination.Agent, "")).toBeNull();
  });

  it("honours a claim the Python side made (the same file)", () => {
    const ledger = ledgerFor(env)!;
    const key = makeMessageKey("maintenance_nov3", 7);
    mkdirSync(join(ledger.folder, "person"), { recursive: true });
    writeFileSync(join(ledger.folder, "person", `${key}.shown`), "");
    expect(ledger.claim(key, Destination.Person)).toBeNull();
  });
});

describe("pi's session-start read", () => {
  it("asks as the CLI at its version, saves the list for every client, and reuses it", async () => {
    const seen: Array<{ url: string; headers: Record<string, string> }> = [];
    const deps = { env, fetchImpl: fakeFetch([MAINTENANCE, { message_id: "bad id", text: "x" }], seen), cliVersion: async () => "probe 0.370.0\n" };
    const items = await fetchNotices(deps);
    expect(items.map((item) => item.messageId)).toEqual(["maintenance_nov3"]);
    expect(seen).toHaveLength(1);
    expect(seen[0].url).toBe("https://probe.test/v1/client/messages");
    expect(seen[0].headers).toMatchObject({ Authorization: "Bearer probe_pat_pi", "X-Probe-Client": "cli", "X-Probe-Client-Version": "0.370.0" });
    expect(await fetchNotices(deps)).toHaveLength(1);
    expect(seen).toHaveLength(1);
    expect(ledgerFor(env)!.loadList("cli")!.items).toHaveLength(2);
    // Saved with its set revision: the CLI's next answer naming that set reuses it, no second read.
    expect(ledgerFor(env)!.loadList("cli")!.setRevision).toBe("0123456789abcdef");
  });

  it("reads the version off `probe --version` and never shows from another version's list", async () => {
    expect(parseCliVersion("probe 0.370.0\n")).toBe("0.370.0");
    expect(parseCliVersion("0.371.0-rc.1")).toBe("0.371.0-rc.1");
    expect(parseCliVersion("probe: command not found")).toBeNull();
    const seen: Array<{ url: string; headers: Record<string, string> }> = [];
    await fetchNotices({ env, fetchImpl: fakeFetch([MAINTENANCE], seen), cliVersion: async () => "probe 0.370.0" });
    // Upgraded within FRESH_S: the old version's list (its "please update") is not this one's.
    expect(await fetchNotices({ env, fetchImpl: fakeFetch([], seen), cliVersion: async () => "probe 0.371.0" })).toEqual([]);
    expect(seen.map((s) => s.headers["X-Probe-Client-Version"])).toEqual(["0.370.0", "0.371.0"]);
    expect(ledgerFor(env)!.loadList("cli")!.clientVersion).toBe("0.371.0");
  });

  it("keys the ledger by the CLI's token, never an exported read-only MCP token beside it", () => {
    expect(readAccount({ ...env, PROBE_MCP_TOKEN: "probe_mcp_other" })).toEqual({ base: "https://probe.test", token: "probe_pat_pi" });
    expect(readAccount({ ...env, PROBE_TOKEN: "probe_pat_env" })!.token).toBe("probe_pat_env");
  });

  it("drops characters a terminal or banner would act on", () => {
    const esc = String.fromCharCode(0x1b), bel = String.fromCharCode(0x07), csi = String.fromCharCode(0x9b);
    expect(readItem({ ...MAINTENANCE, text: `${esc}]52;c;ZXZpbA==${bel}Update${csi} now` })!.text).toBe("]52;c;ZXZpbA==Update now");
    expect(readItem({ ...MAINTENANCE, text: `${esc}${bel}` })).toBeNull();
  });

  it("reads nothing signed out, with no CLI version, or from an older server", async () => {
    const seen: Array<{ url: string; headers: Record<string, string> }> = [];
    expect(await fetchNotices({ env: { XDG_STATE_HOME: env.XDG_STATE_HOME }, fetchImpl: fakeFetch([MAINTENANCE], seen), cliVersion: async () => "probe 0.370.0\n" })).toEqual([]);
    expect(await fetchNotices({ env, fetchImpl: fakeFetch([MAINTENANCE], seen), cliVersion: async () => null })).toEqual([]);
    expect(seen).toEqual([]);
    expect(await fetchNotices({ env, fetchImpl: fakeFetch([MAINTENANCE], seen, 404), cliVersion: async () => "probe 0.370.0\n" })).toEqual([]);
  });

  it("tells the person once per computer, and gives a notice back when it could not be shown", () => {
    const items = [MAINTENANCE, { ...MAINTENANCE, message_id: "agents", audience: "agent" }].map(readItem) as NoticeItem[];
    const shown: Array<[string, string]> = [];
    const ledger = ledgerFor(env);
    expect(notifyPerson(items, ledger, () => { throw new Error("no UI"); })).toBe(0);
    expect(notifyPerson(items, ledger, (text, level) => shown.push([text, level]))).toBe(1);
    expect(notifyPerson(items, ledger, (text, level) => shown.push([text, level]))).toBe(0);
    expect(shown).toEqual([["Probe: Probe is read-only 02:00-03:00 UTC on Nov 3.", "warning"]]);
  });

  it("tells each session's agent once, from the saved list", async () => {
    await fetchNotices({ env, fetchImpl: fakeFetch([MAINTENANCE, { ...MAINTENANCE, message_id: "people", audience: "person" }]), cliVersion: async () => "probe 0.370.0\n" });
    const first = takeAgentNotices(SID, env);
    expect(first.texts).toEqual([renderServerText(MAINTENANCE.text)]);
    for (const claim of first.claims) claim.commit();
    expect(takeAgentNotices(SID, env).texts).toEqual([]);
    expect(takeAgentNotices(OTHER, env).texts).toEqual([renderServerText(MAINTENANCE.text)]);
    // A day-old list is never shown from.
    expect(takeAgentNotices(OTHER, env, Date.now() / 1000 + 2 * 86400).texts).toEqual([]);
  });
});

describe("the mailbox's server lane in pi", () => {
  let box: Mailbox;
  beforeEach(() => {
    box = new Mailbox(join(tmp, "state", "probe", "reads"), join(tmp, "sessions"), SID);
  });

  function post(id: string, fields: Record<string, unknown>, sid = SID): void {
    const dir = join(tmp, "state", "probe", "reads", "messages", sid);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, `${"0".repeat(20)}-${id}.json`), JSON.stringify({
      id, session: sid, kind: "server", text: "", ask: null, question: null, reason: null, made_at: 1,
      expires_at: Date.now() / 1000 + 3600, ...fields,
    }));
  }

  it("delivers server messages in their own lane, served or not, and the person's alone to nobody", () => {
    post("nfiles_freeze_0", { text: "Team-level files are read-only.", severity: "blocking", audience: "both" });
    post("npeople_1", { text: "For the person.", severity: "warning", audience: "person" });
    const texts = box.deliver(box.currentTurn(), "before_agent_start", true, true);
    expect(texts).toEqual([renderServerText("Team-level files are read-only.")]);
    expect(box.lastClaimed).toEqual(["nfiles_freeze_0"]);
    expect(readdirSync(join(tmp, "state", "probe", "reads", "messages", SID))).toEqual([]);
  });

  it("shows the person a refusal once per session and a notice once per computer", () => {
    post("nfiles_freeze_0", { text: "Team-level files are read-only.", severity: "blocking", audience: "both" });
    post(`n${makeMessageKey("maintenance_nov3", 7)}`, { text: MAINTENANCE.text, severity: "warning", audience: "both" });
    post("nfyi_0", { text: "Just so you know.", severity: "info", audience: "both" });
    box.deliver(box.currentTurn(), "test", true, true);
    const ledger = ledgerFor(env);
    const banners = box.takeBanners(() => ledger);
    expect(banners.map((b) => [b.text, b.severity])).toEqual([
      ["Probe: Team-level files are read-only.", "blocking"],
      [`Probe: ${MAINTENANCE.text}`, "warning"],
    ]);
    for (const banner of banners) banner.claim?.commit();
    expect(box.takeBanners(() => ledger)).toEqual([]);

    const other = new Mailbox(join(tmp, "state", "probe", "reads"), join(tmp, "sessions"), OTHER);
    post("nfiles_freeze_0", { text: "Team-level files are read-only.", severity: "blocking", audience: "both" }, OTHER);
    post(`n${makeMessageKey("maintenance_nov3", 7)}`, { text: MAINTENANCE.text, severity: "warning", audience: "both" }, OTHER);
    other.deliver(other.currentTurn(), "test", true, true);
    expect(other.takeBanners(() => ledger).map((b) => b.text)).toEqual(["Probe: Team-level files are read-only."]);
  });
});
