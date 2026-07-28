"""The below-run coordinate layer: ``run.unit(...)`` + producer wiring.

coords = bounded grouping axes (SERIES identity), labels = per-sample
drill-down ids (POINT identity only), span_id = exemplar pointer. The wire
assertions read the payloads the FakeApp captured, because the SDK serializes
through the generated models with exclude_none — a field the model lacks (or a
kwarg never wired) vanishes silently, and only the captured body can tell.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid

import pytest

from probe.sdk.unit_context import UnitContext, current
from tests.conftest import open_run


# -- explicit kwargs reach the wire ---------------------------------------------
def test_log_sends_dimensions_labels_and_span_id(client, app):
    run = open_run(client, experiment="e", name="r")
    sid = str(uuid.uuid4())
    run.log(
        {"loss": 0.5}, step=3,
        dimensions={"rank": 0}, labels={"sample": 7}, span_id=sid,
    )
    (point,) = app.metric_points_posted[run.id]
    assert point["dimensions"] == {"rank": 0}
    assert point["labels"] == {"sample": 7}
    assert point["span_id"] == sid


def test_span_sends_coords_field_not_attributes(client, app):
    run = open_run(client, experiment="e", name="r")
    run.span("shard", name="shard-2", coords={"rank": 2})
    (span,) = app.spans[run.id]
    assert span["coords"] == {"rank": 2}
    # NOT folded into attributes: the server mirrors coords for display itself.
    assert span["attributes"] == {}


def test_log_artifact_sends_coords_and_labels(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log_artifact(
        "completions.jsonl", uri="r2://b/completions.jsonl",
        coords={"rank": 1}, labels={"sample": "s-9"},
    )
    body = json.loads(app.requests[-1].content)
    assert body["coords"] == {"rank": 1}
    assert body["labels"] == {"sample": "s-9"}


# -- the ambient unit context ---------------------------------------------------
def test_ambient_unit_flows_without_call_site_kwargs(client, app):
    """The whole point: producers inside the block carry the unit's maps."""
    run = open_run(client, experiment="e", name="r")
    with run.unit(coords={"rank": 0}, labels={"sample": 3}):
        run.log({"loss": 0.42}, step=12)
        run.span("rollout", name="rollout-0")
        run.log_artifact("notes.txt", uri="r2://b/notes.txt")
    (point,) = app.metric_points_posted[run.id]
    assert point["dimensions"] == {"rank": 0}
    assert point["labels"] == {"sample": 3}
    (span,) = app.spans[run.id]
    assert span["coords"] == {"rank": 0}  # coords only; labels never ride spans
    (artifact,) = app.artifacts[run.id]
    assert artifact["coords"] == {"rank": 0}
    assert artifact["labels"] == {"sample": 3}


def test_nested_units_merge_child_wins_per_key(client, app):
    run = open_run(client, experiment="e", name="r")
    with run.unit(coords={"rank": 0, "split": "val"}, labels={"sample": 1}):
        with run.unit(coords={"rank": 1}, labels={"uid": "p1"}):
            run.log({"loss": 1.0}, step=0)
    (point,) = app.metric_points_posted[run.id]
    assert point["dimensions"] == {"rank": 1, "split": "val"}
    assert point["labels"] == {"sample": 1, "uid": "p1"}


def test_call_site_kwargs_override_ambient(client, app):
    run = open_run(client, experiment="e", name="r")
    with run.unit(coords={"rank": 0, "split": "val"}, labels={"sample": 1}):
        run.log({"loss": 1.0}, dimensions={"rank": 5}, labels={"sample": 9})
        run.span("shard", coords={"rank": 7})
    point = app.metric_points_posted[run.id][0]
    assert point["dimensions"] == {"rank": 5, "split": "val"}
    assert point["labels"] == {"sample": 9}
    assert app.spans[run.id][0]["coords"] == {"rank": 7, "split": "val"}


def test_context_restored_after_exit_and_bare_points_stay_byte_identical(client, app):
    run = open_run(client, experiment="e", name="r")
    with run.unit(coords={"rank": 0}, labels={"sample": 1}):
        run.log({"loss": 1.0}, step=1)
    run.log({"loss": 2.0}, step=2)
    after = app.metric_points_posted[run.id][-1]
    assert after["dimensions"] == {}
    # Absent, not null/empty: a unit-less point serializes exactly as before.
    assert "labels" not in after
    assert "span_id" not in after
    assert app.spans.get(run.id) is None  # (sanity: nothing else was written)


# -- the coords/labels split is enforced client-side ---------------------------
def test_unit_rejects_a_key_in_both_maps(client, app):
    run = open_run(client, experiment="e", name="r")
    with pytest.raises(ValueError, match="both coords and labels"):
        with run.unit(coords={"rank": 0}, labels={"rank": 1}):
            pass


def test_nesting_cannot_smuggle_an_overlap(client, app):
    run = open_run(client, experiment="e", name="r")
    with run.unit(coords={"rank": 0}):
        with pytest.raises(ValueError, match="both coords and labels"):
            with run.unit(labels={"rank": 1}):
                pass


def test_call_site_overlap_with_ambient_raises_before_any_write(client, app):
    run = open_run(client, experiment="e", name="r")
    with run.unit(coords={"rank": 0}):
        with pytest.raises(ValueError, match="both coords and labels"):
            run.log({"loss": 1.0}, labels={"rank": 1})
    assert app.metric_points_posted == {}  # the bad point never left the client


def test_unit_rejects_nested_values(client, app):
    run = open_run(client, experiment="e", name="r")
    with pytest.raises(ValueError, match="must be a scalar"):
        run.unit(coords={"rank": {"nested": 1}})


# -- isolation: contextvars, not globals ---------------------------------------
def test_unit_is_not_inherited_by_threads():
    seen: dict[str, tuple] = {}
    with UnitContext(coords={"rank": 0}):
        thread = threading.Thread(target=lambda: seen.update(ctx=current()))
        thread.start()
        thread.join()
        assert current() == ({"rank": 0}, {})
    assert seen["ctx"] == ({}, {})  # a fresh thread starts with no unit


def test_units_are_isolated_across_asyncio_tasks():
    async def worker(rank: int, out: dict) -> None:
        with UnitContext(coords={"rank": rank}):
            await asyncio.sleep(0)  # yield so the tasks interleave
            out[rank] = current()[0]["rank"]

    async def main() -> dict:
        out: dict = {}
        await asyncio.gather(worker(0, out), worker(1, out))
        return out

    assert asyncio.run(main()) == {0: 0, 1: 1}


# -- spool interaction: baked at call time, never resolved at flush ------------
def test_spooled_write_replays_the_coordinate_ambient_at_call_time(client, app):
    run = open_run(client, experiment="e", name="r")
    app.fail_next_metrics = True
    with run.unit(coords={"rank": 3}, labels={"sample": 4}):
        assert run.log({"loss": 0.1}, step=1) is None  # spooled, fail-open
    # The unit is gone by flush time; the payload must already carry it.
    client.flush()
    (point,) = app.metric_points_posted[run.id]
    assert point["dimensions"] == {"rank": 3}
    assert point["labels"] == {"sample": 4}


# -- the presign uploads door has no coordinate fields -------------------------
def test_upload_door_never_carries_coords_but_the_fallback_reference_does(
    client, app, tmp_path
):
    run = open_run(client, experiment="e", name="r")
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 16)
    with run.unit(coords={"rank": 0}, labels={"sample": 1}):
        run.log_artifact("a.bin", path=str(f))
        upload_req = json.loads(
            next(
                r for r in app.requests if r.url.path.endswith("/artifacts/uploads")
            ).content
        )
        # The server's UploadRequest has no such fields; sending them anyway
        # would be dropped at best (and a 422 on a stricter model).
        assert "coords" not in upload_req
        assert "labels" not in upload_req

        app.fail_next_uploads = True
        with pytest.warns(UserWarning, match="recorded as a reference"):
            run.log_artifact("b.bin", path=str(f))
    fallback = json.loads(app.requests[-1].content)
    assert fallback["is_reference"] is True
    assert fallback["coords"] == {"rank": 0}
    assert fallback["labels"] == {"sample": 1}
