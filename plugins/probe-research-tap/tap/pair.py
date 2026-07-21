"""`python -m tap pair <pairing-token>` — exchange pairing token for a bearer.

POSTs to ${API}/agent-tap/pair with {pairing_token, os, hostname}. On
success the response carries {device_id, device_token, customer_id}; we
write the bearer to ${PLUGIN_DIR}/.token (mode 0600) and persist
device_id/customer_id/paired_at to meta. Any prior last_401_at is cleared.

Re-pair behavior: if a prior pairing exists on this device, its server-side
device entry is revoked AFTER the new pairing succeeds — so a re-pair never
leaves an orphan device row in the user's dashboard. The order matters: we
revoke the old bearer only once we know the new one works, otherwise a bad
pairing token would strand the user with no working pairing at all.
"""

from __future__ import annotations

import json
import platform
import socket
import sys
import time

from tap import config as cfg
from tap import httpclient
from tap.storage import Storage


def _os_label() -> str:
    p = platform.system().lower()
    if p == "darwin":
        return "macos"
    return p


def _revoke_old_pairing(bearer: str) -> None:
    """Best-effort server-side revoke of a prior pairing's bearer.

    Runs after a successful new pair, so:
      - Success → old device is cleanly retired in the dashboard.
      - Halt (401: already revoked / unknown) → benign no-op, no message.
      - Anything else → warn the user that the old device may linger;
        the new pairing is unaffected.
    """
    url = cfg.api_base_url() + cfg.REVOKE_PATH
    resp = httpclient.post_json(url, json.dumps({}).encode("utf-8"), bearer=bearer)
    if resp.classification == httpclient.Classification.SUCCESS:
        print("Revoked previous pairing on this device.")
        return
    if resp.classification == httpclient.Classification.HALT:
        return  # old token already revoked or unknown — nothing to clean up
    msg = resp.error or f"http {resp.status}"
    print(
        f"warning: could not revoke previous pairing ({msg}); the old device "
        "may still appear in your dashboard's Devices list — revoke it manually "
        "if it sticks around.",
        file=sys.stderr,
    )


def run(pairing_token: str) -> int:
    if not pairing_token:
        print("error: pairing token required", file=sys.stderr)
        return 2

    # Capture the old bearer BEFORE the new pair runs. If we picked it up
    # after writing the new .token we'd just be reading our own new token
    # back; capturing now keeps the two cleanly separated.
    old_bearer = cfg.load_token()

    # The backend host comes from the pairing token's `iss` claim (env can
    # still override). No hardcoded fallback — see cfg.base_url_from_pairing_token.
    try:
        base = cfg.pair_base_url(pairing_token)
    except ValueError as exc:
        print(f"pair failed: {exc}", file=sys.stderr)
        return 1

    body = json.dumps({
        "pairing_token": pairing_token,
        "os": _os_label(),
        "hostname": socket.gethostname(),
    }).encode("utf-8")

    url = base + cfg.PAIR_PATH
    resp = httpclient.post_json(url, body)

    if resp.classification == httpclient.Classification.HALT:
        print(
            "pairing token rejected by server (request a fresh one from the dashboard)",
            file=sys.stderr,
        )
        return 1
    if resp.classification != httpclient.Classification.SUCCESS:
        msg = resp.error or f"status {resp.status}"
        print(f"pair failed: {msg}", file=sys.stderr)
        return 1

    data = httpclient.parse_json(resp)
    device_id = data.get("device_id", "")
    device_token = data.get("device_token", "")
    customer_id = data.get("customer_id", "")
    if not device_id or not device_token:
        print("pair response missing device_id or device_token", file=sys.stderr)
        return 1

    # Pin the host this pairing used so the daemon (and the old-pairing revoke
    # below) reach the same backend without re-deriving or guessing.
    cfg.persist_api_base_url(base)

    cfg.write_token(device_token)

    storage = Storage(cfg.state_db_path())
    try:
        storage.set_meta("device_id", device_id)
        storage.set_meta("customer_id", customer_id)
        storage.set_meta("paired_at", str(int(time.time())))
        storage.delete_meta("last_401_at")
        storage.delete_meta("last_401_token_sha256")
    finally:
        storage.close()

    # New pairing committed. Now retire the old server-side device, if any.
    # Skipped on first-ever pair (no old_bearer) and when the old happens to
    # equal the new (defensive — server-side mints are random so this should
    # never match in practice).
    if old_bearer and old_bearer != device_token:
        _revoke_old_pairing(old_bearer)

    print(f"Paired. device_id={device_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[2:]
    if len(argv) != 1:
        print("usage: python -m tap pair <pairing-token>", file=sys.stderr)
        return 2
    return run(argv[0])


if __name__ == "__main__":
    sys.exit(main())
