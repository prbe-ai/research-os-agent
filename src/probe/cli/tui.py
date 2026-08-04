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


def wrap(text: str, width: int = CONTENT_WIDTH) -> list[str]:
    """Break a paragraph at CONTENT_WIDTH so centring has something to centre.

    A line wider than the block runs off the right edge once it carries the
    left pad, and then wraps at whatever column the terminal happens to end at
    -- which reads as a broken layout rather than a long sentence.

    Leading indent is preserved. A bullet hangs two further, to clear its own
    "- " marker; a plain paragraph stays flush, because indenting its second
    line makes it look like a list item that lost its bullet.
    """
    import textwrap

    lead = " " * (len(text) - len(text.lstrip()))
    hang = lead + ("  " if text.lstrip().startswith("- ") else "")
    return (
        textwrap.wrap(text.strip(), width, initial_indent=lead, subsequent_indent=hang) or [""]
    )


def say(text: str = "") -> None:
    """Print centred -- but only when there is a screen to centre in.

    Piped output stays flush-left: an indent there is noise every log grep has
    to strip, and the one consumer that never sees the layout is CI.
    """
    if not text:
        print()
    elif interactive():
        print(indent(text))
    else:
        print(text)


def page(lines: list[str], prompt: str | None = None) -> None:
    """Show a block of OUTPUT as a centred page, like every prompt in the wizard.

    Results used to print from column 0 at the top of the screen while every
    prompt sat centred, so finishing an action visibly threw you out of the
    wizard and back into a bare terminal. It is the same wizard; it should look
    like it.

    Plain prints, not prompt_toolkit: after clear() the cursor owns a blank
    screen, so vertical centring is just N blank lines -- and unlike a
    full-screen app, what it draws is still there after the wizard exits.
    """
    raw = "\n".join(lines).splitlines()
    if not interactive():
        # Verbatim: hard-wrapping piped output only breaks a grep.
        print("\n".join(raw))
        return
    body: list[str] = []
    for line in raw:
        # Wrapped HERE, not by the terminal: a line that overflows the block
        # wraps back to column 0, which breaks the centred column the whole
        # page is built on.
        body += wrap(line) if len(line) > CONTENT_WIDTH else [line]
    clear()
    # A block taller than the screen gets no spacer: centring it would only
    # choose which end to amputate, and the top is the end with the verdict.
    height = len(body) + (2 if prompt else 0)
    print("\n" * max(0, (rows() - height) // 2), end="")
    for line in body:
        print(indent(line) if line.strip() else "")
    if prompt is not None:
        print()
        input(indent(prompt))


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


def content_height(message: str, choices=()) -> int:
    """How many rows the prompt will occupy.

    Counted, not asked. `Container.preferred_height()` needs a running event
    loop and returns a placeholder without one, which silently produced a
    22-row spacer for 44 rows of content and pushed the BOTTOM off instead --
    the same bug wearing a different hat.
    """
    import questionary

    rows = message.count("\n") + 1  # the message block, question included
    for choice in choices:
        if isinstance(choice, questionary.Separator):
            rows += 1
        else:
            rows += str(getattr(choice, "title", "")).count("\n") + 1
    # Only a list prompt draws an instruction line. A confirm renders its
    # answer inline on the last message row, so counting one for it would sit
    # the whole page half a row high.
    return rows + (1 if choices else 0)


def center_vertically(question, height: int | None = None):
    """Render the prompt FULL SCREEN and vertically centred.

    questionary renders inline, growing downward from the cursor, so anything
    taller than the room below it scrolls -- which is what kept eating the first
    rows of the state block, and is worse in a block-based terminal like Warp
    where the block boundary is not where a classic terminal's would be.

    Full screen removes the problem outright: prompt_toolkit owns the viewport,
    so there is no "above" to scroll into. It is also the only way centring
    becomes possible at all.

    Content taller than the screen gets NO spacer. Centring something that does
    not fit only chooses which end to amputate.
    """
    try:
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.dimension import Dimension

        app = question.application
        inner = app.layout.container
        focused = app.layout.current_window

        def spacer() -> Dimension:
            from prompt_toolkit.application import get_app

            available = get_app().output.get_size().rows
            if height is None or height >= available:
                return Dimension.exact(0)
            return Dimension.exact((available - height) // 2)

        app.full_screen = True
        app.layout = Layout(HSplit([Window(height=spacer), inner]), focused_element=focused)
    except Exception:  # noqa: BLE001 - layout is cosmetic, never break the prompt
        pass
    return question


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


def ask(question, height: int | None = None):
    """Run a prompt full-screen and centred, with Escape bound.

    Returns BACK, None (Ctrl-C), or the chosen value. `height` is the counted
    row total from content_height(); without it the prompt still renders
    full-screen, just top-aligned.
    """
    return bind_escape(center_vertically(question, height)).ask()


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


#: How a step reads in the plan list, by state.
_PENDING, _ACTIVE, _OK, _FAILED = "pending", "active", "ok", "failed"
_MARKS = {_PENDING: "·", _ACTIVE: "»", _OK: "✔", _FAILED: "✗"}

#: Width of the bar itself, inside its brackets. Comfortably under
#: CONTENT_WIDTH so the bar plus its counter never needs wrapping.
_BAR_WIDTH = 28


class Progress:
    """The install phase as ONE live screen, not a stream of lines.

    The phase used to collect every message into a list and print the lot after
    all of it had finished. Since a step here shells out to `claude` -- up to
    six times, each with its own multi-second timeout -- that meant the screen
    sat on "This run will:" and nothing else for minutes, which reads as a hang.
    It also printed through `say()`, which left-pads but does NOT wrap, so a
    200-character error from `claude` ran off the block and wrapped at the
    terminal's right edge back to column 0.

    Both problems are `page()`'s job, so this drives `page()`: it wraps, it
    centres horizontally AND vertically, and it is already tested. Each state
    change redraws the whole screen.

    TWO RENDERINGS, because a screen and a log want opposite things:

      interactive   clear + redraw the whole block, bar and all
      piped/CI      append ONE line per step as it resolves

    A redraw into a pipe would print the growing block once per step (page()
    skips the clear when it is not interactive), which is log spam rather than
    progress. Appending keeps `probe wizard --yes` greppable and -- the reason
    it matters -- leaves evidence of WHICH step a wedged CI job died on.

    Results accumulate rather than scrolling past: `clear()` emits \\033[3J,
    which drops the scrollback, so anything not re-drawn is gone for good.
    """

    def __init__(self, title: str, steps: list[str]) -> None:
        self._title = title
        self._steps = list(steps)
        self._state = [_PENDING] * len(steps)
        self._results: list[str] = []
        self._queued: list[str] = []

    # -- state ---------------------------------------------------------------

    def start(self, index: int) -> None:
        if 0 <= index < len(self._state):
            self._state[index] = _ACTIVE
        self.render()

    def finish(self, index: int, *, ok: bool = True) -> None:
        if 0 <= index < len(self._state):
            self._state[index] = _OK if ok else _FAILED
            self._queued.append(
                f"[{self._done()}/{len(self._steps)}] {self._steps[index]}"
                f" ... {'ok' if ok else 'FAILED'}"
            )
        self.render()

    def note(self, *lines: str) -> None:
        """Attach output to the screen. Kept until the phase ends."""
        for line in lines:
            if line:
                self._results.append(line)
                self._queued.append(line)
        self.render()

    # -- rendering -----------------------------------------------------------

    def _done(self) -> int:
        return sum(1 for s in self._state if s in (_OK, _FAILED))

    def bar(self) -> str:
        total = len(self._steps) or 1
        filled = round(_BAR_WIDTH * self._done() / total)
        return f"[{'#' * filled}{'-' * (_BAR_WIDTH - filled)}]  {self._done()}/{total}"

    def block(self) -> list[str]:
        lines = [self._title, ""]
        lines += [
            f"  {_MARKS[state]} {step}"
            for step, state in zip(self._steps, self._state, strict=False)
        ]
        lines += ["", self.bar()]
        if self._results:
            lines += ["", *self._results]
        return lines

    def render(self) -> None:
        if not interactive():
            # Append-only: emit what is NEW, never the whole block again.
            for line in self._queued:
                print(line)
            self._queued.clear()
            return
        self._queued.clear()
        page(self.block())

    def close(self) -> None:
        """Leave the finished screen on display, results and all."""
        self.render()
