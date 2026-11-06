"""route a capture source to its harness's sanitizer, title reader and pasted-image reader (the TranscriptSanitizer registry)."""

from __future__ import annotations
import json
from pathlib import Path

from ..sanitizers import codex as codex_sanitize, kimi_code as kimi_sanitize, pi as pi_sanitize, claude_code as sanitize
from ..sanitizers.claude_code import CallModels, call_models
from ..pinned import open_transcript
#: The sanitizer modules a registry row can name (`capture.sanitizer`). A new
#: harness adds its module here and in the Makefile's sync-tap-core list.
_SANITIZER_MODULES = {
    "sanitize": sanitize,
    "codex_sanitize": codex_sanitize,
    "pi_sanitize": pi_sanitize,
    "kimi_sanitize": kimi_sanitize,
}


def _harness(source: str):
    """The registry row for a capture source. KeyError for an unknown source:
    an upload never guesses a route or a sanitizer."""
    from .. import harness_registry  # the copy beside this module (tap_core and the tap both carry one)

    return harness_registry.get_registry().get(source)


def _sanitizer(source: str):
    return _SANITIZER_MODULES[_harness(source).capture.sanitizer].sanitize_event


def _title_reader(source: str):
    """The harness's reader of the session's own name (`session_title`), or
    None for a harness whose transcript carries none."""
    return getattr(_SANITIZER_MODULES[_harness(source).capture.sanitizer], "session_title", None)


def _pasted_extractor(source: str):
    """The harness's reader of researcher-pasted images (`pasted_images`), or
    None for a harness that has none (its images keep their placeholders)."""
    return getattr(_SANITIZER_MODULES[_harness(source).capture.sanitizer], "pasted_images", None)


def _get_inherited_end_reader(source: str):
    """The harness's reader of where a session's own records start, past history copied in from
    another chat (`get_inherited_end`: a Codex sub-agent's), or None for a harness that copies none."""
    return getattr(_SANITIZER_MODULES[_harness(source).capture.sanitizer], "get_inherited_end", None)


def _call_model_markers(source: str) -> tuple[bytes, ...]:
    """The lines that change a file's call models (`sanitize.CallModels`), for
    a harness whose usage lines do not name their model; () for any other."""
    return getattr(_SANITIZER_MODULES[_harness(source).capture.sanitizer], "CALL_MODEL_MARKERS", ())


#: Where a session's call models are saved in its state, with the cursor they
#: belong to: `{"at": source byte, "models": {key: model}}`.
CALL_MODELS_STATE = "call_models"
#: How far back the journal reads to rebuild call models it has not saved at
#: this cursor (a session from an older tap, a cursor the server moved).
CALL_MODELS_LOOKBACK_BYTES = 16 * 1024 * 1024


def _resume_call_models(state: dict, path: Path, sanitize_line, markers: tuple[bytes, ...],
                        at: int) -> CallModels:
    """The call models of `path` up to byte `at`: the saved ones when they
    belong to that cursor, else rebuilt from the lines before it (at most
    CALL_MODELS_LOOKBACK_BYTES; only lines naming a marker are parsed)."""
    saved = state.get(CALL_MODELS_STATE)
    if isinstance(saved, dict) and saved.get("at") == at:
        return CallModels(saved.get("models"))
    models = CallModels()
    if at <= 0:
        return models
    start = max(0, at - CALL_MODELS_LOOKBACK_BYTES)
    try:
        with open_transcript(path) as handle:
            handle.seek(start)
            window = handle.read(at - start)
    except OSError:
        return models
    lines = window.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first may be the tail of a line before the window
    with call_models(models):
        for raw in lines:
            if not any(marker in raw for marker in markers):
                continue
            try:
                sanitize_line(json.loads(raw))
            except Exception:  # noqa: BLE001 -- a line it cannot read costs that line's model
                continue
    return models
