"""`probe wizard` -> Import existing work: point one agent at one folder.

The wizard cannot read a research folder. Forty files -- a report, a config,
three CSVs, a training script -- are an experiment to a person and an
undifferentiated pile to `find`, and the mapping from file to experiment is
exactly the judgment nobody wrote down at the time. So the wizard does the two
things a program is better at and hands the middle to an agent:

* ENUMERATE (here, deterministic): walk the folder, count files and bytes.
  That count is the DENOMINATOR, and it is trustworthy precisely because no
  model produced it. Silent 2% coverage that reads as "done" is the failure
  mode this whole module exists to make impossible.
* DECIDE (the agent): what these files are, which deserve a description,
  whether they amount to an experiment. If the folder is large the agent
  subdivides it ITSELF -- it can see the shape, and a histogram cannot.
* RECONCILE (here, deterministic): what landed, against what was counted.

The agent is launched with a FIXED anchor. It may decide what a folder means;
it may not decide what the folder is called. A second run inventing a second
project slug is the one mistake in this flow nobody can undo, so the slug is
resolved by code before the agent starts and passed in as a given.

stdlib plus a lazy questionary/client import, like the rest of cli/ -- `probe
log` runs inside training loops and must not pay for any of this.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..sdk.errors import NotFoundError

#: Never walked, never offered, never counted. Not a judgment call about what
#: is interesting -- these are machine-generated trees whose file counts would
#: swamp the denominator and whose contents nobody wrote.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".ipynb_checkpoints",
        ".tox",
        ".eggs",
        ".idea",
        ".vscode",
        ".probe",
    }
)

#: Above this, record the PATH and not the bytes. A 1.2B-parameter checkpoint
#: is ~10GB and there is one every 2k steps; uploading them is neither possible
#: nor useful, and on a shared cluster mount a `file://` reference resolves
#: from every other researcher's pod anyway.
REFERENCE_ABOVE_BYTES = 100 * 1024 * 1024

#: Stop counting a subtree here. The browser scans every child directory to
#: label it, and on a network mount with a million-file dataset an exact count
#: costs a minute for a number nobody reads past the first two digits.
COUNT_CAP = 20_000

#: One page of the anchor listing, matching the server's per-request artifact
#: ceiling. Hitting it exactly means "at least this many", never "this many".
RECONCILE_PAGE = 1_000

class Agent(StrEnum):
    """Which coding agent reads the folder.

    Both are driven headlessly and both do the work the same way -- read files,
    shell out to `probe artifact add` -- but they are CONFINED differently, and
    the difference is not cosmetic. See CONFINEMENT below.
    """

    CLAUDE = "claude"
    CODEX = "codex"


#: Display name and the one-line honest description of what each agent may do.
#: Shown at the picker, because "an agent is about to read this folder
#: unattended" is a decision someone should make with the isolation in view.
AGENT_COPY: dict[Agent, tuple[str, str]] = {
    Agent.CLAUDE: (
        "Claude Code",
        "Limited to the probe CLI and reading — it cannot write, delete or fetch.",
    ),
    Agent.CODEX: (
        "Codex",
        "Sandboxed to this folder, but any command is allowed inside it.",
    ),
}

#: CONFINEMENT, Claude Code: a TOOL ALLOWLIST. It may call the probe CLI and
#: read; it may not write, delete, or reach the network by any other route.
#: Backfill is a read-and-upload job, so anything broader is blast radius with
#: no upside -- and this runs unattended over folders nobody has audited.
AGENT_TOOLS = "Bash(probe:*),Read,Glob,Grep,Task"

#: CONFINEMENT, Codex: a FILESYSTEM+NETWORK SANDBOX, which is a coarser
#: instrument. Codex confines where commands may act, not WHICH commands run,
#: so there is no equivalent of `Bash(probe:*)` -- inside the sandbox the agent
#: may run anything. That is why AGENT_COPY says so out loud.
#:
#: `workspace-write` rather than `read-only` because the upload has to reach the
#: network, and read-only mode has none. `--skip-git-repo-check` is REQUIRED,
#: not tidiness: Codex refuses to run outside a git repo, and a research folder
#: on a shared mount is virtually never one.
CODEX_ARGS = (
    "-s",
    "workspace-write",
    "-c",
    "sandbox_workspace_write.network_access=true",
    "--skip-git-repo-check",
)


@dataclass(frozen=True)
class Census:
    """What a deterministic walk found. The denominator, and the browser's labels."""

    files: int
    bytes: int
    capped: bool = False

    def describe(self) -> str:
        count = f"{self.files:,}{'+' if self.capped else ''}"
        noun = "file" if self.files == 1 and not self.capped else "files"
        return f"{count} {noun}   {human_bytes(self.bytes)}"


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover - loop always returns


def scan(root: Path, *, cap: int = COUNT_CAP) -> Census:
    """Count files and bytes under `root`, pruning SKIP_DIRS.

    Stats, never reads: the size comes from the dirent, so this stays cheap
    even on a network mount. `cap` bounds the walk so one enormous subtree
    cannot stall the browser -- a capped Census says so rather than reporting
    a number that is quietly wrong.
    """
    files = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            files += 1
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                # A broken symlink or a race with a training job deleting a
                # checkpoint. It still counts as a file -- the agent will meet
                # it too, and a denominator that skips it hides the mismatch.
                pass
            if files >= cap:
                return Census(files=files, bytes=total, capped=True)
    return Census(files=files, bytes=total)


def subdirectories(root: Path) -> list[tuple[Path, Census]]:
    """The browsable children of `root`, each with its own census."""
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    return [(p, scan(p)) for p in entries if p.name not in SKIP_DIRS and not p.name.startswith(".")]


def slug_for(folder: Path) -> str:
    """A project slug derived from the folder name, deterministically.

    Derived rather than asked because the same folder must resolve to the same
    project on a re-run. A prompt would let a typo fork the identity, which is
    the one failure this flow cannot recover from.
    """
    raw = folder.resolve().name.lower()
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "backfill"


def resolve_anchor(client, folder: Path, *, requested: str | None = None) -> tuple[str, str]:
    """The project this folder imports into, as ``(project_id, slug)``.

    Returns the UUID, never the slug, because that is what the agent's commands
    need: every ``/v1/projects/{project_id}`` route types the path param as a
    UUID, so a slug arrives as a 422 about UUID parsing rather than a lookup.
    The slug rides along only to be shown to a human.

    THE FOLDER DECIDES, unless someone names a project on this command.

    The AMBIENT active project (`probe project use`, `PROBE_PROJECT`) is
    deliberately NOT consulted. It used to win, on the reasoning that whoever
    set it had already answered this question -- but it answers a different
    one. `project use` sets where new RUNS go by default; it is not a statement
    that every folder imported from here on belongs there. Honouring it meant
    pointing at `anthrogen-backfill-test` and watching 37 artifacts land in
    whatever project happened to be active, which is a surprising place to have
    to go looking for them.

    `requested` is an explicit `--project` on this invocation -- a slug, or an
    id written `id:<uuid>` -- and it still wins.

    Creation goes through ``ensure_project``, whose near-miss guard refuses a
    slug that looks like a typo of one already there. A folder called
    `odyssey-v2` sitting next to an existing `odyssey_v2` should stop, not
    quietly open a second identity for the same work.
    """
    if requested:
        return _resolve_ref(client, requested)
    proj = client.ensure_project(slug_for(folder), name=folder.resolve().name)
    return str(proj["id"]), proj.get("slug") or slug_for(folder)


def _resolve_ref(client, ref: str) -> tuple[str, str]:
    """An explicitly named project slug (or ``id:<uuid>``) -> ``(project_id, slug)``.

    Shares :mod:`probe.cli.refs` with the project verbs so an anchor cannot mean
    one project here and another one there -- this names every artifact the
    import uploads, and getting it wrong files someone's whole folder into a
    stranger's project.
    """
    from . import refs

    try:
        found = refs.resolve(client, "project", ref)
    except NotFoundError as exc:
        raise ValueError(str(exc)) from None
    return found.id, found.row.get("slug") or ref


def _subdivide_line(agent: Agent) -> str:
    """How to handle a big folder — which differs by what the agent HAS.

    Claude has a Task tool, so it can fan out over the subdirectories itself.
    Codex has no equivalent; telling it to "use subagents" would invite it to
    invent something, so it is told to work through the folder in passes
    instead.
    """
    if agent is Agent.CLAUDE:
        return (
            "If the folder is large or clearly splits into several independent pieces\n"
            "of work, subdivide it and use subagents — one per piece, each with this\n"
            "same anchor."
        )
    return (
        "If the folder is large, work through it one subdirectory at a time and\n"
        "keep going until every one is done. Do not stop at a sample."
    )


def build_prompt(
    *,
    folder: Path,
    census: Census,
    project: str | None = None,
    agent: Agent = Agent.CLAUDE,
) -> str:
    """The prompt the wizard hands the agent.

    This is the actual deliverable of the feature: the user never writes it, and
    every rule that keeps the import honest lives here rather than in code the
    agent cannot see.

    THE AGENT OWNS THE PROJECT STRUCTURE. An earlier version pinned one project,
    resolved before launch, on the reasoning that an agent free to name things
    would fork identities. That traded a real problem for a worse one: a folder
    like `/workspace` holds Michael's work, Xian's work and Connor's work, and
    collapsing three lines of research into one project named after the
    directory is a wrong answer that no amount of naming discipline fixes. The
    shape of the work is exactly the judgment we are paying an agent for.

    What replaces the pin is DISCIPLINE, not a lock: list what exists, reuse
    before creating, and let `ensure_project`'s near-miss guard refuse a slug
    that reads as a typo of one already there. `--project` still forces a single
    destination for anyone who wants the old behaviour.
    """
    fixed = f"""
EVERYTHING GOES IN ONE PROJECT, named by the person who started this:

    --project {project}

Do not create any other project. If some of this work does not belong there,
say so in your summary rather than filing it elsewhere.
""" if project else """
YOU DECIDE THE PROJECTS. This is the judgement you are here for.

    First, see what already exists:
        probe project list

    Then decide. A folder is not automatically one project. `/workspace` with
    three researchers' directories under it is at least three; a single
    experiment directory is one. Split where the WORK is genuinely separate —
    different question, different model, different line of research — and keep
    it together where it is not.

    REUSE BEFORE YOU CREATE. If a project for this work already exists, file
    into it. Two projects for the same research is the one mistake here that
    nobody can undo later, and it is much easier to make than it looks: a
    second run that invents `odyssey-infill-v3` next to an existing
    `odyssey_infill_v3` has silently split the record in half.

    Name them for the WORK, not the directory. `odyssey-infill-v3` and
    `esm3-baseline` are names someone will recognise in six months;
    `workspace`, `data` and `michael` are not. Read enough of the folder to
    name it honestly before you create anything.

        probe project create <slug> --name "<human name>" \\
            --description "<what this project is, 1-3 sentences>" \\
            --tag <topic> --tag <model-or-dataset>

    ALWAYS pass --description: a plain description of the project, 1-3 sentences,
    no more. Nothing else fills it in — the server generates one only when a child
    RUN reaches a terminal status, and importing a folder creates no runs, so a
    project left undescribed here stays undescribed forever.

    `--project` takes the SLUG -- the one you just chose -- in every command
    that follows. (An id needs the `id:` prefix: `--project id:<uuid>`.)
"""
    return f"""\
You are backfilling existing research work into Probe.

FOLDER: {folder}
This folder and its subdirectories are your entire scope. Do not read outside it.
A deterministic walk counted {census.files:,} files here — roughly what you should
expect to account for.
{fixed}
Step 1 — upload everything of substance.

    files under {human_bytes(REFERENCE_ABOVE_BYTES)}:
        probe artifact add --project <project> <path>

    files over {human_bytes(REFERENCE_ABOVE_BYTES)} (checkpoints, datasets, archives):
        probe artifact add --project <project> <path> --reference --allow-missing

    The second form records the PATH and uploads no bytes. Use it for anything
    large. Do NOT pass --hash on those: fingerprinting a 10GB file over a shared
    mount costs minutes and buys nothing here.

    Skip build noise and caches (.git, __pycache__, .venv, node_modules).

Step 2 — say what things are.

    For each artifact that carries meaning — a report, a result table, a config,
    a plot, a script someone would look for again — say in one line what it is,
    what produced it, what it shows. This is the part that makes a file findable
    later, and it is the part nobody did at the time. It matters more than the
    upload.

    The description goes in --notes:

        probe artifact add --project <project> <path> \\
            --notes "<what it is, what produced it, what it shows>"

    NEVER put the description in --name. The name is the file's relative path
    and nothing else: the folder tree is built by splitting it on '/', the
    dashboard works out how to preview a file from the extension at its end, and
    both break the moment a sentence is appended to it. --notes is a real field
    on every anchor; use it and leave the name alone.

    For what no single file explains — what this folder is, how the pieces
    relate, what is missing, what a reader should not trust — write the
    project's notes, once, at the end:

        probe notes write --project <project> <file>     (or '-' for stdin)

    That is ONE markdown document per project, not a per-file note. It REPLACES
    by default, so if you split this folder across subagents, they must pass
    --append or the last one to finish erases the rest.

Step 3 — group into experiments ONLY if the evidence is there.

    If some part of this plainly IS one experiment — a hypothesis, a method, a
    result you can point at — create it and attach those artifacts.
    If that would be a guess, DO NOT. Leave them at the project level.
    Artifacts at the project level are findable; an invented experiment is a
    wrong answer that looks like a right one.

        probe experiment create <slug> --project <project> \\
            --name "<human name>" --hypothesis "<what it tests>" \\
            --description "<what this experiment is, 1-3 sentences>"

    --description here too, and for the same reason: only a terminal run
    generates one, and this import creates none. The files that convinced you
    this was a real experiment go in `probe notes write`, not here.

{_subdivide_line(agent)}

Finish with a JSON summary on its own line. `projects` must list every project
you filed into, by slug — it is how the import is checked against the {census.files:,}
files counted above:
{{{{"files_seen": N, "files_landed": N, "files_skipped": N,
  "projects": ["<slug>", ...], "experiments_created": N,
  "summary": "one sentence on what this folder contains"}}}}
"""


def which_agent(agent: Agent) -> str | None:
    """The agent's real binary, or None.

    `shutil.which`, never a shell: `codex` in particular is commonly SHADOWED by
    a shell alias (a local wrapper here), and an alias would silently swallow
    the arguments below. A PATH lookup with no shell cannot see one.
    """
    return shutil.which(agent.value)


def available_agents() -> list[Agent]:
    """Installed agents, in menu order. Empty means backfill cannot run."""
    return [a for a in Agent if which_agent(a) is not None]


def agent_argv(agent: Agent, binary: str, prompt: str, folder: Path) -> list[str]:
    """The headless invocation for one agent.

    Both are asked for a JSONL EVENT STREAM, and that is not a preference. A
    bare `claude -p` prints nothing at all until the whole run finishes, so a
    backfill over a real folder sat silent for minutes and read as hung -- which
    is exactly what it looked like. The event stream is the only way to know the
    agent is alive, and the only way to count what it has done so far.

    Claude takes its working directory from the process (`cwd=`); Codex needs it
    named explicitly with `-C`, because it resolves its workspace -- the thing
    its sandbox is scoped to -- from that flag rather than from cwd.
    """
    if agent is Agent.CLAUDE:
        return [
            binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",  # required: stream-json emits only the result without it
            "--allowedTools",
            AGENT_TOOLS,
        ]
    return [binary, "exec", "--json", *CODEX_ARGS, "-C", str(folder), prompt]


#: Spinner frames. Something has to MOVE while the agent is thinking, or a long
#: quiet turn is indistinguishable from a hang -- the bug this replaced.
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: How an upload is recognised in either agent's event stream. Counting these
#: is what turns "still going" into "14 of 37".
_UPLOAD_MARKER = "artifact add"


@dataclass
class Activity:
    """Live state of one agent run, folded from its event stream."""

    total: int
    uploaded: int = 0
    doing: str = "starting up"
    ticks: int = 0

    def line(self, elapsed: float) -> str:
        mins, secs = divmod(int(elapsed), 60)
        done = f"{self.uploaded}/{self.total}" if self.uploaded else f"0/{self.total}"
        doing = self.doing if len(self.doing) <= 46 else self.doing[:45] + "…"
        return f"  {_SPIN[self.ticks % len(_SPIN)]} {mins}:{secs:02d} · {done} · {doing}"


def _shorten(command: str) -> str:
    """A shell command as a phrase worth showing on one line.

    The interesting part of `probe artifact add --project <uuid> /long/path`
    is the filename, and the uuid is the least interesting thing in it.
    """
    text = " ".join(command.split())
    for wrapper in ("/bin/zsh -lc ", "/bin/bash -lc ", "/bin/sh -c "):
        if text.startswith(wrapper):
            text = text[len(wrapper) :].strip("'\"")
    if _UPLOAD_MARKER in text:
        parts = [p for p in text.split() if not p.startswith("--") and p not in ("probe", "artifact", "add")]
        # Drop the project id; keep the path.
        paths = [p for p in parts if "/" in p or "." in p]
        return f"uploading {Path(paths[-1]).name}" if paths else "uploading"
    return text


def fold_event(raw: str, state: Activity) -> bool:
    """Fold one stdout line into `state`. True if it said something new.

    Tolerant by construction: both agents interleave NON-JSON on stdout (Claude
    prints the connectors warning there, Codex a stdin notice and tracing), and
    a parser that treated that as fatal would blank the display for the rest of
    the run.
    """
    import json

    line = raw.strip()
    if not line or not line.startswith("{"):
        return False
    try:
        event = json.loads(line)
    except ValueError:
        return False
    if not isinstance(event, dict):
        return False

    kind = event.get("type")

    # Claude: assistant turns carry tool_use blocks.
    if kind == "assistant":
        for block in event.get("message", {}).get("content", []) or []:
            if block.get("type") != "tool_use":
                continue
            args = block.get("input") or {}
            name = block.get("name", "")
            if name == "Bash":
                command = str(args.get("command", ""))
                if _UPLOAD_MARKER in command:
                    state.uploaded += 1
                state.doing = str(args.get("description") or _shorten(command))
            elif args.get("file_path"):
                state.doing = f"reading {Path(str(args['file_path'])).name}"
            elif args.get("pattern"):
                state.doing = f"searching {args['pattern']}"
            else:
                state.doing = name.lower() or "working"
            return True
        return False

    # Codex: item.started / item.completed carry command_execution + messages.
    if kind in ("item.started", "item.completed"):
        item = event.get("item") or {}
        if item.get("type") == "command_execution":
            command = str(item.get("command", ""))
            if kind == "item.started":
                if _UPLOAD_MARKER in command:
                    state.uploaded += 1
                state.doing = _shorten(command)
                return True
        elif item.get("type") == "agent_message" and kind == "item.completed":
            text = " ".join(str(item.get("text", "")).split())
            if text:
                state.doing = text
                return True
        return False

    if kind == "turn.started":
        state.doing = "thinking"
        return True
    return False


def launch_agent(
    folder: Path,
    prompt: str,
    *,
    agent: Agent = Agent.CLAUDE,
    timeout: float | None = None,
    stream=None,
    progress: bool = True,
    total: int = 0,
) -> tuple[bool, str]:
    """Run one headless agent inside `folder`, showing what it is doing.

    ONE self-updating line, not the transcript. A raw agent transcript is
    thousands of lines nobody reads, and the previous version showed neither --
    a bare `claude -p` emits nothing until it exits, so a long import was
    indistinguishable from a hang. What a watcher actually needs is that it is
    alive, roughly how far along it is, and what it is touching right now.

    `total` is the census, so the counter reads against the denominator the
    reconcile will check. The tail is returned so the caller can pull the
    agent's JSON summary out of it.
    """
    binary = which_agent(agent)
    if not binary:
        name = AGENT_COPY[agent][0]
        return False, f"`{agent.value}` is not on PATH — install {name}, or pick the other agent"

    out = stream if stream is not None else sys.stdout
    live = progress and hasattr(out, "isatty") and out.isatty()
    state = Activity(total=total or 0)
    started = time.monotonic()
    tail: list[str] = []

    def paint() -> None:
        if not live:
            return
        # Centred like every other page of the wizard. Left at column 0 it read
        # as output from a different program running underneath the wizard,
        # which is exactly what it is not.
        from probe.cli import tui

        text = state.line(time.monotonic() - started)
        out.write("\r\033[2K" + " " * tui.left_pad() + text)
        out.flush()

    stop = threading.Event()

    def tick() -> None:
        # The spinner has to advance on its own: an agent can think for a minute
        # between events, and a frozen spinner is the thing we set out to fix.
        while not stop.wait(0.12):
            state.ticks += 1
            paint()

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed binary, no shell
            agent_argv(agent, binary, prompt, folder),
            cwd=str(folder),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)

    ticker = threading.Thread(target=tick, daemon=True)
    if live:
        ticker.start()

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line.rstrip("\n"))
            del tail[:-40]
            changed = fold_event(line, state)
            if live:
                if changed:
                    paint()
            elif changed:
                # No TTY (a pipe, a CI log): one plain line per step. Rewriting
                # with \r into a log file produces an unreadable single line.
                out.write(f"  {state.uploaded}/{state.total} · {state.doing}\n")
                out.flush()
            elif not progress:
                out.write(line)
                out.flush()
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "the agent did not finish in time; re-run to resume"
    except KeyboardInterrupt:
        # Ctrl-C should stop the agent, not orphan it holding the folder.
        proc.kill()
        return False, "interrupted"
    finally:
        # Always: an abandoned ticker keeps repainting over whatever the wizard
        # prints next, and the status line must not outlive the run either way.
        stop.set()
        if live:
            ticker.join(timeout=1.0)
            out.write("\r\033[2K")
            out.flush()

    return code == 0, "\n".join(tail)


def _rows(payload) -> list | None:
    """The item list out of a bare list, a page dict, or a Page object.

    dict is tested BEFORE the attribute lookup: `dict.items` is a bound method,
    so a getattr-first version reads every page dict as "no rows" and quietly
    counts zero.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = payload.get("items", payload)
    else:
        items = getattr(payload, "items", None)
    return items if isinstance(items, list) else None


def _project_id(client, project: str) -> str | None:
    """`project` as a UUID. A bare ref is a SLUG; ids arrive as `id:<uuid>`.

    ``/v1/projects/{project_id}/artifacts`` types its path param as a UUID, so a
    slug arrives as a 422 about UUID parsing rather than a lookup -- the same
    trap :func:`resolve_anchor` documents. It matters here because the slugs come
    from :func:`summary_projects`, i.e. the read-back is fed the one form the
    route cannot take, and the 422 is swallowed as "could not read back".
    """
    from . import refs

    try:
        # verify=False: this is a READ-BACK, so an `id:` ref needs no round trip
        # to confirm what it already states, and one fewer request per project.
        return refs.resolve(client, "project", project, verify=False).id
    except Exception:  # noqa: BLE001 - a read-back miss degrades, never raises here
        return None


def count_landed(client, project: str) -> tuple[int, bool]:
    """Artifacts under a project, INCLUDING its experiments. (count, at_least).

    Counting only the project anchor would undercount every import that did what
    it was told: step 3 of the prompt asks the agent to attach artifacts to an
    experiment where the evidence supports one, and an experiment-anchored
    artifact is not returned by the project listing. A faithful 204-file import
    that grouped 83 of them read back as 121, printing a 40% shortfall that was
    entirely an artifact of where the reconcile looked.

    One page per anchor, deliberately. Walking every page of a 12,000-artifact
    project to print one number would make the cheap half of this feature the
    slow half; hitting the page exactly reports "at least", which is honest,
    instead of a total that is quietly a page size.
    """
    from probe.sdk.client import Anchor

    try:
        project_id = _project_id(client, project)
        if project_id is None:
            return -1, False
        counted = _rows(client.list_anchored(Anchor.PROJECT, project_id, limit=RECONCILE_PAGE))
        if counted is None:
            return -1, False
        total = len(counted)
        at_least = total >= RECONCILE_PAGE

        experiments = _rows(client.list_experiments(project_id=project_id))
        for exp in experiments or []:
            exp_id = exp.get("id") if isinstance(exp, dict) else getattr(exp, "id", None)
            if not exp_id:
                continue
            rows = _rows(client.list_anchored(Anchor.EXPERIMENT, str(exp_id), limit=RECONCILE_PAGE))
            if rows is None:
                return -1, False
            total += len(rows)
            at_least = at_least or len(rows) >= RECONCILE_PAGE
        return total, at_least
    except Exception:  # noqa: BLE001 - reconcile must never fail the import
        return -1, False


def _embedded_summaries(text: str):
    """Every ``{...}`` in `text` that parses and carries a ``projects`` list.

    The summary does not arrive on a line of its own. Both agents are run with
    an EVENT STREAM (`agent_argv`), so the closing JSON is a STRING INSIDE an
    envelope -- `{"type":"result","result":"...done.\\n{...}"}` -- and the
    braces of the two are nested in one line of stdout. Scanning for balanced
    brace runs finds the inner object wherever it sits; matching only the whole
    line finds it exactly when the agent is NOT streaming, which is never.
    """
    import json

    def scan(blob: str):
        depth = 0
        start = -1
        for i, ch in enumerate(blob):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        data = json.loads(blob[start : i + 1])
                    except ValueError:
                        continue
                    if isinstance(data, dict) and isinstance(data.get("projects"), list):
                        yield data

    def strings(node):
        """Every string anywhere in a decoded envelope."""
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for value in node.values():
                yield from strings(value)
        elif isinstance(node, list):
            for value in node:
                yield from strings(value)

    yield from scan(text)
    # The inner object is a STRING FIELD of the envelope, so in the raw line its
    # quotes are backslash-escaped and no substring of it parses. It only becomes
    # JSON again once the envelope itself is decoded — hence the second pass over
    # the decoded strings rather than more clever matching on the raw text.
    try:
        envelope = json.loads(text)
    except ValueError:
        return
    for blob in strings(envelope):
        if "projects" in blob:
            yield from scan(blob)


def summary_projects(tail: str) -> list[str]:
    """The project slugs the agent says it filed into.

    Parsed from its closing JSON summary. This is the ONLY thing taken from the
    agent's own account of the run, and deliberately the least load-bearing one:
    it says WHERE to look, never how much landed. The count still comes from the
    server and the denominator still comes from the walk, so an agent that
    overstates its work cannot make the numbers agree — the gap just shows up.

    It says where to look, but nothing looks anywhere if this returns empty:
    with no pinned `--project` there is no fallback, so a parse miss here
    silently downgrades the whole run to "could not read back". That is what a
    top-level-only match did -- see :func:`_embedded_summaries`.
    """
    found: list[str] = []
    for line in reversed(tail.splitlines()):
        text = line.strip()
        if "projects" not in text:
            continue
        for data in _embedded_summaries(text):
            for slug in data["projects"]:
                if isinstance(slug, str) and slug and slug not in found:
                    found.append(slug)
        if found:
            return found
    return found


def count_landed_across(client, projects: list[str]) -> tuple[int, bool]:
    """Artifacts across every project the agent used. Returns (count, at_least).

    A project that cannot be read counts as unknown for the whole reconcile
    rather than silently zero: reporting a shortfall that is really a failed
    lookup would train people to ignore the one number this feature exists for.
    """
    if not projects:
        return -1, False
    total = 0
    at_least = False
    for slug in projects:
        count, capped = count_landed(client, slug)
        if count < 0:
            return -1, False
        total += count
        at_least = at_least or capped
    return total, at_least


def reconcile(census: Census, landed: int, at_least: bool) -> list[str]:
    """The denominator, printed. The whole point of enumerating first."""
    found = f"{census.files:,}{'+' if census.capped else ''}"
    if landed < 0:
        return [f"{found} files found — could not read back the project to confirm what landed."]
    shown = f"{landed:,}{'+' if at_least else ''}"
    lines = [f"{found} files found on disk · {shown} artifacts now on the project."]
    if not at_least and not census.capped and landed < census.files:
        lines.append(
            f"{census.files - landed:,} unaccounted for — build noise and caches are "
            "expected here; re-run to pick up anything genuinely missed."
        )
    return lines


# -- the folder picker ------------------------------------------------------


def choose_directory(start: Path):
    """Browse to the folder to import. Returns a Path, None (quit), or tui.BACK.

    The counts are the reason this exists. Typing a path is easy enough; what
    is not easy is knowing that `checkpoints/` is 2.9 TB before pointing an
    importer at it. Same stat walk the reconcile needs, spent up front where it
    can still change the answer.
    """
    import questionary

    from probe.cli import tui

    here = start.resolve()
    while True:
        children = subdirectories(here)
        own = scan(here)

        # THE PATH BAR, first row and selectable. Where you are and where you
        # can type are the same control: the path was already displayed above
        # the list as dead text, so putting the cursor on it is the shortest
        # route from "I have this on my clipboard" to done. Anyone arriving from
        # a cluster shell, a Slack message or the dashboard HAS the path.
        choices: list = [questionary.Separator(" ")]
        choices.append(
            questionary.Choice(
                title=f"{here}\n{tui.body_indent()}  enter to type a path · ~ works",
                value=("type", here),
            )
        )
        choices.append(questionary.Separator(" "))
        choices.append(
            questionary.Choice(
                title=f"Import this folder\n{tui.body_indent()}  {own.describe()}",
                value=here,
            )
        )
        choices.append(questionary.Separator(" "))
        for path, census in children[:40]:
            choices.append(
                questionary.Choice(
                    title=f"{path.name}/\n{tui.body_indent()}  {census.describe()}",
                    value=("cd", path),
                )
            )
        if here.parent != here:
            choices.append(questionary.Separator(" "))
            choices.append(questionary.Choice(title="../", value=("cd", here.parent)))

        # The path is the first row now, so repeating it in the block above the
        # question would print it twice on every screen.
        message = tui.framed(
            "Import existing work into Probe.",
            [],
            "Which folder?",
        )
        picked = tui.ask(
            questionary.select(
                message,
                choices=choices,
                instruction="(arrow keys, enter to choose, esc goes back)",
                style=tui.style(),
                qmark=tui.qmark(),
                pointer=tui.pointer(),
            ),
            height=tui.content_height(message, choices),
        )
        if picked is None or picked is tui.BACK:
            return picked
        if isinstance(picked, tuple):
            kind, target = picked
            if kind == "cd":
                here = target.resolve()
                continue
            typed = ask_path(target)
            if typed is None or typed is tui.BACK:
                continue  # back to the browser, not out of the picker
            # Land the browser ON it rather than importing blind: the counts are
            # the whole point of this screen, and a pasted path deserves them
            # too. `Import this folder` is then one keypress away.
            here = typed
            continue
        return picked


def ask_path(default: Path):
    """Prompt for a directory. Returns a resolved Path, None, or tui.BACK.

    Re-asks rather than failing: a path pasted from Slack or a cluster shell is
    routinely missing a segment or wrapped in quotes, and dropping the user back
    into a browser two directories away is a worse answer than asking again.
    """
    import questionary

    from probe.cli import tui

    while True:
        answer = tui.ask(
            questionary.path(
                tui.framed(
                    "Import existing work into Probe.",
                    tui.wrap("Paste or type the folder. `~` and relative paths work."),
                    "Path:",
                ),
                default=str(default),
                only_directories=True,
                style=tui.style(),
                qmark=tui.qmark(),
            )
        )
        if answer is None or answer is tui.BACK:
            return answer
        text = str(answer).strip().strip("'\"")
        if not text:
            return tui.BACK
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = (default / candidate).resolve()
        candidate = candidate.expanduser().resolve()
        if candidate.is_dir():
            return candidate
        tui.say()
        tui.say(f"{candidate} is not a folder." if candidate.exists() else f"No such folder: {candidate}")
        tui.say()


# -- the wizard action ------------------------------------------------------


def choose_agent(available: list[Agent]):
    """Pick the agent. Returns an Agent, None (quit), or tui.BACK.

    Only ever reached with a real choice to make -- `resolve_agent` answers it
    without asking when one agent is installed, which is the common case.
    """
    import questionary

    from probe.cli import tui

    choices: list = [questionary.Separator(" ")]
    for index, agent in enumerate(available):
        if index:
            choices.append(questionary.Separator(" "))
        title, detail = AGENT_COPY[agent]
        choices.append(
            questionary.Choice(title=f"{title}\n{tui.body_indent()}  {detail}", value=agent)
        )

    message = tui.framed(
        "Both agents are installed here.",
        tui.wrap("It reads the folder and uploads what it finds. They differ in what else they can do."),
        "Which agent should read it?",
    )
    return tui.ask(
        questionary.select(
            message,
            choices=choices,
            instruction="(arrow keys, enter to choose, esc goes back)",
            style=tui.style(),
            qmark=tui.qmark(),
            pointer=tui.pointer(),
        ),
        height=tui.content_height(message, choices),
    )


def resolve_agent(requested: Agent | None, *, interactive: bool) -> tuple[object, str | None]:
    """Which agent runs, as ``(choice, error)``.

    A TUPLE rather than a union return, because `Agent` is a `StrEnum` and
    therefore IS a str: an `isinstance(result, str)` check meant to catch the
    error case swallows every successful one too, and `run` then returns the
    enum where the caller expected output lines. Separate slots cannot be
    confused that way.

    `choice` is an Agent, None (quit), or tui.BACK. An explicit `--agent` wins
    and is NOT silently downgraded when that agent is missing: someone who named
    one wants that one, and quietly running the other over their filesystem is
    the wrong kind of helpful.
    """
    from probe.cli import tui

    if requested is not None:
        if which_agent(requested) is None:
            name = AGENT_COPY[requested][0]
            return None, (
                f"`{requested.value}` is not on PATH — install {name}, or drop --agent to pick."
            )
        return requested, None

    available = available_agents()
    if not available:
        return None, (
            "No coding agent found. Backfill needs Claude Code or Codex on PATH — "
            "the agent is what reads the folder."
        )
    if len(available) == 1 or not interactive:
        return available[0], None
    tui.clear()
    return choose_agent(available), None


def run(
    *,
    client_factory,
    start: Path | None = None,
    folder: Path | None = None,
    project: str | None = None,
    interactive: bool = True,
    agent: Agent | None = None,
) -> list[str]:
    """The `Import existing work` action. Returns the lines the wizard pages.

    `folder` skips the picker and `agent` skips the agent prompt, which is what
    makes this reachable from `--action backfill` on a box with no TTY.
    `project` names the destination explicitly; omitted, the FOLDER decides.
    """
    from probe.cli import tui

    target = folder
    if target is None:
        if not interactive:
            return ["Backfill needs a folder. Re-run interactively, or pass --folder."]
        picked = choose_directory(start or Path.cwd())
        if picked is None or picked is tui.BACK:
            return []
        target = picked

    # Order here is deliberate: cheapest and most certain first, and nothing
    # that MUTATES anything until every local check has passed.
    #
    #   1. the path            local, free — and telling someone to install an
    #                          agent when they mistyped a path is a wrong answer
    #   2. the census          local, cheap
    #   3. the agent           local, free — BEFORE the project, so a machine
    #                          with no agent cannot leave an empty project behind
    #   4. the project         network, CREATES a row
    #   5. the agent run
    target = Path(target).resolve()
    if not target.is_dir():
        return [f"{target} is not a directory."]

    census = scan(target)
    if census.files == 0:
        return [f"{target} has no files to import."]

    chosen, agent_error = resolve_agent(agent, interactive=interactive)
    if agent_error:
        return [agent_error]
    if chosen is None or chosen is tui.BACK:
        return []

    # A named --project is resolved HERE so a bad one fails before the agent
    # spends twenty minutes reading a folder. Unnamed, nothing is resolved:
    # the agent decides the projects, and the read-back finds out which.
    pinned = None
    if project:
        try:
            with client_factory() as client:
                _, pinned = resolve_anchor(client, target, requested=project)
        except Exception as exc:  # noqa: BLE001 - a credential problem is likely
            return [
                f"Could not resolve project {project!r}: {exc}",
                "Run `probe login`, or drop --project and let the agent choose.",
            ]

    prompt = build_prompt(folder=target, census=census, project=pinned, agent=chosen)

    tui.say()
    where = f"into project `{pinned}`" if pinned else "— the agent will choose the projects"
    tui.say(f"Importing {target} {where}.")
    tui.say(f"{census.describe()} — {AGENT_COPY[chosen][0]} is reading them now.")
    tui.say()

    ok, tail = launch_agent(target, prompt, agent=chosen, total=census.files)

    reported = summary_projects(tail) or ([pinned] if pinned else [])
    try:
        with client_factory() as client:
            landed, at_least = count_landed_across(client, reported)
    except Exception:  # noqa: BLE001 - never fail the import on the read-back
        landed, at_least = -1, False

    lines = [f"Imported {target}"]
    if reported:
        lines.append("Projects: " + ", ".join(reported))
    lines.append("")
    lines += reconcile(census, landed, at_least)
    if not reported:
        lines += [
            "The agent named no projects, so nothing could be counted back. "
            "Check `probe project list` before re-running."
        ]
    if not ok:
        lines += ["", f"The agent did not finish cleanly: {tail.splitlines()[-1] if tail else ''}"]
        lines += ["Re-running is safe — identical content is deduplicated server-side."]
    return lines
