"""Tests for `python -m tap pair` / `tap revoke` — the device-pairing client.

Covers:
  - first pair writes .token + meta and the transport reads that token;
  - re-pair auto-revokes the prior server-side device (AFTER the new pair
    succeeds), tolerates a 401/again-gone, warns-but-succeeds on a flaky
    revoke, and does NOT touch the old token when the new pair fails;
  - `tap revoke` wipes local state even when the server call fails (offline).

All network is mocked through tap.httpclient.post_json. The isolated fixture
pins PROBE_BASE_URL (so pair skips iss-derivation) and points the probe CLI
config at a nonexistent file + clears PROBE_INGEST_TOKEN, so each test starts
fully unconfigured except for whatever .token it seeds.
"""

from __future__ import annotations

import json as _json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from tap import httpclient


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="probe-research-tap-pair-test-")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", tmp)
    monkeypatch.setenv("PROBE_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(Path(tmp) / "probe-config.json"))
    monkeypatch.delenv("PROBE_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    yield Path(tmp)


def _success_response(body: bytes) -> httpclient.Response:
    return httpclient.Response(
        status=200, body=body,
        classification=httpclient.Classification.SUCCESS,
    )


def _halt_response() -> httpclient.Response:
    return httpclient.Response(
        status=401, body=b"",
        classification=httpclient.Classification.HALT,
        error="HTTP Error 401",
    )


def _retry_response(status: int = 503, error: str = "upstream down") -> httpclient.Response:
    return httpclient.Response(
        status=status, body=b"upstream",
        classification=httpclient.Classification.RETRY,
        error=error,
    )


def _pair_body(device_id: str = "new-dev-1", token: str = "new-token") -> bytes:
    return _json.dumps({
        "device_id": device_id,
        "device_token": token,
        "customer_id": "cust-1",
    }).encode("utf-8")


# ---------------------------------------------------------------------------


def test_first_pair_does_not_revoke(_isolated_plugin_dir: Path) -> None:
    """No .token on disk yet — pair runs once, doesn't try to revoke
    something that doesn't exist."""
    from tap.pair import run

    calls: list[dict] = []

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        calls.append({"url": url, "bearer": bearer})
        assert url.endswith("/agent-tap/pair")
        return _success_response(_pair_body())

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("fresh-pairing-token")

    assert rc == 0
    assert len(calls) == 1, "first pair must NOT call revoke"
    assert calls[0]["url"] == "https://api.invalid/agent-tap/pair"


def test_pair_writes_token_and_meta_and_transport_reads_it(_isolated_plugin_dir: Path) -> None:
    """A successful pair persists the device token to .token, records
    device_id/customer_id/paired_at in meta, and the outbox transport then
    ships batches with that exact token as the bearer."""
    from tap import config as cfg
    from tap import outbox
    from tap.pair import run
    from tap.storage import Storage

    def fake_pair_post(url, body, *, bearer=None, timeout=30.0):
        return _success_response(_pair_body(device_id="dev-xyz", token="ros_ing_DEVICE"))

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_pair_post):
        assert run("fresh-pairing-token") == 0

    # .token written and resolved as the daemon's bearer source.
    assert cfg.token_file().is_file()
    assert cfg.load_token() == "ros_ing_DEVICE"

    # meta persisted.
    storage = Storage(cfg.state_db_path())
    try:
        assert storage.get_meta("device_id") == "dev-xyz"
        assert storage.get_meta("customer_id") == "cust-1"
        assert storage.get_meta("paired_at")  # a unix timestamp string
        # Enqueue a batch, then drain — the transport must send the paired token.
        outbox.enqueue(
            storage=storage, session_id="s1", batch_seq=0, cwd="/tmp",
            body=b'{"events":[]}', now=0,
        )
        sent: dict = {}

        def capture_post(url, body, *, bearer=None, timeout=30.0):
            sent["bearer"] = bearer
            return _success_response(b'{"ok":true}')

        with mock.patch("tap.outbox.httpclient.post_json", side_effect=capture_post):
            outbox.drain_once(
                storage=storage, token=cfg.load_token(),
                base_url=cfg.api_base_url(), session_id="s1",
            )
        assert sent["bearer"] == "ros_ing_DEVICE"
    finally:
        storage.close()


def test_repair_revokes_old_token_after_new_pair(_isolated_plugin_dir: Path, capsys) -> None:
    """A second pair on the same laptop captures the old bearer, mints
    the new one, and POSTs revoke with the OLD bearer. The old device is
    cleanly retired."""
    from tap import config as cfg
    from tap.pair import run

    cfg.write_token("old-token")

    pair_calls: list[dict] = []
    revoke_calls: list[dict] = []

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        if url.endswith("/agent-tap/pair"):
            pair_calls.append({"bearer": bearer})
            return _success_response(_pair_body(token="brand-new"))
        if url.endswith("/agent-tap/revoke"):
            revoke_calls.append({"bearer": bearer})
            return _success_response(b'{"ok":true}')
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("fresh-pairing-token")

    assert rc == 0
    assert len(pair_calls) == 1
    assert pair_calls[0]["bearer"] is None  # pair endpoint is JWT-in-body, no bearer
    assert len(revoke_calls) == 1, "re-pair must revoke the old server-side device"
    assert revoke_calls[0]["bearer"] == "old-token", "must revoke with the OLD bearer"

    out = capsys.readouterr().out
    assert "Revoked previous pairing on this device." in out
    assert "Paired." in out
    assert cfg.load_token() == "brand-new"


def test_repair_silently_ignores_revoke_401(_isolated_plugin_dir: Path, capsys) -> None:
    """Old token might already be revoked (e.g. user revoked from the
    dashboard first). The 401 is a benign no-op — no scary warning."""
    from tap import config as cfg
    from tap.pair import run

    cfg.write_token("old-already-revoked-token")

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        if url.endswith("/agent-tap/pair"):
            return _success_response(_pair_body())
        if url.endswith("/agent-tap/revoke"):
            return _halt_response()
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("fresh-pairing-token")

    assert rc == 0
    out = capsys.readouterr()
    assert "Revoked previous pairing" not in out.out
    assert "warning" not in out.err.lower()
    assert "Paired." in out.out


def test_repair_warns_but_succeeds_when_revoke_fails(_isolated_plugin_dir: Path, capsys) -> None:
    """Revoke 5xx / network blip leaves the old device orphaned in the
    dashboard. We warn but the new pair still commits — never stranded."""
    from tap import config as cfg
    from tap.pair import run

    cfg.write_token("old-token")

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        if url.endswith("/agent-tap/pair"):
            return _success_response(_pair_body(token="brand-new"))
        if url.endswith("/agent-tap/revoke"):
            return _retry_response()
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("fresh-pairing-token")

    assert rc == 0
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "could not revoke previous pairing" in err
    assert cfg.load_token() == "brand-new"


def test_failed_new_pair_does_not_touch_old_token(_isolated_plugin_dir: Path) -> None:
    """If the new pair fails, we must NOT revoke the old one — otherwise a
    bad re-pair attempt strands the user with no working pairing."""
    from tap import config as cfg
    from tap.pair import run

    cfg.write_token("old-token")

    revoke_calls: list[dict] = []

    def fake_post(url: str, body: bytes, *, bearer: str | None = None, timeout: float = 30.0):
        if url.endswith("/agent-tap/pair"):
            return _halt_response()  # pairing token rejected
        if url.endswith("/agent-tap/revoke"):
            revoke_calls.append({"bearer": bearer})
            return _success_response(b'{"ok":true}')
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch("tap.pair.httpclient.post_json", side_effect=fake_post):
        rc = run("rejected-pairing-token")

    assert rc == 1, "bad pair must surface as failure"
    assert revoke_calls == [], "must NOT revoke the old token on failed re-pair"
    assert cfg.load_token() == "old-token"


# --- revoke -----------------------------------------------------------------


def _seed_paired_state() -> None:
    from tap import config as cfg
    from tap.storage import Storage

    cfg.write_token("device-token")
    storage = Storage(cfg.state_db_path())
    try:
        storage.set_meta("device_id", "dev-1")
        storage.set_meta("customer_id", "cust-1")
        storage.set_meta("paired_at", "123")
        storage.enqueue_batch(
            session_id="s1", batch_seq=0, cwd="/tmp",
            body=b"{}", created_at=0, next_attempt_at=0,
        )
    finally:
        storage.close()


def test_revoke_wipes_local_even_when_server_fails(_isolated_plugin_dir: Path, capsys) -> None:
    """A 5xx / network blip on the server-side revoke must not block the local
    wipe — uninstall has to succeed offline. Token file, meta, and the queued
    outbox are all cleared, and revoke still returns 0."""
    from tap import config as cfg
    from tap.revoke import run
    from tap.storage import Storage

    _seed_paired_state()

    def fake_post(url, body, *, bearer=None, timeout=30.0):
        assert url.endswith("/agent-tap/revoke")
        assert bearer == "device-token"
        return _retry_response()

    with mock.patch("tap.revoke.httpclient.post_json", side_effect=fake_post):
        rc = run()

    assert rc == 0
    assert not cfg.token_file().exists(), "local token must be wiped even on server failure"
    err = capsys.readouterr().err
    assert "server-side revoke failed" in err

    storage = Storage(cfg.state_db_path())
    try:
        assert storage.get_meta("device_id") == ""
        assert storage.get_meta("customer_id") == ""
        assert storage.get_meta("paired_at") == ""
        assert storage.outbox_row_count() == 0
    finally:
        storage.close()


def test_revoke_wipes_local_when_base_url_unset(_isolated_plugin_dir: Path, monkeypatch) -> None:
    """No host on record (env cleared, none pinned) → we can't reach the server,
    but the local wipe still happens and revoke returns 0."""
    from tap import config as cfg
    from tap.revoke import run

    monkeypatch.delenv("PROBE_BASE_URL", raising=False)
    _seed_paired_state()

    def boom(*_a, **_k):
        raise AssertionError("must not POST when no base URL is resolvable")

    with mock.patch("tap.revoke.httpclient.post_json", side_effect=boom):
        rc = run()

    assert rc == 0
    assert not cfg.token_file().exists()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
