/**
 * The per-conversation tracking switch, for pi.
 *
 * WHAT WAS MISSING. On Claude Code and Codex the switch is not the skill --
 * it is `hooks/tracking_guard.py`, wired at `UserPromptSubmit` and
 * `PostToolUse`, which parses the invocation and writes the session's
 * tracking signal. `skills/track-work/SKILL.md` is only the manual. pi shipped
 * with the manual and no mechanism, so `/skill:track-work off` loaded guidance
 * and changed nothing: the skill told the researcher a switch existed, the
 * agent read `probe session status`, saw no change, and correctly reported a
 * broken hook rather than reaching for `probe session track` itself (which the
 * skill forbids, because an agent that can flip the switch can unblock its own
 * writes and the opt-out means nothing).
 *
 * WHY THIS IS SMALLER THAN THE PYTHON GUARD. The hard part of that file is
 * proving a PERSON typed it: it has no direct signal, so it infers personhood
 * from the invocation's SHAPE (`RESEARCHER_SHAPES`, the raw-line vs tool-call
 * distinction, the `track-work`-is-also-a-manual carve-out). pi hands us the
 * answer as a field -- `input` events carry `source: "interactive" | "rpc" |
 * "extension"` -- so the whole shape taxonomy collapses into one equality
 * check, made by the caller in `extension.ts`. What stays here is the
 * vocabulary, which is deliberately IDENTICAL to the guard's
 * (`OFF_WORDS`/`ON_WORDS`/`TOGGLE_WORDS`/`STATUS_WORDS`): two implementations
 * of consent logic that disagree about what "resume" means is exactly the
 * drift worth avoiding.
 *
 * THE WRITE IS THE SERVICE'S. `extension.ts` forwards the typed line (`input`) to the Probe
 * service, whose prompt routes (the Python guard) move the switch (service-only task 16): this
 * file only reads the line and words the notice.
 */

/** Verbatim from `hooks/tracking_guard.py`; keep the two in step. */
const OFF_WORDS = new Set(["off", "stop", "disable", "end"]);
const ON_WORDS = new Set(["on", "start", "resume", "full"]);
// `read` leads because it is the word the switch now prints; the hyphenated
// spellings are what the state is stored as and what earlier versions taught
// people to type, and every one of them keeps working. Mirrors
// `session_marker.TRACKING_READ_ONLY_VALUES` and the guard's `READ_ONLY_WORDS`.
const READ_ONLY_WORDS = new Set(["read", "read-only", "readonly", "read_only", "ro"]);
const TOGGLE_WORDS = new Set(["toggle", "flip", "cycle", "next"]);
const STATUS_WORDS = new Set(["status"]);
// `/probe inline` (Richard 2026-10-06): the researcher hands Probe to the agent
// and the daemon stops. `tracking_guard.py::INLINE_WORDS`; only an interactive
// source reaches this function, so it is always the researcher's.
const INLINE_WORDS = new Set(["inline"]);
// `/probe auto`: the researcher hands this chat back to auto mode, its state
// unchanged (`session_marker.hand_chat_to_auto`). Not a state, so not in the
// state words. `tracking_guard.py::AUTO_WORDS`; interactive sources only.
const AUTO_WORDS = new Set(["auto"]);

/**
 * Slugs that predate the third state, and what `off` means when typed at one.
 *
 * `/track-work off` has always meant "stop recording, keep searching" -- the
 * switch never gated reads -- which is `read-only` exactly. Re-pointing it at
 * the new `off` would take reads away from everyone with that phrase in muscle
 * memory. Verbatim from `tracking_guard.py::LEGACY_SLUGS`; keep the two in step.
 */
const LEGACY_SLUGS = new Set(["track-work", "toggle-research-tracking", "research-tracking"]);

/**
 * Every spelling a researcher might type, canonicalised to pi's real one.
 *
 * pi expands ONLY `/skill:<name>` (`agent-session.js`'s `_expandSkillCommand`
 * returns its input untouched otherwise), so `/track-work` and `$track-work`
 * -- the two spellings SKILL.md advertises, and the two a researcher arrives
 * from Claude Code or Codex already typing -- reach pi as ordinary prose. They
 * are matched here and REWRITTEN to `/skill:track-work` so all three behave
 * identically: the switch flips AND the guidance still loads, instead of the
 * model receiving a bare line that looks like a command pi ignored.
 */
/**
 * The switch's own spellings FIRST, then the legacy ones it inherited.
 *
 * Order is load-bearing: `find` takes the first prefix that matches, and
 * `/probe` must not be shadowed. The legacy names stay forever -- matching a
 * name that no longer resolves costs nothing, missing one that did costs the
 * flip.
 */
const SPELLINGS = [
  "/skill:probe",
  "/probe",
  "$probe",
  "/skill:track-work",
  "/track-work",
  "$track-work",
] as const;
const CANONICAL = "/skill:probe";
const LEGACY_CANONICAL = "/skill:track-work";

/**
 * The switch's three positions, the relative request, and `inline` (the
 * researcher's hand-over to the agent, `session_marker.switch_to_inline`).
 * Mirrors `session_marker.SWITCH_STATES`. The daemon is not a position (Richard
 * 2026-09-29): it is chosen in `probe wizard`, and a typed `daemon` moves
 * nothing -- it canonicalises like any unrecognised word and the skill says so.
 * `auto` moves no state: it hands the chat back to auto mode.
 */
export type SwitchDirection = "full" | "read-only" | "off" | "cycle" | "inline" | "auto";

export interface SwitchIntent {
  direction: SwitchDirection | null;
  /** `text` rewritten to pi's own spelling, for `{action: "transform"}`. */
  canonicalText: string;
}

/**
 * The switch intent in a raw input line, or null when the line is not one.
 *
 * `direction: null` with a non-null return is a real outcome, not a miss: it
 * is `status`, or an argument nobody recognises. Those still canonicalise (so
 * `/track-work status` loads the manual) and still must not move the switch --
 * asking a question never flips one.
 */
export function parseSwitchIntent(text: string): SwitchIntent | null {
  const trimmed = text.trim();
  const spelling = SPELLINGS.find(
    (candidate) => trimmed === candidate || trimmed.startsWith(candidate + " "),
  );
  if (!spelling) return null;

  const rest = trimmed.slice(spelling.length).trim();
  // A legacy spelling canonicalises to the MANUAL it has always loaded, not to
  // the new switch skill. Someone typing `/track-work off` wants both halves of
  // what that line used to do: the switch moves, and the manual still opens.
  const slug = spelling.replace(/^[/$]/, "").replace(/^skill:/, "");
  const legacy = LEGACY_SLUGS.has(slug);
  const head = legacy ? LEGACY_CANONICAL : CANONICAL;
  const canonicalText = rest ? `${head} ${rest}` : head;
  const first = rest.split(/\s+/)[0]?.toLowerCase() ?? "";

  // Bare is a TOGGLE. Only an interactive source reaches this function (see
  // extension.ts), which is the researcher reaching for the switch -- the
  // guard's `RESEARCHER_SHAPES` branch, reached by a field instead of a guess.
  if (!first) return { direction: "cycle", canonicalText };
  if (READ_ONLY_WORDS.has(first)) return { direction: "read-only", canonicalText };
  // THE SLUG DECIDES WHAT `off` MEANS. See LEGACY_SLUGS.
  if (OFF_WORDS.has(first)) {
    return { direction: legacy ? "read-only" : "off", canonicalText };
  }
  if (ON_WORDS.has(first)) return { direction: "full", canonicalText };
  if (INLINE_WORDS.has(first)) return { direction: "inline", canonicalText };
  if (AUTO_WORDS.has(first)) return { direction: "auto", canonicalText };
  if (TOGGLE_WORDS.has(first)) return { direction: "cycle", canonicalText };
  if (STATUS_WORDS.has(first)) return { direction: null, canonicalText };
  return { direction: null, canonicalText };
}

/**
 * The line appended to the transformed input after the switch has MOVED.
 *
 * Observed live, and the reason this exists: on a BARE `/skill:track-work` the
 * model received the manual, summarised it, and told the researcher "tracking
 * is currently your default" -- while the switch had in fact just flipped to
 * off. The skill already says to read the state back rather than decide it,
 * but a bare invocation reads as "explain yourself" and the read-back gets
 * skipped. Saying a write HAPPENED, at the moment it happened, is what the
 * skill text alone cannot do: it is written once and cannot know.
 *
 * Deliberately states only that the signal moved and that the state must be
 * read -- never which way it landed. A relative flip resolved against a state
 * this handler did not read would be a second opinion competing with
 * `probe session status`, which is the one authority the skill names.
 */
export function switchAppliedNotice(direction: SwitchDirection): string {
  return (
    `\n\n(The Probe switch was just moved by the \`${direction}\` request above. ` +
    "Read the resulting state back with `probe session status` and report that, " +
    "rather than describing what tracking does.)"
  );
}

/**
 * The line appended to a typed `/probe auto` after the service ran it: what the
 * service told the person (handed back, already auto, or a computer in manual
 * mode), so the model hears the same outcome and never that a switch moved.
 * Empty when the service said nothing (its write failed).
 */
export function handBackNotice(lines: readonly string[]): string {
  return lines.length ? `\n\n(${lines.join(" ")})` : "";
}

/**
 * Whether `prompt` (a `before_agent_start` prompt) is the turn of the line a typed `/probe auto` became
 * (`typed`, its transformed text): that text itself, or it as pi expands a `/skill:` line (the skill's block, then
 * the line's arguments). Any other prompt -- an earlier one still queued -- is not.
 */
export function isTypedSwitchPrompt(prompt: unknown, typed: string): boolean {
  if (typeof prompt !== "string" || !typed) return false;
  if (prompt === typed || prompt.startsWith(typed)) return true;
  const space = typed.indexOf(" ");
  const args = space === -1 ? "" : typed.slice(space + 1).trim();
  return typed.startsWith("/skill:") && !!args && prompt.endsWith(`\n\n${args}`);
}
