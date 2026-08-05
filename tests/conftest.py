"""A stateful in-memory fake of the Probe Research v3 API over httpx.MockTransport.

Routes only what the client exercises, with response shapes matching CONTRACT.md.
Lets us test the SDK + CLI end to end with no live server.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

from probe.client import Client
from probe.config import Settings
from probe.transport import Transport

@pytest.fixture(autouse=True)
def _hw_collector_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the hardware collector OFF regardless of the developer's shell.

    The collector is opt-in for users, but a developer with PROBE_HW=1
    exported would otherwise get a collector + inventory thread per run() in
    the legacy suite (real psutil/NVML probes, extra fake-API calls that
    break exact-sequence assertions). The hw tests opt in explicitly via
    their own autouse fixture, which runs after this one."""
    monkeypatch.setenv("PROBE_HW", "0")


@pytest.fixture(autouse=True)
def _no_live_token_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the hosted MCP's edge token check off by default.

    It calls the real ``/v1/me``, so any test handing a bearer to the wrapper would
    quietly hit production and fail on a 401. Tests that cover verification inject
    their own ``token_rejected`` instead (see tests/test_mcp_hosted.py).
    """
    monkeypatch.setenv("PROBE_MCP_VERIFY_TOKEN", "0")


@pytest.fixture(autouse=True)
def _isolate_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """Point every test's config at a throwaway dir — no exceptions.

    ``config_path()`` resolves ``$XDG_CONFIG_HOME/probe/config.json`` and falls back to
    ``~/.config``. Only a handful of tests used to pin the env var, so any *other* test
    that reached config — directly or through ``resolve()`` — read the developer's real
    credential file. That was survivable while the config was read-mostly. It stops being
    survivable once ``load_file()`` migrates shapes: a bug in that path would rewrite a
    real ``~/.config/probe/config.json``, and the tokens in it are not recoverable.

    Autouse and unconditional, so isolation is the default rather than something each new
    test has to remember. Tests that need their own config dir still set the var
    themselves; setting it again inside the test simply wins over this one.

    HOME is redirected too, and the post-condition is asserted on the way out: the env
    var is only half the resolution (``config_path()`` falls back to ``Path.home()``),
    so a test that does ``monkeypatch.delenv("XDG_CONFIG_HOME")`` — a pattern that
    already exists in this repo — would silently re-point every write at the real
    credential file. The assert turns that into a failed test instead.

    The check is "did it escape to the REAL home", not "is it under my tmp dir":
    plenty of tests legitimately point at a tmp dir of their own choosing, and only
    reaching the developer's actual credentials is the failure worth catching.
    """
    real_home = Path.home().resolve()
    root = tmp_path_factory.mktemp("xdg-config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.setenv("HOME", str(root))
    yield
    from probe.sdk.config import config_path

    assert real_home not in config_path().resolve().parents, (
        f"config isolation was defeated: {config_path()} is inside the real home"
    )


_RUN_METRICS = re.compile(r"^/v1/runs/([^/]+)/metrics$")
_RUN_METRICS_GROUPED = re.compile(r"^/v1/runs/([^/]+)/metrics/grouped$")
_RUN_METRICS_WIDE = re.compile(r"^/v1/runs/([^/]+)/metrics/wide$")
_RUN_METRICS_EXPORT = re.compile(r"^/v1/runs/([^/]+)/metrics/export$")
_RUN_VIEWS = re.compile(r"^/v1/runs/([^/]+)/views$")
_RUN_VIEWS_PREVIEW = re.compile(r"^/v1/runs/([^/]+)/views/preview$")
_RUN_VIEW_DATA = re.compile(r"^/v1/runs/([^/]+)/views/([^/]+)/data$")
_VIEW = re.compile(r"^/v1/views/([^/]+)$")
_RUN_COORDINATES = re.compile(r"^/v1/runs/([^/]+)/coordinates$")
_RUN_SPANS = re.compile(r"^/v1/runs/([^/]+)/spans$")
_RUN_STEPS = re.compile(r"^/v1/runs/([^/]+)/steps$")
_RUN_SERIES = re.compile(r"^/v1/runs/([^/]+)/series$")
_RUN_ARTIFACTS = re.compile(r"^/v1/runs/([^/]+)/artifacts$")
_RUN_REOPEN = re.compile(r"^/v1/runs/([^/]+)/reopen$")
_RUN_BUNDLE = re.compile(r"^/v1/runs/([^/]+)/bundle$")
_RUN_LINEAGE = re.compile(r"^/v1/runs/([^/]+)/lineage$")
_RUN_ITEM = re.compile(r"^/v1/runs/([^/]+)$")
_EXP_RUNS = re.compile(r"^/v1/experiments/([^/]+)/runs$")
_PROJ_RUNS = re.compile(r"^/v1/projects/([^/]+)/runs$")
_EXP_ITEM = re.compile(r"^/v1/experiments/([^/]+)$")
_PROJ_ITEM = re.compile(r"^/v1/projects/([^/]+)$")
_WS_ITEM = re.compile(r"^/v1/workspaces/([^/]+)$")
_WS_FILES = re.compile(r"^/v1/workspaces/([^/]+)/files$")
_WS_FILE_UPLOADS = re.compile(r"^/v1/workspaces/([^/]+)/files/uploads$")
_PROJ_ARTIFACTS = re.compile(r"^/v1/projects/([^/]+)/artifacts$")
_PROJ_ARTIFACT_UPLOADS = re.compile(r"^/v1/projects/([^/]+)/artifacts/uploads$")
_EXP_ARTIFACTS = re.compile(r"^/v1/experiments/([^/]+)/artifacts$")
_EXP_ARTIFACT_UPLOADS = re.compile(r"^/v1/experiments/([^/]+)/artifacts/uploads$")
_SHARED_FILE_ITEM = re.compile(r"^/v1/shared/files/([^/]+)$")
_SHARED_FILE_SUB = re.compile(r"^/v1/shared/files/([^/]+)/(confirm|download|unshare)$")
_WS_FILE_SHARE = re.compile(r"^/v1/workspace-files/([^/]+)/share$")

#: Must match the user_id the fake's /v1/me reports, or "mine" never resolves.
_ME = "00000000-0000-0000-0000-000000000001"
_WS_MINE = "11111111-1111-1111-1111-111111111111"
_WS_OTHER = "22222222-2222-2222-2222-222222222222"
_T0 = "2026-01-01T00:00:00Z"


def _newest_first(rows: list[dict]) -> list[dict]:
    """`order_by="created_at DESC"`, as app/artifacts/{project,experiment}_router.py do."""
    return sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)


class FakeApp:
    # Set False to model a backend PREDATING server-side project scope: it
    # accepts the unknown `project_id` body field, ignores it, and answers
    # tenant-wide. The echo is the only thing distinguishing the two, so the
    # fake has to be able to be both.
    echoes_project_scope = True
    # Set False to model a backend PREDATING research-os 0094: it accepts the
    # unknown `notes` field on a project PATCH, drops it, and still answers 200.
    stores_project_notes = True
    #: Whether this fake understands `notes_append` (research-os 0.117.0.0).
    #: False plays a backend that takes the field, ignores it and answers 200,
    #: which is the failure the client's read-back exists to catch.
    stores_notes_append = True
    # Set False to model a backend PREDATING research-os 0096: it accepts the
    # unknown `notes` field on run and group create/PATCH, drops it, and still
    # answers 2xx -- so the row comes back with NO `notes` key at all, which is
    # what the SDK's silent-drop warning keys on.
    stores_entity_notes = True
    # Set False to model a backend PREDATING project-direct runs (0054): the
    # /v1/projects/{id}/runs route 404s FastAPI-style, GET /v1/runs ignores the
    # project_id/direct params, and run rows carry NO project_id field — the
    # exact shapes the SDK's old-backend guards key on.
    supports_project_direct = True
    # Set False to model a backend PREDATING run reopen (research-os#364): the
    # /v1/runs/{id}/reopen route 404s FastAPI-style.
    supports_reopen = True
    # Set False to model a backend PREDATING the team wiki (research-os 0098):
    # every /v1/wiki route 404s FastAPI-style. The browse half of that backend is
    # modelled by `browse_response` simply carrying no `wiki` key, which is what a
    # server that has never heard of the field sends -- and which a client
    # generated AFTER the field exists must still read without raising.
    supports_wiki = True

    def _echo_scope(self, response: dict, body: dict | None) -> dict:
        if not self.echoes_project_scope or not body or not body.get("project_id"):
            return response
        return {**response, "project_id": body["project_id"]}

    def _view_envelope(self, spec: dict, *, view_id: str | None, name: str | None = None) -> dict:
        """The shape both `view_data` and `preview_view` return.

        Two stepped points and every disclosure field, always populated —
        `missing_inputs` and `dropped_nonfinite` are how a client learns a curve
        is incomplete, so a fake that omitted them would let a caller that
        ignores them pass.
        """
        return {
            "view_id": view_id,
            "name": name,
            "x_axis": "step",
            "points": [
                {"step_index": 0, "wall_clock": "2026-08-03T00:00:00Z", "value": 1.5},
                {"step_index": 1, "wall_clock": "2026-08-03T00:01:00Z", "value": 1.25},
            ],
            "truncated": False,
            "missing_inputs": [],
            "dropped_nonfinite": 0,
            "echo_spec": spec,
        }

    def _stepped_points(self, rid: str, params, *, keys: list[str] | None = None) -> list[dict]:
        """The stepped points a grouped/wide read draws from: key/kind/step-window
        filtered, wall-clock-only points (no step_index) excluded."""
        rows = [
            r for r in self.metric_points.get(rid, []) if r.get("step_index") is not None
        ]
        if keys:
            rows = [r for r in rows if r.get("key") in keys]
        if params.get("kind") is not None:
            rows = [r for r in rows if r.get("kind") == params["kind"]]
        if params.get("step_from") is not None:
            rows = [r for r in rows if r["step_index"] >= int(params["step_from"])]
        if params.get("step_to") is not None:
            rows = [r for r in rows if r["step_index"] <= int(params["step_to"])]
        return rows

    def _wiki(self, method: str, path: str, body: dict, request) -> httpx.Response | None:
        """The four /v1/wiki routes, including the three status codes that ARE the
        feature (app/wiki/router.py).

        Modelled rather than stubbed, because every interesting client behaviour is
        a reaction to one of them and a fake that always answered 200 would test
        nothing: 428 is what `probe wiki write`'s hidden read-then-write exists to
        avoid, 409 carrying `current_body` is what its conflict report renders, and
        the 200-on-an-empty-team is what stops `wiki read` reporting an empty lab
        as a failure.

        Returns None for a path this does not handle, so an unmatched /v1/wiki/*
        falls through to the handler's catch-all instead of being absorbed here.
        """
        if not self.supports_wiki:
            return httpx.Response(404, json={"detail": "Not Found"})

        if path == "/v1/wiki" and method == "GET":
            return httpx.Response(200, json=dict(self.wiki))

        if path == "/v1/wiki" and method == "PUT":
            if body.get("version") is None:
                # 428, not 422. The request is well-formed; it carries no
                # precondition. Getting this wrong in the fake would let a client
                # that omits the version pass as a validation error.
                return httpx.Response(428, json={"detail": "a wiki write must carry a version"})
            if body["version"] != self.wiki["version"]:
                return httpx.Response(409, json={"detail": {
                    "message": (
                        f"the wiki has moved to version {self.wiki['version']} since "
                        f"you read version {body['version']}."
                    ),
                    "expected_version": body["version"],
                    "current_version": self.wiki["version"],
                    "current_body": self.wiki["body"],
                }})
            return httpx.Response(200, json=self._wiki_commit(body["body"], body.get("summary")))

        if path == "/v1/wiki/versions" and method == "GET":
            params = request.url.params
            limit = int(params.get("limit") or 50)
            if self.wiki_versions_page is not None:
                limit = min(limit, self.wiki_versions_page)
            before = params.get("before_version")
            rows = self.wiki_history
            if before is not None:
                rows = [r for r in rows if r["version"] < int(before)]
            page = rows[:limit]
            last = page[-1]["version"] if page else None
            more = last is not None and last > 1 and len(page) == limit
            return httpx.Response(200, json={
                "versions": page,
                "next_before_version": last if more else None,
            })

        if path == "/v1/wiki/revert" and method == "POST":
            wanted = body.get("version")
            if wanted not in self.wiki_bodies:
                return httpx.Response(404, json={"detail": f"no wiki version {wanted}"})
            # FORWARD, never in place: the older body becomes a NEW version and the
            # ones in between survive. A fake that rewound `version` would let a
            # client that assumed destructive revert pass.
            return httpx.Response(200, json=self._wiki_commit(
                self.wiki_bodies[wanted], f"revert to version {wanted}"
            ))
        return None

    def _wiki_commit(self, body: str, summary: str | None) -> dict:
        """Append a revision and return the new document.

        The BODY is stored beside the history, not on it. `WikiVersionOut` carries
        `size_chars` and no body on purpose -- a 50-row page of 20k documents is a
        megabyte -- so a fake that hung the body off the history row would serve a
        shape the real backend never serves, and a client reading it would pass
        here and find nothing in production.
        """
        version = self.wiki["version"] + 1
        self.wiki = {"body": body, "version": version, "updated_at": self._stamp()}
        self.wiki_bodies[version] = body
        self.wiki_history.insert(0, {
            "version": version,
            "author": "user:00000000-0000-0000-0000-000000000001",
            "summary": summary,
            "created_at": self.wiki["updated_at"],
            "size_chars": len(body),
        })
        return dict(self.wiki)

    def _find_artifact(self, artifact_id: str) -> dict | None:
        """One artifact by id, whatever it hangs off — the fake's echo of the server's
        single anchor-aware confirm/delete core."""
        for rows in self.artifacts.values():
            for row in rows:
                if row.get("id") == artifact_id:
                    return row
        return None

    def _move_artifact(self, artifact_id: str, body: dict) -> "httpx.Response":
        """``POST /v1/artifacts/{id}/move`` — the anchor-move rail.

        Performs a REAL re-anchor (200, the row, id unchanged) so a test can
        assert the artifact actually landed somewhere else rather than that a
        request was merely shaped correctly.

        The refusals are injected through ``artifact_move_error`` instead of being
        reimplemented. Which moves are legal is the engine's rule and it is
        currently MOVING (lateral project->project is landing), so a fake that
        encoded today's answer would fail the CLI for tomorrow's, and a fake that
        guessed would certify the guess. What the CLI owes on that path is only to
        relay the server's own words, and that is what these tests check.
        """
        if self.artifact_move_error is not None:
            status, detail = self.artifact_move_error
            return httpx.Response(status, json={"detail": detail})
        row = self._find_artifact(artifact_id)
        if row is None:
            return httpx.Response(404, json={"detail": "artifact not found"})
        level = (body or {}).get("level")
        if level not in ("run", "experiment", "project"):
            return httpx.Response(422, json={"detail": f"unknown level {level!r}"})
        target_id = (body or {}).get("target_id")
        if target_id is None:
            # A promote derives its destination from the artifact's own chain.
            target_id = row.get(f"{level}_id")
            if target_id is None:
                return httpx.Response(
                    422,
                    json={"detail": f"no {level} on this artifact's chain to promote to"},
                )
        for rows in self.artifacts.values():
            if row in rows:
                rows.remove(row)
        for other in ("run", "experiment", "project"):
            row.pop(f"{other}_id", None)
        row[f"{level}_id"] = target_id
        self.moves.append(
            {"artifact_id": artifact_id, "level": level, "target_id": target_id}
        )
        key = target_id if level == "run" else f"{level}:{target_id}"
        self.artifacts.setdefault(key, []).append(row)
        return httpx.Response(200, json=row)

    def _presign(self, anchor: str, anchor_id: str, body: dict):
        """The shared presign leg for the non-run anchors.

        `have` (the server already holds these bytes) means no PUT. For a file anchor
        the swap to live also already happened, so the row comes back complete.
        """
        content_hash = body["content_hash"]
        artifact_id = str(uuid.uuid4())
        have = content_hash in self.uploaded
        row = {
            "id": artifact_id,
            "name": body["name"],
            "content_hash": content_hash,
            "size_bytes": body.get("size_bytes"),
            "content_type": body.get("content_type"),
            "status": "complete" if have else "pending",
            "is_reference": False,
            "created_at": self._stamp(),
            f"{anchor}_id": anchor_id,
        }
        self.artifacts.setdefault(f"{anchor}:{anchor_id}", []).append(row)
        return httpx.Response(201, json={
            "artifact_id": artifact_id,
            "have": have,
            "upload_url": None if have else f"http://r2.test/put/{artifact_id}",
            "key": f"lab-42/{artifact_id}",
            "upload_headers": getattr(self, "upload_headers", {}),
        })

    def seed_series(self, run_id: str, key: str, points: dict[int, float], *, kind: str = "model") -> None:
        """Give a run a metric series that POST /v1/series/query will return.

        `points` is {step_index: value}; runs deliberately need not share steps,
        because differing length is usually the thing a comparison is about."""
        self.series_points.setdefault(str(run_id), []).append(
            {
                "run_id": str(run_id),
                "key": key,
                "kind": kind,
                "x_axis": "step",
                "dimensions": {},
                "points": [
                    {"step_index": step, "value": value, "wall_clock": "2026-07-27T00:00:00Z"}
                    for step, value in sorted(points.items())
                ],
            }
        )

    def seed_experiment(self, slug: str, *, project_id: str | None = None) -> dict:
        """Put an experiment straight into the fake, no HTTP.

        `client.run()` resolves its parents instead of creating them, so tests
        that are about something ELSE (auth, spans, artifacts) need one to exist
        without going through the create path or its preconditions."""
        eid = str(uuid.uuid4())
        row = {
            "id": eid, "customer_id": "lab-42", "slug": slug, "name": slug,
            "hypothesis": "h", "project_id": project_id,
            "created_at": _T0,
        }
        self.experiments[eid] = row
        return row

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.runs: dict[str, dict] = {}
        self.run_heartbeats: dict[str, int] = {}
        self.experiments: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        # Two personal workspaces so "mine first" ordering and the not-mine display
        # branch are both exercisable. `owner_user_id=None` + kind="shared" is the
        # legacy retired row that must still deserialize — see WorkspaceKind.
        self.workspaces: dict[str, dict] = {
            _WS_MINE: {
                "id": _WS_MINE, "customer_id": "lab-42", "name": "Mine",
                "slug": "mine", "kind": "personal", "owner_user_id": _ME,
                "project_count": 0, "created_at": _T0,
            },
            _WS_OTHER: {
                "id": _WS_OTHER, "customer_id": "lab-42", "name": "Teammate",
                "slug": "teammate", "kind": "personal", "owner_user_id": "user-other",
                "project_count": 0, "created_at": _T0,
            },
        }
        # Artifacts are keyed by an anchor key ("run:<id>", "project:<id>", ...) so the
        # one confirm handler can find a row whatever it hangs off, exactly like the
        # server's single confirm core.
        self.artifacts: dict[str, list[dict]] = {}
        # Every accepted `POST /v1/artifacts/{id}/move`, and the refusal a test can
        # make the next one answer with. See _move_artifact.
        self.moves: list[dict] = []
        self.artifact_move_error: tuple[int, object] | None = None
        # Artifact rows are STAMPED and the anchored listings come back newest
        # first, like the real routers (`order_by="created_at DESC"`). The fake
        # used to omit created_at entirely and answer in insertion order -- the
        # exact opposite -- so 'which row is current?' was backwards here and
        # right in production.
        # Re-index fan-outs triggered by a project move, so a test can assert the
        # descendant reprojection actually fired.
        self.reindexed: list[str] = []
        self.tokens: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.series: dict[str, list[dict]] = {}
        #: run_id -> SeriesResult rows, served by POST /v1/series/query (compare()).
        self.series_points: dict[str, list[dict]] = {}
        self.series_queries: list[dict] = []
        self.metric_points: dict[str, list[dict]] = {}
        # Points as POSTED (coords/labels/span_id included), keyed by run id.
        # Separate from `metric_points`, which read-path tests seed by hand;
        # write-path tests assert on the captured wire payloads here.
        self.metric_points_posted: dict[str, list[dict]] = {}
        # Coordinate catalog rows (0060), seeded by hand like `metric_points`.
        self.coordinates: dict[str, list[dict]] = {}
        # 0062 per-key declared reduce fns, as a seed knob. The grouped handler
        # also honors `agg` fields on POSTED points, so the write-side
        # declaration is exercisable end to end.
        self.declared_aggs: dict[str, str] = {}
        # Server-side per-page ceilings BELOW the requested max_rows, so the
        # client's next_step/paging loops have something real to follow.
        self.grouped_page_rows: int | None = None
        self.wide_page_rows: int | None = None
        self.spans: dict[str, list[dict]] = {}
        #: run_id -> {step_index: step record}. Where log() puts non-numeric values.
        self.steps: dict[str, dict[int, dict]] = {}
        self.artifact_versions: dict[str, list[dict]] = {}
        self.edges: list[dict] = []
        self.execution_records: dict[str, dict] = {}
        self.experiment_versions: dict[str, list[dict]] = {}
        self.run_events: dict[str, list[dict]] = {}
        self.uploaded: set[str] = set()
        self.puts: list[str] = []
        self.put_headers: list[dict[str, str]] = []
        self.gets: list[str] = []
        self.metrics_inserted = 0
        # WHOLE batch bodies, not just points: origin/provenance are batch-level
        # (0087), so a test asserting a derived write has to see the envelope.
        self.metric_batches_posted: list[dict] = []
        self.views: dict[str, list[dict]] = {}
        self.deleted_series: list[dict] = []
        self.spans_upserted = 0
        self.spans: dict[str, list[dict]] = {}
        self.blobs: dict[str, bytes] = {}
        # test knobs
        self.experiment_conflict_id: str | None = None
        self.fail_next_metrics = False
        # /v1/search (workspaces+kb fold-in): None = a backend that predates the
        # endpoint (404); a dict is returned verbatim. Bodies are captured either way.
        # search_responses (a queue, popped per request) takes precedence over
        # search_response; search_404_once simulates one stale pod mid-deploy.
        self.search_response: dict | None = None
        self.search_responses: list[dict] = []
        self.search_requests: list[dict] = []
        # GET /v1/browse. `browse_response = None` models a backend that predates
        # the route, so the source's capability probe has something to discover.
        self.browse_requests: list[dict] = []
        self.browse_response: dict | None = None
        # The team wiki (research-os 0098). ONE document per tenant, so one dict --
        # no id anywhere, exactly like the real routes. `version: 0` with an empty
        # body is the REAL initial state, not a stand-in for "unset": the server
        # answers exactly this for a team that has never generated one, and a fake
        # that 404'd instead would let a client with the missing-means-empty bug
        # pass every test here.
        self.wiki: dict = {"body": "", "version": 0, "updated_at": None}
        #: Newest-first history rows (version/author/summary/created_at/size_chars).
        self.wiki_history: list[dict] = []
        #: version -> body, kept OFF the history rows -- see `_wiki_commit`. This is
        #: what `revert` copies forward, and it is deliberately unreachable through
        #: any route: the wire has no way to fetch an old body except by reverting.
        self.wiki_bodies: dict[int, str] = {}
        #: Server-side page ceiling for GET /v1/wiki/versions, so the client's
        #: "there is older history" signal has something real to key on.
        self.wiki_versions_page: int | None = None
        self.search_404_workspace_ids: set[str] = set()
        self.search_404_once = False
        self.fail_next_uploads = False
        self._ts = 0
        # /v1/me reports the *token's* scopes, not the principal's: a read-only PAT
        # answers ["read"] even when its owner is an owner.
        self.me_scopes: list[str] = ["read", "write", "delete", "admin"]
        self.me_status = 200

    def _stamp(self) -> str:
        """A fresh, monotonically increasing timestamp per call. Distinct values let a
        test that pins ordering actually catch a wrong re-stamp."""
        self._ts += 1
        return f"2026-07-15T00:00:{self._ts:02d}Z"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method
        path = request.url.path
        # Per-path fault injection. Set `app.fail_paths = {"/v1/..."}` to make ONE
        # route 500 while the rest of the fake stays healthy -- the shape you need to
        # test that a degraded dependency is REPORTED rather than propagated.
        if path in getattr(self, "fail_paths", ()):
            return httpx.Response(500, json={"detail": "injected failure"})
        try:
            body = json.loads(request.content) if request.content else {}
        except (json.JSONDecodeError, ValueError):
            body = {}  # e.g. a raw-bytes PUT to a presigned URL

        if path == "/v1/me" and method == "GET":
            if self.me_status != 200:
                return httpx.Response(self.me_status, json={"error": "invalid_token"})
            return httpx.Response(200, json={
                "user_id": "00000000-0000-0000-0000-000000000001",
                "email": "dev@example.com", "name": "Dev",
                "customer_id": "lab-42", "role": "owner",
                "scopes": list(self.me_scopes), "via": "token",
            })

        if path == "/v1/tokens/current" and method == "DELETE":
            return httpx.Response(204)

        if path == "/v1/browse" and method == "GET":
            self.browse_requests.append(dict(request.url.params))
            if self.browse_response is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json=self.browse_response)

        if path.startswith("/v1/wiki"):
            response = self._wiki(method, path, body, request)
            if response is not None:
                return response

        if path == "/v1/search" and method == "POST":
            self.search_requests.append(body)
            if self.search_404_once:
                self.search_404_once = False
                return httpx.Response(404, json={"detail": "Not Found"})
            if body.get("workspace_id") in self.search_404_workspace_ids:
                return httpx.Response(404, json={"detail": "not found"})
            if self.search_responses:
                return httpx.Response(200, json=self._echo_scope(self.search_responses.pop(0), body))
            if self.search_response is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json=self._echo_scope(self.search_response, body))
        # -- tokens (mint is session-only, so it is NOT routed here: the CLI mints
        # via the device flow, which tests/test_device_login.py covers) --
        if path == "/v1/tokens" and method == "GET":
            return httpx.Response(200, json=list(self.tokens.values()))
        m = re.match(r"^/v1/tokens/([^/]+)$", path)
        if m and method == "DELETE":
            tid = m.group(1)
            if tid not in self.tokens:
                return httpx.Response(404, json={"detail": "token not found"})
            self.tokens.pop(tid)
            return httpx.Response(204)

        if path == "/v1/projects" and method == "POST":
            existing = next(
                (row for row in self.projects.values() if row["slug"] == body["slug"]),
                None,
            )
            if existing:
                return httpx.Response(
                    409,
                    json={
                        "detail": {
                            "message": "slug exists",
                            "existing_id": existing["id"],
                        }
                    },
                )
            pid = str(uuid.uuid4())
            row = {
                "id": pid,
                "slug": body["slug"],
                "name": body.get("name", body["slug"]),
                "customer_id": "lab-42",
                # Present-but-nullable, exactly like ProjectOut: it IS in `required`
                # and typed anyOf[uuid, null], because legacy rows predate workspaces.
                "workspace_id": body.get("workspace_id"),
                "description": body.get("description"),
                "metadata": body.get("metadata") or {},
                "created_at": _T0,
            }
            self.projects[pid] = row
            return httpx.Response(201, json=row)

        if path == "/v1/projects" and method == "GET":
            rows = list(self.projects.values())
            wanted = request.url.params.get("workspace_id")
            if wanted:
                rows = [r for r in rows if r.get("workspace_id") == wanted]
            # Exact slug lookup, mirroring the engine: this is what lets a client
            # RESOLVE a slug without being able to create one.
            slug = request.url.params.get("slug")
            if slug:
                rows = [r for r in rows if r.get("slug") == slug]
            # Exact, case-insensitive -- mirrors the engine's ?name= filter. A fake
            # that ignored it would make every `name:` assertion pass against an
            # unfiltered page, which is the drop the client guards against.
            name = request.url.params.get("name")
            if name:
                rows = [r for r in rows if str(r.get("name", "")).lower() == name.lower()]
            return httpx.Response(200, json=rows)

        m = _PROJ_ITEM.match(path)
        if m and method == "DELETE":
            pid = m.group(1)
            if pid not in self.projects:
                return httpx.Response(404, json={"detail": "not found"})
            # Mirrors the engine cascade (0080): the tree goes with the project.
            doomed = {
                eid for eid, e in self.experiments.items() if e.get("project_id") == pid
            }
            for rid in [
                rid
                for rid, r in self.runs.items()
                if r.get("project_id") == pid or r.get("experiment_id") in doomed
            ]:
                self.runs.pop(rid)
            for eid in doomed:
                self.experiments.pop(eid)
            self.projects.pop(pid)
            return httpx.Response(204)

        m = _PROJ_ITEM.match(path)
        if m and method == "GET":
            pid = m.group(1)
            if pid not in self.projects:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=self.projects[pid])

        m = _PROJ_ITEM.match(path)
        if m and method == "PATCH":
            pid = m.group(1)
            if pid not in self.projects:
                return httpx.Response(404, json={"detail": "not found"})
            row = self.projects[pid]
            if "workspace_id" in body:
                dest = body["workspace_id"]
                if dest not in self.workspaces:
                    # A rejected VALUE, not a missing resource: 422, never 404.
                    return httpx.Response(422, json={"detail": "unknown workspace"})
                # Fan out to descendants ONLY when the workspace actually changes —
                # their documents denormalize workspace_id, so they must reproject.
                if row.get("workspace_id") != dest:
                    row["workspace_id"] = dest
                    for eid, exp in self.experiments.items():
                        if exp.get("project_id") == pid:
                            self.reindexed.append(eid)
                    for rid, run in self.runs.items():
                        if run.get("project_id") == pid:
                            self.reindexed.append(rid)
            for field in ("name", "description", "metadata"):
                if body.get(field) is not None:
                    row[field] = body[field]
            # `notes` is gated so the fake can be a backend on EITHER side of
            # research-os 0094. ProjectPatch does not forbid extra fields, so a
            # pre-0094 server takes `notes`, ignores it, and answers 200 -- the write
            # vanishes and the caller is told it worked. Being able to be both is the
            # only way to test that the client notices (same shape as
            # `echoes_project_scope`).
            if self.stores_project_notes and body.get("notes") is not None:
                row["notes"] = body["notes"]
            # `notes_append` EXTENDS server-side, deriving from the stored value
            # rather than one the client read earlier -- that derivation is the
            # whole mechanism, so the fake has to do it here and not accept a
            # pre-concatenated string. Gated alongside `notes` so the fake can
            # still play a backend that predates the field, which is how the
            # client's "the server ignored it" guard gets tested.
            if self.stores_notes_append and body.get("notes_append") is not None:
                current = row.get("notes") or ""
                # Tops the document up to a BLANK LINE, matching research-os
                # 0.117.0.0. Notes are one markdown document, and two paragraphs
                # joined by a single newline render as one paragraph -- so a fake
                # that used \n would let a test pass on prose the real server
                # would render wrong.
                if not current:
                    sep = ""
                elif current.endswith("\n\n"):
                    sep = ""
                elif current.endswith("\n"):
                    sep = "\n"
                else:
                    sep = "\n\n"
                row["notes"] = current + sep + body["notes_append"]
            return httpx.Response(200, json=row)

        # -- workspaces --
        if path == "/v1/workspaces" and method == "GET":
            # Server order: the caller's own first, then everyone else's alphabetically.
            # A retired null-owner row sorts as "not mine" and never first.
            rows = sorted(
                self.workspaces.values(),
                key=lambda w: (w.get("owner_user_id") != _ME, w.get("name") or ""),
            )
            for row in rows:
                row["project_count"] = sum(
                    1 for p in self.projects.values() if p.get("workspace_id") == row["id"]
                )
            return httpx.Response(200, json=rows)

        m = _WS_ITEM.match(path)
        if m and method == "GET":
            wid = m.group(1)
            if wid not in self.workspaces:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=self.workspaces[wid])

        m = _WS_ITEM.match(path)
        if m and method == "PATCH":
            wid = m.group(1)
            if wid not in self.workspaces:
                return httpx.Response(404, json={"detail": "not found"})
            name = (body.get("name") or "").strip()
            if not name:
                return httpx.Response(422, json={"detail": "name must not be blank"})
            self.workspaces[wid]["name"] = name
            return httpx.Response(200, json=self.workspaces[wid])

        if path == "/v1/experiments" and method == "POST":
            if not (body or {}).get("project_id"):
                return httpx.Response(
                    422, json={"detail": [{"loc": ["body", "project_id"], "type": "missing"}]}
                )
            # Mirror the projects handler and the real UNIQUE (customer_id, slug):
            # without this the fake happily mints duplicate identities, so
            # `create_experiment` never conflicts and a regression that put the
            # POST-then-swallow-409 back inside run() would leave the suite green.
            for _row in self.experiments.values():
                if _row.get("slug") == (body or {}).get("slug"):
                    return httpx.Response(409, json={"detail": {
                        "message": "experiment with this slug already exists",
                        "existing_id": _row["id"]}})
            if self.experiment_conflict_id:
                return httpx.Response(
                    409,
                    json={"detail": {"message": "slug exists", "existing_id": self.experiment_conflict_id}},
                )
            eid = str(uuid.uuid4())
            row = {
                "id": eid,
                "slug": body["slug"],
                "name": body["name"],
                "hypothesis": body["hypothesis"],
                "project_id": body.get("project_id") or str(uuid.uuid4()),
                "customer_id": "lab-42",
                "created_at": self._stamp(),
            }
            self.experiments[eid] = row
            return httpx.Response(201, json=row)

        if path == "/v1/experiments" and method == "GET":
            rows = list(self.experiments.values())
            project_id = request.url.params.get("project_id")
            if project_id:
                rows = [row for row in rows if row.get("project_id") == project_id]
            # Exact slug lookup, mirroring the engine (see the projects handler).
            slug = request.url.params.get("slug")
            if slug:
                rows = [row for row in rows if row.get("slug") == slug]
            # Exact, case-insensitive -- mirrors the engine's ?name= filter. A fake
            # that ignored it would make every `name:` assertion pass against an
            # unfiltered page, which is the drop the client guards against.
            name = request.url.params.get("name")
            if name:
                rows = [row for row in rows if str(row.get("name", "")).lower() == name.lower()]
            return httpx.Response(200, json=rows)

        if path == "/v1/runs" and method == "GET":
            rows = list(self.runs.values())
            # Exact, case-insensitive -- mirrors the engine's ?name= filter.
            name = request.url.params.get("name")
            if name:
                rows = [row for row in rows if str(row.get("name", "")).lower() == name.lower()]
            experiment_id = request.url.params.get("experiment_id")
            if experiment_id:
                rows = [row for row in rows if row.get("experiment_id") == experiment_id]
            if self.supports_project_direct:
                # project_id (0054): ALL of a project's runs — direct AND attached.
                project_id = request.url.params.get("project_id")
                if project_id:
                    rows = [row for row in rows if row.get("project_id") == project_id]
                if request.url.params.get("direct") == "true":
                    rows = [row for row in rows if not row.get("experiment_id")]
            else:
                # Pre-0054: unknown params are ignored, rows have no project_id.
                rows = [
                    {k: v for k, v in row.items() if k != "project_id"} for row in rows
                ]
            # Page it, like the real endpoint: limit defaults to 50 and caps at
            # 200 (schema/openapi.json), with an opaque cursor. Serving every row
            # regardless made any "does the client paginate?" test vacuous — a
            # client that read one page and stopped passed identically.
            limit = min(int(request.url.params.get("limit") or 50), 200)
            start = int(request.url.params.get("cursor") or 0)
            window = rows[start : start + limit]
            headers = {}
            if start + limit < len(rows):
                headers["x-next-cursor"] = str(start + limit)
            return httpx.Response(200, json=window, headers=headers)

        m = _EXP_ITEM.match(path)
        if m and method == "GET":
            eid = m.group(1)
            return httpx.Response(200, json=self.experiments.get(eid, {"id": eid, "hypothesis": "h", "project_id": str(uuid.uuid4())}))
        if m and method == "PATCH":
            eid = m.group(1)
            row = self.experiments.get(eid)
            if row is None:
                return httpx.Response(404, json={"detail": "not found"})
            row.update(body)
            return httpx.Response(200, json=row)

        m = _RUN_REOPEN.match(path)
        if m and method == "POST":
            if not self.supports_reopen:
                # Pre-#364: the route does not exist — FastAPI's route-level
                # 404, NOT the handler's "run not found".
                return httpx.Response(404, json={"detail": "Not Found"})
            row = self.runs.get(m.group(1))
            if row is None:
                return httpx.Response(404, json={"detail": "run not found"})
            if row["status"] not in ("failed", "crashed", "canceled"):
                return httpx.Response(409, json={"detail": {
                    "message": f"run is {row['status']}; only a dead run reopens"}})
            stored = self.metric_points.get(row["id"], []) + \
                self.metric_points_posted.get(row["id"], [])
            steps = [p["step_index"] for p in stored
                     if p.get("step_index") is not None]
            last_step = max(steps) if steps else None
            prior_status = row["status"]
            row["status"] = "running"
            row["ended_at"] = None
            row["write_epoch"] = row.get("write_epoch", 1) + 1
            row["current_session_id"] = (body or {}).get("session_id")
            row.setdefault("recoveries", []).append({
                "at": self._stamp(),
                "prior_status": prior_status,
                "last_step": last_step,
                "session_id": row["current_session_id"],
            })
            return httpx.Response(200, json={
                "run": row,
                "write_epoch": row["write_epoch"],
                "last_step": last_step,
            })

        m = _EXP_RUNS.match(path)
        if m and method == "POST":
            conflict = self._run_external_id_conflict(body)
            if conflict is not None:
                return conflict
            rid = str(uuid.uuid4())
            eid = m.group(1)
            # Mirror the engine: an attached run inherits ITS EXPERIMENT'S
            # project (0054).
            row = self._new_run(
                rid,
                eid,
                body,
                project_id=(self.experiments.get(eid) or {}).get("project_id"),
            )
            return httpx.Response(201, json=row)

        m = _PROJ_RUNS.match(path)
        if m and method == "POST":
            if not self.supports_project_direct:
                # Pre-0054: the route does not exist — FastAPI's route-level 404,
                # NOT the handler's "project not found".
                return httpx.Response(404, json={"detail": "Not Found"})
            # PROJECT-DIRECT run (0054): no experiment; group_id is rejected
            # like the engine does (run groups are experiment-anchored), and an
            # unknown project is the handler's oracle-safe 404.
            if m.group(1) not in self.projects:
                return httpx.Response(404, json={"detail": "project not found"})
            if body.get("group_id") is not None:
                return httpx.Response(
                    422, json={"detail": "group_id requires an experiment-attached run"}
                )
            conflict = self._run_external_id_conflict(body)
            if conflict is not None:
                return conflict
            rid = str(uuid.uuid4())
            row = self._new_run(rid, None, body, project_id=m.group(1))
            return httpx.Response(201, json=row)

        m = _RUN_METRICS.match(path)
        if m and method == "POST":
            if self.fail_next_metrics:
                self.fail_next_metrics = False
                return httpx.Response(503, json={"detail": "db down"})
            points = body.get("points", [])
            if body.get("origin") == "derived" and not body.get("provenance"):
                return httpx.Response(
                    422, json={"detail": "derived batch requires provenance"}
                )
            self.metrics_inserted += len(points)
            self.metric_batches_posted.append(body)
            self.metric_points_posted.setdefault(m.group(1), []).extend(points)
            return httpx.Response(200, json={"inserted": len(points)})
        if m and method == "GET":
            rows = self.metric_points.get(m.group(1), [])
            key = request.url.params.get("key")
            kind = request.url.params.get("kind")
            limit = request.url.params.get("limit")
            if key is not None:
                rows = [r for r in rows if r.get("key") == key]
            if kind is not None:
                rows = [r for r in rows if r.get("kind") == kind]
            return httpx.Response(200, json=rows[: int(limit)] if limit else rows)

        # -- expression views (0088). Storage + identity only: EVALUATION is the
        # server's job and research-os tests it. What matters here is that the
        # client reaches every route with the right body and reads the envelope.
        m = _RUN_VIEWS_PREVIEW.match(path)
        if m and method == "POST":
            return httpx.Response(200, json=self._view_envelope(body["spec"], view_id=None))

        m = _RUN_VIEWS.match(path)
        if m and method == "POST":
            rid = m.group(1)
            rows = self.views.setdefault(rid, [])
            if any(r["name"] == body["name"] for r in rows):
                return httpx.Response(409, json={"detail": "view name already in use"})
            row = {
                "id": str(uuid.uuid4()),
                "run_id": rid,
                "name": body["name"],
                "spec": body["spec"],
                "created_by": "user:test",
                "created_at": "2026-08-03T00:00:00Z",
                "updated_at": "2026-08-03T00:00:00Z",
            }
            rows.append(row)
            return httpx.Response(201, json=row)
        if m and method == "GET":
            return httpx.Response(200, json=self.views.get(m.group(1), []))

        m = _RUN_VIEW_DATA.match(path)
        if m and method == "GET":
            rid, vid = m.group(1), m.group(2)
            row = next((r for r in self.views.get(rid, []) if r["id"] == vid), None)
            if row is None:
                return httpx.Response(404, json={"detail": "view not found"})
            return httpx.Response(
                200, json=self._view_envelope(row["spec"], view_id=vid, name=row["name"])
            )

        m = _VIEW.match(path)
        if m:
            vid = m.group(1)
            for rid, rows in self.views.items():
                for i, row in enumerate(rows):
                    if row["id"] != vid:
                        continue
                    if method == "PATCH":
                        updated = {**row, **{k: v for k, v in body.items() if v is not None}}
                        updated["updated_at"] = "2026-08-03T01:00:00Z"
                        rows[i] = updated
                        return httpx.Response(200, json=updated)
                    if method == "DELETE":
                        rows.pop(i)
                        return httpx.Response(204)
            return httpx.Response(404, json={"detail": "view not found"})

        # -- coordinate reads (0059-0062): grouped / wide / export / catalog --
        m = _RUN_METRICS_GROUPED.match(path)
        if m and method == "GET":
            rid = m.group(1)
            p = request.url.params
            key = p.get("key")
            rows = self._stepped_points(rid, p, keys=[key] if key else None)
            agg = p.get("agg")
            if agg is None:
                # 0062: omitted agg resolves to the key's DECLARED reduce fn
                # (else mean); conflicting declarations are a 422, mirroring
                # the server. Declarations arrive on posted points or the
                # `declared_aggs` seed knob.
                declared = {
                    q["agg"]
                    for q in self.metric_points_posted.get(rid, [])
                    if q.get("key") == key and q.get("agg")
                }
                if key in self.declared_aggs:
                    declared.add(self.declared_aggs[key])
                if len(declared) > 1:
                    return httpx.Response(
                        422, json={"detail": f"conflicting agg declarations for {key!r}"}
                    )
                agg = declared.pop() if declared else "mean"
            by = [axis for axis in (p.get("by") or "").split(",") if axis]
            where = json.loads(p["where"]) if "where" in p else None
            if where:
                rows = [
                    r for r in rows
                    if all((r.get("dimensions") or {}).get(k) == v for k, v in where.items())
                ]
            bucket = int(p.get("step_bucket") or 1)
            max_rows = int(p.get("max_rows") or 10000)
            if self.grouped_page_rows:
                max_rows = min(max_rows, self.grouped_page_rows)
            cells: dict[tuple, list[float]] = {}
            for r in rows:
                b = (r["step_index"] // bucket) * bucket
                group = tuple(
                    (axis, (r.get("dimensions") or {}).get(axis)) for axis in by
                )
                cells.setdefault((b, group), []).append(r["value"])
            fns = {
                "mean": lambda v: sum(v) / len(v),
                "sum": sum, "min": min, "max": max, "count": len,
            }
            groups: list[dict] = []
            truncated, next_step = False, None
            for (b, group), values in sorted(
                cells.items(), key=lambda cell: (cell[0][0], str(cell[0][1]))
            ):
                # Cut only at a bucket boundary, so next_step is a clean re-entry.
                if len(groups) >= max_rows and b != groups[-1]["step_index"]:
                    truncated, next_step = True, b
                    break
                groups.append({
                    "step_index": b,
                    # Group labels are each axis value's JSON text (int 1 vs "1").
                    "group": {
                        axis: (None if v is None else json.dumps(v)) for axis, v in group
                    } or None,
                    "value": float(fns[agg](values)),
                    "n": len(values),
                })
            return httpx.Response(200, json={
                "key": key, "kind": p.get("kind"), "agg": agg, "by": by or None,
                "where": where, "step_bucket": bucket, "groups": groups,
                "truncated": truncated, "next_step": next_step,
            })

        m = _RUN_METRICS_WIDE.match(path)
        if m and method == "GET":
            p = request.url.params
            rows = self._stepped_points(m.group(1), p, keys=p.get_list("key") or None)
            steps = sorted({r["step_index"] for r in rows})
            max_rows = int(p.get("max_rows") or 10000)
            if self.wide_page_rows:
                max_rows = min(max_rows, self.wide_page_rows)
            truncated = len(steps) > max_rows
            next_step = steps[max_rows] if truncated else None
            steps = steps[:max_rows]
            # Columns cover THIS page's window only (a series with no point in
            # the emitted steps is absent from `columns`), so a paging client
            # must realign by series identity, never trust positions.
            window = set(steps)
            rows = [r for r in rows if r["step_index"] in window]
            idents = sorted({
                (r.get("key"), r.get("kind"),
                 json.dumps(r.get("dimensions") or {}, sort_keys=True))
                for r in rows
            })
            values = {
                (r["step_index"],
                 (r.get("key"), r.get("kind"),
                  json.dumps(r.get("dimensions") or {}, sort_keys=True))): r["value"]
                for r in rows
            }
            return httpx.Response(200, json={
                "columns": [
                    {"key": k, "kind": kd, "dimensions": json.loads(d)}
                    for k, kd, d in idents
                ],
                "rows": [
                    {"step_index": s, "values": [values.get((s, c)) for c in idents]}
                    for s in steps
                ],
                "truncated": truncated,
                "next_step": next_step,
            })

        m = _RUN_METRICS_EXPORT.match(path)
        if m and method == "GET":
            p = request.url.params
            rows = sorted(
                self.metric_points.get(m.group(1), []), key=lambda r: r.get("id", 0)
            )
            for param, field in (("key", "key"), ("kind", "kind")):
                if p.get(param) is not None:
                    rows = [r for r in rows if r.get(field) == p[param]]
            if p.get("step_from") is not None:
                rows = [r for r in rows if r.get("step_index") is not None
                        and r["step_index"] >= int(p["step_from"])]
            if p.get("step_to") is not None:
                rows = [r for r in rows if r.get("step_index") is not None
                        and r["step_index"] <= int(p["step_to"])]
            if p.get("after_id") is not None:
                rows = [r for r in rows if r["id"] > int(p["after_id"])]
            limit = int(p.get("limit") or 1000)
            page, rest = rows[:limit], rows[limit:]
            return httpx.Response(200, json={
                "points": page,
                "next_after_id": page[-1]["id"] if rest else None,
            })

        m = _RUN_COORDINATES.match(path)
        if m and method == "GET":
            return httpx.Response(200, json=self.coordinates.get(m.group(1), []))

        if path == "/v1/series/latest" and method == "POST":
            scalars = []
            for rid in body.get("run_ids", []):
                row = self.runs.get(rid)
                # Every run is validated in-tenant AND live before any read.
                if row is None:
                    return httpx.Response(404, json={"detail": "run not found"})
                for s in self.series.get(rid, []):
                    if body.get("keys") and s.get("key") not in body["keys"]:
                        continue
                    if body.get("kind") and s.get("kind") != body["kind"]:
                        continue
                    scalars.append({**s, "run_id": rid})
            return httpx.Response(200, json={"scalars": scalars})

        m = _RUN_SERIES.match(path)
        if m and method == "GET":
            return httpx.Response(200, json=self.series.get(m.group(1), []))
        if m and method == "DELETE":
            # Records the identity triple rather than mutating a catalog: what
            # matters on the client side is that the write identity and the
            # delete identity are the same three fields.
            params = request.url.params
            raw = params.get("dimensions")
            self.deleted_series.append(
                {
                    "run_id": m.group(1),
                    "kind": params.get("kind"),
                    "key": params.get("key"),
                    "dimensions": json.loads(raw) if raw else None,
                }
            )
            return httpx.Response(204)

        if path == "/v1/series/query" and method == "POST":
            # Multi-run series read, backing client.compare(). Serves whatever
            # seed_series() put in `series_points`, filtered the way the real
            # endpoint filters: by run, by (key, kind) selector, and by step.
            self.series_queries.append(body)
            wanted = {(s["key"], s.get("kind", "model")) for s in (body.get("series") or [])}
            step_from, step_to = body.get("step_from"), body.get("step_to")
            out = []
            for rid in body.get("run_ids", []):
                for row in self.series_points.get(str(rid), []):
                    if wanted and (row["key"], row.get("kind", "model")) not in wanted:
                        continue
                    points = [
                        p
                        for p in row["points"]
                        if (step_from is None or p.get("step_index", 0) >= step_from)
                        and (step_to is None or p.get("step_index", 0) <= step_to)
                    ]
                    out.append({**row, "run_id": str(rid), "points": points})
            return httpx.Response(200, json={"series": out})

        m = _RUN_STEPS.match(path)
        if m and method == "POST":
            # StepCreate{step_index, name, attributes, summary} -> SpanOut. The
            # per-step record; log() routes non-numeric values into `attributes`.
            # Upserts on (run, step_index), like the real endpoint.
            rid, index = m.group(1), body["step_index"]
            record = self.steps.setdefault(rid, {}).setdefault(
                index, {"step_index": index, "attributes": {}, "summary": {}}
            )
            record["attributes"].update(body.get("attributes") or {})
            record["summary"].update(body.get("summary") or {})
            if body.get("name") is not None:
                record["name"] = body["name"]
            return httpx.Response(
                201, json={"id": str(uuid.uuid4()), "span_type": "step", **record}
            )

        m = _RUN_SPANS.match(path)
        if m and method == "POST":
            n = len(body.get("spans", []))
            self.spans_upserted += n
            self.spans.setdefault(m.group(1), []).extend(body.get("spans", []))
            return httpx.Response(200, json={"upserted": n})
        if m and method == "GET":
            rows = self.spans.get(m.group(1), [])
            span_type = request.url.params.get("span_type")
            parent = request.url.params.get("parent_span_id")
            step_from = request.url.params.get("step_from")
            step_to = request.url.params.get("step_to")
            limit = request.url.params.get("limit")
            if span_type is not None:
                rows = [r for r in rows if r.get("span_type") == span_type]
            if parent is not None:
                rows = [r for r in rows if r.get("parent_span_id") == parent]
            if step_from is not None:
                rows = [r for r in rows
                        if r.get("step_index") is not None and r["step_index"] >= int(step_from)]
            if step_to is not None:
                rows = [r for r in rows
                        if r.get("step_index") is not None and r["step_index"] <= int(step_to)]
            return httpx.Response(200, json=rows[: int(limit)] if limit else rows)

        m = _RUN_ARTIFACTS.match(path)
        if m and method == "POST":
            row = {"id": str(uuid.uuid4()), **body}
            self.artifacts.setdefault(m.group(1), []).append(row)
            return httpx.Response(201, json=row)
        if m and method == "GET":
            rows = self.artifacts.get(m.group(1), [])
            kind = request.url.params.get("kind")
            step_from = request.url.params.get("step_from")
            step_to = request.url.params.get("step_to")
            if kind is not None:
                rows = [r for r in rows if r.get("kind") == kind.strip().lower()]
            if step_from is not None:
                rows = [r for r in rows if r.get("step_index") is not None
                        and r["step_index"] >= int(step_from)]
            if step_to is not None:
                rows = [r for r in rows if r.get("step_index") is not None
                        and r["step_index"] <= int(step_to)]
            return httpx.Response(200, json=rows)

        m = _RUN_BUNDLE.match(path)
        if m and method == "GET":
            rid = m.group(1)
            artifacts = self.artifacts.get(rid, [])
            return httpx.Response(
                200,
                json={
                    "run": self.runs[rid],
                    "series": [],
                    "artifacts": artifacts,
                    "artifact_total": len(artifacts),
                    "span_types": [],
                    "parent_run_id": self.runs[rid].get("parent_run_id"),
                    "child_run_ids": [],
                },
            )

        m = _RUN_LINEAGE.match(path)
        if m and method == "GET":
            return httpx.Response(200, json={"run_id": m.group(1), "ancestors": [], "descendants": []})

        m = _RUN_ITEM.match(path)
        if m and method == "DELETE":
            rid = m.group(1)
            if self.runs.pop(rid, None) is None:
                return httpx.Response(404, json={"detail": "run not found"})
            return httpx.Response(204)
        if m and method == "GET":
            rid = m.group(1)
            row = self.runs.get(rid)
            if row is None:
                # GET /v1/runs/{run_ref} is the ONE item route whose path param is
                # an untyped string rather than format:uuid -- the server resolves
                # a petname short_id here. Mirror that before the auto-vivify
                # fallback below, or a short_id becomes a brand-new run's id and
                # every by-petname assertion passes against a fiction.
                row = next(
                    (r for r in self.runs.values() if r.get("short_id") == rid), None
                )
            if row is None:
                row = self._new_run(rid, "exp", {"name": "r"})
            return httpx.Response(200, json=row)
        if m and method == "PATCH":
            rid = m.group(1)
            # NB: not setdefault() - _new_run has a side effect (stores the row), so
            # an eager default would clobber the existing run on every PATCH.
            row = self.runs.get(rid) or self._new_run(rid, "exp", {"name": "r"})
            for k, v in body.items():
                if k == "foreign_keys":  # per-key new-wins merge (mirrors the backend)
                    row["foreign_keys"] = {**(row.get("foreign_keys") or {}), **v}
                elif k == "notes" and not self.stores_entity_notes:
                    # Pre-0096: the field is unknown, so it is accepted and dropped
                    # rather than rejected -- the row keeps no `notes` key at all.
                    continue
                else:
                    row[k] = v
            return httpx.Response(200, json=row)

        # -- run groups --
        m = re.match(r"^/v1/experiments/([^/]+)/groups$", path)
        if m and method == "POST":
            eid = m.group(1)
            dup = next(
                (g for g in self.groups.values()
                 if g["experiment_id"] == eid and g["name"] == body["name"]),
                None,
            )
            if dup:
                return httpx.Response(
                    409, json={"detail": {"message": "group name exists", "existing_id": dup["id"]}}
                )
            gid = str(uuid.uuid4())
            row = {"id": gid, "customer_id": "lab-42", "experiment_id": eid,
                   "kind": body.get("kind", "group"), "name": body["name"],
                   "spec": body.get("spec", {}), "created_at": "2026-07-15T00:00:00Z"}
            if self.stores_entity_notes:
                row["notes"] = body.get("notes")
            self.groups[gid] = row
            return httpx.Response(201, json=row)
        if m and method == "GET":
            eid = m.group(1)
            return httpx.Response(
                200, json=[g for g in self.groups.values() if g["experiment_id"] == eid]
            )
        m = re.match(r"^/v1/groups/([^/]+)$", path)
        if m and method in ("GET", "PATCH"):
            gid = m.group(1)
            row = self.groups.get(gid)
            if row is None:
                return httpx.Response(404, json={"detail": "group not found"})
            if method == "PATCH":
                patch = {k: v for k, v in body.items() if v is not None}
                if not self.stores_entity_notes:
                    patch.pop("notes", None)
                row.update(patch)
            return httpx.Response(200, json=row)

        # -- permanent delete --
        m = _EXP_ITEM.match(path)
        if m and method == "DELETE":
            eid = m.group(1)
            if self.experiments.pop(eid, None) is None:
                return httpx.Response(404, json={"detail": "experiment not found"})
            # Mirrors the engine cascade (0080): the experiment's runs go with it.
            for rid in [
                rid for rid, r in self.runs.items() if r.get("experiment_id") == eid
            ]:
                self.runs.pop(rid)
            return httpx.Response(204)

        m = re.match(r"^/v1/runs/([^/]+)/heartbeat$", path)
        if m and method == "POST":
            rid = m.group(1)
            row = self.runs.get(rid)
            if row is None:
                return httpx.Response(404, json={"detail": "run not found"})
            # Mirrors app/runs/router.py: only a 'running' run is stamped; a late
            # beat racing a completion is a no-op, not an error.
            if row.get("status") == "running":
                row["last_heartbeat_at"] = self._stamp()
                self.run_heartbeats[rid] = self.run_heartbeats.get(rid, 0) + 1
            return httpx.Response(200, json=row)


        if path == "/v1/artifacts/uploads/gc" and method == "POST":
            older_than = body["older_than"]
            swept = 0
            for arts in self.artifacts.values():
                for a in list(arts):
                    # Pending AND old enough: a confirmed artifact is never swept, and
                    # neither is an upload started after the cutoff.
                    if a.get("status") == "pending" and a.get("created_at", "") < older_than:
                        arts.remove(a)
                        swept += 1
            return httpx.Response(200, json={"swept": swept})

        m = re.match(r"^/v1/artifacts/([^/]+)$", path)
        if m and method == "DELETE":
            aid = m.group(1)
            for arts in self.artifacts.values():
                for a in list(arts):
                    if a.get("id") == aid:
                        arts.remove(a)
                        return httpx.Response(204)
            return httpx.Response(404, json={"detail": "artifact not found"})

        # -- reads: series / metrics / spans / experiment edges --
        m = _RUN_SERIES.match(path)
        if m and method == "GET":
            return httpx.Response(200, json=self.series.get(m.group(1), []))

        m = _RUN_METRICS.match(path)
        if m and method == "GET":
            rid = m.group(1)
            rows = self.metric_points.get(rid, [])
            key = request.url.params.get("key")
            if key:
                rows = [p for p in rows if p.get("key") == key]
            return httpx.Response(200, json=rows)

        m = _RUN_SPANS.match(path)
        if m and method == "GET":
            rid = m.group(1)
            rows = self.spans.get(rid, [])
            span_type = request.url.params.get("span_type")
            if span_type:
                rows = [s for s in rows if s.get("span_type") == span_type]
            return httpx.Response(200, json=rows)

        m = re.match(r"^/v1/spans/([^/]+)$", path)
        if m and method == "GET":
            sid = m.group(1)
            for rows in self.spans.values():
                for span in rows:
                    if span.get("id") == sid:
                        return httpx.Response(200, json=span)
            return httpx.Response(404, json={"detail": "span not found"})

        m = re.match(r"^/v1/experiments/([^/]+)/artifacts$", path)
        if m and method == "GET":
            # Mirrors app/artifacts/experiment_router.py: `experiment_id = $1`, and
            # nothing else. This used to roll up the artifacts of the experiment's
            # RUNS -- rows the real route has never returned -- while reading the
            # directly-filed ones from the wrong key, so it missed the only rows that
            # DO belong here. Both halves inverted: it invented an inheritance the
            # backend does not implement and hid the anchor the backend does.
            eid = m.group(1)
            rows = list(self.artifacts.get(f"experiment:{eid}", []))
            rows += [a for a in self.artifacts.get(eid, []) if a.get("experiment_id") == eid]
            return httpx.Response(200, json=_newest_first(rows))

        m = re.match(r"^/v1/experiments/([^/]+)/edges$", path)
        if m and method == "GET":
            # Mirrors app/lineage/router.py: an edge belongs to the experiment when the
            # run/artifact on either end does. Returning every edge instead would let a
            # client that passed the wrong id still pass the test.
            eid = m.group(1)
            run_ids = {r for r, row in self.runs.items() if row.get("experiment_id") == eid}
            artifact_ids = {
                a["id"] for rid in run_ids for a in self.artifacts.get(rid, []) if a.get("id")
            }
            def _touches(edge: dict) -> bool:
                for side in ("source", "target"):
                    kind, ref = edge.get(f"{side}_type"), edge.get(f"{side}_id")
                    if kind == "run" and ref in run_ids:
                        return True
                    if kind == "artifact" and ref in artifact_ids:
                        return True
                return False

            return httpx.Response(200, json=[e for e in self.edges if _touches(e)])

        # -- artifact versions (the chain the retired asset registry folded into) --
        m = re.match(r"^/v1/artifacts/([^/]+)/versions$", path)
        if m:
            aid = m.group(1)
            if method == "POST":
                vers = self.artifact_versions.setdefault(aid, [])
                v = {"id": str(uuid.uuid4()), "customer_id": "lab-42", "artifact_id": aid,
                     "version": len(vers) + 1, "label": body.get("label"),
                     "content_hash": body.get("content_hash"), "uri": body.get("uri"),
                     "size_bytes": body.get("size_bytes"), "content_type": body.get("content_type"),
                     "source_artifact_id": body.get("from_artifact_id"), "meta": body.get("meta", {}),
                     "status": "complete", "origin": "upload",
                     "created_at": "2026-07-11T00:00:00Z"}
                vers.append(v)
                return httpx.Response(201, json=v)
            if method == "GET":
                # 404 on an UNKNOWN id, exactly like the route: it calls
                # `require_artifact(conn, artifact_id)` before listing, which raises
                # 404 "artifact not found" for an absent or soft-deleted row
                # (research-os app/artifacts/versions_router.py). Returning 200 + []
                # here would conflate "this artifact has no versions yet" with "no
                # such artifact" -- opposite answers, and the second is the one a
                # caller may use as an existence probe. A fake that cannot tell them
                # apart certifies a client that cannot either.
                if self._find_artifact(aid) is None:
                    return httpx.Response(404, json={"detail": "artifact not found"})
                return httpx.Response(200, json=self.artifact_versions.get(aid, []))

        # -- lineage edges (fold #2) --
        if path == "/v1/edges" and method == "POST":
            row = {"id": str(uuid.uuid4()), "customer_id": "lab-42", **body, "created_at": "2026-07-11T00:00:00Z"}
            self.edges.append(row)
            return httpx.Response(201, json=row)
        m = re.match(r"^/v1/runs/([^/]+)/edges$", path)
        if m and method == "GET":
            rid = m.group(1)
            return httpx.Response(200, json=[e for e in self.edges if rid in (e.get("source_id"), e.get("target_id"))])

        # -- execution records (fold #7) --
        if path == "/v1/execution-records" and method == "POST":
            ch = "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            row = {"customer_id": "lab-42", "content_hash": ch,
                   **{k: body.get(k, {}) for k in ("code", "deps", "hardware", "settings", "paths")},
                   "created_at": "2026-07-11T00:00:00Z"}
            self.execution_records[ch] = row
            return httpx.Response(201, json=row)
        m = re.match(r"^/v1/execution-records/(.+)$", path)
        if m and method == "GET":
            return httpx.Response(200, json=self.execution_records.get(m.group(1), {"content_hash": m.group(1)}))

        # -- experiment versions (fold #6) --
        m = re.match(r"^/v1/experiments/([^/]+)/versions$", path)
        if m and method == "POST":
            eid = m.group(1)
            vers = self.experiment_versions.setdefault(eid, [])
            v = {"id": str(uuid.uuid4()), "experiment_id": eid, "version": len(vers) + 1,
                 "label": body.get("label"), "created_at": "2026-07-11T00:00:00Z"}
            vers.append(v)
            return httpx.Response(201, json=v)
        if m and method == "GET":
            return httpx.Response(200, json=self.experiment_versions.get(m.group(1), []))

        # -- events (fold #10, read-only) --
        if path == "/v1/events" and method == "GET":
            return httpx.Response(200, json=[])
        m = re.match(r"^/v1/runs/([^/]+)/events$", path)
        if m and method == "GET":
            return httpx.Response(200, json=self.run_events.get(m.group(1), []))

        # -- artifact upload flow (fold #16) --
        m = re.match(r"^/v1/runs/([^/]+)/artifacts/uploads$", path)
        if m and method == "POST":
            if self.fail_next_uploads:
                self.fail_next_uploads = False
                return httpx.Response(503, json={"detail": "storage down"})
            rid = m.group(1)
            ch = body["content_hash"]
            aid = str(uuid.uuid4())
            have = ch in self.uploaded
            art = {"id": aid, "run_id": rid, "name": body["name"], "content_hash": ch,
                   "size_bytes": body.get("size_bytes"),
                   "kind": (body.get("kind") or "file").strip().lower(),
                   "meta": body.get("meta"), "step_index": body.get("step_index"),
                   "span_id": body.get("span_id"),
                   "status": "complete" if have else "pending", "is_reference": False}
            self.artifacts.setdefault(rid, []).append(art)
            return httpx.Response(201, json={
                "artifact_id": aid, "have": have,
                "upload_url": None if have else f"http://r2.test/put/{aid}",
                "key": f"lab-42/{aid}",
                "upload_headers": getattr(self, "upload_headers", {}),
            })
        # -- the other three anchors: project / experiment / workspace / shared --
        # ScopedUploadRequest is declared extra="forbid", so anything run-only that
        # reaches here is a 422 — the client is supposed to have refused it first, and
        # this is what proves the client actually does. `notes` is IN the allowlist
        # (0095): it is the one descriptive field this contract accepts, and the
        # absence of it is what drove agents to concatenate descriptions onto `name`.
        for pattern, anchor in (
            (_PROJ_ARTIFACT_UPLOADS, "project"),
            (_EXP_ARTIFACT_UPLOADS, "experiment"),
            (_WS_FILE_UPLOADS, "workspace"),
        ):
            m = pattern.match(path)
            if m and method == "POST":
                extras = sorted(
                    set(body) - {"name", "content_hash", "size_bytes", "content_type", "notes"}
                )
                if extras:
                    return httpx.Response(
                        422, json={"detail": f"extra fields not permitted: {extras}"}
                    )
                return self._presign(anchor, m.group(1), body)
        if path == "/v1/shared/files/uploads" and method == "POST":
            extras = sorted(set(body) - {"name", "content_hash", "size_bytes", "content_type", "notes"})
            if extras:
                return httpx.Response(422, json={"detail": f"extra fields not permitted: {extras}"})
            return self._presign("shared", "team", body)

        for pattern, anchor in (
            (_PROJ_ARTIFACTS, "project"),
            (_EXP_ARTIFACTS, "experiment"),
        ):
            m = pattern.match(path)
            if m and method == "POST":
                aid = str(uuid.uuid4())
                row = {
                    "id": aid, "name": body["name"], "uri": body.get("uri"),
                    "is_reference": bool(body.get("is_reference")),
                    "kind": body.get("kind") or "file", "status": "complete",
                    # `meta` is PERSISTED, like ExperimentArtifactCreate/
                    # ProjectArtifactCreate declare and apply_artifact stores. The fake
                    # used to drop it, which is the shape of guard that certifies its
                    # own rot: a research note IS its `meta`, so a note test would have
                    # gone green against a fake that threw the note away.
                    "meta": body.get("meta") or {},
                    "created_at": self._stamp(),
                    f"{anchor}_id": m.group(1),
                }
                self.artifacts.setdefault(f"{anchor}:{m.group(1)}", []).append(row)
                return httpx.Response(201, json=row)
            if m and method == "GET":
                return httpx.Response(200, json=_newest_first(
                    self.artifacts.get(f"{anchor}:{m.group(1)}", [])
                ))

        m = _WS_FILES.match(path)
        if m and method == "GET":
            return httpx.Response(200, json=self.artifacts.get(f"workspace:{m.group(1)}", []))

        if path == "/v1/shared/files" and method == "GET":
            # `prefix` is a FOLDER filter, and this fake models that EXACTLY,
            # because the kind version certifies broken code. The backend derives
            # `path` as the DIRNAME of `name` (0029, GENERATED column) and matches
            # `path = prefix OR path LIKE 'prefix/%'` -- so a root-level file has
            # path '' and a prefix of its own NAME matches nothing. A fake that
            # did startswith() on `name` instead would make a client that passes
            # the whole name look correct, and that client returns "no such
            # artifact" for every flat-named scorer -- read downstream as licence
            # to create a duplicate.
            rows = self.artifacts.get("shared:team", [])
            if (want := request.url.params.get("prefix")) is not None:
                want = want.rstrip("/")
                if want:
                    def _dirname(row: dict) -> str:
                        nm = str(row.get("name", ""))
                        return nm.rsplit("/", 1)[0] if "/" in nm else ""

                    rows = [
                        a for a in rows
                        if _dirname(a) == want or _dirname(a).startswith(f"{want}/")
                    ]
            return httpx.Response(200, json=rows)

        m = _SHARED_FILE_SUB.match(path)
        if m and method in ("POST", "GET"):
            aid, verb = m.group(1), m.group(2)
            row = self._find_artifact(aid)
            if row is None:
                return httpx.Response(404, json={"detail": "not found"})
            if verb == "download":
                return httpx.Response(200, json={"download_url": f"http://r2.test/get/{aid}"})
            if verb == "confirm":
                row["status"] = "complete"
                return httpx.Response(200, json=row)
            # unshare: a MOVE back to the caller's personal workspace, not a copy.
            self.artifacts.get("shared:team", []).remove(row)
            self.artifacts.setdefault(f"workspace:{_WS_MINE}", []).append(row)
            return httpx.Response(200, json=row)

        m = _SHARED_FILE_ITEM.match(path)
        if m and method == "DELETE":
            row = self._find_artifact(m.group(1))
            if row is None:
                return httpx.Response(404, json={"detail": "not found"})
            self.artifacts.get("shared:team", []).remove(row)
            return httpx.Response(204)

        m = _WS_FILE_SHARE.match(path)
        if m and method == "POST":
            aid = m.group(1)
            row = self._find_artifact(aid)
            if row is None:
                return httpx.Response(404, json={"detail": "not found"})
            # Ownership-transfer MOVE: it leaves the workspace listing entirely.
            for key, rows in self.artifacts.items():
                if key.startswith("workspace:") and row in rows:
                    rows.remove(row)
            self.artifacts.setdefault("shared:team", []).append(row)
            return httpx.Response(200, json=row)

        if path.startswith("/put/") and method == "PUT":
            self.puts.append(path)
            self.put_headers.append(dict(request.headers))
            self.blobs[path.rsplit("/", 1)[-1]] = request.content or b""
            return httpx.Response(200)
        m = re.match(r"^/v1/artifacts/([^/]+)/move$", path)
        if m and method == "POST":
            return self._move_artifact(m.group(1), body or {})

        m = re.match(r"^/v1/artifacts/([^/]+)/confirm$", path)
        if m and method == "POST":
            aid = m.group(1)
            for arts in self.artifacts.values():
                for a in arts:
                    if a.get("id") == aid:
                        a["status"] = "complete"
                        if a.get("content_hash"):
                            self.uploaded.add(a["content_hash"])
                        return httpx.Response(200, json=a)
            return httpx.Response(404, json={"detail": "not found"})

        # artifact download (presigned GET) -> used by asset materialize
        m = re.match(r"^/v1/artifacts/([^/]+)/download$", path)
        if m and method == "POST":
            return httpx.Response(200, json={"download_url": f"http://r2.test/get/{m.group(1)}"})
        if path.startswith("/get/") and method == "GET":
            self.gets.append(path)
            blob = self.blobs.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, content=blob if blob is not None else b"ASSET-BYTES")

        if path == "/ingest/v1/runs" and method == "POST":
            rid = str(uuid.uuid4())
            run = body["run"]
            row = self._new_run(rid, "exp", {"name": run["name"], "source": run.get("source", "api")})
            return httpx.Response(200, json=row)

        return httpx.Response(404, json={"detail": f"no fake route for {method} {path}"})

    def _run_external_id_conflict(self, body: dict) -> httpx.Response | None:
        """Mirror the engine's UNIQUE (customer_id, source, external_id): without
        this the fake happily mints duplicate run identities and the supersede
        path in ``run(on_conflict=...)`` has nothing real to test against."""
        ext = (body or {}).get("external_id")
        if ext is None:
            return None
        source = (body or {}).get("source", "api")
        for _row in self.runs.values():
            if _row.get("external_id") == ext and _row.get("source", "api") == source:
                return httpx.Response(409, json={"detail": {
                    "message": "run with this (source, external_id) already exists",
                    "existing_id": _row["id"]}})
        return None

    def _new_run(
        self,
        rid: str,
        experiment_id: str | None,
        body: dict,
        *,
        project_id: str | None = None,
    ) -> dict:
        # RunDetailOut shape (fold fields surfaced on /v1 reads). project_id is
        # always set on the real backend (0054); the fake defaults one so old
        # seeds stay valid.
        row = {
            "id": rid,
            "experiment_id": experiment_id,
            "project_id": project_id or str(uuid.uuid4()),
            "name": body.get("name", "run"),
            "description": body.get("description"),
            "status": "running",
            "source": body.get("source", "api"),
            "external_id": body.get("external_id"),
            "tags": body.get("tags", []),
            "metadata": body.get("metadata", {}),
            "config": body.get("config", {}),
            "parent_run_id": body.get("parent_run_id"),
            "parent_relation": body.get("parent_relation"),
            "group_id": body.get("group_id"),
            # Mirrors the engine: a caller-chosen `slug` lands in short_id (0.110.0.0).
            "short_id": body.get("slug") or body.get("short_id", f"run-{rid[:8]}"),
            "foreign_keys": body.get("foreign_keys", {}),
            "env_ref": body.get("env_ref"),
            "created_by": "ingest:test",
            # Required on the real RunDetailOut, so read responses match the
            # contract rather than a leaner fiction.
            "customer_id": "lab-42",
            "created_at": self._stamp(),
        }
        if self.stores_entity_notes:
            row["notes"] = body.get("notes")
        self.runs[rid] = row
        return row


def make_client(
    app: FakeApp,
    *,
    fail_open: bool = True,
    tmp_spool=None,
    async_writes: bool = False,
    **client_kwargs,
) -> Client:
    settings = Settings(
        base_url="http://test",
        token="ros_pat_deadbeef",
        ingest_token="ros_ing_cafef00d",
        hmac_secret="s3cr3t",
    )
    httpx_client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(app.handler))
    transport = Transport(settings, client=httpx_client)
    from probe.sdk.journal import Journal

    # ALWAYS an isolated journal: the default would be the developer's real
    # ~/.local/state/probe/outbox, and Journal._ensure imports any real legacy
    # spool it finds -- a test must never touch either.
    journal = Journal(
        tmp_spool if tmp_spool else tempfile.mkdtemp(prefix="probe-test-journal-"),
        context={"name": None, "base_url": settings.base_url},
    )
    return Client(
        settings=settings,
        transport=transport,
        fail_open=fail_open,
        journal=journal,
        async_writes=async_writes,
        **client_kwargs,
    )


@pytest.fixture(autouse=True)
def _no_real_outbox(monkeypatch, tmp_path_factory):
    """Point every durable-state path away from the developer's real
    ~/.local/state. The every-command banner reads the outbox, the drainer
    re-kick would SPAWN against it, and Journal._ensure imports -- AND THEN
    DELETES -- any legacy spool it finds via PROBE_SPOOL_DIR/XDG_STATE_HOME
    (testing review: isolating only PROBE_OUTBOX_DIR left the real spool
    reachable). None of it may be touched by a test."""
    monkeypatch.setenv(
        "PROBE_OUTBOX_DIR", str(tmp_path_factory.mktemp("outbox-guard"))
    )
    monkeypatch.setenv(
        "XDG_STATE_HOME", str(tmp_path_factory.mktemp("state-guard"))
    )
    monkeypatch.delenv("PROBE_SPOOL_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_background_heartbeat(monkeypatch):
    """create_run starts a heartbeat thread by default; a background beat landing
    between an action and its `app.requests[-1]` assertion would make half this
    suite flaky. Kill it globally; tests that exercise liveness re-enable it with
    an explicit interval (the argument outranks the env var)."""
    monkeypatch.setenv("PROBE_HEARTBEAT_SECONDS", "0")


@pytest.fixture(autouse=True)
def _no_auto_snapshot(monkeypatch):
    """Auto-snapshot (Task 8) would make every client.run() in this suite
    snapshot the working tree. Tests that exercise the auto path re-enable it
    explicitly."""
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "0")


@pytest.fixture(autouse=True)
def _no_capture_enforcement_leakage(monkeypatch):
    """Strip PROBE_REQUIRE_COMPLETE and PROBE_ENV_ALLOWLIST from every test's
    environment. Both are real dev-shell exports (the former from local
    `probe exec --strict` runs, the latter from per-site env capture tuning),
    and neither is otherwise isolated the way config/outbox state is -- a
    developer with either set in their shell would get spurious strict-gate
    raises or extra captured env values with no test-visible cause. Tests
    that exercise these paths set them explicitly, which wins over this."""
    monkeypatch.delenv("PROBE_REQUIRE_COMPLETE", raising=False)
    monkeypatch.delenv("PROBE_ENV_ALLOWLIST", raising=False)


@pytest.fixture
def app() -> FakeApp:
    return FakeApp()


@pytest.fixture
def client(app: FakeApp, tmp_path) -> Client:
    return make_client(app, tmp_spool=tmp_path / "spool")


def open_run(client: Client, *, experiment: str, name: str | None = None, **run_kw):
    """Create the experiment, then open a run in it.

    `client.run()` does get-or-create its parents, so most of this is belt and
    braces — but creating an experiment needs a hypothesis, and these callers have
    no opinion about one. Seeding it here keeps every test that just needs *a run
    to exist* from having to invent a hypothesis it does not care about. Tests
    ABOUT identity resolution call create_experiment / run directly and assert on
    the behaviour."""
    from probe import errors as _errors

    project_slug = f"project-{experiment}"
    try:
        project = client.create_project(project_slug)
    except _errors.ConflictError:
        project = client.resolve_project(project_slug)
        assert project is not None
    try:
        client.create_experiment(
            experiment,
            experiment,
            hypothesis="h",
            project_id=project["id"],
        )
    except _errors.ConflictError:
        pass
    return client.run(experiment=experiment, name=name, **run_kw)
