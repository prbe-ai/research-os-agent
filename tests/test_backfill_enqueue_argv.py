"""The enqueue argv has to be a command the CLI actually accepts.

This is the step where bytes finally move, and it shipped broken: the argv
carried `--async` AFTER the subcommand, but `--async` is a ROOT option. Typer
answered "Error: No such option: --async" for every manifest, so a real import
of 204 files read them, described them, wrote six manifests, reported 6/6 units
done -- and uploaded nothing.

Nothing caught it because every test of this path mocks `subprocess.run`, so the
argv was only ever compared against a string another test also wrote. The fix is
to hand it to the REAL parser: these tests take the argv `enqueue_manifests`
builds and parse it with the same click command the CLI runs, so an option that
does not exist fails here instead of in someone's import.
"""

from __future__ import annotations

import json


from probe.cli import backfill_ledger as bl
from probe.cli import backfill_run as br


def _argv_for(tmp_path) -> list[str]:
    """The argv `enqueue_manifests` would hand to a subprocess.

    Scoped with `mock.patch`, NOT monkeypatch. The parse test needs to run a
    real subprocess afterwards, and `monkeypatch.undo()` is all-or-nothing --
    it reverted conftest's config isolation along with this patch, which is how
    a test meant to escape a mock ended up defeating the sandbox instead.
    """
    from unittest import mock
    manifest = tmp_path / "u-1.jsonl"
    manifest.write_text(json.dumps({"path": "a.py", "notes": "n"}) + "\n")
    outcome = br.UnitOutcome(
        unit=bl.Unit(unit_id="u-1", project="odyssey", paths=("a.py",)),
        ok=True, manifest=manifest, rows=1,
    )

    seen: list[list[str]] = []

    class _Done:
        stdout = json.dumps({"enqueued": 1, "failures": []})
        stderr = ""

    def fake_run(argv, **kw):
        seen.append(list(argv))
        return _Done()

    # Patched GLOBALLY: enqueue_manifests imports subprocess inside the
    # function, so there is no module attribute on `br` to replace.
    with mock.patch("subprocess.run", fake_run):
        br.enqueue_manifests(tmp_path, [outcome], project_of={"u-1": "odyssey"})
    assert seen, "enqueue_manifests did not shell out at all"
    return seen[0]


def _cli_args(argv: list[str]) -> list[str]:
    """Just the probe arguments: drop the interpreter and `-c <program>`."""
    return argv[3:]


def test_the_enqueue_argv_parses_against_the_real_cli(tmp_path):
    """The regression, checked by the thing that actually rejected it.

    IT HAS TO RUN THE REAL BINARY. The obvious version of this test --
    `get_command(app).make_context(...)` -- is worthless here and was written
    that way first: a root group's `make_context` parses ROOT options and stops
    at the subcommand name, so `--async` sitting after `artifact add` is never
    looked at and the test passes against the exact bug it exists for.

    So this runs `probe` for real and reads what it says. `--from-manifest`
    points at a file that does not exist, which fails AFTER parsing -- that is
    the point: a parse error and a missing file are different exits, and only
    one of them is the bug.
    """
    import subprocess
    import sys

    args = _cli_args(_argv_for(tmp_path))
    args[args.index("--from-manifest") + 1] = str(tmp_path / "definitely-absent.jsonl")
    proc = subprocess.run(  # noqa: S603 - fixed interpreter, no shell
        [sys.executable, "-c",
         "import sys; from probe.cli import main; sys.exit(main(sys.argv[1:]))",
         *args],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert "No such option" not in output, (
        f"the enqueue argv is not a valid probe command:\n{output}"
    )
    assert "Usage:" not in output, f"the CLI rejected the argv:\n{output}"


def test_the_enqueue_argv_carries_the_manifest_and_the_anchor(tmp_path):
    """Both are load-bearing and neither is obvious from the argv.

    Rows carry no anchor key, so without `--project` every row is rejected for
    want of one and the command exits 1 having enqueued nothing -- a zero-import
    wearing a well-formed error."""
    args = _cli_args(_argv_for(tmp_path))
    assert args[:2] == ["artifact", "add"]
    assert "--from-manifest" in args
    assert args[args.index("--project") + 1] == "odyssey"


def test_async_is_never_passed_after_the_subcommand(tmp_path):
    """`--async` is a ROOT option. After `artifact add` it is not a flag with a
    different meaning, it is a parse error -- and it took a whole import down.

    It is not wanted before the subcommand either: `--from-manifest` queues to
    the outbox and lets the drainer deliver it regardless."""
    args = _cli_args(_argv_for(tmp_path))
    assert "--async" not in args


def test_the_cwd_is_the_imported_folder(tmp_path, monkeypatch):
    """A manifest row's `path` is relative to the folder and the reader resolves
    it against the process working directory. Anywhere else, every row fails
    "is not a regular file" -- or worse, a same-named file under the wrong cwd
    uploads the wrong bytes under the right name."""
    manifest = tmp_path / "u-1.jsonl"
    manifest.write_text(json.dumps({"path": "a.py"}) + "\n")
    outcome = br.UnitOutcome(
        unit=bl.Unit(unit_id="u-1", project="odyssey", paths=("a.py",)),
        ok=True, manifest=manifest, rows=1,
    )
    cwds: list = []

    class _Done:
        stdout = json.dumps({"enqueued": 1})
        stderr = ""

    monkeypatch.setattr(
        "subprocess.run", lambda argv, **kw: cwds.append(kw.get("cwd")) or _Done(),
    )
    br.enqueue_manifests(tmp_path, [outcome], project_of={})
    assert cwds == [str(tmp_path)]
