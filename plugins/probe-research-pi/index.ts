/**
 * probe-research-pi — pi extension entry point.
 *
 * Discovered by pi's extension loader either as a direct file under
 * `~/.pi/agent/extensions/` (rule 2: "Subdirectory with index") or via this
 * package's `package.json` `"pi": {"extensions": ["index.ts"]}` manifest
 * (rule 1, used by `pi install npm:...` / `pi install git:...`). See
 * README.md for why USER scope (`~/.pi/agent/extensions/`) is the only
 * install location that actually loads in non-interactive modes.
 *
 * FROZEN, with `src/shim.ts`: pi loads this once per process (again only at
 * `/reload`), and the shim it starts runs the implementation of whichever
 * Probe version is pinned newest, switching at a turn boundary with no
 * `/reload`. This file only computes where pi loaded the package from (the
 * package's identity, and the implementation beside it, used when no pin can
 * be) and hands both to the shim.
 */

import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import * as cloneImpl from "./src/impl.js";
import { attachShim } from "./src/shim.js";

const installRoot = dirname(fileURLToPath(import.meta.url));

export default async function probeResearchPi(pi: ExtensionAPI): Promise<void> {
  await attachShim(pi, installRoot, cloneImpl);
}
