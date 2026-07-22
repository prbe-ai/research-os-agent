"""Durable, backend-agnostic capture ledger primitives."""

from __future__ import annotations

import json

import pytest

from probe.sdk.capture import (
    CaptureLedger,
    CaptureState,
    stable_external_key,
    stable_span_id,
)


def test_external_keys_are_stable_and_delimiter_safe():
    key = stable_external_key("Miles", "Rollout", "job:7", 600, "sample/a")
    assert key == "probe:v1:miles:rollout:job%3A7:600:sample%2Fa"
    assert stable_external_key("Miles", "Rollout", "job:7", 600, "sample/a") == key
    assert stable_span_id("run-1", key) == stable_span_id("run-1", key)
    assert stable_span_id("run-2", key) != stable_span_id("run-1", key)


@pytest.mark.parametrize(
    "args",
    [
        ("", "rollout", "1"),
        ("miles", "", "1"),
        ("miles", "rollout"),
        ("miles", "rollout", None),
    ],
)
def test_external_keys_reject_ambiguous_empty_parts(args):
    with pytest.raises(ValueError):
        stable_external_key(*args)


def test_ledger_distinguishes_collection_from_remote_confirmation(tmp_path):
    path = tmp_path / "capture.json"
    ledger = CaptureLedger(
        path,
        source="harbor",
        external_key="probe:v1:harbor:trial:t-1",
        context={"scope": "trial_directory"},
    )
    ledger.expect("trajectory", role="trajectory", relative_path="trajectory.json")
    ledger.mark(
        "trajectory",
        CaptureState.hashed,
        content_hash="a" * 64,
        size_bytes=17,
    )
    ledger.finish_inventory()

    report = ledger.report()
    assert report["collection"] == {"state": "complete", "missing": []}
    assert report["capture"]["state"] == "pending"
    assert report["capture"]["missing"][0]["state"] == "hashed"

    ledger.mark("trajectory", CaptureState.upload_failed, error="R2 unavailable")
    report = ledger.report()
    assert report["collection"]["state"] == "complete"  # teardown remains safe
    assert report["capture"]["state"] == "partial"

    ledger.mark("trajectory", CaptureState.confirmed, artifact_id="art-1", error=None)
    assert ledger.report()["capture"] == {"state": "complete", "missing": []}

    reopened = CaptureLedger.open(path)
    assert reopened.get("trajectory")["artifact_id"] == "art-1"
    assert json.loads(path.read_text())["schema_version"] == "probe.capture/v1"


def test_declared_missing_artifact_prevents_collection_claim(tmp_path):
    ledger = CaptureLedger(tmp_path / "capture.json", source="harbor")
    ledger.expect("result", role="result", relative_path="result.json")
    ledger.mark("result", CaptureState.missing, error="not produced")
    ledger.finish_inventory()

    report = ledger.report()
    assert report["collection"]["state"] == "partial"
    assert report["capture"]["state"] == "partial"
    assert report["collection"]["missing"][0]["relative_path"] == "result.json"


def test_optional_skips_are_visible_but_do_not_make_capture_partial(tmp_path):
    ledger = CaptureLedger(tmp_path / "capture.json", source="harbor")
    ledger.expect("hidden", role="other", relative_path=".secret", required=False)
    ledger.mark("hidden", CaptureState.intentionally_skipped, error="hidden control file")
    ledger.finish_inventory()

    report = ledger.report()
    assert report["collection"]["state"] == "complete"
    assert report["capture"]["state"] == "complete"
    assert report["skipped"][0]["relative_path"] == ".secret"
