"""run(on_conflict=...) — the supersede policy for duplicate external_ids.

"supersede" opens external_id-rN with parent_relation="retry" lineage and
marks a dead incumbent "superseded" — the from-scratch-retry half. The
resume half (auto/resume policies) lives in test_run_resume.py.
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


def test_duplicate_of_a_live_run_still_conflicts_by_default(lab, app):
    # The fake leaves a fresh run "running", so the default (auto) policy
    # refuses to hijack it rather than resuming or superseding.
    _open(lab)
    with pytest.raises(errors.ConflictError) as excinfo:
        _open(lab)
    assert "alive" in str(excinfo.value)


def test_explicit_error_policy_keeps_the_bare_409(lab, app):
    _open(lab)
    with pytest.raises(errors.ConflictError) as excinfo:
        _open(lab, on_conflict="error")
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


# -- the incumbent is picked on the WHOLE key --------------------------------
#
# Run identity is (customer_id, source, external_id). The recovery path scanned
# the first page of list_runs for external_id ALONE, so a run under a different
# source sharing the id could be resumed or superseded in place of the real
# incumbent. The 409 already names the right row -- the server resolves it on
# both halves of the key -- so ask it instead of re-deriving the answer.


def test_the_conflicts_existing_id_names_the_incumbent(lab, app):
    original = _open(lab, source="sdk")
    app.runs[original.id]["status"] = "failed"

    recovered = _open(lab, source="sdk", on_conflict="resume")

    assert recovered.id == original.id


def test_another_sources_run_is_not_mistaken_for_the_incumbent(lab, app):
    """Same external_id, two sources: the namespace is shared by design (an
    ingest run and an sdk run may both be 'se-cnn-600-s0').

    The decoy is opened FIRST and left `running`, so a scan matching on
    external_id alone meets it before the real incumbent and the policy then
    refuses with "its writer may still be alive" -- stranding a run that was
    recoverable all along.
    """
    decoy = _open(lab, source="ingest")  # left running
    incumbent = _open(lab, source="sdk")
    app.runs[incumbent.id]["status"] = "failed"

    recovered = _open(lab, source="sdk", on_conflict="resume")

    assert recovered.id == incumbent.id
    assert recovered.id != decoy.id


def test_supersede_retries_from_the_right_incumbent(lab, app):
    """The wrong incumbent poisons supersede too: the retry hangs its
    parent_run_id / retry_of lineage off whichever run was picked."""
    decoy = _open(lab, source="ingest")
    incumbent = _open(lab, source="sdk")
    app.runs[incumbent.id]["status"] = "crashed"

    retry = _open(lab, source="sdk", on_conflict="supersede")

    assert app.runs[retry.id]["parent_run_id"] == incumbent.id
    assert app.runs[retry.id]["parent_run_id"] != decoy.id
