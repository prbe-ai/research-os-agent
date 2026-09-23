import { mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, statSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TURN_SUFFIX, writeTurnSignal, type TurnSignalDeps } from "../src/turnSignal.js";

const SID = "11111111-2222-3333-4444-555555555555";

let tmp: string;
let sessions: string;
let transcript: string;

function deps(): TurnSignalDeps {
  return {
    env: { XDG_STATE_HOME: tmp },
    now: () => 1_700_000_000_000,
    pid: 4242,
    readFileSync: (path) => readFileSync(path, "utf-8"),
    sizeOf: (path) => statSync(path).size,
    writeFileSync: (path, content) => writeFileSync(path, content),
    renameSync: (from, to) => renameSync(from, to),
  };
}

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-turn-"));
  sessions = join(tmp, "probe", "sessions");
  mkdirSync(sessions, { recursive: true });
  transcript = join(tmp, "session.jsonl");
  writeFileSync(transcript, "x".repeat(321));
});

afterEach(() => rmSync(tmp, { recursive: true, force: true }));

describe("writeTurnSignal (pi's Stop hook)", () => {
  it("writes the transcript size in daemon, the same file the tap's turn-end.sh writes", () => {
    writeFileSync(join(sessions, `${SID}.state`), "daemon\n");
    expect(writeTurnSignal(SID, transcript, deps())).toBe(true);
    const turn = JSON.parse(readFileSync(join(sessions, `${SID}${TURN_SUFFIX}`), "utf-8"));
    expect(turn).toEqual({ offset: 321, at: 1_700_000_000 });
  });

  it("writes nothing in any other state, without a state, or with an unsafe id", () => {
    writeFileSync(join(sessions, `${SID}.state`), "on\n");
    expect(writeTurnSignal(SID, transcript, deps())).toBe(false);
    expect(writeTurnSignal("22222222-2222-3333-4444-555555555555", transcript, deps())).toBe(false);
    expect(writeTurnSignal("../../etc/passwd", transcript, deps())).toBe(false);
    expect(writeTurnSignal(SID, undefined, deps())).toBe(false);
    expect(existsSync(join(sessions, `${SID}${TURN_SUFFIX}`))).toBe(false);
  });

  it("never throws when the transcript is gone", () => {
    writeFileSync(join(sessions, `${SID}.state`), "daemon");
    expect(writeTurnSignal(SID, join(tmp, "missing.jsonl"), deps())).toBe(false);
  });
});
