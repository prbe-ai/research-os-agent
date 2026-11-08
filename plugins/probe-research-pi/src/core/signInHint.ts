/**
 * How a missing or rejected credential is fixed from the chat (fixes-1107b): the command the agent runs,
 * never the wizard or "a terminal". A copy of `probe.sdk.session_marker.SIGN_IN_HINT`, because this
 * extension cannot import the Python package; `agent/tests/test_sign_in_hint_sync.py` fails when the two differ.
 */
export const SIGN_IN_HINT = "run `npx -y probe-research sign-in --json`";
