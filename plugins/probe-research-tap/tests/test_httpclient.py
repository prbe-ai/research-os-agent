"""Retry-disposition classification for HTTP responses.

The invariant that matters for data safety: a batch that can NEVER succeed on
retry of the SAME body must be POISON (dropped + logged), not RETRY. The tap has
no client-side batch-splitting, so a 413 (body over the gateway's 2MB cap) or a
422 (schema rejection) classified as RETRY would re-POST the same doomed body
forever until the outbox byte cap reaps it — silent data loss + wasted bandwidth.
401 stays HALT (dead ingest token), checked ahead of the 4xx bucket.
"""

from __future__ import annotations

import pytest

from tap import httpclient
from tap.httpclient import Classification, classify


def test_2xx_is_success() -> None:
    for status in (200, 201, 202, 204, 299):
        assert classify(status, err=False) is Classification.SUCCESS


def test_401_is_halt() -> None:
    assert classify(401, err=False) is Classification.HALT


@pytest.mark.parametrize("status", [400, 402, 403, 404, 405, 409, 410, 413, 422, 429, 499])
def test_all_other_4xx_is_poison(status: int) -> None:
    """Every non-401 4xx drops the batch — the daemon keeps running."""
    assert classify(status, err=False) is Classification.POISON


def test_413_and_422_are_poison_not_retry() -> None:
    """The wedge this fix closes: an oversized (413) or malformed (422) batch can
    never succeed on retry, so it must be dropped, not retried into an infinite
    re-POST loop."""
    assert classify(413, err=False) is Classification.POISON
    assert classify(422, err=False) is Classification.POISON
    assert classify(413, err=False) is not Classification.RETRY
    assert classify(422, err=False) is not Classification.RETRY


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_retry(status: int) -> None:
    """Server-side transient failures are worth retrying."""
    assert classify(status, err=False) is Classification.RETRY


def test_transport_error_is_retry() -> None:
    """A network error (err=True) is always retryable regardless of status."""
    assert classify(0, err=True) is Classification.RETRY
    assert classify(413, err=True) is Classification.RETRY


def test_module_exposes_classification_members() -> None:
    # Guard the vocabulary the outbox drain branches on.
    assert {c.value for c in httpclient.Classification} == {
        "success",
        "poison",
        "halt",
        "retry",
    }


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
