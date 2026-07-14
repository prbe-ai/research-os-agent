"""A stateful in-memory fake of the Probe Research v3 API over httpx.MockTransport.

Routes only what the client exercises, with response shapes matching CONTRACT.md.
Lets us test the SDK + CLI end to end with no live server.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

import httpx
import pytest

from probe.client import Client
from probe.config import Settings
from probe.transport import Transport

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
        self.assets: dict[str, dict] = {}
        self.asset_versions: dict[str, list[dict]] = {}
        self.edges: list[dict] = []
        self.execution_records: dict[str, dict] = {}
        self.experiment_versions: dict[str, list[dict]] = {}
        self.uploaded: set[str] = set()
        self.puts: list[str] = []
        self.put_headers: list[dict[str, str]] = []
        self.gets: list[str] = []
        self.metrics_inserted = 0
        self.spans_upserted = 0
        # test knobs
        self.experiment_conflict_id: str | None = None
        self.fail_next_metrics = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method
        path = request.url.path
        try:
            body = json.loads(request.content) if request.content else {}
        except (json.JSONDecodeError, ValueError):
            body = {}  # e.g. a raw-bytes PUT to a presigned URL

        if path == "/v1/me" and method == "GET":
            return httpx.Response(200, json={
                "user_id": "00000000-0000-0000-0000-000000000001",
                "email": "dev@example.com", "name": "Dev",
                "customer_id": "lab-42", "role": "owner",
                "scopes": ["read", "write", "delete", "admin"], "via": "token",
            })

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
            # NB: not setdefault() - _new_run has a side effect (stores the row), so
            # an eager default would clobber the existing run on every PATCH.
            row = self.runs.get(rid) or self._new_run(rid, "exp", {"name": "r"})
            for k, v in body.items():
                if k == "foreign_keys":  # per-key new-wins merge (mirrors the backend)
                    row["foreign_keys"] = {**(row.get("foreign_keys") or {}), **v}
                else:
                    row[k] = v
            return httpx.Response(200, json=row)

        # -- assets (fold #5) --
        if path == "/v1/assets" and method == "POST":
            dup = next((a for a in self.assets.values() if a["name"] == body["name"]), None)
            if dup:
                return httpx.Response(409, json={"detail": {"message": "asset name exists", "existing_id": dup["id"]}})
            aid = str(uuid.uuid4())
            row = {"id": aid, "customer_id": "lab-42", "name": body["name"], "kind": body.get("kind", "dataset"),
                   "description": body.get("description"), "tags": body.get("tags", []),
                   "metadata": body.get("metadata", {}), "created_at": "2026-07-11T00:00:00Z"}
            self.assets[aid] = row
            self.asset_versions[aid] = []
            return httpx.Response(201, json=row)
        if path == "/v1/assets" and method == "GET":
            return httpx.Response(200, json=list(self.assets.values()))
        if path.startswith("/v1/assets/") and path.endswith("/versions"):
            aid = path.split("/")[3]
            if method == "POST":
                vers = self.asset_versions.setdefault(aid, [])
                v = {"id": str(uuid.uuid4()), "customer_id": "lab-42", "asset_id": aid,
                     "version": len(vers) + 1, "label": body.get("label"),
                     "content_hash": body.get("content_hash"), "uri": body.get("uri"),
                     "size_bytes": body.get("size_bytes"), "content_type": body.get("content_type"),
                     "source_artifact_id": body.get("from_artifact_id"), "meta": body.get("meta", {}),
                     "created_at": "2026-07-11T00:00:00Z"}
                vers.append(v)
                return httpx.Response(201, json=v)
            if method == "GET":
                return httpx.Response(200, json=self.asset_versions.get(aid, []))
        m = re.match(r"^/v1/assets/([^/]+)$", path)
        if m and method == "GET":
            aid = m.group(1)
            return httpx.Response(200, json=self.assets[aid]) if aid in self.assets else httpx.Response(404, json={"detail": "not found"})

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
            return httpx.Response(200, json=[])

        # -- artifact upload flow (fold #16) --
        m = re.match(r"^/v1/runs/([^/]+)/artifacts/uploads$", path)
        if m and method == "POST":
            rid = m.group(1)
            ch = body["content_hash"]
            aid = str(uuid.uuid4())
            have = ch in self.uploaded
            art = {"id": aid, "run_id": rid, "name": body["name"], "content_hash": ch,
                   "size_bytes": body.get("size_bytes"), "kind": "file",
                   "status": "complete" if have else "pending", "is_reference": False}
            self.artifacts.setdefault(rid, []).append(art)
            return httpx.Response(201, json={
                "artifact_id": aid, "have": have,
                "upload_url": None if have else f"http://r2.test/put/{aid}",
                "key": f"lab-42/{aid}",
                "upload_headers": getattr(self, "upload_headers", {}),
            })
        if path.startswith("/put/") and method == "PUT":
            self.puts.append(path)
            self.put_headers.append(dict(request.headers))
            return httpx.Response(200)
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
            return httpx.Response(200, content=b"ASSET-BYTES")

        if path == "/ingest/v1/runs" and method == "POST":
            rid = str(uuid.uuid4())
            run = body["run"]
            row = self._new_run(rid, "exp", {"name": run["name"], "source": run.get("source", "api")})
            return httpx.Response(200, json=row)

        return httpx.Response(404, json={"detail": f"no fake route for {method} {path}"})

    def _new_run(self, rid: str, experiment_id: str, body: dict) -> dict:
        # RunDetailOut shape (fold fields surfaced on /v1 reads).
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
            "short_id": body.get("short_id", f"run-{rid[:8]}"),
            "foreign_keys": body.get("foreign_keys", {}),
            "env_ref": body.get("env_ref"),
            "created_by": "ingest:test",
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
    from probe.spool import Spool

    spool = Spool(tmp_spool) if tmp_spool else None
    return Client(settings=settings, transport=transport, fail_open=fail_open, spool=spool)


@pytest.fixture
def app() -> FakeApp:
    return FakeApp()


@pytest.fixture
def client(app: FakeApp, tmp_path) -> Client:
    return make_client(app, tmp_spool=tmp_path / "spool")
