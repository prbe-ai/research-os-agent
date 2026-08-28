# tests/test_sources.py
import pytest

from tap import sources


def test_known_sources_are_the_three_shipped_harnesses():
    assert set(sources.SOURCES) == {"claude_code", "codex", "pi"}


def test_lookup_returns_the_row_for_a_known_source():
    row = sources.get("pi")
    assert row.source_id == "pi"
    assert row.webhook_path == "/ingest/v1/sessions/pi"
    assert row.sanitizer_module == "tap.pi_sanitize"


def test_claude_code_keeps_its_shipped_webhook_path():
    # The hyphen here is load-bearing: the route has always been
    # /sessions/claude-code while the source id is claude_code.
    assert sources.get("claude_code").webhook_path == "/ingest/v1/sessions/claude-code"


def test_unknown_source_raises_rather_than_defaulting():
    # Defaulting is how a misconfigured PROBE_TAP_SOURCE silently shipped
    # pi transcripts to the claude-code route.
    with pytest.raises(KeyError):
        sources.get("cursor")
