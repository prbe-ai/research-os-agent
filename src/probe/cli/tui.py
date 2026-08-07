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
from collections.abc import Sequence
from typing import NamedTuple

#: Returned by a prompt when the user pressed Escape. Distinct from None, which
#: questionary already uses for Ctrl-C -- "go back one step" and "abandon the
#: whole wizard" are different intentions and must not collapse into each other.
BACK = object()

#: Wide enough for the longest description, narrow enough to stay readable on a
#: full-screen terminal. Centering a block wider than this gains nothing.
CONTENT_WIDTH = 76

#: Rows kept clear at the top AND bottom of every wizard screen.
#:
#: Content welded to row 0 reads as output that has already scrolled past --
#: you cannot tell, looking at it, whether something was cut off above. The
#: same at the bottom, where a choice list ending on the last row looks like a
#: list with more in it. Two rows of air says "this is the whole screen"
#: without spending a terminal on whitespace.
#: Blank rows kept above and below every framed screen. Three, not one: at one
#: the content reads as welded to the terminal chrome, and at two a prompt whose
#: message block scrolls still ends up with its first line against the edge.
MARGIN = 3


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


class Frame(NamedTuple):
    """How one full screen's rows are spent, top to bottom."""

    top: int
    header: int
    body: int
    bottom: int


def frame_rows(available: int, height: int | None, header: int = 0) -> Frame:
    """Split `available` screen rows into top margin, header, body, bottom margin.

    The one place the margin arithmetic lives, so `center_vertically` and the
    tests cannot drift apart. Guarantees, in order of precedence:

    1. Every field is >= 0 and the four sum to exactly `available`. A negative
       Dimension is not a cosmetic bug -- prompt_toolkit raises on it, and the
       whole point of this module's guards is that layout never takes the
       prompt down with it.
    2. `body` never exceeds `available - header - 2*MARGIN`. Content taller
       than that SCROLLS inside the frame rather than overflowing past the
       bottom edge, which is why the body gets a fixed height instead of its
       preferred one.
    3. `top` and `bottom` are each at least MARGIN -- until the terminal is too
       short to afford it, at which point the MARGIN shrinks. Whitespace is
       what gives way on a tiny screen; the content is not amputated for it.
    """
    available = max(1, available)
    header = max(0, min(header, available - 1))
    margin = max(0, min(MARGIN, (available - header - 1) // 2))
    body_max = max(1, available - header - 2 * margin)
    body = body_max if height is None else max(1, min(height, body_max))
    extra = max(0, available - header - body)
    top = min(extra, max(margin, extra // 2))
    return Frame(top=top, header=header, body=body, bottom=extra - top)


def top_spacer(height: int) -> int:
    """Blank rows `page()` prints above a block of `height` rows.

    `page()` cannot reserve rows the way a prompt_toolkit layout can -- it is
    plain prints onto a screen `clear()` has already blanked, so the bottom
    margin is simply the rows it declines to fill. That makes the top spacer
    the only knob, and it carries both jobs: the MARGIN, and the centring.

    Two rules survive from before the margin existed:

    * A block taller than the SCREEN still gets no spacer at all. Centring it
      would only choose which end to amputate, and the top is the end with the
      verdict on it.
    * Between those, the margin gives way before the content does: a block that
      fits the screen but not the framed area keeps every line, with whatever
      margin is left over.
    """
    screen = max(1, rows())
    framed_height = max(1, screen - 2 * MARGIN)
    return min(MARGIN + max(0, framed_height - height) // 2, max(0, screen - height))


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
    # Margin + centring in one number -- see top_spacer(). A block taller than
    # the screen still gets no spacer: centring it would only choose which end
    # to amputate, and the top is the end with the verdict.
    #
    # No trailing newlines for the bottom margin. `clear()` already blanked the
    # screen, so the rows below the block ARE the margin; printing them would
    # scroll the top away on any page that fills the frame.
    height = len(body) + (2 if prompt else 0)
    print("\n" * top_spacer(height), end="")
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

    Still used by prompts that let questionary draw the box. The capability
    picker draws its own -- see `TICK` / `UNTICK` and `checkbox_control`.
    """
    from questionary.prompts import common

    common.INDICATOR_SELECTED = "✔"
    common.INDICATOR_UNSELECTED = "○"


#: The box we draw OURSELVES, on rows that carry one.
TICK = "✔"
UNTICK = "○"


def checkbox_control(question):
    """The questionary InquirerControl behind a checkbox prompt, or None.

    Reached through the layout because `checkbox()` keeps the control in a
    closure and hands back only a Question. Same posture as
    `center_vertically`: questionary is pinned `>=2.0,<3`, the codebase already
    patches its module-level indicator constants, and every reach-in here is
    guarded so a reshuffle degrades the prompt instead of breaking it.
    """
    try:
        from questionary.prompts.common import InquirerControl

        def walk(container):
            for child in getattr(container, "children", []) or []:
                yield from walk(child)
            inner = getattr(container, "content", None)
            if isinstance(inner, InquirerControl):
                yield inner
            elif inner is not None:
                yield from walk(inner)

        return next(walk(question.application.layout.container), None)
    except Exception:  # noqa: BLE001 - a prompt that renders is worth more than a box
        return None


def draw_own_boxes(question) -> bool:
    """Stop questionary drawing the box, so WE decide which rows get one.

    `use_indicator` is all-rows-or-nothing and `checkbox()` does not expose it
    (passing it reaches PromptSession and raises), so it is set on the control
    after construction. The reason we want it off at all: a row you can put the
    cursor on ALWAYS gets a box, and an action row -- "Next" -- rendered as
    `○ Next` reads as an option someone forgot to tick.

    Returns whether it worked, so the caller can fall back to questionary's own
    box rather than shipping a picker with no boxes at all.
    """
    control = checkbox_control(question)
    if control is None:
        return False
    control.use_indicator = False
    return True


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


def header_lines(header: str | Sequence[str] | None) -> list[str]:
    """Normalise a `header=` argument into the rows that get pinned.

    Public because the caller needs to know what its header will cost before it
    counts the rest: the header sits OUTSIDE `content_height()`, which counts
    only what questionary draws. Long lines are wrapped to CONTENT_WIDTH and
    every line carries the same `left_pad()` as the block below it, so a pinned
    path lines up with the question rather than floating at column 0.

    Accepts a plain string (split on newlines) or any sequence of lines.
    """
    if header is None:
        return []
    raw = header.splitlines() if isinstance(header, str) else list(header)
    out: list[str] = []
    for line in raw:
        for part in wrap(line) if len(line) > CONTENT_WIDTH else [line]:
            out.append(indent(part) if part.strip() else "")
    return out


def center_vertically(
    question, height: int | None = None, header: str | Sequence[str] | None = None
):
    """Render the prompt FULL SCREEN, vertically centred, inside a margin.

    questionary renders inline, growing downward from the cursor, so anything
    taller than the room below it scrolls -- which is what kept eating the first
    rows of the state block, and is worse in a block-based terminal like Warp
    where the block boundary is not where a classic terminal's would be.

    Full screen removes the problem outright: prompt_toolkit owns the viewport,
    so there is no "above" to scroll into. It is also the only way centring
    becomes possible at all.

    The layout is four bands -- top margin, pinned header, body, bottom margin
    -- sized by `frame_rows()`. Three consequences worth stating:

    * The top and bottom bands are EXACT, so the body is whatever is left. That
      is what makes content taller than the frame scroll inside it instead of
      running off the bottom edge: squeezed below its preferred height,
      questionary's choice window scrolls itself to keep the pointed row
      visible -- it emits a `[SetCursorPosition]` token, and that is what
      prompt_toolkit scrolls to. The state block above it stays put.
    * NOT a ScrollablePane, which is the obvious thing to reach for and is
      wrong here. It follows the FOCUSED window's cursor, and the focused
      window is questionary's input buffer up in the message block -- so the
      pane sits at scroll 0 and the choices below the fold become unreachable.
      Bounding the band lets each window scroll on its own cursor instead.
    * `header` rows are pinned ABOVE the body, so a folder path stays readable
      at the bottom of a long list. They are NOT part of `height`.

    Every reach into questionary/prompt_toolkit internals is guarded, same
    posture as `checkbox_control`: questionary is pinned `>=2.0,<3` and a
    library reshuffle must degrade the prompt, never fail to render it.
    """
    try:
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension

        app = question.application
        inner = app.layout.container
        focused = app.layout.current_window
        pinned = header_lines(header)

        def frame() -> Frame:
            from prompt_toolkit.application import get_app

            return frame_rows(get_app().output.get_size().rows, height, len(pinned))

        def band(name: str):
            return lambda: Dimension.exact(getattr(frame(), name))

        bands = [Window(height=band("top"))]
        if pinned:
            bands.append(
                Window(
                    height=band("header"),
                    content=FormattedTextControl(
                        [("class:question", "\n".join(pinned))], focusable=False
                    ),
                    # Exact height is only exact if a long line cannot silently
                    # become two rows and push the body down.
                    wrap_lines=False,
                    always_hide_cursor=True,
                )
            )
        bands += [inner, Window(height=band("bottom"))]

        app.full_screen = True
        app.layout = Layout(HSplit(bands), focused_element=focused)
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


def ask(
    question, height: int | None = None, header: str | Sequence[str] | None = None
):
    """Run a prompt full-screen and centred, with Escape bound.

    Returns BACK, None (Ctrl-C), or the chosen value.

    `height` is the counted row total from content_height(); without it the
    prompt still fills the frame, just top-aligned inside it.

    `header` is a string or list of lines pinned ABOVE the scrolling body, so
    it stays on screen at the bottom of a long list -- what a folder picker
    needs to keep the current path readable. Do NOT add its rows to `height`:
    the frame reserves them separately (see `header_lines`).
    """
    return bind_escape(center_vertically(question, height, header=header)).ask()


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
