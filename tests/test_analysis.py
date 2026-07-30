"""client.compare(): several runs read back aligned on step.

The job people open `wandb.Api()` for. Here it is a shaping layer over the
existing `query_series`, not a second client.
"""

from __future__ import annotations

import pytest

from tests.conftest import open_run


@pytest.fixture
def three_runs(client, app):
    """Three runs of DELIBERATELY different lengths — comparing configs that ran
    for different numbers of steps is the normal case, not an edge case."""
    runs = [open_run(client, experiment="e1", name=f"r{i}") for i in range(3)]
    app.seed_series(runs[0].id, "dockq", {0: 0.1, 1: 0.2, 2: 0.3})
    app.seed_series(runs[1].id, "dockq", {0: 0.5, 1: 0.6})
    app.seed_series(runs[2].id, "dockq", {0: 0.9})
    app.seed_series(runs[0].id, "loss", {0: 2.0, 1: 1.0})
    return runs


def test_compare_aligns_runs_on_a_shared_step_axis(client, app, three_runs):
    comparison = client.compare(run_ids=[r.id for r in three_runs])
    aligned = comparison.aligned("dockq")

    assert aligned.steps == [0, 1, 2]
    assert len(aligned.labels) == 3
    # the union of every run's axis, with holes where a run stopped early —
    # truncating to the shortest would hide exactly what is being compared
    by_length = sorted(aligned.values.values(), key=lambda col: sum(v is not None for v in col))
    assert by_length[0] == [0.9, None, None]
    assert by_length[-1] == [0.1, 0.2, 0.3]


def test_columns_are_labelled_by_petname(client, app, three_runs):
    """`short_id` is what a person recognises, and unlike `name` the server
    guarantees it is distinct."""
    comparison = client.compare(run_ids=[r.id for r in three_runs])
    first = three_runs[0]
    expected = app.runs[first.id].get("short_id") or first.name
    assert comparison.label(first.id) == expected
    assert expected in comparison.aligned("dockq").labels


def test_keys_lists_what_is_available(client, app, three_runs):
    comparison = client.compare(run_ids=[r.id for r in three_runs])
    assert sorted(comparison.keys) == ["dockq", "loss"]


def test_an_absent_key_says_what_is_there(client, app, three_runs):
    comparison = client.compare(run_ids=[r.id for r in three_runs])
    with pytest.raises(KeyError, match="dockq"):
        comparison.aligned("dock")


def test_selecting_keys_narrows_the_query(client, app, three_runs):
    """The selector goes to the server, so a wide comparison does not drag back
    every series a run ever wrote."""
    client.compare(run_ids=[r.id for r in three_runs], keys=["dockq"])
    (sent,) = app.series_queries
    assert sent["series"] == [{"key": "dockq", "kind": "model"}]


def test_step_bounds_are_passed_through(client, app, three_runs):
    comparison = client.compare(run_ids=[r.id for r in three_runs], step_from=1, step_to=1)
    assert comparison.aligned("dockq").steps == [1]


def test_runs_can_be_selected_by_filter_instead_of_id(client, app, three_runs):
    """`compare(experiment_id=...)` is the shape people actually want — they know
    the experiment, not the ids."""
    experiment_id = three_runs[0].experiment_id
    comparison = client.compare(experiment_id=experiment_id)
    assert len(comparison) == 3


def test_comparing_nothing_is_empty_rather_than_an_error(client, app):
    comparison = client.compare(run_ids=[])
    assert len(comparison) == 0
    assert comparison.keys == []
    assert app.series_queries == []


def test_more_than_fifty_runs_are_batched_not_truncated(client, app, monkeypatch):
    """The endpoint caps run_ids at 50. Dropping runs 51+ would read as "these are
    all of them", which is the wrong answer to hand someone comparing configs."""
    runs = [open_run(client, experiment="e1", name=f"r{i}") for i in range(51)]
    for index, run in enumerate(runs):
        app.seed_series(run.id, "dockq", {0: float(index)})

    with pytest.warns(UserWarning, match="51 runs"):
        comparison = client.compare(run_ids=[r.id for r in runs])

    assert len(app.series_queries) == 2
    assert len(comparison.aligned("dockq").labels) == 51


def test_rows_iterates_step_by_step(client, app, three_runs):
    comparison = client.compare(run_ids=[r.id for r in three_runs])
    rows = list(comparison.aligned("dockq").rows())
    assert [step for step, _ in rows] == [0, 1, 2]
    assert all(len(values) == 3 for _, values in rows)


def test_a_point_with_no_step_is_left_off_the_step_axis(client, app):
    """Wall-clock points have no position on a step-aligned table. Inventing one
    would put unrelated points on the same row."""
    run = open_run(client, experiment="e1", name="r")
    app.series_points[str(run.id)] = [
        {
            "run_id": str(run.id),
            "key": "dockq",
            "kind": "model",
            "x_axis": "wall_clock",
            "dimensions": {},
            "points": [
                {"step_index": None, "value": 0.4, "wall_clock": "2026-07-27T00:00:00Z"},
                {"step_index": 3, "value": 0.7, "wall_clock": "2026-07-27T00:00:01Z"},
            ],
        }
    ]
    aligned = client.compare(run_ids=[run.id]).aligned("dockq")
    assert aligned.steps == [3]
    assert list(aligned.values.values())[0] == [0.7]


# -- regressions caught in pre-landing review ---------------------------------
def test_a_filtered_comparison_follows_the_cursor(client, app):
    """GET /v1/runs defaults to limit=50 and Page does not auto-paginate, so
    taking .items would compare an experiment's first page and present it as the
    whole thing — the exact failure the >50 batching guards against."""
    runs = [open_run(client, experiment="e1", name=f"r{i}") for i in range(120)]
    for index, run in enumerate(runs):
        app.seed_series(run.id, "dockq", {0: float(index)})

    experiment_id = runs[0].experiment_id
    comparison = client.compare(experiment_id=experiment_id)
    assert len(comparison) == 120
    assert len(comparison.aligned("dockq").labels) == 120


def test_runs_sharing_a_label_do_not_collapse_into_one_column(client, app):
    """short_id is optional AND nullable on the wire, so the fallback is `name` —
    and generated run names are second-resolution, so a sweep produces collisions.
    Merging two runs into one curve is a silently wrong answer."""
    a = open_run(client, experiment="e1", name="same")
    b = open_run(client, experiment="e1", name="same")
    app.runs[a.id]["short_id"] = None
    app.runs[b.id]["short_id"] = None
    app.seed_series(a.id, "dockq", {0: 0.1})
    app.seed_series(b.id, "dockq", {0: 0.9})

    aligned = client.compare(run_ids=[a.id, b.id]).aligned("dockq")
    assert len(aligned.labels) == 2, "two runs collapsed into one column"
    assert sorted(v[0] for v in aligned.values.values()) == [0.1, 0.9]
