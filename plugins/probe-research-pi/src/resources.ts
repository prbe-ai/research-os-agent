/**
 * The fingerprint of what pi reads from this package only when it loads it
 * (at start, at `/reload`, and for each new session runtime): the skills the
 * manifest lists, the daemon profile's skill paths (`resources_discover`),
 * each skill's frontmatter (the name and description pi puts in the prompt)
 * and the MCP manifest.
 *
 * A newer version whose fingerprint differs still takes over a running pi at
 * the next turn (`src/shim.ts`), but pi keeps the skills it loaded until
 * `/reload`, so the shim marks the live record `skills_reload_needed`. A
 * skill's BODY is not in it: the model reads that file when it uses the skill,
 * and the daemon profile's prompt names this version's own copy every turn
 * (`profile.ts::leanSkills`).
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join, relative } from "node:path";

import { daemonSkillPaths } from "./profile.js";

function frontmatter(path: string, read: (path: string) => string): string | null {
  try {
    return /^---\r?\n([\s\S]*?)\r?\n---/.exec(read(path))?.[1] ?? "";
  } catch {
    return null;
  }
}

/** A short hex digest of `packageDir`'s skill and resource set. Never throws. */
export function resourcesFingerprint(
  packageDir: string,
  read: (path: string) => string = (path) => readFileSync(path, "utf-8"),
): string {
  let manifest: Record<string, unknown> = {};
  try {
    const pi = (JSON.parse(read(join(packageDir, "package.json"))) as { pi?: unknown }).pi;
    if (pi && typeof pi === "object") manifest = pi as Record<string, unknown>;
  } catch {
    manifest = {};
  }
  const listed = Array.isArray(manifest.skills) ? manifest.skills.filter((entry): entry is string => typeof entry === "string") : [];
  const skills = [
    ...listed.map((entry) => ["package", entry, frontmatter(join(packageDir, entry, "SKILL.md"), read)]),
    ...daemonSkillPaths(packageDir).map((path) => ["daemon", relative(packageDir, path), frontmatter(join(path, "SKILL.md"), read)]),
  ];
  let mcp: string | null = null;
  if (typeof manifest.mcp === "string") {
    try {
      mcp = read(join(packageDir, manifest.mcp));
    } catch {
      mcp = null;
    }
  }
  const material = JSON.stringify({ extensions: manifest.extensions ?? null, skills, mcpManifest: manifest.mcp ?? null, mcp });
  return createHash("sha256").update(material).digest("hex").slice(0, 16);
}
