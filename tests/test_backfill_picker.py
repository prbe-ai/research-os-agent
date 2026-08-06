"""The folder picker: it lists folders, and that is all it does.

`subdirectories` had NO test coverage before this file, and `choose_directory`
is guarded only by two `inspect.getsource` greps -- which is why a change to
the return type of one and the row-building of the other passed a green suite.
"""

from __future__ import annotations

from pathlib import Path

from probe.cli import backfill


def _tree(root, *names):
    for n in names:
        (root / n).mkdir(parents=True, exist_ok=True)
    return root


def test_every_child_is_listed_with_no_cap(tmp_path):
    """The old `[:40]` slice hid the rest silently. On the shared drive this
    feature exists for, a folder you cannot see reads as one that is not there."""
    _tree(tmp_path, *[f"researcher_{i:03d}" for i in range(60)])
    assert len(backfill.subdirectories(tmp_path)) == 60


def test_children_come_back_as_paths_not_censused_tuples(tmp_path):
    _tree(tmp_path, "michael")
    got = backfill.subdirectories(tmp_path)
    assert got == [tmp_path / "michael"]
    assert all(isinstance(p, Path) for p in got)


def test_listing_children_never_walks_into_them(tmp_path, monkeypatch):
    """The reason the cap existed: every child was walked RECURSIVELY before the
    screen could draw, paid again on every keypress that changes directory."""
    _tree(tmp_path, "a", "b")
    (tmp_path / "a" / "deep").mkdir()
    (tmp_path / "a" / "deep" / "f.bin").write_text("x")

    calls = []
    real = backfill.scan
    monkeypatch.setattr(backfill, "scan", lambda *a, **k: calls.append(a) or real(*a, **k))
    backfill.subdirectories(tmp_path)
    assert calls == [], "the picker must not census its children"


def test_build_noise_and_dotdirs_are_not_offered(tmp_path):
    _tree(tmp_path, "keep", "__pycache__", ".git", "node_modules", ".venv")
    assert [p.name for p in backfill.subdirectories(tmp_path)] == ["keep"]


def test_children_are_sorted_so_the_list_is_stable(tmp_path):
    _tree(tmp_path, "zeta", "alpha", "mid")
    assert [p.name for p in backfill.subdirectories(tmp_path)] == ["alpha", "mid", "zeta"]


def test_files_are_not_offered_as_folders(tmp_path):
    _tree(tmp_path, "adir")
    (tmp_path / "afile.txt").write_text("x")
    assert [p.name for p in backfill.subdirectories(tmp_path)] == ["adir"]


def test_an_unreadable_directory_lists_as_empty_rather_than_raising(tmp_path, monkeypatch):
    def boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert backfill.subdirectories(tmp_path) == []


def test_the_folder_you_are_standing_in_is_still_counted(tmp_path):
    """`own = scan(here)` stays -- capped and fast. It answers the one question
    this screen asks, and the import runs its own uncapped census afterwards."""
    (tmp_path / "a.txt").write_text("x" * 10)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("y" * 5)
    census = backfill.scan(tmp_path)
    assert census.files == 2 and census.bytes == 15


def test_the_browse_census_stays_capped(tmp_path):
    """Capped on purpose here: you want the cursor to move now, and '20,000+'
    answers the only question a browsing screen asks."""
    for i in range(12):
        (tmp_path / f"f{i}.txt").write_text("x")
    census = backfill.scan(tmp_path, cap=5)
    assert census.capped is True and census.files == 5
    assert census.describe().startswith("5+ files")
