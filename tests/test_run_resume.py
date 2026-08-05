"""run(on_conflict="auto"/"resume") — a dead incumbent is reopened in place.

Same run, same curve: the reopen bumps the write epoch, registers the new
writer session, and the returned handle refuses steps the first execution
already wrote. Completed and live incumbents still conflict — resuming is for
dead runs only, and a from-scratch retry belongs to "supersede".
"""

from __future__ import annotations

import pytest

from probe import errors


def _crashed_job(client, app, status="crashed", steps=(50, 100)):
    """A run that logged some curve and then died."""
    client.create_project("resume-lab", "Resume Lab")
    run = client.run(project="resume-lab", name="job", external_id="job",
                     heartbeat=False)
    for s in steps:
        run.log({"loss": 1.0 / (s or 1)}, step=s)
    app.runs[run.id]["status"] = status
    app.runs[run.id]["ended_at"] = "2026-01-01T01:00:00Z"
    return run


def test_auto_resumes_a_crashed_incumbent_in_place(client, app):
    first = _crashed_job(client, app)

    resumed = client.run(project="resume-lab", name="job", external_id="job",
                         heartbeat=False)  # default policy: auto

    assert resumed.id == first.id  # same identity, not a -rN sibling
    row = app.runs[first.id]
    assert row["status"] == "running"
    assert row["ended_at"] is None
    assert row["write_epoch"] == 2
    assert row["current_session_id"] == resumed.session_id
    (recovery,) = row["recoveries"]
    assert recovery["prior_status"] == "crashed"
    assert recovery["last_step"] == 100


def test_resumed_run_refuses_steps_already_written(client, app):
    _crashed_job(client, app)
    resumed = client.run(project="resume-lab", name="job", external_id="job",
                         heartbeat=False)

    with pytest.raises(errors.ValidationError) as excinfo:
        resumed.log({"loss": 0.5}, step=100)
    assert "supersede" in str(excinfo.value)

    resumed.log({"loss": 0.5}, step=101)  # past the resume point: fine
    steps = [p["step_index"] for p in app.metric_points_posted[resumed.id]]
    assert steps.count(101) == 1


def test_resumed_auto_steps_continue_past_the_resume_point(client, app):
    _crashed_job(client, app)
    resumed = client.run(project="resume-lab", name="job", external_id="job",
                         heartbeat=False)

    resumed.log({"loss": 0.4})  # no step: auto counter must start at 101

    assert max(p["step_index"] for p in app.metric_points_posted[resumed.id]) == 101


def test_auto_on_a_completed_incumbent_still_conflicts(client, app):
    _crashed_job(client, app, status="completed")

    with pytest.raises(errors.ConflictError) as excinfo:
        client.run(project="resume-lab", name="job", external_id="job",
                   heartbeat=False)
    assert "supersede" in str(excinfo.value)


def test_auto_on_a_live_incumbent_refuses_to_hijack(client, app):
    client.create_project("resume-lab", "Resume Lab")
    client.run(project="resume-lab", name="job", external_id="job",
               heartbeat=False)  # fake leaves it "running"

    with pytest.raises(errors.ConflictError) as excinfo:
        client.run(project="resume-lab", name="job", external_id="job",
                   heartbeat=False)
    assert "alive" in str(excinfo.value)


def test_explicit_resume_on_a_live_incumbent_also_refuses(client, app):
    client.create_project("resume-lab", "Resume Lab")
    client.run(project="resume-lab", name="job", external_id="job",
               heartbeat=False)

    with pytest.raises(errors.ConflictError):
        client.run(project="resume-lab", name="job", external_id="job",
                   heartbeat=False, on_conflict="resume")


def test_resume_against_an_old_backend_names_the_upgrade(client, app):
    _crashed_job(client, app)
    app.supports_reopen = False

    with pytest.raises(errors.NotFoundError) as excinfo:
        client.run(project="resume-lab", name="job", external_id="job",
                   heartbeat=False)
    assert "predates" in str(excinfo.value)


def test_supersede_still_available_for_from_scratch_retries(client, app):
    first = _crashed_job(client, app)

    retry = client.run(project="resume-lab", name="job", external_id="job",
                       heartbeat=False, on_conflict="supersede")

    assert retry.id != first.id
    assert app.runs[retry.id]["external_id"] == "job-r2"
    assert "superseded" in app.runs[first.id]["tags"]
