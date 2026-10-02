/**
 * The daemon profile, for pi: what changes when the CLI says the Probe daemon
 * records and reads for this session (`Recorder.Daemon`, `trackingState.ts`).
 *
 * On Claude Code and Codex the daemon profile is a second, lean plugin
 * (`plugins/probe-research-daemon`): `instrument-code` and its own `probe`
 * skill, no MCP server, no daemon notices. pi has one package for both
 * profiles (decision D1), so this extension narrows ITSELF:
 *
 *   - skills: `resources_discover` adds `daemon-skills/probe` (a byte copy of
 *     the lean plugin's skill), and every prompt lists only `DAEMON_PROFILE_SKILLS`
 *     -- `leanSkills` below, which also covers a project-level pi settings
 *     entry that loads every skill of this package;
 *   - tools: the extension's MCP bridge stays off, and any Probe MCP tool
 *     another extension registered (pi-mcp-adapter) is deactivated;
 *   - messages: no `DAEMON_CONTEXT`, no hand-back notices to the model. A daemon
 *     that stops recording is the RESEARCHER's to hear about, once, through
 *     pi's notification (`researcherDaemonNotice`).
 */

import { readFileSync, realpathSync } from "node:fs";
import { join } from "node:path";

import { DAEMON_REASON_WORDS, DaemonReason, DaemonStatus, type DaemonStatusValue } from "./core/daemonNotice.js";
import { TOOL_NAME_PREFIX } from "./mcpSchema.js";

/** The skills a daemon-profile prompt keeps of this package's: the lean plugin's two. */
export const DAEMON_PROFILE_SKILLS: ReadonlySet<string> = new Set(["instrument-code", "probe"]);

/**
 * Every skill this package ships: package.json's `pi.skills`, which is
 * agent/skills/profiles.json's `full` (tests/profile.test.ts and
 * agent/tests/test_skill_profiles.py hold them together). The daemon profile
 * drops these, less `DAEMON_PROFILE_SKILLS`, and nothing else: a skill the
 * researcher or another package brought is theirs, in either profile.
 */
export const PACKAGE_SKILLS: ReadonlySet<string> = new Set([
  "probe",
  "track-work",
  "visualize-progress",
  "instrument-code",
  "audit-team-note",
  "edit-notes",
  "notes-audit",
]);

/** Where the daemon's `probe` skill lives inside this package. Never named in package.json. */
export const DAEMON_SKILLS_DIR = "daemon-skills";
export const DAEMON_PROBE_SKILL = "probe";

export function daemonSkillDir(extensionDir: string): string {
  return join(extensionDir, DAEMON_SKILLS_DIR, DAEMON_PROBE_SKILL);
}

/**
 * `tracking_guard._PROBE_MCP_RE`: a Probe MCP tool by NAME, whichever client
 * registered it (pi-mcp-adapter's `probe-research-pi__probe...`, pi's own
 * `mcp__probe_research__...`). This extension's bridge spells its tools with
 * `TOOL_NAME_PREFIX`, which the regex does not see, so that is checked too.
 */
export const PROBE_MCP_TOOL_SOURCE = "probe[-_]research";
const PROBE_MCP_TOOL = new RegExp(PROBE_MCP_TOOL_SOURCE, "i");

export function isProbeMcpTool(toolName: string): boolean {
  return toolName.startsWith(TOOL_NAME_PREFIX) || PROBE_MCP_TOOL.test(toolName);
}

/** The fields of pi's `Skill` this module reads or replaces. */
export interface PromptSkill {
  name: string;
  description: string;
  filePath: string;
  baseDir: string;
}

export type DaemonSkill = Omit<PromptSkill, "name">;

/** The daemon skill's file, and the description its frontmatter declares; null when unreadable. */
export function readDaemonSkill(
  extensionDir: string,
  read: (path: string) => string = (path) => readFileSync(path, "utf-8"),
): DaemonSkill | null {
  const baseDir = daemonSkillDir(extensionDir);
  const filePath = join(baseDir, "SKILL.md");
  try {
    const frontmatter = /^---\n([\s\S]*?)\n---/.exec(read(filePath));
    const description = frontmatter ? /^description:\s*(.+)$/m.exec(frontmatter[1])?.[1]?.trim() : undefined;
    return description ? { description, filePath, baseDir } : null;
  } catch {
    return null;
  }
}

function samePath(a: string, b: string): boolean {
  if (a === b) return true;
  try {
    return realpathSync(a) === realpathSync(b);
  } catch {
    return false;
  }
}

/**
 * The prompt's skills with this package's narrowed to `DAEMON_PROFILE_SKILLS`,
 * `probe` the daemon's, and every other skill (the researcher's own
 * `~/.pi/agent/skills`, a project's, another package's) kept as it was.
 *
 * pi keeps the FIRST skill of a name (`skills.js`'s `loadSkills`) and loads an
 * extension's `resources_discover` paths after every package's, so wherever
 * this package's full `skills/probe` is loaded (a project entry with every
 * skill), it wins over `daemon-skills/probe` -- and that one tells the agent it
 * records. Its entry is pointed at the daemon's file instead, so the skill the
 * model is told to read is the daemon's. New objects: pi's own list is not
 * edited in place.
 */
export function leanSkills<T extends PromptSkill>(skills: readonly T[], daemonSkill: DaemonSkill | null): T[] {
  return skills
    .filter((skill) => !PACKAGE_SKILLS.has(skill.name) || DAEMON_PROFILE_SKILLS.has(skill.name))
    .map((skill) =>
      skill.name === DAEMON_PROBE_SKILL && daemonSkill && !samePath(skill.filePath, daemonSkill.filePath)
        ? { ...skill, ...daemonSkill }
        : skill,
    );
}

/**
 * What the researcher hears when the daemon stops recording, once per change.
 *
 * The daemon profile gives the agent no `track-work` and refuses its Probe
 * writes, so a daemon without a live lease means nobody records this session:
 * the researcher is the one who can act on that, and the model is told
 * nothing (the lean plugin's rule). Same grace as `daemonNotice`: the worker
 * is spawned at session start and may not hold its first lease at the first
 * prompt, so `not-started` waits one prompt -- when it has a key to start with.
 */
export function researcherDaemonNotice(
  previous: DaemonStatusValue | null,
  status: DaemonStatusValue,
  reason: string | null,
  firstPrompt: boolean,
  keyHeld: () => boolean,
): { notice: string | null; next: DaemonStatusValue | null } {
  if (status === DaemonStatus.Live) return { notice: null, next: status };
  if (reason === DaemonReason.NotStarted && previous === null && firstPrompt && keyHeld()) {
    return { notice: null, next: null };
  }
  if (previous === DaemonStatus.Degraded) return { notice: null, next: previous };
  const words = DAEMON_REASON_WORDS[reason ?? ""] ?? DAEMON_REASON_WORDS[DaemonReason.NotStarted];
  return {
    notice: `Probe: the daemon is ${words}, so it is not recording this session. \`probe doctor\` says why.`,
    next: DaemonStatus.Degraded,
  };
}
