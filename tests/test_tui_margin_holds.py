"""The margin is not negotiable, and the folder header does not scroll.

Reported from a real terminal: the "Which folder?" line sat clipped against the
top edge on a directory with a hundred children. Two causes, both fixed here --
the margin was thin enough that one scrolled row reached the edge, and
questionary renders its message INSIDE the scrolling choice region, so the line
naming what you are looking at scrolled away with the list.
"""

from __future__ import annotations

import inspect

import pytest

from probe.cli import backfill, tui


@pytest.mark.parametrize("screen", [12, 24, 40, 60, 120])
def test_the_margin_holds_however_tall_the_content_is(screen):
    """Content 400 rows tall on any screen still gets its margins; the BODY
    gives way, never the whitespace."""
    frame = tui.frame_rows(screen, height=400, header=2)
    assert frame.top == tui.MARGIN
    assert frame.bottom == tui.MARGIN
    assert frame.top + frame.header + frame.body + frame.bottom == screen
    assert frame.body >= 1


def test_the_margin_is_more_than_one_row():
    """At one row the content reads as welded to the terminal chrome, and at two
    a prompt whose message block scrolls still lands its first line on the edge."""
    assert tui.MARGIN >= 3


def test_a_header_costs_body_rows_not_margin_rows():
    plain = tui.frame_rows(40, height=400, header=0)
    withhdr = tui.frame_rows(40, height=400, header=2)
    assert withhdr.top == plain.top == tui.MARGIN
    assert withhdr.body == plain.body - 2


def test_the_picker_pins_the_folder_it_is_showing():
    """Without this the list scrolls and nothing on screen says what it is a
    list OF."""
    source = inspect.getsource(backfill.choose_directory)
    assert "header=" in source, "the picker must pass a pinned header"
    assert "Folder: {here}" in source or 'f"Folder: {here}"' in source


def test_the_pinned_header_is_not_counted_in_the_prompt_height():
    """`frame_rows` reserves the header band separately; adding it to `height`
    would shrink the body twice and reintroduce the clipping."""
    source = inspect.getsource(backfill.choose_directory)
    assert "height=tui.content_height(message, choices)" in source
