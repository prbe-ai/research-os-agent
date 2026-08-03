"""Which environment does a snapshot actually record?

The bug these pin down: `capture_env` enumerated the CALLING process's packages.
That is right for the SDK, which runs inside the training venv, and wrong for the
CLI, which is a uv-tool install with its own interpreter -- so `probe snapshot`
recorded typer/rich/questionary as the project's dependencies. `strict` refused
an EMPTY dep set, so the wrong one sailed through as a confident capture.

These build real throwaway venvs rather than mocking the resolution, because the
failure was precisely that the code looked right while reading the wrong prefix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from probe.sdk.snapshot import SnapshotError, capture_env, find_venv, venv_python


def _git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def _no_ambient_venv(monkeypatch):
    """`uv run` exports VIRTUAL_ENV, which is a legitimate fallback in
    `find_venv` and would mask every "nothing was found" assertion below."""
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)


def _make_venv(path, marker: str, version: str = "9.9.9"):
    """A real venv holding exactly one distribution, named `marker`.

    The marker is what makes these tests falsifiable: it exists in NO other
    environment, so its presence proves which prefix was actually read. Built
    with `--without-pip` on purpose -- `uv venv` ships no pip either, and the
    capture path must not depend on one.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:  # pragma: no cover - venv is stdlib, but be honest
        pytest.skip(f"could not create a venv: {proc.stderr}")
    site = next(iter((path / "lib").glob("python*"))) / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    dist_info = site / f"{marker.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {marker}\nVersion: {version}\n"
    )
    (dist_info / "RECORD").write_text("")
    return path


@pytest.fixture
def project(tmp_path):
    """A git repo with a real `.venv` holding one package the caller lacks."""
    work = tmp_path / "project"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "train.py").write_text("print('hi')\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _make_venv(work / ".venv", "only-in-project")
    return work


def _names(info):
    return {p.split("==")[0] for p in info["packages"]}


# --- detection --------------------------------------------------------------

def test_finds_the_project_venv_from_a_subdirectory(project):
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    root, via = find_venv(str(nested))
    assert root == str(project / ".venv")
    assert via == "project-venv"


def test_search_stops_at_the_git_toplevel(project, tmp_path):
    """A venv in a PARENT of the repo belongs to something else."""
    _make_venv(tmp_path / "outer.venv", "only-outside")
    inner = project / "nested"
    inner.mkdir()
    _git(inner, "init", "-q")  # its own repo, no venv of its own
    root, via = find_venv(str(inner))
    assert root is None and via is None


def test_virtual_env_is_used_when_the_project_has_no_venv(project, monkeypatch, tmp_path):
    activated = _make_venv(tmp_path / "activated", "only-activated")
    monkeypatch.setenv("VIRTUAL_ENV", str(activated))
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-q")
    root, via = find_venv(str(bare))
    assert root == str(activated) and via == "VIRTUAL_ENV"


# --- the actual bug ---------------------------------------------------------

def test_detect_venv_records_the_project_env_not_the_callers(project):
    """The regression test for `probe snapshot`: a foreign interpreter is read."""
    info = capture_env(str(project), detect_venv=True)

    assert "only-in-project" in _names(info), "did not read the project's venv"
    assert info["resolved_via"] == "project-venv"
    assert info["venv"] == str(project / ".venv")
    assert info["python_executable"] == venv_python(str(project / ".venv"))
    # ...and it is NOT this process's environment.
    assert "pytest" not in _names(info)
    assert os.path.realpath(info["venv"]) != os.path.realpath(sys.prefix)
    # Note what is deliberately NOT asserted: that the two INTERPRETERS differ.
    # Both `bin/python` symlink to the same base CPython, so their realpaths are
    # equal while the environments are unrelated. Any future shortcut that
    # decides "same interpreter, skip the subprocess" by resolving the
    # executable would therefore read the CALLER's packages here.
    assert os.path.realpath(info["python_executable"]) == os.path.realpath(sys.executable)


def test_a_frozen_interpreter_is_refused_rather_than_guessed(project, monkeypatch):
    """PyInstaller rewrites sys.executable to the bundled app, which does not
    understand `-c` -- it would re-run the app. Refuse, name the escape hatch."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(SnapshotError, match="frozen interpreter"):
        capture_env(str(project))


def test_in_process_default_still_records_this_interpreter(project):
    """The SDK path must not change: run.snapshot() IS the training env."""
    info = capture_env(str(project))
    assert info["resolved_via"] == "interpreter"
    assert info["venv"] is None
    assert "pytest" in _names(info)
    assert "only-in-project" not in _names(info)


def test_explicit_venv_wins_over_detection(project, tmp_path):
    other = _make_venv(tmp_path / "other", "only-explicit")
    info = capture_env(str(project), venv=str(other), detect_venv=True)
    assert info["resolved_via"] == "explicit"
    assert info["venv"] == str(other)
    assert "only-explicit" in _names(info)
    assert "only-in-project" not in _names(info)


def test_explicit_non_venv_path_is_refused(project, tmp_path):
    with pytest.raises(SnapshotError, match="not a virtualenv"):
        capture_env(str(project), venv=str(tmp_path))


# --- strict now covers "wrong", not just "empty" -----------------------------

def test_strict_refuses_to_record_a_foreign_interpreter(tmp_path):
    """No project venv + an outside interpreter == the exact silent-wrong case."""
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-q")
    with pytest.raises(SnapshotError, match="refusing to record"):
        capture_env(str(bare), detect_venv=True)


def test_non_strict_degrades_but_says_so(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-q")
    info = capture_env(str(bare), detect_venv=True, strict=False)
    assert info["resolved_via"] == "unresolved-fallback"


def test_interpreter_inside_the_project_is_an_acceptable_fallback(project, monkeypatch):
    """`probe` pip-installed into the project's own env: no venv dir to find,
    but sys.prefix lives in the tree, so it is the project's environment."""
    monkeypatch.setattr("probe.sdk.snapshot.find_venv", lambda cwd=None: (None, None))
    monkeypatch.setattr(sys, "prefix", str(project / ".venv"))
    info = capture_env(str(project), detect_venv=True)
    assert info["resolved_via"] == "interpreter"
    assert "pytest" in _names(info)


def test_a_broken_interpreter_raises_rather_than_recording_nothing(project):
    broken = project / ".venv" / "bin" / "python"
    if not broken.exists():  # pragma: no cover - Windows layout
        pytest.skip("posix venv layout only")
    broken.unlink()
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    with pytest.raises(SnapshotError, match="could not enumerate"):
        capture_env(str(project), detect_venv=True)


# --- shape ------------------------------------------------------------------

def test_provenance_is_always_recorded(project):
    for kwargs in ({}, {"detect_venv": True}):
        info = capture_env(str(project), **kwargs)
        assert set(info) >= {
            "python",
            "python_executable",
            "venv",
            "resolved_via",
            "packages",
            "package_count",
            "packages_sha256",
        }
        assert info["package_count"] == len(info["packages"])


def test_identity_carries_no_machine_specific_paths(project):
    """`deps` is hashed into the execution record's content_hash, so a venv path
    in it would make two identical environments at different paths produce
    different env_refs -- and env_ref equality is what a reader compares to ask
    'same environment?'. Provenance belongs on the artifact meta instead."""
    from probe.sdk.snapshot import split_env_provenance

    identity, provenance = split_env_provenance(capture_env(str(project), detect_venv=True))

    assert set(provenance) == {"venv", "python_executable", "resolved_via"}
    assert provenance["venv"] == str(project / ".venv")
    # identity is what the environment IS, and nothing else
    assert set(identity) == {"python", "packages", "package_count", "packages_sha256"}
    blob = json.dumps(identity)
    assert str(project) not in blob and sys.prefix not in blob


def test_split_is_total_and_lossless(project):
    """Every key lands in exactly one side -- a new field added to capture_env
    must be classified deliberately, not silently hashed."""
    from probe.sdk.snapshot import split_env_provenance

    info = capture_env(str(project))
    identity, provenance = split_env_provenance(info)
    assert identity.keys() | provenance.keys() == info.keys()
    assert not (identity.keys() & provenance.keys())


# --- _enumerate_foreign error paths -----------------------------------------

def test_a_hanging_interpreter_times_out_instead_of_wedging_the_launch(monkeypatch):
    """Snapshot runs at the start of a training run; an unbounded wait here
    stalls the experiment rather than failing it."""
    from probe.sdk import snapshot as S

    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="python", timeout=S._ENUMERATE_TIMEOUT)

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(SnapshotError, match="did not report its packages"):
        S._enumerate_foreign(sys.executable)


def test_an_unexecutable_interpreter_is_reported_not_swallowed(monkeypatch):
    from probe.sdk import snapshot as S

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("Exec format error"))
    )
    with pytest.raises(SnapshotError, match="could not run"):
        S._enumerate_foreign(sys.executable)


def test_unparseable_output_is_refused_rather_than_recorded_as_empty(monkeypatch):
    """A garbled stdout must not degrade into 'zero packages, captured fine'."""
    from probe.sdk import snapshot as S

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(["python"], 0, "not json", ""),
    )
    with pytest.raises(SnapshotError, match="unreadable package list"):
        S._enumerate_foreign(sys.executable)


def test_pythonhome_is_stripped_from_the_child(monkeypatch):
    """PYTHONHOME repoints an interpreter's stdlib at another installation's,
    which breaks a foreign interpreter outright. Everything else is inherited
    on purpose -- PYTHONPATH genuinely contributes modules to the run."""
    from probe.sdk import snapshot as S

    seen: dict = {}
    real = subprocess.run

    def spy(cmd, **kw):
        seen.update(kw.get("env") or {})
        return real(cmd, **kw)

    monkeypatch.setenv("PYTHONHOME", "/nonexistent/should/be/stripped")
    monkeypatch.setattr(subprocess, "run", spy)
    S._enumerate_foreign(sys.executable)
    assert "PYTHONHOME" not in seen
    assert "PATH" in seen, "the rest of the environment is inherited on purpose"


# --- remaining find_venv branches -------------------------------------------

def test_conda_prefix_is_the_last_resort(tmp_path, monkeypatch):
    conda = _make_venv(tmp_path / "conda-env", "only-conda")
    monkeypatch.setenv("CONDA_PREFIX", str(conda))
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-q")
    assert find_venv(str(bare)) == (str(conda), "CONDA_PREFIX")


def test_outside_a_git_repo_the_search_does_not_climb(tmp_path):
    """No repo means no ceiling to stop at, so only cwd itself is considered --
    otherwise a directory with no venv would adopt an unrelated parent's."""
    _make_venv(tmp_path / ".venv", "only-parent")
    child = tmp_path / "child"
    child.mkdir()
    assert find_venv(str(child)) == (None, None)


def test_foreign_python_version_comes_from_the_foreign_interpreter(project):
    info = capture_env(str(project), detect_venv=True)
    expected = subprocess.run(
        [info["python_executable"], "-c",
         "import sys;print('.'.join(str(p) for p in sys.version_info[:3]))"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert info["python"] == expected
