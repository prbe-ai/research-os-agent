/**
 * Where every message sends a person to set up, sign in to, or repair this device.
 *
 * Setup is wizard-only (Richard 2026-09-29): no user-facing text names a
 * `probe <setup command>`. This is a copy of `probe.sdk.session_marker.WIZARD_HINT`,
 * because this extension cannot import the Python package;
 * `agent/tests/test_wizard_hint_sync.py` fails when the two differ.
 */
export const WIZARD_HINT = "run the Probe wizard: probe wizard or npx probe-research";
