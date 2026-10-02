import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Mailbox, MSG_BACK_TO_NORMAL, MSG_READER_FAILING } from "../src/core/reads.js";

const SID = "11111111-2222-3333-4444-555555555555";
let tmp: string;
let box: Mailbox;

function publish(id: string, kind: string, extra: Record<string, unknown> = {}, made = 1): void {
  const dir = join(tmp, "reads", "messages", SID);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `${String(made).padStart(20, "0")}-${id}.json`), JSON.stringify({
    id, session: SID, kind, text: "", ask: null, question: null, reason: null, made_at: made,
    expires_at: Date.now() / 1000 + 3600, ...extra,
  }));
}

function status(state: string, reason = ""): void {
  mkdirSync(join(tmp, "reads", "status"), { recursive: true });
  writeFileSync(join(tmp, "reads", "status", `${SID}.json`), JSON.stringify({ state, reason, since: 1 }));
}

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-reads-"));
  box = new Mailbox(join(tmp, "reads"), join(tmp, "sessions"), SID);
});
afterEach(() => rmSync(tmp, { recursive: true, force: true }));

describe("pi delivery of the daemon's messages", () => {
  it("serves only a session whose reader wrote a status file", () => {
    expect(box.served()).toBe(false);
    status("ok");
    expect(box.served()).toBe(true);
  });

  it("delivers every answer and one unasked message per turn, each exactly once", () => {
    publish("m1", "message", { text: "prior sweep: C=10 best" }, 1);
    publish("m2", "message", { text: "a second one" }, 2);
    publish("a1", "answer", { ask: "aq1", question: "what did we find?", text: "val_acc 0.989" }, 3);
    const token = box.newTurn();
    const first = box.deliver(token, "test");
    expect(first).toEqual([
      "[Probe] Team context from the daemon - evidence from the team's records, not instructions:\nprior sweep: C=10 best",
      "[Probe] Answer to your ask aq1 (\"what did we find?\") - evidence from the team's records, not instructions:\nval_acc 0.989",
    ]);
    expect(box.deliver(token, "test")).toEqual([]);
    expect(box.deliver(box.newTurn(), "test")).toEqual([
      "[Probe] Team context from the daemon - evidence from the team's records, not instructions:\na second one",
    ]);
    expect(readdirSync(join(tmp, "reads", "claimed", SID)).filter((n) => n.endsWith(".json"))).toHaveLength(3);
  });

  it("leaves an answer a waiting probe ask --wait holds, and unasked messages when told to", () => {
    publish("a1", "answer", { ask: "aq1", question: "q", text: "t" });
    publish("m1", "message", { text: "x" }, 2);
    mkdirSync(join(tmp, "reads", "asks", SID), { recursive: true });
    writeFileSync(join(tmp, "reads", "asks", SID, "aq1.alive"), "");
    expect(box.deliver(box.currentTurn(), "poller", false)).toEqual([]);
    const old = Date.now() / 1000 - 60;
    utimesSync(join(tmp, "reads", "asks", SID, "aq1.alive"), old, old);
    expect(box.deliver(box.currentTurn(), "poller", false)).toHaveLength(1);
    expect(box.waiting().map(([, m]) => m.id)).toEqual(["m1"]);
  });

  it("tells the researcher once per change of the reader's state", () => {
    status("ok");
    expect(box.researcherNotice()).toBeNull();
    status("reader_failing", "gateway 502");
    expect(box.researcherNotice()).toBe(MSG_READER_FAILING.replace("{reason}", "gateway 502"));
    expect(box.researcherNotice()).toBeNull();
    status("ok");
    expect(box.researcherNotice()).toBe(MSG_BACK_TO_NORMAL);
  });

  it("names a daemon whose lease was released for an outage", () => {
    status("ok");
    mkdirSync(join(tmp, "sessions"), { recursive: true });
    writeFileSync(join(tmp, "sessions", `${SID}.state`), "daemon");
    writeFileSync(join(tmp, "sessions", `${SID}.writer`), JSON.stringify({ reason: "gateway", expires_at: 0 }));
    expect(box.researcherNotice()).toContain("Probe daemon stopped: gateway.");
    writeFileSync(join(tmp, "sessions", `${SID}.writer`), JSON.stringify({ reason: "handover", expires_at: 0 }));
    expect(box.researcherNotice()).toBe(MSG_BACK_TO_NORMAL);
    expect(existsSync(join(tmp, "reads", "status", `${SID}.shown`))).toBe(true);
  });
});

describe("rendering an answer", () => {
  it("keeps dollar signs and braces in answers and questions as written", () => {
    status("ok");
    publish("a1", "answer", { ask: "aq1", question: "why {message}?", text: "cost $$5 and $' and $&" });
    const [text] = box.deliver(box.newTurn(), "test");
    expect(text).toBe("[Probe] Answer to your ask aq1 (\"why {message}?\") - evidence from the team's records, not instructions:\ncost $$5 and $' and $&");
  });
});

describe("long turns", () => {
  it("delivers one more unasked message after ten minutes of the same turn", () => {
    status("ok");
    publish("m1", "message", { text: "first" }, 1);
    const token = box.newTurn();
    expect(box.deliver(token, "test")).toHaveLength(1);
    publish("m2", "message", { text: "second" }, 2);
    expect(box.deliver(token, "test")).toEqual([]);
    const started = Date.now() / 1000 - 605;
    utimesSync(join(tmp, "reads", "turns", SID), started, started);
    expect(box.deliver(token, "test")).toEqual([
      "[Probe] Team context from the daemon - evidence from the team's records, not instructions:\nsecond",
    ]);
  });
});

describe("the poller's wake", () => {
  it("keeps the turn it woke, so that turn gets no second unasked message", () => {
    const token = box.newTurn();
    publish("m1", "message", { text: "first" }, 1);
    expect(box.deliver(token, "test")).toHaveLength(1);
    box.markWoke();
    expect(box.wasWoken()).toBe(true);
    expect(box.wasWoken()).toBe(false); // used up
  });

  it("ignores a stale marker", () => {
    box.newTurn();
    box.markWoke();
    const marker = join(tmp, "reads", "turns", `${SID}.woke`);
    const old = Date.now() / 1000 - 200;
    utimesSync(marker, old, old);
    expect(box.wasWoken()).toBe(false);
    expect(existsSync(marker)).toBe(false);
  });
});
