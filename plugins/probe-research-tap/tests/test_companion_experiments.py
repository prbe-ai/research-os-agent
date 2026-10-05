"""The daemon addresses an experiment as an experiment (light experiments X21b).

The worker reads and writes what a session acted on. An experiment's id was a
project id while its leaf row existed, so the worker used `/v1/projects/{E}`
for it -- the leaf door, which answers from adapters until R6 removes it. Every
project-typed id is now PLACED with `GET /v1/scopes/{id}` and an experiment is
read and written on the experiment API (`/v1/projects/{P}/experiments/{E}` and
its `/sub-notes`, `/artifacts`, `/artifacts/uploads`); a project keeps its
address. These pin that no request reaches the leaf door for an experiment.
"""

from __future__ import annotations

import re

import pytest

from tap import companion_ledger as ledger_mod
from tap import companion_worker as worker_mod

from .test_companion import (  # noqa: F401 - `_state` is the autouse fixture that isolates XDG_STATE_HOME
    PROJECT,
    RUN,
    FakeApi,
    _assistant,
    _claude_lines,
    _live_worker,
    _state,
    _tool_result,
    _tool_use,
)
from .test_companion_conclusions import _scope

EXP = "e0e00000-0000-4000-8000-0000000000e1"
HOME = "b0b00000-0000-4000-8000-0000000000b1"
BASE = f"/v1/projects/{HOME}/experiments/{EXP}"
LEAF_DOOR = re.compile(rf"^/v1/projects/{EXP}(/|$)")


class Recording(FakeApi):
    """A FakeApi that remembers every path it was asked for, reads and writes."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.paths: list[str] = []

    def get(self, path, **params):
        self.paths.append(path)
        return super().get(path, **params)

    def request(self, method, path, body=None, **kw):
        self.paths.append(path)
        return super().request(method, path, body, **kw)

    def leaf_door_hits(self) -> list[str]:
        return [p for p in self.paths if LEAF_DOOR.match(p)]


def _experiment_api(**extra) -> Recording:
    return Recording(
        entities={
            **_scope(EXP, kind="experiment", project=HOME),
            BASE: {"id": EXP, "project_id": HOME, "slug": "svm-vs-lr", "name": "SVM vs LR",
                   "question": "Does an RBF SVM beat logistic regression on digits?",
                   "notes": None, "notes_version": 0},
            f"/v1/runs/{RUN}": {"id": RUN, "name": "best", "status": "completed", "tags": [],
                                "project_id": HOME, "experiment_id": EXP},
            **extra.pop("entities", {}),
        },
        lists={
            f"{BASE}/sub-notes": {"sub_notes": [], "limit_count": 20},
            f"{BASE}/artifacts": [],
            f"/v1/projects/{HOME}/papers": [],
            f"/v1/runs/{RUN}/artifacts": [],
            **extra.pop("lists", {}),
        },
    )


def _decider(api, tmp_path, **overrides):
    kwargs = dict(known_ids={EXP: "project", RUN: "run"}, run_starts={RUN}, run_ends=set(), cwd=tmp_path,
                  touched=set(), chunk_text="", range_start=0, range_end=1000, agent_ranges=())
    kwargs.update(overrides)
    return worker_mod.Decider(api, **kwargs)


def _raw(kind, target_id=EXP, **fields):
    return {"kind": kind, "target": {"type": "project", "id": target_id}, "evidence": {"from": 10, "to": 20},
            **fields}


def _proposal(op, attempts=0):
    return ledger_mod.Proposal(id=1, idem_key="pc1-e", kind="note", target_type="project", target_id=EXP,
                               payload={}, evidence={"_op": list(op)}, status="pending", reason=None,
                               attempts=attempts)


def test_notes_on_an_experiment_go_to_the_experiment_api(tmp_path):
    api = _experiment_api()
    decider = _decider(api, tmp_path)
    sub = decider.decide(_raw("note", title="PCA verdict", body="PCA to 32 dims costs 0.4 points"))
    assert sub["op"] == ("POST", f"{BASE}/sub-notes", {"title": "companion: PCA verdict",
                                                        "body": "PCA to 32 dims costs 0.4 points"})
    main = decider.decide(_raw("note", main=True, title="Result", body="## Result\nSVM 0.9889 vs 0.9603"))
    assert main["op"] == ("NOTES", BASE, {"title": "Result", "body": "## Result\nSVM 0.9889 vs 0.9603"})
    assert api.paths[0] == f"/v1/scopes/{EXP}"  # placed once, first
    assert api.paths.count(f"/v1/scopes/{EXP}") == 1
    assert api.leaf_door_hits() == []


def test_publishing_an_experiments_main_document_sends_no_op_key(tmp_path):
    """The experiment API's PATCH body is closed: `op_key` would be a 422. The
    retry rides the Idempotency-Key header, as it always did."""
    op = ("NOTES", BASE, {"title": "Result", "body": "## Result\nx"})
    empty = _experiment_api()
    worker_mod.publish(empty, _proposal(op))
    method, path, body, key = empty.writes[-1]
    assert (method, path, body, key) == ("PATCH", BASE, {"notes": "## Result\nx", "base_version": 0}, "pc1-e")
    # Filled meanwhile: the fallback is a titled sub-note on the experiment's twin.
    filled = _experiment_api(entities={BASE: {"id": EXP, "project_id": HOME, "notes": "someone's",
                                              "notes_version": 3}})
    worker_mod.publish(filled, _proposal(op))
    assert filled.writes[-1][:2] == ("POST", f"{BASE}/sub-notes")
    assert empty.leaf_door_hits() == filled.leaf_door_hits() == []


def _project_api() -> Recording:
    return Recording(entities={**_scope(PROJECT), f"/v1/projects/{PROJECT}": {"id": PROJECT, "notes": None,
                                                                             "notes_version": 0}},
                     lists={f"/v1/projects/{PROJECT}/sub-notes": {"sub_notes": [], "limit_count": 20}})


def test_a_projects_main_document_keeps_its_address_and_its_op_key(tmp_path):
    decider = _decider(_project_api(), tmp_path, known_ids={PROJECT: "project"})
    out = decider.decide(_raw("note", target_id=PROJECT, main=True, title="Result", body="## Result\nx"))
    assert out["op"][:2] == ("NOTES", f"/v1/projects/{PROJECT}")
    assert decider.decide(_raw("note", target_id=PROJECT, title="t", body="b"))["op"][1] == (
        f"/v1/projects/{PROJECT}/sub-notes"
    )
    api = _project_api()  # the decider marked its cached row filled; publish reads afresh
    worker_mod.publish(api, _proposal(out["op"]))
    assert api.writes[-1][2] == {"notes": "## Result\nx", "base_version": 0, "op_key": "pc1-e"}


def test_a_note_an_older_tap_stored_at_the_leaf_door_still_publishes_there(tmp_path):
    """Legacy form, kept until R6: a persisted op is never rewritten."""
    api = Recording(entities={f"/v1/projects/{EXP}": {"id": EXP, "notes": None, "notes_version": 0}})
    worker_mod.publish(api, _proposal(("NOTES", f"/v1/projects/{EXP}", {"title": "R", "body": "x"})))
    assert api.writes[-1][:3] == ("PATCH", f"/v1/projects/{EXP}", {"notes": "x", "base_version": 0,
                                                                   "op_key": "pc1-e"})


def test_an_experiments_description_and_tags_are_held(tmp_path):
    api = _experiment_api()
    decider = _decider(api, tmp_path)
    # Its description is its question: a describe carrying only that writes nothing.
    with pytest.raises(worker_mod.Held, match="already filled"):
        decider.decide(_raw("describe", description="what it is"))
    with pytest.raises(worker_mod.Held, match="tags are read-only"):
        decider.decide(_raw("tag", tags=["svm"]))
    assert not api.writes and api.leaf_door_hits() == []


def test_an_experiment_still_named_by_its_slug_is_named_on_the_experiment_api(tmp_path):
    """Its NAME is the daemon's to give while it reads as its slug, as at the
    leaf door; the description is dropped and the PATCH goes to the experiment."""
    api = _experiment_api()
    api.entities[BASE] = {**api.entities[BASE], "name": "svm-vs-lr"}
    out = _decider(api, tmp_path).decide(_raw("describe", name="SVM vs LR", description="what it is"))
    assert out["op"] == ("DESCRIBE", BASE, {"name": "SVM vs LR", "authored_by": "agent"})
    worker_mod.publish(api, _proposal(out["op"]))
    assert api.writes[-1][:3] == ("PATCH", BASE, {"name": "SVM vs LR", "authored_by": "agent"})
    assert api.leaf_door_hits() == []


def test_a_paper_proposed_on_an_experiment_is_recorded_on_its_project(tmp_path):
    url = "https://arxiv.org/abs/1234.5678"
    api = _experiment_api()
    out = _decider(api, tmp_path, chunk_text=f"read {url}").decide(_raw("paper", title="SVMs", source_url=url))
    assert out["op"] == ("POST", f"/v1/projects/{HOME}/papers", {"title": "SVMs", "source_url": url})
    assert out["target_id"] == EXP  # the proposal is still the model's; only the write moves
    assert f"/v1/projects/{HOME}/papers" in api.paths and api.leaf_door_hits() == []
    # Recorded already on the project: held, read where it is.
    api.lists[f"/v1/projects/{HOME}/papers"] = [{"source_url": url}]
    with pytest.raises(worker_mod.Held, match="already recorded"):
        _decider(api, tmp_path, chunk_text=f"read {url}").decide(_raw("paper", title="SVMs", source_url=url))


def test_a_file_for_an_experiment_uploads_to_the_experiments_files(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "table.md").write_text("| model | cv |\n| svm | 0.9889 |\n")
    api = _experiment_api()
    out = _decider(api, tmp_path, cwd=work, produced_text="wrote table.md").decide(
        _raw("artifact", path="table.md")
    )
    assert out["op"][:2] == ("UPLOAD", f"{BASE}/artifacts/uploads")
    assert "kind" not in out["payload"]  # a run-only field
    assert f"{BASE}/artifacts" in api.paths  # the dedupe read, on the experiment's files
    assert api.leaf_door_hits() == []


def test_an_id_the_scope_read_cannot_place_is_held(tmp_path):
    api = Recording(entities={})  # /v1/scopes/{EXP} answers 404
    with pytest.raises(worker_mod.Held, match=rf"read refused \(404\): /v1/scopes/{EXP}"):
        _decider(api, tmp_path).decide(_raw("note", title="t", body="b"))
    run_scope = Recording(entities={f"/v1/scopes/{EXP}": {"id": EXP, "kind": "run", "project_id": HOME}})
    with pytest.raises(worker_mod.Held, match="not a project or an experiment"):
        _decider(run_scope, tmp_path).decide(_raw("note", title="t", body="b"))
    assert api.leaf_door_hits() == run_scope.leaf_door_hits() == []


def test_the_session_facts_read_an_experiment_on_the_experiment_api(tmp_path):
    swept = "5eeb0000-0000-4000-8000-0000000000e2"
    api = _experiment_api(
        entities={f"/v1/runs/{swept}": {"id": swept, "name": "svm-C1", "project_id": HOME, "experiment_id": EXP,
                                        "tags": [], "created_at": "2100-01-01T00:00:00Z"}},
        lists={f"{BASE}/sub-notes": {"sub_notes": [{"title": "companion: PCA verdict"}], "limit_count": 20},
               f"/v1/runs/{swept}/artifacts": []},
    )
    body = _claude_lines(
        _tool_use("c1", f"probe exec --project {EXP} -- python run.py"),
        _tool_result("c1", f"CV 0.9889\nprobe run: https://x/runs/{RUN}"),
        _tool_use("c2", "python sweep.py"),
        _tool_result("c2", f"svm-C1: CV 0.97\nprobe run: https://x/runs/{swept}"),
        _assistant("The SVM wins."),
    )
    worker, _ = _live_worker(tmp_path, api, body)
    worker.cycle(shadow=False)
    mentioned = worker._session_view(len(body)).mentioned
    entities, _files, _shown, admitted, notes = worker._session_entities(mentioned)
    experiment = next(e for e in entities if e["id"] == EXP)
    assert experiment["kind"] == "experiment" and experiment["parent_project_id"] == HOME
    assert experiment["main_document"] == "(empty)"
    assert "SVM vs LR: companion: PCA verdict" in notes  # its sub-notes, from the twin
    # A run the sweep printed sits in the experiment (new shape: project_id is its
    # project, experiment_id the experiment): admitted.
    assert admitted == {swept: "run"}
    assert api.leaf_door_hits() == []
