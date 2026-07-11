"""A stateful in-memory fake of the research-os v3 API over httpx.MockTransport.

Routes only what the client exercises, with response shapes matching CONTRACT.md.
Lets us test the SDK + CLI end to end with no live server.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest

from ros.client import Client
from ros.config import Settings
from ros.transport import Transport

_RUN_METRICS = re.compile(r"^/v1/runs/([^/]+)/metrics$")
_RUN_SPANS = re.compile(r"^/v1/runs/([^/]+)/spans$")
_RUN_ARTIFACTS = re.compile(r"^/v1/runs/([^/]+)/artifacts$")
_RUN_BUNDLE = re.compile(r"^/v1/runs/([^/]+)/bundle$")
_RUN_LINEAGE = re.compile(r"^/v1/runs/([^/]+)/lineage$")
_RUN_ITEM = re.compile(r"^/v1/runs/([^/]+)$")
_EXP_RUNS = re.compile(r"^/v1/experiments/([^/]+)/runs$")
_EXP_ITEM = re.compile(r"^/v1/experiments/([^/]+)$")
_PROJ_ITEM = re.compile(r"^/v1/projects/([^/]+)$")


class FakeApp:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.runs: dict[str, dict] = {}
        self.experiments: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        self.artifacts: dict[str, list[dict]] = {}
        self.metrics_inserted = 0
        self.spans_upserted = 0
        # test knobs
        self.experiment_conflict_id: str | None = None
        self.fail_next_metrics = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method
        path = request.url.path
        body = json.loads(request.content) if request.content else {}

        if path == "/auth/me" and method == "GET":
            return httpx.Response(200, json={"email": "dev@example.com", "customer_id": "lab-42"})

        if path == "/v1/tokens/current" and method == "DELETE":
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
            row = {"id": pid, "slug": body["slug"], "name": body.get("name", body["slug"])}
            self.projects[pid] = row
            return httpx.Response(201, json=row)

        if path == "/v1/projects" and method == "GET":
            return httpx.Response(200, json=list(self.projects.values()))

        m = _PROJ_ITEM.match(path)
        if m and method == "GET":
            pid = m.group(1)
            if pid not in self.projects:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=self.projects[pid])

        if path == "/v1/experiments" and method == "POST":
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
            }
            self.experiments[eid] = row
            return httpx.Response(201, json=row)

        if path == "/v1/experiments" and method == "GET":
            rows = list(self.experiments.values())
            project_id = request.url.params.get("project_id")
            if project_id:
                rows = [row for row in rows if row.get("project_id") == project_id]
            return httpx.Response(200, json=rows)

        if path == "/v1/runs" and method == "GET":
            rows = list(self.runs.values())
            experiment_id = request.url.params.get("experiment_id")
            if experiment_id:
                rows = [row for row in rows if row.get("experiment_id") == experiment_id]
            return httpx.Response(200, json=rows)

        m = _EXP_ITEM.match(path)
        if m and method == "GET":
            eid = m.group(1)
            return httpx.Response(200, json=self.experiments.get(eid, {"id": eid, "hypothesis": "h", "project_id": str(uuid.uuid4())}))

        m = _EXP_RUNS.match(path)
        if m and method == "POST":
            rid = str(uuid.uuid4())
            row = self._new_run(rid, m.group(1), body)
            return httpx.Response(201, json=row)

        m = _RUN_METRICS.match(path)
        if m and method == "POST":
            if self.fail_next_metrics:
                self.fail_next_metrics = False
                return httpx.Response(503, json={"detail": "db down"})
            n = len(body.get("points", []))
            self.metrics_inserted += n
            return httpx.Response(200, json={"inserted": n})

        m = _RUN_SPANS.match(path)
        if m and method == "POST":
            n = len(body.get("spans", []))
            self.spans_upserted += n
            return httpx.Response(200, json={"upserted": n})

        m = _RUN_ARTIFACTS.match(path)
        if m and method == "POST":
            row = {"id": str(uuid.uuid4()), **body}
            self.artifacts.setdefault(m.group(1), []).append(row)
            return httpx.Response(201, json=row)
        if m and method == "GET":
            return httpx.Response(200, json=self.artifacts.get(m.group(1), []))

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
        if m and method == "GET":
            rid = m.group(1)
            row = self.runs.get(rid)
            if row is None:
                row = self._new_run(rid, "exp", {"name": "r"})
            return httpx.Response(200, json=row)
        if m and method == "PATCH":
            rid = m.group(1)
            row = self.runs.setdefault(rid, self._new_run(rid, "exp", {"name": "r"}))
            row.update({k: v for k, v in body.items()})
            return httpx.Response(200, json=row)

        if path == "/ingest/v1/runs" and method == "POST":
            rid = str(uuid.uuid4())
            run = body["run"]
            row = self._new_run(rid, "exp", {"name": run["name"], "source": run.get("source", "api")})
            return httpx.Response(200, json=row)

        return httpx.Response(404, json={"detail": f"no fake route for {method} {path}"})

    def _new_run(self, rid: str, experiment_id: str, body: dict) -> dict:
        row = {
            "id": rid,
            "experiment_id": experiment_id,
            "name": body.get("name", "run"),
            "status": "running",
            "source": body.get("source", "api"),
            "metadata": body.get("metadata", {}),
            "config": body.get("config", {}),
            "parent_run_id": body.get("parent_run_id"),
            "parent_relation": body.get("parent_relation"),
        }
        self.runs[rid] = row
        return row


def make_client(app: FakeApp, *, fail_open: bool = True, tmp_spool=None) -> Client:
    settings = Settings(
        base_url="http://test",
        token="ros_pat_deadbeef",
        ingest_token="ros_ing_cafef00d",
        hmac_secret="s3cr3t",
    )
    httpx_client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(app.handler))
    transport = Transport(settings, client=httpx_client)
    from ros.spool import Spool

    spool = Spool(tmp_spool) if tmp_spool else None
    return Client(settings=settings, transport=transport, fail_open=fail_open, spool=spool)


@pytest.fixture
def app() -> FakeApp:
    return FakeApp()


@pytest.fixture
def client(app: FakeApp, tmp_path) -> Client:
    return make_client(app, tmp_spool=tmp_path / "spool")
