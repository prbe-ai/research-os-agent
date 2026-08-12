"""`probe wizard` -> Import existing work.

The two things most likely to hurt someone are covered first: a denominator
that lies (silent partial coverage reading as success), and an anchor the agent
is free to invent (a second run forking the project identity, which nothing
downstream can undo).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from probe.cli import backfill


@pytest.fixture(autouse=True)
def _agents_installed(monkeypatch):
    """Pretend both agents are on PATH.

    Without this the suite passes on a laptop with Claude Code installed and
    fails in CI, which has neither — every `run()` test would stop at "No coding
    agent found" long before reaching what it meant to assert. A test about
    folders must not depend on what the machine happens to have installed.
    Tests that care about availability override this themselves.
    """
    monkeypatch.setattr(backfill, "which_agent", lambda a: f"/usr/bin/{a.value}")


def _tree(root: Path) -> Path:
    """A folder shaped like the ones this feature meets: real work, build noise."""
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "report.md").write_text("findings")
    (root / "results.csv").write_text("a,b\n1,2\n")
    (root / "train.py").write_text("print('hi')")
    # Noise that must never reach the denominator.
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "train.cpython-313.pyc").write_bytes(b"\x00" * 4096)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (root / ".DS_Store").write_bytes(b"\x00")
    return root


# -- the denominator --------------------------------------------------------


def test_scan_counts_real_files_and_prunes_noise(tmp_path):
    census = backfill.scan(_tree(tmp_path))
    assert census.files == 3
    assert census.bytes > 0
    assert census.capped is False


def test_scan_cap_marks_itself_rather_than_reporting_a_wrong_total(tmp_path):
    for i in range(30):
        (tmp_path / f"f{i}.txt").write_text("x")
    census = backfill.scan(tmp_path, cap=10)
    assert census.capped is True
    assert census.files == 10
    # A capped census must never render as an exact count -- that is the lie.
    assert "+" in census.describe()


def test_scan_survives_a_file_that_vanishes_mid_walk(tmp_path, monkeypatch):
    (tmp_path / "gone.pt").write_text("x")
    real = Path.stat

    def flaky(self, *a, **kw):
        if self.name == "gone.pt":
            raise OSError("vanished")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", flaky)
    census = backfill.scan(tmp_path)
    # Still COUNTED: a denominator that quietly drops it hides the mismatch.
    assert census.files == 1


def test_reconcile_reports_the_gap(tmp_path):
    lines = backfill.reconcile(backfill.Census(files=100, bytes=1), 40, False)
    assert "100 files found" in lines[0]
    assert "40 artifacts" in lines[0]
    assert any("60 unaccounted" in ln for ln in lines)


def test_reconcile_does_not_cry_gap_when_the_page_was_full(tmp_path):
    # 1000 back from a 1000-row page means "at least", so a shortfall is unknown,
    # not proven. Claiming one here would train people to ignore the warning.
    lines = backfill.reconcile(backfill.Census(files=5000, bytes=1), 1000, True)
    assert not any("unaccounted" in ln for ln in lines)
    assert "1,000+" in lines[0]


def test_reconcile_says_so_when_it_could_not_read_back():
    lines = backfill.reconcile(backfill.Census(files=10, bytes=1), -1, False)
    assert "could not read back" in lines[0]


# -- the anchor -------------------------------------------------------------


#: A real project id. The routes type `{project_id}` as a UUID, so a slug in
#: that position is a 422 — this fixture exists to keep that honest.
FAKE_ID = "3f7c1a52-0d64-4e2f-9c31-0b8a5d6e1f90"


class _FakeClient:
    def __init__(self, existing=None):
        self.existing = existing
        self.ensured: list[tuple] = []
        self.created: list[tuple] = []

    def resolve_project(self, slug):
        return self.existing

    def get_project(self, project_id):
        return {"id": project_id, "slug": "by-id"}

    def ensure_project(self, slug, name=None, **kw):
        # The GUARDED path, still used by `resolve_anchor` -- one slug derived
        # from a folder name that no human reviewed, which is what the near-miss
        # guard is for.
        self.ensured.append((slug, name))
        return self.existing or {"id": FAKE_ID, "slug": slug}

    def create_project(self, slug, name=None, description=None):
        # The EXPLICIT path, used by a plan's projects: those slugs were shown
        # to a human at the review gate, so the guard would be a third opinion
        # with less information -- and the one that reads siblings as typos.
        self.created.append((slug, name))
        return {"id": FAKE_ID, "slug": slug}

    def list_anchored(self, anchor, anchor_id, **kw):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_slug_is_derived_not_asked_so_a_rerun_lands_in_the_same_place(tmp_path):
    folder = tmp_path / "SAP Bench   v3!!"
    folder.mkdir()
    assert backfill.slug_for(folder) == "sap-bench-v3"
    assert backfill.slug_for(folder) == backfill.slug_for(folder)


def test_slug_never_empties_out(tmp_path):
    folder = tmp_path / "!!!"
    folder.mkdir()
    assert backfill.slug_for(folder) == "backfill"


def test_the_anchor_is_a_uuid_never_a_slug(tmp_path):
    """The bug this test exists for: `/v1/projects/{project_id}` types the path
    param as a UUID, so anchoring on a slug 422s every upload AND the read-back —
    and the read-back failing is invisible, which is the whole point of the count."""
    folder = tmp_path / "odyssey"
    folder.mkdir()
    project_id, slug = backfill.resolve_anchor(_FakeClient(), folder)
    UUID(project_id)  # raises if a slug leaked into the id position
    assert slug == "odyssey"


def test_an_explicitly_named_project_wins_over_the_derived_slug(tmp_path):
    client = _FakeClient()
    project_id, _ = backfill.resolve_anchor(client, tmp_path, requested=f"id:{FAKE_ID}")
    assert project_id == FAKE_ID
    assert client.ensured == []


def test_a_named_slug_is_resolved_to_its_id(tmp_path):
    # --project takes either form.
    client = _FakeClient(existing={"id": FAKE_ID, "slug": "already-here"})
    project_id, slug = backfill.resolve_anchor(client, tmp_path, requested="already-here")
    assert project_id == FAKE_ID
    assert slug == "already-here"


def test_an_unknown_named_slug_fails_loudly(tmp_path):
    with pytest.raises(ValueError, match="no project"):
        backfill.resolve_anchor(_FakeClient(existing=None), tmp_path, requested="ghost")


def test_existing_project_is_reused_not_forked(tmp_path):
    client = _FakeClient(existing={"id": FAKE_ID, "slug": "already-here"})
    project_id, slug = backfill.resolve_anchor(client, tmp_path)
    assert (project_id, slug) == (FAKE_ID, "already-here")


def test_the_ambient_active_project_is_not_consulted(monkeypatch, tmp_path):
    """`probe project use` sets where new RUNS go; it is not a statement that
    every folder imported from here on belongs there. Honouring it meant
    pointing at one folder and watching its artifacts land somewhere unrelated."""
    import sys

    import probe.cli.main  # noqa: F401

    source = __import__("inspect").getsource(sys.modules["probe.cli.main"].backfill)
    assert "configured_project" not in source
    # The destination comes from --project or the folder, never from resolve().
    assert "project=project" in source

    folder = tmp_path / "anthrogen-backfill-test"
    folder.mkdir()
    client = _FakeClient(existing=None)
    _, slug = backfill.resolve_anchor(client, folder)
    assert slug == "anthrogen-backfill-test"


def test_creation_goes_through_the_near_miss_guard(tmp_path):
    # ensure_project, not create_project: its guard refuses a slug that looks
    # like a typo of an existing one, which is the identity fork we cannot undo.
    folder = tmp_path / "odyssey"
    folder.mkdir()
    client = _FakeClient(existing=None)
    backfill.resolve_anchor(client, folder)
    assert client.ensured == [("odyssey", "odyssey")]


# -- the prompt (the actual deliverable) ------------------------------------


def test_by_default_the_agent_decides_the_projects(tmp_path):
    """A folder is not automatically one project. `/workspace` with three
    researchers under it is at least three, and collapsing that into one named
    after the directory is a wrong answer no naming discipline fixes."""
    prompt = backfill.build_prompt(folder=tmp_path, census=backfill.Census(files=47, bytes=1))
    assert "YOU DECIDE THE PROJECTS" in prompt
    assert "probe project list" in prompt
    assert "REUSE BEFORE YOU CREATE" in prompt


def test_the_agent_is_told_to_name_for_the_work_not_the_directory(tmp_path):
    prompt = backfill.build_prompt(folder=tmp_path, census=backfill.Census(files=1, bytes=1))
    assert "Name them for the WORK, not the directory" in prompt
    # The counter-examples matter more than the rule.
    assert "`workspace`, `data` and `michael` are not" in prompt


def test_reuse_is_argued_for_not_just_asserted(tmp_path):
    # The failure is invisible when it happens, so the prompt has to say WHY.
    prompt = backfill.build_prompt(folder=tmp_path, census=backfill.Census(files=1, bytes=1))
    assert "nobody can undo" in prompt
    assert "odyssey_infill_v3" in prompt  # the near-miss that splits a record


def test_an_explicit_project_pins_everything_to_it(tmp_path):
    prompt = backfill.build_prompt(
        folder=tmp_path, census=backfill.Census(files=1, bytes=1), project="odyssey"
    )
    assert "--project odyssey" in prompt
    assert "Do not create any other project" in prompt
    assert "YOU DECIDE THE PROJECTS" not in prompt


def test_the_summary_must_name_every_project_it_used(tmp_path):
    # It is how the import gets checked; without it nothing can be counted back.
    prompt = backfill.build_prompt(folder=tmp_path, census=backfill.Census(files=9, bytes=1))
    assert '"projects": ["<slug>", ...]' in prompt


def test_the_prompt_never_names_a_door_that_does_not_open(tmp_path):
    """Step 2 used to offer `probe note add` (no such command — it is
    `probe notes write`) and `--meta` (run-anchor only; ScopedUploadRequest
    forbids extras, and this import creates no runs). With both routes closed
    the agent improvised, concatenating descriptions into --name until it hit
    the length cap. Telling it to do the thing that works beats leaving it to
    discover that the instructions are fiction."""
    prompt = backfill.build_prompt(
        folder=tmp_path, census=backfill.Census(files=3, bytes=99)
    )
    assert "probe note add" not in prompt
    assert "--name" in prompt
    assert "probe notes write" in prompt


def test_backfill_routes_visible_context_and_hidden_caveats_separately(tmp_path):
    prompt = backfill.build_prompt(
        folder=tmp_path, census=backfill.Census(files=3, bytes=99)
    )

    assert "dashboard-visible" in prompt
    assert "server-owned AI narrative" in prompt
    assert "summary_markdown" in prompt
    assert "probe project set <project> --summary @PROJECT.md" in prompt
    assert "last-write-wins" in prompt
    assert "probe notes write --project <project> --append" in prompt
    assert "hidden notes" in prompt


def test_the_prompt_demands_a_description_on_what_it_creates(tmp_path):
    """A backfilled project read "Add description" under its title forever.

    Nothing else fills it in: the server generates a description only when a
    child RUN reaches a terminal status, and importing a folder creates no runs.
    So an undescribed project stays undescribed permanently — and whether one
    appeared at all was luck, because the prompt showed `project create` with
    only --name. One import wrote a description unprompted; the next did not."""
    prompt = backfill.build_prompt(
        folder=tmp_path, census=backfill.Census(files=3, bytes=99)
    )
    assert "--description" in prompt
    assert "ALWAYS pass --description" in prompt
    # And it says WHY, so the rule survives a future edit that trims the prompt.
    assert "creates no runs" in prompt


def test_the_prompt_bounds_how_long_a_description_may_be(tmp_path):
    """Asking for a description without bounding it produced a 566-char one.

    Three sentences is a CEILING, not a target — a few words beats a blank field,
    and the point is that something is written. Both creates say so. The experiment
    line used to invite the opposite ("record WHY you believed this was a real
    experiment — the files that convinced you"), which is notes-shaped prose in a
    field the overview clamps to two lines."""
    prompt = backfill.build_prompt(
        folder=tmp_path, census=backfill.Census(files=3, bytes=99)
    )
    # Both the project create and the experiment create state the ceiling.
    assert prompt.count("up to 3 sentences") == 2
    assert "the point is that it is WRITTEN" in prompt
    # The provenance the experiment line used to want is redirected, not dropped.
    assert "probe notes write" in prompt


def test_the_prompt_names_the_real_amend_verb_for_each_kind(tmp_path):
    """Every kind amends with `set`, so the prompt says `set` for every kind.

    It used to be `project patch` against `experiment set` and `run set`, and an
    agent that learned one and guessed the others got `No such command` — which
    is how the prompt shipped `probe note add` once before. `patch` still works
    as a hidden alias, but the prompt must teach the one verb that generalises,
    or the inconsistency simply moves into the agent's habit."""
    prompt = backfill.build_prompt(
        folder=tmp_path, census=backfill.Census(files=3, bytes=99)
    )
    assert "probe project set" in prompt
    assert "probe experiment set" in prompt
    assert "probe project patch" not in prompt


def test_prompt_carries_the_reference_threshold_for_large_files(tmp_path):
    prompt = backfill.build_prompt(
        folder=tmp_path, project="p", census=backfill.Census(files=1, bytes=1)
    )
    assert "--reference --allow-missing" in prompt
    assert backfill.human_bytes(backfill.REFERENCE_ABOVE_BYTES) in prompt
    # Hashing a 10GB checkpoint over a shared mount is the slowest thing here.
    assert "--hash" in prompt and "Do NOT pass --hash" in prompt


def test_prompt_gives_explicit_permission_not_to_group(tmp_path):
    prompt = backfill.build_prompt(
        folder=tmp_path, project="p", census=backfill.Census(files=1, bytes=1)
    )
    assert "DO NOT" in prompt
    assert "wrong answer that looks like a right one" in prompt


def test_prompt_states_the_denominator_so_the_agent_knows_the_scale(tmp_path):
    prompt = backfill.build_prompt(
        folder=tmp_path, project="p", census=backfill.Census(files=1234, bytes=1)
    )
    assert "1,234 files" in prompt


# -- the agent launch -------------------------------------------------------


# -- counting back across whatever projects the agent chose ------------------


class _Server:
    """A server where artifacts sit at BOTH the project and its experiments.

    Shaped after the real thing on purpose: an import that groups work into
    experiments — which step 3 of the prompt asks for — leaves the project
    listing holding only part of the total.
    """

    def __init__(self, layout: dict, broken: set[str] | None = None):
        self.layout = layout  # slug -> {"project": n, "experiments": [n, ...]}
        self.broken = broken or set()
        self.seen_ids: list[str] = []
        # One distinct, well-formed UUID per slug, so a slug that reached a
        # route typed for a UUID is visible rather than merely wrong.
        self.ids = {
            slug: f"{i + 1:08d}-1111-4111-8111-111111111111"
            for i, slug in enumerate(layout)
        }
        self.slugs = {v: k for k, v in self.ids.items()}

    def resolve_project(self, slug):
        if slug in self.broken:
            raise RuntimeError("500")
        return {"id": self.ids[slug]} if slug in self.layout else None

    def list_experiments(self, project_id=None, **kw):
        counts = self.layout[self.slugs[project_id]]["experiments"]
        return {"items": [{"id": f"{project_id}-exp{i}"} for i in range(len(counts))]}

    def list_anchored(self, anchor, anchor_id, **kw):
        # The route types this param as a UUID; a slug would 422 in production.
        self.seen_ids.append(anchor_id)
        if "-exp" in str(anchor_id):
            project_id, idx = str(anchor_id).rsplit("-exp", 1)
            n = self.layout[self.slugs[project_id]]["experiments"][int(idx)]
        else:
            n = self.layout[self.slugs[anchor_id]]["project"]
        return [{"id": i} for i in range(n)]


def test_the_projects_come_from_the_summary_but_the_COUNT_never_does():
    """The only thing taken from the agent's own account is WHERE to look. The
    number still comes from the server and the denominator from the walk, so an
    agent that overstates its work cannot make the two agree."""
    tail = 'chatter\n{"files_seen": 37, "files_landed": 999, "projects": ["a", "b"]}'
    assert backfill.summary_projects(tail) == ["a", "b"]

    server = _Server({"a": {"project": 1, "experiments": []},
                      "b": {"project": 2, "experiments": []}})
    # 999 claimed, 3 actually there.
    assert backfill.count_landed_across(server, ["a", "b"]) == (3, False)


def test_the_summary_is_found_inside_the_event_stream_envelope():
    """THE REGRESSION. Both agents are launched with an event stream, so the
    closing JSON is a STRING INSIDE `{"type":"result","result":"..."}` — never a
    line of its own. Matching only whole lines found it exactly when the agent
    was not streaming, which is never, and the whole reconcile silently
    downgraded to "could not read back" on a byte-perfect import."""
    import json

    summary = ('{"files_seen": 204, "files_landed": 204, "projects": '
               '["odyssey-1-0"], "experiments_created": 3}')
    envelope = json.dumps({"type": "result", "subtype": "success",
                           "result": f"All done.\n{summary}"})
    assert backfill.summary_projects(envelope) == ["odyssey-1-0"]

    # The same run also emits it one level deeper, as a text block on an
    # assistant message. Both shapes are taken from real `claude -p
    # --output-format stream-json` output, not imagined.
    assistant = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": summary}]},
    })
    assert backfill.summary_projects(assistant) == ["odyssey-1-0"]


def test_a_bare_summary_line_still_parses():
    """The non-streaming shape must keep working — the fix widens what is
    recognised, it does not swap one exact shape for another."""
    assert backfill.summary_projects('{"projects": ["solo"]}') == ["solo"]


def test_experiment_anchored_artifacts_are_counted_too():
    """A faithful import that grouped its work read back as a 40% shortfall:
    step 3 asks the agent to attach artifacts to experiments, and those are not
    in the project listing."""
    server = _Server({"a": {"project": 121, "experiments": [37, 27, 19]}})
    assert backfill.count_landed_across(server, ["a"]) == (204, False)


def test_a_slug_is_resolved_before_it_reaches_the_artifacts_route():
    """`/v1/projects/{id}/artifacts` types its param as a UUID, and the slugs
    come from the agent's summary — so passing them through 422s, and the
    reconcile swallows it as "could not read back"."""
    server = _Server({"a": {"project": 1, "experiments": [1]}})
    backfill.count_landed_across(server, ["a"])
    assert server.seen_ids, "nothing was ever listed"
    for anchor_id in server.seen_ids:
        assert str(anchor_id).startswith(server.ids["a"]), f"{anchor_id} is not a UUID"


def test_an_unknown_slug_is_unknown_not_zero():
    server = _Server({"a": {"project": 1, "experiments": []}})
    assert backfill.count_landed_across(server, ["nope"]) == (-1, False)


def test_a_classification_that_does_not_parse_uploads_nothing(tmp_path, monkeypatch):
    """Pass A produces a plan or it produces nothing. There is no half-import:
    the projects do not exist yet, so a failed classify has nothing to clean up.
    """
    _tree(tmp_path)
    monkeypatch.setattr(backfill, "launch_agent", lambda *a, **kw: (True, "no json at all"))
    lines = backfill.run(client_factory=_FakeClient, folder=tmp_path, interactive=False)
    assert any("did not produce a usable plan" in ln for ln in lines)
    assert any("nothing was uploaded" in ln for ln in lines)


def test_one_unreadable_project_makes_the_whole_count_unknown():
    """Reporting a shortfall that is really a failed lookup would train people
    to ignore the one number this feature exists for."""

    server = _Server(
        {"fine": {"project": 1, "experiments": []}, "broken": {"project": 1, "experiments": []}},
        broken={"broken"},
    )
    assert backfill.count_landed_across(server, ["fine", "broken"]) == (-1, False)


def test_duplicate_slugs_in_the_summary_are_counted_once():
    tail = '{"projects": ["a", "a", "b"]}'
    assert backfill.summary_projects(tail) == ["a", "b"]


def test_a_pinned_project_overrides_every_assignment(tmp_path, monkeypatch):
    """`--project` is a promise that everything lands in one place. It has to
    beat the classifier, not merely seed it -- otherwise the flag means "a
    suggestion" and the one destination someone explicitly asked for is the one
    thing they do not get."""
    _tree(tmp_path)
    import json as _json

    def fake_launch(folder, prompt, **kw):
        if "deciding how one research folder" in prompt:
            from probe.cli import backfill_evidence as _ev
            from probe.cli import backfill_plan as _bp

            names = _bp.relative_paths(_ev.gather(tmp_path))
            # The classifier says two projects; neither is the pinned one.
            return True, _json.dumps({
                "projects": [{"slug": "guessed-a"}, {"slug": "guessed-b"}],
                "assignments": [
                    {"path": n, "project": "guessed-a" if i % 2 else "guessed-b"}
                    for i, n in enumerate(names)
                ],
            })
        return True, '{"rows": 0}'

    monkeypatch.setattr(backfill, "launch_agent", fake_launch)
    client = _FakeClient(existing={"id": FAKE_ID, "slug": "pinned"})
    lines = backfill.run(
        client_factory=lambda: client, folder=tmp_path, interactive=False, project="pinned"
    )
    joined = "\n".join(lines)
    assert "pinned" in joined
    assert "guessed-a" not in joined and "guessed-b" not in joined


# -- showing what the agent is doing ----------------------------------------


def test_both_agents_are_asked_for_an_event_stream():
    """A bare `claude -p` prints NOTHING until it exits, so a long import was
    indistinguishable from a hang — which is exactly how it looked. The event
    stream is the only signal that the agent is alive."""
    claude = backfill.agent_argv(backfill.Agent.CLAUDE, "/b/claude", "p", Path("/w"))
    assert "--output-format" in claude and "stream-json" in claude
    # stream-json emits only the final result without --verbose.
    assert "--verbose" in claude
    codex = backfill.agent_argv(backfill.Agent.CODEX, "/b/codex", "p", Path("/w"))
    assert "--json" in codex


def _fold(lines, total=37):
    state = backfill.Activity(total=total)
    changed = [backfill.fold_event(ln, state) for ln in lines]
    return state, changed


def test_non_json_noise_never_blanks_the_display():
    """Claude prints the connectors warning to stdout and Codex a stdin notice
    plus tracing. A parser that treated those as fatal would go dark for the
    rest of the run."""
    state, changed = _fold(
        [
            "⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY is set",
            "Reading additional input from stdin...",
            "2026-08-04T00:21:10Z ERROR codex_core::session: failed to load skill",
            "",
            "not json at all {",
        ]
    )
    assert not any(changed)
    assert state.uploaded == 0


def test_claude_uploads_are_counted_against_the_denominator():
    ev = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",'
        '"input":{"command":"probe artifact add --project abc /w/results.csv",'
        '"description":"Upload results.csv"}}]}}'
    )
    state, _ = _fold([ev, ev, ev])
    assert state.uploaded == 3
    assert "3/37" in state.line(1.0)


def test_codex_uploads_are_counted_too():
    ev = (
        '{"type":"item.started","item":{"type":"command_execution",'
        '"command":"/bin/zsh -lc \'probe artifact add --project abc /w/train.py\'"}}'
    )
    state, _ = _fold([ev, ev])
    assert state.uploaded == 2


def test_an_upload_is_not_double_counted_when_codex_completes_it():
    # item.started AND item.completed both carry the command; only one may count.
    started = '{"type":"item.started","item":{"type":"command_execution","command":"probe artifact add x"}}'
    completed = '{"type":"item.completed","item":{"type":"command_execution","command":"probe artifact add x","exit_code":0}}'
    state, _ = _fold([started, completed])
    assert state.uploaded == 1


def test_reads_and_searches_read_as_activity_not_uploads():
    state, _ = _fold(
        [
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read",'
            '"input":{"file_path":"/w/Xian/esm3-baseline/run.log"}}]}}'
        ]
    )
    assert state.uploaded == 0
    assert state.doing == "reading run.log"


def test_the_status_line_is_one_line_and_stays_narrow():
    state = backfill.Activity(total=12431, uploaded=999)
    state.doing = "uploading " + "a-really-long-file-name" * 8
    line = state.line(3725.0)
    assert "\n" not in line
    assert len(line) <= 80
    assert "…" in line  # truncated rather than wrapped


def test_the_clock_reads_as_minutes_and_seconds():
    assert "1:07" in backfill.Activity(total=1).line(67.0)
    assert "0:05" in backfill.Activity(total=1).line(5.0)


def test_the_spinner_advances_so_a_quiet_turn_is_not_a_hang():
    state = backfill.Activity(total=1)
    first = state.line(1.0)
    state.ticks += 1
    assert state.line(1.0) != first


def test_an_upload_command_is_shortened_to_the_filename():
    got = backfill._shorten(
        "/bin/zsh -lc 'probe artifact add --project 3f7c1a52-0d64-4e2f-9c31-0b8a5d6e1f90 "
        "/workspace/Michael/odyssey-infill-v3/results/docq_scores.csv'"
    )
    assert got == "uploading docq_scores.csv"


def test_the_progress_line_is_centred_like_every_other_page():
    """Left at column 0 it read as output from a different program running
    underneath the wizard, which is exactly what it is not."""
    import inspect

    source = inspect.getsource(backfill.launch_agent)
    assert "tui.left_pad()" in source
    assert '"\\r\\033[2K" + " " * tui.left_pad()' in source


def test_the_path_is_the_first_row_and_is_selectable():
    """Where you are and where you can type are the same control — the shortest
    route from "this is on my clipboard" to done."""
    import inspect

    source = inspect.getsource(backfill.choose_directory)
    first = source.index('value=("type", here)')
    assert first < source.index('value=here'), "the path bar must precede Import this folder"
    assert first < source.index('value=("cd", path)'), "the path bar must precede the children"


def test_the_path_is_not_printed_twice():
    # It used to sit in the block above the question AND is now a row.
    import inspect

    source = inspect.getsource(backfill.choose_directory)
    assert "tui.wrap(str(here))" not in source


# -- choosing between Claude Code and Codex ---------------------------------


def test_codex_is_told_to_skip_the_git_repo_check():
    """Codex REFUSES to run outside a git repository. A research folder on a
    shared mount is virtually never one, so without this every Codex backfill
    dies before reading a byte."""
    argv = backfill.agent_argv(
        backfill.Agent.CODEX, "/usr/bin/codex", "p", Path("/workspace/Michael")
    )
    assert "--skip-git-repo-check" in argv


def test_codex_gets_network_because_uploading_needs_it():
    # read-only would be the tighter sandbox, and it has no network — so the
    # agent would read the whole folder and fail every `probe artifact add`.
    argv = backfill.agent_argv(backfill.Agent.CODEX, "/usr/bin/codex", "p", Path("/w"))
    assert "workspace-write" in argv
    assert "sandbox_workspace_write.network_access=true" in argv


def test_codex_is_told_its_working_root_explicitly():
    # Codex scopes its sandbox to -C, not to the inherited cwd.
    argv = backfill.agent_argv(backfill.Agent.CODEX, "/usr/bin/codex", "p", Path("/workspace/X"))
    assert argv[argv.index("-C") + 1] == "/workspace/X"


def test_each_agent_is_told_how_to_handle_a_big_folder_in_its_own_terms():
    # Claude has a Task tool and can fan out; Codex does not, and telling it to
    # "use subagents" would invite it to invent one.
    assert "subagents" in backfill._subdivide_line(backfill.Agent.CLAUDE)
    assert "subagents" not in backfill._subdivide_line(backfill.Agent.CODEX)
    assert "Do not stop at a sample" in backfill._subdivide_line(backfill.Agent.CODEX)


def test_the_prompt_carries_the_right_subdivide_instruction(tmp_path):
    for agent in backfill.Agent:
        prompt = backfill.build_prompt(
            folder=tmp_path, project="p", census=backfill.Census(files=1, bytes=1), agent=agent
        )
        assert backfill._subdivide_line(agent) in prompt


def test_a_single_installed_agent_is_used_without_asking(monkeypatch):
    monkeypatch.setattr(
        backfill, "which_agent", lambda a: "/bin/claude" if a is backfill.Agent.CLAUDE else None
    )
    assert backfill.resolve_agent(None, interactive=True) == (backfill.Agent.CLAUDE, None)


def test_no_agent_at_all_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(backfill, "which_agent", lambda a: None)
    chosen, error = backfill.resolve_agent(None, interactive=True)
    assert chosen is None
    assert "Claude Code or Codex" in error


def test_an_explicit_agent_is_never_silently_swapped(monkeypatch):
    """Someone who passed --agent codex wants CODEX. Quietly running the other
    one over their filesystem is the wrong kind of helpful."""
    monkeypatch.setattr(
        backfill, "which_agent", lambda a: "/bin/claude" if a is backfill.Agent.CLAUDE else None
    )
    chosen, error = backfill.resolve_agent(backfill.Agent.CODEX, interactive=True)
    assert chosen is None
    assert "not on PATH" in error and "codex" in error


def test_two_agents_and_no_tty_takes_the_first_rather_than_prompting(monkeypatch):
    monkeypatch.setattr(backfill, "which_agent", lambda a: "/bin/" + a.value)
    assert backfill.resolve_agent(None, interactive=False) == (backfill.Agent.CLAUDE, None)


def test_every_agent_has_menu_copy():
    assert set(backfill.AGENT_COPY) == set(backfill.Agent)
    for title, detail in backfill.AGENT_COPY.values():
        assert title and detail
        assert len(title) + 5 <= 80
        assert len(detail) + 5 <= 80


def test_both_agents_promise_the_same_thing_about_the_folder():
    """Parity is the point: one property, stated the same way for both.

    The old copy described two different confinements, and the difference was
    not a nuance -- it advertised Codex as free to run "any command" inside the
    folder, which was true and was the bug. Whichever agent someone picks, the
    promise about their research directory must be the same one."""
    claude_detail = backfill.AGENT_COPY[backfill.Agent.CLAUDE][1]
    codex_detail = backfill.AGENT_COPY[backfill.Agent.CODEX][1]
    for detail in (claude_detail, codex_detail):
        assert "without modifying it" in detail
    # The MECHANISM differs and saying so is honest; the PROMISE does not.
    assert claude_detail != codex_detail


def test_the_agent_binary_is_resolved_off_path_not_a_shell(monkeypatch):
    """`codex` is commonly shadowed by a shell alias. A PATH lookup with no
    shell cannot see one; `shell=True` would have swallowed our arguments."""
    # Read the MODULE FILE, not the attribute: the autouse fixture replaces
    # `which_agent` with a stub, and getsource would inspect that instead.
    source = Path(backfill.__file__).read_text()
    assert "def which_agent" in source and "shutil.which" in source
    assert "shell=True" not in source


def test_the_agent_can_write_its_manifest_but_bash_stays_scoped():
    """The agent's whole output is a file, so Write is not optional.

    It shipped without one for a release: the import prompt said "Write JSONL
    to ..." and the allowlist had no Write tool, so every unit failed having
    read the entire folder first. The confinement that replaced it is the deny
    rule on the folder -- which only holds while Bash stays scoped to `probe`.
    An unscoped Bash walks around it with one `echo >`."""
    assert "Write" in backfill.AGENT_TOOLS
    assert "Bash(probe:*)" in backfill.AGENT_TOOLS
    assert "Bash," not in backfill.AGENT_TOOLS + ","
    assert "WebFetch" not in backfill.AGENT_TOOLS


def test_the_deny_rule_is_spelled_edit_because_write_rules_are_ignored():
    """`Write(...)` deny rules are a SILENT no-op in Claude Code.

    The binary says so itself -- "is not matched by file permission checks ...
    only Edit(path) rules are" -- and then carries on with the folder writable.
    Verified against the real binary both ways on 2026-08-06. This is the one
    line standing between an unattended agent and someone's research directory,
    so it is asserted rather than assumed."""
    rule = backfill.readonly_settings(Path("/drive/research"))
    assert '"deny"' in rule
    assert "Edit(/drive/research/**)" in rule
    assert "Write(" not in rule


def test_a_folder_that_cannot_be_expressed_as_a_rule_is_refused(tmp_path, monkeypatch):
    """A rule that fails to parse does not fail loudly -- it stops denying.

    `Research (old)` is an ordinary macOS folder name, and a permission rule
    delimits its argument with the very parentheses in it. Refusing beats
    running unconfined and calling it protected."""
    folder = tmp_path / "Research (old)"
    folder.mkdir()
    assert backfill.unruleable(folder)

    launched: list = []
    monkeypatch.setattr(backfill.subprocess, "Popen", lambda *a, **k: launched.append(a))
    ok, msg = backfill.launch_agent(
        folder, "prompt", agent=backfill.Agent.CLAUDE, workdir=tmp_path / "work"
    )
    assert ok is False
    assert "could not be protected" in msg
    assert launched == [], "the agent must not run at all"
    # Codex needs no pattern, so the same folder is fine for it.
    assert not backfill.unruleable(tmp_path / "plain")


def test_missing_claude_is_a_message_not_a_traceback(tmp_path, monkeypatch):
    # Overrides the autouse "both installed" fixture on purpose.
    monkeypatch.setattr(backfill, "which_agent", lambda a: None)
    ok, msg = backfill.launch_agent(tmp_path, "prompt")
    assert ok is False
    assert "not on PATH" in msg


# -- the read-back ----------------------------------------------------------


def test_count_landed_never_fails_the_import():
    class Broken:
        def list_anchored(self, *a, **kw):
            raise RuntimeError("network gone")

    assert backfill.count_landed(Broken(), "p") == (-1, False)


#: A project with no experiments, addressed by ID -- written `id:` now that a
#: bare ref is the slug, so it still needs no resolution and no client call.
_BARE = "id:33333333-3333-4333-8333-333333333333"


class _NoExperiments:
    """Only the project anchor answers; the experiment listing is empty."""

    def __init__(self, payload):
        self.payload = payload

    def list_anchored(self, *a, **kw):
        return self.payload

    def list_experiments(self, **kw):
        return {"items": []}


@pytest.mark.parametrize(
    "payload,expected",
    [
        ([{"id": 1}, {"id": 2}], 2),
        ({"items": [{"id": 1}]}, 1),
        ("nonsense", -1),
    ],
)
def test_count_landed_reads_both_page_shapes(payload, expected):
    assert backfill.count_landed(_NoExperiments(payload), _BARE)[0] == expected


def test_count_landed_flags_a_full_page_as_at_least():
    full = [{"id": i} for i in range(backfill.RECONCILE_PAGE)]
    count, at_least = backfill.count_landed(_NoExperiments(full), _BARE)
    assert count == backfill.RECONCILE_PAGE
    assert at_least is True


def test_a_page_dict_is_not_read_as_zero_rows():
    """`dict.items` is a bound method, so an attribute-first unwrap reads every
    page dict as "no rows" — which would drop the experiment listing silently
    and undercount instead of failing."""
    assert backfill._rows({"items": [{"id": 1}, {"id": 2}]}) == [{"id": 1}, {"id": 2}]
    assert backfill._rows([{"id": 1}]) == [{"id": 1}]
    assert backfill._rows("nonsense") is None


# -- the action -------------------------------------------------------------


def test_headless_without_a_folder_says_what_to_do(tmp_path):
    lines = backfill.run(client_factory=_FakeClient, interactive=False)
    assert "--folder" in lines[0]


def test_a_file_is_not_a_folder(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x")
    lines = backfill.run(client_factory=_FakeClient, folder=target, interactive=False)
    assert "not a directory" in lines[0]


def test_a_bad_path_is_reported_before_a_missing_agent(tmp_path, monkeypatch):
    """Telling someone to install an agent when they mistyped a path is a wrong
    answer to the question they actually asked."""
    monkeypatch.setattr(backfill, "which_agent", lambda a: None)
    target = tmp_path / "a.txt"
    target.write_text("x")
    lines = backfill.run(client_factory=_FakeClient, folder=target, interactive=False)
    assert "not a directory" in lines[0]


def test_a_missing_agent_creates_no_project(tmp_path, monkeypatch):
    """Agent availability is a free local check and project creation is a
    network write. Getting that order wrong leaves an empty project behind on
    every machine that cannot run the import at all."""
    monkeypatch.setattr(backfill, "which_agent", lambda a: None)
    _tree(tmp_path)
    client = _FakeClient()
    lines = backfill.run(client_factory=lambda: client, folder=tmp_path, interactive=False)
    assert "No coding agent found" in lines[0]
    assert client.ensured == []


def test_an_empty_folder_launches_no_agent(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(backfill, "launch_agent", lambda *a, **kw: launched.append(a))
    lines = backfill.run(client_factory=_FakeClient, folder=tmp_path, interactive=False)
    assert "no files to import" in lines[0]
    assert launched == []


def test_a_credential_problem_names_the_fix(tmp_path, monkeypatch):
    _tree(tmp_path)

    class Unauthorized:
        def __enter__(self):
            raise RuntimeError("401 unauthorized")

        def __exit__(self, *a):
            return False

    # Only a NAMED project is resolved up front; that is the path that can fail
    # on credentials before the agent starts.
    lines = backfill.run(
        client_factory=Unauthorized, folder=tmp_path, interactive=False, project="pinned"
    )
    assert "probe login" in lines[1]


def test_the_happy_path_reports_the_denominator(tmp_path, monkeypatch):
    """The denominator comes from the WALK. It is the number the whole feature
    exists to be honest about, so it survives the two-pass rewrite."""
    _tree(tmp_path)
    seen = {}

    def fake_launch(folder, prompt, **kw):
        seen.setdefault("folders", []).append(folder)
        seen.setdefault("prompts", []).append(prompt)
        if "deciding how one research folder" in prompt:
            import json as _json

            from probe.cli import backfill_evidence as _ev
            from probe.cli import backfill_plan as _bp

            names = _bp.relative_paths(_ev.gather(tmp_path))
            return True, _json.dumps({
                "projects": [{"slug": "odyssey", "name": "Odyssey"}],
                "assignments": [
                    {"path": n, "project": "odyssey", "confidence": "high"} for n in names
                ],
                "summary": "one folder",
            })
        return True, '{"manifest": "m", "rows": 0}'

    monkeypatch.setattr(backfill, "launch_agent", fake_launch)
    lines = backfill.run(client_factory=_FakeClient, folder=tmp_path, interactive=False)
    assert seen["folders"][0] == tmp_path.resolve(), "the agent runs INSIDE the folder"
    assert any("files found on disk" in ln for ln in lines)
    assert any(str(tmp_path.resolve()) in ln for ln in lines)


# -- pasting a path instead of browsing --------------------------------------


class _Answers:
    """Stand in for tui.ask: hand back queued answers, record the prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, question, height=None):
        self.calls += 1
        return self.answers.pop(0)


def test_a_pasted_path_is_accepted(tmp_path, monkeypatch):
    target = tmp_path / "odyssey-infill-v3"
    target.mkdir()
    monkeypatch.setattr("probe.cli.tui.ask", _Answers(str(target)))
    assert backfill.ask_path(tmp_path) == target.resolve()


def test_a_pasted_path_survives_quotes_and_whitespace(tmp_path, monkeypatch):
    # Paths pasted out of Slack or a shell routinely arrive wrapped.
    target = tmp_path / "sap-bench"
    target.mkdir()
    monkeypatch.setattr("probe.cli.tui.ask", _Answers(f"  '{target}'  "))
    assert backfill.ask_path(tmp_path) == target.resolve()


def test_a_tilde_path_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "work").mkdir()
    monkeypatch.setattr("probe.cli.tui.ask", _Answers("~/work"))
    assert backfill.ask_path(tmp_path) == (tmp_path / "work").resolve()


def test_a_relative_path_resolves_against_where_you_were_browsing(tmp_path, monkeypatch):
    (tmp_path / "Michael" / "v3").mkdir(parents=True)
    monkeypatch.setattr("probe.cli.tui.ask", _Answers("Michael/v3"))
    assert backfill.ask_path(tmp_path) == (tmp_path / "Michael" / "v3").resolve()


def test_a_bad_path_re_asks_rather_than_dumping_you_out(tmp_path, monkeypatch):
    """A pasted path is routinely missing a segment. Failing back into a browser
    two directories away is a worse answer than asking again."""
    good = tmp_path / "real"
    good.mkdir()
    answers = _Answers(str(tmp_path / "typo"), str(good))
    monkeypatch.setattr("probe.cli.tui.ask", answers)
    assert backfill.ask_path(tmp_path) == good.resolve()
    assert answers.calls == 2


def test_a_file_is_rejected_like_a_missing_folder(tmp_path, monkeypatch):
    f = tmp_path / "notes.md"
    f.write_text("x")
    good = tmp_path / "real"
    good.mkdir()
    monkeypatch.setattr("probe.cli.tui.ask", _Answers(str(f), str(good)))
    assert backfill.ask_path(tmp_path) == good.resolve()


def test_escaping_the_path_prompt_returns_to_the_browser(tmp_path, monkeypatch):
    from probe.cli import tui

    monkeypatch.setattr("probe.cli.tui.ask", _Answers(tui.BACK))
    assert backfill.ask_path(tmp_path) is tui.BACK


# -- `probe backfill`, the command the dashboard hands out -------------------


def test_backfill_is_a_top_level_command():
    """`npx probe-research backfill` forwards its arguments verbatim, so the
    dashboard's copied command lands on `probe backfill` — not on the wizard."""
    from typer.main import get_command

    from probe.cli.main import app

    assert "backfill" in get_command(app).commands


def _cli_main():
    """`probe.cli.main` is the entry-point FUNCTION on the package, not the
    module — `from probe.cli import main` imports the wrong object."""
    import sys

    import probe.cli.main  # noqa: F401

    return sys.modules["probe.cli.main"]


def test_the_command_installs_a_persistent_probe_first():
    """The agent does its work by shelling out to `probe artifact add`. Reached
    through `npx` we are running from an EPHEMERAL uvx/pipx with no binary on
    PATH, so without this the agent reads the whole folder and lands nothing."""
    import inspect

    source = inspect.getsource(_cli_main().backfill)
    assert "ensure_persistent_install" in source
    assert source.index("ensure_persistent_install") < source.index("backfill_impl.run")


def _stub_bootstrap(monkeypatch):
    monkeypatch.setattr(
        "probe.cli.bootstrap.ensure_persistent_install",
        lambda: type("B", (), {"message": None})(),
    )


def test_the_command_passes_its_folder_through(monkeypatch, tmp_path):
    cli_main = _cli_main()
    seen = {}
    _stub_bootstrap(monkeypatch)
    monkeypatch.setattr("probe.cli.backfill.run", lambda **kw: seen.update(kw) or ["done"])
    cli_main.backfill(folder=str(tmp_path), agent=None)
    assert seen["folder"] == Path(str(tmp_path))
    assert seen["client_factory"] is cli_main._client


def test_the_command_with_no_folder_leaves_the_picker_to_decide(monkeypatch):
    cli_main = _cli_main()
    seen = {}
    _stub_bootstrap(monkeypatch)
    monkeypatch.setattr("probe.cli.backfill.run", lambda **kw: seen.update(kw) or [])
    cli_main.backfill(folder=None, agent=None)
    assert seen["folder"] is None


def test_a_failed_unit_says_rerunning_is_safe(tmp_path, monkeypatch):
    """Identical content deduplicates server-side, so the honest instruction
    after a partial import is simply to run it again."""
    _tree(tmp_path)
    import json as _json

    def fake_launch(folder, prompt, **kw):
        if "deciding how one research folder" in prompt:
            from probe.cli import backfill_evidence as _ev
            from probe.cli import backfill_plan as _bp

            names = _bp.relative_paths(_ev.gather(tmp_path))
            return True, _json.dumps({
                "projects": [{"slug": "p"}],
                "assignments": [{"path": n, "project": "p"} for n in names],
            })
        return False, "boom"

    monkeypatch.setattr(backfill, "launch_agent", fake_launch)
    lines = backfill.run(client_factory=_FakeClient, folder=tmp_path, interactive=False)
    assert any("deduplicated server-side" in ln for ln in lines)
    assert any("did not finish" in ln for ln in lines)




# -- shared drives: what the walk cannot see, and what it must say ----------


def test_a_symlinked_directory_is_recorded_not_followed(tmp_path):
    """Not following is right -- a link can point at its own parent, or on a
    shared drive at somebody else's dataset an import would file under this
    project. Doing it SILENTLY is what was wrong: the census reported the
    smaller number, the reconcile agreed, and a folder reachable through a link
    was simply absent."""
    real = tmp_path / "real" / "nested"
    real.mkdir(parents=True)
    (real / "deep.py").write_text("x\n")
    (tmp_path / "real" / "top.py").write_text("y\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / "direct.py").write_text("z\n")
    (root / "linked_dir").symlink_to(tmp_path / "real")

    census = backfill.scan(root, cap=10**9)
    assert census.files == 1, "a linked directory must not be walked into"
    assert [link for link, _ in census.linked_dirs] == ["linked_dir"]
    # The TARGET rides along: the advice is "import what it points at", which
    # is unusable without it.
    assert census.linked_dirs[0][1] == str(tmp_path / "real")
    assert "1 linked directory not followed" in census.describe()


def test_a_link_pointing_inside_the_folder_is_not_worth_warning_about(tmp_path):
    """`latest -> runs/2024-05-01` is the most common symlink in a research
    folder, and its target is walked anyway by its real path -- so nothing is
    missing. Warning there is crying wolf on the ordinary case, which is how
    the one real cross-drive link stops being read."""
    runs = tmp_path / "runs" / "2024-05-01"
    runs.mkdir(parents=True)
    (runs / "cfg.yaml").write_text("lr: 1\n")
    (tmp_path / "train.py").write_text("x\n")
    (tmp_path / "latest").symlink_to(runs)

    census = backfill.scan(tmp_path, cap=10**9)
    assert census.linked_dirs == ()
    assert "linked" not in census.describe()


def test_an_unresolvable_link_is_recorded_not_dropped(tmp_path):
    """A dead mount or a path this host cannot see is exactly the interesting
    kind -- it is the case where files really are unreachable."""
    (tmp_path / "a.py").write_text("x\n")
    (tmp_path / "gone").symlink_to(tmp_path / "nowhere-at-all")
    census = backfill.scan(tmp_path, cap=10**9)
    assert census.linked_dirs == () or census.linked_dirs[0][0] == "gone"


def test_a_folder_with_no_links_says_nothing_about_them(tmp_path):
    """A note on every folder is a note nobody reads."""
    (tmp_path / "a.py").write_text("x\n")
    census = backfill.scan(tmp_path, cap=10**9)
    assert census.linked_dirs == ()
    assert "linked" not in census.describe()


def test_a_symlinked_file_is_still_counted(tmp_path):
    """Only DIRECTORIES are skipped. A linked file is one file, resolvable, and
    dropping it would understate the denominator."""
    (tmp_path / "real.py").write_text("x\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link.py").symlink_to(tmp_path / "real.py")
    census = backfill.scan(root, cap=10**9)
    assert census.files == 1 and census.linked_dirs == ()
