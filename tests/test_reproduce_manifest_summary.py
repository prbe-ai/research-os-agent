"""`reproduce` must stay small enough to actually return.

It is an atomic view — never truncated — so anything unbounded inside it makes the
whole call fail rather than degrade. The code manifest holds one row per captured
file; on a 224-file repo those rows were 94% of the payload and the call errored on
token budget. The summary is what answers "is this reproducible"; the rows are not.
"""

from __future__ import annotations

import json

from probe.mcp.service import _summarize_manifest


def _record(n_entries: int) -> dict:
    return {
        "content_hash": "abc",
        "code": {
            "git": {"commit": "c" * 40, "dirty": False},
            "manifest": {
                "entries": [
                    {"path": f"src/f{i}.py", "mode": "100644", "sha256": "s" * 64,
                     "size": 100, "source": "git", "blob": "b" * 40}
                    for i in range(n_entries)
                ],
                "tree_sha256": "t" * 64,
                "base_commit": "d" * 40,
                "remote": "https://github.com/acme/repo.git",
                "n_git_referenced": n_entries,
                "n_pending_upload": 0,
            },
        },
        "deps": {"python": "3.12.13"},
    }


def test_rows_are_dropped_but_counted():
    out = _summarize_manifest(_record(224))
    man = out["code"]["manifest"]
    assert "entries" not in man
    assert man["entries_omitted"] == 224


def test_the_fields_that_decide_reproducibility_survive():
    man = _summarize_manifest(_record(3))["code"]["manifest"]
    for key in ("tree_sha256", "base_commit", "remote", "n_git_referenced", "n_pending_upload"):
        assert key in man, f"{key} is what the caller judges reproducibility on"


def test_payload_stops_scaling_with_file_count():
    small = len(json.dumps(_summarize_manifest(_record(10))))
    large = len(json.dumps(_summarize_manifest(_record(5000))))
    # entries_omitted grows by a few digits; nothing else may.
    assert large - small < 20, "summary must not grow with the repo"


def test_git_and_deps_are_left_alone():
    out = _summarize_manifest(_record(2))
    assert out["code"]["git"]["commit"] == "c" * 40
    assert out["deps"]["python"] == "3.12.13"


def test_records_without_a_manifest_pass_through_unchanged():
    """Runs captured before this feature, and the no-snapshot case."""
    old = {"code": {"git": {"commit": "x" * 40}}, "deps": {}}
    assert _summarize_manifest(old) is old
    assert _summarize_manifest(None) is None
