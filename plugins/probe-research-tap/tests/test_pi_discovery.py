# tests/test_pi_discovery.py
import json
import os
import shutil
from pathlib import Path

from tap.pi_discovery import DEFAULT_ROOT, discover_session_files, is_pi_session_file, session_roots

_PI_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pi"
_CODEX_ROLLOUT_FIXTURE = Path(__file__).parent / "fixtures" / "rollout-sample.jsonl"


def _write(path, first_line):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(first_line) + "\n")
    return path


def test_recognizes_a_pi_session_by_its_header(tmp_path):
    p = _write(tmp_path / "s.jsonl",
               {"type": "session", "version": 3, "id": "u", "cwd": "/r"})
    assert is_pi_session_file(p) is True


def test_recognizes_real_pi_session_fixtures():
    # Real fixture files, not synthesized ones — same rationale as
    # test_reconcile.py's pi filename test: exercise the actual shape a pi
    # install writes, not a shape we imagined.
    fixtures = sorted(_PI_FIXTURES_DIR.glob("*.jsonl"))
    assert fixtures, f"no pi fixtures found under {_PI_FIXTURES_DIR}"
    for path in fixtures:
        assert is_pi_session_file(path) is True


def test_rejects_a_codex_rollout(tmp_path):
    p = _write(tmp_path / "r.jsonl",
               {"type": "session_meta", "payload": {}, "timestamp": "t"})
    assert is_pi_session_file(p) is False


def test_rejects_a_real_codex_rollout_fixture():
    # A real Codex rollout, not a synthesized session_meta line — the header
    # shape codex_sanitize itself is tested against.
    assert _CODEX_ROLLOUT_FIXTURE.exists(), _CODEX_ROLLOUT_FIXTURE
    assert is_pi_session_file(_CODEX_ROLLOUT_FIXTURE) is False


def test_rejects_an_empty_or_partial_file(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text("")
    assert is_pi_session_file(p) is False
    p.write_text('{"type": "sess')
    assert is_pi_session_file(p) is False


def test_rejects_a_file_with_a_non_dict_first_line(tmp_path):
    # Valid JSON, valid line — just not an object. `.get("type")` on a list
    # or a bare string would raise AttributeError if this weren't guarded.
    p = tmp_path / "list.jsonl"
    p.write_text("[1, 2, 3]\n")
    assert is_pi_session_file(p) is False


def test_rejects_a_missing_file(tmp_path):
    assert is_pi_session_file(tmp_path / "does-not-exist.jsonl") is False


def test_discovers_across_multiple_roots_including_a_fork(tmp_path, monkeypatch):
    # A rebranded fork writes to its own directory. Discovery is by SHAPE
    # across configured roots, never by one hardcoded path.
    upstream = _write(tmp_path / "pi" / "a.jsonl",
                      {"type": "session", "version": 3, "id": "1", "cwd": "/r"})
    fork = _write(tmp_path / "myfork" / "nested" / "b.jsonl",
                  {"type": "session", "version": 3, "id": "2", "cwd": "/r"})
    _write(tmp_path / "pi" / "notes.jsonl", {"type": "something-else"})

    monkeypatch.setenv("PROBE_PI_SESSION_ROOTS",
                       f"{tmp_path / 'pi'}:{tmp_path / 'myfork'}")
    found = set(discover_session_files())
    assert found == {upstream, fork}


def test_discovers_real_pi_fixtures_by_shape_not_filename(tmp_path, monkeypatch):
    # Copy the real fixtures plus a decoy .jsonl that is NOT a pi session
    # into one configured root, and confirm only the real sessions come back
    # — proving discovery filters by content, not by the *.jsonl glob alone.
    root = tmp_path / "sessions"
    root.mkdir()
    expected = set()
    for fixture in sorted(_PI_FIXTURES_DIR.glob("*.jsonl")):
        dest = root / fixture.name
        shutil.copy(fixture, dest)
        expected.add(dest)
    decoy = root / "not-a-pi-session.jsonl"
    shutil.copy(_CODEX_ROLLOUT_FIXTURE, decoy)

    monkeypatch.setenv("PROBE_PI_SESSION_ROOTS", str(root))
    assert set(discover_session_files()) == expected


def test_a_root_that_does_not_exist_is_skipped_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_PI_SESSION_ROOTS", str(tmp_path / "does-not-exist"))
    assert discover_session_files() == []


def test_one_missing_root_does_not_block_a_sibling_root(tmp_path, monkeypatch):
    real = _write(tmp_path / "real" / "s.jsonl",
                  {"type": "session", "version": 3, "id": "1", "cwd": "/r"})
    missing = tmp_path / "missing"
    monkeypatch.setenv("PROBE_PI_SESSION_ROOTS", f"{missing}{os.pathsep}{tmp_path / 'real'}")
    assert discover_session_files() == [real]


def test_deduplicates_a_file_reachable_through_two_overlapping_roots(tmp_path, monkeypatch):
    # A symlinked alias reaches the same physical file through a literally
    # different path; dedup must compare resolved identity, not the raw
    # path string, or the same session ships twice.
    real_dir = tmp_path / "real"
    session = _write(real_dir / "s.jsonl",
                     {"type": "session", "version": 3, "id": "1", "cwd": "/r"})
    alias = tmp_path / "alias"
    alias.symlink_to(real_dir)

    monkeypatch.setenv("PROBE_PI_SESSION_ROOTS", f"{real_dir}:{alias}")
    found = discover_session_files()
    assert len(found) == 1
    assert found[0] in (session, alias / "s.jsonl")


def test_session_roots_defaults_to_the_upstream_location(monkeypatch):
    monkeypatch.delenv("PROBE_PI_SESSION_ROOTS", raising=False)
    assert session_roots() == [DEFAULT_ROOT]


def test_session_roots_splits_on_pathsep(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("PROBE_PI_SESSION_ROOTS", f"{a}{os.pathsep}{b}")
    assert session_roots() == [a, b]


def test_session_roots_ignores_blank_segments(tmp_path, monkeypatch):
    # A trailing separator (a common shell-config typo) must not produce a
    # bogus empty-string root that then fails `.is_dir()` every scan.
    a = tmp_path / "a"
    monkeypatch.setenv("PROBE_PI_SESSION_ROOTS", f"{a}{os.pathsep}")
    assert session_roots() == [a]
