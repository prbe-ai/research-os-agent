"""The coordinate read surface (B7): grouped / wide / export / catalog / latest.

Five backend routes were REST-only (the parity ledger's coordinate/telemetry
entries). This file proves the SDK methods that retired those entries actually
speak the routes' contracts: `by` comma-joined, `where` JSON-encoded, grouped and
wide reads follow `next_step` paging, the export generator follows `after_id`
keyset paging, and the 0062 `agg` declaration rides the write so a read can omit
its own. The CLI verbs and read-only MCP tools are smoked over the same fake.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from probe import cli, errors
from probe.mcp.server import create_server
from probe.mcp.service import ResearchReadService
from probe.mcp.source import ResearchOSSource
from tests.conftest import make_client, open_run


def _point(id: int, key: str, value: float, step: int, dims: dict | None = None) -> dict:
    return {
        "id": id, "key": key, "kind": "model", "value": value,
        "step_index": step, "dimensions": dims or {},
    }


# -- SDK: grouped -------------------------------------------------------------


def test_grouped_encodes_by_comma_joined_and_where_json(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [
        _point(1, "loss", 1.0, 0, {"rank": 0, "split": "train"}),
        _point(2, "loss", 3.0, 0, {"rank": 1, "split": "train"}),
        _point(3, "loss", 9.0, 0, {"rank": 0, "split": "val"}),
    ]
    out = client.get_metrics_grouped(
        run.id, "loss", agg="sum", by=["rank", "split"], where={"split": "train"}
    )
    request = next(r for r in app.requests if r.url.path.endswith("/metrics/grouped"))
    assert request.url.params["by"] == "rank,split"  # comma-joined, one param
    assert json.loads(request.url.params["where"]) == {"split": "train"}
    assert out["agg"] == "sum"
    # the val point is filtered out; group labels are each value's JSON text
    assert [(g["group"], g["value"], g["n"]) for g in out["groups"]] == [
        ({"rank": "0", "split": '"train"'}, 1.0, 1),
        ({"rank": "1", "split": '"train"'}, 3.0, 1),
    ]


def test_grouped_follows_next_step_until_exhausted(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [_point(i + 1, "loss", float(i), i) for i in range(5)]
    app.grouped_page_rows = 2  # the server's own ceiling sits below the request

    out = client.get_metrics_grouped(run.id, "loss")

    assert [g["value"] for g in out["groups"]] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert out["truncated"] is False and out["next_step"] is None
    pages = [r for r in app.requests if r.url.path.endswith("/metrics/grouped")]
    assert [p.url.params.get("step_from") for p in pages] == [None, "2", "4"]


def test_grouped_max_rows_bounds_the_total_and_reports_the_cut(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [_point(i + 1, "loss", float(i), i) for i in range(5)]

    out = client.get_metrics_grouped(run.id, "loss", max_rows=2)
    assert len(out["groups"]) == 2
    assert out["truncated"] is True and out["next_step"] == 2

    rest = client.get_metrics_grouped(run.id, "loss", step_from=out["next_step"])
    assert [g["value"] for g in rest["groups"]] == [2.0, 3.0, 4.0]
    assert rest["truncated"] is False


def test_grouped_omitted_agg_resolves_the_declared_reduce_fn(client, app):
    """0062: the request carries NO agg param, and the server answers with the
    key's declared fn (else mean)."""
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [
        _point(1, "loss", 1.0, 0), _point(2, "loss", 3.0, 0),
    ]
    app.declared_aggs["loss"] = "sum"

    out = client.get_metrics_grouped(run.id, "loss")
    request = next(r for r in app.requests if r.url.path.endswith("/metrics/grouped"))
    assert "agg" not in request.url.params
    assert out["agg"] == "sum"
    assert out["groups"][0]["value"] == 4.0


def test_grouped_conflicting_declarations_are_a_422(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 1.0}, step=0, agg="mean")  # write-side declaration...
    app.declared_aggs["loss"] = "sum"  # ...conflicting with another
    with pytest.raises(errors.ValidationError):
        client.get_metrics_grouped(run.id, "loss")


# -- SDK: the write-side agg declaration --------------------------------------


def test_log_agg_rides_every_point_and_none_stays_off_the_wire(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log({"tokens": 512.0, "loss": 0.4}, step=1, agg="sum")
    body = json.loads(app.requests[-1].content)
    assert [p["agg"] for p in body["points"]] == ["sum", "sum"]

    run.log({"loss": 0.3}, step=2)
    body = json.loads(app.requests[-1].content)
    assert "agg" not in body["points"][0]


# -- SDK: wide ----------------------------------------------------------------


def test_wide_follows_paging_and_realigns_columns_by_series_identity(client, app):
    """A page's columns cover only its own step window, so a series that starts
    late widens a later page; merged rows must realign, never trust positions."""
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [
        _point(1, "loss", 0.9, 0),
        _point(2, "loss", 0.8, 1),
        _point(3, "loss", 0.7, 2),
        _point(4, "acc", 0.5, 2),
        _point(5, "loss", 0.6, 3),
        _point(6, "acc", 0.6, 3),
    ]
    app.wide_page_rows = 2

    out = client.get_metrics_wide(run.id)

    assert [c["key"] for c in out["columns"]] == ["loss", "acc"]
    assert [(r["step_index"], r["values"]) for r in out["rows"]] == [
        (0, [0.9, None]),
        (1, [0.8, None]),
        (2, [0.7, 0.5]),
        (3, [0.6, 0.6]),
    ]
    assert out["truncated"] is False and out["next_step"] is None
    pages = [r for r in app.requests if r.url.path.endswith("/metrics/wide")]
    assert [p.url.params.get("step_from") for p in pages] == [None, "2"]


def test_wide_key_narrows_as_a_repeated_param(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [
        _point(1, "loss", 0.9, 0), _point(2, "acc", 0.5, 0), _point(3, "lr", 0.1, 0),
    ]
    out = client.get_metrics_wide(run.id, key=["loss", "acc"])
    request = next(r for r in app.requests if r.url.path.endswith("/metrics/wide"))
    assert request.url.params.get_list("key") == ["loss", "acc"]
    assert [c["key"] for c in out["columns"]] == ["acc", "loss"]


# -- SDK: export --------------------------------------------------------------


def test_export_generator_follows_after_id_keyset_paging(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [
        _point(i + 1, "loss", float(i), i) for i in range(5)
    ] + [_point(9, "acc", 0.5, 0)]

    points = list(client.export_metric_points(run.id, key="loss", limit=2))

    assert [p["id"] for p in points] == [1, 2, 3, 4, 5]
    pages = [r for r in app.requests if r.url.path.endswith("/metrics/export")]
    assert [p.url.params.get("after_id") for p in pages] == [None, "2", "4"]
    assert all(p.url.params["key"] == "loss" for p in pages)


# -- SDK: coordinates + latest ------------------------------------------------


_CATALOG_ROW = {
    "dims_hash": "h-rank0", "dimensions": {"rank": 0},
    "has_metrics": True, "has_spans": False, "has_artifacts": False,
    "first_seen_at": "2026-07-15T00:00:00Z", "last_seen_at": "2026-07-15T00:00:01Z",
}


def test_list_run_coordinates_returns_the_catalog(client, app):
    run = open_run(client, experiment="e", name="r")
    app.coordinates[run.id] = [_CATALOG_ROW]
    assert client.list_run_coordinates(run.id) == [_CATALOG_ROW]
    assert run.coordinates() == [_CATALOG_ROW]


def test_latest_scalars_posts_run_ids_keys_and_kind(client, app):
    first = open_run(client, experiment="e", name="r1")
    second = open_run(client, experiment="e", name="r2")
    app.series[first.id] = [
        {"key": "loss", "kind": "scalar", "dimensions": {}, "point_count": 2,
         "last_value": 0.3, "min_value": 0.3, "max_value": 0.9, "last_step_index": 1},
    ]
    app.series[second.id] = [
        {"key": "loss", "kind": "scalar", "dimensions": {}, "point_count": 1,
         "last_value": 0.5, "min_value": 0.5, "max_value": 0.5, "last_step_index": 0},
        {"key": "acc", "kind": "scalar", "dimensions": {}, "point_count": 1,
         "last_value": 0.7, "min_value": 0.7, "max_value": 0.7, "last_step_index": 0},
    ]

    out = client.latest_scalars([first.id, second.id], keys=["loss"], kind="scalar")

    body = json.loads(app.requests[-1].content)
    assert body == {"run_ids": [first.id, second.id], "keys": ["loss"], "kind": "scalar"}
    assert [(s["run_id"], s["last_value"]) for s in out["scalars"]] == [
        (first.id, 0.3), (second.id, 0.5),
    ]


def test_latest_scalars_404s_on_a_deleted_run(client, app):
    run = open_run(client, experiment="e", name="r")
    client.delete_run(run.id)
    with pytest.raises(errors.NotFoundError):
        client.latest_scalars([run.id])


def test_run_handle_delegators_hit_this_runs_routes(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [_point(1, "loss", 1.0, 0)]
    run.grouped_metrics("loss")
    run.wide_metrics(key=["loss"])
    list(run.export_points(key="loss"))
    run.coordinates()
    paths = [r.url.path for r in app.requests]
    for suffix in ("/metrics/grouped", "/metrics/wide", "/metrics/export", "/coordinates"):
        assert f"/v1/runs/{run.id}{suffix}" in paths


# -- CLI ----------------------------------------------------------------------


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    def factory(**_kw):
        return make_client(app, tmp_spool=tmp_path / "spool")

    monkeypatch.setattr(cli, "Client", factory)
    cli.main(["project", "create", "p"])
    cli.main(["experiment", "create", "e", "--hypothesis", "h", "--project", "p"])
    return app


def _started_run(wired, capsys) -> str:
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    return capsys.readouterr().out.strip()


def test_metrics_grouped_command(wired, capsys):
    rid = _started_run(wired, capsys)
    wired.metric_points[rid] = [
        _point(1, "loss", 1.0, 0, {"rank": 0, "split": "train"}),
        _point(2, "loss", 3.0, 0, {"rank": 1, "split": "train"}),
    ]
    rc = cli.main([
        "metrics", "grouped", rid, "--key", "loss",
        "--agg", "sum", "--by", "rank", "--where", '{"split": "train"}',
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["agg"] == "sum"
    assert [g["group"] for g in out["groups"]] == [{"rank": "0"}, {"rank": "1"}]
    request = next(r for r in wired.requests if r.url.path.endswith("/metrics/grouped"))
    assert request.url.params["by"] == "rank"
    assert json.loads(request.url.params["where"]) == {"split": "train"}


def test_metrics_wide_command(wired, capsys):
    rid = _started_run(wired, capsys)
    wired.metric_points[rid] = [
        _point(1, "loss", 0.9, 0), _point(2, "acc", 0.5, 0), _point(3, "lr", 0.1, 0),
    ]
    rc = cli.main(["metrics", "wide", rid, "--key", "loss", "--key", "acc"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [c["key"] for c in out["columns"]] == ["acc", "loss"]
    request = next(r for r in wired.requests if r.url.path.endswith("/metrics/wide"))
    assert request.url.params.get_list("key") == ["loss", "acc"]


def test_metrics_export_command_streams_ndjson(wired, capsys):
    rid = _started_run(wired, capsys)
    wired.metric_points[rid] = [_point(i + 1, "loss", float(i), i) for i in range(3)]
    rc = cli.main(["metrics", "export", rid, "--key", "loss", "--limit", "2"])
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json.loads(line)["id"] for line in lines] == [1, 2, 3]


def test_coordinates_command(wired, capsys):
    rid = _started_run(wired, capsys)
    wired.coordinates[rid] = [_CATALOG_ROW]
    rc = cli.main(["coordinates", rid])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == [_CATALOG_ROW]


def test_series_latest_command(wired, capsys):
    first = _started_run(wired, capsys)
    cli.main(["run", "start", "--experiment", "e", "--name", "r2"])
    second = capsys.readouterr().out.strip()
    for rid, value in ((first, 0.3), (second, 0.5)):
        wired.series[rid] = [
            {"key": "loss", "kind": "scalar", "dimensions": {}, "point_count": 1,
             "last_value": value, "min_value": value, "max_value": value,
             "last_step_index": 0},
        ]
    rc = cli.main(["series", "latest", first, second, "--key", "loss"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [(s["run_id"], s["last_value"]) for s in out["scalars"]] == [
        (first, 0.3), (second, 0.5),
    ]


def test_log_command_declares_agg(wired, capsys):
    rid = _started_run(wired, capsys)
    rc = cli.main(["log", rid, "tokens=512", "--step", "1", "--agg", "sum"])
    assert rc == 0
    body = json.loads(wired.requests[-1].content)
    assert body["points"][0]["agg"] == "sum"


# -- MCP ----------------------------------------------------------------------


def _server(client):
    return create_server(ResearchReadService(ResearchOSSource(client)))


def _call(server, tool: str, args: dict):
    """Invoke a tool the way a real MCP client does, and unwrap the payload."""
    result = asyncio.run(server.call_tool(tool, args))
    payload = result[1] if isinstance(result, tuple) else result
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    if isinstance(payload, list):  # content blocks
        return json.loads(payload[0].text)
    return payload


def test_mcp_grouped_tool_reads_the_reduction(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [
        _point(1, "loss", 1.0, 0, {"rank": 0}), _point(2, "loss", 3.0, 0, {"rank": 1}),
    ]
    out = _call(_server(client), "get_metrics_grouped",
                {"run_id": run.id, "key": "loss", "agg": "sum", "by": ["rank"]})
    assert out["completeness"] == {"state": "complete", "missing": []}
    assert [g["group"] for g in out["data"]["groups"]] == [{"rank": "0"}, {"rank": "1"}]


def test_mcp_grouped_tool_reports_a_cut_read_with_a_resume_cursor(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [_point(i + 1, "loss", float(i), i) for i in range(5)]
    server = _server(client)

    page = _call(server, "get_metrics_grouped",
                 {"run_id": run.id, "key": "loss", "max_rows": 2})
    assert page["completeness"]["state"] == "partial"
    assert "rows_beyond_page_bound" in page["completeness"]["missing"]
    assert page["next_cursor"] == "2"

    rest = _call(server, "get_metrics_grouped",
                 {"run_id": run.id, "key": "loss", "step_from": int(page["next_cursor"])})
    assert rest["completeness"]["state"] == "complete"
    assert [g["value"] for g in rest["data"]["groups"]] == [2.0, 3.0, 4.0]


def test_mcp_coordinates_tool_lists_the_catalog(client, app):
    run = open_run(client, experiment="e", name="r")
    app.coordinates[run.id] = [_CATALOG_ROW]
    out = _call(_server(client), "get_run_coordinates", {"run_id": run.id})
    assert out["completeness"] == {"state": "complete", "missing": []}
    assert out["data"]["coordinates"] == [_CATALOG_ROW]


def test_mcp_export_tool_serves_bounded_keyset_pages(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [_point(i + 1, "loss", float(i), i) for i in range(5)]
    server = _server(client)

    page = _call(server, "export_metric_points",
                 {"run_id": run.id, "key": "loss", "limit": 2})
    assert [p["id"] for p in page["data"]["points"]] == [1, 2]
    assert page["completeness"]["state"] == "partial"
    assert "rows_beyond_page_bound" in page["completeness"]["missing"]
    assert page["next_cursor"] == "2"

    last = _call(server, "export_metric_points",
                 {"run_id": run.id, "key": "loss", "limit": 3,
                  "after_id": int(page["next_cursor"])})
    assert [p["id"] for p in last["data"]["points"]] == [3, 4, 5]
    assert last["completeness"] == {"state": "complete", "missing": []}
    assert last["next_cursor"] is None


def test_mcp_export_tool_clamps_a_runaway_limit(client, app):
    run = open_run(client, experiment="e", name="r")
    app.metric_points[run.id] = [_point(1, "loss", 1.0, 0)]
    _call(_server(client), "export_metric_points",
          {"run_id": run.id, "limit": 10**6})
    request = next(r for r in app.requests if r.url.path.endswith("/metrics/export"))
    assert request.url.params["limit"] == "1000"  # the tool's page ceiling
