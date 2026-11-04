/**
 * What the Probe service answers pi's forwarded events with (`hooks.run` v2, `reply: "actions"`).
 *
 * Twin: `src/probe/foundation/service_api/hook_actions.py` (`HookAction`). One fixture set,
 * `agent/tests/fixtures/hook_actions.json`, drives both sides' tests. A kind this version does not
 * know, or an action missing what its kind needs, is dropped and logged: a newer service never
 * breaks an older extension.
 */

/** One thing to do with an event's answer (`HookAction`). */
export const ServiceAction = {
  /** Append `text` to this turn's system prompt. */
  SystemPrompt: "system_prompt",
  /** Keep `text` for the session: appended to every later turn's system prompt. */
  Context: "context",
  /** This turn's message to the model (`text`, not shown to the person). */
  Message: "message",
  /** The typed line: `action` continue, or transform to `text`. */
  Input: "input",
  /** Refuse the tool call, with `text` as the reason. */
  Block: "block",
  /** Keep the agent going: one more entry (`text`, shown) and continue. */
  Continue: "continue",
  /** Tell the person `text` at `level`. */
  Notify: "notify",
  /** Set the footer item `key` to `text` (none clears it). */
  Status: "status",
  /** Ack delivery `receipt` once this answer was applied (`hooks.ack`). */
  Receipt: "receipt",
} as const;
export type ServiceActionKind = (typeof ServiceAction)[keyof typeof ServiceAction];
const KINDS: ReadonlySet<string> = new Set(Object.values(ServiceAction));

/** `ServiceAction.Notify`'s levels (pi's `ui.notify`). */
export const NotifyLevel = { Info: "info", Warning: "warning", Error: "error" } as const;
export type NotifyLevelValue = (typeof NotifyLevel)[keyof typeof NotifyLevel];
const LEVELS: ReadonlySet<string> = new Set(Object.values(NotifyLevel));

/** `ServiceAction.Input`'s `action` (pi's `InputEventResult` words). */
export const InputAction = { Continue: "continue", Transform: "transform" } as const;
export type InputActionValue = (typeof InputAction)[keyof typeof InputAction];
const INPUT_ACTIONS: ReadonlySet<string> = new Set(Object.values(InputAction));

/** A receipt id: 32 lowercase hex digits (`HOOK_EVENT_ID_LEN`). */
const RECEIPT_ID = /^[0-9a-f]{32}$/;

export interface ReplyAction {
  kind: ServiceActionKind;
  text?: string;
  level?: NotifyLevelValue;
  key?: string;
  action?: InputActionValue;
  receipt?: string;
}

function isText(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

/** One wire action, checked: null when its kind is unknown or it lacks what its kind needs. */
export function readAction(raw: unknown): ReplyAction | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const fields = raw as Record<string, unknown>;
  if (typeof fields.kind !== "string" || !KINDS.has(fields.kind)) return null;
  const kind = fields.kind as ServiceActionKind;
  switch (kind) {
    case ServiceAction.SystemPrompt:
    case ServiceAction.Context:
    case ServiceAction.Message:
    case ServiceAction.Block:
    case ServiceAction.Continue:
      return isText(fields.text) ? { kind, text: fields.text } : null;
    case ServiceAction.Notify:
      if (!isText(fields.text)) return null;
      return { kind, text: fields.text, level: LEVELS.has(fields.level as string) ? (fields.level as NotifyLevelValue) : NotifyLevel.Info };
    case ServiceAction.Status:
      return isText(fields.key) ? { kind, key: fields.key, ...(isText(fields.text) ? { text: fields.text } : {}) } : null;
    case ServiceAction.Input:
      if (!INPUT_ACTIONS.has(fields.action as string)) return null;
      if (fields.action === InputAction.Transform && typeof fields.text !== "string") return null;
      return { kind, action: fields.action as InputActionValue, ...(typeof fields.text === "string" ? { text: fields.text } : {}) };
    case ServiceAction.Receipt:
      return typeof fields.receipt === "string" && RECEIPT_ID.test(fields.receipt) ? { kind, receipt: fields.receipt } : null;
  }
}

/** Every readable action of an answer's `actions`, in order; the rest dropped (`log` names them). */
export function readActions(raw: unknown, log?: (message: string) => void): ReplyAction[] {
  if (!Array.isArray(raw)) return [];
  const out: ReplyAction[] = [];
  for (const item of raw) {
    const action = readAction(item);
    if (action) out.push(action);
    else log?.(`probe service: an action this extension does not read was dropped: ${JSON.stringify(item).slice(0, 120)}`);
  }
  return out;
}

/** An answer's actions, gathered by what a handler does with them. */
export interface AnswerEffects {
  systemPrompt: string[];
  context: string[];
  messages: string[];
  /** The first refusal's reason, or null. */
  block: string | null;
  /** The first send-back's text, or null. */
  continueText: string | null;
  input: { action: InputActionValue; text?: string } | null;
  notices: { text: string; level: NotifyLevelValue }[];
  status: { key: string; text: string | undefined }[];
  receipts: string[];
}

export function gatherEffects(actions: readonly ReplyAction[]): AnswerEffects {
  const out: AnswerEffects = {
    systemPrompt: [], context: [], messages: [], block: null, continueText: null, input: null, notices: [], status: [], receipts: [],
  };
  for (const action of actions) {
    switch (action.kind) {
      case ServiceAction.SystemPrompt:
        out.systemPrompt.push(action.text!);
        break;
      case ServiceAction.Context:
        out.context.push(action.text!);
        break;
      case ServiceAction.Message:
        out.messages.push(action.text!);
        break;
      case ServiceAction.Block:
        out.block ??= action.text!;
        break;
      case ServiceAction.Continue:
        out.continueText ??= action.text!;
        break;
      case ServiceAction.Input:
        out.input ??= { action: action.action!, ...(action.text !== undefined ? { text: action.text } : {}) };
        break;
      case ServiceAction.Notify:
        out.notices.push({ text: action.text!, level: action.level ?? NotifyLevel.Info });
        break;
      case ServiceAction.Status:
        out.status.push({ key: action.key!, text: action.text });
        break;
      case ServiceAction.Receipt:
        out.receipts.push(action.receipt!);
        break;
    }
  }
  return out;
}
