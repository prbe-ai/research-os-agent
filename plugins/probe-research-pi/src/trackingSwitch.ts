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
 * THE WRITE IS NOT REIMPLEMENTED EITHER. This spawns the CLI's existing
 * `probe session track|untrack|toggle`, which the CLI documents as the path
 * "for reconciling a machine where that hook is absent". There is one writer
 * of the tracking signal and it stays in Python.
 */

import { type PathEnv } from "./paths.js";
import { findProbeBinary, type ProbeBinaryDeps } from "./teamNote.js";

/** Verbatim from `hooks/tracking_guard.py`; keep the two in step. */
const OFF_WORDS = new Set(["off", "stop", "disable", "end"]);
const ON_WORDS = new Set(["on", "start", "resume", "full"]);
const READ_ONLY_WORDS = new Set(["read-only", "readonly", "read_only", "ro"]);
const TOGGLE_WORDS = new Set(["toggle", "flip", "cycle", "next"]);
const STATUS_WORDS = new Set(["status"]);

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

/** The three states, plus the relative request. Mirrors `session_marker.STATES`. */
export type SwitchDirection = "full" | "read-only" | "off" | "cycle";

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
 * The CLI call each direction makes. ARGUMENT LISTS, not one verb: the switch
 * grew a value, and `session state <name>` is two tokens where `track` was one.
 *
 * The write is still not reimplemented here -- this spawns the CLI's own
 * commands, so there is exactly one writer of the state and it stays in Python.
 */
const SUBCOMMAND: Record<SwitchDirection, readonly string[]> = {
  full: ["state", "full"],
  "read-only": ["state", "read-only"],
  off: ["state", "off"],
  cycle: ["toggle"],
};

/**
 * A child we can WAIT ON, which `daemon.ts`/`teamNote.ts`'s `SpawnFn` is not:
 * those return `{pid, unref}` because both detach and never look back. This
 * one has to resolve on exit — see `applyTrackingSwitch` for why the write is
 * awaited rather than fired and forgotten.
 */
export interface SwitchChild {
  on: (event: string, cb: (arg: unknown) => void) => void;
}

export type SwitchSpawnFn = (
  command: string,
  args: string[],
  options: { detached: boolean; stdio: "ignore"; env: PathEnv },
) => SwitchChild;

export interface TrackingSwitchDeps extends ProbeBinaryDeps {
  spawn: SwitchSpawnFn;
  log: (message: string) => void;
}

/**
 * Write the signal, and RESOLVE ONLY ONCE THE WRITE HAS LANDED.
 *
 * Deliberately awaited, unlike `spawnTeamNoteSync`'s detached fire-and-forget.
 * The very next thing that happens is the model receiving the skill text,
 * which instructs it to read the state back with `probe session status`; a
 * detached write would race that read and the model would report the OLD
 * state as authoritative. The write itself is a local marker file, so the cost
 * is the CLI's import time and nothing else, and it is only ever paid on a
 * line that already matched a switch spelling.
 *
 * Never throws and never rejects: a broken switch must degrade to "the skill
 * says it did not move", which the agent is already told how to report, rather
 * than taking down the researcher's turn.
 */
export async function applyTrackingSwitch(
  direction: SwitchDirection,
  sessionId: string,
  deps: TrackingSwitchDeps,
): Promise<boolean> {
  const binary = findProbeBinary(deps);
  if (!binary) {
    deps.log("tracking switch skipped: no probe CLI found on PATH or in the documented fallback locations");
    return false;
  }
  try {
    const child = deps.spawn(binary, ["session", ...SUBCOMMAND[direction], "--session", sessionId], {
      detached: false,
      stdio: "ignore",
      env: { ...deps.env, PROBE_AGENT: "pi" } as PathEnv,
    });
    const code = await waitForExit(child);
    if (code !== 0) {
      deps.log(`tracking switch (${direction}) exited ${code}`);
      return false;
    }
    deps.log(`tracking switch: ${direction} for ${sessionId}`);
    return true;
  } catch (err) {
    deps.log(`tracking switch (${direction}) failed: ${err instanceof Error ? err.message : String(err)}`);
    return false;
  }
}

function waitForExit(child: SwitchChild): Promise<number> {
  return new Promise((resolve) => {
    child.on("error", () => resolve(-1));
    child.on("exit", (code) => resolve(typeof code === "number" ? code : -1));
  });
}
