"""run(on_conflict=...) — what a duplicate external_id means.

Default stays the engine's 409. "supersede" opens external_id-rN with
parent_relation="retry" lineage, and marks a dead incumbent "superseded"
instead of reopening it (reopening splices two executions into one curve).
"""

from __future__ import annotations

import pytest

from probe import errors


def _open(client, external_id="se-cnn-600-s0", **kw):
    return client.run(
        project="retry-lab",
        name=external_id,
        external_id=external_id,
        heartbeat=False,
        **kw,
    )


@pytest.fixture
def lab(client):
    client.create_project("retry-lab", "Retry Lab")
    return client


def test_duplicate_external_id_still_409s_by_default(lab, app):
    _open(lab)
    with pytest.raises(errors.ConflictError) as excinfo:
        _open(lab)
    assert "already exists" in str(excinfo.value)


def test_bogus_policy_is_refused_before_any_network(lab):
    with pytest.raises(errors.ValidationError) as excinfo:
        _open(lab, external_id="x", on_conflict="reuse")
    assert "not a policy" in str(excinfo.value)


def test_supersede_after_a_crash_opens_r2_and_marks_the_husk(lab, app):
    first = _open(lab)
    app.runs[first.id]["status"] = "crashed"
    app.runs[first.id]["tags"] = ["debug"]

    retry = _open(lab, on_conflict="supersede")

    row = app.runs[retry.id]
    assert row["external_id"] == "se-cnn-600-s0-r2"
    assert row["name"] == "se-cnn-600-s0-r2"
    assert row["parent_run_id"] == first.id
    assert row["parent_relation"] == "retry"
    assert row["foreign_keys"]["retry_of"] == first.id
    assert row["foreign_keys"]["retry_attempt"] == 2
    # The husk keeps its record but nobody should trust its numbers.
    assert app.runs[first.id]["tags"] == ["debug", "superseded"]


def test_supersede_after_completion_leaves_the_incumbent_unmarked(lab, app):
    first = _open(lab)
    app.runs[first.id]["status"] = "completed"

    retry = _open(lab, on_conflict="supersede")

    assert app.runs[retry.id]["external_id"] == "se-cnn-600-s0-r2"
    # A repeat of a good run is a repeat, not a correction.
    assert "superseded" not in app.runs[first.id]["tags"]


def test_supersede_skips_retry_slots_already_taken(lab, app):
    first = _open(lab)
    app.runs[first.id]["status"] = "crashed"
    second = _open(lab, on_conflict="supersede")  # -r2
    app.runs[second.id]["status"] = "crashed"

    third = _open(lab, on_conflict="supersede")

    assert app.runs[third.id]["external_id"] == "se-cnn-600-s0-r3"
    # Lineage always points at the ORIGINAL identity, not the last attempt.
    assert app.runs[third.id]["parent_run_id"] == first.id


def test_supersede_works_for_experiment_attached_runs(client, app):
    project = client.create_project("retry-lab-exp", "Retry Lab")
    client.create_experiment(
        "sweep", "Sweep", hypothesis="h", project_id=project["id"]
    )
    first = client.run(
        experiment="sweep", name="run-a", external_id="run-a", heartbeat=False
    )
    app.runs[first.id]["status"] = "failed"

    retry = client.run(
        experiment="sweep",
        name="run-a",
        external_id="run-a",
        heartbeat=False,
        on_conflict="supersede",
    )

    row = app.runs[retry.id]
    assert row["external_id"] == "run-a-r2"
    assert row["experiment_id"] == app.runs[first.id]["experiment_id"]
    assert app.runs[first.id]["tags"] == ["superseded"]
