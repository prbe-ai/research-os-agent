/**
 * Probe's tracking switched off on this package while capture keeps running.
 *
 * pi carries both halves in one package, so the wizard cannot uninstall
 * tracking without deleting capture. It writes `probeTracking: false` (and
 * narrows `skills` to none) on our settings entry instead
 * (`pi_config._entry_for`), and THIS EXTENSION enforces it: no MCP bridge, no
 * Probe MCP tool another extension registered, no package skill and nothing
 * injected into the model's prompt. Capture, the daemon and the approvals
 * guard are untouched.
 *
 * Read straight from pi's settings files on every session_start, never from
 * the CLI: a missing, old or slow (5 s) `probe session initialize` must not
 * turn tracking back on.
 */

import { mergedOurEntry, type AdapterHandoffOptions } from "./adapterHandoff.js";

/** The settings-entry key the CLI writes (`pi_config.TRACKING_KEY`). */
export const TRACKING_KEY = "probeTracking";

export function trackingOff(opts: AdapterHandoffOptions): boolean {
  return mergedOurEntry(opts)?.[TRACKING_KEY] === false;
}
