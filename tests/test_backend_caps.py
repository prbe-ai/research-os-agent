"""Server-side caps must never be reported as a complete read.

Two live bugs, both the same shape: the BACKEND silently bounds a list, the client
hands the bounded list back, and the envelope calls it complete. The agent then acts
on "all of it" when it saw a prefix.

The fakes hid both — one ignored `limit`, the other never exercised the cap. A fake
kinder than the backend is a fake that certifies broken code.
"""

from __future__ import annotations


from probe.mcp.service import ResearchReadService
from probe.mcp.source import ResearchOSSource


def _service(client):
    return ResearchReadService(ResearchOSSource(client))


def _artifact(rid: str, i: int) -> dict:
    return {
        "id": f"art-{i}", "run_id": rid, "name": f"f-{i}.png", "kind": "figure",
        "status": "ready", "is_reference": False, "uri": f"s3://b/f-{i}.png",
        "customer_id": "lab-42", "created_at": "2026-07-16T00:00:00Z",
    }


def test_handoff_says_so_when_the_bundle_caps_its_artifact_list(client, app, monkeypatch):
    """GET /v1/runs/{ref}/bundle caps artifacts at 200 server-side (`_BUNDLE_ARTIFACT_LIMIT`)
    while `artifact_total` counts them all, and the route takes no offset.

    handoff used the bundle's artifacts as its rows with more_beyond=False, so a run
    with 5000 artifacts emitted 200 and reported state="complete" — the agent believes
    it saw every output the run produced."""
    _proj = client.create_project("folding")
    client.create_experiment("e", "e", hypothesis="h", project_id=_proj["id"])
    run = client.run(project="folding", experiment="e", name="r")
    rid = run.id

    # The backend hands back a capped list beside an honest total.
    def _capped_bundle(_run_id):
        return {
            "run": app.runs[rid], "series": [],
            "artifacts": [_artifact(rid, i) for i in range(200)],  # the server cap
            "artifact_total": 5000,                                 # the truth
            "span_types": [], "parent_run_id": None, "child_run_ids": [],
        }

    monkeypatch.setattr(ResearchOSSource, "bundle", staticmethod(_capped_bundle))
    result = _service(client).get_entity(f"run:{rid}", view="handoff", token_budget=1_000_000)

    assert result["data"]["artifact_total"] == 5000
    assert len(result["data"]["artifacts"]) == 200
    assert "artifacts_beyond_bundle_limit" in result["completeness"]["missing"]
    assert result["completeness"]["state"] == "partial"
    # No cursor: paging a server-truncated list would hand back an empty page as if
    # it were the end. view="artifacts" reads the uncapped route instead.
    assert result["next_cursor"] is None


def test_handoff_is_complete_when_the_bundle_was_not_capped(client, app):
    """The marker must be CONDITIONAL — an unconditional one is the lie this whole
    change removed."""
    _proj = client.create_project("folding")
    client.create_experiment("e", "e", hypothesis="h", project_id=_proj["id"])
    run = client.run(project="folding", experiment="e", name="r")
    app.artifacts[run.id] = [_artifact(run.id, 0)]
    result = _service(client).get_entity(f"run:{run.id}", view="handoff", token_budget=100_000)
    assert result["completeness"]["missing"] == []
    assert result["completeness"]["state"] == "complete"

