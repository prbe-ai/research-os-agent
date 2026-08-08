"""The always-loaded pointer into Probe, written to the selected agent's memory.

A skill has to be SELECTED before its body is read; global agent instructions
(`CLAUDE.md` or `AGENTS.md`) are in context on every turn of every session. That
difference decides whether tracking happens. Observed directly: a session whose instructions mandated searching Probe before
design work used the read surfaces perfectly all session, and never once
registered a project, an experiment, or a note -- because the write side had no
equivalent standing rule. Same agent, same session, same tools. The only
asymmetry was which surface carried the instruction.

WHAT GOES IN IT IS THE WHOLE DESIGN. This block names SURFACES, never procedures.
Procedures rot: in eight days the note vocabulary was added (#144), re-triggered
(#149), replaced by a markdown file (#150), and then moved off artifacts onto a
project column. Any copy of the commands written before today would now be
teaching `probe note add --kind decision`, which no longer exists. This file cannot be reached by a release -- it lives in the
researcher's home directory on their machine -- so anything version-specific in
it is stale the moment the skills change and stays stale forever. Naming the
skills and letting THEM carry the detail is what makes an unreachable copy safe.

It is user-global, so it fires in repositories that have nothing to do with
research. That is why the rule is conditional on the work being research rather
than phrased as an unconditional instruction: a global file that orders an agent
to register a project while it is fixing a CSS bug teaches the agent to ignore
the block.
"""

from __future__ import annotations

import os
from pathlib import Path

from probe.sdk.durable import write_text_atomic

BEGIN_MARKER = "<!-- probe-research:begin (managed by `probe wizard`) -->"
END_MARKER = "<!-- probe-research:end -->"

#: Bumped when POINTER_BODY changes, so an older installed block is recognised
#: as stale and rewritten instead of being left in place or duplicated.
POINTER_VERSION = 5

POINTER_BODY = """## Probe research tracking

When the work is research -- training, evaluation, sweeps, ablations, data
curation, benchmarks, designing any of them, or building and provisioning the
pipelines, data and hardware they run on -- register the project in Probe
BEFORE designing or scaffolding, not after a run exists; add the experiment
when there is a hypothesis to test.

Before proposing a research direction or implementation, search the Probe
Research knowledge base for prior experiments, decisions, documents, artifacts
and captured coding-agent sessions that could change the plan. Use the installed
`probe-research` MCP read surface; do not treat the local repository as the
team's complete research history. If that read surface is unavailable, say so
instead of silently proceeding as though no prior work exists.

Append decisions, findings and reversals to the project's notes as they happen,
and read them on arrival. A plan-first or approval-gated brief does not defer
this: registering is not the gated action.

Read the team wiki on arrival, alongside the project's notes: one markdown page
per team saying what this lab works on and what it has already learned. It rides
along on Probe's structured browse read, and `probe wiki` is its command group.
Correct it when you learn something that contradicts it -- it is the team's
current account, not a permanent record.

Use the `probe-research:start-research-work` and
`probe-research:track-research-work` skills rather than improvising CLI calls --
they carry the current commands, which change.

Every project, experiment and run carries a dashboard `url`. When you report one
as created or finished, end with that link -- the one you were given, never one
you assemble.
"""


def memory_path(source: str | None = None) -> Path:
    """The selected agent's user-global instruction file.

    Codex reads ``$CODEX_HOME/AGENTS.md`` (``~/.codex/AGENTS.md`` by
    default), while Claude Code reads ``$CLAUDE_CONFIG_DIR/CLAUDE.md``. The
    wizard configures either agent, so writing one hard-coded filename makes a
    successful Codex run install guidance that Codex never sees.

    `CLAUDE_CONFIG_DIR` moves the whole config directory, and a researcher who
    sets it gets a different `CLAUDE.md`. Writing to a hardcoded `~/.claude`
    there would create a second memory file that Claude Code never reads, and
    the wizard would report success over a file with no effect.
    """
    selected = (source or os.environ.get("PROBE_AGENT") or "claude_code").strip().lower()
    if selected == "codex":
        configured = os.environ.get("CODEX_HOME")
        root = Path(configured).expanduser() if configured else Path.home() / ".codex"
        return root / "AGENTS.md"

    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return root / "CLAUDE.md"


def render_block(*, version: int = POINTER_VERSION) -> str:
    """The managed block, markers included."""
    return f"{BEGIN_MARKER}\n<!-- v{version} -->\n{POINTER_BODY}{END_MARKER}\n"


class DamagedBlock(Exception):
    """The markers are present but are not one clean BEGIN/END pair.

    Its whole job is to stop us GUESSING which bytes are ours. Every guess is a
    span, and every span we rewrite deletes whatever a researcher wrote inside
    it -- so a file we cannot read unambiguously is one we refuse to touch.
    """


def _find_block(text: str) -> tuple[int, int] | None:
    """Span of the managed block in `text`, or None when there is none.

    Returns the OUTER span (marker to marker inclusive) so a rewrite replaces
    the markers too -- otherwise a marker rename would orphan the old pair and
    the next install would append a second block below it.

    Raises DamagedBlock for anything that is not exactly one pair. It used to
    return None for an orphan BEGIN, and `install` reads None as "append" --
    which produced TWO opening markers and one close. On the NEXT run this
    function walked from the orphan BEGIN to our real END and the rewrite
    swallowed everything between them: the researcher's own rules, silently,
    while reporting "Refreshed the Probe block". The file this module exists to
    write to is the one it destroyed.
    """
    opens = text.count(BEGIN_MARKER)
    closes = text.count(END_MARKER)
    if opens == 0 and closes == 0:
        return None
    if opens != 1 or closes != 1:
        raise DamagedBlock(f"expected one {BEGIN_MARKER}/{END_MARKER} pair, found {opens}/{closes}")
    start = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER, start)
    if end == -1:
        # The close exists but sits ABOVE the open, so there is no span to speak
        # of -- someone reordered the file by hand.
        raise DamagedBlock("the end marker precedes the begin marker")
    return start, end + len(END_MARKER)


def installed_version(path: Path | None = None) -> int | None:
    """Version of the block currently in the file, or None if absent.

    An unparseable version on a present block reports 0, not None: the block IS
    there, and returning None would make the caller append a duplicate.
    """
    path = path or memory_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        span = _find_block(text)
    except DamagedBlock:
        # PRESENT and unusable. Reporting absence here would make the wizard
        # append a second block to a file that already has a stray marker,
        # which is exactly how the damage compounds.
        return 0
    if span is None:
        return None
    block = text[span[0] : span[1]]
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("<!-- v") and stripped.endswith("-->"):
            try:
                return int(stripped[6:-3].strip())
            except ValueError:
                return 0
    return 0


def is_installed(path: Path | None = None) -> bool:
    return installed_version(path) is not None


def is_current(path: Path | None = None) -> bool:
    return installed_version(path) == POINTER_VERSION


def install(path: Path | None = None) -> bool:
    """Write or refresh the block. Returns True if the file changed.

    Everything outside the markers is preserved byte for byte. This file is the
    researcher's own memory -- the rules they wrote for themselves are the
    reason it is worth writing to at all, and clobbering them would be a worse
    outcome than never having written anything.
    """
    path = path or memory_path()
    block = render_block()

    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, block)
        return True

    span = _find_block(original)
    if span is None:
        separator = (
            ""
            if original.endswith("\n\n") or not original.strip()
            else ("\n" if original.endswith("\n") else "\n\n")
        )
        updated = f"{original}{separator}{block}"
    else:
        updated = original[: span[0]] + block.rstrip("\n") + original[span[1] :]

    if updated == original:
        return False
    write_text_atomic(path, updated)
    return True


def remove(path: Path | None = None) -> bool:
    """Drop the block, leaving the rest of the file alone. True if it changed.

    Unticking the menu row must not delete a file the researcher owns, so a
    file whose ONLY content was our block is left empty rather than unlinked.
    """
    path = path or memory_path()
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    # OSError and UnicodeDecodeError PROPAGATE. They used to be swallowed into
    # `False`, which the caller renders as "nothing to do" and prints nothing at
    # all: the researcher unticked the row, saw silence, and the rule kept
    # firing in every session. "I could not read it" is not "it is already
    # clean", and only one of those is safe to report as success.
    span = _find_block(original)
    if span is None:
        return False
    updated = (original[: span[0]].rstrip("\n") + "\n" + original[span[1] :].lstrip("\n")).lstrip(
        "\n"
    )
    if updated.strip() == "":
        updated = ""
    write_text_atomic(path, updated)
    return True
