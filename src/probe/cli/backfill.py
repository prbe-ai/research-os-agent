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
from dataclasses import dataclass
from pathlib import Path

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

#: The agent may call the probe CLI and READ files. It may not write, delete,
#: or reach the network by any other route. Backfill is a read-and-upload job,
#: so anything broader is blast radius with no upside -- and this runs
#: unattended over folders whose contents nobody has audited.
AGENT_TOOLS = "Bash(probe:*),Read,Glob,Grep,Task"


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


def resolve_anchor(client, folder: Path, *, configured: str | None = None) -> str:
    """The project this folder imports into. Created if it does not exist.

    An explicitly configured active project always wins -- someone who ran
    `probe project use` has already answered this question, and silently
    importing somewhere else would be the wrong kind of helpful.
    """
    if configured:
        return configured
    slug = slug_for(folder)
    existing = client.resolve_project(slug)
    if existing:
        return existing.get("slug") or slug
    client.create_project(slug, name=folder.resolve().name)
    return slug


def build_prompt(*, folder: Path, project: str, census: Census) -> str:
    """The prompt the wizard hands the agent.

    This is the actual deliverable of the feature: the user never writes it,
    and every rule that keeps the import honest lives here rather than in code
    the agent cannot see. The negative rules matter most -- fixed anchor,
    bounded scope, and explicit permission NOT to group. An agent that declines
    to invent an experiment is doing the right thing; files at the project
    level are findable, and a wrong experiment is worse than no experiment.
    """
    return f"""\
You are backfilling ONE folder of existing research work into Probe.

FOLDER: {folder}
This folder and its subdirectories are your entire scope. Do not read outside it.
A deterministic walk counted {census.files:,} files here — roughly what you should
expect to account for.

ANCHOR (fixed — do not change, do not create another project):
    --project {project}

Step 1 — upload everything of substance.

    files under {human_bytes(REFERENCE_ABOVE_BYTES)}:
        probe artifact add --project {project} <path>

    files over {human_bytes(REFERENCE_ABOVE_BYTES)} (checkpoints, datasets, archives):
        probe artifact add --project {project} <path> --reference --allow-missing

    The second form records the PATH and uploads no bytes. Use it for anything
    large. Do NOT pass --hash on those: fingerprinting a 10GB file over a shared
    mount costs minutes and buys nothing here.

    Skip build noise and caches (.git, __pycache__, .venv, node_modules).

Step 2 — say what things are.

    For each artifact that carries meaning — a report, a result table, a config,
    a plot, a script someone would look for again — record a one-line description:
    what it is, what produced it, what it shows. Use `probe note add` or --meta.
    This is the part that makes a file findable later, and it is the part nobody
    did at the time. It matters more than the upload.

Step 3 — group ONLY if the evidence is there.

    If this folder plainly IS one experiment — a hypothesis, a method, a result
    you can point at — create it and attach the artifacts to it.
    If that would be a guess, DO NOT. Leave everything at the project level.
    Artifacts at the project level are findable; an invented experiment is a
    wrong answer that looks like a right one.

If the folder is large or clearly splits into several independent pieces of work,
subdivide it and use subagents — one per piece, each with this same anchor.

Finish with a JSON summary on its own line:
{{"files_seen": N, "files_landed": N, "files_skipped": N, "experiments_created": N,
  "summary": "one sentence on what this folder contains"}}
"""


def launch_agent(
    folder: Path,
    prompt: str,
    *,
    timeout: float | None = None,
    stream=None,
) -> tuple[bool, str]:
    """Run one headless Claude agent inside `folder`. Streams as it goes.

    Streamed rather than captured because this is the long step -- minutes to
    hours -- and a wizard that prints nothing until it finishes is
    indistinguishable from a wizard that has hung. The tail is returned so the
    caller can pull the agent's JSON summary out of it.
    """
    binary = shutil.which("claude")
    if not binary:
        return False, "`claude` is not on PATH — install Claude Code to use backfill"

    out = stream if stream is not None else sys.stdout
    tail: list[str] = []
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed binary, no shell
            [binary, "-p", prompt, "--allowedTools", AGENT_TOOLS],
            cwd=str(folder),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            out.write(line)
            out.flush()
            tail.append(line.rstrip("\n"))
            del tail[:-40]
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "the agent did not finish in time; re-run to resume"
    except KeyboardInterrupt:
        # Ctrl-C should stop the agent, not orphan it holding the folder.
        proc.kill()
        return False, "interrupted"

    return code == 0, "\n".join(tail)


def count_landed(client, project: str) -> tuple[int, bool]:
    """Artifacts now sitting at the anchor. Returns (count, at_least).

    One page, deliberately. Walking every page of a 12,000-artifact project to
    print one number would make the cheap half of this feature the slow half;
    hitting the page exactly reports "at least", which is honest, instead of a
    total that is quietly a page size.
    """
    try:
        rows = client.list_anchored(_project_anchor(), project, limit=RECONCILE_PAGE)
    except Exception:  # noqa: BLE001 - reconcile must never fail the import
        return -1, False
    items = rows.get("items", rows) if isinstance(rows, dict) else rows
    if not isinstance(items, list):
        return -1, False
    return len(items), len(items) >= RECONCILE_PAGE


def _project_anchor():
    from probe.sdk.client import Anchor

    return Anchor.PROJECT


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

        choices: list = [questionary.Separator(" ")]
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

        message = tui.framed(
            "Import existing work into Probe.",
            tui.wrap(str(here)),
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
            here = picked[1].resolve()
            continue
        return picked


# -- the wizard action ------------------------------------------------------


def run(
    *,
    client_factory,
    start: Path | None = None,
    folder: Path | None = None,
    configured_project: str | None = None,
    interactive: bool = True,
) -> list[str]:
    """The `Import existing work` action. Returns the lines the wizard pages.

    `folder` skips the picker, which is what makes this reachable from
    `--action backfill` on a box with no TTY.
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

    target = Path(target).resolve()
    if not target.is_dir():
        return [f"{target} is not a directory."]

    census = scan(target)
    if census.files == 0:
        return [f"{target} has no files to import."]

    try:
        with client_factory() as client:
            project = resolve_anchor(client, target, configured=configured_project)
    except Exception as exc:  # noqa: BLE001 - a credential problem is the likely cause
        return [
            f"Could not resolve a project to import into: {exc}",
            "Run `probe login` (or `probe project use <slug>`) and try again.",
        ]

    prompt = build_prompt(folder=target, project=project, census=census)

    tui.say()
    tui.say(f"Importing {target} into project `{project}`.")
    tui.say(f"{census.describe()} — the agent is reading them now.")
    tui.say()

    ok, tail = launch_agent(target, prompt)

    try:
        with client_factory() as client:
            landed, at_least = count_landed(client, project)
    except Exception:  # noqa: BLE001 - never fail the import on the read-back
        landed, at_least = -1, False

    lines = [f"Imported {target}", f"Project: {project}", ""]
    lines += reconcile(census, landed, at_least)
    if not ok:
        lines += ["", f"The agent did not finish cleanly: {tail.splitlines()[-1] if tail else ''}"]
        lines += ["Re-running is safe — identical content is deduplicated server-side."]
    return lines
