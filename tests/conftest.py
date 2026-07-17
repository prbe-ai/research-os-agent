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

@pytest.fixture(autouse=True)
def _no_live_token_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the hosted MCP's edge token check off by default.

    It calls the real ``/v1/me``, so any test handing a bearer to the wrapper would
    quietly hit production and fail on a 401. Tests that cover verification inject
    their own ``token_rejected`` instead (see tests/test_mcp_hosted.py).
    """
    monkeypatch.setenv("PROBE_MCP_VERIFY_TOKEN", "0")


_RUN_METRICS = re.compile(r"^/v1/runs/([^/]+)/metrics$")
_RUN_SPANS = re.compile(r"^/v1/runs/([^/]+)/spans$")
_RUN_SERIES = re.compile(r"^/v1/runs/([^/]+)/series$")
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
        self.tokens: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.series: dict[str, list[dict]] = {}
        self.metric_points: dict[str, list[dict]] = {}
        self.spans: dict[str, list[dict]] = {}
        self.assets: dict[str, dict] = {}
        self.asset_versions: dict[str, list[dict]] = {}
        self.edges: list[dict] = []
        self.execution_records: dict[str, dict] = {}
        self.experiment_versions: dict[str, list[dict]] = {}
        self.run_events: dict[str, list[dict]] = {}
        self.uploaded: set[str] = set()
        self.puts: list[str] = []
        self.put_headers: list[dict[str, str]] = []
        self.gets: list[str] = []
        self.metrics_inserted = 0
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
        preservation test (archive is idempotent) actually catch a wrong re-stamp."""
        self._ts += 1
        return f"2026-07-15T00:00:{self._ts:02d}Z"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method
        path = request.url.path
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

        if path == "/v1/search" and method == "POST":
            self.search_requests.append(body)
            if self.search_404_once:
                self.search_404_once = False
                return httpx.Response(404, json={"detail": "Not Found"})
            if body.get("workspace_id") in self.search_404_workspace_ids:
                return httpx.Response(404, json={"detail": "not found"})
            if self.search_responses:
                return httpx.Response(200, json=self.search_responses.pop(0))
            if self.search_response is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json=self.search_response)
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
                "customer_id": "lab-42",
                "created_at": self._stamp(),
                "archived_at": None,
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
        if m and method == "PATCH":
            eid = m.group(1)
            row = self.experiments.get(eid)
            if row is None:
                return httpx.Response(404, json={"detail": "not found"})
            row.update(body)
            return httpx.Response(200, json=row)

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

        m = _RUN_SERIES.match(path)
        if m and method == "GET":
            return httpx.Response(200, json=self.series.get(m.group(1), []))

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
            row = self.runs.get(rid)
            if row is None or row.get("deleted_at"):
                return httpx.Response(404, json={"detail": "run not found"})
            row["deleted_at"] = "2026-07-15T00:00:00Z"
            return httpx.Response(200, json=row)
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
                row.update({k: v for k, v in body.items() if v is not None})
            return httpx.Response(200, json=row)

        # -- archive / restore / gc --
        m = re.match(r"^/v1/experiments/([^/]+)/(archive|restore)$", path)
        if m and method == "POST":
            eid, verb = m.group(1), m.group(2)
            row = self.experiments.get(eid)
            if row is None:
                return httpx.Response(404, json={"detail": "experiment not found"})
            if verb == "archive":
                # Idempotent: keep the FIRST archive time (mirrors the backend). The
                # stamp is unique per call, so an idempotency test that asserts the time
                # was preserved would actually fail if this wrongly re-stamped.
                row["archived_at"] = row.get("archived_at") or self._stamp()
            else:
                row["archived_at"] = None
            return httpx.Response(200, json=row)

        m = re.match(r"^/v1/runs/([^/]+)/restore$", path)
        if m and method == "POST":
            rid = m.group(1)
            row = self.runs.get(rid)
            if row is None:
                return httpx.Response(404, json={"detail": "run not found"})
            row["deleted_at"] = None
            return httpx.Response(200, json=row)

        if path == "/v1/runs/gc" and method == "POST":
            ids, older_than = body.get("run_ids"), body.get("older_than")
            if (ids is None) == (older_than is None):  # exactly one selector (backend 422s)
                return httpx.Response(422, json={"detail": "exactly one of run_ids/older_than"})
            # Only ever purges SOFT-DELETED runs — a live run is untouched even if its
            # id is named explicitly. Mirrors app/runs/router.py.
            if ids is not None:
                doomed = [r for r in ids if (self.runs.get(r) or {}).get("deleted_at")]
            else:
                doomed = [
                    r for r, row in self.runs.items()
                    if row.get("deleted_at") and row["deleted_at"] < older_than
                ]
            for rid in doomed:
                self.runs.pop(rid)
            return httpx.Response(200, json={"purged": len(doomed)})

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
            # Mirrors the backend: an artifact belongs to the experiment when its
            # run does (or when it was filed on the experiment directly).
            eid = m.group(1)
            run_ids = {r for r, row in self.runs.items() if row.get("experiment_id") == eid}
            rows = [a for rid in run_ids for a in self.artifacts.get(rid, [])]
            rows += [a for a in self.artifacts.get(eid, []) if a.get("experiment_id") == eid]
            return httpx.Response(200, json=rows)

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
            # Mirrors the backend's keyset paging: limit defaults to 50, caps at 200,
            # and the cursor is an opaque offset. The fake used to return EVERY asset
            # and ignore `limit`, which hid a real bug — assets.resolve() read one
            # default-limit page, so asset 51+ resolved to "no_match" and callers were
            # told to register a duplicate. A fake kinder than the backend is a fake
            # that certifies broken code.
            rows = list(self.assets.values())
            limit = min(int(request.url.params.get("limit") or 50), 200)
            start = int(request.url.params.get("cursor") or request.url.params.get("offset") or 0)
            window = rows[start : start + limit]
            nxt = start + limit
            return httpx.Response(
                200,
                json=window,
                headers={"x-next-cursor": str(nxt)} if nxt < len(rows) else {},
            )
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
        if path.startswith("/put/") and method == "PUT":
            self.puts.append(path)
            self.put_headers.append(dict(request.headers))
            self.blobs[path.rsplit("/", 1)[-1]] = request.content or b""
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
            blob = self.blobs.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, content=blob if blob is not None else b"ASSET-BYTES")

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
            "group_id": body.get("group_id"),
            "short_id": body.get("short_id", f"run-{rid[:8]}"),
            "foreign_keys": body.get("foreign_keys", {}),
            "env_ref": body.get("env_ref"),
            "created_by": "ingest:test",
            # Required on the real RunDetailOut, so delete_run/restore_run responses
            # match the contract rather than a leaner fiction.
            "customer_id": "lab-42",
            "created_at": self._stamp(),
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
