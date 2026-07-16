"""The surface closed in the 2026-07-15 parity pass: tokens, groups, lifecycle, reads.

tests/test_parity.py proves each backend route is *reachable*. These prove the calls
are shaped right — that reachable also means correct.
"""

from __future__ import annotations

import json
import uuid

import pytest
import typer

from probe import cli, errors
from tests.conftest import make_client


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    def factory(**_kw):
        return make_client(app, tmp_spool=tmp_path / "spool")

    monkeypatch.setattr(cli, "Client", factory)
    return app


# -- tokens -----------------------------------------------------------------
def test_list_response_model_omits_the_secret_by_contract():
    """`list_tokens` is a passthrough, so "never exposes a secret" cannot be proven
    against the fake (which only returns what the test put in). The real guarantee is
    the contract: GET /v1/tokens returns TokenOut, which has no plaintext field, while
    only the mint response (TokenCreated) carries one. Assert that, not a tautology."""
    from probe.models import TokenCreated, TokenOut

    assert "token" not in TokenOut.model_fields  # the list/rows model — no secret
    assert "token" in TokenCreated.model_fields  # the mint model — the one place it lives


def test_list_tokens_passes_through_the_endpoint_rows(client, app):
    app.tokens["t-1"] = {
        "id": "t-1",
        "name": "ci-bot",
        "token_prefix": "probe_pat_abcd",
        "scopes": ["read"],
        "created_at": "2026-07-15T00:00:00Z",
    }
    (listed,) = client.list_tokens()
    assert listed["token_prefix"] == "probe_pat_abcd"


def test_revoke_token_deletes_it(client, app):
    app.tokens["t-1"] = {"id": "t-1", "name": "old", "token_prefix": "probe_pat_x"}
    client.revoke_token("t-1")
    assert app.tokens == {}


def test_revoking_an_unknown_token_raises(client, app):
    with pytest.raises(errors.NotFoundError):
        client.revoke_token("nope")


def test_create_token_uses_the_device_flow_not_the_session_only_mint(client, app, monkeypatch):
    """`POST /v1/tokens` 403s for a token caller by design, so mint MUST go through
    the browser device flow. Guards against someone 'simplifying' it back."""
    seen = {}

    def fake_authorize(base_url, **kw):
        seen.update(base_url=base_url, **kw)
        return {"id": "t-9", "name": kw["token_name"], "token": "probe_pat_thesecret"}

    monkeypatch.setattr("probe.sdk.device.device_authorize", fake_authorize)
    created = client.create_token("ci-bot", scopes=["read"])

    assert created["token"] == "probe_pat_thesecret"
    assert seen["token_name"] == "ci-bot"
    assert seen["scopes"] == ["read"]
    # Nothing was sent to the session-only mint route.
    assert not [r for r in app.requests if r.url.path == "/v1/tokens" and r.method == "POST"]


def test_token_create_prints_the_secret_once(wired, capsys, monkeypatch):
    monkeypatch.setattr(
        "probe.sdk.device.device_authorize",
        lambda base_url, **kw: {"id": "t-9", "name": kw["token_name"], "token": "probe_pat_shown1ce"},
    )
    rc = cli.main(["token", "create", "--name", "ci-bot", "--scope", "read"])
    assert rc == 0
    out = capsys.readouterr()
    assert out.out.count("probe_pat_shown1ce") == 1
    assert "only time it is shown" in out.err


def test_token_create_shows_the_secret_even_if_name_and_id_are_missing(wired, capsys, monkeypatch):
    """The token is minted the instant the mint response arrives, and its plaintext
    exists exactly once. A drifted response missing name/id must NOT KeyError before
    the secret is printed — that would orphan an unrecoverable token."""
    monkeypatch.setattr(
        "probe.sdk.device.device_authorize",
        lambda base_url, **kw: {"token": "probe_pat_survives"},  # no id, no name
    )
    rc = cli.main(["token", "create", "--name", "ci-bot"])
    assert rc == 0
    out = capsys.readouterr()
    assert "probe_pat_survives" in out.out
    assert "ci-bot" in out.out  # falls back to the requested name


# -- groups -----------------------------------------------------------------
def test_group_create_then_run_start_can_reference_it(wired, capsys):
    """The dead-end this closes: create_run accepted a group_id the client had no
    way to obtain, because group creation was unreachable."""
    cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()
    experiment_id = wired.runs[run_id]["experiment_id"]

    rc = cli.main(["group", "create", experiment_id, "--name", "lr-sweep", "--kind", "sweep",
                   "--spec", '{"lr": [0.1, 0.01]}'])
    assert rc == 0
    group = json.loads(capsys.readouterr().out)
    assert group["kind"] == "sweep"
    assert group["spec"] == {"lr": [0.1, 0.01]}

    rc = cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r2",
                   "--group", group["id"]])
    assert rc == 0
    grouped = capsys.readouterr().out.strip()
    assert wired.runs[grouped]["group_id"] == group["id"]


def test_group_name_conflict_raises(client, app):
    client.fail_open = False
    exp = client.ensure_experiment("e", "E", "h")
    client.create_group(exp["id"], "dupe")
    with pytest.raises(errors.ConflictError):
        client.create_group(exp["id"], "dupe")


def test_update_group_is_field_replace(client, app):
    exp = client.ensure_experiment("e", "E", "h")
    group = client.create_group(exp["id"], "sweep-1", spec={"lr": [0.1]})
    updated = client.update_group(group["id"], name="renamed")
    assert updated["name"] == "renamed"
    assert updated["spec"] == {"lr": [0.1]}  # untouched


def test_update_group_needs_a_field(client, app):
    with pytest.raises(ValueError):
        client.update_group("g-1")


def test_list_and_get_group_read_back(client, app):
    """list_groups/get_group had only reachability coverage — prove the calls are
    shaped right and the response reads back."""
    client.fail_open = False
    exp = client.ensure_experiment("e", "E", "h")
    created = client.create_group(exp["id"], "sweep-x", spec={"lr": [0.1]})
    assert [g["id"] for g in client.list_groups(exp["id"])] == [created["id"]]
    fetched = client.get_group(created["id"])
    assert fetched["name"] == "sweep-x" and fetched["spec"] == {"lr": [0.1]}
    with pytest.raises(errors.NotFoundError):
        client.get_group(str(uuid.uuid4()))


# -- lifecycle --------------------------------------------------------------
def test_experiment_archive_is_idempotent_then_restores(client, app):
    exp = client.ensure_experiment("e", "E", "h")
    first = client.archive_experiment(exp["id"])
    assert first["archived_at"]
    again = client.archive_experiment(exp["id"])
    assert again["archived_at"] == first["archived_at"]  # keeps the original time
    assert client.restore_experiment(exp["id"])["archived_at"] is None


def test_run_delete_then_restore(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    assert client.delete_run(run.id)["deleted_at"]
    with pytest.raises(errors.NotFoundError):
        client.delete_run(run.id)  # already deleted
    assert client.restore_run(run.id)["deleted_at"] is None


def test_gc_runs_requires_exactly_one_selector(client, app):
    with pytest.raises(ValueError):
        client.gc_runs()
    with pytest.raises(ValueError):
        client.gc_runs(run_ids=["r-1"], older_than="2026-01-01T00:00:00Z")


def test_gc_runs_rejects_an_empty_id_list(client, app):
    """An empty run_ids is not a valid selector — sending `{"run_ids": []}` risks the
    backend reading it as an unfiltered purge. It must raise, not slip through."""
    with pytest.raises(ValueError):
        client.gc_runs(run_ids=[])
    assert not [r for r in app.requests if r.url.path == "/v1/runs/gc"]


def test_gc_runs_purges_by_id(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    client.delete_run(run.id)
    assert client.gc_runs(run_ids=[run.id])["purged"] == 1
    assert run.id not in app.runs


def test_gc_runs_never_purges_a_live_run(client, app):
    """gc only ever reaps soft-deleted runs; naming a live one purges nothing."""
    run = client.run(experiment="e", hypothesis="h", name="r")
    assert client.gc_runs(run_ids=[run.id])["purged"] == 0
    assert run.id in app.runs


def test_gc_runs_honors_the_older_than_cutoff(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    client.delete_run(run.id)  # fake stamps deleted_at = 2026-07-15
    assert client.gc_runs(older_than="2026-01-01T00:00:00Z")["purged"] == 0
    assert client.gc_runs(older_than="2026-08-01T00:00:00Z")["purged"] == 1


def test_run_gc_cli_requires_confirmation(wired, monkeypatch):
    """Purging is irreversible, so the CLI must not do it on a bare invocation."""
    asked = []

    def refuse(text, **kw):
        asked.append(text)
        raise typer.Abort()

    monkeypatch.setattr(typer, "confirm", refuse)
    assert cli.main(["run", "gc", "--older-than", "2026-01-01T00:00:00Z"]) == 1
    assert "cannot be undone" in asked[0]
    assert not [r for r in wired.requests if r.url.path == "/v1/runs/gc"]


def test_run_gc_cli_proceeds_with_yes(wired):
    """--yes is the scriptable path: no prompt, and the purge actually fires."""
    assert cli.main(["run", "gc", "--older-than", "2026-01-01T00:00:00Z", "--yes"]) == 0
    assert [r for r in wired.requests if r.url.path == "/v1/runs/gc"]


def test_run_gc_cli_rejects_both_selectors(wired):
    assert cli.main(["run", "gc", "--id", "r-1", "--older-than", "2026-01-01T00:00:00Z", "--yes"]) == 2


def test_delete_artifact(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.log_artifact("out", uri="s3://b/k")
    artifact_id = app.artifacts[run.id][0]["id"]
    client.delete_artifact(artifact_id)
    assert app.artifacts[run.id] == []


def test_delete_tolerates_a_non_json_2xx_body(client, app):
    """A 2xx DELETE carrying a non-JSON body (a CDN/ingress HTML interstitial) must
    not escape as a raw JSONDecodeError — the delete still succeeded."""
    import httpx

    from probe.sdk.transport import Transport

    def handler(request):
        return httpx.Response(200, content=b"<html>rate limited</html>")

    mock = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    t = Transport(client.settings, client=mock)
    assert t.delete("/v1/runs/whatever") is None  # tolerated, not a crash


def test_gc_uploads_only_sweeps_pending(client, app, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
    blob = tmp_path / "w.bin"
    blob.write_bytes(b"bytes")
    run.log_artifact("confirmed", path=str(blob), strict=True)  # presign->PUT->confirm
    app.artifacts[run.id].append(
        {"id": "a-pending", "status": "pending", "created_at": "2026-07-01T00:00:00Z"}
    )

    assert client.gc_uploads("2026-07-16T00:00:00Z")["swept"] == 1
    remaining = [a["status"] for a in app.artifacts[run.id]]
    assert remaining == ["complete"]  # the confirmed upload survives


def test_gc_uploads_honors_the_older_than_cutoff(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    app.artifacts[run.id] = [
        {"id": "a-recent", "status": "pending", "created_at": "2026-07-14T00:00:00Z"}
    ]
    assert client.gc_uploads("2026-07-01T00:00:00Z")["swept"] == 0
    assert client.gc_uploads("2026-07-16T00:00:00Z")["swept"] == 1


# -- reads ------------------------------------------------------------------
def test_run_series_and_metrics_read_back(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    app.series[run.id] = [{"key": "loss", "kind": "model", "last_value": 0.1}]
    app.metric_points[run.id] = [
        {"key": "loss", "value": 0.5, "step_index": 1},
        {"key": "acc", "value": 0.9, "step_index": 1},
    ]
    assert client.run_series(run.id)[0]["key"] == "loss"
    assert [p["key"] for p in client.run_metrics(run.id, key="loss")] == ["loss"]


def test_run_spans_and_get_span(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    app.spans[run.id] = [
        {"id": "s-1", "span_type": "rollout", "name": "a"},
        {"id": "s-2", "span_type": "eval", "name": "b"},
    ]
    assert [s["id"] for s in client.run_spans(run.id, span_type="eval")] == ["s-2"]
    assert client.get_span("s-1")["name"] == "a"


def test_experiment_edges_are_scoped_to_the_experiment(client, app):
    client.fail_open = False
    mine = client.run(experiment="e", hypothesis="h", name="r")
    client.add_edge(
        source_type="run", source_id=mine.id, relation="produces",
        target_type="artifact", target_id=str(uuid.uuid4()),
    )
    # An edge under a different experiment must not leak into this one's view.
    other = client.create_run(str(uuid.uuid4()), "other-run")
    client.add_edge(
        source_type="run", source_id=other.id, relation="produces",
        target_type="artifact", target_id=str(uuid.uuid4()),
    )

    edges = client.experiment_edges(app.runs[mine.id]["experiment_id"])
    assert [e["source_id"] for e in edges] == [mine.id]


def test_trace_file_reports_its_missing_backend_without_a_doomed_call(client, app):
    """The artifact trace index has no backend route; the MCP tool degrades honestly
    rather than 404ing on every call."""
    from probe.mcp.source import ResearchOSSource

    result = ResearchOSSource(client).trace_file("loss.png")
    assert result["missing_capability"] == "artifact_trace_index"
    assert not [r for r in app.requests if "trace" in r.url.path]
