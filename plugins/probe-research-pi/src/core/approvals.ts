/**
 * pi's own dialog for the Probe daemon's held questions (decision D2 = A).
 *
 * A daemon action that needs the researcher's yes is HELD: the daemon writes
 * the exact question to `requests/<id>.json` in `approvalsDir` and waits for
 * `answers/<id>.json` (`probe.daemon.approvals`, the Board). On Claude Code the
 * agent puts it to the researcher through its question tool and a hook writes
 * the pick; pi has a dialog of its own, so this extension asks directly:
 * `ctx.ui.select(question, [yes_label, no_label])`, and writes the pick in
 * EXACTLY the shape `Board.answer` writes, with `channel: "pi-dialog"`.
 *
 * THE PROTOCOL IS THE BOARD'S, kept exactly:
 *   - a question is asked only while its request is this session's, WAITING,
 *     unexpired, not `terminal_only` and unanswered (`approvals_hook._waiting`);
 *   - yes only for the exact yes label, anything else is a no (`Board.answer`);
 *   - the request file is never moved, renamed or edited -- the worker and
 *     `probe approvals` enumerate `requests/*.json`, and the worker settles it;
 *   - the answer is written once (never over an answer already there), and
 *     only after the request is read again and is still waiting and unexpired;
 *   - atomically: a temporary file of its own, then a rename.
 *
 * WHEN: at every prompt (`before_agent_start`) and on the 2 s poller that
 * already delivers the daemon's reads. One dialog at a time. Each question is
 * asked once per process; Esc writes nothing and asks again at the NEXT
 * prompt, not two seconds later. With no UI (print or json mode) nothing is
 * asked: the researcher answers with `probe approvals` in a terminal, which a
 * `terminal_only` question always needs.
 *
 * `agent/tests/fixtures/pi_approval_answer.json` is what this writes, and
 * `agent/tests/test_pi_approval_answer.py` feeds it to the Board and the
 * worker. Never throws: a failure is "nothing answered", and the question waits.
 */

import { join } from "node:path";

import { approvalsDir, type PathEnv } from "./paths.js";

/** `approvals.WAITING`: the only state a question is asked in. */
export const REQUEST_WAITING = "waiting";

/** `approvals.YES` / `approvals.NO`. */
export const Verdict = {
  Yes: "yes",
  No: "no",
} as const;
export type VerdictValue = (typeof Verdict)[keyof typeof Verdict];

/** The `channel` this extension's answers carry. */
export const PI_DIALOG_CHANNEL = "pi-dialog";

/** One question, as a request file holds it. */
export interface HeldQuestion {
  id: string;
  question: string;
  yesLabel: string;
  noLabel: string;
  askedAt: number;
}

/** `Board.answer`'s file: `{id, answer, choice, channel, at}`, `at` in epoch seconds. */
export interface ApprovalAnswer {
  id: string;
  answer: VerdictValue;
  choice: string;
  channel: string;
  at: number;
}

export interface ApprovalsDeps {
  env: PathEnv;
  readdirSync: (dir: string) => string[];
  readFileSync: (path: string) => string;
  existsSync: (path: string) => boolean;
  writeFileSync: (path: string, content: string) => void;
  renameSync: (from: string, to: string) => void;
  rmSync: (path: string) => void;
  mkdirSync: (path: string) => void;
  /** Milliseconds, like `Date.now`. */
  now: () => number;
  /** A fresh suffix for this writer's temporary file. */
  token: () => string;
}

function requestsDir(env: PathEnv): string {
  return join(approvalsDir(env), "requests");
}

function answerPath(env: PathEnv, id: string): string {
  return join(approvalsDir(env), "answers", `${id}.json`);
}

/** The request as a question this session may be asked now, or null. */
function askable(data: unknown, sessionId: string, nowSeconds: number): HeldQuestion | null {
  if (typeof data !== "object" || data === null) return null;
  const req = data as Record<string, unknown>;
  if (req.state !== REQUEST_WAITING || req.session_id !== sessionId) return null;
  if (typeof req.id !== "string" || !/^[A-Za-z0-9_-]+$/.test(req.id)) return null;
  const expiresAt = Number(req.expires_at ?? 0);
  if (req.expires_at && nowSeconds > expiresAt) return null;
  const q = req.question;
  if (typeof q !== "object" || q === null) return null;
  const { question, yes_label: yesLabel, no_label: noLabel, terminal_only: terminalOnly } = q as Record<string, unknown>;
  // Too long for a dialog: `probe approvals` prints it whole.
  if (terminalOnly === true) return null;
  if (typeof question !== "string" || typeof yesLabel !== "string" || typeof noLabel !== "string") return null;
  return { id: req.id, question, yesLabel, noLabel, askedAt: Number(req.asked_at) || 0 };
}

function readJson(path: string, deps: ApprovalsDeps): unknown {
  try {
    return JSON.parse(deps.readFileSync(path));
  } catch {
    return null;
  }
}

/** This session's questions that may be asked now, oldest first (`approvals_hook._waiting`). */
export function heldQuestions(sessionId: string, deps: ApprovalsDeps): HeldQuestion[] {
  let names: string[];
  try {
    names = deps.readdirSync(requestsDir(deps.env)).filter((name) => name.endsWith(".json")).sort();
  } catch {
    return [];
  }
  const nowSeconds = deps.now() / 1000;
  const out: HeldQuestion[] = [];
  for (const name of names) {
    const question = askable(readJson(join(requestsDir(deps.env), name), deps), sessionId, nowSeconds);
    if (question && !deps.existsSync(answerPath(deps.env, question.id))) out.push(question);
  }
  return out.sort((a, b) => a.askedAt - b.askedAt);
}

/** The answer file for a pick: yes only for the exact yes label (`Board.answer`). */
export function answerFor(question: HeldQuestion, choice: string, nowMs: number): ApprovalAnswer {
  return {
    id: question.id,
    answer: choice === question.yesLabel ? Verdict.Yes : Verdict.No,
    choice,
    channel: PI_DIALOG_CHANNEL,
    at: nowMs / 1000,
  };
}

/**
 * Write the researcher's pick, once. False, writing nothing, when the request
 * is no longer this session's waiting, unexpired question (the daemon settled
 * it, it expired while the dialog was open) or an answer is already there
 * (`probe approvals` in a terminal got there first).
 */
export function writeAnswer(sessionId: string, question: HeldQuestion, choice: string, deps: ApprovalsDeps): boolean {
  const now = deps.now();
  const current = askable(readJson(join(requestsDir(deps.env), `${question.id}.json`), deps), sessionId, now / 1000);
  if (!current) return false;
  const path = answerPath(deps.env, question.id);
  if (deps.existsSync(path)) return false;
  const tmp = join(approvalsDir(deps.env), "answers", `.${question.id}.${deps.token()}.tmp`);
  try {
    deps.mkdirSync(join(approvalsDir(deps.env), "answers"));
    deps.writeFileSync(tmp, JSON.stringify(answerFor(current, choice, now), null, 1));
    deps.renameSync(tmp, path);
    return true;
  } catch {
    try {
      deps.rmSync(tmp);
    } catch {
      // nothing to clean up
    }
    return false;
  }
}

/** What happened to a question asked in this process. */
export const Asked = {
  Open: "open",
  Dismissed: "dismissed",
  Done: "done",
} as const;
type AskedValue = (typeof Asked)[keyof typeof Asked];

export interface DialogUI {
  hasUI: boolean;
  select: (title: string, options: string[]) => Promise<string | undefined>;
}

/**
 * The dialog, one at a time, each question once per process. `atPrompt` is
 * true from `before_agent_start`: a question the researcher dismissed (Esc)
 * is asked again there, and only there.
 */
export class QuestionAsker {
  private open = false;
  private readonly asked = new Map<string, AskedValue>();

  /** How many answers this call wrote. Never throws. */
  async ask(sessionId: string, ui: DialogUI, deps: ApprovalsDeps, atPrompt: boolean, log: (message: string) => void = () => {}): Promise<number> {
    if (!ui.hasUI || this.open) return 0;
    this.open = true;
    let written = 0;
    try {
      for (;;) {
        const next = heldQuestions(sessionId, deps).find((q) => {
          const seen = this.asked.get(q.id);
          return seen === undefined || (atPrompt && seen === Asked.Dismissed);
        });
        if (!next) return written;
        this.asked.set(next.id, Asked.Open);
        let choice: string | undefined;
        try {
          choice = await ui.select(next.question, [next.yesLabel, next.noLabel]);
        } catch (err) {
          choice = undefined;
          log(`question ${next.id}: the dialog failed: ${err instanceof Error ? err.message : String(err)}`);
        }
        if (typeof choice !== "string" || !choice) {
          // Esc: nothing written. Asked again at the next prompt; no further
          // question now, the researcher just waved one away.
          this.asked.set(next.id, Asked.Dismissed);
          return written;
        }
        this.asked.set(next.id, Asked.Done);
        if (writeAnswer(sessionId, next, choice, deps)) {
          written += 1;
          log(`question ${next.id}: answered ${choice === next.yesLabel ? Verdict.Yes : Verdict.No} in pi's dialog`);
        } else {
          log(`question ${next.id}: no longer waiting, the pick was not written`);
        }
      }
    } catch (err) {
      log(`held questions: ${err instanceof Error ? err.message : String(err)}`);
      return written;
    } finally {
      this.open = false;
    }
  }
}
