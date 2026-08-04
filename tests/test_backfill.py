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

    def resolve_project(self, slug):
        return self.existing

    def get_project(self, project_id):
        return {"id": project_id, "slug": "by-id"}

    def ensure_project(self, slug, name=None, **kw):
        self.ensured.append((slug, name))
        return self.existing or {"id": FAKE_ID, "slug": slug}

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


def test_configured_project_wins_over_the_derived_slug(tmp_path):
    client = _FakeClient()
    project_id, _ = backfill.resolve_anchor(client, tmp_path, configured=FAKE_ID)
    assert project_id == FAKE_ID
    assert client.ensured == []


def test_a_configured_slug_is_resolved_to_its_id(tmp_path):
    # PROBE_PROJECT takes a slug, so the configured value is not always an id.
    client = _FakeClient(existing={"id": FAKE_ID, "slug": "already-here"})
    project_id, slug = backfill.resolve_anchor(client, tmp_path, configured="already-here")
    assert project_id == FAKE_ID
    assert slug == "already-here"


def test_an_unknown_configured_slug_fails_loudly(tmp_path):
    with pytest.raises(ValueError, match="no project"):
        backfill.resolve_anchor(_FakeClient(existing=None), tmp_path, configured="ghost")


def test_existing_project_is_reused_not_forked(tmp_path):
    client = _FakeClient(existing={"id": FAKE_ID, "slug": "already-here"})
    project_id, slug = backfill.resolve_anchor(client, tmp_path)
    assert (project_id, slug) == (FAKE_ID, "already-here")


def test_creation_goes_through_the_near_miss_guard(tmp_path):
    # ensure_project, not create_project: its guard refuses a slug that looks
    # like a typo of an existing one, which is the identity fork we cannot undo.
    folder = tmp_path / "odyssey"
    folder.mkdir()
    client = _FakeClient(existing=None)
    backfill.resolve_anchor(client, folder)
    assert client.ensured == [("odyssey", "odyssey")]


# -- the prompt (the actual deliverable) ------------------------------------


def test_prompt_pins_the_anchor_and_forbids_inventing_another(tmp_path):
    prompt = backfill.build_prompt(
        folder=tmp_path, project="odyssey", census=backfill.Census(files=47, bytes=1)
    )
    assert "--project odyssey" in prompt
    assert "do not change" in prompt.lower()
    assert "do not create another project" in prompt.lower()


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


def test_the_copy_does_not_claim_codex_is_tool_restricted():
    """Codex confines WHERE commands act, not WHICH run. Claiming otherwise
    would be a security promise the sandbox does not make."""
    claude_detail = backfill.AGENT_COPY[backfill.Agent.CLAUDE][1]
    codex_detail = backfill.AGENT_COPY[backfill.Agent.CODEX][1]
    assert "cannot write" in claude_detail
    assert "any command" in codex_detail


def test_the_agent_binary_is_resolved_off_path_not_a_shell(monkeypatch):
    """`codex` is commonly shadowed by a shell alias. A PATH lookup with no
    shell cannot see one; `shell=True` would have swallowed our arguments."""
    import inspect

    source = inspect.getsource(backfill.which_agent)
    assert "shutil.which" in source
    launch = inspect.getsource(backfill.launch_agent)
    assert "shell=True" not in launch


def test_agent_toolset_is_probe_and_reads_only():
    # This runs unattended over folders nobody audited: no write, no delete, no
    # network beyond the probe CLI itself.
    assert backfill.AGENT_TOOLS == "Bash(probe:*),Read,Glob,Grep,Task"
    assert "Write" not in backfill.AGENT_TOOLS
    assert "Edit" not in backfill.AGENT_TOOLS
    assert "WebFetch" not in backfill.AGENT_TOOLS


def test_missing_claude_is_a_message_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill.shutil, "which", lambda _: None)
    ok, msg = backfill.launch_agent(tmp_path, "prompt")
    assert ok is False
    assert "not on PATH" in msg


# -- the read-back ----------------------------------------------------------


def test_count_landed_never_fails_the_import():
    class Broken:
        def list_anchored(self, *a, **kw):
            raise RuntimeError("network gone")

    assert backfill.count_landed(Broken(), "p") == (-1, False)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ([{"id": 1}, {"id": 2}], 2),
        ({"items": [{"id": 1}]}, 1),
        ("nonsense", -1),
    ],
)
def test_count_landed_reads_both_page_shapes(payload, expected):
    class Fake:
        def list_anchored(self, *a, **kw):
            return payload

    assert backfill.count_landed(Fake(), "p")[0] == expected


def test_count_landed_flags_a_full_page_as_at_least():
    class Fake:
        def list_anchored(self, *a, **kw):
            return [{"id": i} for i in range(backfill.RECONCILE_PAGE)]

    count, at_least = backfill.count_landed(Fake(), "p")
    assert count == backfill.RECONCILE_PAGE
    assert at_least is True


# -- the action -------------------------------------------------------------


def test_headless_without_a_folder_says_what_to_do(tmp_path):
    lines = backfill.run(client_factory=_FakeClient, interactive=False)
    assert "--folder" in lines[0]


def test_a_file_is_not_a_folder(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x")
    lines = backfill.run(client_factory=_FakeClient, folder=target, interactive=False)
    assert "not a directory" in lines[0]


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

    lines = backfill.run(client_factory=Unauthorized, folder=tmp_path, interactive=False)
    assert "probe login" in lines[1]


def test_the_happy_path_reports_the_denominator(tmp_path, monkeypatch):
    _tree(tmp_path)
    seen = {}

    def fake_launch(folder, prompt, **kw):
        seen["folder"] = folder
        seen["prompt"] = prompt
        return True, ""

    monkeypatch.setattr(backfill, "launch_agent", fake_launch)
    monkeypatch.setattr(backfill, "count_landed", lambda *a, **kw: (3, False))
    lines = backfill.run(client_factory=_FakeClient, folder=tmp_path, interactive=False)
    assert seen["folder"] == tmp_path.resolve()
    assert "--project" in seen["prompt"]
    assert any("3 files found on disk · 3 artifacts" in ln for ln in lines)


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


def test_a_failed_agent_says_rerunning_is_safe(tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(backfill, "launch_agent", lambda *a, **kw: (False, "boom"))
    monkeypatch.setattr(backfill, "count_landed", lambda *a, **kw: (1, False))
    lines = backfill.run(client_factory=_FakeClient, folder=tmp_path, interactive=False)
    assert any("deduplicated server-side" in ln for ln in lines)
