import * as fs from "node:fs";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PI_DIALOG_CHANNEL, QuestionAsker, Verdict, heldQuestions, type ApprovalsDeps, type DialogUI } from "../src/core/approvals.js";
import { approvalsDir } from "../src/core/paths.js";

/** What the Board holds and what this writer must produce; shared with agent/tests/test_pi_approval_answer.py. */
const FIXTURE = JSON.parse(
  fs.readFileSync(new URL("../../../tests/fixtures/pi_approval_answer.json", import.meta.url), "utf-8"),
) as {
  request: Record<string, any>;
  yes: Record<string, unknown>;
  no: Record<string, unknown>;
};
const SID: string = FIXTURE.request.session_id;
const ASKED_MS = FIXTURE.request.asked_at * 1000;

let tmp: string;
let env: Record<string, string | undefined>;
let nowMs: number;

function deps(): ApprovalsDeps {
  return {
    env,
    readdirSync: (dir) => fs.readdirSync(dir),
    readFileSync: (path) => fs.readFileSync(path, "utf-8"),
    existsSync: fs.existsSync,
    writeFileSync: (path, content) => fs.writeFileSync(path, content),
    renameSync: (from, to) => fs.renameSync(from, to),
    rmSync: (path) => fs.rmSync(path, { force: true }),
    mkdirSync: (path) => fs.mkdirSync(path, { recursive: true }),
    now: () => nowMs,
    token: () => "t0k3n",
  };
}

const requestsDir = () => join(approvalsDir(env), "requests");
const answersDir = () => join(approvalsDir(env), "answers");

/** A request file as the Board writes it (`indent=1`), with fields overridden. */
function hold(overrides: Record<string, unknown> = {}, question: Record<string, unknown> = {}): { id: string; path: string } {
  const request: Record<string, any> = { ...FIXTURE.request, ...overrides, question: { ...FIXTURE.request.question, ...question } };
  fs.mkdirSync(requestsDir(), { recursive: true });
  fs.mkdirSync(answersDir(), { recursive: true });
  const path = join(requestsDir(), `${request.id}.json`);
  fs.writeFileSync(path, JSON.stringify(request, null, 1));
  return { id: request.id, path };
}

function answerOf(id: string): Record<string, unknown> | null {
  try {
    return JSON.parse(fs.readFileSync(join(answersDir(), `${id}.json`), "utf-8"));
  } catch {
    return null;
  }
}

function dialog(...picks: Array<string | undefined>): DialogUI & { select: ReturnType<typeof vi.fn> } {
  const select = vi.fn(async () => picks.shift());
  return { hasUI: true, select };
}

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "probe-pi-approvals-"));
  env = { XDG_STATE_HOME: join(tmp, "state") };
  nowMs = ASKED_MS + 60_000;
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

describe("the researcher's pick, written as the Board's answer", () => {
  it("writes exactly the fixture's yes for the yes label", async () => {
    const { id } = hold();
    nowMs = (FIXTURE.yes.at as number) * 1000;
    const ui = dialog(FIXTURE.request.question.yes_label);
    expect(await new QuestionAsker().ask(SID, ui, deps(), true)).toBe(1);
    // The exact question and the two labels, yes first.
    expect(ui.select).toHaveBeenCalledWith(FIXTURE.request.question.question, [
      FIXTURE.request.question.yes_label,
      FIXTURE.request.question.no_label,
    ]);
    expect(answerOf(id)).toEqual(FIXTURE.yes);
    expect(FIXTURE.yes).toMatchObject({ answer: Verdict.Yes, channel: PI_DIALOG_CHANNEL });
    // Nothing else is left in the answers folder: the temporary file was renamed.
    expect(fs.readdirSync(answersDir())).toEqual([`${id}.json`]);
  });

  it("writes exactly the fixture's no for the no label", async () => {
    const { id } = hold();
    nowMs = (FIXTURE.no.at as number) * 1000;
    expect(await new QuestionAsker().ask(SID, dialog(FIXTURE.request.question.no_label), deps(), true)).toBe(1);
    expect(answerOf(id)).toEqual(FIXTURE.no);
  });

  it("never moves, renames or edits the request file", async () => {
    const { path } = hold();
    const before = fs.readFileSync(path, "utf-8");
    const mtime = fs.statSync(path).mtimeMs;
    await new QuestionAsker().ask(SID, dialog(FIXTURE.request.question.yes_label), deps(), true);
    expect(fs.readFileSync(path, "utf-8")).toBe(before);
    expect(fs.statSync(path).mtimeMs).toBe(mtime);
    expect(fs.readdirSync(requestsDir())).toEqual([`${FIXTURE.request.id}.json`]);
  });
});

describe("when a question is asked", () => {
  it("Esc writes nothing, waits out the poller, and asks again at the next prompt", async () => {
    const { id } = hold();
    const asker = new QuestionAsker();
    const ui = dialog(undefined, FIXTURE.request.question.no_label);
    expect(await asker.ask(SID, ui, deps(), true)).toBe(0);
    expect(answerOf(id)).toBeNull();
    // The 2 s poller does not pop it straight back up...
    expect(await asker.ask(SID, ui, deps(), false)).toBe(0);
    expect(ui.select).toHaveBeenCalledTimes(1);
    // ...the next prompt does.
    expect(await asker.ask(SID, ui, deps(), true)).toBe(1);
    expect(ui.select).toHaveBeenCalledTimes(2);
    expect(answerOf(id)).toMatchObject({ answer: Verdict.No });
  });

  it("asks nothing without a UI (print or json mode): the terminal's `probe approvals` answers", async () => {
    hold();
    const select = vi.fn();
    expect(await new QuestionAsker().ask(SID, { hasUI: false, select }, deps(), true)).toBe(0);
    expect(select).not.toHaveBeenCalled();
  });

  it.each([
    ["a terminal-only question", {}, { terminal_only: true }],
    ["an expired question", { expires_at: ASKED_MS / 1000 + 1 }, {}],
    ["a settled question", { state: "denied" }, {}],
    ["another session's question", { session_id: "99999999-2222-3333-4444-555555555555" }, {}],
  ])("skips %s", async (_name, overrides, question) => {
    hold(overrides, question);
    const ui = dialog(FIXTURE.request.question.yes_label);
    expect(await new QuestionAsker().ask(SID, ui, deps(), true)).toBe(0);
    expect(ui.select).not.toHaveBeenCalled();
  });

  it("skips a question already answered, and never writes over that answer", async () => {
    const { id } = hold();
    const terminal = JSON.stringify({ id, answer: "no", choice: "Keep it", channel: "terminal", at: 1 });
    fs.writeFileSync(join(answersDir(), `${id}.json`), terminal);
    const ui = dialog(FIXTURE.request.question.yes_label);
    expect(await new QuestionAsker().ask(SID, ui, deps(), true)).toBe(0);
    expect(ui.select).not.toHaveBeenCalled();
    expect(fs.readFileSync(join(answersDir(), `${id}.json`), "utf-8")).toBe(terminal);
  });

  it("writes nothing when the question was settled while the dialog was open", async () => {
    const { id, path } = hold();
    const ui: DialogUI = {
      hasUI: true,
      select: async () => {
        fs.writeFileSync(path, JSON.stringify({ ...FIXTURE.request, state: "expired" }));
        return FIXTURE.request.question.yes_label;
      },
    };
    expect(await new QuestionAsker().ask(SID, ui, deps(), true)).toBe(0);
    expect(answerOf(id)).toBeNull();
  });

  it("opens one dialog at a time, and asks each question once", async () => {
    hold();
    let release: (pick: string) => void = () => {};
    const select = vi.fn(() => new Promise<string>((resolve) => (release = resolve)));
    const asker = new QuestionAsker();
    const first = asker.ask(SID, { hasUI: true, select }, deps(), false);
    expect(await asker.ask(SID, { hasUI: true, select }, deps(), true)).toBe(0);
    release(FIXTURE.request.question.yes_label);
    expect(await first).toBe(1);
    expect(await asker.ask(SID, { hasUI: true, select }, deps(), true)).toBe(0);
    expect(select).toHaveBeenCalledTimes(1);
  });

  it("asks the oldest first, and reads nothing but well-formed requests", () => {
    hold({ id: "bbbbbbbb", asked_at: FIXTURE.request.asked_at + 5 });
    hold({ id: "aaaaaaaa", asked_at: FIXTURE.request.asked_at + 9 });
    // An id that would aim the answer outside the folder.
    fs.writeFileSync(join(requestsDir(), "dddddddd.json"), JSON.stringify({ ...FIXTURE.request, id: "../escape" }));
    fs.writeFileSync(join(requestsDir(), "cccccccc.json"), ""); // an id reservation the Board left empty
    expect(heldQuestions(SID, deps()).map((q) => q.id)).toEqual(["bbbbbbbb", "aaaaaaaa"]);
  });
});
