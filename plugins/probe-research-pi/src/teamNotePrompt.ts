/**
 * The team note in pi's prompt (the shared half, reading and syncing the
 * document, is `core/teamNote.ts`).
 *
 * INJECT, DO NOT RENDER. Claude Code and Codex get the note by having a
 * managed block rewritten into their global instruction file
 * (`~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md`) at sync time, and reading
 * that file — like any instruction file — at the START of their NEXT
 * session. pi gets the same content by appending it to `event.systemPrompt`
 * in a `before_agent_start` handler instead (see `extension.ts`'s wiring).
 * Since pi 0.86.0 a returned `systemPrompt` becomes that run's
 * `forceSystemPrompt`, sent as the leading system prompt; `event.systemPrompt`
 * is re-rendered each run from the session's base options, which never carry
 * the forced text, so the note is appended once per turn and never stacks
 * (`agent-session.js` passes `_baseSystemPromptOptions` to
 * `emitBeforeAgentStart`, and `runner.js` copies them first).
 * This sidesteps two things the render path has to deal with: it never
 * writes to a file the researcher owns (an `AGENTS.md` a person edits by
 * hand), and it can never collide with `AGENTS.override.md` shadowing a
 * project's `AGENTS.md` (pi 0.86.0's `resource-loader.js`, same as 0.84.3's,
 * checks `AGENTS.override.md` before `AGENTS.md` in the same directory — irrelevant
 * to text injected straight into the prompt, and a real hazard for anything
 * written to disk instead).
 *
 * CACHE ONCE PER SESSION. `before_agent_start` fires on every turn; reading
 * and re-parsing the note file that often would be pure waste for content
 * that — by design — only ever changes between sessions (`core/teamNote.ts`'s
 * ONE SYNC TRIGGER). The cache lives in `extension.ts`'s module scope, populated once at
 * `session_start` by `readTeamNote()`, and injected unchanged into every
 * `before_agent_start` of that session by `renderTeamNoteForPrompt()` below.
 */

/**
 * Format the cached note for appending to `event.systemPrompt`. Named the
 * real, absolute file path — the whole point of the team note is that "you
 * edit that file like any other markdown," and an agent that cannot see
 * where the file lives cannot do that.
 */
export function renderTeamNoteForPrompt(note: string, documentPath: string): string {
  return (
    `\n\n## Probe team note\n\n` +
    `The lab's shared memory -- what this team is working on, has decided, and what not to repeat. ` +
    `Edit \`${documentPath}\` directly to change it for the team; edits sync automatically.\n\n` +
    `${note.trim()}\n`
  );
}
