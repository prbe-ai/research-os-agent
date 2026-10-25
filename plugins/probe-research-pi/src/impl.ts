/**
 * The implementation entry: the module the shim (`src/shim.ts`) loads, from
 * the pi clone at start and from a pinned copy of a newer version at a turn
 * boundary. Its exports are the contract `ImplModule` names; keep them, and
 * keep this path (`IMPL_ENTRY`), in every version.
 *
 *   REQUIRES_SHIM_API      the shim API this version needs (`SHIM_API`)
 *   STATE_SCHEMA           the shared-state layout it reads and writes
 *   MIGRATES_FROM          older layouts `prepare` migrates
 *   RESOURCES_FINGERPRINT  its skill/resource set (`resources.ts`)
 *   prepare(env)           the candidate, with no side effect
 */

import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { MIGRATES_FROM, prepareExtension, STATE_SCHEMA } from "./extension.js";
import { resourcesFingerprint } from "./resources.js";
import type { Candidate, ImplEnv } from "./shim.js";

export { MIGRATES_FROM, STATE_SCHEMA };

/** The shim API this implementation is written against. */
export const REQUIRES_SHIM_API = 1;

/** This package's folder: `src/`'s parent, wherever this copy is. */
export const PACKAGE_DIR = dirname(dirname(fileURLToPath(import.meta.url)));

export const RESOURCES_FINGERPRINT = resourcesFingerprint(PACKAGE_DIR);

export function prepare(env: ImplEnv): Candidate {
  return prepareExtension(env);
}
