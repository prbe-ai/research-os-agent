# tests/test_outbox_dispatch.py
from tap import outbox


def test_dispatch_selects_the_pi_sanitizer(monkeypatch):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    fn = outbox.sanitizer_for_current_source()
    assert fn.__module__ == "tap.pi_sanitize"


def test_dispatch_selects_the_codex_sanitizer(monkeypatch):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "codex")
    assert outbox.sanitizer_for_current_source().__module__ == "tap.codex_sanitize"


def test_dispatch_is_resolved_once_per_batch_not_per_line(monkeypatch):
    # The shipped code re-ran the import inside the per-line loop.
    calls = []
    monkeypatch.setattr(outbox, "sanitizer_for_current_source",
                        lambda: (calls.append(1), lambda e: e)[1])
    outbox.build_batch_body(
        lines=[b'{"type":"session"}', b'{"type":"message"}'],
        base_line_no=0, device_id="d", session_id="s", batch_seq=1, cwd="/tmp",
    )
    assert len(calls) == 1
