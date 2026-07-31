"""Reconcile-before-retry: a lost response must not duplicate an artifact.

The 502 that motivated ``with_retries`` came from the proxy in FRONT of the API,
so it fires after the write has already landed. Retrying blindly records the same
bytes twice, and downstream selection then picks one of two without saying so.
"""

from __future__ import annotations

from probe.sdk.run import Run


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def list_run_artifacts(self, run_id, *, scope="all", **filters):
        self.calls.append((run_id, scope, filters))
        rows = self._rows
        if "name" in filters:
            rows = [r for r in rows if r.get("name") == filters["name"]]
        return rows


def _run(rows):
    client = _FakeClient(rows)
    return Run.__new__(Run), client


def _bind(rows):
    run, client = _run(rows)
    run._client = client
    run._data = {"id": "run-1"}
    return run, client


def test_finds_the_artifact_a_lost_response_hid():
    run, _ = _bind([{"id": "art-1", "name": "adapter", "content_hash": "abc123"}])
    assert run.reconcile_artifact("adapter", "abc123")["id"] == "art-1"


def test_same_name_different_content_is_not_a_match():
    """A genuinely new version must still be created, not silently reused."""
    run, _ = _bind([{"id": "art-1", "name": "adapter", "content_hash": "abc123"}])
    assert run.reconcile_artifact("adapter", "def456") is None


def test_absent_artifact_returns_none():
    run, _ = _bind([])
    assert run.reconcile_artifact("adapter", "abc123") is None


def test_missing_content_hash_never_matches():
    """Without a hash there is no exact identity, so refuse to guess."""
    run, _ = _bind([{"id": "art-1", "name": "adapter", "content_hash": None}])
    assert run.reconcile_artifact("adapter", "") is None


def test_scopes_to_this_run_only():
    """An inherited experiment/project artifact is not this run's record."""
    run, client = _bind([{"id": "art-1", "name": "adapter", "content_hash": "abc123"}])
    run.reconcile_artifact("adapter", "abc123")
    assert client.calls[0][1] == "own"


class _Run:
    """Minimal stand-in exercising the retry loop around a flaky create."""

    def __init__(self, run):
        self.run = run
        self.created = []

    def create(self, name, content_hash, *, fail_after_write: bool):
        existing = self.run.reconcile_artifact(name, content_hash)
        if existing:
            return existing
        row = {"id": f"art-{len(self.created) + 1}", "name": name, "content_hash": content_hash}
        self.created.append(row)
        self.run._client._rows.append(row)
        if fail_after_write:
            raise RuntimeError("502 Bad Gateway")
        return row


def test_retry_after_lost_response_creates_exactly_one():
    run, _ = _bind([])
    helper = _Run(run)
    try:
        helper.create("adapter", "abc123", fail_after_write=True)
    except RuntimeError:
        pass
    helper.create("adapter", "abc123", fail_after_write=False)
    assert len(helper.created) == 1, "retry must reuse, not duplicate"
