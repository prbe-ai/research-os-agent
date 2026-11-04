/**
 * The action set and the `hooks.run` v2 client (PR 7): the same fixture as the service's own
 * test (`agent/tests/fixtures/hook_actions.json`), and each way a forward ends (D50).
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { gatherEffects, readAction, readActions, ServiceAction } from "../src/serviceActions.js";
import {
  ackReceipt,
  CAP_HOOKS_PROXY_V2,
  ForwardOutcome,
  forwardEvent,
  isForwarding,
  makeQueueEnv,
  piConfigRoot,
  type ForwardRequest,
} from "../src/serviceForward.js";
import { answer, fakeService, socketFolder, type Behaviour, type FakeService } from "./fakeService.js";

const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, "..", "..", "..", "tests", "fixtures", "hook_actions.json"), "utf-8"));

describe("the action set is the service's (one fixture drives both sides)", () => {
  it("knows every kind the service sends", () => {
    expect(new Set(Object.values(ServiceAction))).toEqual(new Set(FIXTURE.kinds));
  });

  it("reads each action as the service writes it", () => {
    for (const action of FIXTURE.actions) expect(readAction(action)).toEqual(action);
  });

  it("drops what it cannot read, and says so", () => {
    const dropped: string[] = [];
    expect(readActions(FIXTURE.ignored, (line) => dropped.push(line))).toEqual([]);
    expect(dropped).toHaveLength(FIXTURE.ignored.length);
  });

  it("gathers an answer's actions by what a handler does with them", () => {
    const effects = gatherEffects(readActions(FIXTURE.actions));
    expect(effects.block).toBe("`write` to the approvals folder is refused");
    expect(effects.context).toEqual(["kept for the session"]);
    expect(effects.receipts).toEqual(["ab".repeat(16)]);
  });
});

const SOCKET = join(socketFolder(), "service.sock");
let service: FakeService | null = null;

afterEach(async () => {
  await service?.close();
  service = null;
});

const REQUEST: ForwardRequest = {
  event: "tool_call",
  payload: { session_id: "s1", cwd: "/tmp", tool_name: "bash", input: { command: "probe x" } },
  env: { HOME: "/tmp" },
  cwd: "/tmp",
  timeoutMs: 250,
  waitMs: 1000,
};
const DEPS = { clientVersion: "test" };

async function serve(behaviour: (request: Record<string, unknown>) => Behaviour, caps = [CAP_HOOKS_PROXY_V2]): Promise<FakeService> {
  service = await fakeService(SOCKET, behaviour, { caps });
  return service;
}

describe("a forward ends answered, not accepted, or with no answer (D50)", () => {
  it("sends v2 as pi and reads the actions", async () => {
    const fake = await serve(() => ({ result: answer({ actions: [{ kind: "block", text: "no" }, { kind: "teleport" }] }) }));
    const got = await forwardEvent(SOCKET, REQUEST, DEPS);
    expect(got).toMatchObject({ outcome: ForwardOutcome.Answered, exitCode: 0, actions: [{ kind: "block", text: "no" }] });
    const run = fake.requests.find((r) => r.method === "hooks.run")!.params as Record<string, unknown>;
    expect(run).toMatchObject({ routes: "service", v: 2, reply: "actions", harness: "pi", event: "tool_call", timeout_ms: 250 });
    expect(JSON.parse(run.payload as string)).toEqual(REQUEST.payload);
    expect(run.event_id).toMatch(/^[0-9a-f]{32}$/);
  });

  it("is not accepted by a service without the capability: nothing is sent", async () => {
    const fake = await serve(() => ({ result: answer() }), []);
    expect((await forwardEvent(SOCKET, REQUEST, DEPS)).outcome).toBe(ForwardOutcome.NotAccepted);
    expect(fake.requests.map((r) => r.method)).toEqual(["service.hello"]);
  });

  it("is not accepted when refused or met with an error, with the refusal's reason", async () => {
    await serve(() => ({ result: { refused: "a caller in a sandbox", reason: "sandboxed" } }));
    expect(await forwardEvent(SOCKET, REQUEST, DEPS)).toMatchObject({ outcome: ForwardOutcome.NotAccepted, refusedReason: "sandboxed" });
    await service!.close();
    await serve(() => ({ error: { code: -32602, message: "v must be 1" } }));
    expect((await forwardEvent(SOCKET, REQUEST, DEPS)).outcome).toBe(ForwardOutcome.NotAccepted);
  });

  it("has no answer when asked and never answered: the service may have run it", async () => {
    await serve(() => ({ silent: true }));
    expect((await forwardEvent(SOCKET, { ...REQUEST, waitMs: 200 }, DEPS)).outcome).toBe(ForwardOutcome.NoAnswer);
  });

  it("is not accepted with no socket", async () => {
    expect((await forwardEvent(null, REQUEST, DEPS)).outcome).toBe(ForwardOutcome.NotAccepted);
  });

  it("acks a receipt", async () => {
    const fake = await serve(() => ({ result: { acked: true } }));
    expect(await ackReceipt(SOCKET, "ab".repeat(16), true, DEPS)).toBe(true);
    expect(fake.requests.find((r) => r.method === "hooks.ack")!.params).toEqual({ receipt: "ab".repeat(16), shown: true });
  });
});

describe("whether this machine forwards pi's events is the ledger's (D47)", () => {
  const dir = socketFolder();
  const env = { HOME: "/home/u", PI_CODING_AGENT_DIR: "/home/u/.pi/agent" };

  function ledger(switches: { at: number; to: string }[], root = "/home/u/.pi/agent"): void {
    mkdirSync(join(dir, "hooks"), { recursive: true });
    writeFileSync(join(dir, "hooks", "migrations.json"), JSON.stringify({ v: 1, harnesses: { pi: { [root]: { switches } } } }));
  }

  it("forwards once pi's record ends in a shim switch, for this config only", () => {
    ledger([{ at: 1, to: "shim" }]);
    expect(isForwarding(dir, env)).toBe(true);
    expect(isForwarding(dir, { ...env, PI_CODING_AGENT_DIR: "/elsewhere" })).toBe(false);
  });

  it("stops after a rollback, and with no ledger at all", () => {
    ledger([{ at: 1, to: "shim" }, { at: 2, to: "legacy" }]);
    expect(isForwarding(dir, env)).toBe(false);
    expect(isForwarding(null, env)).toBe(false);
    expect(isForwarding(join(dir, "nope"), env)).toBe(false);
  });

  it("keeps no secret of the session in a queue file", () => {
    expect(makeQueueEnv({ HOME: "/h", PATH: "/b", PI_SESSION_ID: "s", PROBE_TOKEN: "t", OPENAI_API_KEY: "k", XDG_STATE_HOME: "/x", PROBE_AGENT: "pi" }))
      .toEqual({ HOME: "/h", PATH: "/b", XDG_STATE_HOME: "/x", PROBE_AGENT: "pi" });
  });

  it("keys pi's config as the CLI does", () => {
    expect(piConfigRoot({ HOME: "/home/u" })).toBe("/home/u/.pi/agent");
    expect(piConfigRoot({ HOME: "/home/u", PI_CODING_AGENT_DIR: "/x/agent" })).toBe("/x/agent");
  });
});
