/**
 * The daemon profile, for pi: what changes when the CLI says the Probe daemon
 * records and reads for this session (`Recorder.Daemon`, `trackingState.ts`).
 *
 * On Claude Code and Codex the daemon profile is a second, lean plugin
 * (`plugins/probe-research-daemon`): `instrument-code`, `plan-research-review`
 * and its own `probe` and `visualize-progress` skills, no MCP server, no daemon
 * notices. pi has one package for both profiles (decision D1), so this
 * extension narrows ITSELF:
 *
 *   - skills: `resources_discover` adds `DAEMON_OWN_SKILLS` from
 *     `daemon-skills/` (byte copies of the lean plugin's) and
 *     `DAEMON_ADDED_SKILLS` (this package's own copies, which the settings
 *     filter written before them does not load),
 *     and every prompt lists only `DAEMON_PROFILE_SKILLS`
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

/** The skills a daemon-profile prompt keeps of this package's: the lean plugin's four. */
export const DAEMON_PROFILE_SKILLS: ReadonlySet<string> = new Set([
  "instrument-code",
  "probe",
  "plan-research-review",
  "visualize-progress",
]);

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
  "plan-research-review",
  "write-summary",
  "overview-visuals",
  // The page skills a full-page summary is written by (writer-owns-summary
  // ARTIFACT-PAGES P2): the agent profile's pi writes summaries too.
  "overview-page",
  "overview-contract",
  "overview-charts",
  "artifact-diagramming",
  "artifact-design",
]);

/** Where the daemon's own skills live inside this package. Never named in package.json. */
export const DAEMON_SKILLS_DIR = "daemon-skills";

/**
 * The lean plugin's hand-kept skills (the Makefile's `DAEMON_HAND_KEPT`): the
 * daemon's own wording of a skill this package also ships under `skills/` for
 * the agent profile -- `probe` the switch, `visualize-progress` asking the
 * daemon instead of reading Probe.
 */
export const DAEMON_OWN_SKILLS: readonly string[] = ["probe", "visualize-progress"];

export function daemonSkillDir(extensionDir: string, name: string): string {
  return join(extensionDir, DAEMON_SKILLS_DIR, name);
}

/**
 * Daemon-profile skills this extension adds from the package's own `skills/`,
 * instead of the settings entry's filter (`pi_config.DAEMON_PACKAGE_SKILLS`).
 * A filter is written once, by the wizard, so a skill added to the daemon
 * profile later would never reach a machine already on it; and widening the
 * filter would make every older filter read as the agent profile
 * (`pi_config._is_daemon_filter` matches it exactly). pi keeps the first skill
 * of a name, so where a filter or a project entry already loaded one, this
 * path is a no-op.
 */
export const DAEMON_ADDED_SKILLS: readonly string[] = ["plan-research-review"];

/** Every skill path `resources_discover` adds in the daemon profile. */
export function daemonSkillPaths(extensionDir: string): string[] {
  return [
    ...DAEMON_OWN_SKILLS.map((name) => daemonSkillDir(extensionDir, name)),
    ...DAEMON_ADDED_SKILLS.map((name) => join(extensionDir, "skills", name)),
  ];
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

/** One of `DAEMON_OWN_SKILLS`: its file, and the description its frontmatter declares; null when unreadable. */
export function readDaemonSkill(
  extensionDir: string,
  name: string,
  read: (path: string) => string = (path) => readFileSync(path, "utf-8"),
): DaemonSkill | null {
  const baseDir = daemonSkillDir(extensionDir, name);
  const filePath = join(baseDir, "SKILL.md");
  try {
    const frontmatter = /^---\n([\s\S]*?)\n---/.exec(read(filePath));
    const description = frontmatter ? /^description:\s*(.+)$/m.exec(frontmatter[1])?.[1]?.trim() : undefined;
    return description ? { description, filePath, baseDir } : null;
  } catch {
    return null;
  }
}

/** Every readable one of `DAEMON_OWN_SKILLS`, by name. */
export function readDaemonSkills(
  extensionDir: string,
  read?: (path: string) => string,
): ReadonlyMap<string, DaemonSkill> {
  const skills = new Map<string, DaemonSkill>();
  for (const name of DAEMON_OWN_SKILLS) {
    const skill = readDaemonSkill(extensionDir, name, read);
    if (skill) skills.set(name, skill);
  }
  return skills;
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
 * each of `DAEMON_OWN_SKILLS` the daemon's, and every other skill (the
 * researcher's own `~/.pi/agent/skills`, a project's, another package's) kept
 * as it was.
 *
 * pi keeps the FIRST skill of a name (`skills.js`'s `loadSkills`) and loads an
 * extension's `resources_discover` paths after every package's, so wherever
 * this package's full `skills/probe` is loaded (a project entry with every
 * skill), it wins over `daemon-skills/probe` -- and that one tells the agent it
 * records (`skills/visualize-progress` names MCP reads it does not have). Such
 * an entry is pointed at the daemon's file instead, so the skill the model is
 * told to read is the daemon's; with no readable daemon file it is dropped,
 * never listed as the agent profile's. New objects: pi's own list is not
 * edited in place.
 */
export function leanSkills<T extends PromptSkill>(
  skills: readonly T[],
  daemonSkills: ReadonlyMap<string, DaemonSkill>,
): T[] {
  return skills
    .filter((skill) => !PACKAGE_SKILLS.has(skill.name) || DAEMON_PROFILE_SKILLS.has(skill.name))
    .filter((skill) => !DAEMON_OWN_SKILLS.includes(skill.name) || daemonSkills.has(skill.name))
    .map((skill) => {
      const own = daemonSkills.get(skill.name);
      return own && !samePath(skill.filePath, own.filePath) ? { ...skill, ...own } : skill;
    });
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

/**
 * Tracking switched off (`trackingSetting.ts`): every skill this package
 * ships leaves the prompt, the daemon profile's own `probe` skill included.
 * A skill the researcher or another package brought stays.
 */
export function withoutPackageSkills<T extends PromptSkill>(skills: readonly T[]): T[] {
  return skills.filter((skill) => !PACKAGE_SKILLS.has(skill.name));
}
