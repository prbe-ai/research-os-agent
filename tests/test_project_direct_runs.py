"""Project-direct runs (research-os 0054) — the client seams.

Covers the SDK happy path (run(project=...) with no experiment), the client-side
group rejection, both old-backend guards (the actionable 404 on the missing
route and the refuse-to-mislabel guard on the ignored project_id filter), and
the search-collapse pass-through for run hits.
"""

from __future__ import annotations

import pytest

from probe import errors
from probe.mcp.service import _collapse_experiments
from tests.conftest import open_run


def test_run_with_only_a_project_opens_a_direct_run(client, app):
    project = client.create_project("folding", "Folding")
    run = client.run(project="folding", name="direct-1", heartbeat=False)

    row = app.runs[run.id]
    assert row["experiment_id"] is None
    assert row["project_id"] == project["id"]


def test_run_with_a_project_and_a_group_is_rejected_client_side(client, app):
    """Run groups are experiment-anchored; the SDK says so instead of letting
    the backend 422 phrase it."""
    client.create_project("folding-grouped", "Folding")
    with pytest.raises(errors.ValidationError) as excinfo:
        client.run(
            project="folding-grouped", name="d", group_id="g-1", heartbeat=False
        )
    assert "experiment-anchored" in str(excinfo.value)


def test_create_project_run_on_an_old_backend_names_the_upgrade(client, app):
    """A pre-0054 backend has no /v1/projects/{id}/runs route; its bare
    route-level 404 must not masquerade as 'project does not exist'."""
    client.create_project("folding-old", "Folding")
    app.supports_project_direct = False
    with pytest.raises(errors.NotFoundError) as excinfo:
        client.run(project="folding-old", name="d", heartbeat=False)
    assert "predates" in str(excinfo.value)


def test_create_project_run_against_a_missing_project_stays_a_plain_404(client, app):
    """The handler's own 'project not found' passes through untranslated — the
    upgrade hint is reserved for the route-level 404."""
    with pytest.raises(errors.NotFoundError) as excinfo:
        client.create_project_run(
            "00000000-0000-0000-0000-000000000000", "d", heartbeat=False
        )
    assert "project not found" in str(excinfo.value)
    assert "predates" not in str(excinfo.value)


def test_list_runs_project_scope_on_an_old_backend_refuses_to_mislabel(client, app):
    """A pre-0054 backend ignores ?project_id= and returns the unscoped list;
    the SDK detects the missing project_id field and raises instead of handing
    back tenant-wide rows labeled as project-scoped."""
    open_run(client, experiment="e-old-scope")
    app.supports_project_direct = False
    with pytest.raises(errors.NotFoundError) as excinfo:
        client.list_runs(project_id="p-1")
    assert "predates" in str(excinfo.value)


def test_list_runs_direct_filter_narrows_to_experiment_less_runs(client, app):
    project = client.create_project("folding-direct", "Folding")
    open_run(client, experiment="e-mixed")
    direct = client.create_project_run(project["id"], "d", heartbeat=False)

    rows = client.list_runs(project_id=project["id"], direct=True).items
    assert [row["id"] for row in rows] == [direct.id]


def test_collapse_keeps_one_experiment_and_one_run_per_id():
    """collapse="experiment" dedupes experiments AND passes run hits through
    (deduped) — a project-direct run has no experiment hit to represent it."""
    rows = [
        {"entity_type": "experiment", "id": "e1", "why_matched": {"score": 1.0}},
        {"entity_type": "experiment", "id": "e1", "why_matched": {"score": 0.5}},
        {"entity_type": "run", "id": "r1", "why_matched": {"score": 0.9}},
        {"entity_type": "run", "id": "r1", "why_matched": {"score": 0.2}},
        {"entity_type": "document", "id": "d1", "why_matched": {"score": 0.8}},
    ]
    collapsed = _collapse_experiments(rows)
    assert sorted((row["entity_type"], row["id"]) for row in collapsed) == [
        ("experiment", "e1"),
        ("run", "r1"),
    ]
