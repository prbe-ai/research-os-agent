"""Terminal presentation for the wizard.

A wizard is a place you are IN, not a transcript you scroll. Every earlier
version redrew the state block and the menu below the previous one, so a few
actions left a screen of stale copies and you had to work out which block was
current. This module clears between steps so there is exactly one truth on
screen.

Kept separate from setup.py so the wizard's logic never has to think about
escape codes, and so all of it stays out of `probe log`'s import path.
"""

from __future__ import annotations

import os
import shutil
import sys

#: Returned by a prompt when the user pressed Escape. Distinct from None, which
#: questionary already uses for Ctrl-C -- "go back one step" and "abandon the
#: whole wizard" are different intentions and must not collapse into each other.
BACK = object()

#: Wide enough for the longest description, narrow enough to stay readable on a
#: full-screen terminal. Centering a block wider than this gains nothing.
CONTENT_WIDTH = 76


def interactive() -> bool:
    """Both ends must be a TTY: piped stdin with a TTY stdout is a script."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def columns() -> int:
    try:
        return shutil.get_terminal_size().columns
    except OSError:
        return 80


def left_pad() -> int:
    """Spaces needed to centre a CONTENT_WIDTH block in this terminal.

    Zero on a narrow terminal: padding something that already does not fit only
    makes it wrap, which is worse than being left-aligned.
    """
    slack = columns() - CONTENT_WIDTH
    return max(0, slack // 2)


def clear() -> None:
    """Wipe the screen and park the cursor at the top.

    Only when interactive -- doing this to a pipe or a CI log would emit escape
    codes into captured output, and there is no screen to clear anyway.
    """
    if not interactive():
        return
    # \033[3J also drops the scrollback, so the previous step cannot be
    # recovered by scrolling. That is deliberate: the wizard is showing live
    # state, and a stale copy of it further up is a lie waiting to be read.
    sys.stdout.write("\033[3J\033[2J\033[H")
    sys.stdout.flush()


def indent(text: str, pad: int | None = None) -> str:
    """Shift every line right so a block sits centred."""
    prefix = " " * (left_pad() if pad is None else pad)
    return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())


def say(text: str = "") -> None:
    """Print centred, so output lines up with the centred prompts."""
    print(indent(text) if text else "")


def style():
    """Questionary styling.

    Two deliberate choices:

    * `highlighted` is `noreverse`. The default inverts the whole entry into a
      block of background colour, which on a multi-line choice paints three
      lines of solid white and is genuinely hard to read.
    * `selected` is green, so a ticked box reads as ticked at a glance rather
      than needing you to compare two similar glyphs.
    """
    import questionary

    return questionary.Style(
        [
            ("qmark", "fg:#5f87ff bold"),
            ("question", "bold"),
            ("pointer", "fg:#5f87ff bold"),
            ("highlighted", "noreverse bold"),
            ("selected", "fg:#00af5f noreverse"),
            ("separator", "fg:#6c6c6c"),
            ("instruction", "fg:#6c6c6c"),
            ("answer", "fg:#00af5f bold"),
        ]
    )


def use_checkmarks() -> None:
    """Swap questionary's ●/○ for ✔/○.

    `common` does `from questionary.constants import INDICATOR_SELECTED`, so the
    name is bound at import time and patching `questionary.constants` has no
    effect. The module that actually reads it is the one to patch.
    """
    from questionary.prompts import common

    common.INDICATOR_SELECTED = "✔"
    common.INDICATOR_UNSELECTED = "○"


def bind_escape(question):
    """Make Escape resolve the prompt to BACK.

    questionary gives Ctrl-C (abandon) but nothing for "I chose wrong, take me
    back one step", which is the far more common intention in a menu you are
    meant to sit in.
    """
    try:
        bindings = question.application.key_bindings

        @bindings.add("escape", eager=True)
        def _(event) -> None:  # pragma: no cover - requires a live terminal
            event.app.exit(result=BACK)

    except Exception:  # noqa: BLE001 - never let styling break the prompt
        pass
    return question


def ask(question):
    """Run a prompt with Escape bound. Returns BACK, None (Ctrl-C), or a value."""
    return bind_escape(question).ask()


def rows() -> int:
    try:
        return shutil.get_terminal_size().lines
    except OSError:
        return 24


# NOTE: no vertical centring. questionary emits the qmark BEFORE the message,
# so leading newlines inside the message strand a lone `?` at the top of the
# screen with the content pushed below it. Padding outside the prompt does not
# work either -- prompt_toolkit renders relative to the cursor, so the padding
# is what scrolls the top away. Vertically centred would be nice; wrong-looking
# is worse than top-aligned.


def framed(title: str, lines: list[str], question: str) -> str:
    """State block + question as ONE prompt message.

    Printing the state separately and letting questionary render underneath is
    what cut the top off: prompt_toolkit takes the screen after the print, and
    anything already emitted scrolls away. Handing it the whole block means it
    owns the layout and nothing can drift out of view.
    """
    block = [title, "", *lines, "", question]
    padded = [(" " * left_pad() + ln if ln.strip() else "") for ln in block]
    # The first line rides behind the qmark, which already carries the indent.
    padded[0] = padded[0].lstrip()
    return "\n".join(padded)


def qmark() -> str:
    """The `?` marker, carrying the indent itself.

    Padding the MESSAGE instead leaves the marker stranded at column 0 with its
    text 78 columns away, which is what shipped in 0.13.0 and looks like a
    rendering fault. questionary emits `(qmark)(space)(message)`, so putting the
    pad inside the marker moves the whole line together.
    """
    return " " * left_pad() + "?"


def pointer() -> str:
    """The `»` marker, likewise carrying the indent.

    questionary draws `" {pointer} "` on the highlighted row and
    `" " * (2 + len(pointer))` on the others, so a padded pointer keeps every
    row aligned -- selected and unselected end at the same column.
    """
    return " " * max(0, left_pad() - 1) + "»"


def body_indent() -> str:
    """Where a choice's continuation lines start.

    The pointer prefix only exists on the FIRST line of a choice; wrapped
    description lines get nothing, so they carry their own pad.
    """
    return " " * (left_pad() + 2)
