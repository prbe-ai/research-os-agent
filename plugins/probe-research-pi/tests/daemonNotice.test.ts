import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  companionKeyHeld,
  DAEMON_LIVE_NOTICE,
  daemonNotice,
  daemonStatus,
  DaemonStatus,
  type DaemonNoticeDeps,
} from "../src/daemonNotice.js";

const SID = "11111111-2222-3333-4444-555555555555";

let tmp: string;
let now: number;

function deps(): DaemonNoticeDeps {
  return {
    env: { XDG_STATE_HOME: tmp, XDG_CONFIG_HOME: join(tmp, "config") },
    readFileSync: (path) => readFileSync(path, "utf-8"),
    writeFileSync: (path, content) => writeFileSync(path, content),
    mkdirSync: (path) => mkdirSync(path, { recursive: true }),
    rmSync: (path) => rmSync(path, { force: true }),
    now: () => now,
  };
}

function writeKey(): void {
  mkdirSync(join(tmp, "config", "probe"), { recursive: true });
  writeFileSync(
    join(tmp, "config", "probe", "config.json"),
    JSON.stringify({ current_context: "default", contexts: { default: { companion_token: "k" } } }),
  );
}

function sessions(): string {
  return join(tmp, "probe", "sessions");
}

function writeLease(fields: Record<string, unknown>): void {
  mkdirSync(sessions(), { recursive: true });
  writeFileSync(
    join(sessions(), `${SID}.writer`),
    JSON.stringify({ v: 1, writer: "daemon", pid: 1, expires_at: now / 1000 + 60, renewed_at: now / 1000, reason: null, ...fields }),
  );
}

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-daemon-notice-"));
  now = 1_790_000_000_000;
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

describe("daemonStatus", () => {
  it("reads a fresh lease as live", () => {
    writeLease({});
    expect(daemonStatus(SID, deps())).toEqual({ status: DaemonStatus.Live, reason: null });
  });

  it("fails open: missing, malformed, unknown-version, expired and released leases are degraded", () => {
    expect(daemonStatus(SID, deps()).reason).toBe("not-started");
    mkdirSync(sessions(), { recursive: true });
    writeFileSync(join(sessions(), `${SID}.writer`), "{not json");
    expect(daemonStatus(SID, deps()).reason).toBe("not-started");
    writeLease({ v: 2 });
    expect(daemonStatus(SID, deps()).reason).toBe("not-started");
    writeLease({ expires_at: now / 1000 - 1 });
    expect(daemonStatus(SID, deps()).reason).toBe("expired");
    writeLease({ reason: "budget" });
    expect(daemonStatus(SID, deps())).toEqual({ status: DaemonStatus.Degraded, reason: "budget" });
  });
});

describe("daemonNotice", () => {
  it("says nothing while a live daemon keeps recording", () => {
    writeLease({});
    expect(daemonNotice(SID, true, deps())).toBeNull();
    expect(daemonNotice(SID, true, deps())).toBeNull();
  });

  it("gives the worker one prompt of grace to take its first lease, when it has a key", () => {
    writeKey();
    expect(daemonNotice(SID, true, deps())).toBeNull();
    expect(daemonNotice(SID, true, deps())).toContain("the daemon is not running");
  });

  it("says at once that a daemon with no key cannot start", () => {
    expect(daemonNotice(SID, true, deps())).toContain("the daemon is not running");
    expect(companionKeyHeld(deps())).toBe(false);
  });

  it("announces a lapse once, and the recovery once", () => {
    writeLease({});
    expect(daemonNotice(SID, true, deps())).toBeNull();
    writeLease({ expires_at: now / 1000 - 1 });
    expect(daemonNotice(SID, true, deps())).toContain("the daemon is not responding, so recording is back with you");
    expect(daemonNotice(SID, true, deps())).toBeNull();
    writeLease({});
    expect(daemonNotice(SID, true, deps())).toBe(DAEMON_LIVE_NOTICE);
    expect(daemonNotice(SID, true, deps())).toBeNull();
  });

  it("clears its record when the session leaves the daemon state", () => {
    writeLease({});
    daemonNotice(SID, true, deps());
    const record = join(sessions(), `${SID}.writer-notified`);
    expect(existsSync(record)).toBe(true);
    expect(daemonNotice(SID, false, deps())).toBeNull();
    expect(existsSync(record)).toBe(false);
  });

  it("reads the record the Python guard writes", () => {
    writeLease({ reason: "unauthorized" });
    mkdirSync(sessions(), { recursive: true });
    writeFileSync(join(sessions(), `${SID}.writer-notified`), JSON.stringify({ status: "degraded", prompts: 3 }));
    expect(daemonNotice(SID, true, deps())).toBeNull();
  });
});
