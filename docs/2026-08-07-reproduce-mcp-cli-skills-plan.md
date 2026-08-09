# Reproduce pull surface (MCP + CLI + skills) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the research-os `/reproduce` endpoints (shipped in research-os PR #404) through the probe agent's MCP views, CLI verbs, and research skills, so a coworker can pull the complete reproduction record of any run or experiment.

**Architecture:** The server already assembles the record (`GET /v1/runs/{id}/reproduce`, `GET /v1/experiments/{id}/reproduce?version=N`). This is Plan 3 of 3 — the client is a **thin passthrough**: SDK read methods → MCP `source` passthrough → MCP `view="reproduce"` delegation (run) + a new experiment-level reproduce view → CLI `probe run reproduce` / `probe experiment reproduce` / `probe experiment freeze`. No new client-side assembly, no backend change. Completeness vocabulary stays byte-identical to `check_run` (already true server-side). Reproduce views are **atomic**: never silently truncated, overflow REPORTED — the server contract we mirror.

**Tech Stack:** Python 3.11+, `httpx` transport, Typer CLI, in-house MCP `ResearchReadService`, pytest with an in-memory `FakeApp` (`tests/conftest.py`) round-tripped through `httpx.MockTransport`.

---

## Design decisions locked before implementation

- **Both reproduce views are atomic** (`_ViewData(payload=..., rows=None)`), like the run reproduce view already is and like `_view_wiki_card`. Overflow past `token_budget` is reported as `token_budget_exceeded` by `get_entity`; nothing is dropped. The experiment view stays a *map* of compact per-run summaries (each carries a `reproduce_url` drill-down), so even a 500-run experiment is a few KB.
- **`missing` vs domain completeness.** The MCP envelope's `missing` means *the response is degraded*. A legacy run that reproduces incompletely still yields a COMPLETE response (we returned everything that exists) — its gaps live in the payload's `completeness` block. BUT: the run reproduce view has always surfaced "no execution record" as an envelope `missing` marker, and the server's `completeness.missing` is the authoritative list of reproduction-blocking gaps. So the delegating run view surfaces `completeness.missing` verbatim into the envelope `missing` (keeping the "partial" signal agents already expect from this view), and leaves `advisories` in the payload. The experiment view surfaces nothing as envelope `missing` (the map is complete); per-run states ride in the summaries.
- **Version pinning through MCP** uses the existing `filters` seam: add `(EntityType.EXPERIMENT, View.REPRODUCE): {"version"}` to `_VIEW_FILTERS`; the view reads `request.filters.get("version")`. Precedent: `(ARTIFACT, VERSIONS): {"requirement"}`.
- **`probe experiment freeze`** is an ergonomic alias for the already-existing `client.experiment_version(...)` mint (`POST /v1/experiments/{id}/versions`). No new backend. There is no `experiment` CLI group yet — this plan creates one.
- **Skills** are edited in canonical `skills/` then reconciled to `plugins/probe-research/skills/` via `make sync-plugin-skills`; `tests/test_skills_sync.py` guards byte-equality.

## Server response shapes (mirror exactly — from research-os `app/read_models/reproduce.py`)

`RunReproduce`: `run` (RunDetailOut), `hypothesis`, `execution_record`, `launch`, `restore_command` (str), `code_snapshot`, `inputs_decision` (list of `{artifact, content, content_omitted_reason}`), `note_artifacts`, `lockfiles` (list[dict]), `edges`, `span_env_refs` (list of `{..., env_ref}`), `completeness` (`{state, missing, advisories}`; state ∈ `incomplete|unverified`).

`ExperimentReproduce`: `versions` (list[dict]), `resolved_version` (int|None), `runs` (list of `RunReproduceSummary`), `completeness` (rollup dict: `total`, `incomplete`, `missing_pins`, ...).

`RunReproduceSummary`: `id`, `short_id`, `status`, `env_ref` (str|None), `has_launch` (bool), `state` (str), `missing_pin` (bool), `reproduce_url` (str).

## File structure

- `src/probe/sdk/client.py` — add `run_reproduce()`, `experiment_reproduce()` read methods (Task 1).
- `src/probe/mcp/source.py` — add `reproduce()`, `experiment_reproduce()` passthroughs (Task 2).
- `src/probe/mcp/service.py` — rewrite `_view_reproduce` to delegate; add `_view_experiment_reproduce`, `_VIEWS` + `_VIEW_FILTERS` entries (Tasks 3, 4).
- `src/probe/mcp/server.py` — extend the `get_entity` view-matrix docstring with experiment `reproduce` (Task 4).
- `src/probe/cli/main.py` — `run reproduce` verb + new `experiment` Typer app with `reproduce`/`freeze` (Tasks 5, 6).
- `tests/conftest.py` — add `/v1/runs/{id}/reproduce` + `/v1/experiments/{id}/reproduce` routes to `FakeApp` (Task 1).
- `tests/test_reproduce_pull.py` (new) — client + source round-trip (Tasks 1, 2).
- `tests/test_mcp_views.py` — run + experiment reproduce view behavior (Tasks 3, 4).
- `tests/test_cli.py` — CLI verbs (Tasks 5, 6).
- `skills/{start-research-work,track-research-work,capture-run-inputs}/SKILL.md` + plugin sync (Task 7).

---

### Task 1: SDK client read methods + FakeApp reproduce routes

**Files:**
- Modify: `src/probe/sdk/client.py` (near `run_bundle`, ~line 2083, and the experiment-version block ~line 2734)
- Modify: `tests/conftest.py` (`FakeApp.handler`, add two routes + regexes near `_RUN_BUNDLE` ~line 97)
- Create: `tests/test_reproduce_pull.py`

- [ ] **Step 1: Add FakeApp reproduce routes.** Near `_RUN_BUNDLE` add:

```python
_RUN_REPRODUCE = re.compile(r"^/v1/runs/([^/]+)/reproduce$")
_EXPERIMENT_REPRODUCE = re.compile(r"^/v1/experiments/([^/]+)/reproduce$")
```

In `FakeApp.handler`, before the catch-all, add handling that 404s an unknown id and otherwise returns a minimal-but-shaped record (keyed off `self.runs` / `self.experiments`). Mirror the real field names exactly:

```python
m = _RUN_REPRODUCE.match(path)
if m and request.method == "GET":
    rid = m.group(1)
    run = self._resolve_run(rid)  # reuse whatever the bundle route uses; 404 if None
    if run is None:
        return httpx.Response(404, json={"detail": "run not found"})
    env_ref = run.get("env_ref")
    missing = [] if env_ref else ["execution_record"]
    return httpx.Response(200, json={
        "run": run,
        "hypothesis": None,
        "execution_record": None,
        "launch": run.get("metadata", {}).get("launch"),
        "restore_command": f"probe snapshot-restore {run.get('short_id') or rid}",
        "code_snapshot": None,
        "inputs_decision": [],
        "note_artifacts": [],
        "lockfiles": [],
        "edges": [],
        "span_env_refs": [],
        "completeness": {
            "state": "incomplete" if missing else "unverified",
            "missing": missing,
            "advisories": [] if run.get("metadata", {}).get("launch") else ["launch_context"],
        },
    })

m = _EXPERIMENT_REPRODUCE.match(path)
if m and request.method == "GET":
    eid = m.group(1)
    exp = self._resolve_experiment(eid)  # 404 if None
    if exp is None:
        return httpx.Response(404, json={"detail": "experiment not found"})
    version = request.url.params.get("version")
    runs = [
        {
            "id": r["id"], "short_id": r.get("short_id"), "status": r.get("status", "running"),
            "env_ref": r.get("env_ref"), "has_launch": bool(r.get("metadata", {}).get("launch")),
            "state": "unverified" if r.get("env_ref") else "incomplete",
            "missing_pin": False, "reproduce_url": f"/v1/runs/{r['id']}/reproduce",
        }
        for r in self.runs.values() if r.get("experiment_id") == exp["id"]
    ]
    return httpx.Response(200, json={
        "versions": [], "resolved_version": int(version) if version else None,
        "runs": runs,
        "completeness": {"total": len(runs),
                         "incomplete": sum(1 for r in runs if r["state"] == "incomplete"),
                         "missing_pins": 0},
    })
```

(Use the SAME run/experiment resolution helpers the existing `bundle`/experiment routes use — read those first; do not invent new lookup logic.)

- [ ] **Step 2: Write the failing client test.** In `tests/test_reproduce_pull.py`:

```python
from __future__ import annotations
import pytest


def _seed_run(client, app):
    project = client.create_project("folding")
    client.create_experiment("p", "p", hypothesis="h", project_id=project["id"])
    run = client.run(project="folding", experiment="p", name="r1")
    return run.id, app.runs[run.id]["experiment_id"]


def test_run_reproduce_returns_server_record(client, app):
    rid, _ = _seed_run(client, app)
    rec = client.run_reproduce(rid)
    assert rec["run"]["id"] == rid
    assert rec["restore_command"].startswith("probe snapshot-restore")
    assert rec["completeness"]["state"] in {"incomplete", "unverified"}


def test_run_reproduce_legacy_run_is_incomplete_not_error(client, app):
    rid, _ = _seed_run(client, app)  # no env_ref, no launch
    rec = client.run_reproduce(rid)
    assert rec["completeness"]["state"] == "incomplete"
    assert "execution_record" in rec["completeness"]["missing"]
    assert "launch_context" in rec["completeness"]["advisories"]


def test_experiment_reproduce_lists_run_summaries(client, app):
    rid, eid = _seed_run(client, app)
    rec = client.experiment_reproduce(eid)
    assert rec["completeness"]["total"] == 1
    assert rec["runs"][0]["reproduce_url"] == f"/v1/runs/{rid}/reproduce"


def test_experiment_reproduce_forwards_version(client, app):
    _, eid = _seed_run(client, app)
    rec = client.experiment_reproduce(eid, version=2)
    assert rec["resolved_version"] == 2
```

- [ ] **Step 3: Run it, verify it fails.** `pytest tests/test_reproduce_pull.py -q` → FAIL (`AttributeError: 'Client' object has no attribute 'run_reproduce'`).

- [ ] **Step 4: Implement the two client methods.** In `client.py`, next to `run_bundle`:

```python
    def run_reproduce(self, run_id: str) -> dict:
        """GET /v1/runs/{id}/reproduce — the server-assembled reproduction record
        (execution record, launch, restore command, code snapshot, inputs, lockfiles,
        edges, span env refs, completeness). A run captured before capture-core
        answers 200 with a degraded body, never a 404. See research-os
        app/read_models/reproduce.py for the shape."""
        return self.transport.get(f"/v1/runs/{run_id}/reproduce")

    def experiment_reproduce(self, experiment_id: str, *, version: int | None = None) -> dict:
        """GET /v1/experiments/{id}/reproduce — per-run reproduction summaries (a map,
        not N full assemblies). `version` pins against a minted experiment_versions
        manifest; omitted reads live rows."""
        params = {"version": version} if version is not None else None
        return self.transport.get(f"/v1/experiments/{experiment_id}/reproduce", params=params)
```

- [ ] **Step 5: Run tests, verify pass.** `pytest tests/test_reproduce_pull.py -q` → PASS (4 tests).

- [ ] **Step 6: Commit.**

```bash
git add src/probe/sdk/client.py tests/conftest.py tests/test_reproduce_pull.py
git commit -m "feat(sdk): run_reproduce + experiment_reproduce read methods"
```

---

### Task 2: MCP source passthrough

**Files:**
- Modify: `src/probe/mcp/source.py` (near `bundle`/`execution_record`, ~line 488–571)
- Modify: `tests/test_reproduce_pull.py`

- [ ] **Step 1: Write the failing test.** Append to `tests/test_reproduce_pull.py`:

```python
from probe.mcp.source import ResearchOSSource


def test_source_reproduce_passthrough(client, app):
    rid, eid = _seed_run(client, app)
    src = ResearchOSSource(client)
    assert src.reproduce(rid)["run"]["id"] == rid
    assert src.experiment_reproduce(eid)["completeness"]["total"] == 1
    assert src.experiment_reproduce(eid, version=1)["resolved_version"] == 1
```

- [ ] **Step 2: Run it, verify it fails.** `pytest tests/test_reproduce_pull.py::test_source_reproduce_passthrough -q` → FAIL (no attribute `reproduce`).

- [ ] **Step 3: Implement passthroughs.** In `source.py`, next to `execution_record`:

```python
    def reproduce(self, run_id: str) -> dict:
        """The server-assembled run reproduction record. Passthrough: the client is
        thin here on purpose — the backend is the one place that reads every piece
        together (research-os /reproduce)."""
        return self.client.run_reproduce(run_id)

    def experiment_reproduce(self, experiment_id: str, *, version: int | None = None) -> dict:
        return self.client.experiment_reproduce(experiment_id, version=version)
```

- [ ] **Step 4: Run tests, verify pass.** `pytest tests/test_reproduce_pull.py -q` → PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/probe/mcp/source.py tests/test_reproduce_pull.py
git commit -m "feat(mcp): source passthrough for run/experiment reproduce"
```

---

### Task 3: MCP run reproduce view delegates to the server

**Files:**
- Modify: `src/probe/mcp/service.py` (`_view_reproduce`, ~line 1466)
- Modify: `tests/test_mcp_views.py`

- [ ] **Step 1: Write the failing test.** In `tests/test_mcp_views.py` (uses the existing `_populated`/`client`/`app` helpers):

```python
def test_reproduce_view_delegates_to_server_record(client, app):
    rid = _populated(client, app)  # seeds env_ref, artifacts, execution record
    svc = _service(client)
    env = svc.get_entity(f"run:{rid}", view="reproduce")
    data = env["data"]
    # server-assembled fields, not the old hypothesis+config+env_ref triple
    assert "restore_command" in data
    assert "completeness" in data
    assert data["completeness"]["state"] in {"incomplete", "unverified"}


def test_reproduce_view_surfaces_completeness_missing_as_partial(client, app):
    # a run with no env_ref → server says missing execution_record → envelope partial
    project = client.create_project("folding")
    client.create_experiment("e", "e", hypothesis="h", project_id=project["id"])
    run = client.run(project="folding", experiment="e", name="bare")
    svc = _service(client)
    env = svc.get_entity(f"run:{run.id}", view="reproduce")
    assert env["state"] == "partial"
    assert "execution_record" in env["missing"]
    # advisories stay in the payload, never flip the envelope
    assert "launch_context" in env["data"]["completeness"]["advisories"]
```

- [ ] **Step 2: Run it, verify it fails.** `pytest tests/test_mcp_views.py -k reproduce -q` → FAIL (`restore_command` absent — old view returns hypothesis/config/env_ref).

- [ ] **Step 3: Rewrite `_view_reproduce`.** Replace the body:

```python
    def _view_reproduce(self, entity: dict, request: _Req) -> _ViewData:
        """The server-assembled reproduction record (research-os /reproduce), not a
        client-side re-derivation. Atomic: never truncated — a reproduction manifest
        with fields dropped reproduces nothing, so overflow is REPORTED by get_entity.

        The envelope's `missing` carries the server's `completeness.missing` verbatim
        (the reproduction-blocking gaps), so this view still reads `partial` when a
        run cannot be fully rebuilt. `advisories` stay in the payload: they are human
        judgment calls (no notes, no inputs decision) and legacy gaps, never a degraded
        response."""
        record = self.source.reproduce(str(entity["id"]))
        completeness = record.get("completeness") or {}
        return _ViewData(payload=record, missing=list(completeness.get("missing") or []))
```

- [ ] **Step 4: Run tests, verify pass.** `pytest tests/test_mcp_views.py -k reproduce -q` → PASS. Then the differentiation guard: `pytest tests/test_mcp_views.py -q` (the `reproduce != handoff` test must still hold — it does, the payloads differ structurally).

- [ ] **Step 5: Commit.**

```bash
git add src/probe/mcp/service.py tests/test_mcp_views.py
git commit -m "feat(mcp): run reproduce view delegates to server /reproduce"
```

---

### Task 4: MCP experiment-level reproduce view

**Files:**
- Modify: `src/probe/mcp/service.py` (`_VIEWS` ~line 504, `_VIEW_FILTERS` ~line 551, new `_view_experiment_reproduce` near `_view_versions` ~line 1625)
- Modify: `src/probe/mcp/server.py` (`get_entity` docstring view matrix, ~line 583)
- Modify: `tests/test_mcp_views.py`

- [ ] **Step 1: Write the failing test.**

```python
def test_experiment_reproduce_view_lists_run_summaries(client, app):
    rid = _populated(client, app)
    eid = app.runs[rid]["experiment_id"]
    svc = _service(client)
    env = svc.get_entity(f"experiment:{eid}", view="reproduce")
    data = env["data"]
    assert data["completeness"]["total"] >= 1
    assert any(r["reproduce_url"].endswith("/reproduce") for r in data["runs"])


def test_experiment_reproduce_view_accepts_version_filter(client, app):
    rid = _populated(client, app)
    eid = app.runs[rid]["experiment_id"]
    svc = _service(client)
    env = svc.get_entity(f"experiment:{eid}", view="reproduce", filters={"version": 3})
    assert env["data"]["resolved_version"] == 3


def test_experiment_reproduce_view_rejects_unknown_filter(client, app):
    rid = _populated(client, app)
    eid = app.runs[rid]["experiment_id"]
    svc = _service(client)
    with pytest.raises(errors.ValidationError):
        svc.get_entity(f"experiment:{eid}", view="reproduce", filters={"bogus": 1})
```

- [ ] **Step 2: Run it, verify it fails.** `pytest tests/test_mcp_views.py -k experiment_reproduce -q` → FAIL (view not in `_VIEWS`; `get_entity` raises "reproduce not available for experiment").

- [ ] **Step 3: Register the view + filter + builder.** In `_VIEWS` add:

```python
    (EntityType.EXPERIMENT, View.REPRODUCE): "_view_experiment_reproduce",
```

In `_VIEW_FILTERS` add:

```python
    # `version` pins the map against a minted experiment_versions manifest; applied
    # server-side by /v1/experiments/{id}/reproduce?version=N, not client-narrowed.
    (EntityType.EXPERIMENT, View.REPRODUCE): {"version"},
```

Add the builder next to `_view_versions`:

```python
    def _view_experiment_reproduce(self, entity: dict, request: _Req) -> _ViewData:
        """Per-run reproduction summaries across the experiment — a MAP, not N full
        assemblies. Each summary carries a `reproduce_url` for drill-down, so this stays
        one cheap read regardless of run count. `filters={"version": N}` pins against a
        frozen manifest; omitted reads live rows. Atomic + overflow-reported like the run
        view: summaries are compact, so this fits for any real experiment."""
        version = request.filters.get("version")
        record = self.source.experiment_reproduce(
            str(entity["id"]), version=int(version) if version is not None else None
        )
        return _ViewData(payload=record)
```

- [ ] **Step 4: Update the `get_entity` docstring matrix in `server.py`.** Change the experiment row from `experiment  card | artifacts | lineage | groups | versions` to include `reproduce`:

```
          experiment  card | artifacts | lineage | groups | versions | reproduce
```

- [ ] **Step 5: Run tests, verify pass.** `pytest tests/test_mcp_views.py -k experiment_reproduce -q` → PASS (3 tests). Then `pytest tests/test_mcp_schema_docs.py -q` (the docstring matrix is asserted there — if it checks the matrix, this keeps it green).

- [ ] **Step 6: Commit.**

```bash
git add src/probe/mcp/service.py src/probe/mcp/server.py tests/test_mcp_views.py
git commit -m "feat(mcp): experiment-level reproduce view with version pinning"
```

---

### Task 5: CLI — `probe run reproduce` (print + export) and `probe experiment` group

**Files:**
- Modify: `src/probe/cli/main.py` (`run_app` block ~line 2519; add a new `experiment_app` near the `project_app`/`token_app` app registrations)
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test.** Read `tests/test_cli.py` first for its invocation helper (it calls `main([...])` and captures stdout, faking `_client`/using the `app` fixture). Mirror that exactly. Add:

```python
def test_run_reproduce_prints_record(capsys, cli_env, app):
    rid, _ = _seed_run_cli(...)      # use the file's existing seeding helper
    assert main(["run", "reproduce", rid]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"]["id"] == rid
    assert "restore_command" in out


def test_run_reproduce_export_writes_bundle(tmp_path, cli_env, app):
    rid, _ = _seed_run_cli(...)
    dest = tmp_path / "repro.json"
    assert main(["run", "reproduce", rid, "--export", str(dest)]) == 0
    assert json.loads(dest.read_text())["run"]["id"] == rid


def test_experiment_reproduce_prints_summaries(capsys, cli_env, app):
    _, eid = _seed_run_cli(...)
    assert main(["experiment", "reproduce", eid]) == 0
    assert "runs" in json.loads(capsys.readouterr().out)


def test_experiment_freeze_mints_version(capsys, cli_env, app):
    _, eid = _seed_run_cli(...)
    assert main(["experiment", "freeze", eid, "--label", "v1"]) == 0
    # freeze delegates to POST /v1/experiments/{id}/versions
```

- [ ] **Step 2: Run it, verify it fails.** `pytest tests/test_cli.py -k "reproduce or freeze" -q` → FAIL (`No such command 'reproduce'`).

- [ ] **Step 3: Add the `run reproduce` command** in the `run_app` block:

```python
@run_app.command("reproduce")
def run_reproduce(
    run: str = run_ref(),
    export: str = typer.Option(None, "--export", help="write the record as a portable JSON bundle (a rendering, not the source of truth)"),
) -> None:
    """Pull the server-assembled reproduction record for a run."""
    with _client() as c:
        record = c.run_reproduce(run)
    if export:
        Path(export).write_text(json.dumps(record, indent=2, sort_keys=True))
        typer.echo(f"wrote {export}")
    else:
        _print_json(record)
```

- [ ] **Step 4: Add the `experiment` Typer app** near the other `app.add_typer(...)` registrations:

```python
experiment_app = typer.Typer(no_args_is_help=True, help="experiments — reproduce and freeze")
app.add_typer(experiment_app, name="experiment")


@experiment_app.command("reproduce")
def experiment_reproduce(
    experiment: str = typer.Argument(...),
    version: int = typer.Option(None, "--version", help="pin against a minted experiment version"),
) -> None:
    """Pull per-run reproduction summaries for an experiment (a map; drill into a run with `run reproduce`)."""
    with _client() as c:
        _print_json(c.experiment_reproduce(experiment, version=version))


@experiment_app.command("freeze")
def experiment_freeze(
    experiment: str = typer.Argument(...),
    label: str = typer.Option(None, "--label"),
) -> None:
    """Mint an immutable experiment version (a launch-time manifest of the run set)."""
    with _client() as c:
        _print_json(c.experiment_version(experiment, label=label))
```

Ensure `from pathlib import Path` and `import json` are already imported at the top (they are — `_json_value`/`_print_json` use them).

- [ ] **Step 5: Run tests, verify pass.** `pytest tests/test_cli.py -k "reproduce or freeze" -q` → PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/probe/cli/main.py tests/test_cli.py
git commit -m "feat(cli): run reproduce (+export) and experiment reproduce/freeze"
```

---

### Task 6: CLI — `probe run reproduce --materialize DIR`

**Files:**
- Modify: `src/probe/cli/main.py` (`run_reproduce` command from Task 5)
- Modify: `tests/test_cli.py`

Read `src/probe/cli/main.py:snapshot_restore` (~line 5115) and `src/probe/sdk/restore.py:restore_snapshot` first — `--materialize` reuses that path, then writes lockfiles and inputs-decision contents alongside.

- [ ] **Step 1: Write the failing test.**

```python
def test_run_reproduce_materialize_writes_tree(tmp_path, cli_env, app, monkeypatch):
    rid, _ = _seed_run_cli(...)  # seed a run whose record carries a code_snapshot + a lockfile + inputs_decision
    dest = tmp_path / "work"
    assert main(["run", "reproduce", rid, "--materialize", str(dest)]) == 0
    # lockfiles + inputs-decision contents land on disk next to the restored tree
    assert (dest / "reproduce-manifest.json").exists()
```

- [ ] **Step 2: Run it, verify it fails.** `pytest tests/test_cli.py -k materialize -q` → FAIL (`--materialize` unknown option).

- [ ] **Step 3: Extend `run_reproduce`** with a `--materialize` option that, when set: calls the existing snapshot-restore path for the code snapshot (delegate to the same helper `snapshot_restore` uses, do NOT duplicate its logic), writes each `lockfiles[]` entry and each `inputs_decision[].content` (when not omitted) into `DIR`, and drops the full record as `DIR/reproduce-manifest.json`. Reference, not silently-omitted: any inputs-decision whose `content_omitted_reason` is set is written as a `.omitted` marker naming the reason, never skipped.

```python
    materialize: str = typer.Option(None, "--materialize", help="reconstruct a runnable directory: restore the code snapshot + write lockfiles and inputs"),
```

(Full body: guard that `--export` and `--materialize` are not both set; on `--materialize`, `record = c.run_reproduce(run)`, then restore + write files, then `_print_json` a short summary of what was written.)

- [ ] **Step 4: Run tests, verify pass.** `pytest tests/test_cli.py -k "reproduce or freeze or materialize" -q` → PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/probe/cli/main.py tests/test_cli.py
git commit -m "feat(cli): run reproduce --materialize reconstructs a runnable tree"
```

---

### Task 7: Skills — snapshot-steps become verify-steps + claim gate

**Files:**
- Modify: `skills/start-research-work/SKILL.md`
- Modify: `skills/track-research-work/SKILL.md`
- Modify: `skills/capture-run-inputs/SKILL.md`
- Then: `make sync-plugin-skills` (updates `plugins/probe-research/skills/*`)
- Test: `tests/test_skills_sync.py`

Read all three canonical SKILL.md files first — edit surgically, matching each file's existing voice and structure. Do not rewrite them.

- [ ] **Step 1: start-research-work.** Change the snapshot step from an *action* to a *verify*: `probe exec` and the SDK `run()` now auto-snapshot, so the step is "confirm capture with `probe run check`; snapshot explicitly ONLY when launching outside `probe exec`/SDK." Keep recording `foreign_keys` (W&B, scheduler job, pod, storage) at launch mandatory.

- [ ] **Step 2: track-research-work.** Add the three-part update from design D6:
  1. Record decisions / user overrides / tools-behaving-differently as notes *when they happen* — name the new `launch`/determinism context as things worth annotating when surprising.
  2. **Claim gate:** before reporting a run done/handoff-ready, run `probe run check` and state the verdict verbatim; if `incomplete`, fix it or say why not in the handoff note. Machine-checkable via exit code, not prose.
  3. At experiment completion/publication: `probe experiment freeze`.
  Add: to pull the full record for a handoff or a question about a past run, use `probe run reproduce RUN` (or MCP `get_entity view="reproduce"`), and `probe experiment reproduce EXP` for the set.

- [ ] **Step 3: capture-run-inputs.** Drop lockfiles from the manual checklist (captured automatically now). Datasets, checkpoints, out-of-tree configs, and the `inputs-decision.json` artifact remain its job. Core otherwise unchanged.

- [ ] **Step 4: Sync the plugin copies.** `make sync-plugin-skills`

- [ ] **Step 5: Run the sync + any skill tests.** `pytest tests/test_skills_sync.py -q` → PASS (byte-equal copies).

- [ ] **Step 6: Commit.**

```bash
git add skills/ plugins/probe-research/skills/
git commit -m "docs(skills): reproduce pull + claim gate; snapshot steps become verify steps"
```

---

## Final verification (after all tasks)

- [ ] Full suite: `pytest -q` (Docker not required for the agent repo — FakeApp is in-memory).
- [ ] Lint/type per repo convention (check `Makefile`: `make test` / `ruff` / `mypy` as configured).
- [ ] **Live smoke (standing rule — pytest alone is not enough for a cross-repo pull surface):** boot research-os locally against Docker Postgres at `0.122.0.0`, capture a run via `probe exec`, then: `probe run reproduce RUN`, `probe experiment reproduce EXP`, and MCP `get_entity view="reproduce"` all return the assembled record; a legacy run (no launch) returns `incomplete` with `launch_context` advisory, never an error.
- [ ] Version bump + CHANGELOG per repo release convention; open the PR.

## Self-review notes (author)

- **Spec coverage:** D5 MCP (run delegate ✓ Task 3, experiment view ✓ Task 4), D5 CLI (`run reproduce` ✓ T5, `--export` ✓ T5, `--materialize` ✓ T6, `experiment reproduce` ✓ T5, `experiment freeze` ✓ T5), D6 skills (all three ✓ T7). Client read methods (prereq, not explicit in D5) ✓ T1–T2.
- **Type consistency:** `run_reproduce`/`experiment_reproduce` names identical across client/source/CLI. Server field names copied verbatim from `reproduce.py`.
- **Open items to resolve during execution:** (a) confirm the exact `_seed_run_cli`/`cli_env` helper names in `test_cli.py` before writing Task 5/6 tests; (b) confirm `test_mcp_schema_docs.py` asserts the `get_entity` matrix string (Task 4 Step 5) — if it parses the matrix, keep the spacing it expects; (c) `--materialize` reuse of the restore helper must not duplicate `snapshot_restore`'s body.
