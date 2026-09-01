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
 * All real logic lives in ./src — this file only computes `__dirname`
 * (needed for the sibling-checkout tap-root default; see src/tapRuntime.ts)
 * and calls into the wiring layer.
 */

import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { registerExtension } from "./src/extension.js";

const extensionDir = dirname(fileURLToPath(import.meta.url));

export default function probeResearchPi(pi: ExtensionAPI): void {
  registerExtension(pi, extensionDir);
}
