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


def test_a_failed_agent_says_rerunning_is_safe(tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(backfill, "launch_agent", lambda *a, **kw: (False, "boom"))
    monkeypatch.setattr(backfill, "count_landed", lambda *a, **kw: (1, False))
    lines = backfill.run(client_factory=_FakeClient, folder=tmp_path, interactive=False)
    assert any("deduplicated server-side" in ln for ln in lines)
