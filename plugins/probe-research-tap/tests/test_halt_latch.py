"""The 401-halt latch must never wedge longer than it can be justified.

On daemon start, `main()` decides whether a prior 401 halt still holds. It clears
(and resumes) when: (a) the configured token differs from the rejected one, (b) the
halt is older than HALT_RETRY_AFTER_SECONDS (a re-probe that self-heals a transient
401 with the SAME still-valid token), or (c) no rejected-token fingerprint was
recorded (a split state we cannot justify holding). It HOLDS only when a recent halt
still names the currently-configured token.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

CURRENT_TOKEN = "the-current-ingest-token"


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="probe-research-tap-halt-test-")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", tmp)
    # A configured, logged-in install: token + base URL both present so main()
    # reaches the latch check (not the no-token / no-base-url early exits).
    monkeypatch.setenv("PROBE_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("PROBE_INGEST_TOKEN", CURRENT_TOKEN)
    yield Path(tmp)


def _watch_args(tmp_path: Path, sid: str) -> list[str]:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n")
    return [
        "--session-id", sid,
        "--transcript", str(transcript),
        "--cwd", str(tmp_path),
    ]


def _seed_meta(**meta: str) -> None:
    from tap import config as cfg
    from tap.storage import Storage

    storage = Storage(cfg.state_db_path())
    try:
        for key, value in meta.items():
            storage.set_meta(key, value)
    finally:
        storage.close()


def _read_meta(key: str) -> str:
    from tap import config as cfg
    from tap.storage import Storage

    storage = Storage(cfg.state_db_path())
    try:
        return storage.get_meta(key)
    finally:
        storage.close()


def _run_main(tmp_path: Path, sid: str) -> tuple[int, mock.Mock]:
    """Call main() with _run_loop stubbed out; return (rc, run_loop_mock)."""
    from tap import main as tapmain

    with mock.patch.object(tapmain, "_run_loop", return_value=0) as run_loop:
        rc = tapmain.main(_watch_args(tmp_path, sid))
    return rc, run_loop


def test_halt_holds_when_recent_401_names_current_token(tmp_path: Path) -> None:
    from tap.outbox import token_fingerprint

    _seed_meta(
        last_401_at=str(int(time.time())),
        last_401_token_sha256=token_fingerprint(CURRENT_TOKEN),
    )
    rc, run_loop = _run_main(tmp_path, "halt-hold")
    assert rc == 1, "a fresh 401 on the still-configured token must keep halting"
    run_loop.assert_not_called()
    assert _read_meta("last_401_at") != "", "latch must be preserved while it holds"


def test_halt_clears_when_token_changed(tmp_path: Path) -> None:
    from tap.outbox import token_fingerprint

    _seed_meta(
        last_401_at=str(int(time.time())),
        last_401_token_sha256=token_fingerprint("an-old-dead-token"),
    )
    rc, run_loop = _run_main(tmp_path, "halt-tokenchange")
    assert rc == 0
    run_loop.assert_called_once()
    assert _read_meta("last_401_at") == ""
    assert _read_meta("last_401_token_sha256") == ""


def test_halt_clears_after_cooldown_expiry_same_token(tmp_path: Path) -> None:
    """Transient 401 (member removed then re-added, SAME still-valid token): no
    fingerprint change to self-clear on, so the cooldown is the only path back."""
    from tap import main as tapmain
    from tap.outbox import token_fingerprint

    stale = int(time.time()) - (tapmain.HALT_RETRY_AFTER_SECONDS + 60)
    _seed_meta(
        last_401_at=str(stale),
        last_401_token_sha256=token_fingerprint(CURRENT_TOKEN),
    )
    rc, run_loop = _run_main(tmp_path, "halt-cooldown")
    assert rc == 0, "a halt older than the cooldown must clear and re-probe"
    run_loop.assert_called_once()
    assert _read_meta("last_401_at") == ""


def test_halt_still_holds_just_inside_cooldown(tmp_path: Path) -> None:
    """A halt younger than the cooldown, same token, still holds — the cooldown
    clears only once it has actually elapsed."""
    from tap import main as tapmain
    from tap.outbox import token_fingerprint

    recent = int(time.time()) - (tapmain.HALT_RETRY_AFTER_SECONDS - 60)
    _seed_meta(
        last_401_at=str(recent),
        last_401_token_sha256=token_fingerprint(CURRENT_TOKEN),
    )
    rc, run_loop = _run_main(tmp_path, "halt-inside-cooldown")
    assert rc == 1
    run_loop.assert_not_called()


def test_halt_clears_when_fingerprint_missing(tmp_path: Path) -> None:
    """Crash between writing last_401_at and the fingerprint leaves an empty fp;
    we do not hold a halt we cannot justify."""
    _seed_meta(last_401_at=str(int(time.time())))  # no fingerprint recorded
    rc, run_loop = _run_main(tmp_path, "halt-nofp")
    assert rc == 0, "an unjustifiable (fingerprint-less) latch must clear"
    run_loop.assert_called_once()
    assert _read_meta("last_401_at") == ""


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
