"""The board that concurrent agents share.

The bug it exists for: three import units each repainting `\\r` on "the" status
line. That is not three progress indicators, it is one row with three writers --
whichever ticked last wins, the other two are invisible, and the count on screen
belongs to whichever unit happened to interrupt. On screen it read as a backfill
that kept restarting.

So the property under test is INDEPENDENCE: writing row 1 must not disturb row
0. That only holds while every row is addressed absolutely, which is what these
assert -- against the escape codes, because that is where the guarantee lives.
"""

from __future__ import annotations

import io
import re
import threading

from probe.cli import tui


class _Screen(io.StringIO):
    """A TTY as far as the code under test can tell."""

    def isatty(self) -> bool:
        return True


def _rows_touched(text: str) -> list[int]:
    """Which absolute screen rows a run of output addressed, in order."""
    return [int(m) for m in re.findall(r"\033\[(\d+);1H", text)]


def test_each_row_is_addressed_absolutely_and_only_its_own(monkeypatch):
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    out = _Screen()
    board = tui.Board("Importing 3 unit(s)", ["a", "b", "c"], out=out)
    board.open()

    out.seek(0), out.truncate()
    board.update(1, "middle")
    touched = _rows_touched(out.getvalue())
    assert len(touched) == 1, "one update must repaint exactly one row"
    middle = touched[0]

    out.seek(0), out.truncate()
    board.update(0, "first")
    board.update(2, "last")
    assert _rows_touched(out.getvalue()) == [middle - 1, middle + 1]


def test_a_row_never_moves_when_another_one_updates(monkeypatch):
    """The regression itself: row addresses are fixed for the board's life."""
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    out = _Screen()
    board = tui.Board("t", ["a", "b"], out=out)
    board.open()

    seen = []
    for _ in range(5):
        out.seek(0), out.truncate()
        board.update(0, "tick")
        seen.append(_rows_touched(out.getvalue()))
        board.update(1, "other unit is busy")
    assert len({tuple(s) for s in seen}) == 1, f"row 0 drifted: {seen}"


def test_rows_out_of_range_are_ignored_not_written_somewhere_else(monkeypatch):
    """An index past the end must not address a row belonging to something else."""
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    out = _Screen()
    board = tui.Board("t", ["a", "b"], out=out)
    board.open()
    out.seek(0), out.truncate()
    board.update(7, "nowhere")
    board.update(-1, "nowhere")
    assert out.getvalue() == ""


def test_a_long_row_is_cut_to_the_block_not_wrapped_by_the_terminal(monkeypatch):
    """A row that overflows wraps back to column 0 and pushes every row below it
    down by one -- which breaks every absolute address on the board at once."""
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    monkeypatch.setattr(tui, "columns", lambda: 200)
    out = _Screen()
    board = tui.Board("t", ["unit-a"], out=out)
    board.open()
    out.seek(0), out.truncate()
    board.update(0, "x" * 500)
    painted = out.getvalue().split("\033[2K")[-1]
    assert len(painted) <= tui.left_pad() + tui.CONTENT_WIDTH
    assert "\n" not in painted


def test_the_board_sits_where_every_other_page_sits(monkeypatch):
    """Same top margin as `page()`, or it reads as a different program."""
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    out = _Screen()
    board = tui.Board("t", ["a", "b", "c"], out=out)
    board.open()
    assert _rows_touched(out.getvalue()) == [tui.top_spacer(5) + 1]


def test_rows_survive_being_written_from_several_threads(monkeypatch):
    """Units run concurrently, so the board is written concurrently.

    This passes with the lock removed and it is not pretending otherwise -- one
    `write()` of one string is already serialised by the GIL. What it does check
    is that the board holds up under real concurrent use, which is the thing a
    reader wants to know before pointing three agents at it."""
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    out = _Screen()
    board = tui.Board("t", [f"u{i}" for i in range(4)], out=out)
    board.open()
    out.seek(0), out.truncate()

    def hammer(index: int) -> None:
        for _ in range(50):
            board.update(index, f"row {index}")

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = out.getvalue()
    # Every write landed, and none of them interleaved into a torn escape code.
    assert len(_rows_touched(text)) == 200
    assert "\033[\033[" not in text
    for index in range(4):
        assert text.count(f"row {index}") == 50
