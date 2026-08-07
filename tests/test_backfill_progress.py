"""What the progress line says, and whether any of it was ever true.

It was not. The counter incremented only on a shell command containing
`artifact add`, and the two-pass split stopped agents running that at all --
classify uploads nothing by design, and an import unit writes a manifest that
one process enqueues afterwards. So every run showed `0/204` from start to
finish, in both passes, which is what a hang looks like.

Progress is now files READ against the unit's own file list: the denominator is
exactly what the agent was told to open, so the fraction is real and an ETA
derived from it means something.
"""

from __future__ import annotations

import json

from probe.cli import backfill as bf


def _tool_use(name: str, **args) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": args}]},
    })


def _codex_command(command: str) -> str:
    return json.dumps({
        "type": "item.started",
        "item": {"type": "command_execution", "command": command},
    })


# -- the counter actually moves ---------------------------------------------


def test_reading_files_advances_the_counter():
    state = bf.Activity(total=3)
    assert state.done == 0
    for name in ("a.py", "b.py"):
        bf.fold_event(_tool_use("Read", file_path=f"/drive/{name}"), state)
    assert state.done == 2


def test_the_same_file_twice_counts_once():
    """A set, not a counter: an agent re-reading a file it already read must
    not advance the bar past what it has covered."""
    state = bf.Activity(total=5)
    for _ in range(4):
        bf.fold_event(_tool_use("Read", file_path="/drive/a.py"), state)
    assert state.done == 1


def test_the_counter_never_exceeds_its_denominator():
    """An agent may legitimately read outside its unit -- a shared config, a
    README one level up. `14/8` reads as a bug in the tool rather than
    curiosity in the agent."""
    state = bf.Activity(total=2)
    for i in range(9):
        bf.fold_event(_tool_use("Read", file_path=f"/drive/f{i}.py"), state)
    assert state.done == 2


def test_codex_progress_comes_from_the_shell_it_runs():
    """Codex has no Read tool event, so its only signal is the command."""
    state = bf.Activity(total=4)
    bf.fold_event(_codex_command("cat /drive/train.py"), state)
    bf.fold_event(_codex_command("head -50 /drive/eval.csv"), state)
    assert state.done == 2


def test_an_ambiguous_command_is_not_counted():
    """Over-counting inflates the bar AND the ETA, and strands the run at
    "almost done". Anything with a pipe, a glob or several paths is skipped."""
    state = bf.Activity(total=9)
    for command in (
        "cat a.py b.py",            # two targets
        "cat *.py",                 # a glob
        "cat a.py | head",          # a pipeline
        "ls -la",                   # not a read
        "python train.py",          # not a read
        "cat > out.txt",            # a WRITE wearing cat's name
    ):
        bf.fold_event(_codex_command(command), state)
    assert state.done == 0


def test_a_wrapped_shell_command_still_counts():
    state = bf.Activity(total=2)
    bf.fold_event(_codex_command("/bin/zsh -lc 'cat /drive/notes.md'"), state)
    assert state.done == 1


# -- the ETA ----------------------------------------------------------------


def test_no_eta_before_there_is_enough_to_say():
    """An estimate off two files is noise wearing a number's clothes."""
    state = bf.Activity(total=100)
    for i in range(bf._ETA_FLOOR - 1):
        state.seen.add(f"f{i}")
    assert state.eta(30.0) == ""


def test_the_eta_extrapolates_from_what_has_been_read():
    state = bf.Activity(total=100)
    for i in range(10):
        state.seen.add(f"f{i}")
    # 10 files in 60s -> 90 left -> ~540s -> 9:00
    assert state.eta(60.0) == "~9:00 left"


def test_no_eta_once_everything_is_read():
    state = bf.Activity(total=5)
    for i in range(5):
        state.seen.add(f"f{i}")
    assert state.eta(60.0) == ""


def test_an_absurd_eta_is_said_in_words_not_digits():
    state = bf.Activity(total=1_000_000)
    for i in range(bf._ETA_FLOOR):
        state.seen.add(f"f{i}")
    assert state.eta(600.0) == "~over an hour left"


def test_the_eta_appears_in_the_line_once_it_exists():
    state = bf.Activity(total=100)
    for i in range(10):
        state.seen.add(f"f{i}")
    assert "~9:00 left" in state.line(60.0)
    assert "10/100" in state.line(60.0)


# -- queued units are visible -----------------------------------------------


def test_a_queued_unit_says_queued_rather_than_showing_nothing():
    """Only `concurrency` units start at once, so a board headed "Importing 6
    unit(s)" painted three lines and three blanks -- and a blank row is
    indistinguishable from a row that is not there."""
    line = bf.Activity(total=30, queued=True).line(0.0)
    assert "queued" in line
    assert "0/30" in line


def test_a_running_unit_does_not_say_queued():
    assert "queued" not in bf.Activity(total=30).line(1.0)


# -- the line still fits the block ------------------------------------------


def test_a_long_filename_cannot_overflow_the_block():
    """The board addresses rows absolutely; a line that wraps pushes every row
    below it down by one and breaks every address at once."""
    state = bf.Activity(total=100)
    for i in range(10):
        state.seen.add(f"f{i}")
    state.doing = "reading " + "x" * 300
    assert len(state.line(60.0)) <= 46 + 30, state.line(60.0)


# -- the piped/CI path counts the same thing the screen does ----------------


def test_piped_output_counts_files_read_not_uploads(tmp_path, monkeypatch):
    """The live line was moved onto files READ when the upload counter turned
    out to be permanently zero -- and this branch, the one a pipe, a CI log or
    `nohup` takes, was left counting the dead field. A real backfill run to a
    log file showed `0/704 · reading README.md` for the whole classify pass.

    Which is the worst place to leave it: an unwatched run is exactly the one
    someone reads afterwards to find out what happened."""
    import io

    events = [
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": f"/d/f{i}.py"}}]}})
        for i in range(3)
    ]

    class _Proc:
        # In __init__, NOT on the class: a one-shot iterator shared by every
        # instance is exhausted after the first, so a rerun (pytest-repeat, a
        # parametrize, or a retry inside launch_agent) fails with "printed
        # nothing" instead of the real reason.
        def __init__(self):
            self.stdout = iter([e + "\n" for e in events])

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    out = io.StringIO()  # NOT a tty -> the plain, appending branch
    monkeypatch.setattr(bf, "which_agent", lambda a: "/bin/claude")
    monkeypatch.setattr(bf.subprocess, "Popen", lambda *a, **k: _Proc())
    bf.launch_agent(tmp_path, "prompt", stream=out, total=704,
                    workdir=tmp_path / "w")

    lines = [ln for ln in out.getvalue().splitlines() if "/704" in ln]
    assert lines, "the plain branch printed nothing"
    assert lines[-1].strip().startswith("3/704"), (
        f"the counter never moved: {lines}"
    )


def test_progress_false_gives_the_raw_transcript(tmp_path, monkeypatch):
    """`progress=False` asks for the stream verbatim, and it did not get it.

    The branch sat under `elif changed`, and `fold_event` returns True for
    every tool call, command and message -- so the caller got a hybrid: counter
    lines exactly where the interesting events should have been, and raw JSON
    only for the lines nobody wanted.
    """
    import io

    events = [
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/d/a.py"}}]}}),
        json.dumps({"type": "turn.started"}),
    ]

    class _Proc:
        def __init__(self):
            self.stdout = iter([e + "\n" for e in events])

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    out = io.StringIO()
    monkeypatch.setattr(bf, "which_agent", lambda a: "/bin/claude")
    monkeypatch.setattr(bf.subprocess, "Popen", lambda *a, **k: _Proc())
    bf.launch_agent(tmp_path, "prompt", stream=out, total=9, progress=False,
                    workdir=tmp_path / "w")

    text = out.getvalue()
    assert "tool_use" in text, "the raw transcript was replaced by counter lines"
    assert "·" not in text, f"a progress line leaked into a raw transcript: {text!r}"


def test_a_piped_unit_line_says_which_unit_it_is(tmp_path, monkeypatch):
    """There is no board out here. `run_units` only builds one when
    interactive, so an unattended import writes up to `concurrency` units into
    ONE stream -- and without a name the only thing telling `3/704` from
    `5/704` is a denominator two units can easily share."""
    import io

    event = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/d/a.py"}}]}})

    class _Proc:
        def __init__(self):
            self.stdout = iter([event + "\n"])

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    out = io.StringIO()
    monkeypatch.setattr(bf, "which_agent", lambda a: "/bin/claude")
    monkeypatch.setattr(bf.subprocess, "Popen", lambda *a, **k: _Proc())
    bf.launch_agent(tmp_path, "prompt", stream=out, total=40,
                    label="odyssey-plm", workdir=tmp_path / "w")
    assert "odyssey-plm 1/40" in out.getvalue()


def test_an_unlabelled_run_does_not_grow_a_stray_space(tmp_path, monkeypatch):
    """The classify pass has no unit to name."""
    import io

    event = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/d/a.py"}}]}})

    class _Proc:
        def __init__(self):
            self.stdout = iter([event + "\n"])

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    out = io.StringIO()
    monkeypatch.setattr(bf, "which_agent", lambda a: "/bin/claude")
    monkeypatch.setattr(bf.subprocess, "Popen", lambda *a, **k: _Proc())
    bf.launch_agent(tmp_path, "prompt", stream=out, total=40, workdir=tmp_path / "w")
    assert out.getvalue().startswith("  1/40 ·")
