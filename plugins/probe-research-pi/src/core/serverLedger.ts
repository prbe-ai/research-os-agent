/**
 * The seen ledger of server notices (plan R19, R26): which this computer's person and each coding-agent
 * session were shown. `probe.foundation.server_messages.ledger`'s twin over the SAME files, so the CLI,
 * the SDK, the plugin hooks and pi show a notice once (`tests/serverNotices.test.ts` and
 * `agent/tests/foundation/test_server_message_ledger.py` pin the two together). Never throws.
 */

import { createHash, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import { join } from "node:path";

import { probeStateDir, type PathEnv } from "./paths.js";

/** `ledger.LEDGER_DIRNAME`. */
export const LEDGER_DIRNAME = "server-messages";
/** `ledger.CLAIM_TTL_S`: a claim nobody committed within this long is taken over. */
export const CLAIM_TTL_S = 60;
export const CLAIM_SUFFIX = ".claim";
export const SHOWN_SUFFIX = ".shown";
/** `ledger.LIST_FILENAME`: the account's last list per client kind. */
export const LIST_FILENAME = "messages-{kind}.json";
const PLAIN_ID = /^[A-Za-z0-9_]{1,48}$/;
const UNSAFE = /[^A-Za-z0-9_-]/g;
const MAX_REVISION = 1e9;

/** `ledger.Destination`: the person once per computer, an agent once per session. */
export const Destination = {
  Person: "person",
  Agent: "agent",
} as const;
export type DestinationValue = (typeof Destination)[keyof typeof Destination];

const sha256 = (text: string): string => createHash("sha256").update(text, "utf8").digest("hex");

/** `ledger.make_account_key`: a server + credential, never the token itself. */
export function makeAccountKey(baseUrl: string, token: string): string {
  return sha256(`${baseUrl.replace(/\/+$/, "")}|${token}`).slice(0, 16);
}

/** `ledger.make_message_key`: one message at one revision. */
export function makeMessageKey(messageId: string, revision = 0): string {
  const key = PLAIN_ID.test(messageId) ? messageId : sha256(messageId).slice(0, 16);
  const bounded = Math.min(Math.max(0, Math.trunc(Number(revision) || 0)), MAX_REVISION);
  return `${key}_${bounded}`;
}

/** This process's right to show one message to one destination. */
export class Claim {
  constructor(readonly path: string) {}

  get shownPath(): string {
    return this.path.slice(0, -CLAIM_SUFFIX.length) + SHOWN_SUFFIX;
  }

  commit(): void {
    try {
      fs.renameSync(this.path, this.shownPath);
    } catch {
      try {
        fs.closeSync(fs.openSync(this.shownPath, "a", 0o600)); // taken over as stale: shown all the same
      } catch {
        // nothing more to do
      }
    }
  }

  release(): void {
    fs.rmSync(this.path, { force: true });
  }
}

export interface SavedList {
  setRevision: string | null;
  fetchedAt: number;
  items: unknown[];
  /** The client version that asked: another version's list is not this one's (notices match by version). */
  clientVersion: string | null;
}

export class MessageLedger {
  constructor(readonly folder: string) {}

  static fromAccount(baseUrl: string, token: string, env: PathEnv = process.env): MessageLedger {
    return new MessageLedger(join(probeStateDir(env), LEDGER_DIRNAME, makeAccountKey(baseUrl, token)));
  }

  /** The claim to show `messageKey` to `destination`, or null (shown, being shown, unwritable). */
  claim(messageKey: string, destination: DestinationValue, sessionId = ""): Claim | null {
    const folder = this.getScopeFolder(destination, sessionId);
    if (!folder) return null;
    const path = join(folder, messageKey + CLAIM_SUFFIX);
    const claim = new Claim(path);
    try {
      fs.mkdirSync(folder, { recursive: true });
    } catch {
      return null;
    }
    for (let attempt = 0; attempt < 2; attempt++) {
      if (fs.existsSync(claim.shownPath)) return null;
      let fd: number;
      try {
        fd = fs.openSync(path, "wx", 0o600);
      } catch (err) {
        if ((err as NodeJS.ErrnoException).code === "EEXIST" && takeStale(path)) continue;
        return null;
      }
      try {
        fs.writeSync(fd, JSON.stringify({ pid: process.pid, at: Date.now() / 1000 }));
      } finally {
        fs.closeSync(fd);
      }
      if (fs.existsSync(claim.shownPath)) {
        claim.release();
        return null;
      }
      return claim;
    }
    return null;
  }

  hasShown(messageKey: string, destination: DestinationValue, sessionId = ""): boolean {
    const folder = this.getScopeFolder(destination, sessionId);
    return folder !== null && fs.existsSync(join(folder, messageKey + SHOWN_SUFFIX));
  }

  loadList(clientKind: string): SavedList | null {
    try {
      const data = JSON.parse(fs.readFileSync(this.listPath(clientKind), "utf8"));
      const revision = data?.set_revision ?? null;
      if (!Array.isArray(data?.messages) || !(revision === null || typeof revision === "string")) return null;
      const clientVersion = typeof data.client_version === "string" ? data.client_version : null;
      return { setRevision: revision, fetchedAt: Number(data.fetched_at) || 0, items: data.messages, clientVersion };
    } catch {
      return null;
    }
  }

  saveList(setRevision: string | null, items: unknown[], clientKind: string, now = Date.now() / 1000, clientVersion: string | null = null): void {
    const target = this.listPath(clientKind);
    const temporary = `${target}.${process.pid}.${randomBytes(4).toString("hex")}.tmp`;
    try {
      fs.mkdirSync(this.folder, { recursive: true });
      fs.writeFileSync(temporary, JSON.stringify({ set_revision: setRevision, fetched_at: now, messages: items, client_version: clientVersion }));
      fs.renameSync(temporary, target);
    } catch {
      fs.rmSync(temporary, { force: true });
    }
  }

  private listPath(clientKind: string): string {
    return join(this.folder, LIST_FILENAME.replace("{kind}", clientKind.replace(UNSAFE, "") || "client"));
  }

  private getScopeFolder(destination: DestinationValue, sessionId: string): string | null {
    if (destination === Destination.Person) return join(this.folder, Destination.Person);
    const safe = (sessionId || "").replace(UNSAFE, "");
    return safe ? join(this.folder, Destination.Agent, safe) : null;
  }
}

/** `ledger._take_stale`: a claim in the way is gone or stale; a stale one is renamed aside first. */
function takeStale(path: string): boolean {
  let age: number;
  try {
    age = Date.now() / 1000 - fs.statSync(path).mtimeMs / 1000;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === "ENOENT";
  }
  if (age < CLAIM_TTL_S) return false;
  const aside = `${path}.${randomBytes(4).toString("hex")}.stale`;
  try {
    fs.renameSync(path, aside);
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === "ENOENT";
  }
  fs.rmSync(aside, { force: true });
  return true;
}
