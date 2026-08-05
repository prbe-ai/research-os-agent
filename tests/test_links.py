"""Dashboard links are only worth emitting if a wrong one is impossible.

The whole value of putting a `url` on entities is that an agent stops inventing
them, and an invented URL is indistinguishable from a real one until it 404s in
the researcher's browser. So the tests that matter here are the ones about
DECLINING: an API host that names no dashboard has to produce nothing at all,
because the alternative -- falling back to the public host -- hands a
self-hosted install links into somebody else's tenant.
"""

from __future__ import annotations

import json

import pytest

from probe.mcp.service import ResearchReadService
from probe.mcp.source import ResearchOSSource
from probe.sdk import links
from tests.conftest import open_run

DASHBOARD = "https://research.prbe.ai"


@pytest.fixture(autouse=True)
def _no_ambient_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own PROBE_DASHBOARD_URL must not decide these assertions."""
    monkeypatch.delenv(links.DASHBOARD_URL_ENV, raising=False)


# -- deriving the origin ----------------------------------------------------
def test_public_api_host_implies_the_dashboard_host() -> None:
    assert links.dashboard_base_url("https://api.research.prbe.ai") == DASHBOARD


def test_trailing_slash_on_the_api_url_is_not_carried_into_the_link() -> None:
    assert links.dashboard_base_url("https://api.research.prbe.ai/") == DASHBOARD


@pytest.mark.parametrize(
    "api_base_url",
    [
        # The hosted MCP's own value (deploy/mcp/k8s.yaml). This is THE case the
        # override exists for: an in-cluster Service names no public host, and
        # deriving from it would mint links to a hostname that resolves only
        # inside the cluster -- unreachable from the browser it is handed to.
        "http://research-os.research.svc.cluster.local:8080",
        # A dev API. `localhost` has no dashboard sibling to strip down to.
        "http://localhost:8080",
        "https://api.localhost",
        # Self-hosted shapes where the dashboard could be anywhere.
        "https://probe.acme.internal",
        "https://research-api.acme.com",
        # Nothing left after the label.
        "https://api.",
    ],
)
def test_an_api_host_that_names_no_dashboard_yields_nothing(api_base_url: str) -> None:
    assert links.dashboard_base_url(api_base_url) is None


def test_the_env_override_wins_over_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployment gets the last word — it is the only party that actually knows."""
    monkeypatch.setenv(links.DASHBOARD_URL_ENV, "https://probe.acme.internal/")
    assert (
        links.dashboard_base_url("https://api.research.prbe.ai")
        == "https://probe.acme.internal"
    )


def test_the_override_rescues_a_host_that_derives_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(links.DASHBOARD_URL_ENV, DASHBOARD)
    in_cluster = "http://research-os.research.svc.cluster.local:8080"
    assert links.dashboard_base_url(in_cluster) == DASHBOARD


# -- entity routes ----------------------------------------------------------
@pytest.mark.parametrize(
    ("kind", "route"),
    [("run", "runs"), ("experiment", "experiments"), ("project", "projects")],
)
def test_each_routed_kind_gets_its_page(kind: str, route: str) -> None:
    url = links.entity_url(kind, "abc", api_base_url="https://api.research.prbe.ai")
    assert url == f"{DASHBOARD}/{route}/abc"


@pytest.mark.parametrize("kind", ["artifact", "group", "document", "workspace"])
def test_kinds_the_dashboard_does_not_route_get_no_link(kind: str) -> None:
    """These render inside another entity's page; there is no URL to hand back."""
    assert links.entity_url(kind, "abc", api_base_url="https://api.research.prbe.ai") is None


def test_a_missing_id_yields_nothing_rather_than_a_link_to_none() -> None:
    assert links.entity_url("run", None, api_base_url="https://api.research.prbe.ai") is None


def test_a_petname_is_a_valid_run_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /runs/{run_ref} resolves a uuid OR a short_id, and the dashboard page
    passes its route param straight through — so `probe run end tunneling-sambar-254`
    can link the petname it was given without first resolving it to a uuid."""
    url = links.entity_url(
        "run", "tunneling-sambar-254", api_base_url="https://api.research.prbe.ai"
    )
    assert url == f"{DASHBOARD}/runs/tunneling-sambar-254"


# -- MCP payloads -----------------------------------------------------------
def _service(client) -> ResearchReadService:
    return ResearchReadService(ResearchOSSource(client))


def _seed_browse(app, project: dict) -> None:
    """Serve GET /v1/browse. `browse_response = None` models a backend that
    predates the route, and the source then reports the capability as absent —
    which returns `projects: None` and tests nothing about links."""
    app.browse_response = {
        "depth": 1,
        "projects": [{"id": project["id"], "slug": project["slug"], "name": project["slug"]}],
        "experiments": None,
        "runs": None,
    }


def test_browse_nodes_carry_their_url(client, app, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(links.DASHBOARD_URL_ENV, DASHBOARD)
    project = client.create_project("folding")
    _seed_browse(app, project)

    tree = _service(client).browse_research()

    node = next(p for p in tree["data"]["projects"] if p["id"] == project["id"])
    assert node["url"] == f"{DASHBOARD}/projects/{project['id']}"


def test_the_card_carries_its_url(client, app, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(links.DASHBOARD_URL_ENV, DASHBOARD)
    project = client.create_project("folding")

    card = _service(client).get_entity(f"project:{project['id']}")

    assert card["data"]["url"] == f"{DASHBOARD}/projects/{project['id']}"


def test_no_url_key_at_all_when_the_origin_is_unknown(client, app) -> None:
    """Absent, not null. An explicit `"url": null` on every node of every browse
    response is a token cost paid on every orientation call to say nothing, and
    it invites a model to print the string "None"."""
    project = client.create_project("folding")
    _seed_browse(app, project)

    tree = _service(client).browse_research()
    card = _service(client).get_entity(f"project:{project['id']}")

    node = next(p for p in tree["data"]["projects"] if p["id"] == project["id"])
    assert "url" not in node
    assert "url" not in card["data"]


# -- SDK ---------------------------------------------------------------------
def test_run_exposes_its_url_without_printing_it(
    client, app, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`run.url` is a property, not a side effect: a library that printed to
    stdout from inside a training loop would corrupt whatever the job's own
    output is being parsed by."""
    monkeypatch.setenv(links.DASHBOARD_URL_ENV, DASHBOARD)
    run = open_run(client, experiment="e", name="r")

    assert run.url == f"{DASHBOARD}/runs/{run.id}"
    assert capsys.readouterr().out == ""


def test_run_url_is_none_when_the_origin_is_unknown(client, app) -> None:
    """The test client's base URL is `http://test`, which implies no dashboard."""
    run = open_run(client, experiment="e", name="r")

    assert run.url is None


# -- CLI: stdout stays machine-readable -------------------------------------
# `RUN=$(probe run start ...)` is the documented way to open a run from a shell
# script, and `probe project create | jq` the way to read one back. A link on
# stdout would be captured into that variable, or would make the JSON
# unparseable -- so every one of these goes to stderr.
def test_print_link_writes_to_stderr(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(links.DASHBOARD_URL_ENV, DASHBOARD)
    from probe.cli.main import _print_link

    _print_link("run", "abc")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == f"{DASHBOARD}/runs/abc"


def test_print_link_is_silent_when_the_origin_is_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """With no config at all the SDK defaults to the hosted API, which DOES imply
    a dashboard — so silence has to be tested against a base URL that implies
    none, not against an empty config."""
    monkeypatch.setenv("PROBE_BASE_URL", "http://research-os.research.svc.cluster.local:8080")
    from probe.cli.main import _print_link

    _print_link("run", "abc")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_json_stdout_stays_parseable_alongside_a_link(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`probe project create ... | jq` must keep working: the link goes to stderr,
    so stdout is still exactly one JSON document."""
    monkeypatch.setenv(links.DASHBOARD_URL_ENV, DASHBOARD)
    from probe.cli.main import _print_json, _print_link

    _print_json({"id": "abc", "slug": "folding"})
    _print_link("project", "abc")

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"id": "abc", "slug": "folding"}
    assert captured.err.strip() == f"{DASHBOARD}/projects/abc"
