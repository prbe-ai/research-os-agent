# tests/test_outbox_sanitizer_broken_import.py
"""sanitizer_for_current_source must only relabel a ModuleNotFoundError when
the MISSING module IS the sanitizer module itself (source not shipped yet).

A sanitizer module that exists but fails on one of its OWN imports also
raises ModuleNotFoundError — same exception type, completely different
meaning. Catching that broadly and rewriting it as "has not shipped yet"
throws away the real traceback (which names the actual missing dependency)
and replaces it with a claim that is false: the module IS there, something
it imports is not.
"""
import sys

import pytest

from tap import config as cfg
from tap import outbox, sources


def _fake_source(sanitizer_module: str) -> sources.Source:
    return sources.Source(
        source_id="broken",
        display_name="Broken",
        webhook_path="/ingest/v1/sessions/broken",
        sanitizer_module=sanitizer_module,
        token_env="PROBE_BROKEN_TAP_TOKEN",
        plugin_dir_env="PROBE_BROKEN_TAP_PLUGIN_DIR",
        default_session_root=".broken/sessions",
        session_id_strategy=sources.SESSION_ID_STEM,
    )


def test_a_missing_transitive_dependency_propagates_unrelabelled(tmp_path, monkeypatch):
    pkg = tmp_path / "broken_sanitizer_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "import this_dependency_does_not_exist_anywhere_xyz\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("broken_sanitizer_pkg.mod", None)
    sys.modules.pop("broken_sanitizer_pkg", None)

    fake_source = _fake_source("broken_sanitizer_pkg.mod")
    monkeypatch.setattr(cfg, "current_source", lambda: fake_source)

    with pytest.raises(ModuleNotFoundError) as exc_info:
        outbox.sanitizer_for_current_source()

    # The real culprit, not the sanitizer module — and NOT wrapped as
    # SanitizerNotAvailable, which would misreport "has not shipped yet".
    assert exc_info.value.name == "this_dependency_does_not_exist_anywhere_xyz"
    assert not isinstance(exc_info.value, outbox.SanitizerNotAvailable)


def test_the_sanitizer_module_itself_missing_is_still_relabelled(monkeypatch):
    fake_source = _fake_source("tap.this_sanitizer_module_was_never_written")
    monkeypatch.setattr(cfg, "current_source", lambda: fake_source)

    with pytest.raises(outbox.SanitizerNotAvailable) as exc_info:
        outbox.sanitizer_for_current_source()

    assert isinstance(exc_info.value.__cause__, ModuleNotFoundError)
    assert exc_info.value.__cause__.name == (
        "tap.this_sanitizer_module_was_never_written"
    )
