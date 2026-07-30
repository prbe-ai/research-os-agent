"""Tags client surface (0066): the canonical-form mirror, the CLI tag-verb
merge logic, and the two version-skew guards (filter + write verification)."""

from __future__ import annotations

import pytest
import typer

from probe.cli.main import _apply_tag_ops, _tag_verb_flow
from probe.sdk import errors
from probe.sdk.client import Client
from probe.sdk.tags import canonical_tags


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["  GPU  Training "], ["gpu-training"]),
        (["a", "A", "a "], ["a"]),
        (["", "   "], []),
        (["Straße"], ["strasse"]),  # casefold expands beyond lowercase
        (["B", "a"], ["b", "a"]),  # order preserved, first occurrence wins
        (["a\x00b", "c\td"], ["a-b", "c-d"]),  # control chars separate
        (["pro​d", "﻿x"], ["prod", "x"]),  # format chars vanish
        (["a b"], ["a-b"]),  # unicode whitespace splits
    ],
)
def test_canonical_tags_mirrors_server_contract(raw, expected) -> None:
    # These vectors pin the CONTRACT.md canonical form; the server's
    # app/core/tags.py _canonical must agree on every one of them.
    assert canonical_tags(raw) == expected


class TestApplyTagOps:
    def test_adds_append_canonical_and_removes_drop(self) -> None:
        assert _apply_tag_ops(["a", "b"], ["C"], ["a"], None) == ["b", "c"]

    def test_add_of_existing_tag_dedupes(self) -> None:
        assert _apply_tag_ops(["a"], ["A "], [], None) == ["a"]

    def test_remove_of_absent_tag_is_noop(self) -> None:
        assert _apply_tag_ops(["a"], [], ["zzz"], None) == ["a"]

    def test_set_wins_outright(self) -> None:
        assert _apply_tag_ops(["a", "b"], [], [], ["New Tag"]) == ["new-tag"]

    def test_set_empty_string_clears(self) -> None:
        assert _apply_tag_ops(["a"], [], [], [""]) == []

    def test_set_combined_with_add_is_rejected(self) -> None:
        with pytest.raises(typer.BadParameter):
            _apply_tag_ops(["a"], ["b"], [], ["c"])

    def test_set_combined_with_remove_is_rejected(self) -> None:
        with pytest.raises(typer.BadParameter):
            _apply_tag_ops(["a"], [], ["a"], ["c"])

    def test_same_tag_added_and_removed_rejected_after_canonicalization(self) -> None:
        with pytest.raises(typer.BadParameter):
            _apply_tag_ops([], ["GPU Training"], ["gpu-training"], None)


class TestTagVerbFlow:
    def test_bare_invocation_lists_without_writing(self) -> None:
        wrote = []
        out = _tag_verb_flow("x", ["a"], None, None, None, wrote.append)
        assert out == {"id": "x", "tags": ["a"]} and wrote == []

    def test_noop_on_already_canonical_row_skips_write(self) -> None:
        wrote = []
        out = _tag_verb_flow("x", ["a"], ["a"], None, None, wrote.append)
        assert out == {"id": "x", "tags": ["a"]} and wrote == []

    def test_legacy_row_is_healed_not_skipped(self) -> None:
        # A pre-0066 row storing "Baseline" compares RAW, so re-tagging with
        # the canonical name still writes once and canonicalizes storage.
        writes = []

        def write(wanted):
            writes.append(wanted)
            return {"id": "x", "tags": wanted}

        out = _tag_verb_flow("x", ["Baseline"], ["baseline"], None, None, write)
        assert writes == [["baseline"]] and out["tags"] == ["baseline"]


class TestVerifyTagsFilter:
    def test_rows_carrying_all_tags_pass(self) -> None:
        Client._verify_tags_filter(["baseline"], [{"tags": ["baseline", "x"]}], "r")

    def test_legacy_uncanonical_row_matches_canonically(self) -> None:
        # Both sides canonicalize: a stored "Baseline" cannot false-positive.
        Client._verify_tags_filter(["baseline"], [{"tags": ["Baseline"]}], "r")

    def test_unfiltered_rows_refuse(self) -> None:
        with pytest.raises(errors.NotFoundError, match="0066"):
            Client._verify_tags_filter(["baseline"], [{"tags": []}], "GET /v1/runs")

    def test_empty_page_passes(self) -> None:
        Client._verify_tags_filter(["baseline"], [], "r")


class TestVerifyTagsWritten:
    def test_echoed_canonical_row_passes(self) -> None:
        Client._verify_tags_written(["Base Line"], {"tags": ["base-line"]}, "r")

    def test_missing_tags_key_refuses(self) -> None:
        # Pre-0066 project rows have no tags key at all.
        with pytest.raises(errors.NotFoundError, match="0066"):
            Client._verify_tags_written(["a"], {"id": "p"}, "POST /v1/projects")

    def test_unchanged_row_refuses(self) -> None:
        # Old backend ignored the write and echoed the old list.
        with pytest.raises(errors.NotFoundError, match="0066"):
            Client._verify_tags_written(["new"], {"tags": ["old"]}, "r")

    def test_spooled_write_is_unverifiable_and_passes(self) -> None:
        Client._verify_tags_written(["a"], None, "r")
