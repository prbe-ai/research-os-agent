/**
 * Which `probe` commands a shell line runs, and which of them the daemon
 * profile leaves to the agent. A port of the Python that answers the same
 * question for Claude Code and Codex, so one command is judged alike on every
 * harness:
 *
 *   - `tracking_guard.py`: `_strip_heredocs`, `_segments`, `_probe_invocations`
 *     and `_asks_help` (the shell parse);
 *   - `session_marker.py`: `command_words`, `classify_probe_args` (only the
 *     `matched` half: the daemon profile never asks for the kind),
 *     `daemon_allows_agent` and `daemon_profile_allows`.
 *
 * `_segments` tokenizes with Python's `shlex` (posix, `punctuation_chars`,
 * `whitespace_split`); `shlexTokens` below is that tokenizer's state machine,
 * restricted to the options the guard sets. Every constant is the Python
 * one's copy: `agent/tests/test_pi_guard_parity.py` pins each, and runs the
 * shared fixture `agent/tests/fixtures/pi_probe_commands.json` through the
 * Python while `tests/probeCommands.test.ts` runs it through this file.
 *
 * A TRIPWIRE, like the Python: a command that builds `probe` at run time still
 * runs it. The CLI's own write gate is the boundary.
 */

// ---------------------------------------------------------------------------
// session_marker: what a `probe` command IS.
// ---------------------------------------------------------------------------

/** `session_marker.TOP_LEVEL_WRITES`. */
export const TOP_LEVEL_WRITES: ReadonlySet<string> = new Set(["log", "link", "snapshot", "exec", "backfill", "import"]);

/** `session_marker.WRITE_GROUPS`. */
export const WRITE_GROUPS: ReadonlySet<string> = new Set([
  "project",
  "experiment",
  "run",
  "artifact",
  "notes",
  "span",
  "group",
  "edge",
  "trial",
  "views",
  "paper",
  "wandb",
  "version",
]);

/** `session_marker.SUBGROUPS`. */
export const SUBGROUPS: ReadonlySet<string> = new Set(["project code", "project reference", "run input", "wandb key"]);

/** The keys of `session_marker.READ_UNLESS_FLAGS` (its flags decide the kind, never `matched`). */
export const READ_UNLESS_FLAGS_COMMANDS: ReadonlySet<string> = new Set(["project contributors"]);

/** `session_marker.READ_VERBS`. */
export const READ_VERBS: ReadonlySet<string> = new Set([
  "show",
  "list",
  "get",
  "status",
  "versions",
  "cat",
  "diff",
  "search",
  "export",
  "events",
  "coordinates",
  "download",
  "help",
  "team",
  "check",
  "reproduce",
  "checkout",
  "audit-advisory",
  "tree",
  "pin-impact",
  "edges",
  "metrics",
  "series",
  "data",
  "preview",
  "inputs",
  "upstream",
  "citations",
  "graph",
]);

/** `session_marker.REMOVAL_VERBS`. */
export const REMOVAL_VERBS: ReadonlySet<string> = new Set(["delete", "remove", "rm", "prune", "purge"]);

/** `session_marker.UNGATED_COMMANDS`. */
export const UNGATED_COMMANDS: ReadonlySet<string> = new Set([
  "notes sync",
  "project use",
  "wandb discover",
  "wandb key set",
  "wandb key status",
]);

/** `session_marker.DIRECTED_ONLY`. */
export const DIRECTED_ONLY: ReadonlySet<string> = new Set(["companion feedback"]);

/** `session_marker.ROOT_VALUE_OPTIONS`. */
export const ROOT_VALUE_OPTIONS: ReadonlySet<string> = new Set(["--base-url", "--spool-dir"]);

/** `session_marker.HELP_FLAG`. */
export const HELP_FLAG = "--help";

/** `session_marker.READ_GROUPS`. */
export const READ_GROUPS: ReadonlySet<string> = new Set(["metrics", "series", "get", "bundle", "events", "coordinates", "shared"]);

/** `session_marker.DAEMON_AGENT_WRITES`: the agent's writes under the daemon (a run and its own data). */
export const DAEMON_AGENT_WRITES: ReadonlySet<string> = new Set([
  "exec",
  "log",
  "snapshot",
  "run start",
  "run child",
  "run fork",
  "run end",
  "span add",
  "trial add",
  "trial stage",
  "trial export",
  "trial drain",
  "trial watch",
  "trial reconcile",
  "trial expand",
]);

/** `session_marker.DAEMON_PROFILE_ALLOWED`: what else the daemon profile leaves to the agent. */
export const DAEMON_PROFILE_ALLOWED: ReadonlySet<string> = new Set(["ask", "session status", "run expect", "doctor"]);

/** `session_marker.command_words`: the (at most three) words click dispatches on. */
export function commandWords(args: readonly string[]): string[] {
  const words: string[] = [];
  let skipValue = false;
  for (const token of args) {
    if (skipValue) {
      skipValue = false;
      continue;
    }
    if (token === "--") {
      if (words[0] === "exec") break;
      continue;
    }
    if (token.startsWith("-")) {
      if (words.length === 0 && ROOT_VALUE_OPTIONS.has(token)) skipValue = true;
      continue;
    }
    words.push(token);
    if (words.length === 3) break;
  }
  return words;
}

/** `session_marker.classify_probe_args`'s `matched`: the words a refusal names. */
export function matchedCommand(args: readonly string[]): string {
  const words = commandWords(args);
  if (words.length === 0) return "";
  const group = words[0];
  let head = words[0];
  let verb = words[1] ?? "";
  if (SUBGROUPS.has(`${head} ${verb}`)) {
    head = `${head} ${verb}`;
    verb = words[2] ?? "";
  }
  const command = `${head} ${verb}`;
  if (UNGATED_COMMANDS.has(command)) return `probe ${command}`;
  if (TOP_LEVEL_WRITES.has(head) || READ_GROUPS.has(head)) return `probe ${head}`;
  if (DIRECTED_ONLY.has(command) || READ_UNLESS_FLAGS_COMMANDS.has(command)) return `probe ${command}`;
  if (WRITE_GROUPS.has(group) && (READ_VERBS.has(verb) || (verb && !REMOVAL_VERBS.has(verb)))) {
    return `probe ${command}`;
  }
  return `probe ${head}`;
}

/** `session_marker.daemon_allows_agent`. */
export function daemonAllowsAgent(matched: string): boolean {
  return DAEMON_AGENT_WRITES.has(matched.startsWith("probe ") ? matched.slice("probe ".length) : matched);
}

/** `session_marker.daemon_profile_allows`: `[allowed, matched]` for the args after `probe`. */
export function daemonProfileAllows(args: readonly string[]): [boolean, string] {
  const words = commandWords(args);
  if (words.length === 0) return [true, "probe"];
  const head = words[0];
  const two = words.slice(0, 2).join(" ");
  if (DAEMON_PROFILE_ALLOWED.has(two)) return [true, `probe ${two}`];
  if (DAEMON_PROFILE_ALLOWED.has(head)) return [true, `probe ${head}`];
  const matched = matchedCommand(args);
  return [daemonAllowsAgent(matched), matched];
}

// ---------------------------------------------------------------------------
// tracking_guard: the shell parse.
// ---------------------------------------------------------------------------

/** `tracking_guard._OPERATOR_CHARS`: what shlex hands back as operator tokens. */
export const OPERATOR_CHARS = "();<>|&\n";
/** `tracking_guard._SEPARATOR_CHARS`: an operator token made of these ends a command. */
export const SEPARATOR_CHARS: ReadonlySet<string> = new Set([";", "|", "&", "(", ")", "\n"]);
/** `tracking_guard._REDIRECTIONS`: never a separator. */
export const REDIRECTIONS: ReadonlySet<string> = new Set([">", ">>", "<", "<>", ">|", "&>", "&>>", ">&", "<&", "<<", "<<<"]);
/** `tracking_guard._WORD_BOUNDARY`: what may precede a `#` that starts a comment. */
export const WORD_BOUNDARY: ReadonlySet<string> = new Set([" ", "\t", "\n", ";", "&", "|", "(", ")", "<", ">"]);
/** `tracking_guard._HEREDOC_DELIMITER`'s pattern. */
export const HEREDOC_DELIMITER_SOURCE = String.raw`(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][A-Za-z0-9_.-]*))`;
const HEREDOC_DELIMITER = new RegExp(HEREDOC_DELIMITER_SOURCE, "y");

/** `tracking_guard._ENV_ASSIGNMENT`'s pattern. */
export const ENV_ASSIGNMENT_SOURCE = String.raw`^[A-Za-z_][A-Za-z0-9_]*=`;
const ENV_ASSIGNMENT = new RegExp(ENV_ASSIGNMENT_SOURCE);
/** `tracking_guard._WRAPPERS`: commands that run the command after them. */
export const WRAPPERS: ReadonlySet<string> = new Set(["command", "env", "nohup", "exec", "time", "timeout", "nice", "stdbuf", "setsid"]);
/** `tracking_guard._WRAPPER_VALUE_OPTS`. */
export const WRAPPER_VALUE_OPTS: ReadonlySet<string> = new Set(["-n", "-s", "-k", "--signal", "--kill-after", "-u", "-i", "-o", "-e"]);
/** `tracking_guard._SHELLS`. */
export const SHELLS: ReadonlySet<string> = new Set(["sh", "bash", "zsh", "dash"]);
/** `tracking_guard._PYTHONS`'s pattern. */
export const PYTHONS_SOURCE = String.raw`^python(\d+(\.\d+)?)?$`;
const PYTHONS = new RegExp(PYTHONS_SOURCE);
/** `tracking_guard._PROBE_MODULES`: what `python -m` runs the CLI as. */
export const PROBE_MODULES: ReadonlySet<string> = new Set(["probe", "probe.cli", "probe.cli.main"]);

const Ctx = {
  Shell: "sh",
  Subst: "$(",
  Paren: "(",
  Single: "'",
  Double: '"',
  Arith: "((",
  ArithParen: "a(",
} as const;
type CtxValue = (typeof Ctx)[keyof typeof Ctx];

/** `tracking_guard._skip_heredoc_bodies`. */
function skipHeredocBodies(command: string, start: number, pending: Array<[string, boolean]>): number {
  let index = start;
  while (pending.length && index < command.length) {
    const newline = command.indexOf("\n", index);
    const line = newline < 0 ? command.slice(index) : command.slice(index, newline);
    const [word, tabs] = pending[0];
    if ((tabs ? line.replace(/^\t+/, "") : line) === word) pending.shift();
    index = newline < 0 ? command.length : newline + 1;
  }
  pending.length = 0; // an unterminated body runs to the end: all of it is dropped
  return index;
}

/** `tracking_guard._strip_heredocs`: the command with every heredoc BODY removed. */
export function stripHeredocs(command: string): string {
  let out = "";
  const pending: Array<[string, boolean]> = [];
  const stack: CtxValue[] = [Ctx.Shell];
  let index = 0;
  const size = command.length;
  while (index < size) {
    const top = stack[stack.length - 1];
    const char = command[index];
    if (top === Ctx.Single) {
      if (char === "'") stack.pop();
      out += char;
      index += 1;
      continue;
    }
    if (char === "\\") {
      out += command.slice(index, index + 2);
      index += 2;
      continue;
    }
    if (top === Ctx.Arith || top === Ctx.ArithParen) {
      if (char === "(") {
        stack.push(Ctx.ArithParen);
      } else if (char === ")" && top === Ctx.ArithParen) {
        stack.pop();
      } else if (top === Ctx.Arith && command.startsWith("))", index)) {
        stack.pop();
        out += "))";
        index += 2;
        continue;
      }
      out += char;
      index += 1;
      continue;
    }
    const opener = command.startsWith("$((", index) ? "$((" : command.startsWith("$(", index) ? "$(" : null;
    if (opener) {
      stack.push(opener === "$((" ? Ctx.Arith : Ctx.Subst);
      out += opener;
      index += opener.length;
      continue;
    }
    if (top === Ctx.Double) {
      if (char === '"') stack.pop();
      out += char;
      index += 1;
      continue;
    }
    // A shell context: the top level, a `$(...)`, or a paren inside one.
    if (char === "\n" && pending.length) {
      out += char;
      index = skipHeredocBodies(command, index + 1, pending);
      continue;
    }
    if (char === "#" && (!out || WORD_BOUNDARY.has(out[out.length - 1]))) {
      const end = command.indexOf("\n", index);
      index = end < 0 ? size : end;
      continue;
    }
    if (char === "'" || char === '"') {
      stack.push(char === "'" ? Ctx.Single : Ctx.Double);
    } else if (command.startsWith("((", index)) {
      stack.push(Ctx.Arith);
      out += "((";
      index += 2;
      continue;
    } else if (char === "(" && top !== Ctx.Shell) {
      stack.push(Ctx.Paren);
    } else if (char === ")" && top !== Ctx.Shell) {
      stack.pop();
    } else if (command.startsWith("<<<", index)) {
      out += "<<<";
      index += 3;
      continue;
    } else if (command.startsWith("<<", index)) {
      HEREDOC_DELIMITER.lastIndex = index + 2;
      const match = HEREDOC_DELIMITER.exec(command);
      if (match) {
        pending.push([match[2] || match[3] || match[4], Boolean(match[1])]);
        const end = HEREDOC_DELIMITER.lastIndex;
        out += command.slice(index, end);
        index = end;
        continue;
      }
      out += "<<";
      index += 2;
      continue;
    }
    out += char;
    index += 1;
  }
  return out;
}

const SHLEX_WHITESPACE = new Set([" ", "\t", "\r"]);
const SHLEX_PUNCTUATION = new Set(OPERATOR_CHARS);
const SHLEX_QUOTES = new Set(["'", '"']);
const SHLEX_ESCAPE = "\\";
const SHLEX_ESCAPED_QUOTES = new Set(['"']);

/**
 * Python's `shlex.shlex(text, posix=True, punctuation_chars=OPERATOR_CHARS)`
 * with `whitespace_split = True`, `whitespace = " \t\r"` and no commenters,
 * iterated. shlex raises on an unclosed quote or a trailing escape while it
 * reads the LAST token, after yielding every one before it: `complete` is
 * false then, and `tokens` holds what it had yielded.
 */
export function shlexTokens(text: string): { tokens: string[]; complete: boolean } {
  const tokens: string[] = [];
  const chars = [...text];
  let at = 0;
  let pushback: string | null = null;
  // " " between tokens, "a" in a word, "c" in an operator, a quote char, or the escape.
  let state: string | null = " ";
  while (state !== null) {
    let token = "";
    let quoted = false;
    let escapedState = " ";
    for (;;) {
      let next: string;
      if (pushback !== null) {
        next = pushback;
        pushback = null;
      } else {
        next = at < chars.length ? chars[at++] : "";
      }
      if (state === " ") {
        if (!next) {
          state = null;
          break;
        } else if (SHLEX_WHITESPACE.has(next)) {
          if (token || quoted) break;
        } else if (next === SHLEX_ESCAPE) {
          escapedState = "a";
          state = next;
        } else if (SHLEX_PUNCTUATION.has(next)) {
          token = next;
          state = "c";
        } else if (SHLEX_QUOTES.has(next)) {
          state = next;
        } else {
          token = next;
          state = "a";
        }
      } else if (SHLEX_QUOTES.has(state)) {
        quoted = true;
        if (!next) return { tokens, complete: false }; // "No closing quotation"
        if (next === state) {
          state = "a";
        } else if (next === SHLEX_ESCAPE && SHLEX_ESCAPED_QUOTES.has(state)) {
          escapedState = state;
          state = next;
        } else {
          token += next;
        }
      } else if (state === SHLEX_ESCAPE) {
        if (!next) return { tokens, complete: false }; // "No escaped character"
        // Within quotes only the quote itself or the escape may be escaped.
        if (SHLEX_QUOTES.has(escapedState) && next !== state && next !== escapedState) token += state;
        token += next;
        state = escapedState;
      } else {
        // "a" or "c"
        if (!next) {
          state = null;
          break;
        } else if (SHLEX_WHITESPACE.has(next)) {
          state = " ";
          if (token || quoted) break;
        } else if (state === "c") {
          if (SHLEX_PUNCTUATION.has(next)) {
            token += next;
          } else {
            pushback = next;
            state = " ";
            break;
          }
        } else if (SHLEX_QUOTES.has(next)) {
          state = next;
        } else if (next === SHLEX_ESCAPE) {
          escapedState = "a";
          state = next;
        } else if (!SHLEX_PUNCTUATION.has(next)) {
          token += next;
        } else {
          pushback = next;
          state = " ";
          if (token || quoted) break;
        }
      }
    }
    if (token || quoted) tokens.push(token);
  }
  return { tokens, complete: true };
}

function isSeparator(token: string): boolean {
  if (REDIRECTIONS.has(token)) return false;
  const chars = [...token];
  return chars.every((c) => SHLEX_PUNCTUATION.has(c)) && chars.some((c) => SEPARATOR_CHARS.has(c));
}

/**
 * `tracking_guard._segments`: each simple command's words, split on the
 * operators OUTSIDE quotes. A segment still open when the quotes stop
 * balancing is dropped, not reported: the text after an unclosed quote is
 * inside it.
 */
export function segments(command: string): string[][] {
  // A backslash-newline is removed, as bash does.
  const { tokens, complete } = shlexTokens(stripHeredocs(command).replaceAll("\\\n", ""));
  const out: string[][] = [];
  let segment: string[] = [];
  for (const token of tokens) {
    if (isSeparator(token)) {
      if (segment.length) out.push(segment);
      segment = [];
    } else {
      segment.push(token);
    }
  }
  if (complete && segment.length) out.push(segment);
  return out;
}

function basename(word: string): string {
  return word.slice(word.lastIndexOf("/") + 1);
}

/** `tracking_guard._probe_invocations`: the args after `probe`, for every invocation in a shell line. */
export function probeInvocations(command: string, depth = 0): string[][] {
  const found: string[][] = [];
  for (const tokens of segments(command)) {
    let index = 0;
    while (index < tokens.length) {
      const word = tokens[index];
      if (ENV_ASSIGNMENT.test(word)) {
        index += 1;
      } else if (REDIRECTIONS.has(word)) {
        index += 2; // a leading redirection and its file
      } else if (WRAPPERS.has(basename(word))) {
        const wrapper = basename(word);
        index += 1;
        while (index < tokens.length && tokens[index].startsWith("-")) {
          index += WRAPPER_VALUE_OPTS.has(tokens[index]) ? 2 : 1;
        }
        if (wrapper === "timeout" && index < tokens.length) index += 1; // its duration
      } else {
        break;
      }
    }
    if (index >= tokens.length) continue;
    const head = basename(tokens[index]);
    if (head === "probe") {
      found.push(tokens.slice(index + 1));
    } else if (PYTHONS.test(head) && tokens[index + 1] === "-m" && index + 2 < tokens.length && PROBE_MODULES.has(tokens[index + 2])) {
      found.push(tokens.slice(index + 3));
    } else if (SHELLS.has(head) && depth < 2 && tokens.indexOf("-c", index + 1) >= 0) {
      const at = tokens.indexOf("-c", index + 1);
      if (at + 1 < tokens.length) found.push(...probeInvocations(tokens[at + 1], depth + 1));
    }
  }
  return found;
}

/** `tracking_guard._asks_help`: `--help` before any `--`, and not a redirection's file. */
export function asksHelp(args: readonly string[]): boolean {
  const end = args.indexOf("--");
  const words = end >= 0 ? args.slice(0, end) : args;
  return words.some((word, i) => word === HELP_FLAG && (i === 0 || !REDIRECTIONS.has(words[i - 1])));
}

/** The first `probe` invocation in a shell line the daemon profile refuses, by its `matched` words; null if none. */
export function daemonProfileRefusal(command: string): string | null {
  for (const args of probeInvocations(command)) {
    if (asksHelp(args)) continue;
    const [allowed, matched] = daemonProfileAllows(args);
    if (!allowed) return matched;
  }
  return null;
}
