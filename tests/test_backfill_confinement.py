"""The imported folder is read-only. That is the whole safety story.

Two bugs shipped from getting this wrong in opposite directions, and both are
pinned here:

  * Claude ran with no Write tool at all, while the import prompt instructed it
    to write a manifest. Every unit read the entire folder and then failed.
  * Codex ran `workspace-write` scoped to the imported folder, so the one
    writable thing on disk was the customer's research directory.

What both were missing is a scratch directory. The agent needs somewhere to put
the manifest; it does not need that somewhere to be the folder it is reading.

The argv-level assertions live in `test_backfill_agent_argv`. These are the
wiring: that the scratch dir is real, is outside the folder, and is what both
passes actually hand the agents.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from probe.cli import backfill as bf
from probe.cli import backfill_evidence as ev
from probe.cli import backfill_ledger as bl
from probe.cli import backfill_prompts as bp
from probe.cli import backfill_run as br
from probe.cli import tui


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "drive"
    (root / "src").mkdir(parents=True)
    (root / "src" / "train.py").write_text("import torch\n")
    (root / "notes.md").write_text("# odyssey\n")
    return root


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("PROBE_BACKFILL_STATE_DIR", str(d))
    return d


class _Recorder:
    """Stands in for `launch_agent`, keeping every kwarg it was handed."""

    def __init__(self, *, ok=True, rows=2):
        self.ok, self.rows = ok, rows
        self.calls: list[dict] = []

    def __call__(self, folder, prompt, **kw):
        self.calls.append({"folder": folder, "prompt": prompt, **kw})
        for line in prompt.splitlines():
            if line.strip().startswith("Write JSONL to:"):
                path = Path(line.split("Write JSONL to:", 1)[1].strip())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "\n".join(
                        json.dumps({"path": f"f{i}.py"}) for i in range(self.rows)
                    )
                )
        return self.ok, ""


def _unit(project="odyssey", paths=("notes.md",), uid="u-1"):
    return bl.Unit(unit_id=uid, project=project, paths=tuple(paths))


# -- the scratch dir is real, and it is not inside the folder ----------------


def test_both_passes_get_a_scratch_dir_outside_the_imported_folder(folder, monkeypatch):
    """An import must leave no trace in someone's research directory.

    A manifest written under `folder` would be a file the customer did not have
    before -- and, worse, one the NEXT run would census and try to import."""
    rec = _Recorder()
    monkeypatch.setattr(bf, "launch_agent", rec)

    led = bl.Ledger.for_folder(folder)
    work = led.path.parent / f"{led.path.stem}-manifests"
    work.mkdir(parents=True, exist_ok=True)

    br.classify(folder, ev.gather(folder), agent=bf.Agent.CLAUDE, existing=[],
                work_dir=work)
    br.run_unit(folder, _unit(), agent=bf.Agent.CLAUDE, ledger=led, work_dir=work)

    assert len(rec.calls) == 2
    for call in rec.calls:
        scratch = call["workdir"]
        assert scratch is not None, "an agent launched with no scratch dir is unconfined"
        assert folder not in scratch.parents and scratch != folder
        # And the folder it was pointed at is still the folder being imported.
        assert call["folder"] == folder


def test_the_manifest_lands_in_the_scratch_dir_not_the_folder(folder, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(bf, "launch_agent", rec)
    led = bl.Ledger.for_folder(folder)
    work = led.path.parent / "work"

    out = br.run_unit(folder, _unit(), agent=bf.Agent.CLAUDE, ledger=led, work_dir=work)
    assert out.rows == 2
    assert out.manifest is not None and out.manifest.parent == work
    assert list(folder.rglob("*.jsonl")) == []


# -- the prompt tells the agent what it is actually allowed to do ------------


@pytest.mark.parametrize("build", ["classify", "import_unit"])
def test_every_prompt_states_the_folder_is_read_only(build):
    """Both agents, both passes, the same rule. It used to be in neither."""
    if build == "classify":
        prompt = bp.classify(root="/drive/research", evidence_jsonl="{}", existing=[],
                             truncated=False, work_dir="/state/work")
    else:
        prompt = bp.import_unit(root="/drive/research", project="p", paths=["a.py"],
                                manifest_path="/state/work/u.jsonl")
    assert "THE FOLDER IS READ-ONLY" in prompt
    assert "/state/work" in prompt


@pytest.mark.parametrize("build", ["classify", "import_unit"])
def test_every_prompt_says_to_read_by_absolute_path(build):
    """The cwd is the scratch dir now, so a relative read resolves to the wrong
    place and comes back "no such file" -- for every file in the folder."""
    if build == "classify":
        prompt = bp.classify(root="/drive/research", evidence_jsonl="{}", existing=[],
                             truncated=False, work_dir="/state/work")
    else:
        prompt = bp.import_unit(root="/drive/research", project="p", paths=["a.py"],
                                manifest_path="/state/work/u.jsonl")
    assert "ABSOLUTE paths" in prompt
    assert "/drive/research/<the path as listed>" in prompt


def test_the_manifest_path_stays_relative_even_though_reads_are_absolute():
    """The two halves point opposite ways and that is not a typo.

    Reads resolve against the scratch cwd, so they must be absolute. Manifest
    rows are resolved by `artifact add` running with the FOLDER as its cwd, so
    an absolute path there uploads under a name with someone's home directory
    baked into it."""
    prompt = bp.import_unit(root="/drive/research", project="p", paths=["a.py"],
                            manifest_path="/state/work/u.jsonl")
    assert '"path": "<path relative to the folder, exactly as listed above>"' in prompt
    assert '"path" is RELATIVE even though you read the file by its absolute path' \
        in prompt


# -- concurrent units share one screen, not one line ------------------------


def test_concurrent_units_each_get_their_own_row(folder, monkeypatch):
    """Three units repainting one line is one row with three writers.

    Without a board the last agent to tick owns the display and the other two
    are invisible -- which on screen read as a backfill that kept restarting."""
    painters: list = []

    def fake(folder, prompt, **kw):
        painters.append(kw.get("paint_to"))
        return True, ""

    monkeypatch.setattr(bf, "launch_agent", fake)
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    monkeypatch.setattr(tui.Board, "open", lambda self: None)
    monkeypatch.setattr(tui.Board, "close", lambda self: None)

    led = bl.Ledger.for_folder(folder)
    units = [_unit(project=f"p{i}", uid=f"u-{i}") for i in range(3)]
    br.run_units(folder, units, agent=bf.Agent.CLAUDE, ledger=led,
                 work_dir=led.path.parent / "work", concurrency=3)

    assert len(painters) == 3
    assert all(p is not None for p in painters), "a unit with no row paints over others"

    # Each painter must drive a DIFFERENT row.
    out = io.StringIO()
    out.isatty = lambda: True  # type: ignore[method-assign]
    board = tui.Board("t", ["a", "b", "c"], out=out)
    seen = set()
    for index in range(3):
        out.seek(0), out.truncate()
        board.row(index)("x")
        seen.add(out.getvalue().split("\033[2K")[0])
    assert len(seen) == 3


def test_a_solo_agent_draws_its_own_page(folder, monkeypatch):
    """The classify pass is one agent, and it owns the screen.

    It used to print its status line wherever the cursor happened to be -- under
    the leftover folder picker -- which read as another program's output leaking
    into the wizard."""
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    monkeypatch.setattr(tui, "columns", lambda: 120)

    out = io.StringIO()
    out.isatty = lambda: True  # type: ignore[method-assign]

    class _Proc:
        stdout = io.StringIO("")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(bf.subprocess, "Popen", lambda *a, **k: _Proc())
    bf.launch_agent(folder, "prompt", heading="Reading drive — 204 files",
                    stream=out, workdir=folder.parent / "work", total=204)

    text = out.getvalue()
    assert "\033[2J" in text, "the page must start from a cleared screen"
    assert "Reading drive — 204 files" in text
    # Centred, like every other page in the wizard.
    heading_line = next(ln for ln in text.splitlines() if "204 files" in ln)
    assert heading_line.startswith(" " * tui.left_pad())


def test_a_unit_on_a_board_does_not_clear_the_shared_line(folder, monkeypatch):
    """`\\r\\033[2K` on exit wipes whatever row the cursor sits on -- which under
    a board belongs to a unit that is still running."""
    monkeypatch.setattr(tui, "interactive", lambda: True)
    out = io.StringIO()
    out.isatty = lambda: True  # type: ignore[method-assign]

    class _Proc:
        stdout = io.StringIO("")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(bf.subprocess, "Popen", lambda *a, **k: _Proc())
    bf.launch_agent(folder, "prompt", stream=out, paint_to=lambda text: None,
                    workdir=folder.parent / "work")
    assert "\r\033[2K" not in out.getvalue()
    assert "\033[2J" not in out.getvalue(), "a board owns the screen, not the agent"
